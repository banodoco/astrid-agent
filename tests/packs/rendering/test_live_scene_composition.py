"""Pinned scene windows composed with ordinary timeline audio and overlays."""
import array
import hashlib
import json
import math
import subprocess
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from astrid.core.rendering.contracts import FrameWindow, LayerRef, RenderRequest, RenderSegment
from astrid.core.rendering.errors import RendererUnsupportedError
from astrid.core.rendering.service import RenderService
from astrid.packs.rendering.rendering.renderers.threejs import run as threejs
from tests.core.rendering.test_service import _candidate, _renderer_resolution
from tests.packs.rendering._helpers import _execution_env, _probe
from tests.packs.rendering.test_live_scene_boundary import _package

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('z', [None, 0, 1])
def test_service_layered_plan_native_renderer_isolates_and_adjusts_once(tmp_path, z):
    from astrid.core.rendering.registry import FinalizerRegistry, PlannerRegistry, RendererRegistry
    from tests.core.rendering.test_service import _plan, _TimelineCaptureTransport
    from tests.core.rendering.test_service import _request as service_request

    package = _package()
    config = _composition(package)
    config['theme_overrides']['visual']['canvas']['fps'] = 10
    config['clips'][0]['hold'] = 20
    path = tmp_path / 'scene-input.json'
    path.write_text(json.dumps(config))
    class WindowCaptureTransport(_TimelineCaptureTransport):
        def run(self, verb, *args, **kwargs):
            result = super().run(verb, *args, **kwargs)
            if verb == 'finalize':
                return replace(result, video=replace(result.video, duration_frames=10))
            return result

    transport = WindowCaptureTransport()
    plan = _plan('fixture.native')
    window = FrameWindow(start_frame=60, end_frame=70, fps_rational=(10, 1))
    layer = LayerRef(z=z, tracks=('scene',)) if z is not None else None
    transport.plan = replace(plan, requested_policy='fixture.planner', total_frames=70, window=window, segments=[replace(plan.segments[0], window=window, layer=layer)])
    service = RenderService(
        registries=(RendererRegistry([_candidate(tmp_path, 'fixture.native', 'renderer', capabilities={'supports_windows': True, 'supports_full_timeline': True})]), PlannerRegistry([_candidate(tmp_path, 'fixture.planner', 'planner')]), FinalizerRegistry([_candidate(tmp_path, 'rendering.ffmpeg-finalizer', 'finalizer')])),
        transport=transport, validator=lambda result, **kwargs: result,
    )
    service.render_request(replace(service_request(tmp_path), timeline_path=str(path)), selector='fixture.planner', out_path=tmp_path / 'output.mp4')
    assert len(transport.received_timelines) == 1
    materialized = transport.received_timelines[0]
    request = next(payload for verb, backend, payload in transport.payloads if verb == 'render')
    if z is None:
        assert materialized == config
        assert request['window'] == window.to_dict()
        native = threejs._window_timeline(materialized, _request(path, window=request['window']))
        assert (native['clips'][0]['from'], native['clips'][0]['to']) == (63, 65)
        return
    assert [track['id'] for track in materialized['tracks']] == ['scene']
    assert len(materialized['clips']) == 1
    clip = materialized['clips'][0]
    assert (clip['at'], clip['from'], clip['to'], clip['speed'], clip['hold']) == (0, 63, 65, 2, 2)
    assert clip['app']['liveScene'] == package
    assert materialized['metadata']['astrid_layer'] == {'z': z, 'alpha': z > 0}
    assert request['window'] is None


def test_scene_canvas_uses_existing_shared_default():
    from astrid.core.rendering.profile import resolve_render_profile

    config = _composition(_package())
    del config['theme_overrides']
    profile = resolve_render_profile(config)
    assert threejs._canvas(config) == (profile.width, profile.height, profile.fps_rational[0])
    assert threejs._support_reasons(config, {'assets': {'pulse': {'object_id': 'pulse-object'}}}) == []


def test_scene_window_uses_existing_pinned_theme_canvas():
    config = _composition(_package())
    del config['theme_overrides']
    theme = {'visual': {'canvas': {'width': 640, 'height': 360, 'fps': 24}}}
    assert threejs._canvas(config, theme=theme) == (640, 360, 24)
    request = _request('fixture.json', window=FrameWindow(start_frame=144, end_frame=168, fps_rational=(24, 1)).to_dict())
    clip = threejs._window_timeline(config, request, theme=theme)['clips'][0]
    assert (clip['from'], clip['to']) == (63, 65)


