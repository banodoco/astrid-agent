import copy
import hashlib
import json
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from astrid.packs.rendering.actions.timeline_visualize import filmstrip_cards
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import (
    _attach_input_navigation,
    build_filmstrip_pack,
    plan_filmstrip,
)
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options


def snapshot(**changes):
    data = dict(project_slug='demo', timeline_id='main', timeline_name='A story', render_run_id='run-exact', video_digest='sha256:video', fps_rational=[24, 1], duration_frames=96, clips=[dict(id='a', kind='video', asset='one', at=0, duration=2, shot_id='beat'), dict(id='b', kind='video', asset='two', at=2, duration=2)], scripts=[dict(start=0, end=1, text='Hello'), dict(start=.5, end=1.5, text='Overlap')])
    data.update(changes)
    return data


def test_integer_fractional_fps_and_half_open_window():
    index = plan_filmstrip(snapshot(fps_rational=[30000, 1001], duration_frames=120, clips=[]), {'range': [.5, 2], 'every': .5})
    assert [c['frame'] for c in index['cards']] == [29, 44, 59]
    for c in index['cards']:
        assert Fraction(*c['time_rational']) == Fraction(c['frame'] * 1001, 30000)


def test_five_second_grid_is_strict_and_half_open():
    focused = plan_filmstrip(snapshot(fps_rational=[30, 1], duration_frames=600),
                             {'range': [0, 5], 'every': 5})
    assert [card['time_seconds'] for card in focused['cards']] == [0.0]
    full = plan_filmstrip(snapshot(fps_rational=[30, 1], duration_frames=3512),
                          {'every': 5})
    assert [card['time_seconds'] for card in full['cards'][:4]] == [0.0, 5.0, 10.0, 15.0]
    assert full['cards'][-1]['time_seconds'] == 115.0


def test_cut_neighbors_are_opt_in_between_sparse_intervals():
    strict = plan_filmstrip(snapshot(), {'every': 3})['cards']
    assert [card['frame'] for card in strict] == [0, 72]
    cards = plan_filmstrip(snapshot(), {'every': 3, 'include_cuts': True})['cards']
    frames = {c['frame']: c for c in cards}
    assert {47, 48, 95}.issubset(frames)
    assert frames[47]['clips'][0]['id'] == 'a'
    assert frames[48]['clips'][0]['id'] == 'b'
    assert 'before_cut' in frames[47]['sample_reasons']


def test_explicit_interval_does_not_inject_clip_first_or_shot_midpoint():
    cards = plan_filmstrip(snapshot(duration_frames=240), {'every': 3})['cards']
    assert all(card['sample_reasons'] == ['interval'] for card in cards)


def test_windowed_samples_include_transition_and_fade_boundary_and_interior_moments():
    clip = dict(
        id='effected', kind='video', at=0, duration=4,
        transition_projection={'status': 'resolved', 'transition': {'id': 'cross-fade'},
                               'start': [1, 1], 'end': [2, 1]},
        timed_changes=[{'kind': 'fade_in', 'start': [1, 1], 'end': [2, 1]}],
    )
    index = plan_filmstrip(snapshot(clips=[clip]), {'range': [1, 2], 'every': 2})
    frames = {card['frame']: card for card in index['cards']}

    assert {24, 35, 47}.issubset(frames)
    assert all('event_moment' in frames[frame]['sample_reasons'] for frame in (24, 35, 47))
    assert index['coverage']['event_moments']['event_count'] == 2
    assert index['coverage']['event_moments']['omitted_event_moment_count'] == 0
    assert index['coverage']['event_moments']['clipped_event_count'] == 0

    focused_clip = {**clip, 'at': 1, 'duration': 3}
    focused = plan_filmstrip(snapshot(clips=[focused_clip]),
                             {'clip': 'effected', 'range': [0.9, 2], 'every': 2})
    assert 23 in {card['frame'] for card in focused['cards']}


def test_overview_discloses_event_omissions_and_window_clipping():
    clip = dict(
        id='late-effect', kind='video', at=0, duration=4,
        timed_changes=[{'kind': 'fade_out', 'start': [1, 1], 'end': [2, 1]}],
    )
    index = plan_filmstrip(snapshot(clips=[clip]), {'max_frames': 2})
    coverage = index['coverage']['event_moments']

    assert index['sampling']['overview'] is True
    assert coverage['event_count'] == 1
    assert coverage['omitted_event_moment_count'] > 0
    assert coverage['omitted_event_count'] == 1

    clipped = plan_filmstrip(snapshot(clips=[clip]), {'range': [1.5, 2.5], 'every': 0.5})
    clipped_coverage = clipped['coverage']['event_moments']
    assert clipped_coverage['clipped_event_count'] == 1
    assert clipped_coverage['omissions'][0]['clipped'] is True

    unresolved = plan_filmstrip(snapshot(clips=[dict(
        id='unresolved', kind='video', at=0, duration=4,
        transition_projection={'status': 'unresolved', 'transition': 'cross-fade',
                               'reason': 'transition timing unavailable'},
    )]), {'range': [0, 1]})
    unresolved_event = unresolved['coverage']['event_moments']['omissions'][0]
    assert unresolved_event['status'] == 'unresolved'
    assert unresolved_event['reason'] == 'transition timing unavailable'


