from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from astrid.sdk.project_render import open_render


class RuntimeFixture:
    def __init__(self, data: bytes = b"minkhole-video") -> None:
        self.data = data
        self.digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.project_id = "7f362daea1f048969980ef23f0cd46e6"
        self.timeline_id = "timeline-minkhole"
        self.object_ref = "managed-object-minkhole"
        self.get_object_calls: list[str] = []
        self.current_project_calls = 0
        self.runs = [
            {
                "run_id": "e3aaf311c17b450198d1c2d6f1582887",
                "project_id": self.project_id,
                "capability_id": "rendering.render",
                "status": "completed",
                "created_at": "2026-09-11T00:00:00Z",
                "task_ids": ["task-minkhole"],
                "spec": {"timeline_id": self.timeline_id},
            }
        ]

    def current_project(self):
        self.current_project_calls += 1
        return {"project": {"project_id": "astrid-intro"}}

    def get_project(self, project_id):
        assert project_id == self.project_id
        return {"project_id": self.project_id, "name": "Matrix — Into the Minkhole"}

    def list_timelines(self, project_id, *, cursor=None, limit=50):
        assert project_id == self.project_id
        return [[{"timeline_id": self.timeline_id, "slug": "minkhole"}], None]

    def list_project_runs(self, project_id, *, cursor=None, limit=50):
        assert project_id == self.project_id
        return [self.runs, None]

    def get_run(self, run_id):
        return next(item for item in self.runs if item["run_id"] == run_id)

    def association(self, **overrides):
        return {
            "output_port": "video",
            "managed_object_reference": self.object_ref,
            "digest": self.digest,
            "actual_filename": "minkhole-review-v13.mp4",
            "media_type": "video/mp4",
            "size": len(self.data),
            "ordinal": 0,
            "role": "result",
            "task_id": "task-minkhole",
            "producer_id": "astrid-renderer",
            "attempt_id": "attempt-minkhole",
            **overrides,
        }

    def get_task(self, task_id):
        return {
            "task_id": task_id,
            "capability_id": "rendering.render",
            "state": "completed",
            "attempt_id": "attempt-minkhole",
            "spec": {"spec": {"timeline_id": self.timeline_id}},
            "result": {"outputs": [self.association()]},
        }

    def get_object(self, reference):
        self.get_object_calls.append(reference)
        if reference != self.object_ref:
            raise AssertionError(f"unexpected managed reference: {reference}")
        return {"data": self.data}


def _open(monkeypatch, runtime, tmp_path, **kwargs):
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    opened: list[Path] = []
    result = open_render(
        runtime,
        runtime.project_id,
        runtime.timeline_id,
        cache_root=tmp_path,
        opener=opened.append,
        **kwargs,
    )
    return result, opened


def test_explicit_scope_ignores_wrong_global_project_and_preserves_association(monkeypatch, tmp_path):
    runtime = RuntimeFixture()
    result, opened = _open(monkeypatch, runtime, tmp_path)

    assert result.ok
    assert runtime.current_project_calls == 0
    assert result.data["run_id"] == "e3aaf311c17b450198d1c2d6f1582887"
    assert result.data["digest"] == runtime.digest
    assert result.data["filename"] == "minkhole-review-v13.mp4"
    assert result.data["media_type"] == "video/mp4"
    assert result.data["output_port"] == "video"
    assert result.data["ordinal"] == 0
    assert result.data["role"] == "result"
    assert result.data["producer_id"] == "astrid-renderer"
    assert result.data["attempt_id"] == "attempt-minkhole"
    assert runtime.get_object_calls == [runtime.object_ref]
    assert opened == [Path(result.data["local_path"])]


@pytest.mark.parametrize(
    "run_spec,task_spec",
    [
        ({"timeline_id": "other"}, {"timeline_id": "timeline-minkhole"}),
        ({}, {}),
    ],
)
def test_explicit_timeline_rejects_conflicting_or_absent_provenance(
    monkeypatch, tmp_path, run_spec, task_spec
):
    runtime = RuntimeFixture()
    runtime.runs[0]["spec"] = run_spec
    original = runtime.get_task

    def task(task_id):
        value = original(task_id)
        value["spec"] = {"spec": task_spec}
        return value

    runtime.get_task = task
    result, opened = _open(monkeypatch, runtime, tmp_path, run_id=runtime.runs[0]["run_id"])

    assert not result.ok
    assert result.error.code == "validation_error"
    assert "provenance" in result.error.message
    assert opened == []
    assert runtime.get_object_calls == []


