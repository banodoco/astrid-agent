from __future__ import annotations

import copy
import math
import shutil
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest

from astrid.core.timeline import audio_curves
from astrid.core.timeline.audio_envelope import (
    AudioEnvelopeError,
    compile_duck,
    compile_legacy,
    evaluate,
    ffmpeg_expression,
    keyframes,
    validate,
)


@pytest.mark.parametrize('curve', audio_curves.names())
def test_registry_segments_and_equal_power_pair(curve):
    incoming = keyframes([(0, 0), (1, 1)], curve)
    outgoing = keyframes([(0, 1), (1, 0)], curve)
    for sample in range(1001):
        t = sample / 1000
        assert evaluate(incoming, t) == pytest.approx(audio_curves.gain(curve, t))
        assert evaluate(outgoing, t) == pytest.approx(audio_curves.gain(curve, t, fade_in=False))
        if curve == 'equal-power':
            assert evaluate(incoming, t) ** 2 + evaluate(outgoing, t) ** 2 == pytest.approx(1)
    assert evaluate(incoming, -1) == 0
    assert evaluate(outgoing, 2) == 0


def test_legacy_rounding_overlapping_fades_and_projection_once():
    clip = {'from': 2, 'to': 3, 'speed': 1, 'params': {'fadeIn': .73, 'fadeOut': .83}}
    envelope = compile_legacy(clip, 30)
    for sample in range(48001):
        t = sample / 48000
        expected = min(1, t / (22 / 30)) * min(1, (1 - t) / (25 / 30))
        assert evaluate(envelope, t) == pytest.approx(expected, abs=1e-12)
    clip['audio'] = {'envelope': envelope}
    assert compile_legacy(clip, 30) == envelope
    assert compile_legacy({'to': 1, 'params': {'fadeIn': .001}}, 30) is None


@pytest.mark.parametrize('bad', [
    {'keyframes': [{'at': 0, 'value': 1, 'curve': 'typo'}]},
    {'keyframes': [{'at': 1, 'value': 0}, {'at': 0, 'value': 1}]},
    {'keyframes': [{'at': 0, 'value': -1}]},
    {'keyframes': [{'at': math.nan, 'value': 1}]},
    {'keyframes': []},
])
def test_invalid_plan_refuses_named_code(bad):
    with pytest.raises(AudioEnvelopeError, match='ENVELOPE_UNRENDERED'):
        validate(bad)


def test_unsupported_host_refuses_named_codes():
    with pytest.raises(AudioEnvelopeError, match='ENVELOPE_UNRENDERED'):
        validate(keyframes([(0, 1)]), host='frame-volume')
    with pytest.raises(AudioEnvelopeError, match='DUCK_UNRENDERED'):
        validate(compile_duck([], by_db=-10, attack=.2, release=.4), host='frame-volume')


def test_ducks_overlap_by_strongest_and_reflow_uses_existing_authored_words():
    from astrid.sdk.timeline.audio_envelope import compile_clip_audio
    from astrid.sdk.timeline_checkout import Checkout
    from tests.sdk.test_timeline_reflow_golden import film
    tl = Checkout(film())
    bed = tl.clip('music')
    bed.tag(duck={'under': 'voice', 'by': '-10dB', 'attack': '6f', 'release': '12f'})
    before_document = copy.deepcopy(tl.bundle)
    before_words = list(tl.words())
    before = compile_clip_audio(bed)['envelope']
    assert tl.bundle == before_document  # output is transient
    tl.remove_line('s2')  # real compiler/reflow; following narration changes time
    after_words = list(tl.words())
    old = next(w for w in before_words if w.text == 'work')
    new = next(w for w in after_words if w.id == old.id)
    assert new.start != old.start
    segment = next(c for c in tl.clips() if c.track == 'music' and c.start < new.start < c.end)
    after = compile_clip_audio(segment)['envelope']
    for sample in range(1, 1000):
        offset = (old.end - old.start) * sample / 1000
        assert abs(20 * math.log10(evaluate(before, old.start + offset) / evaluate(after, new.start + offset - segment.start))) < 1e-9
        for error in (-1, 1):
            measured = evaluate(after, new.start + offset - segment.start) * 10 ** (error / 20)
            assert not abs(20 * math.log10(measured / evaluate(before, old.start + offset))) < 1 - 1e-9
    weak = compile_duck([SimpleNamespace(start=1, end=2)], by_db=-6, attack=.2, release=.4)
    strong = compile_duck([SimpleNamespace(start=1.5, end=2.5)], by_db=-12, attack=.2, release=.4)
    merged = {'keyframes': [{'at': 0, 'value': 1}], 'minimum': [weak, strong]}
    assert evaluate(merged, 1.6) == pytest.approx(10 ** (-12 / 20))


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='FFmpeg required')
@pytest.mark.parametrize('curve', audio_curves.names())
def test_ffmpeg_sample_filter_matches_plan_across_seam_and_pair(curve):
    envelopes = [keyframes([(0, 0), (.25, 1), (.75, 1), (1, 0)], curve),
                 keyframes([(0, 1), (.5, 1), (1, 0)], curve),
                 keyframes([(0, 0), (.5, 0), (1, 1)], curve)]
    for envelope in envelopes:
        result = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'aevalsrc=1:s=48000:d=1',
                                 '-af', f"aeval=exprs='val(ch)*({ffmpeg_expression(envelope)})':c=same",
                                 '-c:a', 'pcm_f32le', '-f', 'f32le', '-'], capture_output=True, check=True)
        actual = np.frombuffer(result.stdout, dtype='<f4')
        expected = np.array([evaluate(envelope, sample / 48000) for sample in range(48000)])
        assert len(actual) == 48000
        assert np.max(np.abs(actual - expected)) < 1e-7


@pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='FFmpeg required')
def test_compiled_graph_mixes_paired_clips_preserves_base_gain_and_sample_count(tmp_path):
    from astrid.packs.rendering.rendering.renderers.ffmpeg.command import (
        RenderCommandInputs,
        build_filter_graph,
    )
    from astrid.packs.rendering.rendering.renderers.ffmpeg.support import structural_reasons
    timeline = {'theme': 'banodoco-default',
                'tracks': [{'id': 'v', 'kind': 'visual'}, {'id': 'a', 'kind': 'audio', 'volume': .5}],
                'theme_overrides': {'visual': {'canvas': {'width': 16, 'height': 16, 'fps': 30}}},
                'clips': [{'id': 'v', 'track': 'v', 'clipType': 'media', 'at': 0, 'from': 0, 'to': 1, 'asset': 'v', 'volume': 0}]}
    for id, at, start, end, points in [
        ('out', 0, .25, 1, [(0, 1), (.5, 1), (.75, 0)]),
        ('in', .5, .5, 1, [(0, 0), (.25, 1)]),
    ]:
        timeline['clips'].append({'id': id, 'track': 'a', 'clipType': 'media', 'asset': 'a',
                                  'at': at, 'from': start, 'to': end, 'volume': .4,
                                  'params': {'fadeIn': .6},  # must not be reapplied
                                  'audio': {'envelope': keyframes(points, 'equal-power')}})
    assert structural_reasons(timeline) == []
    inputs = RenderCommandInputs(tmp_path / 'timeline.json', tmp_path / 'assets.json', tmp_path / 'out.wav',
                                 timeline, {'assets': {'v': {'file': 'v.mp4'}, 'a': {'file': 'a.wav'}}})
    graph, _ = build_filter_graph(inputs)
    audio = ';'.join(f for f in graph if 'aeval=exprs' in f or '[aout]' in f)
    assert 'atrim=end_sample=48000' in audio
    encoded = subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=16x16:d=1:r=30',
                              '-f', 'lavfi', '-i', 'aevalsrc=0.25|0.25:s=48000:d=2:c=stereo',
                              '-filter_complex', audio, '-map', '[aout]', '-c:a', 'pcm_f32le', '-f', 'f32le', '-'],
                             capture_output=True, check=True)
    actual = np.frombuffer(encoded.stdout, dtype='<f4').reshape(-1, 2)
    assert len(actual) == 48000
    for sample in (0, 23999, 24000, 30000, 35999, 36000, 47999):
        t = sample / 48000
        expected = .05 * sum(evaluate(clip['audio']['envelope'], t - clip['at'])
                             for clip in timeline['clips'][1:] if clip['at'] <= t < clip['at'] + clip['to'] - clip['from'])
        assert actual[sample, 0] == pytest.approx(expected, abs=1e-8)
        assert actual[sample, 1] == pytest.approx(expected, abs=1e-8)
