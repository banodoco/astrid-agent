"""The Python adapter queries the immutable release, without renderer peers."""

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.rendering import remotion_runtime
from astrid.core.rendering.node_pin import resolve_pinned_node
from astrid.core.rendering import scene_answers

ROOT = Path(__file__).resolve().parents[3]
ARTIFACT = ROOT / "remotion/artifacts/banodoco-timeline-composition-0.0.12.tgz"
SHA256 = "0a7b61529099ce994f807c6d7b1078303a09e7c0219144cc4b07467977f0f1a3"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    # Harness loader overrides are inadmissible; use the clean host environment.
    for key in ("PYTHONPYCACHEPREFIX", "NODE_OPTIONS", "NODE_PATH"):
        monkeypatch.delenv(key, raising=False)
    toolchain = resolve_pinned_node(
        ROOT / "remotion", override=os.environ.get("ASTRID_NODE_EXECUTABLE"))
    assert toolchain.ok, toolchain.problem
    node = str(toolchain.path)
    assert toolchain.version == "v20.19.4"
    assert hashlib.sha256(ARTIFACT.read_bytes()).hexdigest() == SHA256
    package = tmp_path / "node_modules/@banodoco/timeline-composition"
    package.parent.mkdir(parents=True)
    with tarfile.open(ARTIFACT) as archive:
        archive.extractall(tmp_path, filter="data")
    (tmp_path / "package").rename(package)
    (tmp_path / "package.json").write_text('{"type":"module"}')
    shutil.copyfile(ROOT / "remotion/scene-query.mjs", tmp_path / "scene-query.mjs")

    def hashes():
        return {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in package.rglob("*") if p.is_file()}

    before = hashes()
    assert len(before) == 77
    # Admission is tested separately. Only the status seam is replaced here;
    # subprocess/import/preparation/query all execute the real installed artifact.
    monkeypatch.setattr(scene_answers, "remotion_runtime_status", lambda **kwargs:
                        remotion_runtime.RemotionRuntimeStatus(
                            True, tmp_path, node_executable=Path(node), node_version="v20.19.4"))
    yield tmp_path, node, package
    assert hashes() == before


def scene(clips=None, transitions=None):
    clips = clips if clips is not None else [
        {"id": "exact:first", "at": 0, "hold": 1, "track": "plate", "clipType": "custom"},
        {"id": "exact:second", "at": 1, "hold": 1, "track": "plate", "clipType": "custom"},
    ]
    return {"tracks": [{"track": {"id": "plate", "kind": "visual"}, "clips": clips,
                        "transitions": transitions if transitions is not None else [None]}],
            "assets": {"assets": {}}, "fps": 30, "width": 641, "height": 359}


def canonical(project, node, payload, frames):
    script = """
import {prepareScene, querySceneFrame} from '@banodoco/timeline-composition/evaluation';
import {readFileSync} from 'node:fs';
const {scene: s, frames} = JSON.parse(readFileSync(0, 'utf8'));
const prepared = prepareScene(s.tracks, s.assets, s.fps, s.width, s.height,
  {roles: s.roles, elements: new Set(s.elements ?? [])});
console.log(JSON.stringify(frames.map(frame => querySceneFrame(prepared, frame))));
"""
    return json.loads(subprocess.check_output(
        [node, "--input-type=module", "-e", script], cwd=project, text=True,
        input=json.dumps({"scene": payload, "frames": frames})))


