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
    assert "/workspace/astrid/bootstrap/$digest/ok" in text
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
