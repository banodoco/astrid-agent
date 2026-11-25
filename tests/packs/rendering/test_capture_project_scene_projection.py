"""Project snapshot projection through the retained frame scene callback.

These cases exercise the real pinned-snapshot staging and asset staging helpers,
then invoke the same RemotionFrameProvider callback used by frame capture. The
scene-query transport is replaced at its boundary; this does not claim native
Remotion pixels.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
import pytest

from astrid.core.element.registry import ElementRegistry
from astrid.core.rendering import scene_answers
from astrid.core.rendering.project_elements import CARRIER, stage_project_elements
from astrid.packs.rendering.actions.timeline_visualize import composed_frame
from astrid.packs.rendering.rendering.renderers.remotion import run
from astrid.packs.rendering.rendering.renderers.remotion import root_preparation
from tests.core import test_project_packs
from tests.packs.rendering import test_pinned_project_elements as pinned


@pytest.fixture
def admitted_project(monkeypatch, tmp_path):
    # Reuse Plan 69's real Runtime admission fixture setup without invoking a
    # pytest fixture from another test module through pytest's fixture wrapper.
    configured = test_project_packs.configured.__wrapped__(tmp_path, monkeypatch)
    return pinned.admitted.__wrapped__(configured)


def _capture_case(tmp_path, runtime):
    snapshot = pinned._snapshot(runtime)
    config = copy.deepcopy(dict(snapshot.config))
    assert config.get("app", {}).get(CARRIER)
    assert len({clip["elementRef"]["revision"] for clip in config["clips"]}) == 2

    timeline_path = tmp_path / "timeline.json"
    assets_path = tmp_path / "assets.json"
    timeline_path.write_text(json.dumps(config), encoding="utf-8")
    assets_path.write_text(json.dumps(snapshot.registry), encoding="utf-8")
    project_dir = tmp_path / "remotion"
    project_dir.mkdir()
    public_root = project_dir / "public"
    public_root.mkdir()
    materialized_root = tmp_path / "materialized"
    materialized_root.mkdir()
    registry_root = tmp_path / "host-registry"
    registry_root.mkdir()

    selected_registry, project_roots = stage_project_elements(
        config, tmp_path / "host-selected", registry=ElementRegistry(),
    )
    theme = run._resolved_theme_for_render(timeline_path, None)
    timeline = run._serialize_timeline(timeline_path, default_theme=theme["id"])
    frozen_timeline = copy.deepcopy(timeline)
    registry = run._load_registry_mapping(assets_path)
    props = {"timeline": timeline, "assets": registry, "theme": theme, "review": False}
    summary = run._stage_effect_assets_for_timeline(
        props["timeline"], project_dir=project_dir, theme_path=None,
        render_hash="project-scene-fixture", asset_registry=registry,
        registry=selected_registry,
    )
    # This is root_preparation's final Root prop normalization.
    props["timeline"].get("app", {}).pop(CARRIER, None)

    from astrid.core.rendering.profile import resolve_render_profile

    profile = resolve_render_profile(props["timeline"], props["assets"], theme=props["theme"])
    provider = object.__new__(composed_frame.RemotionFrameProvider)
    provider._pending_scene = None
    provider._fresh_requested = (0,)
    provider._source_bytes = {
        timeline_path: timeline_path.read_bytes(),
        assets_path: assets_path.read_bytes(),
    }
    provider._theme = theme
    provider._timeline = frozen_timeline
    provider._registry = copy.deepcopy(registry)
    provider.materialized_root = materialized_root
    provider.project_dir = project_dir
    provider.theme_path = None
    provider.profile = profile
    provider.source_identity = "project-scene-source"
    provider._scene_clips = {
        clip["id"]: {"address": f"scene:{clip['id']}"} for clip in config["clips"]
    }
    return provider, props, selected_registry, project_roots, registry_root, summary


def _answer(props, basis, frames, **kwargs):
    return [{"frame": frame, "width": basis["width"], "height": basis["height"], "layers": []}
            for frame in frames]


def test_project_snapshot_projection_reaches_scene_callback_with_exact_selected_assets(
    admitted_project, tmp_path, monkeypatch,
):
    runtime, refs, _reads = admitted_project
    provider, props, selected_registry, roots, registry_root, summary = _capture_case(tmp_path, runtime)
    staged = [clip["params"]["__astridAssets"]["badge"] for clip in props["timeline"]["clips"]]
    assert staged[0] == staged[2] and staged[0] != staged[1]
    assert [(provider.project_dir / "public" / path).read_text() for path in staged] == [
        "first", "second", "first",
    ]
    assert summary["root"] == str(provider.project_dir / "public" / "astrid-effects" / "project-scene-fixture")

    # Project-only effect refs are not present in the default catalog. The
    # observer must validate against the same selected snapshot registry that
    # supplied the staged module and bytes.
    monkeypatch.setattr(run, "load_default_registry", lambda **_: ElementRegistry())
    observed = []
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *args, **kwargs:
                        observed.append((args, kwargs)) or _answer(*args, **kwargs))
    token = root_preparation._scene_selectors.set((roots, str(registry_root)))
    registry_token = root_preparation._scene_registry.set(selected_registry)
    try:
        provider._observe_scene(props, (0,))
    finally:
        root_preparation._scene_registry.reset(registry_token)
        root_preparation._scene_selectors.reset(token)

    assert len(observed) == 1
    assert observed[0][1] == {"_render_roots": roots, "_registry_root": str(registry_root)}
    assert provider._pending_scene is not None
    assert refs[0]["revision"] != refs[1]["revision"]


@pytest.mark.parametrize("tamper", ["timeline", "asset"])
def test_project_scene_callback_refuses_tampered_project_projection_before_query(
    admitted_project, tmp_path, monkeypatch, tamper,
):
    runtime, _refs, _reads = admitted_project
    provider, props, selected_registry, roots, registry_root, _summary = _capture_case(tmp_path, runtime)
    monkeypatch.setattr(run, "load_default_registry", lambda **_: ElementRegistry())
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a, **kw:
                        pytest.fail("tampered project capture reached scene query"))

    if tamper == "timeline":
        props["timeline"]["clips"][0]["params"]["forged"] = True
        message = "capture serialized timeline mismatch"
    else:
        staged = props["timeline"]["clips"][0]["params"]["__astridAssets"]["badge"]
        (provider.project_dir / "public" / staged).write_text("tampered bytes", encoding="utf-8")
        message = "staged element bytes"

    token = root_preparation._scene_selectors.set((roots, str(registry_root)))
    registry_token = root_preparation._scene_registry.set(selected_registry)
    try:
        with pytest.raises(scene_answers.SceneQueryUnavailable, match=message):
            provider._observe_scene(props, (0,))
    finally:
        root_preparation._scene_registry.reset(registry_token)
        root_preparation._scene_selectors.reset(token)


def test_pinned_animation_render_key_is_derived_from_frozen_clip(monkeypatch):
    from astrid.core.rendering import profile as profiles

    element = SimpleNamespace(kind="animations", id="fade", metadata={
        "render_revision": "sha256:admitted", "render_key": "pinned_admitted",
    })
    clip = {"id": "clip", "entrance": "fade", "elementRef": {
        "kind": "animation", "id": "fade", "packId": "project", "revision": "sha256:admitted",
    }}
    provider = object.__new__(composed_frame.RemotionFrameProvider)
    provider._pending_scene = None
    provider._fresh_requested = (0,)
    provider._source_bytes = {}
    provider._theme = {}
    provider._timeline = {"app": {CARRIER: [{"reference": clip["elementRef"]}]}, "clips": [clip]}
    provider._registry = {"assets": {}}
    provider.materialized_root = None
    provider.profile = SimpleNamespace(width=200, height=100, fps_rational=(24, 1))
    provider.source_identity = "animation-source"
    provider._scene_clips = {"clip": {"address": "clip"}}
    monkeypatch.setattr(provider, "_validate_staged_assets", lambda *a, **kw: element)
    monkeypatch.setattr(profiles, "resolve_render_profile", lambda *a, **kw: provider.profile)
    observed = []
    monkeypatch.setattr(composed_frame.CaptureSceneLabels, "observe",
                        lambda self, props, frames: observed.append((props, frames)))
    actual = copy.deepcopy(clip)
    run._bind_pinned_element_slots(actual, element, {})
    props = {"timeline": {"app": {}, "clips": [actual]}, "assets": {"assets": {}}, "theme": {}}
    token = root_preparation._scene_registry.set(object())
    try:
        provider._observe_scene(props, (0,))
        assert observed and observed[0][0]["timeline"]["clips"][0]["entrance"] == "pinned_admitted"
    finally:
        root_preparation._scene_registry.reset(token)
