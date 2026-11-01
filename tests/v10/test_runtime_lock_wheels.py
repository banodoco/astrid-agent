"""Runtime lock wheel-availability contract."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.reshape import release_reproducibility
from scripts.reshape.release_reproducibility import (
    RUNTIME_WHEEL_PYTHON_VERSION,
    RUNTIME_WHEEL_TARGETS,
    ReproducibilityError,
    validate_runtime_lock_wheels,
)


def _runtime_lock(tmp_path: Path, body: str) -> Path:
    requirements = tmp_path / "requirements"
    requirements.mkdir()
    lock = requirements / "runtime.lock"
    lock.write_text(body, encoding="utf-8")
    return lock


def test_runtime_wheel_check_uses_all_supported_binary_targets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _runtime_lock(tmp_path, "example==1 --hash=sha256:" + "0" * 64 + "\n")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, stdout="uv 0.11.32\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="resolved\n", stderr="")

    monkeypatch.setattr(release_reproducibility.subprocess, "run", fake_run)
    report = validate_runtime_lock_wheels(
        tmp_path,
        uv_executable="uv",
        python_executable=tmp_path / "python3.11",
    )

    assert report["python_version"] == RUNTIME_WHEEL_PYTHON_VERSION
    assert [(item["name"], item["platform"]) for item in report["targets"]] == list(
        RUNTIME_WHEEL_TARGETS
    )
    resolutions = calls[1:]
    assert len(resolutions) == len(RUNTIME_WHEEL_TARGETS)
    for command, (_, platform_target) in zip(resolutions, RUNTIME_WHEEL_TARGETS):
        assert "--dry-run" in command
        assert command[command.index("--require-hashes") + 1] == "--only-binary"
        assert command[command.index("--only-binary") + 1] == ":all:"
        assert command[command.index("--python") + 1].endswith("python3.11")
        assert command[command.index("--python-platform") + 1] == platform_target


def test_runtime_wheel_check_fails_closed_for_an_unavailable_wheel(tmp_path: Path) -> None:
    _runtime_lock(
        tmp_path,
        "cryptography==50.0.0 --hash=sha256:"
        "031e2d5dd4bb9caa3ca9c82e5a197fd8ae680232cee62603d1a813f3f07e3d03\n",
    )

    with pytest.raises(
        ReproducibilityError,
        match=r"macos-x86_64.*cryptography==50\.0\.0.*no usable wheels",
    ):
        validate_runtime_lock_wheels(
            tmp_path,
            targets=(("macos-x86_64", "x86_64-apple-darwin"),),
        )
