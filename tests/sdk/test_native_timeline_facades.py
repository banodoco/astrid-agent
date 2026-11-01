from __future__ import annotations

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTimelines


class _NativeTransport:
    def __init__(self) -> None:
        self.inspect_calls = []
        self.view_calls = []

    def list_timelines(self, project, *, cursor=None, limit=50):
        return [[{"timeline_id": "tl-1", "slug": "main", "parent_revision_id": "rev-7"}], None]

    def get_timeline(self, timeline_id, *, project_id=None):
        raise AssertionError("canonical inspection must not read the legacy timeline document")

    def inspect_timeline(self, project, timeline, *, options):
        self.inspect_calls.append((project, timeline, options))
        return {
            "schema": "runtime.timeline.declared_inputs/v1",
            "project_id": project,
            "timeline_id": timeline,
            "revision_id": "rev-7",
            "snapshot_digest": "sha256:snapshot",
            "selectors": options,
            "selection_status": "selected",
            "target_count": 1,
            "selected": [{
                "role": "target",
                "occurrence": {"occurrence_id": "occ-1", "shot_id": "shot-1"},
                "clips": [{"clip_id": "clip-1", "track_id": "picture"}],
            }],
        }

    def create_timeline_view(self, project, timeline, *, options):
        self.view_calls.append((project, timeline, options))
        return {
            "inspection": {"revision_id": "rev-7", "snapshot_digest": "sha256:snapshot", "selectors": options},
            "evidence_kind": "declared_inputs",
            "render_requested": False,
            "formats": {"md": {"status": "available", "object_id": "sha256:md"}},
            "artifacts": {"md": {"object_id": "sha256:md", "media_type": "text/markdown"}},
        }


class _SelectedNativeTransport(_NativeTransport):
    def current_project(self):
        return {"project": {"project_id": "project-1", "slug": "demo"}}

    def get_project(self, project):
        assert project == "project-1"
        return {
            "project_id": "project-1",
            "slug": "demo",
            "metadata": {"default_timeline_id": "main"},
        }


def test_remote_show_uses_runtime_current_project_and_project_default() -> None:
    transport = _SelectedNativeTransport()
    result = RemoteTimelines(transport).show(None, None)
    assert result.ok
    assert transport.inspect_calls[0][0:2] == ("project-1", "tl-1")


def test_remote_list_without_selection_returns_recovery_error() -> None:
    class _NoSelection(_SelectedNativeTransport):
        def current_project(self):
            return {"selection": None}

    result = RemoteTimelines(_NoSelection()).list(None)
    assert not result.ok
    assert result.error.code == "not_found"
    assert result.error.details["next_action"] == "astrid projects select <project>"


def test_remote_default_timeline_must_belong_to_selected_project() -> None:
    class _CrossProjectDefault(_SelectedNativeTransport):
        def get_project(self, project):
            row = super().get_project(project)
            row["metadata"] = {"default_timeline_id": "other-project-timeline"}
            return row

    result = RemoteTimelines(_CrossProjectDefault()).show(None, None)
    assert not result.ok
    assert result.error.code == "not_found"
    assert result.error.details == {
        "project": "project-1",
        "ref": "other-project-timeline",
    }


def test_remote_open_composition_uses_runtime_inspection_authority() -> None:
    transport = _NativeTransport()
    result = RemoteTimelines(transport).open_composition(
        "project-1", "main", occurrence="occ-1", neighbors=1
    )
    assert result.ok
    assert result.data["summary"]["snapshot_digest"] == "sha256:snapshot"
    assert [target["kind"] for target in result.data["targets"]] == ["occurrence", "clip"]
    assert result.data["targets"][0]["occurrence_id"] == "occ-1"
    assert result.data["targets"][1]["clip_id"] == "clip-1"
    assert transport.inspect_calls == [(
        "project-1", "tl-1", {
            "limit": 50, "detail": False, "neighbors": 1, "revision_id": "rev-7",
            "occurrence": "occ-1",
        }
    )]


def test_remote_show_is_canonical_current_head_alias() -> None:
    transport = _NativeTransport()
    result = RemoteTimelines(transport).show("project-1", "main")
    assert result.ok
    assert result.data["summary"]["revision_id"] == "rev-7"
    assert transport.inspect_calls[0][1] == "tl-1"


def test_remote_open_composition_forwards_native_cursor_without_legacy_read() -> None:
    transport = _NativeTransport()
    result = RemoteTimelines(transport).open_composition(
        "project-1", "main", cursor="cursor-1", limit=3
    )
    assert result.ok
    assert transport.inspect_calls[0][2]["cursor"] == "cursor-1"
    assert transport.inspect_calls[0][2]["limit"] == 3