def _composition(package):
    return {
        'theme': 'banodoco-default',
        'theme_overrides': {'visual': {'canvas': {'width': 320, 'height': 180, 'fps': 30}}},
        'tracks': [{'id': 'overlay', 'kind': 'visual', 'label': 'Overlay'}, {'id': 'scene', 'kind': 'visual', 'label': 'Scene'}, {'id': 'audio', 'kind': 'audio', 'label': 'Audio'}],
        'clips': [
            {'id': 'scene', 'track': 'scene', 'clipType': 'haus.astrid.liveScene', 'at': 2, 'from': 55, 'to': 75, 'speed': 2, 'app': {'liveScene': package, 'sentinel': 'retained'}},
            {'id': 'label', 'track': 'overlay', 'clipType': 'text', 'at': 6, 'hold': 1, 'text': {'content': 'L3 OVERLAY', 'fontSize': 24, 'color': '#ffff00'}, 'params': {'anchor': 'top'}},
            {'id': 'pulse', 'track': 'audio', 'clipType': 'media', 'at': 6.2, 'from': 1, 'to': 2.2, 'speed': 2, 'asset': 'pulse', 'volume': 0.8},
        ],
    }


def _request(path, assets=None, **kwargs):
    return RenderRequest.from_dict({
        'schema_version': 1, 'timeline_path': str(path),
        'assets_registry_path': str(assets) if assets else None,
        'output_name': 'scene.mp4',
        'window': FrameWindow(start_frame=180, end_frame=210, fps_rational=(30, 1)).to_dict(),
        **kwargs,
    })


def test_window_retains_revision_and_ordinary_audio_trim_rate_overlay():
    package = _package()
    config = _composition(package)
    request = _request('fixture.json')
    sliced = threejs._window_timeline(config, request)
    scene, overlay, audio = sliced['clips']
    assert (scene['at'], scene['from'], scene['to'], scene['speed']) == (0, 63, 65, 2)
    assert scene['app']['liveScene'] is package
    assert scene['app']['sentinel'] == 'retained'
    assert overlay['at'] == 0 and overlay['hold'] == 1
    assert audio['at'] == pytest.approx(0.2)
    assert (audio['from'], audio['to'], audio['speed']) == (1, 2.2, 2)
    assert sliced['metadata']['duration_seconds'] == 1
    assert config['clips'][0]['from'] == 55
    assert threejs._support_reasons(sliced, {'assets': {'pulse': {'object_id': 'pulse-object'}}}) == []


def test_window_entering_audio_advances_its_source_and_preserves_shared_scene():
    package = _package()
    config = _composition(package)
    config['clips'].append({**config['clips'][0], 'id': 'second', 'at': 22, 'from': 20, 'to': 30})
    sliced = threejs._window_timeline(config, _request('fixture.json', window=FrameWindow(start_frame=192, end_frame=222, fps_rational=(30, 1)).to_dict()))
    scene, overlay, audio = sliced['clips']
    assert (scene['from'], scene['to']) == pytest.approx((63.8, 65.8))
    assert overlay['hold'] == pytest.approx(.6)
    assert audio['at'] == 0 and audio['from'] == pytest.approx(1.4)
    assert audio['to'] == 2.2
    second = threejs._window_timeline(config, _request('fixture.json', window=FrameWindow(start_frame=690, end_frame=720, fps_rational=(30, 1)).to_dict()))['clips'][0]
    assert second['from'] == 22 and second['to'] == 24
    assert second['app']['liveScene'] is package
    assert scene['app']['liveScene'] is package


@pytest.mark.parametrize('window', [
    FrameWindow(start_frame=180, end_frame=210, fps_rational=(24, 1)),
    FrameWindow(start_frame=180, end_frame=210, fps_rational=(30, 1), speed=2),
    FrameWindow(start_frame=180, end_frame=210, fps_rational=(30, 1), source_range=(10, 40)),
])
def test_window_rejects_other_clocks_and_resampling(window):
    with pytest.raises(ValueError):
        threejs._window_timeline(_composition(_package()), _request('fixture.json', window=window.to_dict()))


