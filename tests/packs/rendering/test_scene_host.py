"""Public10 host transport with current generated maps and renderer aliases.

Status is replaced here; supplied props are not source-admission or pixel proof.
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from astrid.core.element.registry import ElementRegistry
from astrid.core.element.schema import ElementDefinition
from astrid.core.rendering import scene_answers
from astrid.core.rendering.node_pin import resolve_pinned_node
from astrid.core.rendering.remotion_runtime import RemotionRuntimeStatus
from astrid.core.theme import builtin_theme
from scripts import gen_effect_registry

ROOT = Path(__file__).resolve().parents[3]
REMOTION = ROOT / "remotion"

BASIS = {"width": 1920, "height": 1080, "fps": 30}


@pytest.fixture(scope="module")
def host_project(tmp_path_factory, request):
    project_root = tmp_path_factory.mktemp("native-host")
    project = project_root / "remotion"
    (project / "src").mkdir(parents=True)
    (project_root / "astrid").symlink_to(ROOT / "astrid", target_is_directory=True)
    # Reuse a coherent peer graph read-only, with the actual pinned public12
    # extracted into this test's private project. No install or shared mutation.
    peers = Path(os.environ.get("ASTRID_TEST_NODE_MODULES", REMOTION / "node_modules"))
    assert (peers / "esbuild").is_dir(), "requires provisioned renderer peers"
    modules = project / "node_modules"
    modules.mkdir()
    for peer in ("react", "react-dom", "remotion", "@remotion/media", "@remotion/layout-utils", "esbuild"):
        source = peers / peer
        assert source.is_dir(), f"missing peer: {source}"
        if peer != "esbuild":
            expected = "18.3.1" if peer in ("react", "react-dom") else "4.0.509"
            assert json.loads((source / "package.json").read_text())["version"] == expected
        target = modules / peer
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(source.resolve(), target_is_directory=True)
    version = getattr(request, "param", "0.0.12")
    digests = {
        "0.0.12": "0a7b61529099ce994f807c6d7b1078303a09e7c0219144cc4b07467977f0f1a3",
        "0.0.13": "9412ae25c306d5118241f4d631e8eecc6eef9b2fb5a98302d40938e9f6d54738",
    }
    artifact = REMOTION / f"artifacts/banodoco-timeline-composition-{version}.tgz"
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digests[version]
    with tarfile.open(artifact) as archive:
        archive.extractall(project, filter="data")
    package = modules / "@banodoco/timeline-composition"
    package.parent.mkdir(parents=True)
    (project / "package").rename(package)
    for name in ("scene-query.mjs", "native-scene.mjs", "webpack-alias.mjs", "package.json"):
        shutil.copyfile(REMOTION / name, project / name)
    for name in ("host-scene.ts", "host-registries.ts"):
        shutil.copyfile(REMOTION / "src" / name, project / "src" / name)
    pack = project_root / "probe"
    elements = list(gen_effect_registry._element_registry().list())
    rendering = {}
    for kind, element_id in (("effects", "custom"), ("animations", "fade-up"), ("transitions", "fade")):
        element = pack / kind / element_id
        element.mkdir(parents=True)
        source = (REMOTION / "tests" / f"custom-{kind}.tsx").read_text()
        if element_id == "custom":
            source = "import {probeWidth} from 'probe-dependency';\n" + source.replace("width: 1920", "width: probeWidth")
        (element / "component.tsx").write_text(source)
        (element / "element.yaml").write_text(f"id: {element_id}\nkind: {kind[:-1]}\n")
        rendering[f"{kind}/{element_id}"] = {"type": "element", "path": f"{kind}/{element_id}/element.yaml"}
        elements.append(ElementDefinition(
            id=element_id, kind=kind, root=element, source="pack:probe", editable=True,
            priority=100, component=element / "component.tsx", schema={}, defaults={"durationFrames": 8},
            metadata={"pack_id": "probe", "kind": "wrapper", "phase": ["entrance", "exit"],
                      **({"clipTypeAliases": ["custom-alias"]} if element_id == "custom" else {})}))
    (pack / "pack.json").write_text(json.dumps({"id": "probe", "rendering": rendering}))
    dependency = pack / "node_modules/probe-dependency"
    dependency.mkdir(parents=True)
    (dependency / "package.json").write_text('{"type":"module","exports":"./index.js"}')
    (dependency / "index.js").write_text('export const probeWidth = 1920;')
    registry = ElementRegistry(elements)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(gen_effect_registry, "_element_registry", lambda: registry)
        for kind in ("effects", "animations", "transitions"):
            (project / "src" / f"{kind}.generated.ts").write_text(gen_effect_registry.generate_element_registry(kind))
    assert json.loads((package / "package.json").read_text())["version"] == version
    def hashes():
        return {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in package.rglob("*") if p.is_file()}
    before = hashes()
    assert len(before) == 77
    yield project, pack
    assert hashes() == before


@pytest.fixture
def native_host(host_project, monkeypatch):
    project, pack = host_project
    toolchain = resolve_pinned_node(REMOTION, override=os.environ.get("ASTRID_NODE_EXECUTABLE"))
    assert toolchain.ok and toolchain.version == "v20.19.4"
    for key in ("NODE_OPTIONS", "NODE_PATH", "PYTHONPYCACHEPREFIX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(pack))
    monkeypatch.setattr(scene_answers, "remotion_runtime_status", lambda **kwargs:
                        RemotionRuntimeStatus(True, project, node_executable=toolchain.path))
    return project


def props(clips=None, *, theme=None, assets=None):
    return {"timeline": {"theme": "stale-config-slug", "tracks": [{"id": "plate", "kind": "visual"}],
                         "clips": clips or []},
            "assets": assets if assets is not None else {"assets": {}},
            "theme": theme if theme is not None else builtin_theme()}


def test_native_host_default_custom_alias_capability_and_immutable_props(native_host):
    p = props([{"id": "first", "track": "plate", "clipType": "custom-alias", "at": 0, "hold": 1,
                "params": {"nested": [1, None, "preserve"]}}])
    original = copy.deepcopy(p)
    first, end = scene_answers.host_frames_at(p, BASIS, [0, 30])
    assert first["coverage"] == "COVERED"
    assert first["layers"][0]["bounds"] == {"status": "known", "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080}}
    assert first["layers"][0]["opacity"] == 1
    assert end["coverage"] == "GAP" and end["layers"] == []
    assert p == original
    p["timeline"]["clips"][0]["elementRef"] = {"kind": "effect", "id": "custom", "packId": "probe"}
    assert scene_answers.host_frames_at(p, BASIS, [0])[0]["coverage"] == "COVERED"
    p["timeline"]["clips"][0]["elementRef"]["packId"] = "missing"
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="Unknown effect owner"):
        scene_answers.host_frames_at(p, BASIS, [0])


def test_native_host_uses_exact_assets_and_runtime_theme(native_host):
    clip = {"id": "media", "track": "plate", "clipType": "image", "asset": "P", "at": 0, "hold": 1}
    basis = {"width": 200, "height": 100, "fps": 30000 / 1001}
    p = props([clip], assets={"assets": {"P": {"file": "p.png", "type": "image", "resolution": "100x200"}}})
    answer = scene_answers.host_frames_at(p, basis, [0])[0]
    assert answer["layers"][0]["bounds"]["rect"] == {"x": 75, "y": 0, "width": 50, "height": 100}
    assert answer["coverage"] == "UNKNOWN" and answer["layers"][0]["opacity"] is None
    p["assets"] = {"assets": {}}
    assert scene_answers.host_frames_at(p, basis, [0])[0]["layers"][0]["bounds"]["status"] == "unknown"
    p = props([{**clip, "clipType": "art-card"}])
    answer = scene_answers.host_frames_at(p, basis, [0])[0]
    assert answer["coverage"] == "UNKNOWN"
    # Admission is distinct from alpha: art-card intentionally has no declared
    # bounds capability. Inspect the prepared host set, never invent geometry.
    script = """
