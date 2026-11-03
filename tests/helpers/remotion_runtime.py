"""Configure the provisioned runtime used by real render tests."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from astrid.core.env_vars import (
    ASTRID_NODE_EXECUTABLE,
    ASTRID_REMOTION_PROJECT_DIR,
    ASTRID_TIMELINE_SCHEMA_PYTHONPATH,
)

REMOTION = Path(__file__).resolve().parents[2] / "remotion"

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


def configure_remotion_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the provisioned runtime and an absolute server-owned Node."""
    monkeypatch.setenv(ASTRID_REMOTION_PROJECT_DIR, str(REMOTION.resolve()))
    monkeypatch.setenv(ASTRID_TIMELINE_SCHEMA_PYTHONPATH,
                       str(REMOTION.parent / "astrid/core/timeline/schema/timeline-config.schema.json"))
    node_value = os.environ.get(ASTRID_NODE_EXECUTABLE) or shutil.which("node")
    assert node_value, "the provisioned Remotion environment must provide Node"
    monkeypatch.setenv(ASTRID_NODE_EXECUTABLE, str(Path(node_value).expanduser().resolve(strict=True)))
