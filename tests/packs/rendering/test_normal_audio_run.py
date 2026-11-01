"""Real normal props and native PCM preparation, with socket-free consumption."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import struct
import subprocess
import wave
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from astrid.core.rendering.contracts import (
    SCHEMA_VERSION,
    FrameWindow,
    RenderProfile,
    RenderRequest,
)
from astrid.core.rendering.service import RenderService
from astrid.core.timeline.duration import clip_end_frame, clip_start_frame
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion
from astrid.packs.rendering.rendering.renderers.remotion.normal_audio import (
    prepare_normal_audio_sources,
)


def _profile(fps_rational=(30000, 1001)):
    return RenderProfile(width=320, height=180, fps_rational=fps_rational,
                         time_base=(1, 90000), video_codec="h264", pixel_format="yuv420p")


@pytest.mark.parametrize("audio", [False, True])
def test_direct_preparation_requires_server_only_for_audio(tmp_path, audio):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    props = {"timeline": {"tracks": [{"id": "A1", "kind": "audio"}],
                          "clips": [{"id": "sound", "track": "A1"}] if audio else []},
             "assets": {"assets": {}}}
    before = copy.deepcopy(props)
    kwargs = dict(materializer=SimpleNamespace(resolved_registry=lambda server: props["assets"]),
                  asset_server=None, attempt_directory=attempt, project_dir=tmp_path,
                  runtime_tools=None, fps=30000 / 1001)
    if audio:
        with pytest.raises(ValueError, match="managed asset server"):
            prepare_normal_audio_sources(props, **kwargs)
    else:
        output = prepare_normal_audio_sources(props, **kwargs)
        assert output == props
        output["timeline"]["tracks"][0]["id"] = "detached"
    assert props == before
    assert list(attempt.iterdir()) == []


def _timeline():
    return {
        "theme": "banodoco-default",
        "tracks": [{"id": "A1", "kind": "audio", "label": "Audio", "volume": 0.5},
                   {"id": "V1", "kind": "visual", "label": "Visual"}, {"id": "other", "kind": "audio", "label": "Other"}],
        "clips": [{
            "id": "sound", "track": "A1", "asset": "sound", "at": 0.016682,
            "from": 0.051, "to": 3.051, "speed": 1.5, "volume": 3,
            "params": {"fadeIn": 0.2, "fadeOut": 0.2, "fadeInCurve": "equal-power"},
            "app": {"astrid_slice_origin": {"at": 99}, "opaque": [1, {"keep": True}]},
        }, {
            "id": "overlap", "track": "A1", "asset": "sound", "at": 0.2,
            "from": 0, "to": 3, "volume": 0,
            "params": {"fadeIn": 0.1},
        }, {
            "id": "sprite", "track": "V1", "clipType": "am-sprite", "at": 0,
            "hold": 4, "opacity": 0.4, "params": {"keyframes": [{"frame": 0, "x": 1, "y": 0}]},
        }, {"id": "excluded", "track": "other", "asset": "sound", "at": 0, "from": 0, "to": 9}],
        "metadata": {"opaque": {"original": [1, 2]}},
    }


def _layer_request(tmp_path, timeline, fps_rational):
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(timeline))
    window = FrameWindow(15, 45, fps_rational)
    request = RenderRequest(SCHEMA_VERSION, str(timeline_path), "out.mp4", window=window)
    segment = SimpleNamespace(window=window, layer=SimpleNamespace(tracks=("A1", "V1"), z=0))
    candidate = SimpleNamespace(id="fixture", manifest=SimpleNamespace(capabilities={"supports_windows": True}))
    adapted, sidecar = RenderService._segment_request(
        object.__new__(RenderService), request, candidate=candidate, segment=segment,
        index=0, workspace=tmp_path,
    )
    return request, adapted, sidecar


@pytest.fixture
def normal_profile(request):
    return _profile(getattr(request, "param", (30000, 1001)))


@pytest.fixture
def normal_scene(request):
    if getattr(request, "param", None) != "compiled":
        return _timeline()

    from astrid.core.timeline import authoring_bundle as compiler
    from astrid.core.timeline import shot_composition_projection as projector
    from astrid.packs.rendering.actions.render.managed_timeline import _render_compatible_projection
    from tests.timeline.test_authoring_bundle import _closure

    parent, shots, internals = _closure(shared=False)
    bundle = compiler.open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)
    placement = bundle["placements"][0]
    placement.update(duration_ms=1200, track="V1", gain=0.5)
    placement["placement"]["track"] = "V1"
    child = bundle["shots"][placement["shot_id"]]["internal_timeline"]
    child["tracks"] = [_timeline()["tracks"][0]]
    child["clips"] = copy.deepcopy(_timeline()["clips"][:2])
    for clip in child["clips"]:
        clip["asset"] = "voice"
    bundle["parent"]["config"]["tracks"] = [_timeline()["tracks"][1]]
    bundle["parent"]["config"]["clips"][0]["track"] = "V1"
    bundle["parent"]["clips"][0]["track"] = "V1"
    before = copy.deepcopy(bundle)
    with patch.object(compiler, "compile_authoring_candidate", wraps=compiler.compile_authoring_candidate) as compile_call:
        publication = compiler.compile_authoring_candidate(bundle).publication
        assert compile_call.call_count == 1
    with patch.object(projector, "project_runtime_parent_composition", wraps=projector.project_runtime_parent_composition) as project_call:
        projected = projector.project_runtime_parent_composition(
            {"project_id": publication["project_id"], "timeline_id": publication["timeline_id"],
             "revision_id": publication["parent_revision_id"], "payload": publication["parent_composition"]},
            shot_revisions=publication["shot_revisions"],
            internal_timeline_revisions=publication["internal_timeline_revisions"],
        )
        assert project_call.call_count == 1
    assert bundle == before
    config, _ = _render_compatible_projection(projected.config, projected.registry)
    return config


@pytest.fixture(params=["full", "window", "layer"])
def normal_run(tmp_path, monkeypatch, request, normal_profile, normal_scene):
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    monkeypatch.delenv("ASTRID_PROGRESS_PATH", raising=False)
    monkeypatch.setenv("ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES", "0")
    original_request, layer_request, _ = _layer_request(tmp_path, normal_scene, normal_profile.fps_rational)
    render_request = layer_request if request.param == "layer" else original_request
    if request.param == "full":
        render_request = replace(render_request, window=None)
    timeline_path = Path(render_request.timeline_path)
    original = timeline_path.read_bytes()
    audio = io.BytesIO()
    with wave.open(audio, "wb") as writer:
        writer.setparams((2, 2, 30000, 0, "NONE", "not compressed"))
        writer.writeframes(struct.pack("<hh", 8192, -4096) * 90000)
    payload = audio.getvalue()
    registry = tmp_path / "assets.json"
    registry.write_text(json.dumps({"assets": {"sound": {
        "object_id": "object-sound", "digest": hashlib.sha256(payload).hexdigest(), "type": "audio/wav",
    }}}))
    registry_data = json.loads(registry.read_text())
    for clip in normal_scene["clips"]:
        if clip.get("track") == "A1":
            registry_data["assets"][clip["asset"]] = copy.deepcopy(registry_data["assets"]["sound"])
    registry.write_text(json.dumps(registry_data))
    project = Path(os.environ["ASTRID_REMOTION_PROJECT_DIR"])
    runtime = SimpleNamespace(node_executable=Path(os.environ["ASTRID_NODE_EXECUTABLE"]),
                              remotion_cli=project / "unused-render-cli", node_version="v20.19.4")
    for name in ("_regenerate_element_registries", "_validate_renderer_effect_registry"):
        monkeypatch.setattr(remotion, name, lambda *a: None)
    monkeypatch.setattr(remotion, "_validate_project_dir", lambda *a: runtime)
    monkeypatch.setattr(remotion, "_effective_registry_state", lambda *a: {"hash": "fixture"})
    monkeypatch.setattr(remotion, "_canonical_profile", lambda *a: normal_profile)
    monkeypatch.setattr(remotion, "_resolved_theme_for_render", lambda *a: {"id": "banodoco-default"})
    monkeypatch.setattr(remotion, "_stage_effect_assets_for_timeline", lambda *a, **k: {"effects": []})
    monkeypatch.setattr(remotion, "_available_remotion_port", lambda: 32101)
    state = SimpleNamespace(paths=[], events=[], props=None, consume=lambda: None, expected_source_count=2,
                            materialized_objects={"object-sound": payload})

    class ContainedServer:
        def __init__(self, root, **kwargs):
            self.root = root
            self.alive = False
            state.server = self

        def __enter__(self):
            self.alive = True
            state.events.append("server-enter")
            return self

        def local_url(self, path):
            assert self.alive
            return "http://attempt.invalid/" + Path(path).resolve(strict=True).relative_to(self.root).as_posix()

        def __exit__(self, *args):
            state.events.append("server-exit")
            # WAV lifetime belongs to the attempt and ends before the server.
            assert all(not path.exists() for path in state.paths)
            self.alive = False

    monkeypatch.setattr(remotion, "InvocationAssetServer", ContainedServer)
    real_subprocess = subprocess.run

    def command(argv, **kwargs):
        if str(runtime.remotion_cli) not in argv:
            return real_subprocess(argv, **kwargs)
        state.events.append("consume")
        assert state.server.alive
        assert ("--frames=15-44" in argv) == (render_request.window is not None)
        props_path = Path(argv[argv.index("--props") + 1])
        state.props_path = props_path
        state.props = json.loads(props_path.read_text())
        for clip in state.props["timeline"]["clips"]:
            source = clip.get("audio", {}).get("render_source")
            if source:
                path = state.server.root / source["file"].removeprefix("http://attempt.invalid/")
                assert path.is_file()
                state.paths.append(path)
        assert len(state.paths) == state.expected_source_count
        assert timeline_path.read_bytes() == original
        state.consume()
        Path(argv[argv.index("--output") + 1]).write_bytes(b"test consumer output, not a render")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(remotion.subprocess, "run", command)

    def execute(composition_id="TimelineComposition"):
        return remotion._execute_remotion_locked(
            timeline_path, registry, tmp_path / "out.mp4", provenance_out_path=tmp_path / "out.mp4",
            project_dir=project, composition_id=composition_id, theme_path=None,
            min_free_gb=None, materialized_objects=state.materialized_objects, staging_parent=tmp_path,
            window=render_request.window,
            composition_duration_frames=remotion._composition_duration_from_request(render_request),
        )

    state.execute = execute
    state.timeline_path = timeline_path
    state.registry = registry
    state.profile = normal_profile
    state.render_request = render_request
    yield state
    assert timeline_path.read_bytes() == original
    assert not list(tmp_path.glob("astrid-render-assets-*"))
    assert not list(tmp_path.glob(".remotion-runtime-*"))
    assert not (tmp_path / ".remotion-props.json").exists()


@pytest.mark.parametrize("normal_profile", [(30, 1), (30000, 1001)], indirect=True)
@pytest.mark.parametrize("normal_scene", ["compiled"], indirect=True)
def test_normal_serialized_root_clock_and_single_scalar_gain(normal_run):
    state = normal_run
    serialized = remotion._serialize_timeline(state.timeline_path)
    before = copy.deepcopy(serialized)
    expected = next(clip for clip in serialized["clips"] if clip["id"] == "occ-1:sound")
    fps = state.profile.fps_rational[0] / state.profile.fps_rational[1]
    assert expected["at"] == 0.017
    assert clip_start_frame(expected, fps) == 1
    assert expected["to"] == pytest.approx(1.825977)
    assert expected["volume"] == 1.5  # canonical occurrence gain, applied once
    assert clip_end_frame(expected, fps) - clip_start_frame(expected, fps) == 35

    def consume():
        assert "compiledTransitions" not in state.props
        if "astrid_composition_duration_frames" in state.render_request.metadata:
            assert state.props["astridCompositionDurationInFrames"] == int(
                state.render_request.metadata["astrid_composition_duration_frames"])
        assert state.props["timeline"]["tracks"] == serialized["tracks"]
        for original, prepared in zip(serialized["clips"], state.props["timeline"]["clips"], strict=True):
            actual = copy.deepcopy(prepared)
            if "render_source" in actual.get("audio", {}):
                actual.pop("audio")
            assert actual == original
        actual = next(clip for clip in state.props["timeline"]["clips"] if clip["id"] == expected["id"])
        source = actual["audio"]["render_source"]
        assert source["source_from"] == 2 / fps
        assert source["playback_rate"] == 1.5
        path = state.server.root / source["file"].removeprefix("http://attempt.invalid/")
        wav = path.read_bytes()
        assert struct.unpack_from("<HHI", wav, 20) == (3, 2, 30000)
        assert len(wav) == 44 + 90000 * 8
        assert struct.unpack_from("<ff", wav, 44 + 30000 * 8) == (0.25, -0.125)
        assert struct.unpack_from("<ff", wav, 44 + 70000 * 8) == (0, 0)
        # Public primitives independently consume the final serialized original.
        reference = subprocess.run(
            [os.environ["ASTRID_NODE_EXECUTABLE"], "--input-type=module", "-e", """
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {applyAudioEnvelope, audioEnvelopeWav, compileAudioEnvelope} from '@banodoco/timeline-composition/evaluation';
import {compiledAudioSourceClock} from './compiled-audio-source-clock.mjs';
const {clip, fps, path, source} = JSON.parse(readFileSync(0, 'utf8'));
const clock = compiledAudioSourceClock(clip, fps);
const envelope = compileAudioEnvelope(clip, fps);
assert.equal(source.source_from, clock.source_from);
assert.equal(source.playback_rate, clock.playback_rate);
assert.deepEqual(JSON.parse(source.envelope_key), envelope);
const channels = [new Float32Array(90000).fill(0.25), new Float32Array(90000).fill(-0.125)];
applyAudioEnvelope(channels, 30000, envelope, clock.source_from, clock.playback_rate);
assert.deepEqual(readFileSync(path), Buffer.from(audioEnvelopeWav(channels, 30000)));
"""],
            input=json.dumps({"clip": expected, "fps": fps, "path": str(path), "source": source}),
            cwd=os.environ["ASTRID_REMOTION_PROJECT_DIR"], capture_output=True, text=True, check=False,
        )
        assert reference.returncode == 0, reference.stderr
        assert next(track for track in state.props["timeline"]["tracks"] if track["id"] == "A1")["volume"] == 0.5
        overlap = next(clip for clip in state.props["timeline"]["clips"] if clip["id"] == "occ-1:overlap")
        assert overlap["volume"] == 0
        assert struct.unpack_from("<ff", state.paths[1].read_bytes(), 44 + 15000 * 8) == (0.25, -0.125)

    state.consume = consume
    state.execute()
    assert serialized == before
    assert state.events == ["server-enter", "consume", "server-exit"]
    assert all(not path.exists() for path in state.paths)


@pytest.mark.parametrize("error", [RuntimeError("consumer failed"), KeyboardInterrupt(), subprocess.TimeoutExpired("consumer", 1)])
def test_normal_consumer_failure_cancel_and_timeout_release_attempt(normal_run, error):
    def fail():
        raise error

    normal_run.consume = fail
    with pytest.raises(type(error)):
        normal_run.execute()
    assert normal_run.events == ["server-enter", "consume", "server-exit"]
    assert all(not path.exists() for path in normal_run.paths)


def test_normal_pcm_error_never_launches_consumer(normal_run):
    raw = json.loads(normal_run.timeline_path.read_text())
    raw["clips"][1]["params"]["fadeInCurve"] = "unknown"
    original = normal_run.timeline_path.read_bytes()
    try:
        normal_run.timeline_path.write_text(json.dumps(raw))
        with pytest.raises(RuntimeError, match="ENVELOPE_UNRENDERED"):
            normal_run.execute()
    finally:
        normal_run.timeline_path.write_bytes(original)
    assert normal_run.events == ["server-enter", "server-exit"]


def test_normal_source_digest_error_never_prepares_or_launches(normal_run):
    original = normal_run.registry.read_bytes()
    raw = json.loads(original)
    raw["assets"]["sound"]["digest"] = "0" * 64
    try:
        normal_run.registry.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="integrity check"):
            normal_run.execute()
    finally:
        normal_run.registry.write_bytes(original)
    assert normal_run.events == []


def test_normal_native_decode_error_releases_attempt_before_consumer(normal_run):
    original = normal_run.registry.read_bytes()
    raw = json.loads(original)
    corrupt = b"managed bytes with no audio stream"
    raw["assets"]["sound"]["digest"] = hashlib.sha256(corrupt).hexdigest()
    normal_run.materialized_objects["object-sound"] = corrupt
    try:
        normal_run.registry.write_text(json.dumps(raw))
        with pytest.raises(RuntimeError, match="ffprobe"):
            normal_run.execute()
    finally:
        normal_run.registry.write_bytes(original)
    assert normal_run.events == ["server-enter", "server-exit"]


def test_shared_threejs_executor_keeps_its_existing_source_path(normal_run, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Three.js sliced source ownership is outside normal PCM activation")

    monkeypatch.setattr(remotion, "prepare_normal_audio_sources", unexpected)
    normal_run.expected_source_count = 0
    normal_run.execute(composition_id="ThreeTimelineComposition")
    assert normal_run.events == ["server-enter", "consume", "server-exit"]


@pytest.mark.parametrize("failure", [False, True])
def test_read_only_root_context_holds_normal_pcm_until_query_exits(normal_run, tmp_path, failure):
    state = normal_run
    output = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError, match="query failed") if failure else nullcontext():
        with remotion._prepared_remotion_root(
            state.timeline_path, state.registry, output,
            provenance_out_path=output,
            project_dir=Path(os.environ["ASTRID_REMOTION_PROJECT_DIR"]),
            composition_id="TimelineComposition", theme_path=None, min_free_gb=None,
            materialized_objects=state.materialized_objects, staging_parent=tmp_path,
            window=state.render_request.window,
            composition_duration_frames=remotion._composition_duration_from_request(state.render_request),
        ) as prepared:
            props, props_path = prepared[:2]
            assert state.server.alive and state.events == ["server-enter"]
            assert json.loads(props_path.read_text()) == props
            for clip in props["timeline"]["clips"]:
                source = clip.get("audio", {}).get("render_source")
                if source:
                    path = state.server.root / source["file"].removeprefix("http://attempt.invalid/")
                    assert path.is_file() and path.read_bytes().startswith(b"RIFF")
                    state.paths.append(path)
            assert len(state.paths) == state.expected_source_count
            if failure:
                raise RuntimeError("query failed")
    assert state.events == ["server-enter", "server-exit"]
    assert not output.exists() and not props_path.exists()
    assert all(not path.exists() for path in state.paths)
