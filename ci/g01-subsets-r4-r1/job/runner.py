#!/usr/bin/env python3
"""Pinned M CPU acceptance runner. --rehearse is safe on the host."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

JOB = Path(__file__).resolve().parent
POD_ROOT = Path("/opt/M-02-r7-cpu-acceptance")
WORKSPACE = Path("/workspace/jobs/M-02-r7-cpu-acceptance")
LOCAL_OUTPUT = Path("/home/user/astrid/ws-M/manager/evidence/M-02-r7")
PINS = {
    "agent": ("75ad0e6ea1e443a249df406f91379f88ad66ca3c", "9c23b8761ee95f292ca966603e43e9e8f714c602"),
    "runtime": ("1a275bb339ae897e52a5be5e1da99e846e402037", "cf857d0d7f1432d32aae27b79fc1a8f87449d417"),
    "vibecomfy": ("3bebae39655eb460454885040be4d3aefaf2ef31", "c174b8b465e8e18852398c5de04cbf19d413c308"),
    "lifecycle": ("96360a27a53c4608f780f7b370f747a2aa3c7ca8", "24e3562181f0ad64003d5cef5403b15d16415be6"),
}
SOURCES = {
    "agent": "https://github.com/banodoco/astrid-agent.git",
    "runtime": "https://github.com/banodoco/astrid-runtime.git",
    "vibecomfy": "https://github.com/AstridPseudon/VibeComfy-proof.git",
    "lifecycle": "https://github.com/banodoco/runpod-lifecycle.git",
}
PROVISIONAL_TUPLE = {
    "status": "PROVISIONAL rehearsal refs only; not an M-INT coherent tuple",
    "runtime": "1a275bb339ae897e52a5be5e1da99e846e402037",
    "agent": "75ad0e6ea1e443a249df406f91379f88ad66ca3c",
    "vibecomfy": "3bebae39655eb460454885040be4d3aefaf2ef31",
    "lifecycle": "96360a27a53c4608f780f7b370f747a2aa3c7ca8",
    "coherent_tuple_manifest": "PENDING M-INT; do not substitute this rehearsal map",
}
SUITES: dict[str, list[str]] = {
    "agent-focused": [
        "tests/core/generation/test_public_route_guard.py",
        "tests/core/generation/test_engine_execution.py",
        "tests/core/generation/test_engine_runner.py",
        "tests/core/generation/backends/test_qwen_21_turbo.py",
        "tests/core/generation/test_cli.py",
        "tests/core/generation/test_cli_installed_subprocess.py",
        "tests/core/execution/test_child_bridge_target_transport_265.py",
        "tests/test_generic_host_result_json.py",
        "tests/packs/h3_av/test_operation.py",
        "tests/scripts/test_pod_action_source.py",
        "tests/scripts/test_pod_h3_install.py",
    ],
    "agent-generation-journey": ["tests/core/generation/test_generate_target_journey.py"],
    "agent-installed-h3-boundary": ["tests/packs/h3_av/test_character_swap_cpu_boundary.py"],
    "runtime-focused": [
        "tests/test_exact_delegated_child_claim.py",
        "tests/test_child_target_preservation.py",
        "tests/test_default_child_claim.py",
        "tests/test_generation_publish_v1_settlement.py",
        "tests/test_generated_client.py",
        "tests/test_h3_child_claim_diagnostic.py",
        "tests/test_prepared_child_owner_custody.py",
        "tests/test_runtime_child_delegation.py",
        "tests/test_runtime_delegated_expiry_custody.py",
        "tests/test_runtime_local_composition.py",
    ],
    "runtime-typescript-client": ["npm", "test"],
    "vibecomfy-touched": [
        "tests/test_schema_validate.py",
        "tests/test_target_model_approval.py",
        "tests/test_target_model_bundle_custody.py",
        "tests/test_entries.py",
        "tests/test_native_qwen_bindings.py",
        "tests/test_runtime_server_client.py",
    ],
}
RUNS: list[dict[str, object]] = []
TAP_SUMMARY_FIELDS = ("tests", "pass", "fail", "cancelled", "skipped")


class PreflightError(RuntimeError):
    """A setup prerequisite failed before any acceptance suite was started."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(f"PREFLIGHT_{code}: {detail}")


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def preflight_run(code: str, command: list[str], **kwargs: object) -> None:
    """Run a bounded setup check and retain a machine-readable failure code."""
    try:
        subprocess.run(command, check=True, **kwargs)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(code, str(exc)) from exc