def test_at_remains_one_exact_frame_inside_an_effect():
    clip = dict(id='effected', kind='video', at=0, duration=4,
                timed_changes=[{'kind': 'fade_in', 'start': [1, 1], 'end': [2, 1]}])
    index = plan_filmstrip(snapshot(clips=[clip]), {'at': 1.25})

    assert [card['frame'] for card in index['cards']] == [30]
    assert index['sampling']['requested_at'] == 1.25
    assert index['sampling']['resolved_at_frame'] == 30


def test_explicit_half_second_is_not_adaptive_overview():
    options = filmstrip_options({'every': 0.5})
    index = plan_filmstrip(snapshot(duration_frames=240), options)
    assert index['sampling']['overview'] is False
    assert index['sampling']['explicit_interval'] is True
    assert [card['frame'] for card in index['cards']] == [0, 12, 24, 36, 48, 60, 72, 84, 96, 108, 120, 132, 144, 156, 168, 180, 192, 204, 216, 228]


def test_scripts_are_overlapping_segments_and_gaps_explicit():
    cards = {c['frame']: c for c in plan_filmstrip(snapshot(), {})['cards']}
    assert [s['text'] for s in cards[12]['scripts']] == ['Hello', 'Overlap']
    assert cards[24]['scripts'][0]['text'] == 'Overlap'
    assert cards[48]['scripts'] == []
    assert cards[48]['script_status'] == 'no script'


def test_frame_captions_use_only_explicit_timed_speech():
    audio = {'speech': {'phrases': [
        {'id': 'phrase-1', 'status': 'projected', 'canonical_text': 'Timed line',
         'render_interval': {'start': [0, 1], 'end': [1, 1]}},
    ]}}
    cards = {c['frame']: c for c in plan_filmstrip(
        snapshot(duration_frames=72, audio=audio), {'every_frames': 12}
    )['cards']}
    assert [caption['canonical_text'] for caption in cards[0]['captions']] == ['Timed line']
    assert [caption['canonical_text'] for caption in cards[12]['captions']] == ['Timed line']
    assert cards[24]['captions'] == []  # exact half-open end boundary
    assert cards[0]['scripts']  # shot-script context is retained separately
    assert cards[0]['caption_status'] == 'timed caption'
    assert cards[24]['caption_status'] == 'same shot; no new timed text'


def test_shot_script_display_is_once_per_occurrence_without_fake_timing():
    cards = plan_filmstrip(
        snapshot(duration_frames=96, scripts=[dict(start=0, end=4, text='Shot context')]),
        {'every_frames': 24},
    )['cards']
    assert [card['time_seconds'] for card in cards] == [0.0, 1.0, 2.0, 3.0]
    assert [script['text'] for script in cards[0]['display_scripts']] == ['Shot context']
    assert cards[1]['display_scripts'] == []
    assert cards[1]['caption_status'] == 'same shot; no new timed text'
    assert cards[0]['scripts'][0]['text'] == cards[1]['scripts'][0]['text'] == 'Shot context'
    first_lines = filmstrip_cards._static_lines(cards[0])
    later_lines = filmstrip_cards._static_lines(cards[1])
    assert any('Shot context' in line for line in first_lines)
    assert not any('Shot context' in line or 'not word-aligned' in line for line in later_lines)


def test_filters_and_bounds_are_enforced():
    cards = plan_filmstrip(snapshot(), {'clip': 'b'})['cards']
    assert all(c['time_seconds'] >= 2 for c in cards)
    with pytest.raises(ValueError, match='No clips match'):
        plan_filmstrip(snapshot(), {'shot': 'unknown'})
    with pytest.raises(ValueError, match='exceeds'):
        plan_filmstrip(snapshot(), {'every_frames': 1, 'max_frames': 10})
    with pytest.raises(ValueError, match='positive'):
        plan_filmstrip(snapshot(), {'every': 0})
    with pytest.raises(ValueError, match='positive integer'):
        plan_filmstrip(snapshot(), {'every_frames': 1.5})


def test_timestamp_is_one_exact_authored_frame_with_explicit_floor_rule():
    index = plan_filmstrip(
        snapshot(fps_rational=[24, 1], duration_frames=48),
        filmstrip_options({'at': '0.13', 'context': 3}),
    )
    assert [card['frame'] for card in index['cards']] == [3]
    assert float(index['sampling']['requested_at']) == pytest.approx(0.13)
    assert index['sampling']['resolved_at_frame'] == 3
    assert index['sampling']['resolved_at_time'] == 0.125
    assert index['sampling']['rounding_rule'] == 'floor_at_authored_fps'
    assert index['sampling']['range'] == [0.125, 1 / 6]

    with pytest.raises(ValueError, match='outside the rendered extent'):
        plan_filmstrip(
            snapshot(fps_rational=[24, 1], duration_frames=48),
            filmstrip_options({'at': '2.1'}),
        )


