"""Lifecycle regressions for generation workflow snapshots (A3)."""
from contextlib import contextmanager
from pathlib import Path
import tempfile

import pytest

import astrid.sdk.generation as generation


def _trace_stages(monkeypatch, events):
    @contextmanager
    def stage(name):
        events.append(("stage-enter", name))
        try:
            yield
        except BaseException as exc:
            events.append(("stage-error", name, type(exc)))
            raise
        else:
            events.append(("stage-exit", name))

    monkeypatch.setattr(generation, "stage", stage)


def _track_temporary_directories(monkeypatch, events, *, cleanup_error=None):
    real_temporary_directory = tempfile.TemporaryDirectory

    class TrackedTemporaryDirectory:
        def __init__(self, *args, **kwargs):
            self.inner = real_temporary_directory(*args, **kwargs)

        def __enter__(self):
            result = self.inner.__enter__()
            events.append(("temp-enter", Path(result)))
            return result

        def __exit__(self, *exc):
            events.append(("temp-exit", exc[0] if exc else None))
            result = self.inner.__exit__(*exc)
            if cleanup_error is not None:
                raise cleanup_error
            return result

    monkeypatch.setattr(tempfile, "TemporaryDirectory", TrackedTemporaryDirectory)


def test_snapshot_lifecycle_cleans_tempdir_after_successful_yield(tmp_path, monkeypatch):
    events = []
    _trace_stages(monkeypatch, events)
    _track_temporary_directories(monkeypatch, events)
    source = tmp_path / "workflow.json"
    source.write_bytes(b'{"nodes": []}')

    with generation._snapshot_generation_workflow(source) as snapshot:
        assert snapshot.read_bytes() == source.read_bytes()
        scratch = snapshot.parent
        assert scratch.is_dir()
        assert events == [
            ("stage-enter", "workflow snapshot"),
            ("temp-enter", scratch),
            ("stage-exit", "workflow snapshot"),
        ]

    assert not scratch.exists()
    assert events == [
        ("stage-enter", "workflow snapshot"),
        ("temp-enter", scratch),
        ("stage-exit", "workflow snapshot"),
        ("temp-exit", None),
        ("stage-enter", "workflow snapshot cleanup"),
        ("stage-exit", "workflow snapshot cleanup"),
    ]


def test_snapshot_setup_read_failure_keeps_refusal_and_cleans_up(tmp_path, monkeypatch):
    events = []
    _trace_stages(monkeypatch, events)
    _track_temporary_directories(monkeypatch, events)
    source = tmp_path / "workflow.json"
    source.write_bytes(b"original")
    original_read_bytes = Path.read_bytes
    failure = OSError("snapshot source unavailable")

    def read_bytes(path):
        if path == source:
            raise failure
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    with pytest.raises(generation.GenerationInputError) as caught:
        with generation._snapshot_generation_workflow(source):
            pytest.fail("snapshot setup refusal must happen before yield")

    assert "canonical workflow bundle could not be read" in str(caught.value)
    assert caught.value.__cause__ is failure
    assert events[0][0:2] == ("stage-enter", "workflow snapshot")
    assert events[1][0:2] == ("temp-enter", events[1][1])
    assert events[2][0:2] == ("temp-exit", generation.GenerationInputError)
    assert events[3:] == [
        ("stage-enter", "workflow snapshot cleanup"),
        ("stage-exit", "workflow snapshot cleanup"),
        ("stage-error", "workflow snapshot", generation.GenerationInputError),
    ]
    assert not events[1][1].exists()


def test_snapshot_yield_body_exception_propagates_after_cleanup(tmp_path, monkeypatch):
    events = []
    _trace_stages(monkeypatch, events)
    _track_temporary_directories(monkeypatch, events)
    source = tmp_path / "workflow.json"
    source.write_bytes(b"original")
    refusal = RuntimeError("downstream refusal")

    with pytest.raises(RuntimeError, match="downstream refusal") as caught:
        with generation._snapshot_generation_workflow(source) as snapshot:
            scratch = snapshot.parent
            raise refusal

    assert caught.value is refusal
    assert not scratch.exists()
    assert events[2:] == [
        ("temp-exit", RuntimeError),
        ("stage-enter", "workflow snapshot cleanup"),
        ("stage-exit", "workflow snapshot cleanup"),
    ]


def test_snapshot_cleanup_failure_surfaces_without_suppressing_cleanup_stage(
        tmp_path, monkeypatch):
    events = []
    _trace_stages(monkeypatch, events)
    cleanup_error = OSError("temporary directory cleanup failed")
    _track_temporary_directories(monkeypatch, events, cleanup_error=cleanup_error)
    source = tmp_path / "workflow.json"
    source.write_bytes(b"original")
    body_error = RuntimeError("downstream refusal")

    with pytest.raises(OSError, match="temporary directory cleanup failed") as caught:
        with generation._snapshot_generation_workflow(source) as snapshot:
            scratch = snapshot.parent
            raise body_error

    assert caught.value is cleanup_error
    assert caught.value.__context__ is body_error
    assert not scratch.exists()
    assert events[2:] == [
        ("temp-exit", RuntimeError),
        ("stage-enter", "workflow snapshot cleanup"),
        ("stage-error", "workflow snapshot cleanup", OSError),
    ]