def preflight_output(code: str, command: list[str], **kwargs: object) -> str:
    """Read a setup probe while retaining a machine-readable failure code."""
    try:
        return subprocess.check_output(command, text=True, **kwargs)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PreflightError(code, str(exc)) from exc


def require_preflight(code: str, condition: bool, detail: str) -> None:
    if not condition:
        raise PreflightError(code, detail)


def parse_tap_counts(text: str) -> dict[str, int]:
    """Return the actual Node TAP summary, rejecting an incomplete summary."""
    parsed: dict[str, int] = {}
    for field in TAP_SUMMARY_FIELDS:
        matches = re.findall(rf"(?m)^\s*#\s+{re.escape(field)}\s+(\d+)\s*$", text)
        if len(matches) != 1:
            raise RuntimeError(f"raw TAP summary has {len(matches)} {field!r} counts; expected exactly one")
        parsed[field] = int(matches[0])
    accounted = parsed["pass"] + parsed["fail"] + parsed["cancelled"] + parsed["skipped"]
    if accounted != parsed["tests"]:
        raise RuntimeError(
            "raw TAP summary counts do not add up: "
            f"tests={parsed['tests']} pass={parsed['pass']} fail={parsed['fail']} "
            f"cancelled={parsed['cancelled']} skipped={parsed['skipped']}"
        )
    return parsed


def verify_rehearsal_artifacts() -> list[str]:
    return verify_uploaded_artifacts()


def validate_test_paths(roots: dict[str, Path]) -> list[str]:
    mapping = {
        "agent-focused": ("agent", SUITES["agent-focused"]),
        "agent-generation-journey": ("agent", SUITES["agent-generation-journey"]),
        "agent-installed-h3-boundary": ("agent", SUITES["agent-installed-h3-boundary"]),
        "runtime-focused": ("runtime", SUITES["runtime-focused"]),
        "vibecomfy-touched": ("vibecomfy", SUITES["vibecomfy-touched"]),
    }
    errors: list[str] = []
    for suite, (repo, paths) in mapping.items():
        for relative in paths:
            if not (roots[repo] / relative).is_file():
                errors.append(f"{suite}: missing test path {roots[repo] / relative}")
    return errors


def verify_uploaded_artifacts() -> list[str]:
    try:
        manifest = json.loads((JOB / "artifact-manifest.json").read_text())
        expected = {"run.sh", "runner.py"}
        if set(manifest.get("files", {})) != expected:
            return ["artifact manifest file set mismatch"]
        return [f"artifact hash mismatch: {name}" for name in expected
                if hashlib.sha256((JOB / name).read_bytes()).hexdigest() != manifest["files"][name]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"artifact manifest validation failed: {exc}"]


def write_progress(message: str) -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(WORKSPACE).free < 2 * 1024**3:
        raise RuntimeError("disk free space below 2 GiB floor")
    with (WORKSPACE / "progress.log").open("a", encoding="utf-8") as stream:
        stream.write(f"{stamp()} {message}\n")