import {loadHostScene} from './native-scene.mjs';
import {DEFAULT_THEME} from '@banodoco/timeline-composition';
const prepare = await loadHostScene(process.cwd());
console.log(JSON.stringify(['banodoco-default', '2rp'].map(id =>
  prepare({timeline: {theme: 'stale', clips: [], tracks: []}, assets: {assets: {}},
    theme: {...DEFAULT_THEME, id}}, {width: 200, height: 100, fps: 30}, {}).elements.has('art-card'))));
"""
    result = subprocess.run([str(scene_answers.remotion_runtime_status().node_executable), "--input-type=module", "-e", script],
                            cwd=native_host, check=True, text=True, capture_output=True, timeout=30)
    assert json.loads(result.stdout) == [False, True]


@pytest.mark.parametrize("align", ["start", "center", "end"])
def test_native_host_resolves_custom_transition_defaults_and_boundaries(native_host, align):
    clips = [{"id": "first", "track": "plate", "clipType": "custom", "at": 0, "hold": 1,
              "transition": {"id": "fade", "align": align}},
             {"id": "next", "track": "plate", "clipType": "custom", "at": 1, "hold": 1}]
    answers = scene_answers.host_frames_at(props(clips), BASIS, [21, 22, 26, 29, 30, 33, 34, 37, 38, 59, 60])
    assert answers[-1]["layers"] == []
    incoming_start = {"end": 22, "center": 26, "start": 30}[align]
    active = [a["frame"] for a in answers if any(layer["clipId"] == "next" for layer in a["layers"])]
    assert min(active) == incoming_start
    if align == "end":
        incoming = next(layer for layer in answers[1]["layers"] if layer["clipId"] == "next")
        assert incoming["drawLocalFrame"] == incoming["ownFrame"] == 0
    clips[0]["transition"] = "not-admitted"
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="Unknown transition"):
        scene_answers.host_frames_at(props(clips), BASIS, [0])


def test_large_native_batch_is_not_truncated(native_host):
    p = props([{"id": "first", "track": "plate", "clipType": "custom-alias", "at": 0, "hold": 10}])
    frames = list(range(300))
    answers = scene_answers.host_frames_at(p, BASIS, frames)
    assert [answer["frame"] for answer in answers] == frames
    assert all(answer["coverage"] == "COVERED" for answer in answers)


def test_native_host_rational_frame_clock_is_half_open(native_host):
    fps = 30000 / 1001
    clip = {"id": "immutable:clip", "track": "plate", "clipType": "custom-alias", "at": 1001 / 30000,
            "hold": 2002 / 30000, "opaqueExtension": {"preserved": [None, 2]}}
    p = props([clip])
    before = copy.deepcopy(p)
    answers = scene_answers.host_frames_at(p, {**BASIS, "fps": fps}, [0, 1, 2, 3, 1])
    assert [[layer["clipId"] for layer in answer["layers"]] for answer in answers] == [
        [], ["immutable:clip"], ["immutable:clip"], [], ["immutable:clip"]]
    assert p == before


def test_native_host_preserves_root_painter_order_and_current_roles(native_host):
    p = props([
        {"id": "occ:front", "track": "front", "clipType": "custom-alias", "at": 0, "hold": 1,
         "opacity": 0.5},
        {"id": "occ:plate", "track": "plate", "clipType": "custom-alias", "at": 0, "hold": 1},
    ])
    p["timeline"]["tracks"] = [
        {"id": "front", "kind": "visual"}, {"id": "plate", "kind": "visual"},
        {"id": "music", "kind": "audio"},
    ]
    answer = scene_answers.host_frames_at(p, BASIS, [0])[0]
    assert [(layer["clipId"], layer["role"]) for layer in answer["layers"]] == [
        ("occ:plate", "picture"), ("occ:front", "content")]
    assert answer["coverage"] == "COVERED" and answer["content"] == "CONTENT"


def test_native_host_passes_public12_compiled_transitions_unchanged(native_host):
    p = props([
        {"id": "occ:first", "track": "plate", "clipType": "custom-alias", "at": 0, "hold": 1},
        {"id": "occ:next", "track": "plate", "clipType": "custom-alias", "at": 1, "hold": 1},
    ])
    p["compiledTransitions"] = {"fps": 30, "tracks": [{
        "trackId": "plate", "clipIds": ["occ:first", "occ:next"],
        "incomingTransitions": [None, {"T": 8}],
    }]}
    original = copy.deepcopy(p)
    answers = scene_answers.host_frames_at(p, BASIS, [21, 22, 25, 29, 30])
    assert [len(answer["layers"]) for answer in answers] == [1, 2, 2, 2, 1]
    incoming = answers[2]["layers"][1]
    assert (incoming["clipId"], incoming["drawLocalFrame"], incoming["ownFrame"]) == (
        "occ:next", 3, 0)
    assert all(answer["coverage"] == "COVERED" for answer in answers)
    assert p == original
    p["compiledTransitions"]["fps"] = 24
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="transition/invalid-compilation"):
        scene_answers.host_frames_at(p, BASIS, [0])


@pytest.mark.parametrize("missing", ["theme", "assets", "tracks"])
def test_missing_host_input_is_unavailable_not_an_empty_scene(native_host, missing):
    p = props()
    if missing == "tracks":
        del p["timeline"]["tracks"]
    else:
        del p[missing]
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="Root"):
        scene_answers.host_frames_at(p, BASIS, [0])


def test_source_metadata_does_not_supply_missing_host_inputs(native_host):
    p = props()
    p["timeline"]["metadata"] = {"revision_id": "unverified", "certified": True}
    p.pop("assets")
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="Root"):
        scene_answers.host_frames_at(p, BASIS, [0])


def test_declared_background_has_known_empty_content(native_host, tmp_path, monkeypatch):
    from astrid.core.timeline import onscreen

    # A role declaration for the rectangle probe, not a claim about AM media.
    manifest = tmp_path / "probe/rendering/elements/effects/custom-alias/element.yaml"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("metadata:\n  coverage: background\n")
    monkeypatch.setattr(onscreen, "_PACKS_ROOT", tmp_path)
    monkeypatch.setattr(onscreen, "_COVERAGE_CACHE", {})
    p = props([{"id": "background", "track": "plate", "clipType": "custom-alias", "at": 0, "hold": 1}])
    answer = scene_answers.host_frames_at(p, BASIS, [0])[0]
    assert answer["layers"][0]["role"] == "background"
    assert answer["coverage"] == "COVERED" and answer["content"] == "EMPTY"


def test_unknown_animation_diagnostics_and_root_tail_are_preserved(native_host):
    p = props([{"id": "clip", "track": "plate", "clipType": "custom-alias",
                "at": 0, "hold": 1, "entrance": "not-registered"}])
    answer = scene_answers.host_frames_at(p, BASIS, [0])[0]
    assert answer["coverage"] == "UNKNOWN"
    assert answer["layers"][0]["visible"] == "unknown"
    assert {finding["code"] for finding in answer["findings"]} == {"unknown-animation-geometry"}
    p = props()
    p["timeline"]["app"] = {"astrid_render_clock": {"render_duration_frames": 60}}
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="RenderClockTail"):
        scene_answers.host_frames_at(p, BASIS, [0])