def test_canonical_occurrence_filter_wins_over_legacy_alias():
    snap = snapshot(clips=[dict(
        id='canonical', kind='video', asset='one', at=0, duration=2,
        occurrence_id='canonical-occ', shot_occurrence_id='legacy-occ',
    )])
    cards = plan_filmstrip(snap, {'occurrence': 'canonical-occ', 'every_frames': 12})['cards']
    assert cards and cards[0]['clips'][0]['id'] == 'canonical'
    with pytest.raises(ValueError, match='No clips match'):
        plan_filmstrip(snap, {'occurrence': 'legacy-occ', 'every_frames': 12})


def test_friendly_shot_aliases_resolve_in_authored_order():
    base = snapshot(
        duration_frames=144,
        clips=[
            dict(id='a', kind='video', asset='one', at=0, duration=2, shot_id='shot-a', shot_name='Opening'),
            dict(id='b', kind='video', asset='two', at=2, duration=2, shot_id='shot-b', shot_name='Middle'),
        ],
        occurrences=[
            dict(occurrence_id='occ-a', shot_id='shot-a', shot_name='Opening', start=0, end=2, start_frame=0, end_frame=48),
            dict(occurrence_id='occ-b', shot_id='shot-b', shot_name='Middle', start=2, end=4, start_frame=48, end_frame=96),
        ],
    )
    assert [card['clips'][0]['shot_id'] for card in plan_filmstrip(base, {'shot': 'first'})['cards']] == ['shot-a'] * 4
    assert [card['clips'][0]['shot_id'] for card in plan_filmstrip(base, {'shot': '2'})['cards']] == ['shot-b'] * 4
    assert [card['clips'][0]['shot_id'] for card in plan_filmstrip(base, {'shot': 'Opening'})['cards']] == ['shot-a'] * 4
    # Canonical ids win over ordinal interpretation.
    exact_numeric = dict(base, clips=[dict(base['clips'][0], shot_id='1')], occurrences=[])
    assert plan_filmstrip(exact_numeric, {'shot': '1'})['cards'][0]['clips'][0]['shot_id'] == '1'
    with pytest.raises(ValueError, match='out of range'):
        plan_filmstrip(base, {'shot': '3'})


def test_input_navigation_keeps_track_occurrences_distinct_and_copyable():
    index = {
        'provenance': {'project_slug': 'demo', 'timeline_id': 'main', 'render_run_id': 'run'},
        'navigation': {'tracks': [], 'clips': [], 'targets': {}},
    }
    projection = {
        'window': {'fps': [24, 1]},
        'tracks': [
            {'track_id': 'picture', 'clips': [{'clip_id': 'same', 'occurrence_id': 'occ', 'window': [0, 24], 'source_time': [[0, 1], [1, 1]], 'source_preview': {'status': 'placeholder'}, 'subrow': 0}]},
            {'track_id': 'vo', 'clips': [{'clip_id': 'same', 'occurrence_id': 'occ', 'window': [0, 24], 'source_time': [[0, 1], [1, 1]], 'source_preview': {'status': 'placeholder'}, 'subrow': 0}]},
        ],
    }
    _attach_input_navigation(index, projection, track_meta=[{'id': 'picture', 'kind': 'visual'}, {'id': 'vo', 'kind': 'audio'}])
    targets = [key for key in index['navigation']['targets'] if key.startswith('input-clip-')]
    assert len(targets) == 2 and targets[0] != targets[1]
    assert all('focus_command' in index['navigation']['targets'][key]['actions'] for key in targets)
    assert all('seek' not in index['navigation']['targets'][key]['actions'] for key in targets)


def test_v1_04_records_range_density_and_resolution_separately():
    options = filmstrip_options({'range': '1..3', 'every': 0.25, 'resolution': '320x180'})
    index = plan_filmstrip(snapshot(duration_frames=120), options)

    assert options['request'] == {
        'range': [1.0, 3.0], 'at': None,
        'density': {'mode': 'every_seconds', 'value': 0.25},
        'resolution': [320, 180],
    }
    assert index['sampling']['requested_range'] == [1.0, 3.0]
    assert index['sampling']['requested_at'] is None
    assert index['sampling']['density'] == options['density']
    assert index['sampling']['resolution'] == [320, 180]
    assert index['sampling']['effective_range'] == [1.0, 3.0]


def test_v1_04_rejects_conflicting_range_and_density_values():
    with pytest.raises(ValueError, match='range or at'):
        filmstrip_options({'range': '1..2', 'at': 1.5})
    with pytest.raises(ValueError, match='every seconds or every_frames'):
        filmstrip_options({'every': 0.5, 'every_frames': 12})
    with pytest.raises(ValueError, match='resolution'):
        filmstrip_options({'resolution': '320'})