def command_run(command: list[str], cwd: Path, env: dict[str, str], log: Path, timeout: int | None = None) -> tuple[int, float]:
    start = time.monotonic()
    with log.open("w", encoding="utf-8") as out:
        out.write(f"[{stamp()}] cwd={cwd}\nargv={shlex.join(command)}\n")
        out.flush()
        proc = subprocess.run(command, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
    return proc.returncode, time.monotonic() - start


def clean_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    for key in list(env):
        if key == "PYTHONPYCACHEPREFIX" or key.startswith(("ASTRID_", "VIBECOMFY_")):
            env.pop(key)
    return env


def execute() -> int:
    if os.environ.get("M02_POD_EXECUTION") != "1":
        raise SystemExit("--execute is pod-only")
    errors = verify_uploaded_artifacts()
    if errors:
        raise RuntimeError("artifact integrity check failed: " + "; ".join(errors))
    OUT = WORKSPACE / "out"
    OUT.mkdir(parents=True, exist_ok=True)
    WORK = POD_ROOT / "src"
    TMP = POD_ROOT / "tmp"
    CACHE = POD_ROOT / "cache"
    VENV = POD_ROOT / "venv"
    LAUNCHERS = POD_ROOT / "launchers"
    for directory in (WORK, TMP, CACHE, LAUNCHERS, VENV):
        directory.mkdir(parents=True, exist_ok=True)
    write_progress("source extraction started")
    roots: dict[str, Path] = {}
    for name, url in SOURCES.items():
        target = WORK / name
        target.mkdir(parents=True)
        expected_head, expected_tree = PINS[name]
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(["git", "-C", str(target), "fetch", "-q", "--depth=1", url, expected_head], check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "-q", "--detach", "FETCH_HEAD"], check=True)
        if git(target, "rev-parse", "HEAD") != expected_head or git(target, "rev-parse", "HEAD^{tree}") != expected_tree:
            raise RuntimeError(f"extracted {name} source identity mismatch")
        roots[name] = target
    (OUT / "source-identities.json").write_text(json.dumps({name: {"commit": git(root, "rev-parse", "HEAD"), "tree": git(root, "rev-parse", "HEAD^{tree}"), "url": SOURCES[name]} for name, root in roots.items()}, indent=2) + "\n")
    errors = validate_test_paths(roots)
    if errors:
        raise RuntimeError("test inventory validation failed: " + "; ".join(errors))
    write_progress("source extraction complete; setup started")

    # Match the Linux media/font prerequisites used by the Agent CI lanes.
    write_progress("Linux CI prerequisite setup started")
    subprocess.run(["apt-get", "update"], check=True)
    subprocess.run(["apt-get", "install", "-y", "ffmpeg", "fonts-dejavu-core"], check=True)

    # Match the product CI's pinned .node-version before any package builds can
    # invoke node (notably the VibeComfy schema build).
    node_version = (roots["agent"] / ".node-version").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", node_version):
        raise RuntimeError(f"invalid pinned Node version in Agent .node-version: {node_version!r}")
    machine = subprocess.check_output(["uname", "-m"], text=True).strip()
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(machine)
    if arch is None:
        raise RuntimeError(f"unsupported architecture for pinned Node download: {machine}")
    node_root = POD_ROOT / "node"
    node_root.mkdir(parents=True, exist_ok=True)
    node_archive = node_root / f"node-v{node_version}-linux-{arch}.tar.xz"
    node_base = f"https://nodejs.org/dist/v{node_version}"
    preflight_run("NODE_DOWNLOAD", ["curl", "-fsSLo", str(node_archive), f"{node_base}/{node_archive.name}"])
    sums = preflight_output("NODE_CHECKSUM_LIST", ["curl", "-fsSL", f"{node_base}/SHASUMS256.txt"])
    expected_sum = next((line.split()[0] for line in sums.splitlines() if line.endswith(node_archive.name)), None)
    require_preflight("NODE_CHECKSUM", expected_sum is not None and hashlib.sha256(node_archive.read_bytes()).hexdigest() == expected_sum, f"pinned Node archive SHA-256 verification failed: {node_archive}")
    preflight_run("NODE_EXTRACT", ["tar", "-xJf", str(node_archive), "-C", str(node_root), "--strip-components=1"])
    node_bin = node_root / "bin"
    base_env = clean_env()
    base_env["PATH"] = f"{node_bin}{os.pathsep}{base_env.get('PATH', '')}"
    require_preflight("NODE_PATH", (node_bin / "node").is_file() and (node_bin / "npm").is_file(), f"missing node/npm after extraction under {node_bin}")
    node = preflight_output("NODE_VERSION", [str(node_bin / "node"), "--version"], env=base_env).strip().lstrip("v")
    npm = preflight_output("NPM_VERSION", [str(node_bin / "npm"), "--version"], env=base_env).strip()
    require_preflight("NODE_VERSION", node == node_version and bool(npm), f"Node/npm verification failed: node={node!r} expected={node_version!r} npm={npm!r}")
    write_progress(f"pinned Node verified: node={node}; npm={npm}")

    setup_home = POD_ROOT / "home" / "setup"
    setup_tmp = TMP / "setup"
    setup_home.mkdir(parents=True, exist_ok=True)
    setup_tmp.mkdir(parents=True, exist_ok=True)
    # Fresh venv probes must not inherit or set a global pycache prefix: its
    # out-of-tree RECORD paths invalidate the packaging-origin check.
    base_env.update({"HOME": str(setup_home), "PIP_CACHE_DIR": str(CACHE / "pip"), "PIP_DISABLE_PIP_VERSION_CHECK": "1", "TMPDIR": str(setup_tmp), "TMP": str(setup_tmp), "TEMP": str(setup_tmp)})
    python = shutil.which("python3") or sys.executable
    for key, root in roots.items():
        env_dir = VENV / key
        preflight_run(f"{key.upper()}_VENV", [python, "-m", "venv", str(env_dir)], env=base_env)
    agent_py = VENV / "agent" / "bin" / "python"
    runtime_py = VENV / "runtime" / "bin" / "python"
    vibe_py = VENV / "vibecomfy" / "bin" / "python"
    # Resolve each environment before installing; make the unpublished lifecycle
    # pin available locally before VibeComfy's runpod-launch extra is resolved.
    for py in (agent_py, runtime_py, vibe_py):
        preflight_run("PIP_BOOTSTRAP", [str(py), "-m", "pip", "install", "--upgrade", "pip"], env=base_env)
    preflight_run("AGENT_LIFECYCLE_INSTALL", [str(agent_py), "-m", "pip", "install", "--no-deps", "-e", str(roots["lifecycle"])], env=base_env)
    preflight_run("VIBECOMFY_LIFECYCLE_INSTALL", [str(vibe_py), "-m", "pip", "install", "--no-deps", "-e", str(roots["lifecycle"])], env=base_env)
    preflight_run("VIBECOMFY_LIFECYCLE_PRESENT", [str(vibe_py), "-m", "pip", "show", "runpod-lifecycle"], env=base_env, stdout=subprocess.DEVNULL)
    write_progress("fail-fast Agent dependency resolution started")
    preflight_run("AGENT_DEPENDENCY_RESOLUTION", [str(agent_py), "-m", "pip", "install", "--dry-run", "-e", ".[dev]", "-e", str(roots["vibecomfy"])], cwd=roots["agent"], env=base_env)
    write_progress("fail-fast Runtime dependency resolution started")
    preflight_run("RUNTIME_DEPENDENCY_RESOLUTION", [str(runtime_py), "-m", "pip", "install", "--dry-run", "-e", ".[test]"], cwd=roots["runtime"], env=base_env)
    write_progress("fail-fast VibeComfy dependency resolution started")
    preflight_run("VIBECOMFY_DEPENDENCY_RESOLUTION", [str(vibe_py), "-m", "pip", "install", "--dry-run", "--extra-index-url", "https://nodes.appmana.com/simple/", "-e", ".[dev,runpod-launch,comfy]", "lazy-object-proxy>=1.10", "frozendict>=2.4", "pillow>=10", "ConfigArgParse>=1.7.1"], cwd=roots["vibecomfy"], env=base_env)
    write_progress("Agent CI dependency setup started")
    subprocess.run([str(agent_py), "-m", "pip", "install", "-e", ".[dev]"], cwd=roots["agent"], check=True, env=base_env)
    subprocess.run([str(agent_py), "-m", "pip", "install", "--no-deps", "-e", str(roots["runtime"]), "-e", str(roots["runtime"] / "packages/python")], check=True, env=base_env)
    subprocess.run([str(agent_py), "-m", "pip", "install", "-e", str(roots["vibecomfy"])], check=True, env=base_env)
    write_progress("Runtime CI dependency setup started")
    subprocess.run([str(runtime_py), "-m", "pip", "install", "-e", ".[test]"], cwd=roots["runtime"], check=True, env=base_env)
    write_progress("VibeComfy CI dependency setup started; lifecycle already installed")
    preflight_run("VIBECOMFY_CI_INSTALL", ["make", "install-ci", f"PYTHON={vibe_py}"], cwd=roots["vibecomfy"], env=base_env)
    for label, py in (("Agent", agent_py), ("Runtime", runtime_py), ("VibeComfy", vibe_py)):
        preflight_run(f"{label.upper()}_PIP_CHECK", [str(py), "-m", "pip", "check"], env=base_env)
        write_progress(f"{label} pip check passed")

    # Catch a wrong interpreter, missing dependency, or incompatible pytest
    # plugin before spending time on the acceptance suites.
    for label, py, imports in (
        ("Agent", agent_py, ("runtime_protocol", "vibecomfy", "runpod_lifecycle", "PIL", "pytest")),
        ("Runtime", runtime_py, ("runtime_protocol", "pytest")),
        ("VibeComfy", vibe_py, ("vibecomfy", "runpod_lifecycle", "PIL", "pytest")),
    ):
        preflight_run(f"{label.upper()}_IMPORTS", [str(py), "-c", "; ".join(f"import {name}" for name in imports)], env=base_env)
        preflight_run(f"{label.upper()}_PYTEST", [str(py), "-m", "pytest", "--version"], env=base_env, stdout=subprocess.DEVNULL)
        write_progress(f"{label} imports and pytest plugin entry point verified")

    runs = RUNS
    runs.clear()
    def run_pytest(name: str, repo: str, paths: list[str], marker: str | None = None, extra: list[str] | None = None, env_extra: dict[str, str] | None = None, importlib: bool = False, pytest_console: bool = False) -> None:
        run_tmp = TMP / name
        basetemp = run_tmp / "pytest"
        run_tmp.mkdir(parents=True, exist_ok=True)
        xml = OUT / f"{name}.xml"
        log = OUT / f"{name}.log"
        launcher = LAUNCHERS / f"{name}.sh"
        py = {"agent-focused": agent_py, "agent-generation-journey": agent_py, "agent-installed-h3-boundary": VENV / "installed" / "bin" / "python", "runtime-focused": runtime_py, "vibecomfy-touched": vibe_py}[name]
        argv = [str(py.parent / "pytest")] if pytest_console else [str(py), "-m", "pytest"]
        argv.extend(["-q", "--junitxml", str(xml), "--basetemp", str(basetemp)])
        if importlib:
            argv.extend(["--import-mode=importlib"])
        if marker:
            argv.extend(["-m", marker])
        if extra:
            argv.extend(extra)
        argv.extend(paths)
        launcher.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexec " + shlex.join(argv) + "\n", encoding="utf-8")
        launcher.chmod(0o700)
        env = clean_env(base_env)
        env.update({"TMPDIR": str(run_tmp), "TEMP": str(run_tmp), "TMP": str(run_tmp), "PYTEST_ADDOPTS": "", "HOME": str(POD_ROOT / "home" / name)})
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        if env_extra:
            env.update(env_extra)
        start_iso = stamp()
        code, seconds = command_run(["bash", str(launcher)], roots[repo], env, log, timeout=7200)
        counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        if xml.exists():
            suite = ET.parse(xml).getroot()
            counts = {key: sum(int(child.attrib.get(key, 0)) for child in suite) if suite.tag == "testsuites" else int(suite.attrib.get(key, 0)) for key in counts}
        runs.append({"suite": name, "command": shlex.join(argv), "exit_code": code, "counts": counts, "junit": str(xml), "log": str(log), "start": start_iso, "elapsed_seconds": round(seconds, 3)})
        write_progress(f"{name} finished exit={code} counts={counts} seconds={seconds:.1f}")
        if counts["tests"] == 0 and code == 0:
            runs[-1]["exit_code"] = 1
            write_progress(f"{name} failed: zero tests collected")

    agent_files = SUITES["agent-focused"]
    agent_focused_env = {"ASTRID_RUNTIME_CHECKOUT": str(roots["runtime"])}
    run_pytest("agent-focused", "agent", agent_files, env_extra=agent_focused_env)
    agent_env = {"ASTRID_CI_JOB": "lanes-integration", "ASTRID_RUNTIME_CHECKOUT": str(roots["runtime"]), "ASTRID_VIBECOMFY_CHECKOUT": str(roots["vibecomfy"]), "PYTHONPATH": os.pathsep.join((str(roots["runtime"]), str(roots["runtime"] / "packages/python"), str(roots["vibecomfy"]))) }
    run_pytest("agent-generation-journey", "agent", SUITES["agent-generation-journey"], marker="integration", env_extra=agent_env)

    # Use the exact installed-wheel workflow route: build.lock, one wheel, Runtime client from
    # this tuple, and Remotion dependencies/profile. No product installer or models are invoked.
    installed = VENV / "installed"
    preflight_run("INSTALLED_VENV", [python, "-m", "venv", str(installed)], env=base_env)
    build_py = installed / "bin" / "python"
    preflight_run("INSTALLED_PIP_BOOTSTRAP", [str(build_py), "-m", "pip", "install", "--upgrade", "pip"], env=base_env)
    preflight_run("INSTALLED_BUILD_LOCK_RESOLUTION", [str(build_py), "-m", "pip", "install", "--dry-run", "--require-hashes", "-r", "requirements/build.lock"], cwd=roots["agent"], env=base_env)
    subprocess.run([str(build_py), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--require-hashes", "-r", "requirements/build.lock"], cwd=roots["agent"], check=True, env=base_env)
    dist = POD_ROOT / "dist"
    dist.mkdir(exist_ok=True)
    for f in dist.glob("*.whl"):
        f.unlink()
    subprocess.run([str(build_py), "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist)], cwd=roots["agent"], check=True, env=base_env)
    wheels = list(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel, found {len(wheels)}")
    subprocess.run([str(build_py), "-m", "pip", "install", "--dry-run", str(wheels[0]) + "[dev]"], check=True, env=base_env)
    subprocess.run([str(build_py), "-m", "pip", "install", str(wheels[0]) + "[dev]"], check=True, env=base_env)
    subprocess.run([str(build_py), "-m", "pip", "install", "--no-deps", str(roots["runtime"]), str(roots["runtime"] / "packages/python")], check=True, env=base_env)
    subprocess.run([str(build_py), "-m", "pip", "install", "--no-deps", "-e", str(roots["lifecycle"])], check=True, env=base_env)
    subprocess.run([str(build_py), "-m", "pip", "check"], check=True, env=base_env)
    remotion = roots["agent"] / "remotion"
    node = subprocess.check_output(["node", "--version"], text=True, env=base_env).strip().lstrip("v")
    if node != node_version:
        raise RuntimeError(f"installed Node version changed during run: {node} != {node_version}")
    subprocess.run(["npm", "ci", "--prefix", str(remotion)], check=True, env=base_env)
    subprocess.run(["npx", "remotion", "browser", "ensure"], cwd=remotion, check=True, env=base_env)
    installed_env = {"ASTRID_CI_JOB": "lanes-installed", "ASTRID_RUNTIME_CHECKOUT": str(roots["runtime"]), "ASTRID_HOST_EXECUTION_PROFILE": "installed", "ASTRID_REMOTION_PROJECT_DIR": str(remotion)}
    # CI points PATH at the wheel venv and runs this exact installed selector.
    installed_py = installed / "bin" / "python"
    installed_pytest = installed / "bin" / "pytest"
    if not installed_pytest.is_file():
        raise RuntimeError(f"installed pytest console script is missing: {installed_pytest}")
    run_pytest("agent-installed-h3-boundary", "agent", SUITES["agent-installed-h3-boundary"], marker="installed", env_extra=installed_env, importlib=True, pytest_console=True)

    run_pytest("runtime-focused", "runtime", SUITES["runtime-focused"])
    run_pytest("vibecomfy-touched", "vibecomfy", SUITES["vibecomfy-touched"])

    # TypeScript package's own CI command is a single generated-client test surface.
    ts_tmp = TMP / "runtime-typescript-client"
    ts_tmp.mkdir(parents=True, exist_ok=True)
    ts_log = OUT / "runtime-typescript-client.log"
    ts_junit = OUT / "runtime-typescript-client.xml"
    ts_launcher = LAUNCHERS / "runtime-typescript-client.sh"
    ts_launcher.write_text("#!/usr/bin/env bash\nset -euo pipefail\nnpm ci\nnpm test\n", encoding="utf-8")
    ts_launcher.chmod(0o700)
    ts_env = clean_env(base_env)
    ts_env.update({"TMPDIR": str(ts_tmp), "TMP": str(ts_tmp), "TEMP": str(ts_tmp), "HOME": str(POD_ROOT / "home/runtime-typescript-client")})
    Path(ts_env["HOME"]).mkdir(parents=True, exist_ok=True)
    ts_start = stamp()
    ts_code, ts_seconds = command_run(["bash", str(ts_launcher)], roots["runtime"] / "packages/typescript", ts_env, ts_log, timeout=3600)
    # Convert the actual Node TAP summary into JUnit while retaining full TAP in the raw log.
    text = ts_log.read_text(encoding="utf-8", errors="replace")
    tap = parse_tap_counts(text)
    total, passed, failed = tap["tests"], tap["pass"], tap["fail"]
    cancelled, skipped = tap["cancelled"], tap["skipped"]
    root = ET.Element("testsuite", name="runtime-typescript-client", tests=str(total), failures=str(failed), errors=str(cancelled), skipped=str(skipped), time=f"{ts_seconds:.3f}")
    for idx in range(total):
        case = ET.SubElement(root, "testcase", classname="runtime-typescript-client", name=f"node-test-{idx+1}")
        if failed and idx == 0:
            ET.SubElement(case, "failure", message="npm test failed", type="ProcessFailure").text = "See raw log."
        elif failed <= idx < failed + cancelled:
            ET.SubElement(case, "error", message="npm test cancelled", type="ProcessCancelled").text = "See raw log."
        elif failed + cancelled <= idx < failed + cancelled + skipped:
            ET.SubElement(case, "skipped", message="npm test skipped")
    ET.ElementTree(root).write(ts_junit, encoding="utf-8", xml_declaration=True)
    runs.append({"suite": "runtime-typescript-client", "command": "npm ci && npm test (Runtime CI package contract)", "exit_code": ts_code, "counts": {"tests": total, "failures": failed, "errors": cancelled, "skipped": skipped}, "junit": str(ts_junit), "log": str(ts_log), "start": ts_start, "elapsed_seconds": round(ts_seconds, 3), "passed": passed, "tap": tap})
    write_progress(f"runtime-typescript-client finished exit={ts_code} tests={total} seconds={ts_seconds:.1f}")

    summary = {
        "job": "M-02-r7-cpu-acceptance", "completed_at": stamp(), "pins": {k: {"head": v[0], "tree": v[1]} for k, v in PINS.items()},
        "artifact_sha256": json.loads((JOB / "artifact-manifest.json").read_text())["files"], "provisional_tuple": PROVISIONAL_TUPLE, "binding": "rehearsal; rebind/invalidate at I-08/I-12",
        "runs": runs, "ok": all(r["exit_code"] == 0 for r in runs),
        "note": "Focused CPU acceptance only. No GPU generation, installer, credentials, or private media.",
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    write_progress(f"all suites done ok={summary['ok']}")
    return 0 if summary["ok"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--rehearse", action="store_true")
    group.add_argument("--syntax-check", action="store_true")
    group.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.syntax_check:
        import ast
        ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=str(Path(__file__)))
        print("Python syntax: OK")
        return 0
    if args.rehearse:
        errors = verify_rehearsal_artifacts()
        errors.extend(validate_test_paths({name: JOB.parent / "tmp" / "sources" / name for name in SOURCES}))
        for name, root in ((name, JOB.parent / "tmp" / "sources" / name) for name in SOURCES):
            if git(root, "rev-parse", "HEAD") != PINS[name][0] or git(root, "rev-parse", "HEAD^{tree}") != PINS[name][1]:
                errors.append(f"{name}: source identity mismatch")
        print("REHEARSAL FAILED: " + "; ".join(errors) if errors else "REHEARSAL OK: script hashes, pinned source heads/trees, suite paths")
        return int(bool(errors))
    try:
        return execute()
    except Exception as exc:
        OUT = WORKSPACE / "out"
        OUT.mkdir(parents=True, exist_ok=True)
        diagnostic = f"{stamp()} execution failed: {type(exc).__name__}: {exc}\n"
        (OUT / "failure.log").write_text(diagnostic, encoding="utf-8")
        summary = {"job": "M-02-r7-cpu-acceptance", "completed_at": stamp(), "ok": False, "failure": diagnostic.strip(), "runs": RUNS}
        (OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        try:
            write_progress(diagnostic.strip())
        except OSError:
            pass
        print(diagnostic, file=sys.stderr, end="")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