def test_missing_publication_association_is_an_explicit_dependency_failure(monkeypatch, tmp_path):
    runtime = RuntimeFixture()
    runtime.get_task = lambda task_id: {
        "task_id": task_id,
        "capability_id": "rendering.render",
        "state": "completed",
        "spec": {"spec": {"timeline_id": runtime.timeline_id}},
        "result": {"outputs": [{"name": "video", "digest": runtime.digest}]},
    }
    result, opened = _open(monkeypatch, runtime, tmp_path)

    assert not result.ok
    assert result.error.code == "unavailable"
    assert result.error.details["dependency"] == "runtime_publication_association"
    assert opened == []
    assert runtime.get_object_calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("actual_filename", "../unsafe.mp4"),
        ("actual_filename", "render.exe"),
        ("media_type", "application/octet-stream"),
    ],
)
def test_invalid_filename_or_media_type_fails_closed(monkeypatch, tmp_path, field, value):
    runtime = RuntimeFixture()
    runtime.get_task = lambda task_id: {
        "task_id": task_id,
        "capability_id": "rendering.render",
        "state": "completed",
        "spec": {"spec": {"timeline_id": runtime.timeline_id}},
        "result": {"outputs": [runtime.association(**{field: value})]},
    }
    result, opened = _open(monkeypatch, runtime, tmp_path)

    assert not result.ok
    assert result.error.code == "protocol_error"
    assert opened == []
    assert runtime.get_object_calls == []


@pytest.mark.parametrize(
    "data,expected_size",
    [(b"tampered", len(b"minkhole-video")), (b"minkhole-video", len(b"minkhole-video") + 1)],
)
def test_digest_or_size_mismatch_never_publishes(monkeypatch, tmp_path, data, expected_size):
    runtime = RuntimeFixture(data=data)
    runtime.runs[0]["spec"] = {"timeline_id": runtime.timeline_id}
    runtime.get_task = lambda task_id: {
        "task_id": task_id,
        "capability_id": "rendering.render",
        "state": "completed",
        "spec": {"spec": {"timeline_id": runtime.timeline_id}},
        "result": {"outputs": [runtime.association(digest=RuntimeFixture().digest, size=expected_size)]},
    }
    result, opened = _open(monkeypatch, runtime, tmp_path)

    assert not result.ok
    assert result.error.code == "integrity_error"
    assert opened == []
    assert not list(tmp_path.rglob("minkhole-review-v13.mp4"))


def test_cache_reuse_is_independently_verified_and_uses_exact_opener_path(monkeypatch, tmp_path):
    runtime = RuntimeFixture()
    first, opened_first = _open(monkeypatch, runtime, tmp_path)
    assert first.ok
    runtime.get_object = lambda _reference: pytest.fail("valid cache must not redownload")
    second, opened_second = _open(monkeypatch, runtime, tmp_path)

    assert second.ok
    assert second.data["local_path"] == first.data["local_path"]
    assert opened_second == [Path(first.data["local_path"])]
    assert second.data["open_requested"] is True
    assert second.data["playback_confirmed"] is False
    assert second.data["opened"] is True
    assert second.data["open_error"] is None

    Path(first.data["local_path"]).write_bytes(b"corrupt-cache")
    runtime.get_object = lambda reference: {"data": b"minkhole-video"}
    third, _ = _open(monkeypatch, runtime, tmp_path)
    assert third.ok
    assert Path(third.data["local_path"]).read_bytes() == b"minkhole-video"


def test_managed_reference_must_not_be_a_local_path(monkeypatch, tmp_path):
    runtime = RuntimeFixture()
    runtime.get_task = lambda task_id: {
        "task_id": task_id,
        "capability_id": "rendering.render",
        "state": "completed",
        "spec": {"spec": {"timeline_id": runtime.timeline_id}},
        "result": {"outputs": [runtime.association(managed_object_reference="/tmp/raw-cas") ]},
    }
    result, opened = _open(monkeypatch, runtime, tmp_path)

    assert not result.ok
    assert result.error.code == "protocol_error"
    assert opened == []
    assert runtime.get_object_calls == []
