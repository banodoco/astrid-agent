"""Release dependency-lock and toolchain evidence contract."""

from __future__ import annotations

import json
import platform
from pathlib import Path

import pytest

from scripts.reshape.release_reproducibility import (
    ReproducibilityError,
    record_required_toolchain,
    validate_dependency_locks,
    validate_hashed_lock,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

def test_repository_dependency_locks_are_hashed_exact_and_cover_metadata() -> None:
    report = validate_dependency_locks(REPO_ROOT)
    assert report["schema"] == "astrid.dependency_locks.v1"
    assert set(report["locks"]) == {
        "requirements/build.lock",
        "requirements/runtime.lock",
        "requirements/proof.lock",
    }
    assert report["locks"]["requirements/build.lock"]["packages"] >= 6
    assert report["locks"]["requirements/runtime.lock"]["packages"] >= 60
    assert report["locks"]["requirements/proof.lock"]["packages"] >= 65
    assert all(
        len(lock["sha256"]) == 64 for lock in report["locks"].values()
    )


@pytest.mark.parametrize("name", ["runtime", "proof"])
def test_locks_cover_no_deps_vibecomfy_install(name: str) -> None:
    # The installer deliberately installs the pinned VibeComfy with --no-deps.
    pins = validate_hashed_lock(REPO_ROOT / "requirements" / f"{name}.lock")["pins"]
    assert {"huggingface-hub", "hf-xet", "httpx", "pyyaml", "websockets"} <= pins.keys()


def test_runtime_lock_covers_runpod_source_dependencies_without_source_distributions() -> None:
    pins = validate_hashed_lock(REPO_ROOT / "requirements" / "runtime.lock")["pins"]
    assert {
        "aiohttp", "aiohttp-retry", "backoff", "boto3", "click", "colorama",
        "cryptography", "fastapi", "inquirerpy", "paramiko", "prettytable",
        "psutil", "py-cpuinfo", "requests", "tomli", "tomlkit", "tqdm-loggable",
        "urllib3", "watchdog",
    } <= pins.keys()
    assert not {"runpod", "runpod-lifecycle"} & pins.keys()


def test_lock_validation_fails_closed_on_ranges_urls_and_missing_hashes(
    tmp_path: Path,
) -> None:
    for body, match in (
        ("example>=1\n", "exact == pin"),
        ("example @ https://example.invalid/a.whl\n", "exact == pin"),
        ("example==1\n", "no sha256 hash"),
    ):
        lock = tmp_path / "bad.lock"
        lock.write_text(body, encoding="utf-8")
        with pytest.raises(ReproducibilityError, match=match):
            validate_hashed_lock(lock)


def test_required_release_toolchain_is_present_and_version_recorded() -> None:
    expected = ".".join(platform.python_version_tuple()[:2])
    report = record_required_toolchain(
        repo_root=REPO_ROOT,
        expected_python=expected,
    )
    assert report["python"]["version"] == platform.python_version()
    assert set(report["tools"]) == {"make", "bash", "git", "ffmpeg", "ffprobe"}
    assert all(item["version"] and item["executable"] for item in report["tools"].values())


def test_release_ci_enforces_hashed_build_runtime_and_toolchain_evidence() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements/build.lock" in workflow
    assert "-r requirements/runtime.lock" in workflow
    assert "python -m build --wheel --no-isolation" in workflow
    assert "scripts.reshape.release_reproducibility" in workflow
    assert "--output dist/toolchain.json" in workflow
    # The installed-wheel smoke runs against the wheel and venv this job built (testing plan R-1).
    assert 'scripts/smoke_wheel_install.sh --wheel dist/*.whl --venv "$RUNNER_TEMP/release-venv"' in workflow

# End of release reproducibility contract.
