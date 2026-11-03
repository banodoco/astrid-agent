"""Issue the launch proof a real Remotion render needs (shared by tests/packs and tests/stage1)."""

from __future__ import annotations

import os
import secrets
import shutil
import sys
from pathlib import Path

import pytest

from astrid.core.env_vars import (
    ASTRID_NODE_EXECUTABLE,
    ASTRID_REMOTION_PROJECT_DIR,
    ASTRID_REMOTION_VALIDATION_LAUNCH,
    ASTRID_REMOTION_VALIDATION_PATH,
    ASTRID_REMOTION_VALIDATION_ROLE,
    ASTRID_REMOTION_VALIDATION_SHA256,
)
from astrid.core.rendering.remotion_runtime import prepare_remotion_validation

REMOTION = Path(__file__).resolve().parents[2] / "remotion"

_LOADER_INTERPRETER_OVERRIDES = (
    "PYTHONEXECUTABLE", "__PYVENV_LAUNCHER__", "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH", "NODE_ICU_DATA",
    "OPENSSL_CONF", "PYTHONUSERBASE", "PYTHONNOUSERSITE", "PYTHONSAFEPATH", "PYTHONPYCACHEPREFIX",
    "PYTHONPLATLIBDIR", "PYTHONCASEOK", "PYTHONUTF8", "PYTHONCOERCECLOCALE", "PYTHONSTARTUP",
)


REQUIRED_BINARIES = ("node", "npx", "ffmpeg", "ffprobe")
REQUIRED_PACKAGES = (
    "@banodoco/timeline-composition",
    "@remotion/cli",
    "@remotion/three",
    "@react-three/fiber",
    "three",
)


def remotion_install_problems() -> list[str]:
    """What a real render needs that this machine does not provide; empty when it is provisioned."""
    problems = [f"{binary} is not on PATH" for binary in REQUIRED_BINARIES if shutil.which(binary) is None]
    modules = REMOTION / "node_modules"
    problems.extend(
        f"remotion/node_modules/{package} is missing" for package in REQUIRED_PACKAGES if not (modules / package).is_dir()
    )
    return problems


def issue_remotion_launch_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Qualify this process's environment with the production launch transaction.

    The caller has already checked that the Remotion install is provisioned.
    """

    for name in (
        ASTRID_REMOTION_VALIDATION_PATH,
        ASTRID_REMOTION_VALIDATION_SHA256,
        ASTRID_REMOTION_VALIDATION_LAUNCH,
        ASTRID_REMOTION_VALIDATION_ROLE,
    ):
        monkeypatch.delenv(name, raising=False)

    # This qualifies the test process's own environment directly, not a launched host's: the product drops
    # LD_*/DYLD_* only on the host-launch path (_prepare_render_validation), so the loader keys must still go
    # here, as must the interpreter overrides that probe_context refuses. A CI runner legitimately carries some
    # (actions/setup-python exports LD_LIBRARY_PATH on Linux); the validator stays strict.
    for name in list(os.environ):
        if name.startswith(("DYLD_", "LD_")) or name in _LOADER_INTERPRETER_OVERRIDES:
            monkeypatch.delenv(name)

    monkeypatch.setenv(ASTRID_REMOTION_PROJECT_DIR, str(REMOTION.resolve()))
    node_value = os.environ.get(ASTRID_NODE_EXECUTABLE) or shutil.which("node")
    assert node_value, "the provisioned Remotion environment must provide Node"
    node = Path(node_value).expanduser().resolve(strict=True)
    monkeypatch.setenv(ASTRID_NODE_EXECUTABLE, str(node))

    from tests.helpers.remotion_runtime import configure_remotion_runtime

    configure_remotion_runtime(monkeypatch)
    # The transaction qualifies this process's environment (what an in-process render validates) and its
    # allowlist projection (what the transport hands the backend child). The process must therefore carry no
    # loader, interpreter-startup or Git-redirect overrides: the runtime refuses those and so does this fixture.
    reference = prepare_remotion_validation(
        artifact_directory=tmp_path / "remotion-validation",
        host_environment=dict(os.environ),
        python_executable=sys.executable,
        launch_id=secrets.token_hex(16),
        contexts={"standalone": dict(os.environ)},
    )
    for name, value in reference.items():
        monkeypatch.setenv(name, value)