def test_missing_audio_and_corrupt_revision_fail_before_capture(tmp_path, monkeypatch):
    config = _composition(_package())
    assert any('audio asset is missing' in r for r in threejs._support_reasons(config))
    config['clips'][0]['app']['liveScene']['html'] += 'corrupt'
    path = tmp_path / 'timeline.json'
    path.write_text(json.dumps(config))
    monkeypatch.setattr(threejs, '_execute_remotion', lambda *a, **kw: pytest.fail('invalid scene reached capture'))
    with pytest.raises(RendererUnsupportedError, match='does not support'):
        threejs._protocol_render(_request(path), workspace=tmp_path)


@pytest.mark.parametrize('z', [0, 1])
def test_native_window_layer_is_materialized_once_with_pin_hold_and_alpha(tmp_path, z):
    package = _package()
    config = _composition(package)
    config['clips'][0]['hold'] = 20
    path = tmp_path / 'timeline.json'
    path.write_text(json.dumps(config))
    candidate = _candidate(tmp_path, 'fixture.native', 'renderer', capabilities={'supports_windows': True, 'supports_full_timeline': True})
    request = _request(path)
    segment = RenderSegment(window=request.window, renderer=_renderer_resolution(candidate.id), input_hashes={}, layer=LayerRef(z=z, tracks=('scene',)))
    service = RenderService()
    adapted, hashes = service._segment_request(request, candidate=candidate, segment=segment, index=0, workspace=tmp_path)
    assert adapted.window is None
    materialized = json.loads(Path(adapted.timeline_path).read_text())
    assert [track['id'] for track in materialized['tracks']] == ['scene']
    assert len(materialized['clips']) == 1  # Audio/overlay belong to other tracks.
    scene = materialized['clips'][0]
    assert scene['app']['liveScene'] == package
    assert (scene['at'], scene['from'], scene['to'], scene['speed'], scene['hold']) == (0, 63, 65, 2, 2)
    assert materialized['metadata']['astrid_layer'] == {'z': z, 'alpha': z > 0}
    assert hashes['materialized_timeline'] == hashlib.sha256(Path(adapted.timeline_path).read_bytes()).hexdigest()
    assert threejs._window_timeline(materialized, adapted) is materialized
    assert threejs._support_reasons(materialized) == []
    assert json.loads(path.read_text())['clips'][0]['from'] == 55


def test_unlayered_native_window_passes_through_unchanged(tmp_path):
    candidate = _candidate(tmp_path, 'fixture.native', 'renderer', capabilities={'supports_windows': True})
    request = _request(tmp_path / 'not-read.json')
    segment = RenderSegment(window=request.window, renderer=_renderer_resolution(candidate.id), input_hashes={})
    adapted, hashes = RenderService()._segment_request(request, candidate=candidate, segment=segment, index=0, workspace=tmp_path)
    assert adapted is request and hashes == {}


def test_layer_materialization_retains_null_window_support_rejection(tmp_path):
    path = tmp_path / 'timeline.json'
    path.write_text(json.dumps(_composition(_package())))
    candidate = _candidate(tmp_path, 'fixture.native_only', 'renderer', operations=('render',), capabilities={'supports_windows': True, 'supports_full_timeline': False})
    request = _request(path)
    segment = RenderSegment(window=request.window, renderer=_renderer_resolution(candidate.id), input_hashes={}, layer=LayerRef(z=1, tracks=('scene',)))
    service = RenderService()
    adapted, _ = service._segment_request(request, candidate=candidate, segment=segment, index=0, workspace=tmp_path)
    report = service._support(candidate, request=adapted, workspace=tmp_path, registry=service.renderers)
    assert adapted.window is None
    assert report.supported is False
    assert any('full timelines' in reason for reason in report.reasons)


