#!/usr/bin/env python3
"""Provision and exercise the repository-owned Remotion adapter.

The Python renderer intentionally does not discover ``node``, ``npx``, or a
global Remotion install.  This gate is the one local/CI entry point that may
discover a developer's Node installation: it validates the exact pinned
versions, runs ``npm ci`` against the committed lockfile, and exports the
validated absolute Node path to the renderer tests.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REMOTION_ROOT = REPO_ROOT / "remotion"
NODE_VERSION = "v20.19.4"
NPM_VERSION = "10.8.2"
MIN_FREE_BYTES = 2 * 1024**3
VERSION_PROBE_TIMEOUT_SECONDS = 5
NPM_CI_TIMEOUT_SECONDS = 900
TYPECHECK_TIMEOUT_SECONDS = 300
PARITY_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class RemotionTools:
    node: Path
    npm_cli: Path
    node_version: str
    npm_version: str


def _tool(name: str) -> Path:
    explicit = os.environ.get(f"ASTRID_{name.upper()}_EXECUTABLE", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            raise RuntimeError(f"{name} executable must be an absolute path: {candidate}")
    else:
        found = shutil.which(name)
        if not found:
            raise RuntimeError(f"{name} executable not found; install Node {NODE_VERSION}")
        candidate = Path(found)
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{name} executable is not readable: {candidate}: {exc}") from exc
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise RuntimeError(f"{name} executable is not executable: {candidate}")
    return candidate


def _run_version(command: list[str], *, env: dict[str, str] | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"version probe timed out after {VERSION_PROBE_TIMEOUT_SECONDS}s: {' '.join(command)}"
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            f"version probe failed for {command[0]}: {completed.stderr.strip()}"
        )
    return (completed.stdout.strip() or completed.stderr.strip()).splitlines()[0]


def _contained(path: Path, root: Path, *, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{label} must be contained by {root}: {path}") from exc
    return resolved


def _resolve_tools() -> RemotionTools:
    node = _tool("node")
    node_version = _run_version([str(node), "--version"])
    distribution_root = node.parent.parent
    npm_cli = distribution_root / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm_cli = _contained(npm_cli, distribution_root, label="npm CLI")
    configured_npm = os.environ.get("ASTRID_NPM_EXECUTABLE", "").strip()
    if configured_npm:
        configured_path = _contained(
            Path(configured_npm).expanduser(), distribution_root,
            label="ASTRID_NPM_EXECUTABLE",
        )
        if configured_path != npm_cli:
            raise RuntimeError(
                "ASTRID_NPM_EXECUTABLE must name the Node distribution's "
                f"npm CLI ({npm_cli}), not {configured_path}"
            )
    npm_env = os.environ.copy()
    npm_env["PATH"] = str(node.parent) + os.pathsep + npm_env.get("PATH", "")
    npm_version = _run_version(
        [str(node), str(npm_cli), "--version"], env=npm_env
    )
    if node_version != NODE_VERSION or npm_version != NPM_VERSION:
        raise RuntimeError(
            "Remotion requires the pinned toolchain "
            f"Node {NODE_VERSION.removeprefix('v')} / npm {NPM_VERSION}; "
            f"found {node_version} / npm {npm_version} ({node}, {npm_cli})"
        )
    return RemotionTools(node, npm_cli, node_version, npm_version)


def _check_free_space() -> None:
    free = shutil.disk_usage(REPO_ROOT).free
    if free < MIN_FREE_BYTES:
        raise RuntimeError(
            f"refusing Remotion install: only {free / 1024**3:.2f} GiB free; "
            "the safety floor is 2.00 GiB"
        )


def _env(node: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["ASTRID_NODE_EXECUTABLE"] = str(node)
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    return env


def _run(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> None:
    print("+ " + " ".join(command), flush=True)
    try:
        completed = subprocess.run(
            command, cwd=cwd, env=env, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"command timed out after {timeout}s: {' '.join(command)}"
        ) from exc
    if completed.returncode:
        raise SystemExit(completed.returncode)


def _validate_install() -> None:
    package_lock = REMOTION_ROOT / "package-lock.json"
    hidden_lock = REMOTION_ROOT / "node_modules" / ".package-lock.json"
    if not hidden_lock.is_file():
        raise RuntimeError(
            "npm ci did not produce remotion/node_modules/.package-lock.json"
        )
    try:
        expected = json.loads(package_lock.read_text(encoding="utf-8"))
        actual = json.loads(hidden_lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot validate the Remotion npm closure: {exc}") from exc
    if actual.get("lockfileVersion") != expected.get("lockfileVersion"):
        raise RuntimeError("installed Remotion closure has a different lockfile version")
    expected_packages = expected.get("packages", {})
    actual_packages = actual.get("packages", {})
    if not isinstance(expected_packages, dict) or not isinstance(actual_packages, dict):
        raise RuntimeError("Remotion package locks have no package map")
    for package_name, metadata in actual_packages.items():
        if package_name not in expected_packages:
            raise RuntimeError(f"installed package is absent from package-lock.json: {package_name}")
        if not isinstance(metadata, dict) or not isinstance(expected_packages[package_name], dict):
            continue
        for field in ("version", "resolved", "integrity"):
            if field in metadata and metadata.get(field) != expected_packages[package_name].get(field):
                raise RuntimeError(f"installed lock mismatch for {package_name}: {field}")
    for package_name in ("@banodoco/timeline-composition",):
        package_dir = REMOTION_ROOT / "node_modules" / "@banodoco" / package_name.removeprefix("@banodoco/")
        if not package_dir.is_dir():
            raise RuntimeError(f"required Remotion package is missing: {package_dir}")
    root_dependencies = {
        **expected_packages.get("", {}).get("dependencies", {}),
        **expected_packages.get("", {}).get("devDependencies", {}),
    }
    for package_name in root_dependencies:
        lock_name = f"node_modules/{package_name}"
        if lock_name not in actual_packages:
            raise RuntimeError(f"direct dependency is absent from installed npm lock: {package_name}")


def _install(tools: RemotionTools, env: dict[str, str]) -> None:
    _check_free_space()
    # A successful authoritative npm ci must recreate this hidden lock. This
    # prevents a false-zero/no-op npm shim from allowing stale dependencies.
    (REMOTION_ROOT / "node_modules" / ".package-lock.json").unlink(missing_ok=True)
    _run(
        [str(tools.node), str(tools.npm_cli), "ci", "--no-audit", "--fund=false"],
        cwd=REMOTION_ROOT,
        env=env,
        timeout=NPM_CI_TIMEOUT_SECONDS,
    )
    _validate_install()
    _check_free_space()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("install", "typecheck", "parity", "all"),
        help="provision dependencies, run typecheck, run parity, or do all three",
    )
    parser.add_argument(
        "--reuse-installed",
        action="store_true",
        help="validate an existing npm ci closure instead of reinstalling it",
    )
    args = parser.parse_args()
    try:
        tools = _resolve_tools()
        env = _env(tools.node)
        if args.reuse_installed:
            _validate_install()
        else:
            _install(tools, env)
        if args.action in ("typecheck", "all"):
            _run(
                [sys.executable, str(REPO_ROOT / "scripts/gen_remotion_types.py")],
                cwd=REPO_ROOT,
                env=env,
                timeout=TYPECHECK_TIMEOUT_SECONDS,
            )
            _run(
                [str(tools.node), str(tools.npm_cli), "run", "typecheck"],
                cwd=REMOTION_ROOT,
                env=env,
                timeout=TYPECHECK_TIMEOUT_SECONDS,
            )
        if args.action in ("parity", "all"):
            _run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-m",
                    "integration",
                    "tests/packs/test_renderer_parity.py",
                ],
                cwd=REPO_ROOT,
                env=env,
                timeout=PARITY_TIMEOUT_SECONDS,
            )
    except RuntimeError as exc:
        print(f"remotion gate: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