def test_native_projection_keeps_parent_effect_targets_without_shot_identity() -> None:
    transport = _NativeTransport()

    original_inspect = transport.inspect_timeline

    def inspect(project, timeline, *, options):  # noqa: ANN001
        data = original_inspect(project, timeline, options=options)
        data["selected"] = []
        data["selected_parent_clips"] = [{
            "clip_id": "effect-1",
            "track_id": "effects",
            "clip_type": "element",
            "target_kind": "parent_clip",
            "element_ref": {"id": "blur", "kind": "effect"},
            "parameters": {"amount": 0.5},
        }]
        return data

    transport.inspect_timeline = inspect
    result = RemoteTimelines(transport).open_composition("project-1", "main")
    assert result.ok
    assert result.data["targets"] == [{
        "kind": "parent_clip",
        "target_kind": "parent_clip",
        "timeline_id": "tl-1",
        "clip_id": "effect-1",
        "track_id": "effects",
        "element_ref": {"id": "blur", "kind": "effect"},
        "addressable": True,
    }]
    assert result.data["clips"][0]["target_kind"] == "parent_clip"


def test_native_projection_keeps_each_child_target_for_multi_child_occurrence() -> None:
    transport = _NativeTransport()
    original_inspect = transport.inspect_timeline

    def inspect(project, timeline, *, options):  # noqa: ANN001
        data = original_inspect(project, timeline, options=options)
        data["selected"][0]["clips"] = [
            {"clip_id": "clip-a", "track_id": "picture"},
            {"clip_id": "clip-b", "track_id": "effects"},
        ]
        return data

    transport.inspect_timeline = inspect
    result = RemoteTimelines(transport).open_composition("project-1", "main", occurrence="occ-1")
    assert result.ok
    assert [(target["kind"], target.get("clip_id")) for target in result.data["targets"]] == [
        ("occurrence", None), ("clip", "clip-a"), ("clip", "clip-b")
    ]


def test_remote_visualize_creates_runtime_owned_view_without_executor() -> None:
    transport = _NativeTransport()
    result = RemoteTimelines(transport).visualize(
        "project-1", "main", occurrence="occ-1", neighbors=1, formats=("md", "png")
    )
    assert isinstance(result, DomainResult)
    assert not result.ok
    assert result.error.code == "render_required"
    assert result.error.details["input_only_available"] is True
    assert result.error.details["next_actions"]
    assert transport.view_calls == []


def test_remote_visualize_default_requests_composed_output_with_input_lanes() -> None:
    transport = _SelectedNativeTransport()
    calls = []

    def invoke(capability_id, **kwargs):
        calls.append((capability_id, kwargs))
        return DomainResult.success({"mode": "composed_capture"})

    result = RemoteTimelines(transport, invoker=invoke).visualize(
        "project-1", "main", formats=("md", "png"), options={"every_frames": 6},
    )

    assert result.ok
    assert calls[0][0] == "rendering.timeline_visualize"
    inputs = calls[0][1]["inputs"]
    assert inputs["composed_capture"] is True
    assert inputs["show"] == ["output", "inputs", "text", "audio"]
    assert inputs["every_frames"] == 6


def test_remote_visualize_captures_current_head_without_implicitly_reusing_old_render(monkeypatch) -> None:
    transport = _SelectedNativeTransport()
    calls = []

    monkeypatch.setattr(
        "astrid.sdk.timeline_filmstrip.matching_composed_render",
        lambda *args, **kwargs: "run-current",
    )

    def invoke(capability_id, **kwargs):
        calls.append((capability_id, kwargs))
        return DomainResult.success({"mode": "filmstrip"})

    result = RemoteTimelines(transport, invoker=invoke).visualize(
        "project-1", "main", formats=("md", "png"),
    )

    assert result.ok
    inputs = calls[0][1]["inputs"]
    assert "render_run" not in inputs
    assert inputs["auto_selected_render"] is True
    assert inputs["composed_capture"] is True


def test_remote_visualize_preserves_an_explicit_output_only_surface() -> None:
    transport = _SelectedNativeTransport()
    calls = []

    def invoke(capability_id, **kwargs):
        calls.append((capability_id, kwargs))
        return DomainResult.success({"mode": "composed_capture"})

    result = RemoteTimelines(transport, invoker=invoke).visualize(
        "project-1", "main", options={"show": ["output"], "hide": ["inputs"]},
    )

    assert result.ok
    assert calls[0][1]["inputs"]["show"] == ["output"]
    assert calls[0][1]["inputs"]["hide"] == ["inputs"]


def test_native_visualize_rejects_arbitrary_output_path() -> None:
    result = RemoteTimelines(_NativeTransport()).visualize("project-1", "main", out="view.md")
    assert isinstance(result, DomainResult)
    assert not result.ok
    assert result.error.code == "validation_error"