def _three_html():
    """Bundle the already-installed Three.js into a self-contained fixture."""
    script = """
import * as THREE from 'three';
let renderer, scene, camera, cube;
window.astridScene = {
  initialize() {
    renderer = new THREE.WebGLRenderer({preserveDrawingBuffer:true});
    document.body.appendChild(renderer.domElement);
    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(60, 320/180, .1, 100);
    camera.position.z = 5;
    cube = new THREE.Mesh(new THREE.BoxGeometry(), new THREE.MeshBasicMaterial({color:0x00ff00}));
    scene.add(cube);
  },
  render({sourceTime,width,height}) {
    renderer.setSize(width,height); camera.aspect=width/height; camera.updateProjectionMatrix();
    scene.background = new THREE.Color(sourceTime < 64 ? 0xff0000 : 0x0000ff);
    cube.rotation.y=sourceTime; renderer.render(scene,camera); renderer.getContext().finish();
  },
  dispose() { renderer?.dispose(); }
};
"""
    bundled = subprocess.run(
        [str(ROOT / 'remotion/node_modules/.bin/esbuild'), '--bundle', '--format=iife', '--minify'],
        input=script, text=True, capture_output=True, check=True, cwd=ROOT / 'remotion',
    ).stdout
    return '<html><head><style>body{margin:0}</style></head><body><script>' + bundled + '</script></body></html>'


def _pulse(path):
    samples = array.array('h', (int(16000 * math.sin(2 * math.pi * 440 * n / 48000)) if 1.2 <= n / 48000 < 1.6 else 0 for n in range(144000)))
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        output.writeframes(samples.tobytes())


def _rgb_frame(path, frame):
    return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-vf', f'select=eq(n\\,{frame})', '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], check=True, capture_output=True).stdout


@pytest.mark.integration
@pytest.mark.timeout(600)
def test_real_render_scene_window_audio_and_overlay(tmp_path, remotion_runtime_environment):
    package = _package(_three_html())
    timeline = tmp_path / 'timeline.json'
    timeline.write_text(json.dumps(_composition(package)))
    pulse = tmp_path / 'pulse.wav'
    _pulse(pulse)
    assets = tmp_path / 'assets.json'
    assets.write_text(json.dumps({'assets': {'pulse': {'object_id': 'pulse-object', 'digest': 'sha256:' + hashlib.sha256(pulse.read_bytes()).hexdigest(), 'media_type': 'audio/wav'}}}))
    output = tmp_path / 'scene.mp4'
    with _execution_env():
        RenderService().render(_request(timeline, assets, materialized_root=str(tmp_path), materialized_objects={'pulse-object': str(pulse)}), selector='rendering.threejs', out_path=output)
    probe = _probe(output)
    video = next(stream for stream in probe['streams'] if stream['codec_type'] == 'video')
    assert int(video['nb_read_frames']) == 30
    assert float(video['duration']) == pytest.approx(1, abs=0.002)
    before, after = _rgb_frame(output, 14), _rgb_frame(output, 15)
    # Source 63.933.. is red; source 64.000.. is blue. This catches offset,
    # rate, frame-readiness and boundary errors, beyond a moving-picture check.
    pixel = (170 * 320 + 10) * 3
    assert before[pixel] > 200 and before[pixel + 2] < 40
    assert after[pixel] < 40 and after[pixel + 2] > 200
    # Three's existing text-plane color output plus H.264 chroma subsampling
    # dims edge pixels; count visible yellow in the declared top caption area.
    assert sum(before[n] > 150 and before[n+1] > 100 and before[n+2] < 80 for n in range(0, 320 * 30 * 3, 3)) > 100
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output), '-vn', '-ac', '1', '-ar', '48000', '-f', 'f32le', '-'], check=True, capture_output=True).stdout
    pcm = array.array('f')
    pcm.frombytes(decoded)
    def rms(start, end):
        values = pcm[round(start * 48000):round(end * 48000)]
        return math.sqrt(sum(v*v for v in values) / len(values))
    levels = {'before': rms(.05, .22), 'pulse': rms(.34, .46), 'after': rms(.62, .85)}
    assert levels['before'] < .005 and levels['after'] < .005
    assert levels['pulse'] > .1, levels
    provenance = json.loads(Path(str(output) + '.provenance.json').read_text())
    assert provenance['routing']['resolved_backend'] == 'rendering.threejs'
    fragment = provenance['backend_fragments']['rendering.threejs']
    assert fragment['live_scenes'][0]['revision'] == package['revision']
    assert fragment['live_scenes'][0]['from'] == 63
    assert fragment['source_window']['start_frame'] == 180
    print(json.dumps({'output': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(), 'revision': package['revision'], 'rms': levels}))
