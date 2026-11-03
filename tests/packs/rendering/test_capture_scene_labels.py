"""The bounded owner/capture seam; native public queries, no renderer or sockets."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.rendering.node_pin import resolve_pinned_node
from astrid.core.rendering.remotion_runtime import RemotionRuntimeStatus
from astrid.core.theme import builtin_theme
from astrid.core.rendering import scene_answers
from astrid.packs.rendering.actions.timeline_visualize.composed_frame import (
    CaptureSceneLabels,
)
from astrid.packs.rendering.actions.timeline_visualize.overlays.labels import (
    overlay_labels_frame,
)
from scripts import gen_effect_registry

ROOT = Path(__file__).resolve().parents[3]
REMOTION = ROOT / "remotion"
BASIS = {"width": 200, "height": 100, "fps": 30000 / 1001}
SOURCE = "retained-revision-and-projection"
ADDRESSES = {"exact:with:colons": "c08.title"}


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    root = tmp_path_factory.mktemp("capture-label-host")
    (root / "astrid").symlink_to(ROOT / "astrid", target_is_directory=True)
    project = root / "remotion"
    project.mkdir()
    (project / "src").mkdir()
    modules = project / "node_modules"
    modules.mkdir()
    runtime_project = Path(os.environ.get("ASTRID_REMOTION_PROJECT_DIR", str(REMOTION))).resolve()
    peers = Path(os.environ.get("ASTRID_TEST_NODE_MODULES", str(runtime_project / "node_modules"))).resolve()
    for peer in ("react", "react-dom", "remotion", "@remotion/media", "@remotion/layout-utils", "esbuild"):
        target = modules / peer
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to((peers / peer).resolve(), target_is_directory=True)
        assert target.is_dir(), f"missing provisioned peer: {target}"
    artifact = Path(os.environ.get(
        "ASTRID_TEST_COMPOSITION_ARTIFACT",
        str(runtime_project / json.loads((runtime_project / "package.json").read_text())["dependencies"][
            "@banodoco/timeline-composition"].removeprefix("file:")),
    )).resolve()
    lock = json.loads((runtime_project / "package-lock.json").read_text(encoding="utf-8"))
    integrity = lock["packages"]["node_modules/@banodoco/timeline-composition"]["integrity"]
    expected_digest = base64.b64decode(integrity.removeprefix("sha512-"))
    with tarfile.open(artifact) as archive:
        manifest = json.load(archive.extractfile("package/package.json"))
        assert manifest["version"] == lock["packages"][
            "node_modules/@banodoco/timeline-composition"
        ]["version"]
        assert hashlib.sha512(artifact.read_bytes()).digest() == expected_digest
        archive.extractall(project, filter="data")
    package = modules / "@banodoco/timeline-composition"
    package.parent.mkdir()
    (project / "package").rename(package)
    for name in ("scene-query.mjs", "native-scene.mjs", "webpack-alias.mjs", "package.json"):
        shutil.copyfile(REMOTION / name, project / name)
    for name in ("host-scene.ts", "host-registries.ts"):
        shutil.copyfile(REMOTION / "src" / name, project / "src" / name)
    for kind in ("effects", "animations", "transitions"):
        (project / "src" / f"{kind}.generated.ts").write_text(gen_effect_registry.generate_element_registry(kind))
    return project


@pytest.fixture
def native(host, monkeypatch):
    runtime_project = Path(os.environ.get("ASTRID_REMOTION_PROJECT_DIR", str(REMOTION))).resolve()
    node = resolve_pinned_node(runtime_project, override=os.environ.get("ASTRID_NODE_EXECUTABLE"))
    assert node.ok and node.version == "v20.19.4"
    for key in ("NODE_OPTIONS", "NODE_PATH", "PYTHONPYCACHEPREFIX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(scene_answers, "remotion_runtime_status", lambda **kwargs:
                        RemotionRuntimeStatus(True, host, node_executable=node.path))


def props(kind="text-card"):
    return {"timeline": {"tracks": [{"id": "content", "kind": "visual"}], "clips": [
        {"id": "exact:with:colons", "track": "content", "clipType": kind, "at": 0, "hold": 1,
         "params": {"text": "Ordinary title"}}]},
            "assets": {"assets": {}}, "theme": builtin_theme()}


def adapter(p=None, addresses=None):
    return CaptureSceneLabels(source_identity=SOURCE, props=p or props(), basis=BASIS,
                              addresses=ADDRESSES if addresses is None else addresses)


def retain(capture, tmp_path, frames=(0, 30)):
    produced = {}
    for frame in frames:
        path = tmp_path / f"{frame}.png"
        Image.new("RGB", (100, 50)).save(path)
        produced[frame] = path
    capture.retain(produced)


def test_real_host_labels_are_retained_once_and_drawn_without_private_math(native, tmp_path, monkeypatch):
    p = props()
    capture = adapter(p)
    calls = []
    query = scene_answers.host_frames_at

    def observe(*args):
        calls.append(copy.deepcopy(args))
        return query(*args)

    monkeypatch.setattr(scene_answers, "host_frames_at", observe)
    capture.observe(p, (0, 30))
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        capture.frame(0, source_identity=SOURCE)  # preparation alone is not capture success
    retain(capture, tmp_path)
    assert calls == [(p, BASIS, (0, 30))]
    answer = capture.frame(0, source_identity=SOURCE)
    assert answer["layers"][0]["clipId"] == "exact:with:colons"
    layers, canvas = capture.labels(0, source_identity=SOURCE)
    assert layers[0].address == "c08.title"  # supplied projection, no colon parsing
    assert canvas == {"width": 200, "height": 100}
    assert layers[0].opacity == answer["layers"][0]["opacity"]
    assert capture.frame(30, source_identity=SOURCE)["layers"] == []
    monkeypatch.setattr("astrid.core.timeline.onscreen.visible_at", lambda *a, **k: pytest.fail("private visibility"))
    image = Image.new("RGB", (100, 50))
    out = overlay_labels_frame(image, {}, 0, BASIS["fps"], capture=capture, source_identity=SOURCE)
    assert out.tobytes() != image.tobytes()
    assert overlay_labels_frame(image, {}, 30, BASIS["fps"], capture=capture,
                                source_identity=SOURCE).tobytes() == image.tobytes()
    assert len(calls) == 1
    answer["layers"].clear()
    assert capture.frame(0, source_identity=SOURCE)["layers"]


@pytest.mark.parametrize("changed", ["timeline", "assets", "theme"])
def test_stale_root_inputs_refuse_before_query(tmp_path, monkeypatch, changed):
    p = props()
    capture = adapter(p)
    p[changed]["stale"] = True
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("stale query"))
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="mismatch"):
        capture.observe(p, (0,))
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        capture.retain({})


@pytest.mark.parametrize("addresses", [{}, {"title": "c08.title"}])
def test_missing_or_guessed_projected_identity_refuses(monkeypatch, addresses):
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("identity query"))
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="projected"):
        adapter(addresses=addresses).observe(props(), (0,))


@pytest.mark.parametrize("frames", [(), (1, 0), (0, 0), (-1,), (False,)])
def test_only_actual_sorted_unique_capture_frames_are_accepted(frames):
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="mismatch"):
        adapter().observe(props(), frames)


def test_mixed_or_missing_capture_cannot_retain_facts(native, tmp_path):
    capture = adapter()
    capture.observe(props(), (0, 30))
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="complete"):
        retain(capture, tmp_path, (0,))
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        capture.frame(0, source_identity=SOURCE)


def test_other_source_or_uncaptured_frame_never_falls_back(native, tmp_path):
    capture = adapter()
    capture.observe(props(), (0,))
    retain(capture, tmp_path, (0,))
    for source, frame in (("current-head", 0), (SOURCE, 1)):
        with pytest.raises(scene_answers.SceneQueryUnavailable):
            overlay_labels_frame(Image.new("RGB", (100, 50)), props()["timeline"], frame, 30,
                                 capture=capture, source_identity=source)
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="already used"):
        capture.observe(props(), (0,))
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        capture.frame(0, source_identity=SOURCE)


def test_unknown_public_geometry_is_an_answer_and_unavailability_is_not_empty(native, tmp_path, monkeypatch):
    p = props("undeclared-component")
    capture = adapter(p)
    capture.observe(p, (0,))
    retain(capture, tmp_path, (0,))
    answer = capture.frame(0, source_identity=SOURCE)
    assert answer["coverage"] == "UNKNOWN"
    labels, _ = capture.labels(0, source_identity=SOURCE)
    assert labels[0].bounds is None and labels[0].opacity is None
    assert labels[0].visible == "unknown"
    def unavailable(*args):
        raise scene_answers.SceneQueryUnavailable("host missing")
    monkeypatch.setattr(scene_answers, "host_frames_at", unavailable)
    capture = adapter(p)
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="host missing"):
        capture.observe(p, (0,))
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        capture.frame(0, source_identity=SOURCE)


def test_real_media_bounds_use_public_canvas_and_keep_uncertain_alpha(native, tmp_path):
    p = props("image")
    p["timeline"]["clips"][0]["asset"] = "portrait"
    p["assets"]["assets"]["portrait"] = {"file": "portrait.png", "type": "image", "resolution": "100x200"}
    capture = adapter(p)
    capture.observe(p, (0,))
    retain(capture, tmp_path, (0,))
    labels, canvas = capture.labels(0, source_identity=SOURCE)
    assert labels[0].bounds == (75, 0, 125, 100)
    assert labels[0].opacity is None  # resolved geometry does not establish media alpha
    assert canvas == {"width": 200, "height": 100}


def test_cache_only_has_no_observation_to_retain(tmp_path):
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="complete"):
        retain(adapter(), tmp_path)


@pytest.mark.parametrize("changed", ["frame", "width", "height", "clipId"])
def test_mismatched_public_response_is_not_relabelled(native, monkeypatch, changed):
    query = scene_answers.host_frames_at
    def wrong_response(*args):
        answers = query(*args)
        if changed == "clipId":
            answers[0]["layers"][0]["clipId"] = "foreign:title"
        else:
            answers[0][changed] += 1
        return answers
    monkeypatch.setattr(scene_answers, "host_frames_at", wrong_response)
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="response mismatch"):
        adapter().observe(props(), (0,))
