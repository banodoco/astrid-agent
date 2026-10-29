import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pod_bootstrap.sh"


def test_syntax_ok():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_strict_mode_and_marker():
    text = SCRIPT.read_text()
    assert "set -euo pipefail" in text
    assert '${ASTRID_BOOTSTRAP_ROOT:-/workspace/astrid/bootstrap}' in text
    assert 'marker_dir="$BOOTSTRAP_ROOT/$digest"' in text
    assert 'marker="$marker_dir/ok"' in text
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RUNPOD" in text
    assert "1.12.0+astrid.2" in text
    assert "3.12.10" in text


def test_no_secret_variables():
    text = SCRIPT.read_text()
    assert not re.search(r"(KEY|TOKEN|SECRET|PASSWORD|BEARER|CREDENTIAL)", text, re.I)
    assert not re.search(r"\b(sk|rpa|ghp|hf)_[A-Za-z0-9]{8,}", text)


VENV = Path(sys.prefix)
RUNTIME_BIN = VENV / "bin" / "astrid-runtime"


def _setup_runtime_function() -> str:
    text = SCRIPT.read_text()
    start = text.index("setup_runtime() {")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


def _run_setup(tmp_path: Path, runtime_bin: Path, venv: Path) -> subprocess.CompletedProcess:
    src = tmp_path / "src"
    (src / "astrid-runtime").mkdir(parents=True, exist_ok=True)
    (src / "astrid-agent").mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "ASTRID_RUNTIME_DATA_ROOT": str(tmp_path / "data"),
        "ASTRID_REALM_ROOT": str(tmp_path / "ws" / "realm"),
    }
    for key in ("PYTHONPATH", "ASTRID_VIBECOMFY_CHECKOUT"):
        if key in os.environ:
            env[key] = os.environ[key]
    script = (
        "set -euo pipefail\n"
        f'VENV="{venv}"\n'
        'DATA_ROOT="$ASTRID_RUNTIME_DATA_ROOT"\nREALM_ROOT="$ASTRID_REALM_ROOT"\n'
        + _setup_runtime_function()
        + "setup_runtime\n"
    )
    return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True)


def test_setup_runtime_is_called_after_install_and_never_starts_a_runtime():
    text = SCRIPT.read_text()
    assert text.index("uv pip install") < text.rindex("setup_runtime\n\nmkdir")
    body = _setup_runtime_function()
    assert "workspace create" in body
    assert not re.search(r"astrid-runtime\"? +up\b", body)


def test_setup_runtime_writes_profile_and_is_idempotent(tmp_path):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    calls = tmp_path / "calls.log"
    stub = venv / "bin" / "astrid-runtime"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{calls}"\n'
        'while [ $# -gt 0 ]; do [ "$1" = --realm-root ] && { mkdir -p "$2"; : > "$2/realm.sqlite3"; }; shift; done\n'
    )
    stub.chmod(0o755)

    first = _run_setup(tmp_path, stub, venv)
    assert first.returncode == 0, first.stderr
    profile = tmp_path / "data" / "runtime" / "source-profiles" / "astrid.json"
    assert json.loads(profile.read_text()) == {
        "profile": "astrid",
        "runtime_checkout": str(tmp_path / "src" / "astrid-runtime"),
        "source_checkout": str(tmp_path / "src" / "astrid-agent"),
        "runtime_environment": str(venv),
    }
    assert len(calls.read_text().splitlines()) == 1
    assert "--data-root" in calls.read_text()

    second = _run_setup(tmp_path, stub, venv)
    assert second.returncode == 0, second.stderr
    assert "already current" in second.stdout and "already exists" in second.stdout
    assert len(calls.read_text().splitlines()) == 1

    profile.write_text("{}")
    third = _run_setup(tmp_path, stub, venv)
    assert third.returncode == 0, third.stderr
    assert json.loads(profile.read_text())["profile"] == "astrid"


@pytest.mark.skipif(not RUNTIME_BIN.exists(), reason="astrid-runtime not installed in this environment")
def test_setup_runtime_with_real_runtime_creates_workspace_once(tmp_path):
    first = _run_setup(tmp_path, RUNTIME_BIN, VENV)
    assert first.returncode == 0, first.stderr + first.stdout
    assert '"configured_create"' in first.stdout
    assert (tmp_path / "ws" / "realm" / "realm.sqlite3").exists()
    second = _run_setup(tmp_path, RUNTIME_BIN, VENV)
    assert second.returncode == 0, second.stderr + second.stdout
    assert "configured_create" not in second.stdout