def test_shots_sample_authored_midpoint():
    cards = plan_filmstrip(snapshot(), {'sample': 'shots'})['cards']
    assert [(c['frame'], c['sample_reasons']) for c in cards] == [(24, ['shot_midpoint'])]


@pytest.mark.ffmpeg
def test_pack_uses_rendered_frames_without_html_artifact(tmp_path, monkeypatch):
    drawn_text = []
    draw_text = filmstrip_cards._png_draw_text

    def record_text(*args, **kwargs):
        if len(args) >= 3:
            drawn_text.append(str(args[2]))
        return draw_text(*args, **kwargs)

    monkeypatch.setattr(filmstrip_cards, '_png_draw_text', record_text)
    video = tmp_path / 'render.mp4'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=red:size=160x90:rate=24:duration=4', '-c:v', 'libx264', '-y', str(video)], check=True)
    snap = snapshot(
        scripts=[dict(start=0, end=4, text='</script><script>alert("x")</script>')],
        metadata={'selection': 'explicit_render', 'canonical_timeline': {'parent_revision_id': 'R1'}},
    )
    result = build_filmstrip_pack(out_root=tmp_path / 'pack', video_path=video, snapshot=snap, options={'every': 2})
    assert set(result['paths']) == {'png', 'json', 'markdown'}
    assert not (tmp_path / 'pack' / 'filmstrip.html').exists()
    index = json.loads(Path(result['paths']['json']).read_text())
    assert index['provenance']['render_run_id'] == 'run-exact'
    assert '--render-run run-exact' in index['cards'][0]['actions']['focus_command']
    from PIL import Image
    with Image.open(tmp_path / 'pack' / index['cards'][0]['image']) as image:
        r, g, b = image.getpixel((20, 20))
        assert r > 200 and g < 30 and b < 30
    assert Path(result['paths']['png'][0]).exists()
    markdown = Path(result['paths']['markdown']).read_text()
    assert '# Rendered filmstrip · UNCERTIFIED preview (claimed revision R1)' in markdown
    assert 'Revision: `UNCERTIFIED preview (claimed revision R1)`' in markdown
    assert any('UNCERTIFIED preview (claimed revision R1)' in text for text in drawn_text)
    assert '</script><script>alert' not in markdown
    assert not list((tmp_path / 'pack').glob('*.svg'))


@pytest.mark.ffmpeg
def test_pack_keeps_audio_navigation_links_separate_from_compact_receipt(tmp_path):
    video = tmp_path / 'render.mp4'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=red:size=160x90:rate=24:duration=4', '-c:v', 'libx264', '-y', str(video)], check=True)
    raw_audio = {
        'analysis_identity': 'sha256:' + 'b' * 64,
        'status': 'analyzed',
        'render_digest': 'sha256:video',
        'stream': {'sample_rate': 10},
        'presentation_origin': {'seconds': [0, 1]},
        'waveform': {'levels': [{'id': 'level-2', 'bins': [
            {'index': 0, 'start_sample': 0, 'end_sample': 5},
        ]}]},
        'quiet_gaps': [],
        'speech': {'status': 'no_transcript', 'phrases': []},
    }
    result = build_filmstrip_pack(
        out_root=tmp_path / 'pack',
        video_path=video,
        snapshot=snapshot(clips=[], scripts=[], audio=raw_audio),
        options={'every_frames': 24},
    )

    index = json.loads(Path(result['paths']['json']).read_text())
    sidecar = json.loads((tmp_path / 'pack' / 'audio-analysis.json').read_text())
    assert len(index['cards']) == 4
    assert sidecar == raw_audio
    assert 'audio' not in index
    assert 'navigation' not in index
    assert index['schema'] == 'astrid.filmstrip.v2'
    assert index['audio_sidecar']['path'] == 'audio-analysis.json'
    assert index['audio_sidecar']['digest'] == 'sha256:' + hashlib.sha256((tmp_path / 'pack' / 'audio-analysis.json').read_bytes()).hexdigest()


@pytest.mark.ffmpeg
def test_pack_keeps_audio_navigation_in_rich_result_index(tmp_path):
    video = tmp_path / 'render.mp4'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=red:size=160x90:rate=24:duration=4', '-c:v', 'libx264', '-y', str(video)], check=True)
    raw_audio = {
        'analysis_identity': 'sha256:' + 'b' * 64,
        'status': 'analyzed',
        'render_digest': 'sha256:video',
        'stream': {'sample_rate': 10},
        'presentation_origin': {'seconds': [0, 1]},
        'waveform': {'levels': [{'id': 'level-2', 'bins': [
            {'index': 0, 'start_sample': 0, 'end_sample': 5},
        ]}]},
        'quiet_gaps': [],
        'speech': {'status': 'no_transcript', 'phrases': []},
    }
    result = build_filmstrip_pack(
        out_root=tmp_path / 'pack',
        video_path=video,
        snapshot=snapshot(clips=[], scripts=[], audio=raw_audio),
        options={'every_frames': 24},
    )

    index = json.loads(Path(result['paths']['json']).read_text())
    sidecar = json.loads((tmp_path / 'pack' / 'audio-analysis.json').read_text())
    assert len(index['cards']) == 4
    assert index['schema'] == 'astrid.filmstrip.v2'
    assert 'audio' not in index
    assert sidecar == raw_audio
    assert index['audio_sidecar']['path'] == 'audio-analysis.json'

    navigation = result['frame_index']['navigation']
    assert navigation['audio']['waveform_targets']
    target = navigation['waveforms'][0]
    assert navigation['targets'][target['target']] == target
    assert target['actions']['focus_command']
    assert target['actions']['seek'] == {'start': [0, 1], 'end': [1, 2]}
    assert target['target'] in navigation['frames'][0]['active_audio_targets']


