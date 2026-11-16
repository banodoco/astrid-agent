"""An html-less live-scene clip reaches every renderer as html in the Remotion props.

The timeline persists a reference; the Runtime freezes it html-less; the host
fetches the verified entry object; the renderer fills it into its own props.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.packs.rendering.shared.live_scenes.package import (
    LiveSceneFilesUnavailable,
    LiveSceneHtmlError,
    fill_live_scene_html,
    live_scene_entry_refs,
    validate_package,
)
from astrid.sdk.invocation import _authorize_live_scene_objects, _prepare_managed_render_inputs
from tests.packs.rendering.test_managed_timeline_render import _Runtime
from tests.packs.rendering.test_remotion_capture_scene_props import capture, capture_host  # noqa: F401

HTML = "<html><body>entry bytes</body></html>"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def reference_package(html: str = HTML) -> dict:
    """A reference-only package with real content ids, as the editor persists it."""
    html_bytes = html.encode("utf-8")
    body = {
        "manifest": {"formatVersion": 1, "entry": "scene.html", "duration": 100, "authoredFps": 30},
        "entry": {
            "object_id": "sha256:" + _digest(html_bytes), "digest": "sha256:" + _digest(html_bytes),
            "media_type": "text/html", "size": len(html_bytes), "filename": "scene.html",
        },
        "assets": [],
    }
    package_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    revision = "sha256:" + _digest(package_body.encode("utf-8"))
    return {"revision": revision, "source": {"objectId": revision, "revision": revision}, "packageBody": package_body}


def scene_clip(package: dict | None = None) -> dict:
    return {"id": "scene", "track": "v1", "clipType": "haus.astrid.liveScene", "at": 0, "from": 0, "to": 1,
            "app": {"liveScene": package or reference_package()}}


def entry_id(package: dict) -> str:
    return json.loads(package["packageBody"])["entry"]["object_id"]


# --- the host hands the renderer the verified entry object -------------------

def _admit(tmp_path: Path, *, mutate=None, authorize_entry: bool = True, recorded: bool = True,
           agent_snapshot_filled: bool = False, entry_bytes: bytes | None = None):
    package = reference_package()
    runtime = _Runtime()
    runtime.timeline["config"] = {"tracks": [{"id": "v1", "kind": "visual", "label": "V1"}], "clips": [scene_clip(package)]}
    prepared, authority = _prepare_managed_render_inputs({"timeline_ref": "main"}, project="demo", _client=runtime)
    snapshot = prepared["timeline_snapshot"]
    assert "html" not in snapshot["config"]["clips"][0]["app"]["liveScene"]

    def encode(document):
        return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")

    snapshot_bytes = encode(snapshot)
    snapshot_digest = _digest(snapshot_bytes)

    def reference(digest: str) -> dict:
        return {
            "project_id": authority["project_id"], "timeline_id": authority["timeline_id"],
            "revision_id": authority["parent_revision_id"], "revision_digest": authority["parent_content_digest"],
            "source_kind": "revision", "digest": "sha256:" + digest,
        }

    objects = {snapshot_digest: snapshot_bytes}
    agent_ref = reference(snapshot_digest)
    if agent_snapshot_filled:
        filled = json.loads(json.dumps(snapshot))
        filled["config"]["clips"][0]["app"]["liveScene"]["html"] = HTML
        filled_bytes = encode(filled)
        objects[_digest(filled_bytes)] = filled_bytes
        agent_ref = reference(_digest(filled_bytes))
    package_id, scene_entry = package["source"]["objectId"], entry_id(package)
    objects[package_id.removeprefix("sha256:")] = package["packageBody"].encode("utf-8")
    objects[scene_entry.removeprefix("sha256:")] = entry_bytes if entry_bytes is not None else HTML.encode("utf-8")
    authority = {**authority, **({"scene_objects": [{
        "clip_id": "scene", "package_object_id": package_id, "package_size": len(package["packageBody"]),
        "entry_object_id": scene_entry, "entry_size": len(HTML.encode("utf-8")),
    }]} if recorded else {})}
    if mutate:
        mutate(authority)
    ids = [snapshot_digest, package_id, *([scene_entry] if authorize_entry else [])]
    host = GenericPackHost(pack_roots=[tmp_path], client=SimpleNamespace(get_object=lambda key: objects[key]))
    return host._materialize_inputs(
        {"input_object_ids": ids, "spec": {
            "timeline_snapshot_ref": reference(snapshot_digest),
            "inputs": {"timeline_snapshot_ref": agent_ref, "timeline_authority": authority},
        }},
        tmp_path / "attempt",
    ), package


@pytest.mark.parametrize("recorded", [True, False])
def test_host_materializes_the_entry_object_under_both_ids_and_keeps_the_snapshot_htmlless(tmp_path, recorded):
    values, package = _admit(tmp_path, recorded=recorded)
    objects = values["materialized_objects"]
    scene_entry = entry_id(package)
    assert Path(objects[scene_entry]).read_bytes() == HTML.encode("utf-8")
    assert objects[scene_entry.removeprefix("sha256:")] == objects[scene_entry]
    assert Path(values["materialized_root"]) in Path(objects[scene_entry]).parents

    timeline = json.loads(Path(values["timeline"]).read_text(encoding="utf-8"))
    assert "html" not in timeline["clips"][0]["app"]["liveScene"]
    # What the Remotion root preparation does with the handoff.
    props = fill_live_scene_html(timeline, objects)
    assert props["clips"][0]["app"]["liveScene"]["html"] == HTML
    assert "html" not in timeline["clips"][0]["app"]["liveScene"]


def test_agent_snapshot_digest_equals_the_admitted_one_and_a_filled_snapshot_is_refused(tmp_path):
    # Admitted ref == agent ref (both html-less): the host accepts and mints the verified source.
    values, _package = _admit(tmp_path / "ok")
    assert values.timeline_source is not None
    # The old behaviour (agent fills html) changes the digest: refused.
    with pytest.raises(HostError, match="differs from admitted context"):
        _admit(tmp_path / "filled", agent_snapshot_filled=True)


def test_host_refuses_an_entry_the_runtime_did_not_authorize(tmp_path):
    with pytest.raises(HostError, match="not authorized by task input_object_ids"):
        _admit(tmp_path, authorize_entry=False)


def test_host_refuses_entry_bytes_that_do_not_match_their_digest(tmp_path):
    with pytest.raises(HostError, match="hash mismatch"):
        _admit(tmp_path, entry_bytes=b"<html>tampered</html>")


def test_host_refuses_an_entry_size_that_disagrees_with_the_runtime_record(tmp_path):
    def wrong_size(authority):
        authority["scene_objects"][0]["entry_size"] += 1

    with pytest.raises(HostError, match="conflicting declared sizes"):
        _admit(tmp_path, mutate=wrong_size)


# --- the renderer fills html into its props -----------------------------------

def _live_timeline(host, package):
    host.raw["clips"] = [{**scene_clip(package), "params": {}, "track": "v", "hold": 2, "from": 0, "to": 2}]
    host.raw["clips"][0].pop("to")
    host.raw["clips"][0].pop("from")
    host.timeline_path.write_text(json.dumps(host.raw))


def test_remotion_props_carry_the_html_for_an_htmlless_clip(capture_host, tmp_path):  # noqa: F811
    package = reference_package()
    _live_timeline(capture_host, package)
    capture_host.kwargs["materialized_objects"] = {
        **capture_host.kwargs["materialized_objects"], entry_id(package): HTML.encode("utf-8"),
    }
    seen = []
    capture(capture_host, tmp_path, on_scene_props=lambda props, frames: seen.append(props))
    scene = seen[0]["timeline"]["clips"][0]["app"]["liveScene"]
    assert scene["html"] == HTML
    assert json.loads(capture_host.serialized)["timeline"]["clips"][0]["app"]["liveScene"]["html"] == HTML
    assert "html" not in json.loads(capture_host.timeline_path.read_text())["clips"][0]["app"]["liveScene"]


def test_local_file_render_without_an_object_store_fails_with_a_typed_message(capture_host, tmp_path):  # noqa: F811
    _live_timeline(capture_host, reference_package())
    # A local timeline file has no managed media and no object store at all.
    capture_host.assets_path.write_text(json.dumps({"assets": {}}))
    capture_host.kwargs.pop("materialized_objects")
    with pytest.raises(LiveSceneFilesUnavailable, match="not available for a local file render") as error:
        capture(capture_host, tmp_path, on_scene_props=lambda props, frames: None)
    assert isinstance(error.value, LiveSceneHtmlError) and error.value.clip_id == "scene"
    assert "missing from the object store" not in str(error.value)


def test_a_handoff_without_the_entry_is_a_missing_object_not_a_local_file_error(tmp_path):
    package = reference_package()
    timeline = {"tracks": [], "clips": [scene_clip(package)]}
    with pytest.raises(LiveSceneHtmlError, match="is missing from the object store") as error:
        fill_live_scene_html(timeline, {"unrelated": b"x"})
    assert not isinstance(error.value, LiveSceneFilesUnavailable)
    with pytest.raises(LiveSceneFilesUnavailable):
        fill_live_scene_html(timeline, None)
    with pytest.raises(LiveSceneFilesUnavailable):
        fill_live_scene_html(timeline, {})
    inline = {"tracks": [], "clips": [scene_clip({**package, "html": HTML})]}
    assert fill_live_scene_html(inline, None) is not None


# --- the capture provider compares the props with the frozen timeline after filling ---

class _LiveRevisions:
    """The carry fixture's saved parent, with its title clip replaced by a reference-only scene."""

    @staticmethod
    def build(package):
        from tests.packs.rendering.test_scene_source_carry import Revisions

        client = Revisions()
        clip = client.internal["payload"]["clips"][0]
        clip.update(clipType="haus.astrid.liveScene", params={})
        clip["app"] = {**clip["app"], "liveScene": package}
        return client