@pytest.mark.parametrize("kind", ["dip", "dissolve"])
@pytest.mark.parametrize("align", ["start", "center", "end"])
def test_transition_frames_equal_canonical_public_query(installed, kind, align):
    project, node, _ = installed
    payload = scene(transitions=[{"T": 8, "kind": kind, "align": align}])
    original = copy.deepcopy(payload)
    frames = [0, 21, 22, 25, 26, 29, 30, 31, 33, 34, 37, 38, 59, 60, 30]
    answers = scene_answers.frames_at(payload, iter(frames))
    assert answers == canonical(project, node, payload, frames)
    assert payload == original
    assert answers[-2]["layers"] == []  # half-open final boundary
    if align == "end":
        incoming = answers[2]["layers"][1]
        assert (incoming["clipId"], incoming["drawLocalFrame"], incoming["ownFrame"]) == (
            "exact:second", 0, 0)
        assert answers[5]["layers"][1]["ownFrame"] == 0
    if align == "start":
        outgoing = answers[6]["layers"][0]
        assert (outgoing["drawLocalFrame"], outgoing["ownFrame"]) == (30, 29)


def test_cut_fractional_fps_and_empty_scene_are_answers(installed):
    project, node, _ = installed
    payload = scene()
    payload["fps"] = 30000 / 1001
    frames = [29, 30, 59, 60]
    answers = scene_answers.frames_at(payload, frames)
    assert answers == canonical(project, node, payload, frames)
    assert [[layer["clipId"] for layer in answer["layers"]] for answer in answers] == [
        ["exact:first"], ["exact:second"], ["exact:second"], []]
    payload["tracks"] = []
    assert scene_answers.frames_at(payload, [0]) == [
        {"frame": 0, "width": 641, "height": 359, "layers": [],
         "coverage": "GAP", "content": None, "findings": []}]


def test_resolved_painter_order_and_compiled_payload_are_preserved(installed, monkeypatch):
    project, node, _ = installed
    payload = scene()
    payload["tracks"][0]["clips"][0]["compiledExtension"] = {"opaque": [1, None, "keep"]}
    payload["tracks"].append({"track": {"id": "front", "kind": "visual"}, "clips": [
        {"id": "front:exact-id", "at": 0, "hold": 1, "clipType": "custom"}],
        "transitions": []})
    payload["roles"] = {"front:exact-id": "content"}
    requests = []
    run = subprocess.run

    def capture(*args, **kwargs):
        requests.append(json.loads(kwargs["input"]))
        return run(*args, **kwargs)

    monkeypatch.setattr(scene_answers.subprocess, "run", capture)
    answers = scene_answers.frames_at(payload, [1, 0, 1])
    assert requests == [{"scene": payload, "frames": [1, 0, 1]}]
    monkeypatch.setattr(scene_answers.subprocess, "run", run)
    assert answers == canonical(project, node, payload, [1, 0, 1])
    assert [layer["clipId"] for layer in answers[0]["layers"]] == ["exact:first", "front:exact-id"]


@pytest.mark.parametrize("clip_id", ["__proto__", "constructor", "toString"])
@pytest.mark.parametrize("role", [None, "background"])
def test_reserved_clip_ids_use_only_authored_role_overrides(installed, clip_id, role):
    payload = scene(clips=[
        {"id": clip_id, "track": "plate", "clipType": "custom", "at": 0, "hold": 1},
    ], transitions=[])
    if role is not None:
        payload["roles"] = {clip_id: role}
    answer = scene_answers.frames_at(payload, [0])[0]
    assert answer["layers"][0]["clipId"] == clip_id
    assert answer["layers"][0]["role"] == (role or "picture")
    assert answer["coverage"] == "UNKNOWN"


def test_unknown_custom_modifier_and_unresolved_media_stay_unknown(installed):
    payload = scene(clips=[
        {"id": "custom", "at": 0, "hold": 1, "clipType": "my-component",
         "params": {"width": 641, "opaque": True}},
        {"id": "missing", "at": 0, "hold": 1, "asset": "missing-media"},
        {"id": "modifier", "at": 0, "hold": 1, "asset": "image",
         "modifiers": [{"id": "m", "element": "custom-modifier", "params": {}}]},
    ], transitions=[None, None])
    payload["assets"]["assets"]["image"] = {
        "file": "unverified.png", "type": "image", "resolution": "641x359", "opaque": True}
    answer = scene_answers.frames_at(payload, [0])[0]
    assert answer["coverage"] == "UNKNOWN"
    assert all(layer["visible"] == "unknown" for layer in answer["layers"])
    assert all(layer["opaqueSupport"] == "unknown" for layer in answer["layers"])
    assert {finding["code"] for finding in answer["findings"]} >= {
        "unknown-component-internals", "unknown-media-alpha", "modifier-admission-refused"}


