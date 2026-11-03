from __future__ import annotations

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.workspace_client import WorkspaceClientError

pytest_plugins = ("tests.sdk.test_scene_save_core",)


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

    def create_timeline_view(self, project, timeline, *, options, idempotency_key):
        self.view_calls.append((project, timeline, options, idempotency_key))
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


class _CatalogTransport(_SelectedNativeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.recover_calls = []
        self.catalog_version = 7

    def list_timelines(self, project, *, cursor=None, limit=50):
        assert project == "project-1"
        return [[{
            "timeline_id": "tl-1",
            "slug": "main",
            "version": self.catalog_version,
            "archived": True,
            "head_revision_id": "head-7",
            "document_digest": "sha256:document",
        }], None]

    def recover_timeline(self, timeline_id, *, expected_version, version, idempotency_key):
        self.recover_calls.append((timeline_id, expected_version, version, idempotency_key))
        return {
            "timeline_id": timeline_id,
            "archived": False,
            "version": 8,
            "head_revision_id": "head-7",
            "document_digest": "sha256:document",
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


def test_catalog_recover_without_version_unarchives_current_entry_preserving_film() -> None:
    transport = _CatalogTransport()
    result = RemoteTimelines(transport).recover(
        "project-1", "main", idempotency_key="catalog-recover"
    )

    assert result.ok
    assert transport.recover_calls == [("tl-1", 7, 7, "catalog-recover")]
    assert result.data == {
        "timeline_id": "tl-1",
        "archived": False,
        "version": 8,
        "head_revision_id": "head-7",
        "document_digest": "sha256:document",
    }

    replay = RemoteTimelines(transport).recover(
        "project-1", "main", version=7, idempotency_key="catalog-recover"
    )
    assert replay.ok
    assert replay.data == result.data
    assert transport.recover_calls[-1] == ("tl-1", 7, 7, "catalog-recover")


def test_catalog_recover_current_version_unarchives_current_entry() -> None:
    transport = _CatalogTransport()
    result = RemoteTimelines(transport).recover(
        "project-1", "main", version=7, idempotency_key="catalog-current"
    )

    assert result.ok
    assert transport.recover_calls == [("tl-1", 7, 7, "catalog-current")]
    assert result.data["archived"] is False
    assert result.data["head_revision_id"] == "head-7"
    assert result.data["document_digest"] == "sha256:document"


def test_catalog_recover_exact_retry_forwards_original_version_after_catalog_advance() -> None:
    transport = _CatalogTransport()
    first = RemoteTimelines(transport).recover(
        "project-1", "main", expected_version=7, version=7,
        idempotency_key="catalog-recover-retry",
    )
    transport.catalog_version = 8

    replay = RemoteTimelines(transport).recover(
        "project-1", "main", expected_version=7, version=7,
        idempotency_key="catalog-recover-retry",
    )

    assert first.ok and replay.ok
    assert replay.data == first.data
    assert transport.recover_calls == [
        ("tl-1", 7, 7, "catalog-recover-retry"),
        ("tl-1", 7, 7, "catalog-recover-retry"),
    ]


def test_legacy_timeline_recover_refuses_non_current_from_runtime_without_writes() -> None:
    # Forward fresh historical requests too: Runtime's ledger must distinguish
    # them from an exact accepted replay before refusing without film writes.
    class RuntimeRefusesHistorical(_CatalogTransport):
        def recover_timeline(self, timeline_id, *, expected_version, version, idempotency_key):
            self.recover_calls.append((timeline_id, expected_version, version, idempotency_key))
            raise WorkspaceClientError(
                410,
                "retired_route",
                "legacy timeline recover cannot restore a historical film version; restore a revision with timeline.restore",
                {
                    "status": "refused",
                    "reason": "timeline_restore_required",
                    "replacement": "timeline.restore",
                    "requested_version": version,
                    "current_version": self.catalog_version,
                },
            )

    transport = RuntimeRefusesHistorical()
    result = RemoteTimelines(transport).recover(
        "project-1", "main", expected_version=7, version=6,
        idempotency_key="legacy-recover",
    )
    assert not result.ok
    assert result.idempotency_key == "legacy-recover"
    assert result.error.code == "retired_route"
    assert result.error.details == {
        "status": "refused",
        "reason": "timeline_restore_required",
        "replacement": "timeline.restore",
        "requested_version": 6,
        "current_version": 7,
    }
    assert "timeline.restore" in result.error.message
    assert "version" in result.error.message
    assert transport.recover_calls == [("tl-1", 7, 6, "legacy-recover")]


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


def test_remote_open_composition_raw_projection_is_renderer_free(request) -> None:
    request.getfixturevalue("no_renderer")
    result = RemoteTimelines(_NativeTransport()).open_composition(
        "project-1", "main", _raw_projection=True,
    )

    assert result.ok
    assert result.data["summary"]["revision_id"] == "rev-7"
    assert result.data["clips"] == [{
        "clip_id": "clip-1",
        "track_id": "picture",
        "occurrence_id": "occ-1",
        "shot_id": "shot-1",
        "track": "picture",
    }]


def test_remote_open_composition_retains_public_timing_enrichment() -> None:
    class _TimedNativeTransport(_NativeTransport):
        def inspect_timeline(self, project, timeline, *, options):
            data = super().inspect_timeline(project, timeline, options=options)
            clip = data["selected"][0]["clips"][0]
            authored = {
                "id": "clip-1", "clipType": "media", "at": 0, "hold": 1,
                "track": "picture", "effects": {"fade_in": 0.25},
                "transition": {"id": "fade"},
                "params": {"sourceSegments": [{"at": 0, "sourceStart": 0}]},
            }
            clip.update(
                authored_fields=authored,
                render_timing={key: authored[key] for key in ("id", "clipType", "at", "hold", "track")},
            )
            data["render_timing_context"] = {
                "status": "complete", "fps": 30,
                "tracks": [{"id": "picture", "kind": "visual"}],
                "clips": [clip["render_timing"]],
            }
            return data

    result = RemoteTimelines(_TimedNativeTransport()).open_composition("project-1", "main")

    assert result.ok
    clip = result.data["clips"][0]
    assert clip["compositor_dispatch"]["status"] == "resolved"
    assert clip["timing_projection"]["mounted"] == {
        "start_frame": 0, "end_frame": 30, "start": [0, 1], "end": [1, 1],
    }
    assert clip["timed_changes"]
    assert clip["timing_unknowns"] == []
    assert clip["transition_projection"]["status"] == "unresolved"
    assert clip["source_clock"] == "source playback segments use a separate media clock"


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


def test_inputs_mode_reuses_exact_key_and_options_for_a_transport_retry() -> None:
    transport = _NativeTransport()
    timelines = RemoteTimelines(transport)
    options = {"revision_id": "rev-7", "formats": ["md"]}

    first = timelines._typed(
        "create_timeline_view",
        "project-1",
        "tl-1",
        options=options,
        key="view-retry-1",
    )
    replay = timelines._typed(
        "create_timeline_view",
        "project-1",
        "tl-1",
        options=options,
        key="view-retry-1",
    )

    assert first.ok and replay.ok
    assert transport.view_calls == [
        ("project-1", "tl-1", options, "view-retry-1"),
        ("project-1", "tl-1", options, "view-retry-1"),
    ]


def test_inputs_mode_allocates_a_fresh_key_for_a_new_head_request() -> None:
    transport = _NativeTransport()
    timelines = RemoteTimelines(transport)

    first = timelines.visualize("project-1", "main", mode="inputs")
    refreshed = timelines.visualize("project-1", "main", mode="inputs")

    assert first.ok and refreshed.ok
    assert first.idempotency_key
    assert refreshed.idempotency_key
    assert first.idempotency_key != refreshed.idempotency_key
    assert transport.view_calls[0][2] == transport.view_calls[1][2]
    assert transport.view_calls[0][3] == first.idempotency_key
    assert transport.view_calls[1][3] == refreshed.idempotency_key


def test_inputs_mode_surfaces_read_only_refusal_before_view_write() -> None:
    class ReadOnlyTransport(_NativeTransport):
        def create_timeline_view(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise WorkspaceClientError(
                401,
                "forbidden",
                "projects:write is required to create a timeline view",
                {"required_scope": "projects:write"},
            )

    result = RemoteTimelines(ReadOnlyTransport()).visualize(
        "project-1", "main", mode="inputs"
    )

    assert not result.ok
    assert result.error.code == "forbidden"
    assert result.error.details == {"required_scope": "projects:write"}


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


@pytest.mark.parametrize("method", ["show", "inspect"])
def test_read_paths_never_create_view_artifacts(method) -> None:
    transport = _NativeTransport()
    result = getattr(RemoteTimelines(transport), method)("project-1", "main")
    assert result.ok
    assert transport.inspect_calls
    assert transport.view_calls == []


def test_view_retry_preserves_original_key_and_input_after_lost_ack() -> None:
    class LostAckTransport(_NativeTransport):
        def create_timeline_view(self, project, timeline, *, options, idempotency_key):
            result = super().create_timeline_view(
                project, timeline, options=options, idempotency_key=idempotency_key)
            if len(self.view_calls) == 1:
                raise WorkspaceClientError(503, "unavailable", "Acknowledgement lost", {})
            return result

    transport = LostAckTransport()
    timelines = RemoteTimelines(transport)
    options = {"revision_id": "rev-7", "formats": ["md"]}
    first = timelines._typed("create_timeline_view", "project-1", "tl-1", options=options)
    assert not first.ok and first.idempotency_key
    recovered = timelines._typed("create_timeline_view", "project-1", "tl-1", options=options,
                                 key=first.idempotency_key)
    assert recovered.ok and recovered.idempotency_key == first.idempotency_key
    assert transport.view_calls[0] == transport.view_calls[1]