def _capture_live(tmp_path, monkeypatch, materialized):
    from astrid.core.rendering import scene_answers
    from astrid.packs.rendering.actions.render import managed_timeline
    from astrid.packs.rendering.actions.timeline_visualize import composed_frame
    from astrid.packs.rendering.rendering.renderers.remotion import run
    from astrid.sdk.timeline_filmstrip import prepare_filmstrip
    from tests.packs.rendering import test_scene_source_carry as carry

    package = reference_package()
    client = _LiveRevisions.build(package)
    monkeypatch.setattr(managed_timeline, "_runtime_snapshot_registry", lambda registry, **kwargs: registry)
    snapshot = prepare_filmstrip(
        {"composed_capture": True, "timeline_ref": "tl", "revision_id": "parent:old"}, project="demo", client=client,
    )["capture_snapshot"]
    assert "html" not in snapshot["config"]["clips"][0]["app"]["liveScene"]
    monkeypatch.setattr(run, "_stage_effect_assets_for_timeline", lambda *a, **k: {"root": str(tmp_path), "effects": []})
    # Unrelated to the HTML handoff: the cache-key effect census does not know live-scene clips.
    monkeypatch.setattr(composed_frame, "renderer_environment_identity", lambda config: "fixture-environment")
    entry_file = tmp_path / "entry.html"
    entry_file.write_text(HTML, encoding="utf-8")
    objects = {entry_id(package): str(entry_file)} if materialized else {}

    def renderer_fills_html(props):
        # root_preparation fills the props from the host handoff, exactly like this.
        props["timeline"] = dict(fill_live_scene_html(props["timeline"], {entry_id(package): str(entry_file)}))

    def answers(props, basis, frames):
        # A static transport answer: this test is about the props join, not scene geometry.
        return [{"frame": frame, "width": basis["width"], "height": basis["height"], "coverage": "UNKNOWN",
                 "content": None, "findings": [], "layers": [{
                     "clipId": "opaque:occ:A:opaque:source:title", "role": "content", "visible": True, "opacity": 1,
                     "fullFrame": True, "bounds": {"status": "known", "rect": {
                         "x": 0, "y": 0, "width": basis["width"], "height": basis["height"]}}}]} for frame in frames]

    monkeypatch.setattr(scene_answers, "host_frames_at", answers)
    frames, calls = carry.provider(tmp_path, monkeypatch, snapshot, callback_change=renderer_fills_html)
    frames.materialized_objects = objects
    return frames, calls, carry