def test_inputs_mode_never_reads_render_history() -> None:
    transport = _NativeTransport()
    result = RemoteTimelines(transport).visualize(
        "project-1", "main", mode="inputs", occurrence="occ-1"
    )
    assert result.ok
    assert result.data["evidence_kind"] == "declared_inputs"
    # _NativeTransport intentionally has no list_project_runs method.  The
    # render-free input route must still work on that minimal native surface.
    assert transport.view_calls


def test_composed_mode_fails_closed_when_no_exact_render_reader_exists() -> None:
    result = RemoteTimelines(_NativeTransport()).visualize(
        "project-1", "main", mode="composed", occurrence="occ-1"
    )
    assert isinstance(result, DomainResult)
    assert not result.ok
    assert result.error.code == "render_required"
    assert result.error.details["timeline_id"] == "tl-1"
    assert result.error.details["next_actions"]


def test_explicit_composed_run_bypasses_current_head_discovery() -> None:
    transport = _NativeTransport()
    calls = []

    def invoke(capability_id, **kwargs):
        calls.append((capability_id, kwargs))
        return DomainResult.success({"selected": kwargs["inputs"]["render_run"]})

    result = RemoteTimelines(transport, invoker=invoke).visualize(
        "project-1", "main", mode="composed",
        options={"render_run": "candidate-render-7"},
    )

    assert result.ok
    assert result.data == {"selected": "candidate-render-7"}
    assert calls[0][0] == "rendering.timeline_visualize"
    assert calls[0][1]["inputs"]["render_run"] == "candidate-render-7"
    assert calls[0][1]["inputs"]["composed_capture"] is True
    assert "revision_id" not in calls[0][1]["inputs"]
    # The native transport deliberately has no run-list reader. Explicit
    # selection is resolved to its saved revision by composed-capture
    # admission downstream.
    assert transport.view_calls == []


@pytest.mark.parametrize("mode", ["auto", "composed"])
def test_visualize_composes_saved_revision_when_exact_matching_render_exists(monkeypatch, mode) -> None:
    from astrid.sdk.timeline_filmstrip import matching_composed_render

    video_digest = "sha256:" + "a" * 64

    class _MatchingRenderTransport(_NativeTransport):
        def __init__(self):
            super().__init__()
            self.run_list_calls = 0

        def list_project_runs(self, project, *, cursor=None, limit=50):
            self.run_list_calls += 1
            return [[{
                "id": "exact-render-1", "created_at": "2026-10-07T10:00:00Z",
                "capability": "rendering.render", "status": "succeeded",
            }], None]

        def get_run(self, run_id):
            return {
                "id": run_id, "project_id": "project-1", "capability": "rendering.render",
                "status": "succeeded", "task_ids": ["render-task-1"],
            }

        def get_task(self, task_id):
            return {
                "id": task_id, "state": "succeeded", "capability_id": "rendering.render",
                "spec": {"spec": {"inputs": {"timeline_authority": {
                    "timeline_id": "tl-1", "parent_revision_id": "rev-7",
                }}}},
                "result": {"outputs": [{
                    "output_port": "video", "digest": video_digest,
                    "managed_object_reference": {"object_id": video_digest},
                    "filename": "render.mp4", "media_type": "video/mp4", "size": 100,
                    "ordinal": 0, "role": "result",
                }]},
            }

        def list_project_objects(self, project, *, cursor=None, limit=50):
            return [[{"object_id": video_digest}], None]

    transport = _MatchingRenderTransport()
    assert matching_composed_render(
        transport,
        project_id="project-1",
        timeline={"timeline_id": "tl-1", "parent_revision_id": "rev-7"},
    ) == "exact-render-1"
    prior_history_reads = transport.run_list_calls
    calls = []

    def invoke(capability_id, **kwargs):
        calls.append((capability_id, kwargs))
        return DomainResult.success({"captured": True})

    result = RemoteTimelines(transport, invoker=invoke).visualize(
        "project-1", "main", mode=mode
    )

    assert result.ok
    assert calls[0][0] == "rendering.timeline_visualize"
    inputs = calls[0][1]["inputs"]
    assert inputs["composed_capture"] is True
    assert inputs["revision_id"] == "rev-7"
    assert "render_run" not in inputs
    assert "rendered_video" not in inputs
    assert "video_digest" not in inputs
    assert transport.run_list_calls == prior_history_reads


def test_visualize_honors_explicit_revision_for_composed_capture() -> None:
    calls = []

    def invoke(capability_id, **kwargs):
        calls.append((capability_id, kwargs))
        return DomainResult.success({"captured": True})

    result = RemoteTimelines(_NativeTransport(), invoker=invoke).visualize(
        "project-1", "main", mode="auto", revision_id="saved-revision-2"
    )

    assert result.ok
    inputs = calls[0][1]["inputs"]
    assert inputs["composed_capture"] is True
    assert inputs["revision_id"] == "saved-revision-2"
    assert "render_run" not in inputs
