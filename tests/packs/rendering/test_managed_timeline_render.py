"""Contracts for canonical managed rendering through an explicit runtime client."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


from astrid.packs.rendering.actions.render import managed_timeline
from astrid.packs.rendering.actions.render.managed_timeline import (
    ManagedRenderValidationError,
    materialize_managed_render_snapshot,
    resolve_managed_render_snapshot,
    validate_managed_render_snapshot,
)
from astrid.sdk.exceptions import CapabilityValidationError
from astrid.sdk.invocation import _prepare_managed_render_inputs


@pytest.mark.parametrize("location", ["config", "duplicate", "payload"])
def test_saved_parent_clip_precedence_preserves_prepared_scene(location):
    from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition
    from tests.packs.rendering.test_live_scene_boundary import _package

    clips = [{"id": "scene", "clipType": "haus.astrid.liveScene", "track": "v1", "at": 50, "from": 63, "to": 70, "speed": 2, "app": {"liveScene": _package()}}]
    config = {"tracks": [{"id": "v1", "kind": "visual"}], "clips": clips if location != "payload" else [], "theme": "banodoco-default", "app": {"sentinel": 1}}
    parent = {"project_id": "project-demo", "timeline_id": "timeline-1", "revision_id": "saved", "payload": {"config": config, "clips": clips if location != "config" else [], "registry": {"assets": {}}, "occurrences": []}}
    projected = project_runtime_parent_composition(parent, shot_revisions=[], internal_timeline_revisions=[])
    assert projected.config["clips"] == clips
    assert projected.config["app"]["sentinel"] == 1
    assert projected.config["tracks"] == config["tracks"]


def test_saved_parent_disagreeing_populated_clips_fail_closed():
    from astrid.core.timeline.shot_composition_projection import (
        ShotCompositionProjectionError,
        project_runtime_parent_composition,
    )

    parent = {"project_id": "p", "timeline_id": "t", "revision_id": "r", "payload": {"config": {"clips": [{"id": "a"}]}, "clips": [{"id": "b"}], "occurrences": [], "registry": {"assets": {}}}}
    with pytest.raises(ShotCompositionProjectionError, match="disagree"):
        project_runtime_parent_composition(parent, shot_revisions=[], internal_timeline_revisions=[])


def test_managed_saved_prepared_scene_validates_and_corruption_fails():
    from tests.packs.rendering.test_live_scene_boundary import _package

    runtime = _Runtime()
    package = _package()
    runtime.timeline["config"] = {"tracks": [{"id": "v1", "kind": "visual", "label": "V1"}], "clips": [{"id": "scene", "track": "v1", "clipType": "haus.astrid.liveScene", "at": 0, "from": 0, "to": 1, "app": {"liveScene": package}}]}
    snapshot = resolve_managed_render_snapshot(project_ref="demo", timeline_ref="main", client=runtime)
    validate_managed_render_snapshot(snapshot)
    package["html"] += "corrupt"
    snapshot = resolve_managed_render_snapshot(project_ref="demo", timeline_ref="main", client=runtime)
    with pytest.raises(ManagedRenderValidationError, match="entry digest"):
        validate_managed_render_snapshot(snapshot)


def _result(data: object = None, *, ok: bool = True) -> SimpleNamespace:
    return SimpleNamespace(ok=ok, data=data, error=None if ok else {"message": "not found"})


class _Runtime:
    def __init__(self, *, media: list[dict] | None = None, archived: bool = False) -> None:
        self.media_rows = media or []
        self.extra_timelines: dict[str, dict] = {}
        self.shot_rows: dict[str, dict] = {}
        self.text_binding_rows: list[dict] = []
        self.parent_revisions: dict[str, dict] = {}
        self.exact_shot_revisions: dict[tuple[str, str], dict] = {}
        self.internal_revisions: dict[str, dict] = {}
        self.project = {"id": "project-demo", "project_id": "project-demo", "slug": "demo"}
        self.timeline = {
            "timeline_id": "timeline-1",
            "timeline_ulid": "01J00000000000000000000001",
            "project_id": "project-demo",
            "project_slug": "demo",
            "slug": "main",
            "config_version": 1,
            "version": 1,
            "head_revision_id": "parent-default",
            "config": {"tracks": [], "clips": []},
            "registry": {"assets": {}},
            "archived_at": "archived" if archived else None,
        }
        self.projects = SimpleNamespace(show=lambda _ref: _result(self.project))
        self.timelines = SimpleNamespace(show=self._show_timeline, list=self._list_timelines, inspect=self._inspect_timeline)
        # Remote SDK list reads expose the generated client's JSON-safe page
        # pair, including the terminal null cursor.
        self.media = SimpleNamespace(
            list=lambda _project, **_kwargs: _result([self.media_rows, None])
        )
        self.shots = SimpleNamespace(show=self._show_shot, list_text_bindings=lambda _project, **kwargs: _result([[row for row in self.text_binding_rows if row["shot_id"] == kwargs["shot_id"]], None]))

    def _show_timeline(self, _project: str, ref: str):
        row = self.extra_timelines.get(ref)
        if row is None and ref not in {
            self.timeline["timeline_id"], self.timeline["timeline_ulid"], self.timeline["slug"],
            "11111111-1111-4111-8111-111111111111",
        }:
            row = next((item for item in self.extra_timelines.values() if ref in {
                item.get("timeline_id"), item.get("timeline_ulid"), item.get("slug")
            }), None)
        return _result(row or self.timeline)

    def _list_timelines(self, _project: str, **_kwargs: object):
        row = dict(self.timeline)
        row.setdefault("version", row.get("config_version", 1))
        row.setdefault("head_revision_id", "parent-default")
        return _result([[row, *self.extra_timelines.values()], None])

    def _inspect_timeline(self, _project: str, ref: str, **_kwargs: object):
        timeline_id = self.timeline["timeline_id"]
        if ref not in {timeline_id, self.timeline["slug"], self.timeline["timeline_ulid"]}:
            return _result(None, ok=False)
        head = self.timeline.get("head_revision_id") or "parent-default"
        if head == "parent-default":
            self.parent_revisions[head] = {
            "project_id": self.project["project_id"],
            "timeline_id": timeline_id,
            "revision_id": head,
            "content_digest": "sha256:" + "e" * 64,
            "payload": {
                "config": self.timeline.get("config", {"tracks": [], "clips": []}),
                "registry": self.timeline.get("registry", {"assets": {}}),
                "clips": self.timeline.get("config", {}).get("clips", []),
                "occurrences": self.timeline.get("occurrences", []),
            },
            }
        return _result({
            "timeline_id": timeline_id,
            "representation": "canonical_head",
            "is_current_head": True,
            "revision_id": head,
            "head_revision_id": head,
        })

    def _show_shot(self, _project: str, ref: str):
        row = self.shot_rows.get(ref)
        return _result(row, ok=row is not None)

    def get_project_parent_composition_revision(
        self, _project: str, _timeline: str, revision: str
    ):
        return self.parent_revisions[revision]

    def get_project_shot_revision(
        self, _project: str, shot_id: str, revision: str
    ):
        return self.exact_shot_revisions[(shot_id, revision)]

    def get_project_timeline_revision(
        self, _project: str, _timeline: str, revision: str
    ):
        return self.internal_revisions[revision]


def _snapshot(runtime: _Runtime, *, expected_version: int | None = None):
    return resolve_managed_render_snapshot(
        project_ref="demo",
        timeline_ref="main",
        expected_version=expected_version,
        client=runtime,
    )


def test_resolve_requires_explicit_runtime_client_and_pins_runtime_identity() -> None:
    runtime = _Runtime()
    snapshot = _snapshot(runtime, expected_version=1)
    assert snapshot.project_id == "project-demo"
    assert snapshot.timeline_id == "timeline-1"
    assert snapshot.registry == {"assets": {}}


def test_resolve_accepts_canonical_version_when_legacy_config_version_is_absent() -> None:
    runtime = _Runtime()
    runtime.timeline["version"] = runtime.timeline.pop("config_version")

    snapshot = _snapshot(runtime, expected_version=1)

    assert snapshot.config_version == 1


def test_admission_rejects_range_beyond_pinned_timeline() -> None:
    runtime = _Runtime()
    runtime.timeline["config"] = {
        "tracks": [{"id": "v1", "kind": "visual", "label": "V1"}],
        "clips": [{"id": "title", "track": "v1", "clipType": "text", "at": 0, "hold": 10,
                   "text": {"content": "Range"}}],
        "theme_overrides": {"visual": {"canvas": {"width": 320, "height": 180, "fps": 30}}},
    }

    with pytest.raises(CapabilityValidationError, match="beyond timeline duration 10s"):
        _prepare_managed_render_inputs(
            {"timeline_ref": "main", "range": [9, 11]}, project="demo", _client=runtime
        )


@pytest.mark.parametrize("canonical_record_only", [False, True])
def test_exact_parent_head_projects_pinned_children_with_unique_local_ids(
    canonical_record_only: bool,
) -> None:
    digests = [character * 64 for character in ("a", "b", "c")]
    runtime = _Runtime(
        media=[
            {"media_id": digest, "digest": digest, "project_id": "project-demo"}
            for digest in digests
        ]
    )
    runtime.timeline["head_revision_id"] = "parent-committed"
    runtime.timeline["config"] = {
        "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}],
        "clips": [{
            "id": "mutable-shot-placeholder",
            "at": 0,
            "hold": 3,
            "track": "picture",
            "clipType": "shot",
            "params": {"shot_id": "mutable", "timeline_document_id": "mutable-child"},
        }],
    }
    runtime.extra_timelines["mutable-child"] = {
        "timeline_id": "mutable-child",
        "slug": "mutable-child",
        "config_version": 9,
        "config": {"tracks": [], "clips": [{"id": "mutable-only"}]},
        "registry": {"assets": {}},
        "archived_at": None,
    }
    occurrences = []
    for index, digest in enumerate(digests):
        shot_id = f"shot-{index}"
        shot_revision_id = f"shot-rev-{index}"
        internal_revision_id = f"internal-rev-{index}"
        occurrence_id = f"occ-{index}"
        occurrences.append({
            "occurrence_id": occurrence_id,
            "shot_id": shot_id,
            "shot_revision_id": shot_revision_id,
            "placement": {"start_ms": index * 1000, "track": "picture"},
            "duration_ms": 1000,
            "source_offset": 0,
            "speed": 1,
            "gain": 1,
            "mute": False,
            "track": "picture",
            "transform": {},
            "provenance": {},
        })
        runtime.exact_shot_revisions[(shot_id, shot_revision_id)] = {
            "project_id": "project-demo",
            "shot_id": shot_id,
            "revision_id": shot_revision_id,
            "internal_timeline_revision_id": internal_revision_id,
            "content_digest": f"sha256:{digest}",
            "payload": {
                "metadata": {"name": f"Pinned {index}"},
                "text_bindings": [],
            },
        }
        runtime.internal_revisions[internal_revision_id] = {
            "project_id": "project-demo",
            "timeline_id": "timeline-1",
            "revision_id": internal_revision_id,
            "content_digest": f"sha256:{digest}",
            "payload": {
                "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}],
                "clips": [{
                    "id": "charcoal-process-preview",
                    "asset": "preview",
                    "at": 0,
                    "hold": 1,
                    "track": "picture",
                    "clipType": "image",
                }],
                "registry": {"assets": {"preview": {
                    "media_id": digest,
                    "content_sha256": digest,
                    "type": "image",
                }}},
            },
        }
    runtime.parent_revisions["parent-committed"] = {
        "project_id": "project-demo",
        "timeline_id": "timeline-1",
        "revision_id": "parent-committed",
        "content_digest": f"sha256:{'d' * 64}",
        "payload": {
            "config": {
                "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}],
                "clips": [],
            },
            "registry": {"assets": {}},
            "clips": [],
            "occurrences": occurrences,
        },
    }
    if canonical_record_only:
        # The public Runtime read used by the A06 canary exposes the canonical
        # timeline head/version without the legacy document or display slug.
        runtime.timelines.show = lambda _project, _ref: _result({
            "timeline_id": "timeline-1",
            "project_id": "project-demo",
            "head_revision_id": "parent-committed",
            "version": 1,
        })

    snapshot = _snapshot(runtime)
    assert snapshot.timeline_slug == "main"
    if canonical_record_only:
        preview_base = resolve_managed_render_snapshot(
            project_ref="demo", timeline_ref="main", client=runtime,
            candidate_preview=True,
        )
        assert preview_base.project_id == "project-demo"
        assert preview_base.timeline_id == "timeline-1"
        assert preview_base.head_event_id == "parent-committed"
        assert preview_base.head_hash == "d" * 64
        assert preview_base.config["clips"] == []
        assert preview_base.expansion is None

    clips = snapshot.config["clips"]
    assert [clip["id"] for clip in clips] == [
        "occ-0:charcoal-process-preview",
        "occ-1:charcoal-process-preview",
        "occ-2:charcoal-process-preview",
    ]
    assert len({clip["id"] for clip in clips}) == 3
    assert [clip["at"] for clip in clips] == [0, 1, 2]
    assert "mutable-shot-placeholder" not in {clip["id"] for clip in clips}
    assert [clip["asset"] for clip in clips] == [
        "occ-0:preview",
        "occ-1:preview",
        "occ-2:preview",
    ]
    assert [clip["at"] for clip in clips] == [0.0, 1.0, 2.0]
    assert [row["at_ms"] for row in snapshot.expansion["outputs"]] == [0, 1000, 2000]
    assert [
        snapshot.registry["assets"][clip["asset"]]["media_id"] for clip in clips
    ] == digests
    assert snapshot.expansion["parent_revision_id"] == "parent-committed"
    assert snapshot.head_event_id == "parent-committed"

    prepared, authority = _prepare_managed_render_inputs(
        {"timeline_ref": "main"}, project="demo", _client=runtime
    )
    assert [
        clip["id"] for clip in prepared["timeline_snapshot"]["config"]["clips"]
    ] == [
        "occ-0:charcoal-process-preview",
        "occ-1:charcoal-process-preview",
        "occ-2:charcoal-process-preview",
    ]
    assert authority["head_event_id"] == "parent-committed"
    assert authority["parent_revision_id"] == "parent-committed"


def test_materialize_writes_deterministic_private_snapshot(tmp_path: Path) -> None:
    timeline_path, registry_path, authority = materialize_managed_render_snapshot(
        tmp_path, _snapshot(_Runtime())
    )
    materialized = json.loads(timeline_path.read_text())
    assert materialized["tracks"] == [] and materialized["clips"] == []
    assert materialized["app"]["astrid_shot_composition"]["parent_revision_id"] == "parent-default"
    assert json.loads(registry_path.read_text()) == {"assets": {}}
    assert authority["authority"] == "kernel"
    assert authority["project_slug"] == "demo"
    assert timeline_path.parent.name == registry_path.parent.name


def test_runtime_admitted_media_stays_an_identity_not_a_local_cas_path(tmp_path: Path) -> None:
    digest = "a" * 64
    runtime = _Runtime(media=[{"media_id": "media-1", "digest": digest}])
    runtime.timeline["registry"] = {"assets": {"hero": {"media_id": "media-1", "content_sha256": digest}}}
    snapshot = _snapshot(runtime)
    asset = snapshot.registry["assets"]["hero"]
    assert asset["media_id"] == "media-1"
    assert asset["content_sha256"] == digest
    assert "file" not in asset
    assert ".astrid" not in json.dumps(snapshot.registry)


@pytest.mark.parametrize("page", [
    [{"media_id": "media-1"}],
    {"items": [{"media_id": "media-1"}], "next_cursor": None},
    [[{"media_id": "media-1"}], "cursor-1"],
])
def test_runtime_media_admission_requires_terminal_canonical_page(page) -> None:
    class Client:
        media = SimpleNamespace(list=lambda _project: _result(page))

    assert managed_timeline._runtime_media_admissions(Client(), "demo") == {}


def test_runtime_media_identity_mismatch_fails_closed() -> None:
    runtime = _Runtime(media=[{"media_id": "media-1", "digest": "a" * 64}])
    runtime.timeline["registry"] = {"assets": {"hero": {"media_id": "media-1", "content_sha256": "b" * 64}}}
    with pytest.raises(ManagedRenderValidationError, match="claims digest.*runtime admitted") as error:
        _snapshot(runtime)
    assert error.value.details["validator"] == "managed_media_runtime_admission"
    assert error.value.details["reason"] == (
        "authored media identity does not match project runtime admission"
    )


@pytest.mark.parametrize("timeline_ref", [
    "main",
    "timeline-1",
    "01J00000000000000000000001",
])
def test_resolve_accepts_slug_uuid_and_ulid_runtime_references(timeline_ref: str) -> None:
    runtime = _Runtime()
    snapshot = resolve_managed_render_snapshot(
        project_ref="demo", timeline_ref=timeline_ref, client=runtime
    )
    assert snapshot.timeline_id == "timeline-1"
    assert snapshot.timeline_slug == "main"


def test_materialization_records_authority_and_content_hashes(tmp_path: Path) -> None:
    snapshot = _snapshot(_Runtime())
    timeline_path, registry_path, authority = materialize_managed_render_snapshot(tmp_path, snapshot)
    assert timeline_path.is_file() and registry_path.is_file()
    assert authority["head_event_id"] == "parent-default"
    for field in ("head_hash", "config_hash", "registry_hash", "materialized_registry_hash"):
        assert len(authority[field]) == 64
    assert authority["config_hash"] == snapshot.config_hash
    assert authority["registry_hash"] == snapshot.registry_hash


def test_authority_uses_immutable_parent_head_even_when_event_log_exists() -> None:
    runtime = _Runtime()
    runtime.app = SimpleNamespace(event_log=SimpleNamespace(list_events=lambda **_kwargs: [
        SimpleNamespace(subject_id="timeline-1", seq=1, event_id="event-1", event_hash="f" * 64)
    ]))
    snapshot = _snapshot(runtime)
    assert snapshot.head_event_id == "parent-default"
    assert snapshot.head_hash == "e" * 64
    assert snapshot.authority()["parent_revision_id"] == "parent-default"
    # The snapshot reference carries the parent's digest; the Runtime refuses a revision ref without it.
    assert snapshot.authority()["parent_content_digest"] == "sha256:" + "e" * 64


def test_candidate_authority_does_not_label_base_parent_as_saved_candidate() -> None:
    from dataclasses import replace

    candidate = replace(_snapshot(_Runtime()), authoring_preview={
        "candidate_parent_revision_id": "unpublished-parent",
        "candidate_digest": "sha256:" + "a" * 64,
    })
    authority = candidate.authority()
    assert "parent_revision_id" not in authority
    assert authority["head_event_id"] == "parent-default"
    assert authority["render_mode"] == "authoring_candidate_preview"
    assert authority["authoring_preview"]["candidate_parent_revision_id"] == "unpublished-parent"


def test_unadmitted_and_foreign_runtime_media_fail_closed() -> None:
    timeline = _Runtime()
    timeline.timeline["registry"] = {"assets": {"hero": {"media_id": "m1", "content_sha256": "a" * 64}}}
    with pytest.raises(ManagedRenderValidationError, match="not admitted"):
        _snapshot(timeline)

    foreign = _Runtime(media=[{"media_id": "m1", "digest": "a" * 64, "project_slug": "other"}])
    foreign.timeline["registry"] = timeline.timeline["registry"]
    with pytest.raises(ManagedRenderValidationError, match="not admitted"):
        _snapshot(foreign)


def test_conflicting_runtime_media_aliases_fail_closed() -> None:
    runtime = _Runtime(media=[{"media_id": "m1", "digest": "a" * 64}])
    runtime.timeline["registry"] = {"assets": {
        "one": {"media_id": "m1", "content_sha256": "a" * 64},
        "two": {"media_id": "m1", "content_sha256": "b" * 64},
    }}
    with pytest.raises(ManagedRenderValidationError, match="conflicting entries"):
        _snapshot(runtime)


def test_retired_locator_is_rejected_before_runtime_materialization() -> None:
    runtime = _Runtime()
    runtime.timeline["registry"] = {"assets": {"hero": {"file": "/tmp/hero.mp4", "media_id": "media-1"}}}
    with pytest.raises(ManagedRenderValidationError, match="retired media locator"):
        _snapshot(runtime)


def test_stale_and_archived_timelines_are_rejected() -> None:
    with pytest.raises(ValueError, match="stale timeline version"):
        _snapshot(_Runtime(), expected_version=2)
    with pytest.raises(ValueError, match="is archived"):
        _snapshot(_Runtime(archived=True))


def test_managed_preflight_requires_runtime_ref_and_rejects_file_mode(tmp_path: Path) -> None:
    with pytest.raises(CapabilityValidationError, match=r"requires timeline=.*timeline_ref="):
        _prepare_managed_render_inputs({}, project="demo")
    with pytest.raises(CapabilityValidationError, match="mutually exclusive"):
        _prepare_managed_render_inputs(
            {"timeline": "export.json", "timeline_ref": "main"},
            project="demo",
        )


def test_managed_preflight_resolves_selected_project_default_timeline() -> None:
    runtime = _Runtime()
    runtime.project["metadata"] = {"default_timeline_id": "main"}
    runtime.projects.current = lambda: _result(
        {"project": {"project_id": "project-demo", "slug": "demo"}}
    )
    prepared, authority = _prepare_managed_render_inputs(
        {}, project=None, _client=runtime
    )
    assert prepared["timeline_ref"] == "main"
    assert authority["project_slug"] == "demo"


def test_managed_preflight_rejects_malformed_frozen_speech_before_admission() -> None:
    runtime = _Runtime()
    speech = {
        "speech_annotations": [{
            "annotation_id": "speech-shotless-01",
            "start": 248.0,
            "end": 252.5,
            "text": "We stay with the ending.",
            "source_type": "verified_speech",
        }],
        "speech_occurrences": [{
            "occurrence_id": "speech-occurrence-speech-shotless-01",
            "annotation_id": "speech-shotless-01",
            "from": 0,
            "to": 297,
            "placement": 0,
            "speed": 1,
        }],
        "source_audio_digest": "sha256:" + "a" * 64,
        "annotation_digest": "sha256:" + "b" * 63,
    }
    with pytest.raises(CapabilityValidationError, match="annotation_digest must be a sha256 digest"):
        _prepare_managed_render_inputs(
            {"timeline_ref": "main", **speech}, project="demo", _client=runtime
        )


def test_managed_preflight_rejects_unmatched_speech_linkage() -> None:
    runtime = _Runtime()
    with pytest.raises(CapabilityValidationError, match="no matching annotation"):
        _prepare_managed_render_inputs(
            {
                "timeline_ref": "main",
                "speech_annotations": [{
                    "annotation_id": "segment_id=speech-shotless-01",
                    "start": 248.0,
                    "end": 252.5,
                    "text": "We stay with the ending.",
                    "source_type": "verified_speech",
                }],
                "speech_occurrences": [{
                    "occurrence_id": "speech-occurrence-speech-shotless-01",
                    "annotation_id": "speech-shotless-01",
                    "from": 0,
                    "to": 297,
                    "placement": 0,
                    "speed": 1,
                }],
            },
            project="demo",
            _client=runtime,
        )


def test_snapshot_validation_rejects_missing_registry_asset() -> None:
    runtime = _Runtime()
    runtime.timeline["config"] = {
        "tracks": [{"id": "visual", "kind": "visual", "label": "Visual"}],
        "clips": [{"id": "source", "at": 0, "track": "visual", "clipType": "video", "asset": "missing"}],
    }
    with pytest.raises(ValueError, match="missing registry asset"):
        validate_managed_render_snapshot(_snapshot(runtime))


def test_snapshot_validation_accepts_revision_pinned_astrid_element() -> None:
    from astrid.core.element import catalog as element_catalog

    descriptor = next(item for item in element_catalog.list_element_descriptors() if item["kind"] == "effect")
    runtime = _Runtime()
    runtime.timeline["config"] = {
        "tracks": [{"id": "visual", "kind": "visual", "label": "Visual"}],
        "clips": [{
            "id": "overlay",
            "at": 0,
            "hold": 1,
            "track": "visual",
            "clipType": descriptor["id"],
            "elementRef": {
                "id": descriptor["id"],
                "kind": "effect",
                "revision": descriptor["revision"],
            },
        }],
    }
    validate_managed_render_snapshot(_snapshot(runtime))


def test_snapshot_validation_rejects_stale_or_draft_pinned_element() -> None:
    from astrid.core.element import catalog as element_catalog

    descriptor = next(item for item in element_catalog.list_element_descriptors() if item["kind"] == "effect")
    runtime = _Runtime()
    base_clip = {
        "id": "overlay",
        "at": 0,
        "hold": 1,
        "track": "visual",
        "clipType": descriptor["id"],
    }
    runtime.timeline["config"] = {
        "tracks": [{"id": "visual", "kind": "visual", "label": "Visual"}],
        "clips": [{**base_clip, "elementRef": {"id": descriptor["id"], "kind": "effect", "revision": "sha256:stale"}}],
    }
    with pytest.raises(ManagedRenderValidationError, match="stale revision"):
        validate_managed_render_snapshot(_snapshot(runtime))

    runtime.timeline["config"]["clips"] = [{
        **base_clip,
        "elementRef": {"id": descriptor["id"], "kind": "effect", "revision": "draft-local"},
    }]
    with pytest.raises(ManagedRenderValidationError, match="preview-only"):
        validate_managed_render_snapshot(_snapshot(runtime))


def _render_clock() -> dict[str, object]:
    return {
        "authored_duration_frames": 8910,
        "render_duration_frames": 9000,
        "tail": {
            "policy": "unmapped_excess_rendered_region",
            "source_asset": "tail",
            "start_frame": 8910,
            "end_frame": 9000,
            "source_start_frame": 8910,
            "source_end_frame": 9000,
        },
    }


def _clock_runtime() -> _Runtime:
    digest = "a" * 64
    runtime = _Runtime(media=[{"media_id": "media-tail", "digest": digest}])
    runtime.timeline["config"] = {
        "tracks": [{"id": "visual", "kind": "visual", "label": "Visual"}],
        "clips": [{
            "id": "base", "track": "visual", "at": 0, "hold": 297,
            "clipType": "media", "asset": "base",
        }],
        "app": {"astrid_render_clock": _render_clock()},
        "output": {"resolution": "640x360", "fps": 30, "file": "fixture.mp4"},
    }
    runtime.timeline["registry"] = {
        "assets": {
            "base": {"media_id": "media-tail", "content_sha256": digest},
            "tail": {"media_id": "media-tail", "content_sha256": digest},
        }
    }
    return runtime


def test_render_clock_keeps_authored_and_rendered_extents_distinct() -> None:
    from astrid.core.timeline.duration import (
        render_clock,
        timeline_duration_frames,
        timeline_render_duration_frames,
    )

    timeline = {
        "clips": [{"at": 0, "hold": 297}],
        "app": {"astrid_render_clock": _render_clock()},
    }
    assert timeline_duration_frames(timeline, 30) == 8910
    assert timeline_render_duration_frames(timeline, 30) == 9000
    assert render_clock(timeline, 30)["tail"]["source_asset"] == "tail"


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda clock: clock.update(render_duration_frames=8910), "longer than authored"),
        (lambda clock: clock["tail"].update(end_frame=8999), "cover exactly"),
        (lambda clock: clock["tail"].update(policy="hold"), "policy"),
        (lambda clock: clock["tail"].update(source_asset=""), "source_asset"),
    ],
)
def test_render_clock_rejects_ambiguous_or_short_tail(mutate, message) -> None:
    from astrid.core.timeline.duration import timeline_render_duration_frames

    clock = _render_clock()
    mutate(clock)
    with pytest.raises(ValueError, match=message):
        timeline_render_duration_frames(
            {"clips": [{"at": 0, "hold": 297}], "app": {"astrid_render_clock": clock}},
            30,
        )


def test_managed_render_clock_requires_runtime_admitted_tail_identity() -> None:
    runtime = _clock_runtime()
    snapshot = _snapshot(runtime)
    validate_managed_render_snapshot(snapshot)
    assert snapshot.authority()["render_clock"]["render_duration_frames"] == 9000

    runtime.timeline["registry"]["assets"].pop("tail")
    with pytest.raises(ManagedRenderValidationError, match="tail source asset"):
        validate_managed_render_snapshot(_snapshot(runtime))


def test_snapshot_validation_accepts_music_bed_and_visible_title_tracks() -> None:
    digest = "b" * 64
    runtime = _Runtime(media=[{"media_id": "media-music", "digest": digest}])
    runtime.timeline["config"] = {
        "tracks": [
            {"id": "visual", "kind": "visual", "label": "Visual"},
            {"id": "titles", "kind": "visual", "label": "Titles"},
            {"id": "music", "kind": "audio", "label": "Music"},
        ],
        "clips": [
            {"id": "music-bed", "track": "music", "at": 0, "hold": 90, "clipType": "media",
             "asset": "music-bed", "volume": 1},
            {"id": "visible-title", "track": "titles", "at": 0, "hold": 60, "clipType": "text",
             "text": {"content": "Astrid"}},
        ],
        "output": {"resolution": "640x360", "fps": 30, "file": "fixture.mp4"},
    }
    runtime.timeline["registry"] = {
        "assets": {"music-bed": {"media_id": "media-music", "content_sha256": digest}}
    }

    snapshot = _snapshot(runtime)
    validate_managed_render_snapshot(snapshot)

    clips = {row["id"]: row for row in snapshot.config["clips"]}
    assert clips["music-bed"]["track"] == "music"
    assert clips["music-bed"]["asset"] in snapshot.registry["assets"]
    assert clips["visible-title"]["track"] == "titles"
    assert clips["visible-title"]["text"]["content"] == "Astrid"
    assert {"music", "titles"} <= {row["id"] for row in snapshot.config["tracks"]}


def test_snapshot_validation_rejects_incomplete_config_output() -> None:
    runtime = _Runtime()
    runtime.timeline["config"] = {"tracks": [], "clips": [], "output": {"resolution": [1920, 1080]}}
    with pytest.raises(ValueError, match="config.output is incomplete.*fps.*file"):
        validate_managed_render_snapshot(_snapshot(runtime))


def _profile() -> dict[str, object]:
    return {
        "width": 1920, "height": 1080, "fps_rational": [30, 1],
        "time_base": [1, 90000], "container": "mp4", "video_codec": "h264",
        "video_profile": None, "video_level": None, "pixel_format": "yuv420p",
        "audio_codec": "aac", "audio_sample_rate": 48000,
        "audio_channel_layout": "stereo", "duration_tolerance": 1,
    }


def test_render_profile_shape_type_and_canvas_mismatch_are_actionable(tmp_path: Path) -> None:
    runtime = _Runtime()
    with pytest.raises(CapabilityValidationError, match="flat RenderProfile v1"):
        _prepare_managed_render_inputs({"timeline_ref": "main", "profile": {"video": {}}}, project="demo", _client=runtime)
    bad_type = _profile(); bad_type["width"] = "1920"
    with pytest.raises(CapabilityValidationError, match="width must be an integer"):
        _prepare_managed_render_inputs({"timeline_ref": "main", "profile": bad_type}, project="demo", _client=runtime)
    bad_canvas = _profile(); bad_canvas.update(width=320, height=180, fps_rational=[24, 1])
    with pytest.raises(CapabilityValidationError, match="authoritative theme canvas"):
        _prepare_managed_render_inputs({"timeline_ref": "main", "profile": bad_canvas}, project="demo", _client=runtime)


def test_legacy_shot_shell_is_rejected_before_render_admission() -> None:
    runtime = _Runtime()
    runtime.timeline["config"] = {"tracks": [], "clips": [{
        "id": "shot", "at": 0, "hold": 1, "clipType": "shot",
        "track": "picture",
        "params": {"shot_id": "shot-1", "timeline_document_id": "child"},
    }]}
    with pytest.raises(CapabilityValidationError, match="legacy clipType=shot"):
        _prepare_managed_render_inputs({"timeline_ref": "main"}, project="demo", _client=runtime)


def test_legacy_shot_review_context_is_rejected_before_projection() -> None:
    runtime = _Runtime()
    runtime.timeline["config"] = {"tracks": [], "clips": [{
        "id": "shot", "at": 1.25, "hold": 2.75, "clipType": "shot",
        "track": "picture",
        "params": {"shot_id": "shot-1", "timeline_document_id": "child"},
    }]}
    with pytest.raises(CapabilityValidationError, match="legacy clipType=shot"):
        _prepare_managed_render_inputs(
            {"timeline_ref": "main", "review": True, "review_context": {"shots": ["forged"]}},
            project="demo", _client=runtime,
        )


def test_unknown_effect_structured_schema_and_opaque_params_contracts() -> None:
    runtime = _Runtime()
    runtime.timeline["config"] = {"tracks": [{"id": "v", "kind": "visual", "label": "Visual"}], "clips": [{
        "id": "unknown", "at": 0, "track": "v", "clipType": "missing-effect", "hold": 1,
        "params": {"vendor": {"effect": "not-an-element"}},
    }]}
    with pytest.raises(ManagedRenderValidationError, match="unregistered reusable visual element"):
        validate_managed_render_snapshot(_snapshot(runtime))

    runtime.timeline["config"]["clips"][0].update(
        clipType="text",
        # Keep this a valid built-in text clip so the assertion below reaches
        # the deliberately malformed reusable-effect payload.
        text={"content": "fixture"},
    )
    runtime.timeline["config"]["clips"][0]["effects"] = [{"id": "bad", "params": {"amount": 1}}]
    with pytest.raises(ManagedRenderValidationError) as error:
        validate_managed_render_snapshot(_snapshot(runtime))
    assert error.value.details["path"].startswith("$.clips[0].effects")

    runtime.timeline["config"]["clips"][0].pop("effects")
    validate_managed_render_snapshot(_snapshot(runtime))


def test_alpha_mov_compatibility_requires_matching_profile(tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.timeline["config"] = {"metadata": {"astrid_layer": {"alpha": True}}, "tracks": [], "clips": []}
    with pytest.raises(CapabilityValidationError, match="incompatible explicit render profile"):
        profile = _profile(); profile["container"] = "mov"
        _prepare_managed_render_inputs({"timeline_ref": "main", "output_name": "alpha.mov", "profile": profile}, project="demo", _client=runtime)
    profile = _profile(); profile.update(container="mov", video_codec="prores", pixel_format="yuva444p12le", audio_codec="pcm_s16le")
    prepared, _authority = _prepare_managed_render_inputs({"timeline_ref": "main", "output_name": "alpha.mov", "profile": profile}, project="demo", _client=runtime)
    assert prepared["profile"] == profile


@pytest.mark.parametrize('data', [None, [[], 'more'], [[{'binding_id': 'bad'}], None]])
def test_render_rejects_incomplete_or_invalid_shot_text_snapshot(data):
    from astrid.sdk.render_shot_snapshot import shot_text_snapshot
    client = SimpleNamespace(shots=SimpleNamespace(list_text_bindings=lambda *args, **kwargs: _result(data)))
    with pytest.raises(CapabilityValidationError):
        shot_text_snapshot(client, 'project', 'shot')
