"""Authority-boundary checks for the managed visualization readers."""

from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

from astrid.packs.rendering.actions.timeline_visualize import frozen, select


class _Runtime:
    def list_projects(self, *, cursor=None, limit=50):
        return [[{"project_id": "project-1", "slug": "demo", "metadata": {}}], None]

    def list_timelines(self, project_id, *, cursor=None, limit=50):
        assert project_id == "project-1"
        return [[
                {
                    "timeline_id": "timeline-1",
                    "timeline_ulid": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
                    "slug": "main",
                    "name": "Main",
                    "version": 3,
                    "config": {"tracks": []},
                    "registry": {"assets": {}},
                    "head_event_id": "event-3",
                    "head_hash": "hash-3",
                    "updated_at": "2026-08-30T00:00:00Z",
                }
            ], None]


def test_managed_selection_reads_generated_runtime_client():
    timelines, diagnostics = select.select_kernel_timelines(
        Path("/not-an-authority"), project_slug="demo", runtime_client=_Runtime(), default=True
    )

    assert diagnostics == []
    assert [item.slug for item in timelines] == ["main"]
    assert timelines[0].config_version == 3


def test_kernel_visualize_materialization_uses_detached_reader_projection(tmp_path):
    from astrid.core.timeline.reader_projection import READER_PROJECTION_APP_KEY
    from astrid.packs.rendering.actions.timeline_visualize import run

    rate = {"numerator": 30000, "denominator": 1001}
    config = {
        "output": {"resolution": "640x360", "fps": rate, "file": "film.mp4"},
        "tracks": [{"id": "video", "kind": "visual"}],
        "clips": [{"id": "clip", "track": "video", "asset_id": "picture",
                   "at": 0, "hold": 1, "vendor.notes": {"color": "blue"}}],
    }
    registry = {"assets": {"picture": {"media_id": "media-1", "media_type": "video/mp4"}}}
    row = select.KernelTimeline(
        timeline_id="timeline-1", timeline_ulid="01ARZ3NDEKTSV4RRFFQ69G5FAV",
        slug="main", name="Main", is_default=True, config=config, registry=registry,
        config_version=1, head_event_id="event-1", head_hash="hash-1",
        head_created_at="2026-08-30T00:00:00Z",
    )

    materialized = run._materialize_kernel_timeline(
        row, project_root=tmp_path, project_slug="demo", destination=tmp_path / "view"
    )

    projected_clip = materialized.runtime_config["clips"][0]
    assert projected_clip["asset"] == "picture"
    assert "asset_id" not in projected_clip
    assert projected_clip["app"][READER_PROJECTION_APP_KEY]["extensions"]["clip"] == {
        "vendor.notes": {"color": "blue"},
    }
    assert materialized.runtime_config["tracks"][0]["label"] == "video"
    assert materialized.runtime_config["output"]["fps"] == 30000 / 1001
    assert materialized.runtime_config is not row.config
    assert materialized.runtime_registry["assets"]["picture"]["media_id"] == "media-1"
    assert "asset_id" in row.config["clips"][0]
    assert "vendor.notes" in row.config["clips"][0]
    assert row.config["output"]["fps"] == rate
    assert "media_type" in row.registry["assets"]["picture"]


def test_managed_visualization_readers_have_no_local_store_authority():
    select_source = inspect.getsource(select)
    frozen_source = inspect.getsource(frozen)
    for source in (select_source, frozen_source):
        assert "import sqlite3" not in source
        assert "astrid.core.kernel.read" not in source
        assert "astrid.core.store" not in source


def test_frozen_run_info_uses_generated_runtime_client(monkeypatch):
    class Runtime:
        def list_projects(self, *, cursor=None, limit=50):
            return [[{"project_id": "project-1", "slug": "demo"}], None]

        def get_run(self, run_id):
            assert run_id == "run-1"
            return {"project_id": "project-1", "status": "completed", "capability": "rendering.timeline_visualize", "timeline_ids": ["timeline-1"]}

    monkeypatch.setattr(frozen, "_workspace_runtime_client", lambda: Runtime())
    assert frozen._kernel_frozen_run_info("demo", "run-1", Path("/unused")) == {
        "status": "completed",
        "capability": "rendering.timeline_visualize",
        "timeline_ids": ["timeline-1"],
        "project_id": "project-1",
        "current_project_id": "project-1",
    }


def test_frozen_run_info_rejects_a_run_from_another_project(monkeypatch):
    class Runtime:
        def list_projects(self, *, cursor=None, limit=50):
            return [[{"project_id": "project-current", "slug": "demo"}], None]

        def get_run(self, run_id):
            return {"project_id": "project-other", "status": "completed", "capability": "rendering.timeline_visualize"}

    monkeypatch.setattr(frozen, "_workspace_runtime_client", lambda: Runtime())
    info = frozen._kernel_frozen_run_info("demo", "run-1", Path("/unused"))
    assert info["project_id"] != info["current_project_id"]


def test_frozen_run_info_requires_generated_project_page_pair(monkeypatch):
    class Runtime:
        def list_projects(self, *, cursor=None, limit=50):
            return [{"project_id": "project-1", "slug": "demo"}]

        def get_run(self, run_id):
            return {
                "project_id": "project-1",
                "status": "completed",
                "capability": "rendering.timeline_visualize",
            }

    monkeypatch.setattr(frozen, "_workspace_runtime_client", lambda: Runtime())
    assert frozen._kernel_frozen_run_info("demo", "run-1", Path("/unused")) is None