def test_float_arithmetic_noise_does_not_move_cut_boundary():
    clean = snapshot(clips=[dict(id='a', kind='video', at=0, duration=3)])
    noisy = snapshot(clips=[dict(id='a', kind='video', at=0, duration=3.000000000000005)])
    clean_cards = plan_filmstrip(clean, {'sample': 'cuts'})['cards']
    noisy_cards = plan_filmstrip(noisy, {'sample': 'cuts'})['cards']
    assert [c['frame'] for c in noisy_cards] == [c['frame'] for c in clean_cards] == [0, 71, 72]
    assert noisy_cards[-1]['clips'] == []


@pytest.mark.parametrize('fps', [24, 30000 / 1001])
def test_clip_labels_and_cuts_use_canonical_renderer_frames(fps):
    from astrid.core.timeline.duration import clip_end_frame, clip_start_frame
    clip = dict(id='offset', kind='video', at=1.01, **{'from': 0, 'to': 1.02, 'speed': 1.3})
    rational = Fraction(str(fps)).limit_denominator(100000)
    start, end = clip_start_frame(clip, fps), clip_end_frame(clip, fps)
    cards = plan_filmstrip(snapshot(clips=[clip], fps_rational=[rational.numerator, rational.denominator]), {'sample': 'cuts'})['cards']
    by_frame = {c['frame']: c for c in cards}
    assert {start - 1, start, end - 1, end} == set(by_frame)
    assert not by_frame[start - 1]['clips']
    assert by_frame[start]['clips'] == [clip]
    assert by_frame[end - 1]['clips'] == [clip]
    assert not by_frame[end]['clips']


def test_filtered_sampling_jumps_to_short_clip_in_long_movie():
    clip = dict(id='late', kind='video', at=10**10, duration=1)
    cards = plan_filmstrip(snapshot(clips=[clip], duration_frames=24 * (10**10 + 2)), {'clip': 'late', 'every_frames': 1})['cards']
    assert len(cards) == 24
    assert cards[0]['frame'] == 24 * 10**10


def test_static_captions_are_bounded_and_full_script_preserved():
    from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import _static_lines
    text = 'Long script. ' * 100000
    card = plan_filmstrip(snapshot(scripts=[dict(start=0, end=4, text=text)]), {})['cards'][0]
    lines = _static_lines(card)
    assert len(lines) <= 16
    assert lines[-1] == '[Excerpt; full text in JSON / Markdown]'
    assert card['scripts'][0]['text'] == text


def test_repeated_shot_occurrences_have_separate_midpoints():
    clips = [dict(id='first', kind='video', at=0, duration=1, shot_id='repeat', occurrence_id='one'),
             dict(id='second', kind='video', at=3, duration=1, shot_id='repeat', occurrence_id='two')]
    occurrences = [dict(occurrence_id='one', shot_id='repeat', start_frame=0, end_frame=24),
                   dict(occurrence_id='two', shot_id='repeat', start_frame=72, end_frame=96)]
    cards = plan_filmstrip(snapshot(clips=clips, occurrences=occurrences), {'sample': 'shots'})['cards']
    assert [card['frame'] for card in cards] == [12, 84]
    assert [card['clips'][0]['occurrence_id'] for card in cards] == ['one', 'two']


def test_default_is_bounded_full_duration_overview_with_tail_evidence():
    snap = snapshot(
        duration_frames=240,
        clips=[
            dict(id='picture', kind='video', at=0, duration=8),
            dict(id='tail', kind='render_tail', at=8, duration=2, render_tail=True),
        ],
        metadata={'rendered_tail': {'start_frame': 192, 'end_frame': 240, 'status': 'unmapped'}},
    )

    index = plan_filmstrip(snap, {})
    frames = {card['frame']: card for card in index['cards']}

    assert index['sampling']['mode'] == 'overview'
    assert index['sampling']['overview'] is True
    assert len(index['cards']) <= 200
    assert {0, 191, 192, 239}.issubset(frames)
    assert 'overview_first_frame' in frames[0]['sample_reasons']
    assert 'overview_last_frame' in frames[239]['sample_reasons']
    assert 'rendered_tail_transition' in frames[191]['sample_reasons']
    assert 'rendered_tail_eof' in frames[239]['sample_reasons']
    assert index['coverage']['full_duration'] is True
    assert index['coverage']['window_seconds'] == [0.0, 10.0]
    assert index['coverage']['page_count'] == 4
    assert index['coverage']['selected_frame_ids'] == [card['id'] for card in index['cards']]