def test_capture_comparison_accepts_a_props_timeline_the_renderer_filled(tmp_path, monkeypatch):
    frames, calls, carry = _capture_live(tmp_path, monkeypatch, materialized=True)
    frames.capture(carry.cards(0), tmp_path / "output", (100, 50))
    assert calls[0][1]["timeline"]["clips"][0]["app"]["liveScene"]["html"] == HTML
    assert frames.scene_for_frame(0) is not None
    frames.close()


def test_capture_comparison_fails_loudly_when_the_entry_cannot_be_resolved(tmp_path, monkeypatch):
    from astrid.core.rendering.scene_answers import SceneQueryUnavailable

    frames, _calls, carry = _capture_live(tmp_path, monkeypatch, materialized=False)
    with pytest.raises(SceneQueryUnavailable, match="live scene is not renderable"):
        frames.capture(carry.cards(0), tmp_path / "output", (100, 50))
    frames.close()


# --- the invocation authorizes the objects the host will fetch ----------------

def test_invocation_authorizes_package_and_entry_for_render_and_only_the_entry_for_visualize():
    package = reference_package()
    config = {"clips": [scene_clip(package), scene_clip(package)]}
    assert live_scene_entry_refs(config)[0]["package_object_id"] == package["source"]["objectId"]

    render: list[str] = ["sha256:" + "a" * 64]
    _authorize_live_scene_objects(render, config, packages=True)
    assert render == ["sha256:" + "a" * 64, package["source"]["objectId"], entry_id(package)]

    visualize: list[str] = []
    _authorize_live_scene_objects(visualize, config, packages=False)
    assert visualize == [entry_id(package)]

    inline = {"clips": [scene_clip({**package, "html": HTML})]}
    untouched: list[str] = []
    _authorize_live_scene_objects(untouched, inline, packages=True)
    assert untouched == []
    validate_package({**package, "html": HTML})