@pytest.mark.parametrize("events", [
    [{"event_type": "task.completed"}],
    {"items": [{"event_type": "task.completed"}], "next_cursor": None},
    [[{"event_type": "task.completed"}], "cursor-1"],
])
def test_settled_outputs_rejects_noncanonical_event_pages(events):
    class Runtime:
        def list_run_events(self, _run_id, *, cursor=None, limit=50):
            return events

    assert frozen._runtime_settled_outputs(Runtime(), "run-1", {}) is None


def test_settled_outputs_accepts_terminal_canonical_event_page():
    event = {
        "event_type": "task.completed",
        "payload": {"result": {"outputs": [{"name": "manifest.json"}]}},
    }

    class Runtime:
        def list_run_events(self, _run_id, *, cursor=None, limit=50):
            return [[event], None]

    assert frozen._runtime_settled_outputs(Runtime(), "run-1", {}) == [
        {"name": "manifest.json"}
    ]


def test_paged_runtime_rows_follows_canonical_cursor_and_rejects_truncation():
    calls = []

    def reader(*, cursor=None, limit=50):
        calls.append((cursor, limit))
        return [[{"id": len(calls)}], "next"] if cursor is None else [[{"id": 2}], None]

    assert frozen._paged_rows(reader) == [{"id": 1}, {"id": 2}]
    assert calls == [(None, 50), ("next", 50)]

    def truncated(*, cursor=None, limit=50):
        return [[{"id": 1}], "next"]

    assert frozen._paged_rows(truncated) is None


def _owned_manifest(tmp_path: Path, run_id: str, payload: bytes) -> tuple[Path, dict]:
    project = tmp_path / "demo"
    path = project / "runs" / run_id / "agent-view" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        # This is the canonical evidence-pack project identity.  Frozen
        # ownership must validate it before checking the exact settled output;
        # omitting it would exercise an obsolete pre-runtime manifest shape.
        "inputs": {
            "timeline_source": ["demo"],
            "resolved_project": {"slug": "demo"},
        },
        "outputs": [{"path": "manifest.json", "content_hash": f"sha256:{digest}", "bytes": len(payload)}],
    }
    return path, manifest


def test_frozen_ownership_binds_exact_settled_outputs_to_path_run(tmp_path, monkeypatch):
    path, manifest = _owned_manifest(tmp_path, "run-b", b"run-a-pack")

    # The copied A view is now under B, while B settled a different manifest.
    # Project/capability/timeline identity alone must not authorize it.
    monkeypatch.setattr(
        frozen,
        "_kernel_frozen_run_info",
        lambda _slug, run_id, _root: {
            "project_id": "project-1",
            "current_project_id": "project-1",
            "status": "completed",
            "capability": "rendering.timeline_visualize",
            "timeline_ids": ["timeline-1"],
            "outputs": [{
                "path": "agent-view/manifest.json",
                "digest": hashlib.sha256(b"run-b-pack").hexdigest(),
                "size": len(b"run-b-pack"),
            }],
        },
    )
    with pytest.raises(frozen.FrozenIntegrityError, match="manifest.json"):
        frozen._verify_run_ownership(path, tmp_path / "demo", manifest, "timeline-1")


def test_frozen_ownership_accepts_exact_settled_output_for_path_run(tmp_path, monkeypatch):
    path, manifest = _owned_manifest(tmp_path, "run-b", b"run-b-pack")
    digest = hashlib.sha256(b"run-b-pack").hexdigest()
    monkeypatch.setattr(
        frozen,
        "_kernel_frozen_run_info",
        lambda _slug, _run_id, _root: {
            "project_id": "project-1",
            "current_project_id": "project-1",
            "status": "completed",
            "capability": "rendering.timeline_visualize",
            "timeline_ids": ["timeline-1"],
            "outputs": [{"path": "agent-view/manifest.json", "digest": digest, "size": len(b"run-b-pack")}],
        },
    )
    frozen._verify_run_ownership(path, tmp_path / "demo", manifest, "timeline-1")


def test_frozen_ownership_rejects_settled_output_without_exact_size(tmp_path, monkeypatch):
    path, manifest = _owned_manifest(tmp_path, "run-b", b"run-b-pack")
    digest = hashlib.sha256(b"run-b-pack").hexdigest()
    monkeypatch.setattr(
        frozen,
        "_kernel_frozen_run_info",
        lambda _slug, _run_id, _root: {
            "project_id": "project-1",
            "current_project_id": "project-1",
            "status": "completed",
            "capability": "rendering.timeline_visualize",
            "timeline_ids": ["timeline-1"],
            "outputs": [{"path": "agent-view/manifest.json", "digest": digest}],
        },
    )
    with pytest.raises(frozen.ContainmentError, match="byte count"):
        frozen._verify_run_ownership(path, tmp_path / "demo", manifest, "timeline-1")


def test_frozen_ownership_rejects_basename_collision_in_settled_path(tmp_path, monkeypatch):
    path, manifest = _owned_manifest(tmp_path, "run-b", b"run-b-pack")
    digest = hashlib.sha256(b"run-b-pack").hexdigest()
    monkeypatch.setattr(
        frozen,
        "_kernel_frozen_run_info",
        lambda _slug, _run_id, _root: {
            "project_id": "project-1",
            "current_project_id": "project-1",
            "status": "completed",
            "capability": "rendering.timeline_visualize",
            "timeline_ids": ["timeline-1"],
            "outputs": [{
                "path": "agent-view/other/manifest.json",
                "digest": digest,
                "size": len(b"run-b-pack"),
            }],
        },
    )
    with pytest.raises(frozen.ContainmentError, match="selected visualization pack"):
        frozen._verify_run_ownership(path, tmp_path / "demo", manifest, "timeline-1")
