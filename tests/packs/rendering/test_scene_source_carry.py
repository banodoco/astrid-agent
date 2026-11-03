"""Retained SDK projection -> final Root callback -> fresh-only public labels.

The capture backend writes fixture PNGs; source projection and public scene
queries use either the real native host or an explicit transport double. These
tests establish the source join, not rendered pixels.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.rendering import scene_answers
from astrid.core.timeline import Timeline
from astrid.packs.rendering.actions.timeline_visualize import composed_frame
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import (
    _contact_overlay_for_layers,
    attach_frame_addresses,
)
from astrid.packs.rendering.rendering.renderers.remotion import run
from astrid.packs.rendering.shared import rendering_backend
from astrid.sdk.timeline_filmstrip import prepare_filmstrip
from tests.packs.rendering import test_capture_scene_labels as scene_fixture

host = scene_fixture.host
native = scene_fixture.native


@pytest.fixture(params=["native", "transport"])
def source_query(request, monkeypatch):
    if request.param == "native":
        request.getfixturevalue("native")
        return

    def answers(props, basis, frames):
        # Static answers for this fixture's two occurrences. No evaluator or
        # visibility calculation is supplied by the Python transport double.
        ids = {0: "opaque:occ:A:opaque:source:title",
               24: "opaque:occ:B:opaque:source:title",
               29: "opaque:occ:A:opaque:source:title",
               30: "opaque:occ:B:opaque:source:title"}
        return [{"frame": frame, "width": basis["width"], "height": basis["height"],
                 "coverage": "UNKNOWN" if frame != 48 else "GAP", "content": None,
                 "findings": [], "layers": [] if frame == 48 else [{
                     "clipId": ids[frame], "role": "content", "visible": True,
                     "opacity": 1, "fullFrame": True,
                     "bounds": {"status": "known", "rect": {
                         "x": 0, "y": 0, "width": basis["width"], "height": basis["height"]}},
                 }]} for frame in frames]

    monkeypatch.setattr(scene_answers, "host_frames_at", answers)


class Revisions:
    """The live listing differs from the explicitly requested historical parent."""

    def __init__(self, kind="text-card"):
        self.reads = []
        self.head = "late-head"
        self.internal = {
            "project_id": "p", "timeline_id": "tl", "revision_id": "internal:old",
            "content_digest": "sha256:" + "1" * 64,
            "payload": {
                "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}, {"id": "audio", "kind": "audio", "label": "Audio"}],
                "clips": [
                    {"id": "opaque:source:title", "track": "picture", "clipType": kind,
                     "at": 0, "hold": 1, "params": {"content": "Historical title"},
                     "app": {"astrid": {"cut": "c08", "layer": "title"}}},
                    {"id": "opaque:audio:keep", "track": "audio", "clipType": "audio", "at": 0, "hold": 1},
                ], "registry": {"assets": {}},
            },
        }

        if kind == "image":
            self.internal["payload"]["clips"][0]["asset"] = "asset:image"
            self.internal["payload"]["registry"]["assets"]["asset:image"] = {
                "object_id": "object-image", "digest": "sha256:" + hashlib.sha256(b"image bytes").hexdigest(),
                "type": "image", "resolution": "100x50",
            }
        if kind == "text":
            self.internal["payload"]["clips"][0]["text"] = {"content": "Native title"}

    def get_project(self, _project):
        return {"id": "p", "project_id": "p", "slug": "demo"}

    def list_timelines(self, _project, **_kwargs):
        return [[{"timeline_id": "tl", "slug": "cut", "head_revision_id": self.head}], None]

    def get_project_parent_composition_revision(self, project, timeline, revision):
        self.reads.append((project, timeline, revision))
        assert revision == "parent:old"
        return {
            "project_id": project, "timeline_id": timeline, "revision_id": revision,
            "content_digest": "sha256:" + "2" * 64,
            "payload": {"config": {"tracks": [], "clips": [],
                "theme_overrides": {"visual": {"canvas": {"width": 200, "height": 100, "fps": 24}}}},
                "registry": {"assets": {}}, "occurrences": [
                    {"occurrence_id": "opaque:occ:A", "shot_id": "s", "shot_revision_id": "shot:old", "at_ms": 0, "duration_ms": 1000},
                    {"occurrence_id": "opaque:occ:B", "shot_id": "s", "shot_revision_id": "shot:old", "at_ms": 1000, "duration_ms": 1000},
                ]},
        }

    def get_project_shot_revision(self, _project, shot, revision):
        return {"shot_id": shot, "revision_id": revision, "content_digest": "sha256:" + "3" * 64,
                "internal_timeline_revision_id": "internal:old", "payload": {"name": "Old shot"}}

    def get_project_timeline_revision(self, _project, _timeline, revision):
        assert revision == "internal:old"
        return copy.deepcopy(self.internal)


def saved(monkeypatch, kind="text-card"):
    # Registry admission is a separate Runtime boundary; use an empty registry,
    # while exercising the real exact-revision reader and the one projector.
    from astrid.packs.rendering.actions.render import managed_timeline
    monkeypatch.setattr(managed_timeline, "_runtime_snapshot_registry", lambda registry, **kwargs: registry)
    client = Revisions(kind)
    result = prepare_filmstrip({"composed_capture": True, "timeline_ref": "tl", "revision_id": "parent:old"},
                               project="demo", client=client)
    return result["capture_snapshot"], client


def paths(tmp_path, snapshot):
    timeline = tmp_path / "timeline.json"
    assets = tmp_path / "assets.json"
    Timeline.from_config(snapshot["config"]).dump(timeline)
    assets.write_text(json.dumps(snapshot["registry"]))
    return timeline, assets


_DEFAULT_CACHE = object()


def provider(tmp_path, monkeypatch, snapshot, *, callback_change=None, fail=None, theme_path=None,
            cache=_DEFAULT_CACHE):
    timeline, assets = paths(tmp_path, snapshot)
    calls = []
    materialized_root = tmp_path / "materialized"
    materialized_root.mkdir()
    image = materialized_root / "image.png"
    image.write_bytes(b"image bytes")

    def backend(timeline_path, assets_path, output_dir, frames, **kwargs):
        # Run the same serializer/theme/staging helpers as the actual backend.
        theme = rendering_backend._resolved_theme_for_render(timeline_path, kwargs.get("theme_path"))
        from astrid.core.rendering.assets import AssetMaterializer

        class URLs:
            def local_url(self, path):
                return "http://127.0.0.1:4321/" + path.relative_to(materialized_root).as_posix()

        with AssetMaterializer(assets_path, materialized_root=materialized_root,
                               materialized_objects={"object-image": str(image)},
                               reuse_materialized_paths=True) as materializer:
            resolved = materializer.resolved_registry(URLs())
        props = {"timeline": rendering_backend._serialize_timeline(timeline_path, default_theme=theme["id"]),
                 "assets": resolved, "theme": theme, "review": False}
        run._stage_effect_assets_for_timeline(props["timeline"], project_dir=tmp_path, theme_path=theme_path,
                                             render_hash="fixture", composition_clip_types=frozenset({"text-card", "text"}),
                                             asset_registry=props["assets"])
        if callback_change:
            callback_change(props)
        calls.append((tuple(frames), copy.deepcopy(props)))
        if fail != "no_callback":
            kwargs["on_scene_props"](props, (99,) if fail == "frames" else frames)
        if fail == "capture":
            raise RuntimeError("capture failed after observation")
        produced = {}
        for frame in frames:
            path = output_dir / f"frame-{frame:06d}.png"
            Image.new("RGB", (100, 50), "#123456").save(path)
            produced[frame] = path
        if fail == "missing":
            produced.pop(frames[-1])
        if fail == "extra":
            path = output_dir / "frame-99.png"
            Image.new("RGB", (100, 50)).save(path)
            produced[99] = path
        return produced

    monkeypatch.setattr(run, "capture_remotion_frames", backend)
    monkeypatch.setattr(composed_frame, "_SHARED_REMOTION_OWNER", None)
    result = composed_frame.RemotionFrameProvider(
        snapshot, timeline_path=timeline, assets_path=assets, project_dir=tmp_path,
        theme_path=theme_path, materialized_root=materialized_root,
        materialized_objects={"object-image": str(image)},
        cache=(composed_frame.FrameCaptureCache(tmp_path / "cache")
               if cache is _DEFAULT_CACHE else cache),
    )
    return result, calls


def cards(*frames):
    return [{"frame": frame, "image": f"frames/{frame}.png"} for frame in frames]


def test_real_projector_carries_opaque_occurrences_and_all_clip_ids(monkeypatch):
    snapshot, client = saved(monkeypatch)
    mapping = snapshot["metadata"]["scene_clips"]
    assert client.reads == [("p", "tl", "parent:old")]
    assert len(mapping) == 4  # includes both nonvisual clips
    for occurrence in ("opaque:occ:A", "opaque:occ:B"):
        assert mapping[f"{occurrence}:opaque:source:title"] == {
            "address": "c08.title", "source_clip_id": "opaque:source:title", "occurrence_id": occurrence,
        }
        assert mapping[f"{occurrence}:opaque:audio:keep"]["address"] == "opaque:audio:keep"
    assert snapshot["metadata"]["capture_authority"]["expansion"]["children"][0]["revision_id"] == "internal:old"


@pytest.mark.parametrize("kind", ["text-card", "text", "am-snap-plate", "image", "frame-overlay"])
def test_actual_source_join_queries_historical_scene_and_overlay(source_query, tmp_path, monkeypatch, kind):
    snapshot, client = saved(monkeypatch, kind)
    p, calls = provider(tmp_path, monkeypatch, snapshot)
    client.head = "even-later-head"
    info = p.capture(cards(0, 24, 48), tmp_path / "output", (100, 50))
    assert info["fresh_frames"] == 3
    assert len(calls) == 1 and calls[0][0] == (0, 24, 48)
    assert client.reads == [("p", "tl", "parent:old")]
    capture = p.scene_for_frame(0)
    assert capture.frame(0, source_identity=p.source_identity)["layers"][0]["clipId"] == "opaque:occ:A:opaque:source:title"
    assert capture.frame(24, source_identity=p.source_identity)["layers"][0]["clipId"] == "opaque:occ:B:opaque:source:title"
    ended = capture.frame(48, source_identity=p.source_identity)
    assert ended["coverage"] == "GAP" and ended["content"] is None
    assert ended["layers"] == []
    monkeypatch.setattr("astrid.core.timeline.onscreen.visible_at", lambda *a, **k: pytest.fail("private visibility"))
    receipt = attach_frame_addresses({"cards": [{}, {}]}, snapshot, cards(0, 24), frame_provider=p)
    assert [row["addresses"] for row in receipt["cards"]] == [["c08.title"], ["c08.title"]]
    overlay = _contact_overlay_for_layers(snapshot, ["labels"], 24, frame_provider=p)
    image = Image.new("RGB", (100, 50))
    assert overlay(image, 0).tobytes() != image.tobytes()
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        capture.frame(0, source_identity="current-head")
    assert not list(tmp_path.glob("astrid-frame-capture-*"))
    p.close()


def test_mixed_cached_and_fresh_batch_has_facts_only_for_observed_frames(source_query, tmp_path, monkeypatch):
    snapshot, _ = saved(monkeypatch)
    p, calls = provider(tmp_path, monkeypatch, snapshot)
    p.capture(cards(0), tmp_path / "first")
    prior = p.scene_capture
    # This proof covers the unchanged PNG-only fallback, separately from the
    # optional source-bound public rows written by current fresh captures.
    key = composed_frame.frame_cache_key(p.snapshot, 0, None)
    p.cache.write(key, (tmp_path / "first/frames/0.png").read_bytes())
    info = p.capture(cards(0, 24), tmp_path / "mixed")
    assert info["evidence_source"] == "mixed" and info["cached_frames"] == info["fresh_frames"] == 1
    assert [call[0] for call in calls] == [(0,), (24,)]
    assert p.scene_for_frame(0) is None and p.scene_for_frame(24) is not prior
    fallback_frames = []
    from astrid.packs.rendering.actions.timeline_visualize.overlays import labels
    old_addresses = labels.addresses_at
    def fallback(config, frame, fps, canvas):
        fallback_frames.append(frame)
        return old_addresses(config, frame, fps, canvas)
    monkeypatch.setattr(labels, "addresses_at", fallback)
    receipt = attach_frame_addresses({"cards": [{}, {}]}, snapshot, cards(0, 24), frame_provider=p)
    assert all(row["addresses"] == ["c08.title"] for row in receipt["cards"])
    assert fallback_frames == [0]
    p.capture(cards(0, 24), tmp_path / "cached")
    assert p.scene_capture is None and not p.fresh_scene_frames and len(calls) == 2
    p.close()


@pytest.mark.parametrize("failure", ["capture", "missing", "copy", "query", "frames", "no_callback", "extra"])
def test_failed_capture_or_copy_exposes_no_scene_and_removes_partial_files(source_query, tmp_path, monkeypatch, failure):
    snapshot, _ = saved(monkeypatch)
    p, _ = provider(tmp_path, monkeypatch, snapshot, fail=failure)
    if failure == "query":
        def unavailable(*args):
            raise scene_answers.SceneQueryUnavailable("query failed")
        monkeypatch.setattr(scene_answers, "host_frames_at", unavailable)
    if failure == "copy":
        write = Path.write_bytes
        def failed_copy(path, data):
            if path == tmp_path / "output/frames/24.png":
                raise OSError("copy failed")
            return write(path, data)
        monkeypatch.setattr(Path, "write_bytes", failed_copy)
    with pytest.raises((RuntimeError, KeyError, OSError)):
        p.capture(cards(0, 24), tmp_path / "output")
    assert p.scene_capture is None and p._pending_scene is None and not p.fresh_scene_frames
    assert not list((tmp_path / "output/frames").glob("*.png"))
    assert not list(p.cache.root.glob("*.png"))
    assert not list(tmp_path.glob("astrid-frame-capture-*"))


@pytest.mark.parametrize("changed", ["timeline", "theme", "assets", "late_source"])
def test_final_props_must_match_the_same_frozen_source_before_query(tmp_path, monkeypatch, changed):
    snapshot, _ = saved(monkeypatch)
    def change(props):
        if changed == "late_source":
            (tmp_path / "timeline.json").write_text("{}")
        else:
            props[changed]["wrong"] = True
    p, _ = provider(tmp_path, monkeypatch, snapshot, callback_change=change)
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("unbound query"))
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        p.capture(cards(0), tmp_path / "output")
    assert p.scene_capture is None


@pytest.mark.parametrize("changed", ["identity", "timeline", "registry", "map", "occurrence"])
def test_materialized_and_retained_sources_cannot_be_substituted(tmp_path, monkeypatch, changed):
    snapshot, _ = saved(monkeypatch)
    timeline, assets = paths(tmp_path, snapshot)
    if changed == "identity":
        snapshot["metadata"]["capture_identity"] = "wrong"
    elif changed == "timeline":
        config = copy.deepcopy(snapshot["config"])
        config["clips"][0]["params"]["content"] = "new head"
        Timeline.from_config(config).dump(timeline)
    elif changed == "registry":
        assets.write_text('{"assets":{},"different":true}')
    else:
        row = next(iter(snapshot["metadata"]["scene_clips"].values()))
        row["address" if changed == "map" else "occurrence_id"] = "guessed"
    with pytest.raises(ValueError, match="(mismatch|differs)"):
        composed_frame.RemotionFrameProvider(snapshot, timeline_path=timeline, assets_path=assets, project_dir=tmp_path)


def test_canonical_basis_uses_whole_partial_canvas_and_rational_rate(source_query, tmp_path, monkeypatch):
    snapshot, _ = saved(monkeypatch)
    # Create a fresh admitted source using the actual SDK/projector with a
    # partial canvas: Root defaults height, rather than merging theme.height.
    client = Revisions()
    get_parent = client.get_project_parent_composition_revision
    def parent(*args):
        row = get_parent(*args)
        row["payload"]["config"]["theme_overrides"]["visual"]["canvas"] = {"width": 200, "fps": 30000 / 1001}
        return row
    client.get_project_parent_composition_revision = parent
    snapshot = prepare_filmstrip({"composed_capture": True, "timeline_ref": "tl", "revision_id": "parent:old"},
                                project="demo", client=client)["capture_snapshot"]
    theme = rendering_backend.builtin_theme()
    theme["visual"]["canvas"] = {"width": 500, "height": 777, "fps": 12}
    theme_path = tmp_path / "theme.json"
    theme_path.write_text(json.dumps(theme))
    p, _ = provider(tmp_path, monkeypatch, snapshot, theme_path=theme_path)
    assert p.profile.width == 200 and p.profile.height == 1080
    assert p.profile.fps_rational != (30, 1)
    assert snapshot["fps_rational"] == list(p.profile.fps_rational)
    p.capture(cards(29, 30), tmp_path / "output")
    capture = p.scene_capture
    assert capture.frame(29, source_identity=p.source_identity)["height"] == 1080
    assert capture.frame(29, source_identity=p.source_identity)["layers"][0]["clipId"].startswith("opaque:occ:A:")
    assert capture.frame(30, source_identity=p.source_identity)["layers"][0]["clipId"].startswith("opaque:occ:B:")
    p.close()


def test_persistent_renderer_forwards_final_props_callback(tmp_path, monkeypatch):
    observed = []
    def callback(props, frames):
        observed.append((props, frames))
    def backend(*args, **kwargs):
        kwargs["on_scene_props"]({"final": True}, (3,))
        assert kwargs["theme_path"] == tmp_path / "theme.json"
        return {}
    monkeypatch.setattr(run, "capture_remotion_frames", backend)
    renderer = composed_frame._PersistentRemotionRenderer(
        timeline_path=tmp_path / "timeline.json", assets_path=tmp_path / "assets.json", project_dir=tmp_path,
        materialized_root=None, materialized_objects=None, theme_path=tmp_path / "theme.json", on_scene_props=callback,
    )
    renderer((3,), tmp_path / "out", None)
    assert observed == [({"final": True}, (3,))]
    renderer.close()


def test_candidate_retains_real_compiled_candidate_and_uncertified_base_hint(source_query, tmp_path, monkeypatch):
    from astrid.core.timeline.authoring_bundle import (
        _digest,
        open_authoring_bundle,
        preview_authoring_candidate,
    )
    from astrid.packs.rendering.actions.timeline_visualize.provenance import (
        snapshot_label,
    )

    _snapshot, client = saved(monkeypatch)
    parent = client.get_project_parent_composition_revision("p", "tl", "parent:old")
    shot = client.get_project_shot_revision("p", "s", "shot:old")
    shot["project_id"] = "p"
    internal = copy.deepcopy(client.internal)
    internal["payload"]["clips"] = internal["payload"]["clips"][:1]
    for row in (parent, shot, internal):
        row["content_digest"] = _digest(row["payload"])
    bundle = open_authoring_bundle(parent, shot_revisions=[shot], internal_timeline_revisions=[internal])
    for row in bundle["shots"].values():
        row["internal_timeline"]["clips"][0]["params"]["content"] = "Unpublished title"
    preview = preview_authoring_candidate(bundle)
    result = prepare_filmstrip({"composed_capture": True, "timeline_ref": "tl", "authoring_preview": preview},
                               project="demo", client=client)
    snapshot = result["capture_snapshot"]
    marker = snapshot["metadata"]["working_copy"]
    assert marker["candidate_digest"] == preview["candidate_digest"]
    assert marker["publication_digest"] == preview["publication_digest"]
    label = snapshot_label(snapshot)
    assert label["status"] == "uncertified"
    assert label["actual_revision_id"] is None
    assert label["base_revision_id"] == "parent:old"
    assert marker["candidate_parent_revision_id"] in label["label"]
    assert label["label"].endswith("(base parent:old)")
    p, _ = provider(tmp_path, monkeypatch, snapshot)
    p.capture(cards(0), tmp_path / "output")
    assert p.scene_for_frame(0) is not None
    assert p.snapshot["metadata"]["working_copy"] == marker
    p.close()


def test_metadata_only_historical_snapshots_remain_uncertified_with_revision_hint(monkeypatch):
    from astrid.packs.rendering.actions.timeline_visualize.provenance import (
        snapshot_label,
    )

    native_snapshot, client = saved(monkeypatch)
    managed_snapshot = {"metadata": {"canonical_timeline": {"parent_revision_id": "parent:old"},
                                     "selection": "explicit_historical_render"}}
    client.head = "newest-head"
    assert snapshot_label(native_snapshot) == snapshot_label(managed_snapshot) == {
        "status": "uncertified", "label": "UNCERTIFIED preview (claimed revision parent:old)",
        "actual_revision_id": None, "base_revision_id": None,
    }


def test_wrong_canonical_basis_cannot_query(tmp_path, monkeypatch):
    from dataclasses import replace

    snapshot, _ = saved(monkeypatch)
    p, _ = provider(tmp_path, monkeypatch, snapshot)
    p.profile = replace(p.profile, width=201)
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("wrong basis queried"))
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="basis mismatch"):
        p.capture(cards(0), tmp_path / "output")


@pytest.mark.parametrize("changed", ["digest", "locator", "metadata"])
def test_wrong_materialized_media_cannot_query(tmp_path, monkeypatch, changed):
    snapshot, _ = saved(monkeypatch, "image")
    def change(props):
        entry = next(iter(props["assets"]["assets"].values()))
        if changed == "digest":
            (tmp_path / "materialized/image.png").write_bytes(b"wrong source")
        elif changed == "locator":
            entry["file"] = "http://elsewhere/image.png"
        else:
            entry["type"] = "video"
    p, _ = provider(tmp_path, monkeypatch, snapshot, callback_change=change)
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("wrong media queried"))
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        p.capture(cards(0), tmp_path / "output")


def test_execute_handoff_uses_actual_provider_and_public_labels_in_exported_pack(source_query, tmp_path, monkeypatch):
    import zipfile
    from argparse import Namespace

    from astrid.packs.rendering.actions.timeline_visualize import filmstrip_execution
    from astrid.packs.rendering.actions.timeline_visualize.overlays import labels

    snapshot, _ = saved(monkeypatch)
    prepared, calls = provider(tmp_path, monkeypatch, snapshot)
    prepared.close()
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("ASTRID_TIMELINE_FRAME_CACHE", str(tmp_path / "executor-cache"))
    monkeypatch.setattr(labels, "labels_for", lambda *a, **k: pytest.fail("fresh frame used legacy labels"))
    args = Namespace(
        filmstrip_authority=json.dumps({"mode": "composed_capture", "capture_snapshot": snapshot,
                                       "capture_identity": snapshot["metadata"]["capture_identity"]}),
        project_slug="demo", range_value=None, out=tmp_path / "export", include_media=False,
        timeline=tmp_path / "timeline.json", assets_registry=tmp_path / "assets.json",
        materialized_root=tmp_path / "materialized", materialized_objects={},
        frame=0, show=["output"], view="contact", layers=["labels"],
    )
    result = filmstrip_execution.execute_filmstrip(args)
    assert result["returncode"] == 0
    assert len(calls) == 1 and calls[0][0] == (0,)
    with zipfile.ZipFile(result["outputs"]["filmstrip_bundle"]) as archive:
        index = json.loads(archive.read("frame-index.json"))
        assert index["cards"][0]["addresses"] == ["c08.title"]
        assert index["provenance"]["revision"] == {
            "status": "uncertified",
            "label": "UNCERTIFIED preview (claimed revision parent:old)",
            "actual_revision_id": None,
            "base_revision_id": None,
        }
        retained = json.loads(archive.read("render-snapshot.json"))
        assert retained["metadata"]["scene_clips"] == snapshot["metadata"]["scene_clips"]


def test_staged_element_asset_substitution_fails_before_query(tmp_path, monkeypatch):
    snapshot, _ = saved(monkeypatch, "frame-overlay")
    def change(props):
        staged = props["timeline"]["clips"][0]["params"]["__astridAssets"]["frame"]
        (tmp_path / "public" / staged).write_bytes(b"wrong staged bytes")
    p, _ = provider(tmp_path, monkeypatch, snapshot, callback_change=change)
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("wrong staged bytes queried"))
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="staged element bytes"):
        p.capture(cards(0), tmp_path / "output")


def test_source_join_preserves_empty_and_unknown_transport_answers(tmp_path, monkeypatch):
    snapshot, _ = saved(monkeypatch)
    p, _ = provider(tmp_path, monkeypatch, snapshot)
    # This transport boundary test does not claim real-host EMPTY coverage.
    answers = [
        {"frame": 0, "width": 200, "height": 100, "layers": [], "coverage": "COVERED", "content": "EMPTY", "findings": []},
        {"frame": 24, "width": 200, "height": 100, "layers": [], "coverage": "UNKNOWN", "content": None,
         "findings": [{"code": "unproven"}]},
    ]
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: copy.deepcopy(answers))
    p.capture(cards(0, 24), tmp_path / "output")
    assert p.scene_capture.frame(0, source_identity=p.source_identity) == answers[0]
    assert p.scene_capture.frame(24, source_identity=p.source_identity) == answers[1]
    p.close()


@pytest.mark.parametrize("changed", ["missing", "extra", "outside"])
def test_staged_element_asset_map_is_bound_before_query(tmp_path, monkeypatch, changed):
    snapshot, _ = saved(monkeypatch, "frame-overlay")
    def change(props):
        params = props["timeline"]["clips"][0]["params"]
        if changed == "missing":
            params.pop("__astridAssets")
        elif changed == "extra":
            params["__astridAssets"]["unrelated"] = "unrelated.png"
        else:
            params["__astridAssets"]["frame"] = "../../outside.png"
    p, _ = provider(tmp_path, monkeypatch, snapshot, callback_change=change)
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("unbound stage queried"))
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="staged element"):
        p.capture(cards(0), tmp_path / "output")


@pytest.mark.parametrize("kind", ["text-card", "image", "frame-overlay"])
def test_sdk_source_reaches_real_final_staging_callback(source_query, tmp_path, monkeypatch, kind):
    from contextlib import nullcontext
    from types import SimpleNamespace

    snapshot, _ = saved(monkeypatch, kind)
    timeline, assets = paths(tmp_path, snapshot)
    root = tmp_path / "materialized"
    root.mkdir()
    image = root / "image.png"
    image.write_bytes(b"image bytes")
    seen = []

    class Server:
        def __init__(self, serving_root, **kwargs):
            assert serving_root == root

        def __enter__(self):
            return self

        def local_url(self, path):
            return "http://127.0.0.1:4321/" + path.relative_to(root).as_posix()

        def __exit__(self, *args):
            pass

    class Session:
        def render(self, **kwargs):
            props = json.loads(kwargs["props_path"].read_text())
            assert p.scene_capture is None  # queried, but no successful pixels yet
            assert p._pending_scene is not None
            if kind == "frame-overlay":
                staged = props["timeline"]["clips"][0]["params"]["__astridAssets"]["frame"]
                assert (tmp_path / "public" / staged).is_file()
            seen.append((kwargs["frames"], props))
            for frame in kwargs["frames"]:
                Image.new("RGB", (100, 50)).save(kwargs["output_dir"] / f"frame-{frame}.png")

        def retain_staged_public_root(self, path):
            pass

        def close(self, **kwargs):
            pass

    monkeypatch.setattr(run, "InvocationAssetServer", Server)
    monkeypatch.setattr(run, "PersistentRemotionFrameSession", Session)
    monkeypatch.setattr(run, "_available_remotion_port", lambda: 32101)
    monkeypatch.setattr(run.remotion_lock, "remotion_render_lock", nullcontext)
    monkeypatch.setattr(run, "_validate_project_dir", lambda _: SimpleNamespace(
        node_executable=Path("/unused/node"), remotion_cli=Path("/unused/cli"), node_version="v20.19.4"))
    monkeypatch.setattr(run, "_regenerate_element_registries", lambda *a: None)
    monkeypatch.setattr(run, "_validate_renderer_effect_registry", lambda *a: None)
    monkeypatch.setattr(run, "_effective_registry_state", lambda *a: {})
    monkeypatch.setattr(composed_frame, "_SHARED_REMOTION_OWNER", None)
    p = composed_frame.RemotionFrameProvider(
        snapshot, timeline_path=timeline, assets_path=assets, project_dir=tmp_path,
        materialized_root=root, materialized_objects={"object-image": str(image)},
        cache=composed_frame.FrameCaptureCache(tmp_path / "cache"),
    )
    info = p.capture(cards(24, 0), tmp_path / "output", (100, 50))
    assert info["fresh_frames"] == 2 and seen[0][0] == (0, 24)
    assert p.scene_for_frame(0) is p.scene_for_frame(24) is not None
    assert p.scene_capture.frame(0, source_identity=p.source_identity)["layers"][0]["clipId"] == "opaque:occ:A:opaque:source:title"
    assert not list(tmp_path.glob("astrid-frame-capture-*"))
    assert not list((tmp_path / "public/astrid-effects").glob("*/**/*"))
    p.close()
