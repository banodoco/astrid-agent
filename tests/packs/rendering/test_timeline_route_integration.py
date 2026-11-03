"""Narrow parity checks at the public timelines show/visualize boundary."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from astrid.packs.timeline.cli import (
    _cmd_show,
    _cmd_visualize,
    _visualization_show_range,
    build_parser,
)
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.results import InvocationResult


class _Client:
    def __init__(self) -> None:
        self.visualize_inputs = None
        self.create_inputs = None
        self.timelines = SimpleNamespace(
            create_empty=self.create_empty,
            open_composition=self.open_composition,
            visualize=self.visualize,
        )

    def create_empty(self, **kwargs):  # noqa: ANN001
        self.create_inputs = kwargs
        return DomainResult.success(
            {
                "project_id": kwargs["project"],
                "timeline_id": kwargs["timeline_id"],
                "head_revision_id": "head-created",
                "created": True,
            },
            idempotency_key=kwargs["idempotency_key"],
        )

    def open_composition(self, project, ref, **kwargs):  # noqa: ANN001
        occurrence = kwargs.get("occurrence")
        clip = {
            "clip_id": "clip-b",
            "track_id": "picture",
            "occurrence_id": "occ-b",
            "shot_id": "shot-b",
        }
        return DomainResult.success(
            {
                "kind": "timeline-inspection",
                "summary": {
                    "authority": "canonical_head",
                    "revision_id": "head-1",
                    "head_revision_id": "head-1",
                    "is_current_head": True,
                },
                "query": {"occurrence": occurrence},
                "targets": [{
                    "kind": "clip",
                    "timeline_id": "tl-1",
                    "occurrence_id": "occ-b",
                    "clip_id": "clip-b",
                    "shot_id": "shot-b",
                    "addressable": True,
                }] if occurrence else [],
                "clips": [clip] if occurrence else [],
            }
        )

    def visualize(self, project, ref, **kwargs):  # noqa: ANN001
        self.visualize_inputs = kwargs["options"]
        return DomainResult.success({"kind": "timeline-view", "inspection": {"revision_id": "head-1"}})

    def invoke_result(self, capability_id, **kwargs):  # noqa: ANN001
        self.visualize_inputs = kwargs["inputs"]
        return InvocationResult(
            capability_id=capability_id,
            capability_type="executor",
            native_kind="executor",
            ok=True,
            run_id="run-1",
            kernel_run_id=None,
            kernel_task_id=None,
            kernel_attempt_id=None,
            manifest_path=None,
            outputs={},
        )


class _InputAwareClient(_Client):
    def visualize(self, project, ref, **kwargs):  # noqa: ANN001
        self.visualize_inputs = kwargs["options"]
        self.visualize_mode = kwargs["mode"]
        if kwargs["mode"] == "inputs" and kwargs["options"].get("view") != "filmstrip":
            return DomainResult.failure(
                ErrorObject(
                    "validation_error",
                    "render-free input mode supports only view=filmstrip; composed views require output authority",
                    {"field": "view", "mode": "inputs"},
                )
            )
        return DomainResult.success({"kind": "timeline-view", "inspection": {"revision_id": "head-1"}})


def _presentation_bundle() -> dict:
    return {
        "parent": {"config": {"theme_overrides": {"visual": {"canvas": {"fps": 30}}}}},
        "placements": [{
            "shot_id": "shot-1", "occurrence_id": "occ-1",
            "placement": {"start_ms": 0}, "duration_ms": 2000,
        }],
        "shots": {"shot-1": {
            "payload": {"name": "Opening", "text_bindings": [{
                "kind": "voiceover_script", "text": "Hello world",
            }]},
            "internal_timeline": {"tracks": [
                {"id": "plate", "kind": "visual"},
                {"id": "vo", "kind": "audio"},
            ], "clips": [
                {"id": "plate-1", "track": "plate", "clipType": "media",
                 "asset": "image-a", "at": 0.0, "hold": 2.0},
                {"id": "vo-1", "track": "vo", "clipType": "media",
                 "asset": "voice", "at": 0.0, "hold": 2.0,
                 "app": {"words": [[0.0, 0.4, "Hello"], [0.5, 0.9, "world"]]}},
            ]},
        }},
    }


class _PresentationClient(_Client):
    def __init__(self) -> None:
        super().__init__()
        self.timelines.open_bundle = self.open_bundle

    def open_bundle(self, project, ref, *, revision_id=None):  # noqa: ANN001
        bundle = _presentation_bundle()
        if revision_id == "after":
            bundle["placements"][0]["duration_ms"] = 2500
        return DomainResult.success({
            "bundle": deepcopy(bundle),
            "project_id": project,
            "timeline_id": ref,
            "revision_id": revision_id or "head-1",
        })


def test_create_uses_empty_head_route_and_preserves_v3_config(capsys) -> None:
    client = _Client()
    parser = build_parser(client)

    args = parser.parse_args(
        [
            "create",
            "new-cut",
            "--project",
            "demo",
            "--canvas",
            "1280x720",
            "--fps",
            "24",
            "--idempotency-key",
            "create-key",
        ]
    )
    assert args.handler(args) == 0

    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == {"ok", "data", "error", "receipt", "idempotency_key"}
    assert envelope["ok"] is True
    assert envelope["data"]["timeline_id"] == "new-cut"
    assert envelope["idempotency_key"] == "create-key"
    assert client.create_inputs == {
        "project": "demo",
        "timeline_id": "new-cut",
        "config": {
            "tracks": [],
            "theme_overrides": {"visual": {"canvas": {"width": 1280, "height": 720, "fps": 24}}},
        },
        "idempotency_key": "create-key",
    }


def test_create_rejects_malformed_canvas_before_sdk_call() -> None:
    parser = build_parser(_Client())
    with pytest.raises(SystemExit):
        parser.parse_args(["create", "--project", "demo", "--canvas", "1280"])


def test_show_and_visualize_preserve_one_normalized_occurrence_target(capsys) -> None:
    client = _Client()
    parser = build_parser(client)

    show_args = parser.parse_args(
        ["show", "--project", "demo", "main", "--summary", "--occurrence", "occ-b", "--json"]
    )
    assert _cmd_show(show_args) == 0
    shown = json.loads(capsys.readouterr().out)["data"]
    assert shown["query"]["occurrence"] == "occ-b"
    assert shown["targets"] == [
        {
            "kind": "clip",
            "timeline_id": "tl-1",
            "occurrence_id": "occ-b",
            "clip_id": "clip-b",
            "shot_id": "shot-b",
            "addressable": True,
        }
    ]
    show_to_visualize = shown["navigation"]["commands"]["visualize"]
    assert "timelines visualize" in show_to_visualize
    assert "--revision-id head-1" in show_to_visualize
    assert "--occurrence occ-b" in show_to_visualize

    visualize_args = parser.parse_args(
        ["visualize", "main", "--project", "demo", "--occurrence", "occ-b", "--json"]
    )
    assert _cmd_visualize(visualize_args) == 0
    visualized = json.loads(capsys.readouterr().out)["data"]["outputs"]
    assert client.visualize_inputs["occurrence"] == shown["query"]["occurrence"]
    assert "timelines show" in visualized["navigation"]["commands"]["show"]
    assert "--revision-id head-1" in visualized["navigation"]["commands"]["show"]


def test_show_visualize_round_trip_keeps_saved_revision_after_newer_head(capsys) -> None:
    client = _Client()
    parser = build_parser(client)

    show_args = parser.parse_args(
        ["show", "--project", "demo", "main", "--revision-id", "saved-revision", "--range", "2..4", "--json"]
    )
    assert _cmd_show(show_args) == 0
    shown = json.loads(capsys.readouterr().out)["data"]
    assert "--revision-id saved-revision" in shown["navigation"]["commands"]["visualize"]
    assert "--range 2..4" in shown["navigation"]["commands"]["visualize"]

    visualize_args = parser.parse_args(
        ["visualize", "main", "--project", "demo", "--revision-id", "saved-revision", "--range", "2..4", "--json"]
    )
    assert _cmd_visualize(visualize_args) == 0
    visualized = json.loads(capsys.readouterr().out)["data"]["outputs"]
    assert "--revision-id saved-revision" in visualized["navigation"]["commands"]["show"]
    assert "--range 2..4" in visualized["navigation"]["commands"]["show"]


def test_visualize_show_navigation_projects_frame_and_timestamp_to_ranges(capsys) -> None:
    client = _Client()
    parser = build_parser(client)

    frame_args = parser.parse_args(
        ["visualize", "main", "--project", "demo", "--frame", "1", "--json"]
    )
    assert _cmd_visualize(frame_args) == 0
    frame_outputs = json.loads(capsys.readouterr().out)["data"]["outputs"]
    assert "--range 1/30..1/15" in frame_outputs["navigation"]["commands"]["show"]

    at_args = parser.parse_args(
        ["visualize", "main", "--project", "demo", "--at", "4", "--context", "1", "--json"]
    )
    assert _cmd_visualize(at_args) == 0
    at_outputs = json.loads(capsys.readouterr().out)["data"]["outputs"]
    assert "--range 3..5" in at_outputs["navigation"]["commands"]["show"]


def test_visualize_show_navigation_reads_fps_from_frame_index(tmp_path) -> None:
    frame_index = tmp_path / "frame-index.json"
    frame_index.write_text(
        json.dumps({"provenance": {"fps_rational": [24, 1]}}),
        encoding="utf-8",
    )
    assert _visualization_show_range(
        {"frame": 1}, outputs={"frame_index": str(frame_index)}
    ) == "1/24..1/12"


def test_plain_show_returns_the_canonical_inspection_shape(capsys) -> None:
    client = _Client()
    parser = build_parser(client)
    args = parser.parse_args(["show", "--project", "demo", "main", "--json"])
    assert _cmd_show(args) == 0
    shown = json.loads(capsys.readouterr().out)["data"]
    assert shown["kind"] == "timeline-inspection"
    assert shown["summary"]["authority"] == "canonical_head"


def test_visualize_forwards_admitted_contact_and_motion_options(capsys) -> None:
    client = _Client()
    parser = build_parser(client)
    args = parser.parse_args([
        "visualize", "main", "--project", "demo", "--view", "overview",
        "--cut", "2", "--layer", "sync,curves", "--beats", '{"beats":[1.0]}',
        "--frame-budget", "60", "--preset", "motion", "--window", "2.5",
        "--rules", '{"params":{"min_text_px":12},"severity":{},"disable":[]}',
        "--cuts", "1,3", "--preview", "--json",
    ])
    assert _cmd_visualize(args) == 0
    assert client.visualize_inputs == {
        "formats": ["png", "md"],
        "timeline_slug": "main",
        "view": "contact",
        "resolution": "480x270",  # the overview's review scale
        "cut": "2",
        "layers": ["sync", "curves"],
        "beats": {"beats": [1.0]},
        "frame_budget": 60,
        "preset": "motion",
        "window_s": 2.5,
        "rules": {"params": {"min_text_px": 12}, "severity": {}, "disable": []},
        "cuts": "1,3",
        "preview": True,
    }
    capsys.readouterr()


def test_visualize_inputs_defaults_to_native_filmstrip_but_explicit_contact_rejects(capsys) -> None:
    client = _InputAwareClient()
    parser = build_parser(client)

    args = parser.parse_args([
        "visualize", "main", "--project", "demo", "--mode", "inputs", "--format", "md", "--json",
    ])
    assert _cmd_visualize(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert client.visualize_mode == "inputs"
    assert client.visualize_inputs == {
        "formats": ["md"],
        "timeline_slug": "main",
        "view": "filmstrip",
    }

    args = parser.parse_args([
        "visualize", "main", "--project", "demo", "--hide", "output", "--preset", "overview", "--json",
    ])
    assert _cmd_visualize(args) == 0
    json.loads(capsys.readouterr().out)
    assert client.visualize_mode == "inputs"
    assert client.visualize_inputs["view"] == "filmstrip"

    args = parser.parse_args([
        "visualize", "main", "--project", "demo", "--mode", "inputs", "--view", "contact", "--json",
    ])
    assert _cmd_visualize(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == {
        "code": "validation_error",
        "message": "render-free input mode supports only view=filmstrip; composed views require output authority",
        "details": {"field": "view", "mode": "inputs"},
    }

    args = parser.parse_args(["visualize", "main", "--project", "demo", "--json"])
    assert _cmd_visualize(args) == 0
    json.loads(capsys.readouterr().out)
    assert client.visualize_mode == "auto"
    assert client.visualize_inputs["view"] == "contact"


def test_visualize_forwards_admitted_diff_revision_selectors(capsys) -> None:
    client = _Client()
    parser = build_parser(client)
    args = parser.parse_args([
        "visualize", "main", "--project", "demo", "--view", "diff",
        "--before-revision-id", "rev-before", "--after-revision-id", "rev-after", "--json",
    ])
    assert _cmd_visualize(args) == 0
    assert client.visualize_inputs == {
        "formats": ["png", "md"],
        "timeline_slug": "main",
        "view": "diff",
        "before_revision_id": "rev-before",
        "after_revision_id": "rev-after",
    }
    capsys.readouterr()


def test_visualize_diff_requires_explicit_project_timeline_and_revisions(capsys) -> None:
    client = _Client()
    parser = build_parser(client)
    args = parser.parse_args([
        "visualize", "--view", "diff", "--before-revision-id", "rev-before", "--json",
    ])
    assert _cmd_visualize(args) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "validation_error"
    assert "--project" in error["message"]
    assert client.visualize_inputs is None

    args = parser.parse_args([
        "visualize", "main", "--project", "demo", "--view", "diff",
        "--before-revision-id", "rev-before", "--json",
    ])
    assert _cmd_visualize(args) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "validation_error"
    assert "both" in error["message"]
    assert client.visualize_inputs is None


def test_remote_visualize_forwards_diff_without_caller_compiled_payload() -> None:
    captured = {}
    service = object.__new__(RemoteTimelines)
    service._client = SimpleNamespace()
    service._resolve_timeline = lambda _project, _ref: DomainResult.success({
        "timeline_id": "tl-1", "project_ref": "demo", "timeline_ref": "main",
        "project_id": "project-1", "head_revision_id": "head-1",
    })

    def invoke(capability_id, **kwargs):  # noqa: ANN001
        captured["capability_id"] = capability_id
        captured.update(kwargs)
        return DomainResult.success({"kind": "timeline-revision-diff"})

    service._invoker = invoke
    result = service.visualize(
        "demo",
        "main",
        options={
            "view": "diff",
            "before_revision_id": "rev-before",
            "after_revision_id": "rev-after",
        },
    )
    assert result.ok
    assert captured["capability_id"] == "rendering.timeline_visualize"
    assert captured["project"] == "demo"
    assert captured["inputs"]["project_slug"] == "demo"
    assert captured["inputs"]["timeline_slug"] == "main"
    assert captured["inputs"]["view"] == "diff"
    assert captured["inputs"]["before_revision_id"] == "rev-before"
    assert captured["inputs"]["after_revision_id"] == "rev-after"
    assert "revision_diff" not in captured["inputs"]


def test_remote_visualize_rejects_diff_without_explicit_after_revision() -> None:
    service = object.__new__(RemoteTimelines)
    service._client = SimpleNamespace()
    service._invoker = lambda *_args, **_kwargs: pytest.fail("diff must fail before invocation")
    result = service.visualize(
        "demo", "main", options={"view": "diff", "before_revision_id": "rev-before"}
    )
    assert not result.ok
    assert result.error.code == "validation_error"
    assert "after_revision_id" in result.error.details["fields"]


def test_show_as_script_uses_actual_immutable_bundle_parser(capsys) -> None:
    client = _PresentationClient()
    parser = build_parser(client)
    args = parser.parse_args([
        "show", "--project", "demo", "main", "--as", "script",
        "--range", "0..2", "--json",
    ])
    assert _cmd_show(args) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["presentation"]["kind"] == "script"
    assert "┃ CUT 1" in data["presentation"]["text"]


def test_diff_as_cuts_uses_pinned_before_after_bundles(capsys) -> None:
    client = _PresentationClient()
    parser = build_parser(client)
    args = parser.parse_args([
        "diff", "--project", "demo", "main", "--from-revision", "before",
        "--to-revision", "after", "--as", "cuts", "--json",
    ])
    assert args.handler(args) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["presentation"]["kind"] == "cuts"
    assert data["presentation"]["diff"]["placement_changes"]
    assert "Cut diff" in data["presentation"]["text"]


@pytest.mark.parametrize("mode", ["cuts", "script", "code"])
def test_diff_json_presentation_keeps_exact_revision_inputs(mode, monkeypatch, capsys) -> None:
    from astrid.packs.timeline import cli_common

    client = _PresentationClient()
    revisions = []
    original_opener = client.timelines.open_bundle

    def open_exact(project, ref, *, revision_id=None):  # noqa: ANN001
        revisions.append(revision_id)
        return original_opener(project, ref, revision_id=revision_id)

    client.timelines.open_bundle = open_exact
    monkeypatch.setattr(
        cli_common, "working_copy_view",
        lambda *_args, **_kwargs: pytest.fail("explicit revision presentation must not read a working copy"),
    )
    args = build_parser(client).parse_args([
        "diff", "--project", "demo", "main", "--from-revision", "before",
        "--to-revision", "after", "--as", mode, "--json",
    ])
    assert args.handler(args) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["kind"] == "timeline-diff-presentation"
    assert data["from_revision"] == "before"
    assert data["to_revision"] == "after"
    assert revisions == ["before", "after"]
    presentation = data["presentation"]
    assert presentation["kind"] == mode
    assert presentation["diff"]["placement_changes"]
    assert presentation["before_text"]
    assert presentation["after_text"]
    assert "diff" in presentation["text"].lower()


def test_diff_json_without_presentation_keeps_raw_diff(capsys) -> None:
    args = build_parser(_PresentationClient()).parse_args([
        "diff", "--project", "demo", "main", "--from-revision", "before",
        "--to-revision", "after", "--json",
    ])
    assert args.handler(args) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert "presentation" not in data
    assert data["placement_changes"]
    assert data["from_revision"] == "before"
    assert data["to_revision"] == "after"


def test_remote_visualize_keeps_input_only_fail_closed_for_contact() -> None:
    service = object.__new__(RemoteTimelines)
    service._client = SimpleNamespace()
    service._invoker = None
    service._resolve_timeline = lambda _project, _ref: DomainResult.success({
        "timeline_id": "tl-1", "project_ref": "demo", "timeline_ref": "main",
        "head_revision_id": "head-1",
    })
    result = service.visualize("demo", "main", mode="inputs", options={"view": "contact"})
    assert not result.ok
    assert result.error.code == "validation_error"
    assert result.error.details == {"field": "view", "mode": "inputs"}
    assert "view=filmstrip" in result.error.message
    assert "output authority" in result.error.message
    assert result.data is None


def test_show_prefers_shared_open_composition_adapter_when_available(capsys) -> None:
    class OpenClient(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.opened = None
            self.timelines.open_composition = self.open_composition

        def open_composition(self, project, ref, **kwargs):  # noqa: ANN001
            self.opened = (project, ref, kwargs)
            return DomainResult.success({
                "kind": "timeline-inspection", "summary": {"authority": "canonical_head"},
                "query": {"occurrence": kwargs["occurrence"]}, "targets": [], "clips": [],
            })

    client = OpenClient()
    parser = build_parser(client)
    args = parser.parse_args(["show", "--project", "demo", "main", "--summary", "--occurrence", "occ-b", "--json"])
    assert _cmd_show(args) == 0
    assert client.opened[0:2] == ("demo", "main")
    assert client.opened[2]["occurrence"] == "occ-b"
    assert json.loads(capsys.readouterr().out)["data"]["kind"] == "timeline-inspection"


def test_show_fails_closed_without_canonical_adapter(capsys) -> None:
    class LegacyOnly:
        def __init__(self) -> None:
            self.timelines = SimpleNamespace(
                show=lambda *_args: DomainResult.success({"config": {"clips": []}})
            )

    parser = build_parser(LegacyOnly())
    args = parser.parse_args(["show", "--project", "demo", "main", "--json"])
    assert _cmd_show(args) == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "unavailable"
