"""Prepared AM plates through the installed public14 host; geometry/SSR only.

Historical Runtime reads and runtime admission are doubled. The real projector,
serializer, theme/profile, final Root staging and generated owner maps are used.
No server, browser, capture or media-alpha evidence is supplied by these tests.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrid.core.rendering.node_pin import resolve_pinned_node
from astrid.core.rendering import scene_answers
from astrid.core.timeline import Timeline
from astrid.packs.rendering.actions.render import managed_timeline
from astrid.packs.rendering.rendering.renderers.remotion import run
from astrid.sdk.timeline_filmstrip import prepare_filmstrip
from tests.packs.rendering import test_capture_scene_labels as host_fixture
from tests.packs.rendering.test_scene_source_carry import Revisions

host = host_fixture.host
native = host_fixture.native
ROOT = Path(__file__).resolve().parents[3]
SRC = "data:image/png;base64,iVBORw0KGgo="


@pytest.fixture
def prepared_root(native, host, tmp_path, monkeypatch):
    package = host / "node_modules/@banodoco/timeline-composition"
    artifact = ROOT / "remotion/artifacts/banodoco-timeline-composition-635f4de06fda22a85fc2e4cfc764d0153a4c9f40.tgz"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == (
        "819acf6b3815d6eeabe772a9569581fedbe4f9c9cce35946efca9454e2040234")

    def hashes():
        return {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in package.rglob("*") if p.is_file()}

    before = hashes()
    assert len(before) == 80
    for scope in ("@remotion", "@banodoco"):
        assert not (host / "node_modules" / scope).is_symlink()
    for name in ("react", "react-dom", "remotion", "@remotion/media", "@remotion/layout-utils"):
        version = json.loads((host / "node_modules" / name / "package.json").read_text())["version"]
        assert version == ("18.3.1" if name in ("react", "react-dom") else "4.0.509")
    # The host fixture already generated the actual maps in its private project.
    monkeypatch.setattr(run, "_regenerate_element_registries", lambda *a: None)
    monkeypatch.setattr(run, "_validate_project_dir", lambda _: SimpleNamespace(
        node_executable=Path("/unused/node"), remotion_cli=Path("/unused/cli")))
    monkeypatch.setattr(run, "_available_remotion_port", lambda: 32101)
    monkeypatch.setattr(managed_timeline, "_runtime_snapshot_registry", lambda registry, **kwargs: registry)

    class ObservedRoot(Exception):
        pass

    def prepare(clips, *, managed=False):
        client = Revisions("am-snap-plate")
        client.internal["payload"]["clips"] = copy.deepcopy(clips)
        materialized = tmp_path / "materialized"
        objects = {}
        if managed:
            materialized.mkdir(exist_ok=True)
            image = materialized / "plate.png"
            Image.new("RGBA", (320, 180), (0, 0, 0, 0)).save(image)
            objects["object:plate"] = str(image)
            client.internal["payload"]["registry"]["assets"]["P"] = {
                "object_id": "object:plate", "type": "image", "resolution": "320x180",
                "digest": "sha256:" + hashlib.sha256(image.read_bytes()).hexdigest(),
            }

            class URLs:
                def __init__(self, root, **kwargs):
                    assert root == materialized

                def __enter__(self):
                    return self

                def local_url(self, path):
                    assert path == image and path.is_file()
                    return "http://127.0.0.1:4321/plate.png"

                def __exit__(self, *args):
                    pass

            monkeypatch.setattr(run, "InvocationAssetServer", URLs)
        snapshot = prepare_filmstrip(
            {"composed_capture": True, "timeline_ref": "tl", "revision_id": "parent:old"},
            project="demo", client=client)["capture_snapshot"]
        assert client.reads == [("p", "tl", "parent:old")]
        timeline = tmp_path / "timeline.json"
        assets = tmp_path / "assets.json"
        Timeline.from_config(snapshot["config"]).dump(timeline)
        assets.write_text(json.dumps(snapshot["registry"]))
        observed = []

        def observe(props, frames):
            assert frames == (0,)
            assert json.loads((tmp_path / ".remotion-props.json").read_text()) == props
            observed.append(copy.deepcopy(props))
            # Stop at the real final-props boundary, before a render can start.
            raise ObservedRoot

        with pytest.raises(ObservedRoot):
            run._execute_remotion_locked(
                timeline, assets, tmp_path / "unused.mp4", provenance_out_path=tmp_path / "unused.mp4",
                project_dir=host, composition_id=run.DEFAULT_COMPOSITION_ID, theme_path=None,
                min_free_gb=None, frame_numbers=(0,), frame_output_dir=tmp_path / "unused-frames",
                on_scene_props=observe,
                **({"materialized_root": materialized, "materialized_objects": objects} if managed else {}))
        assert not (tmp_path / ".remotion-props.json").exists()
        assert not list(tmp_path.glob(".remotion-runtime-*"))
        profile = run._canonical_profile(timeline, snapshot["registry"], None)
        basis = {"width": profile.width, "height": profile.height,
                 "fps": profile.fps_rational[0] / profile.fps_rational[1]}
        assert basis == {"width": 200, "height": 100, "fps": 24}
        assert observed[0]["theme"]["visual"]["color"]["bg"] == "#050814"
        return observed[0], basis, snapshot

    yield prepare
    assert hashes() == before


def plate(params=None, **fields):
    return {"id": "opaque:plate", "track": "picture", "clipType": "am-snap-plate",
            "at": 0, "hold": 1, "params": {} if params is None else params,
            "app": {"astrid": {"cut": "c08", "layer": "field"}}, **fields}


@pytest.mark.parametrize("owned", [False, True])
def test_prepared_real_plate_background_picture_and_gap(prepared_root, owned):
    owner = {"elementRef": {"kind": "effect", "id": "am-snap-plate", "packId": "astrid_motion"}} if owned else {}
    props, basis, snapshot = prepared_root([
        plate({"src": SRC}, id="picture", hold=0.5, **owner),
        plate({"fill": "#F7F4ED"}, id="field", at=0.75, hold=0.25, **owner),
    ])
    rows = scene_answers.host_frames_at(props, basis, [0, 11, 12, 17, 18, 23, 24, 42, 48])
    assert [(r["coverage"], r["content"]) for r in rows] == [
        ("COVERED", "CONTENT"), ("COVERED", "CONTENT"), ("GAP", None), ("GAP", None),
        ("COVERED", "EMPTY"), ("COVERED", "EMPTY"), ("COVERED", "CONTENT"),
        ("COVERED", "EMPTY"), ("GAP", None),
    ]
    for row in rows:
        for layer in row["layers"]:
            assert layer["bounds"] == {"status": "known", "rect": {"x": 0, "y": 0, "width": 200, "height": 100}}
            assert layer["fullFrame"] and layer["opacity"] == 1 and layer["visible"] is True
            assert layer["opaqueSupport"] == "rect"
            assert layer["clipId"] in snapshot["metadata"]["scene_clips"]
            assert not row["findings"]


def test_prepared_empty_raw_plate_stays_unknown(prepared_root):
    props, basis, _ = prepared_root([plate()])
    assert props["timeline"]["clips"][0]["params"] == {}
    row = scene_answers.host_frames_at(props, basis, [0])[0]
    assert row["coverage"] == "UNKNOWN" and row["content"] is None
    assert row["layers"][0]["bounds"]["status"] == "unknown"
    assert row["layers"][0]["opacity"] is None


SSR_SCRIPT = r"""
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {Internals} from 'remotion';
import {TimelineComposition, DEFAULT_THEME} from '@banodoco/timeline-composition';
import {remotionPrimitiveAliasesFor} from './webpack-alias.mjs';
const project = process.cwd();
const aliases = remotionPrimitiveAliasesFor(project)({}).resolve.alias;
const bundle = await build({stdin: {contents: `
export {resolveHostRegistries} from './src/host-registries';
export {default as Plate} from '../astrid/packs/astrid_motion/rendering/elements/effects/am-snap-plate/component';`,
resolveDir: project}, bundle: true, write: false, platform: 'node', format: 'esm', jsx: 'automatic',
nodePaths: [path.join(project, 'node_modules')], logLevel: 'silent',
plugins: [{name: 'renderer-aliases', setup(builder) {
  builder.onResolve({filter: /^[^./]/}, async args => {
    if (args.pluginData?.resolvingPackage) return;
    const alias = Object.keys(aliases).find(key => args.path === key || args.path.startsWith(`${key}/`));
    if (alias) return builder.resolve(path.join(aliases[alias], args.path.slice(alias.length).split('?')[0]),
      {kind: args.kind, resolveDir: project});
    const resolved = await builder.resolve(args.path, {kind: args.kind, resolveDir: args.resolveDir,
      pluginData: {resolvingPackage: true}});
    if (resolved.errors.length) return {errors: resolved.errors};
    return {path: path.isAbsolute(resolved.path) ? pathToFileURL(resolved.path).href : resolved.path, external: true};
  });
}}]});
const {resolveHostRegistries, Plate} = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`);
const {props, basis, cases} = JSON.parse(readFileSync(0, 'utf8'));
const registries = resolveHostRegistries(props.theme);
const metadata = {id: 'plate', component: TimelineComposition, defaultProps: {}, ...basis,
  durationInFrames: 48, folderName: null, parentFolderName: null, nonce: [], schema: null,
  calculateMetadata: null, stack: null};
const render = (p, frame) => renderToStaticMarkup(h(Internals.CompositionManager.Provider, {value: {
  compositions: [metadata], currentCompositionMetadata: null, folders: [],
  canvasContent: {type: 'composition', compositionId: 'plate'},
}}, h(Internals.TimelineContext.Provider, {value: {frame: {plate: frame}, playing: false,
  imperativePlaying: {current: false}, audioAndVideoTags: {current: []}}},
h(Internals.CanUseRemotionHooksProvider, null, h(TimelineComposition, {...p, registries})))));
const result = {};
for (const [name, params] of Object.entries(cases)) {
  const p = structuredClone(props);
  for (const c of p.timeline.clips) c.params = params;
  const html = [0, 1, 2, 6, 12, 23, 24, 47, 48].map(frame => render(p, frame));
  result[name] = {hash: createHash('sha256').update(JSON.stringify(html)).digest('hex'), first: html[0]};
}
console.log(JSON.stringify({result, defaultTheme: DEFAULT_THEME,
  registered: typeof Plate.prepareSceneCapability === 'function'}));
"""


@pytest.fixture
def plate_ssr(prepared_root, host):
    node = resolve_pinned_node(ROOT / "remotion", override=os.environ.get("ASTRID_NODE_EXECUTABLE"))
    assert node.ok and node.version == "v20.19.4"
    script = host / "plate-ssr.mjs"
    script.write_text(SSR_SCRIPT)

    def render(props, basis, cases):
        result = subprocess.run([str(node.path), str(script)], cwd=host,
                                input=json.dumps({"props": props, "basis": basis, "cases": cases}),
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    return render


def test_registered_plate_ssr_preserves_existing_draw_and_default_theme(prepared_root, plate_ssr):
    from astrid.core.theme import builtin_theme

    props, basis, _ = prepared_root([plate()])
    # Keep the SSR hashes scoped to plate rendering; occurrence track grouping
    # is covered by the shot-layering tests and changes the composition shell.
    props = {**props, "timeline": {**props["timeline"],
                                    "tracks": [{"id": "picture", "kind": "visual", "label": "Picture"}],
                                    "clips": [{**clip, "track": "picture"}
                                              for clip in props["timeline"]["clips"]]}}
    cases = {
        "empty": {}, "fill": {"fill": "paper"}, "token": {"fill": "  ink  "},
        "image": {"src": SRC},
        # Explicit background still wins over fill, while the image remains drawn.
        "background": {"src": SRC, "fill": "ink", "background": "#123456"},
        "transparent": {"fill": "transparent", "background": "transparent"},
        "tint": {"src": SRC, "tint": {"color": "#123456", "opacity": 0.4}},
        "wipe": {"src": SRC, "enter": "blockWipe", "exitWipe": "blockWipe"},
        "mosaic": {"src": SRC, "mosaicIn": {"frames": 8}, "mosaicOut": {"frames": 8}},
        "motion": {"src": SRC, "zoom": 2, "pan": {"dx": 2, "dy": -1},
                   "punchAt": [2], "push": {"to": 3, "frames": 12}, "bob": 1, "words": [[0, 0.5]]},
    }
    result = plate_ssr(props, basis, cases)
    assert result["defaultTheme"] == builtin_theme()
    # Captured from the unchanged component before adding its capability.
    assert {name: row["hash"] for name, row in result["result"].items()} == {
        "background": "f03701fabf14defa15d1cb0e39aa05d6321165df41ca2203d778a762cdec13f1",
        "empty": "494c59a53d312da7d0aca95fc478bffd202e84a19698e2ecbc4a1c960417cd11",
        "fill": "74b910d8107ce9374dada339226fb023e079bd27e69924283363ce56136fcfa9",
        "image": "381a3ab52c2ce585098c21cdd5958ee46f6ce07d280487fec3e8445e3c4dc0f5",
        "mosaic": "39d65ebf2c9bfec6d51d88984ff5a5078cb766fab80ce4d2ec461d0a5f3fdbe3",
        "motion": "80585d662bcfbeb756a4ce648ac64fe8ccabdd816420895e4e036964b8820d35",
        "tint": "51c9d82d6501c194183be031d8146c0e8d35986e223ad97eefae0e3d83d7d95d",
        "token": "f4d67b0ff68ace889c403f50d6ebe5895db5a9856d399a7a980e170e2b5468f9",
        "transparent": "22df2962249e97750fc7b5fa62d737f705be3fbbd627b2542afaa0dfce50990d",
        "wipe": "087dfc0b1c79e255db26e347d8e0195032045ee8db0fe986c3637ddf2621543e",
    }
    assert result["registered"] is True
    # These are the extracted plateSurface decisions exercised by the actual
    # registered component, rather than source-location checks: a fill can
    # paint an assetless plate, an asset still draws over its backing, and an
    # explicit background wins over fill.
    assert 'background-color:#F7F4ED' in result["result"]["fill"]["first"]
    assert '<img' not in result["result"]["fill"]["first"]
    assert 'background-color:#25241F' in result["result"]["token"]["first"]
    assert '<img' in result["result"]["image"]["first"]
    assert 'background-color:#123456' in result["result"]["background"]["first"]
    assert 'background-color:#F7F4ED' in result["result"]["image"]["first"]
    assert '<img' not in result["result"]["empty"]["first"]


def test_managed_transparent_image_has_opaque_backing_without_media_alpha_proof(prepared_root):
    props, basis, _ = prepared_root([plate(asset="P")], managed=True)
    asset = props["timeline"]["clips"][0]["asset"]
    assert props["assets"]["assets"][asset]["file"] == "http://127.0.0.1:4321/plate.png"
    assert props["timeline"]["clips"][0]["params"] == {}
    row = scene_answers.host_frames_at(props, basis, [0])[0]
    assert (row["coverage"], row["content"]) == ("COVERED", "CONTENT")
    assert row["layers"][0]["opaqueSupport"] == "rect"
    assert row["layers"][0]["opacity"] == 1


def test_real_plate_fade_dip_and_translucent_gap(prepared_root):
    props, basis, _ = prepared_root([plate({"src": SRC}, effects={"fade_in": 0.5})])
    rows = scene_answers.host_frames_at(props, basis, [0, 1, 6, 11, 12, 23, 48])
    assert [r["coverage"] for r in rows] == ["DIP"] * 4 + ["COVERED", "COVERED", "GAP"]
    assert [r["content"] for r in rows] == [None] * 4 + ["CONTENT", "CONTENT", None]
    assert rows[2]["layers"][0]["opacity"] == 0.5
    assert all(r["layers"][0]["fullFrame"] for r in rows[:-1])
    props, basis, _ = prepared_root([plate({"src": SRC}, opacity=0.4)])
    row = scene_answers.host_frames_at(props, basis, [0])[0]
    assert row["coverage"] == "GAP" and row["content"] is None
    assert row["layers"][0]["opacity"] == 0.4
    assert row["layers"][0]["bounds"]["status"] == "known"


def test_real_plate_wipe_mosaic_pan_and_tint_keep_the_painted_backing(prepared_root):
    props, basis, _ = prepared_root([plate({
        "src": SRC, "zoom": 2, "pan": {"dx": 4, "dy": -3}, "punchAt": [2],
        "tint": {"color": "#123456", "opacity": 0.5},
        "enter": "blockWipe", "exitWipe": "blockWipe",
        "mosaicIn": {"frames": 8}, "mosaicOut": {"frames": 8},
    })])
    rows = scene_answers.host_frames_at(props, basis, [0, 1, 2, 6, 12, 23])
    assert all((r["coverage"], r["content"]) == ("COVERED", "CONTENT") for r in rows)
    assert all(r["layers"][0]["opacity"] == 1 for r in rows)


@pytest.mark.parametrize("params", [
    {"src": SRC, "background": "transparent"},
    {"fill": "rgba(0, 0, 0, .5)"},
    {"fill": "var(--unresolved)"},
    {"fill": "   "},
])
def test_unproven_backing_cannot_claim_opaque_coverage(prepared_root, params):
    props, basis, _ = prepared_root([plate(params)])
    row = scene_answers.host_frames_at(props, basis, [0])[0]
    assert row["coverage"] == "UNKNOWN" and row["content"] is None
    assert row["layers"][0]["opacity"] is None


@pytest.mark.parametrize("outer", [
    {"cropLeft": 0.2},
    {"entrance": {"type": "scale-in", "duration": 0.5}},
    {"entrance": {"type": "fade-up", "duration": 0.5}},
])
def test_public12_outer_geometry_gap_does_not_erase_original_positive(prepared_root, outer):
    from astrid.core.timeline.onscreen import element_coverage, visible_at

    # Keep the private positive intact while checking the package-owned public
    # capability result for the same wrapper.
    original = plate({"src": SRC}, **outer)
    private = visible_at({"tracks": [{"id": "picture", "kind": "visual"}], "clips": [original]},
                         0, 24, {"width": 200, "height": 100}, element_coverage(["am-snap-plate"]))[0]
    if "fade-up" in str(outer):
        assert private.opacity == 0 and private.full_frame is True
    else:
        assert private.full_frame is False and private.opacity == 1
    props, basis, _ = prepared_root([original])
    row = scene_answers.host_frames_at(props, basis, [0])[0]
    layer = row["layers"][0]
    assert row["coverage"] == "GAP" and row["content"] is None and row["findings"] == []
    assert layer["targetBounds"] == {
        "status": "known", "rect": {"x": 0, "y": 0, "width": 200, "height": 100}}
    if "cropLeft" in outer:
        expected_bounds = {"x": 40, "y": 0, "width": 160, "height": 100}
        expected_opacity = 1
        expected_visible = True
    elif outer["entrance"]["type"] == "scale-in":
        expected_bounds = {"x": 15, "y": 7.5, "width": 170, "height": 85}
        expected_opacity = 0
        expected_visible = False
    else:
        expected_bounds = {"x": 0, "y": 40, "width": 200, "height": 60}
        expected_opacity = 0
        expected_visible = False
    assert layer["bounds"] == {"status": "known", "rect": expected_bounds}
    assert layer["opacity"] == expected_opacity
    assert layer["visible"] is expected_visible
    assert layer["fullFrame"] is False
    assert layer["opaqueSupport"] == "rect"
    assert layer["coverage"] == "partial"
    assert layer["findings"] == []