def test_hundreds_of_cuts_use_bounded_adaptive_overview_and_honest_coverage():
    clips = [dict(id=f'cut-{i:04d}', kind='video', at=i, duration=1) for i in range(400)]
    index = plan_filmstrip(snapshot(duration_frames=400 * 24, clips=clips), {})
    coverage = index['coverage']

    assert len(index['cards']) == 200
    assert index['cards'][0]['frame'] == 0
    assert index['cards'][-1]['frame'] == 9599
    assert coverage['full_duration'] is True
    assert coverage['selected_frame_count'] == 200
    assert coverage['boundary_count'] >= 800
    assert coverage['unselected_boundary_count'] > 0
    assert coverage['all_boundaries_sampled'] is False
    assert 'every fast-cut boundary is sampled' in coverage['not_promised']
    boundary_frames = {entry['frame'] for entry in index['boundary_index']['entries']}
    assert {0, 23, 24, 9599}.issubset(boundary_frames)
    assert all('selected' in entry for entry in index['boundary_index']['entries'])


def test_extract_uses_ffmpeg9_supported_filter_and_frame_mode(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen['argv'] = argv
        frames = tmp_path / 'pack' / 'frames'
        frames.mkdir(parents=True, exist_ok=True)
        (frames / 'sample-000000.jpg').write_bytes(b'jpeg')
        return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

    monkeypatch.setattr(filmstrip_cards.subprocess, 'run', fake_run)
    filmstrip_cards._extract(
        tmp_path / 'render.mp4',
        [{'frame': 12, 'image': 'frames/frame-000000012.jpg'}],
        tmp_path / 'pack',
    )

    argv = seen['argv']
    assert '-vf' in argv
    assert argv[argv.index('-vf') + 1] == "select='eq(n,12)',scale=480:-2"
    assert argv[argv.index('-fps_mode:v') + 1] == 'passthrough'
    assert '-filter_script:v' not in argv
    assert '-vsync' not in argv


def _static_card(image_name='frames/frame.jpg', *, name='Shot one', timestamp='0.000s · frame 0', script='An authored line.'):
    return {
        'id': 'frame-000000000',
        'frame': 0,
        'time_label': timestamp,
        'image': image_name,
        'clips': [{'id': 'clip-1', 'shot_name': name}],
        'scripts': [{'text': script}] if script is not None else [],
        'script_status': 'scripted' if script is not None else 'no script',
    }


def _static_fixture(root, *, size=(64, 36), color=(32, 64, 96)):
    from PIL import Image
    image_path = root / 'frames' / 'frame.jpg'
    image_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, color).save(image_path, format='JPEG', quality=90, optimize=False, progressive=False)
    return image_path


def test_png_text_is_measured_bounded_and_unicode_safe():
    from PIL import Image, ImageDraw
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    name_font, timestamp_font, script_font = filmstrip_cards._png_font(18), filmstrip_cards._png_font(14), filmstrip_cards._png_font(16)
    card = _static_card(
        name='東京 café — ' + ('unbroken-name-' * 80),
        timestamp='1234567890123456789012345678901234567890',
        script='Δé🙂 ' + ('unbroken-script-token-' * 100),
    )
    metrics = filmstrip_cards._png_card_metrics(measure, card, name_font, timestamp_font, script_font)
    assert metrics['name_lines'][-1].endswith('…')
    assert len(metrics['name_lines']) <= 2
    assert all(filmstrip_cards._png_text_width(measure, line, name_font) <= metrics['name_width'] + 0.01 for line in metrics['name_lines'])
    assert all(filmstrip_cards._png_text_width(measure, line, timestamp_font) <= 150.01 for line in metrics['timestamp_lines'])
    assert len(metrics['script_lines']) <= 6
    assert len(metrics['script_lines']) >= 2
    assert all(filmstrip_cards._png_text_width(measure, line, script_font) <= 308.01 for line in metrics['script_lines'])


def test_png_font_chain_uses_bundled_fallbacks_for_missing_glyphs():
    from PIL import Image, ImageDraw
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    font = filmstrip_cards._png_font(18)
    assert font.for_character('A') is font.primary
    assert font.for_character('東') is font.fallback
    assert font.for_character('🙂') is font.emoji
    assert filmstrip_cards._png_text_width(measure, '東🙂', font) > 0


def test_png_extreme_timestamp_gets_its_own_header_line(tmp_path):
    from PIL import Image
    _static_fixture(tmp_path)
    card = _static_card(timestamp='timestamp ' * 80, name='A long shot label')
    result = filmstrip_cards._static_png([card], tmp_path, 1, 50, 'A story', 'run', 'selection')
    with Image.open(result[0]) as page:
        assert page.height >= 314 + 22 * 2 + 22


