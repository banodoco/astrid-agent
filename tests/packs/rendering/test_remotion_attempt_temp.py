"""Pure ownership and path-budget checks for managed Remotion scratch."""

import os
import tempfile
from pathlib import Path

from astrid.core.execution.generic_host import (
    StorageEnvelopeError,
    _assert_live_storage_envelope,
    _cleanup_ephemeral_attempt,
)
from astrid.packs.rendering.rendering.renderers.remotion.root_preparation import (
    _accounted_attempt_root,
)


def _attempt_fixture() -> tuple[tempfile.TemporaryDirectory[str], Path, Path, Path]:
    holder = tempfile.TemporaryDirectory(prefix="tl-", dir="/tmp")
    base = Path(holder.name)
    attempt = base / "astrid-attempt-a1b2c3"
    staging = attempt / ".render-service-a1"
    output = staging / "outputs" / "render.mp4"
    output.parent.mkdir(parents=True)
    attempt.chmod(0o700)
    progress = attempt / ".astrid-progress.json"
    progress.touch()
    return holder, attempt, staging, output


def test_ephemeral_attempt_name_keeps_reclaim_prefix_without_task_identity(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    from astrid.core.execution.generic_host import GenericPackHost

    host = GenericPackHost(pack_roots=[tmp_path])
    attempt = host._allocate_attempt_root("task-with-a-long-identity", "attempt-with-a-long-identity")
    try:
        assert attempt.name.startswith("astrid-attempt-")
        assert "task-with-a-long-identity" not in attempt.name
        assert attempt.stat().st_mode & 0o077 == 0
    finally:
        _cleanup_ephemeral_attempt(attempt)


def test_accounted_attempt_scratch_stays_in_owner_root_and_under_socket_limit(
    monkeypatch,
) -> None:
    holder, attempt, staging, output = _attempt_fixture()
    try:
        monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
        monkeypatch.setenv("ASTRID_PROGRESS_PATH", str(attempt / ".astrid-progress.json"))

        assert _accounted_attempt_root(staging, output) == attempt
        socket_path = (
            attempt / ".r-9x9u2k0m" / "com.google.Chrome.F0OceF" / "SingletonSocket"
        )
        assert len(os.fsencode(socket_path)) < 108
    finally:
        holder.cleanup()


def test_foreign_progress_root_is_refused(monkeypatch) -> None:
    holder, attempt, staging, output = _attempt_fixture()
    try:
        foreign = Path(holder.name) / "foreign"
        foreign.mkdir()
        (foreign / ".astrid-progress.json").touch()
        monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
        monkeypatch.setenv("ASTRID_PROGRESS_PATH", str(foreign / ".astrid-progress.json"))

        assert _accounted_attempt_root(staging, output) is None
    finally:
        holder.cleanup()


def test_symlinked_staging_root_is_refused(monkeypatch) -> None:
    holder, attempt, staging, output = _attempt_fixture()
    try:
        alias = Path(holder.name) / "staging-alias"
        alias.symlink_to(staging, target_is_directory=True)
        monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
        monkeypatch.setenv("ASTRID_PROGRESS_PATH", str(attempt / ".astrid-progress.json"))

        assert _accounted_attempt_root(alias, output) is None
    finally:
        holder.cleanup()


def test_attempt_root_scratch_is_charged_by_existing_live_envelope(tmp_path: Path) -> None:
    attempt = tmp_path / "attempt"
    output = attempt / "outputs"
    scratch = attempt / ".r-abc" / "com.google.Chrome"
    output.mkdir(parents=True)
    scratch.mkdir(parents=True)
    (scratch / "SingletonSocket").write_bytes(b"x")

    try:
        _assert_live_storage_envelope(
            {"scratch_bytes": 0, "output_bytes": 0}, attempt, output,
        )
    except StorageEnvelopeError as exc:
        assert exc.diagnostic["category"] == "scratch_overrun"
        assert exc.diagnostic["observed_scratch_bytes"] == 1
    else:
        raise AssertionError("attempt-root browser scratch escaped live accounting")