@pytest.mark.parametrize("broken", ["missing", "broken"])
def test_public_package_failure_is_unavailable(installed, broken):
    _, _, package = installed
    # Use a separate copy so the immutable fixture itself remains unchanged.
    project = package.parents[2] / broken
    target = project / "node_modules/@banodoco/timeline-composition"
    target.parent.mkdir(parents=True)
    shutil.copytree(package, target)
    shutil.copyfile(ROOT / "remotion/scene-query.mjs", project / "scene-query.mjs")
    evaluation = target / "dist/evaluation.js"
    if broken == "missing":
        evaluation.unlink()
    else:
        evaluation.write_text("throw new Error('broken evaluation');")
    status = scene_answers.remotion_runtime_status()
    from unittest.mock import patch

    with patch.object(scene_answers, "remotion_runtime_status", return_value=
                      remotion_runtime.RemotionRuntimeStatus(
                          True, project, node_executable=status.node_executable)):
        with pytest.raises(scene_answers.SceneQueryUnavailable, match="unavailable"):
            scene_answers.frames_at(scene(), [0])


def test_invalid_runtime_context_never_becomes_empty_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(tmp_path / "missing"))
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="project directory not found"):
        scene_answers.frames_at({"tracks": []}, [0])


def test_invalid_launch_validation_is_unavailable(installed, monkeypatch):
    project, node, _ = installed
    cli = project / remotion_runtime.REMOTION_CLI_RELATIVE_PATH
    cli.parent.mkdir(parents=True)
    cli.write_text("// not executed")
    monkeypatch.setenv("ASTRID_NODE_EXECUTABLE", node)
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(project))
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key)
    for key in remotion_runtime._REF_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(scene_answers, "remotion_runtime_status", lambda **kwargs:
                        remotion_runtime.RemotionRuntimeStatus(
                            False, project, remotion_runtime.resolve_remotion_runtime_tools(project)[1]))
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="missing or invalid launch validation"):
        scene_answers.frames_at(scene(), [0])


def test_unsupported_loader_override_is_unavailable(installed, monkeypatch):
    monkeypatch.setenv("NODE_OPTIONS", "--inspect")
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="unsupported loader"):
        scene_answers.frames_at(scene(), [0])


@pytest.mark.parametrize("escape", [False, True])
def test_missing_or_escaped_admitted_helper_is_unavailable(installed, tmp_path_factory, escape):
    project, _, _ = installed
    helper = project / "scene-query.mjs"
    helper.unlink()
    if escape:
        outside = tmp_path_factory.mktemp("scene-helper") / "scene-query.mjs"
        outside.write_text("throw new Error('must not execute');")
        helper.symlink_to(outside)
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        scene_answers.frames_at(scene(), [0])


@pytest.mark.parametrize("response", [[], [{"frame": 1}], [{"frame": False}], [None], {}])
def test_invalid_response_cardinality_or_frame_is_unavailable(installed, monkeypatch, response):
    monkeypatch.setattr(scene_answers.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(response)))
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="invalid scene query response"):
        scene_answers.frames_at(scene(), [0])


@pytest.mark.parametrize("change", ["unsorted", "unresolved", "frame"])
def test_invalid_resolved_inputs_are_unavailable(installed, change):
    payload = scene()
    frames = [0]
    if change == "unsorted":
        payload["tracks"][0]["clips"].reverse()
    elif change == "unresolved":
        payload["tracks"][0]["transitions"] = [{"id": "fade"}]
    else:
        frames = [-1]
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        scene_answers.frames_at(payload, frames)