def test_png_missing_script_is_explicit_and_bounded():
    from PIL import Image, ImageDraw
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    fonts = [filmstrip_cards._png_font(size) for size in (18, 14, 16)]
    metrics = filmstrip_cards._png_card_metrics(measure, _static_card(script=None), *fonts)
    # The text panel is sized to its content: one explicit line, no filler row.
    assert metrics['script_lines'] == ['No spoken text']
    assert metrics['excerpt'] is False


def test_png_page_chrome_is_measured_for_narrow_pages():
    from PIL import Image, ImageDraw
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    title_font, chrome_font = filmstrip_cards._png_font(20), filmstrip_cards._png_font(12)
    width = 1 * filmstrip_cards._PNG_PAGE_WIDTH_STRIDE + 24
    chrome_width = width - 32
    title = filmstrip_cards._png_chrome_lines(measure, 'Timeline ' + ('very-long-name-' * 100), title_font, chrome_width, 2)
    provenance = filmstrip_cards._png_chrome_lines(measure, 'Render ' + ('r' * 400), chrome_font, chrome_width, 2)
    disclaimer = filmstrip_cards._png_chrome_lines(measure, 'Authored script segments, not word-aligned. No script does not imply silence.', chrome_font, chrome_width, 2)
    assert title[-1].endswith('…')
    assert all(filmstrip_cards._png_text_width(measure, line, title_font) <= chrome_width + 0.01 for line in title)
    assert all(filmstrip_cards._png_text_width(measure, line, chrome_font) <= chrome_width + 0.01 for line in provenance + disclaimer)


def test_static_png_geometry_paginates_and_does_not_mutate_cards(tmp_path):
    _static_fixture(tmp_path, size=(80, 40))
    cards = [_static_card(timestamp=f'{i}.000s · frame {i}', name=f'Shot {i}', script=f'Line {i}') for i in range(9)]
    before = copy.deepcopy(cards)
    result = filmstrip_cards._static(cards, tmp_path, 5, 4, 'A story', 'run-exact', 'selection')
    assert len(result['png']) == 3
    assert cards == before
    from PIL import Image
    with Image.open(result['png'][0]) as page:
        assert page.size == (5 * filmstrip_cards._PNG_PAGE_WIDTH_STRIDE + 24, page.size[1])
    one_column = tmp_path / 'one-column'
    one_column.mkdir()
    _static_fixture(one_column, size=(40, 80))
    one = filmstrip_cards._static([_static_card(image_name='frames/frame.jpg')], one_column, 1, 50, 'A story', 'run-exact', 'selection')
    with Image.open(one['png'][0]) as page:
        assert page.size[0] == filmstrip_cards._PNG_PAGE_WIDTH_STRIDE + 24


def test_static_png_draw_geometry_contains_images_and_varies_row_height(tmp_path, monkeypatch):
    from PIL import Image, ImageDraw
    _static_fixture(tmp_path, size=(160, 90))
    cards = []
    for i in range(9):
        image_name = f'frames/frame-{i:03d}.jpg'
        image = tmp_path / image_name
        image.parent.mkdir(parents=True, exist_ok=True)
        size = (90, 160) if i % 2 else ((160, 90) if i < 8 else (24, 24))
        Image.new('RGB', size, (50 + i * 15, 70 + i * 8, 110 + i * 6)).save(image, format='JPEG', quality=90, optimize=False, progressive=False)
        cards.append(_static_card(image_name=image_name, name=f'Geometry {i}', timestamp=f'{i}.000s', script=('Long row script. ' * 100 if i == 0 else 'Short row script.')))

    text_calls, paste_calls = [], []
    original_text = ImageDraw.ImageDraw.text
    original_paste = Image.Image.paste

    def spy_text(draw, xy, text, *args, **kwargs):
        text_calls.append((xy, str(text)))
        return original_text(draw, xy, text, *args, **kwargs)

    def spy_paste(image, source, box=None, *args, **kwargs):
        paste_calls.append((box, source.size))
        return original_paste(image, source, box, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, 'text', spy_text)
    monkeypatch.setattr(Image.Image, 'paste', spy_paste)
    result = filmstrip_cards._static_png(cards, tmp_path, 8, 50, 'Geometry', 'run', 'selection')
    assert len(paste_calls) == 9
    # Previews fill the card width at each frame's own aspect (16:9, portrait
    # clamped to 1.25x width, square); nothing is letterboxed in a fixed box.
    expected_heights = [filmstrip_cards._png_image_height(h / w) for w, h in
                        [(160, 90), (90, 160), (160, 90), (90, 160), (160, 90), (90, 160), (160, 90), (90, 160), (24, 24)]]
    assert [size for _box, size in paste_calls] == [(328, h) for h in expected_heights]
    image_box_tops = [box[1] for box, _size in paste_calls]
    assert image_box_tops[0] == image_box_tops[1] == image_box_tops[7]
    assert image_box_tops[8] - image_box_tops[0] > 16 + 22 * 2
    assert Path(result[0]).exists()

    name_call = next((xy for xy, text in text_calls if text == 'Geometry 0'), None)
    timestamp_call = next((xy for xy, text in text_calls if text == '0.000s'), None)
    spoken_call = next((xy for xy, text in text_calls if text.startswith('“')), None)
    assert name_call and timestamp_call and spoken_call
    image_x, image_y = paste_calls[0][0]
    assert name_call[1] < image_y
    assert spoken_call[1] > image_box_tops[0] + expected_heights[0]
    layout = filmstrip_cards._png_card_metrics(ImageDraw.Draw(Image.new('RGB', (1, 1))), cards[0], filmstrip_cards._png_font(18), filmstrip_cards._png_font(14), filmstrip_cards._png_font(16))
    assert not layout['separate_timestamp']
    assert name_call[0] + filmstrip_cards._png_text_width(ImageDraw.Draw(Image.new('RGB', (1, 1))), 'Geometry 0', filmstrip_cards._png_font(18)) <= timestamp_call[0] - 12
    assert image_x == 16


