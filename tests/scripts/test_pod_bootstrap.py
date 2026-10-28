import json
import os
import re
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


def test_vibecomfy_editable_install_after_package_step_before_marker():
    text = SCRIPT.read_text()
    editable = 'uv pip install --python "$VENV" --no-deps -e src/VibeComfy'
    assert text.count(editable) == 1
    package_step = 'uv pip install --python "$VENV" "${SRC_DIRS[@]}"'
    assert text.count(package_step) == 1
    # Rerun skip (marker check) < package install < editable install < marker write.
    assert text.index('[ -f "$marker" ]') < text.index(package_step)
    assert text.index(package_step) < text.index(editable) < text.index('mkdir -p "$marker_dir"')


TOOLS = ["find", "sort", "xargs", "sha256sum", "cut", "date", "mkdir", "dirname", "grep", "id", "cat", "env", "bash", "chmod", "ln"]


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
            'case "$1 $2" in\n'
            '  "pip list") exit 1;;\n'
            '  "venv "*) for a; do v="$a"; done; mkdir -p "$v/bin"; ln -sf "$PY" "$v/bin/python"\n'
            '    printf \'#!/bin/sh\\nwhile [ $# -gt 0 ]; do [ "$1" = --realm-root ] && { mkdir -p "$2"; : > "$2/realm.sqlite3"; }; shift; done\\n\' > "$v/bin/astrid-runtime"\n'
            '    chmod +x "$v/bin/astrid-runtime";;\n'
            "esac\nexit 0\n"
        )
        uv.chmod(0o755)
    return b


def _boot(tmp_path: Path, extra: dict, with_uv=True) -> subprocess.CompletedProcess:
    src = tmp_path / "src"
    for d in ("runpod-python", "astrid-runtime", "runpod-lifecycle", "astrid-agent", "VibeComfy"):
        (src / d).mkdir(parents=True, exist_ok=True)
        (src / d / "f.txt").write_text(d)
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