def test_vibecomfy_is_in_noneditable_package_step_before_marker():
    text = SCRIPT.read_text()
    assert "SRC_DIRS=(src/runpod-python src/astrid-runtime src/runpod-lifecycle src/astrid-agent src/VibeComfy)" in text
    package_step = 'staged_uv_install fresh "${SRC_DIRS[@]}"'
    assert text.count(package_step) == 1
    assert text.index('[ -f "$marker" ]') < text.index(package_step) < text.index('mkdir -p "$marker_dir"')
    install_lines = re.findall(r"^\s*uv pip install[^\n]*", text, re.M)
    assert all(not re.search(r"(?:^|\s)(?:-e|--editable)(?:[=\s]|$)", line) for line in install_lines)


TOOLS = ["find", "sort", "xargs", "sha256sum", "cut", "date", "mkdir", "dirname", "grep", "id", "cat", "env", "bash", "chmod", "ln", "tar", "mktemp", "rm"]
PROBE_MODULES = ("engine_runner.py", "engine_runner_target.py")
PROBE_TEXT = {name: f"# {name} from src/astrid-agent\n" for name in PROBE_MODULES}


def _site(venv: Path) -> Path:
    return venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def _fake_venv(tmp_path: Path, copies: dict | None) -> Path:
    """A venv whose interpreter reports its own site-packages, optionally holding astrid copies."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    (venv / "pyvenv.cfg").write_text(f"home = {Path(sys.executable).resolve().parent}\n")
    stub = venv / "bin" / "astrid-runtime"  # what the fake uv venv command writes, for setup_runtime
    stub.write_text(
        "#!/bin/sh\nwhile [ $# -gt 0 ]; do [ \"$1\" = --realm-root ] && { mkdir -p \"$2\"; : > \"$2/realm.sqlite3\"; }; shift; done\n"
    )
    stub.chmod(0o755)
    if copies is not None:
        gen = _site(venv) / "astrid" / "core" / "generation"
        gen.mkdir(parents=True, exist_ok=True)
        for name, text in copies.items():
            (gen / name).write_text(text)
    return venv


def _uv_calls(log: Path) -> list:
    return log.read_text().splitlines() if log.exists() else []


def _line_index(calls: list, prefix: str) -> int:
    matches = [i for i, c in enumerate(calls) if c.startswith(prefix)]
    assert len(matches) == 1, (prefix, calls)
    return matches[0]


def _tool_bin(tmp_path: Path, with_uv: bool) -> Path:
    import shutil

    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    for t in TOOLS:
        if not (b / t).exists():
            (b / t).symlink_to(shutil.which(t))
    curl = b / "curl"
    curl.write_text(f'#!/bin/sh\necho "$@" >> "{tmp_path / "curl.log"}"\nexit 1\n')
    curl.chmod(0o755)
    if with_uv:
        uv = b / "uv"
        uv.write_text(
            "#!/usr/bin/env bash\n"
            '[ -z "${UV_LOG:-}" ] || echo "$*" >> "$UV_LOG"\n'
            'if [ -n "${FAKE_UV_WRITE_LOG:-}" ] && [ "$1 $2" = "pip install" ]; then\n'
            '  for arg in "$@"; do case "$arg" in */src/*)\n'
            '    : > "$arg/.fake-build" || exit 7; echo "$arg" >> "$FAKE_UV_WRITE_LOG"\n'
            '    [ -z "${FAKE_UV_FAIL_BUILD:-}" ] || exit 8;; esac; done\n'
            'fi\n'
            'case "$1 $2" in\n'
            '  "pip list") if [ -n "${FAKE_UV_FREEZE:-}" ]; then cat "$FAKE_UV_FREEZE"; else exit 1; fi;;\n'
            '  "venv "*) for a; do v="$a"; done; mkdir -p "$v/bin"; ln -sf "$PY" "$v/bin/python"\n'
            '    printf \'#!/bin/sh\\nwhile [ $# -gt 0 ]; do [ "$1" = --realm-root ] && { mkdir -p "$2"; : > "$2/realm.sqlite3"; }; shift; done\\n\' > "$v/bin/astrid-runtime"\n'
            '    chmod +x "$v/bin/astrid-runtime";;\n'
            "esac\nexit 0\n"
        )
        uv.chmod(0o755)
    return b


def _sources(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    for d in ("runpod-python", "astrid-runtime", "runpod-lifecycle", "astrid-agent", "VibeComfy"):
        (src / d).mkdir(parents=True, exist_ok=True)
        (src / d / "f.txt").write_text(d)
    gen = src / "astrid-agent" / "astrid" / "core" / "generation"
    gen.mkdir(parents=True, exist_ok=True)
    for name in PROBE_MODULES:
        (gen / name).write_text(PROBE_TEXT[name])
    return src


def _boot(tmp_path: Path, extra: dict, with_uv=True) -> subprocess.CompletedProcess:
    _sources(tmp_path)
    env = {
        "PATH": str(_tool_bin(tmp_path, with_uv)),
        "HOME": str(tmp_path / "home"),
        "PY": sys.executable,
        "ASTRID_RUNTIME_DATA_ROOT": str(tmp_path / "data"),
    }
    env.update(extra)
    return subprocess.run(["/bin/bash", str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True)


def _workspace_snapshot() -> list:
    ws = Path("/workspace")
    return sorted(str(p) for p in ws.rglob("*")) if ws.exists() else []


def _overrides(tmp_path: Path) -> dict:
    return {
        "ASTRID_BOOTSTRAP_ROOT": str(tmp_path / "boot"),
        "ASTRID_REALM_ROOT": str(tmp_path / "realm"),
        "ASTRID_VENV": str(tmp_path / "venv"),
    }


def test_bootstrap_root_override_writes_marker_and_avoids_workspace(tmp_path):
    before = _workspace_snapshot()
    r = _boot(tmp_path, _overrides(tmp_path))
    assert r.returncode == 0, r.stderr + r.stdout
    assert len(list((tmp_path / "boot").glob("*/ok"))) == 1
    assert "/workspace" not in r.stdout + r.stderr
    assert _workspace_snapshot() == before
    again = _boot(tmp_path, _overrides(tmp_path))
    assert again.returncode == 0 and "already done" in again.stdout
    assert _workspace_snapshot() == before


def test_realm_and_venv_overrides_are_honoured(tmp_path):
    r = _boot(tmp_path, _overrides(tmp_path))
    assert r.returncode == 0, r.stderr + r.stdout
    assert (tmp_path / "venv" / "bin" / "python").exists()
    assert (tmp_path / "realm" / "realm.sqlite3").exists()
    profile = json.loads((tmp_path / "data" / "runtime" / "source-profiles" / "astrid.json").read_text())
    assert profile["runtime_environment"] == str(tmp_path / "venv")


def test_uv_missing_fails_clearly_without_curl(tmp_path):
    r = _boot(tmp_path, _overrides(tmp_path), with_uv=False)
    assert r.returncode != 0
    assert "uv not found" in r.stderr
    assert not (tmp_path / "curl.log").exists()
    assert not list((tmp_path / "boot").glob("*/ok"))


def test_unwritable_root_fails_clearly(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    r = _boot(tmp_path, {"ASTRID_BOOTSTRAP_ROOT": str(blocker / "sub")})
    assert r.returncode != 0
    assert "cannot create or write" in r.stderr and "does not chown" in r.stderr


def test_no_hardcoded_cache_paths_or_curl():
    text = SCRIPT.read_text()
    assert "/root" not in text
    assert '${UV_CACHE_DIR:-$DATA_ROOT/uv-cache}' in text
    assert '${UV_PYTHON_INSTALL_DIR:-$DATA_ROOT/uv-python}' in text
    assert "curl" not in text


def _probe_function_body(text: str) -> str:
    start = text.index("verify_astrid_install() {")
    return text[start : text.index("\n}\n", start) + 3]


def test_pip_step_is_unindented_after_package_install_and_before_marker():
    text = SCRIPT.read_text()
    # Unindented means it runs whether or not the packages were already installed (the if/else above).
    pip_line = re.compile(r'^uv pip install --python "\$VENV" pip$', re.M)
    assert len(pip_line.findall(text)) == 1
    package_step = 'staged_uv_install fresh "${SRC_DIRS[@]}"'
    assert text.index(package_step) < pip_line.search(text).start() < text.index('mkdir -p "$marker_dir"')


def test_probe_check_runs_on_rerun_and_after_package_install_and_never_cleans_engine_state():
    text = SCRIPT.read_text()
    assert "PROBE_MODULES=(engine_runner.py engine_runner_target.py)" in text
    calls = [m.start() for m in re.finditer(r"^\s*verify_astrid_install$", text, re.M)]
    assert len(calls) == 2
    rerun, full = calls
    package_step = 'staged_uv_install fresh "${SRC_DIRS[@]}"'
    assert text.index('if [ -f "$marker" ]') < rerun < text.index("  setup_runtime\n  exit 0")
    assert text.index(package_step) < full < text.index('mkdir -p "$marker_dir"')
    body = _probe_function_body(text)
    assert "sha256sum" in body and "staged_uv_install reinstall src/astrid-agent" in body
    # The bootstrap never deletes engine state; stale state is documented in SETUP.md instead.
    assert text.count('rm -rf "$stage"') == 1
    assert "rm -" not in text.replace('rm -rf "$stage"', "") and ".astrid-engine" not in text


def test_fresh_boot_orders_noneditable_package_pip_and_probe_check(tmp_path):
    log = tmp_path / "uv.log"
    r = _boot(tmp_path, {**_overrides(tmp_path), "UV_LOG": str(log)})
    assert r.returncode == 0, r.stderr + r.stdout
    calls = _uv_calls(log)
    venv = tmp_path / "venv"
    pkg = _line_index(calls, f"pip install --python {venv} {tmp_path}/boot/.install.")
    pip = _line_index(calls, f"pip install --python {venv} pip")
    probe = _line_index(calls, f"pip install --python {venv} --reinstall-package astrid ")
    args = shlex.split(calls[pkg])
    assert args[:4] == ["pip", "install", "--python", str(venv)]
    paths = [Path(arg) for arg in args[4:]]
    assert [path.name for path in paths] == ["runpod-python", "astrid-runtime", "runpod-lifecycle", "astrid-agent", "VibeComfy"]
    assert len({path.parent for path in paths}) == 1
    assert paths[0].parent.parent.parent == tmp_path / "boot"
    assert all(not any(arg == "-e" or arg == "--editable" or arg.startswith("--editable=")
                       for arg in shlex.split(call)) for call in calls)
    assert pkg < pip < probe


def test_existing_packages_skip_install_but_still_get_pip_and_a_matching_probe_check(tmp_path):
    venv = _fake_venv(tmp_path, dict(PROBE_TEXT))
    freeze = tmp_path / "freeze.txt"
    freeze.write_text("runpod==1.0\nastrid-runtime==1.0\nrunpod-lifecycle==1.0\nastrid==1.0\nvibecomfy==1.0\n")
    log = tmp_path / "uv.log"
    env = {**_overrides(tmp_path), "UV_LOG": str(log), "FAKE_UV_FREEZE": str(freeze)}
    r = _boot(tmp_path, env)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "skipping install" in r.stdout and "installed astrid matches" in r.stdout
    calls = _uv_calls(log)
    assert not any(c.startswith(f"pip install --python {venv} {tmp_path}/boot/.install.") for c in calls)
    _line_index(calls, f"pip install --python {venv} pip")
    assert not any("--reinstall-package astrid" in c for c in calls)


def test_probe_check_reinstalls_astrid_when_installed_module_differs(tmp_path):
    copies = dict(PROBE_TEXT)
    copies["engine_runner_target.py"] = "# older probe\n"
    venv = _fake_venv(tmp_path, copies)
    log = tmp_path / "uv.log"
    r = _boot(tmp_path, {**_overrides(tmp_path), "UV_LOG": str(log)})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "installed astrid differs from src/astrid-agent (engine_runner_target.py)" in r.stdout
    _line_index(_uv_calls(log), f"pip install --python {venv} --reinstall-package astrid ")


def test_marker_rerun_still_verifies_installed_astrid(tmp_path):
    venv = _fake_venv(tmp_path, dict(PROBE_TEXT))
    log = tmp_path / "uv.log"
    env = {**_overrides(tmp_path), "UV_LOG": str(log)}
    first = _boot(tmp_path, env)
    assert first.returncode == 0, first.stderr + first.stdout
    assert not any("--reinstall-package astrid" in c for c in _uv_calls(log))
    (_site(venv) / "astrid" / "core" / "generation" / "engine_runner.py").write_text("# hand-copied\n")
    second = _boot(tmp_path, env)
    assert second.returncode == 0, second.stderr + second.stdout
    assert "already done" in second.stdout and "installed astrid differs" in second.stdout
    assert sum("--reinstall-package astrid" in c for c in _uv_calls(log)) == 1


@pytest.mark.parametrize("mode", ["fresh", "reinstall"])
def test_source_builds_are_staged_and_cleaned_on_success_and_failure(tmp_path, mode):
    writes = tmp_path / "build-writes.log"
    log = tmp_path / "uv.log"
    env = {**_overrides(tmp_path), "FAKE_UV_WRITE_LOG": str(writes), "UV_LOG": str(log)}
    if mode == "reinstall":
        copies = {**PROBE_TEXT, "engine_runner.py": "# drift\n"}
        _fake_venv(tmp_path, copies)
        freeze = tmp_path / "freeze.txt"
        freeze.write_text("runpod==1.0\nastrid-runtime==1.0\nrunpod-lifecycle==1.0\nastrid==1.0\nvibecomfy==1.0\n")
        env["FAKE_UV_FREEZE"] = str(freeze)

    src = _sources(tmp_path)

    def source_snapshot():
        return {str(path.relative_to(src)): (path.stat().st_mode, path.read_bytes() if path.is_file() else None)
                for path in src.rglob("*")}

    before = source_snapshot()

    failed = _boot(tmp_path, {**env, "FAKE_UV_FAIL_BUILD": "1"})
    assert failed.returncode == 8, failed.stderr + failed.stdout
    assert not list((tmp_path / "boot").glob("*/ok"))
    assert "pod_bootstrap: ok" not in failed.stdout
    assert not (tmp_path / "realm").exists()
    assert not (tmp_path / "data" / "runtime").exists()
    if mode == "fresh":
        assert f"pip install --python {tmp_path / 'venv'} pip" not in _uv_calls(log)
    assert not list((tmp_path / "boot").glob(".install.*"))
    assert source_snapshot() == before

    writes.unlink()
    result = _boot(tmp_path, env)
    assert result.returncode == 0, result.stderr + result.stdout
    staged = [Path(line) for line in writes.read_text().splitlines()]
    assert staged and all(path.parent.name == "src" and path.parent.parent.parent == tmp_path / "boot" for path in staged)
    assert len(list((tmp_path / "boot").glob("*/ok"))) == 1
    assert not list((tmp_path / "boot").glob(".install.*"))
    assert source_snapshot() == before


def _gpu_sanity_function() -> str:
    text = SCRIPT.read_text()
    start = text.index("run_gpu_sanity() {")
    return text[start:text.index("\n}\n", start) + 3]


def _run_sanity(tmp_path: Path, engine_root: Path, stub_exit: int = 0):
    """Run only run_gpu_sanity with a stub python that records its argv."""
    log = tmp_path / "argv.log"
    venv = tmp_path / "astrid-venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    stub = venv / "bin" / "python"
    stub.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\necho "libs: /stub/libcublas.so.13"\nexit {stub_exit}\n')
    stub.chmod(0o755)
    script = (f'set -euo pipefail\nVENV="{venv}"\nENGINE_ROOT="{engine_root}"\n' + _gpu_sanity_function() + "run_gpu_sanity\n")
    r = subprocess.run(["bash", "-c", script], cwd=tmp_path, env={"PATH": os.environ["PATH"]}, capture_output=True, text=True)
    return r, log


def _engine_venv(root: Path) -> Path:
    py = root / "runtime" / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    return root / "runtime" / "venv"


def test_gpu_sanity_skips_when_engine_venv_absent(tmp_path):
    r, log = _run_sanity(tmp_path, tmp_path / "engine")
    assert r.returncode == 0, r.stderr
    assert "skipped" in r.stdout and not log.exists()


def test_gpu_sanity_runs_when_engine_venv_present(tmp_path):
    venv = _engine_venv(tmp_path / "engine")
    r, log = _run_sanity(tmp_path, tmp_path / "engine")
    assert r.returncode == 0, r.stderr
    assert log.read_text().split() == ["src/astrid-agent/scripts/gpu_sanity.py", "--venv", str(venv)]


def test_gpu_sanity_failure_exits_2_and_names_the_venv(tmp_path):
    venv = _engine_venv(tmp_path / "engine")
    r, _ = _run_sanity(tmp_path, tmp_path / "engine", stub_exit=2)
    assert r.returncode == 2 and str(venv) in r.stderr


def test_bootstrap_wires_sanity_before_engine_side_work_and_into_install():
    text = SCRIPT.read_text()
    assert 'ENGINE_ROOT="${ASTRID_ENGINE_ROOT:-' in text and "/root" not in text
    assert text.index('[ -f "$marker" ]') < text.index("  run_gpu_sanity\n") < text.index("verify_astrid_install\n  setup_runtime")
    assert "ASTRID_ENGINE_SANITY_CMD" in text and "cap 128" in text.split("set -euo")[0]