def test_png_waveform_projects_frozen_audio_bins_into_card_strip(tmp_path):
    from PIL import Image, ImageDraw
    _static_fixture(tmp_path)
    audio = {
        'status': 'ok',
        'stream': {'sample_rate': 10, 'channels': 1},
        'presentation_origin': {'seconds': [0, 1]},
        'waveform': {
            'duration_seconds': 4,
            'levels': [{
                'id': 'level-8', 'target_bins': 8,
                'bins': [
                    {'index': 0, 'start_sample': 0, 'end_sample': 5, 'peak': [0.8]},
                    {'index': 1, 'start_sample': 5, 'end_sample': 10, 'peak': [0.0]},
                    {'index': 2, 'start_sample': 10, 'end_sample': 15, 'peak': [0.4]},
                ],
            }],
        },
    }
    card = _static_card(timestamp='1.000s', script='Spoken line')
    measure = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    fonts = [filmstrip_cards._png_font(size) for size in (18, 14, 16)]
    metrics = filmstrip_cards._png_card_metrics(measure, card, *fonts, audio)
    assert metrics['waveform'] is not None
    assert max(metrics['waveform']['amplitudes']) == pytest.approx(0.8)
    # The measured values stay intact, while a quiet mix gets a display-only
    # peak normalization so its voice waveform remains legible in a card.
    assert max(metrics['waveform']['display_amplitudes']) == pytest.approx(0.96)
    assert metrics['waveform']['display_amplitudes'][30] == pytest.approx(0.0)
    assert metrics['audio_extra'] == (filmstrip_cards._PNG_AUDIO_TOP_GAP + filmstrip_cards._PNG_AUDIO_HEIGHT
                                      + filmstrip_cards._PNG_AUDIO_BOTTOM_GAP)
    result = filmstrip_cards._static_png([card], tmp_path, 1, 50, 'A story', 'run', 'selection', audio)
    assert Path(result[0]).exists()


def test_static_png_rejects_oversized_page_before_allocating(tmp_path):
    _static_fixture(tmp_path)
    huge_timestamp = 'timestamp\n' * 30000
    with pytest.raises(ValueError, match='64 million pixels'):
        filmstrip_cards._static([_static_card(timestamp=huge_timestamp)], tmp_path, 1, 50, 'A story', 'run-exact', 'selection')


def test_png_font_loading_fails_closed_when_bundled_font_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(filmstrip_cards, '_PNG_FONT_PATH', tmp_path / 'missing.ttf')
    with pytest.raises(RuntimeError, match='Bundled PNG font is unavailable'):
        filmstrip_cards._png_font(12)


@pytest.mark.ffmpeg
def test_d69_upstream_pack_uses_rendered_frames_without_html_artifact(tmp_path):
    video = tmp_path / 'render.mp4'
    subprocess.run(['ffmpeg', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=red:size=160x90:rate=24:duration=4', '-c:v', 'libx264', '-y', str(video)], check=True)
    snap = snapshot(scripts=[dict(start=0, end=4, text='</script><script>alert("x")</script>')], metadata={'selection': 'explicit_render'})
    result = build_filmstrip_pack(out_root=tmp_path / 'pack', video_path=video, snapshot=snap, options={'every': 2})
    assert set(result['paths']) == {'png', 'json', 'markdown'}
    assert not (tmp_path / 'pack' / 'filmstrip.html').exists()
    index = json.loads(Path(result['paths']['json']).read_text())
    assert index['provenance']['render_run_id'] == 'run-exact'
    assert '--render-run run-exact' in index['cards'][0]['actions']['focus_command']
    from PIL import Image
    with Image.open(tmp_path / 'pack' / index['cards'][0]['image']) as image:
        r, g, b = image.getpixel((20, 20))
        assert r > 200 and g < 30 and b < 30
    assert Path(result['paths']['png'][0]).exists()
    markdown = Path(result['paths']['markdown']).read_text()
    assert '</script><script>alert' not in markdown
    assert not list((tmp_path / 'pack').glob('*.svg'))
