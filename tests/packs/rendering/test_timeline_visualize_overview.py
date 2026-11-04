"""Overview, labels, spoken text, card geometry and timing for timeline visualize."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from astrid.packs.rendering.actions.timeline_visualize import (
    composed_frame,
    contact_sheet,
    filmstrip_cards,
)
from astrid.packs.rendering.actions.timeline_visualize.contact_sheet import (
    contact_reasons,
    static_contact_png,
)
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import (
    _PNG_CARD_WIDTH,
    _png_card_metrics,
    _png_font,
    _png_image_height,
    _png_script_lines,
    _timed_word_records,
    _word_caption_text,
    plan_filmstrip,
)
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_execution import (
    execute_filmstrip,
)
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options
from astrid.sdk import timeline_filmstrip as tf

FPS = 24


def synthetic_snapshot(*, clips=3, frames_per_clip=48, project='demo', selection='composed_frame_capture'):
    """A composed-capture-shaped snapshot: one occurrence per chapter, VO word timings and shot scripts."""
    rows, occurrences, scripts = [], [], []
    for index in range(clips):
        start = index * frames_per_clip
        end = start + frames_per_clip
        occ = f'occ-ch{index + 1:02d}'
        shot, name = f'ch{index + 1:02d}', f'Chapter {index + 1}'
        at = start / FPS
        rows.append({'id': f'{occ}:pic', 'track': 'picture', 'kind': 'visual', 'at': at,
                     'hold': frames_per_clip / FPS, 'duration': frames_per_clip / FPS, 'shot_id': shot,
                     'shot_name': name, 'shot_occurrence_id': occ, 'occurrence_id': occ,
                     'start_frame': start, 'end_frame': end, 'app': {}})
        rows.append({'id': f'{occ}:vo', 'track': 'vo', 'kind': 'audio', 'clipType': 'media', 'at': at,
                     'from': 0.0, 'to': frames_per_clip / FPS, 'shot_id': shot, 'shot_name': name,
                     'shot_occurrence_id': occ, 'occurrence_id': occ, 'duration': frames_per_clip / FPS,
                     'start_frame': start, 'end_frame': end,
                     'app': {'words': [[0.0, 0.4, 'Line'], [0.4, 0.8, f'{index + 1} spoken']]}})
        occurrences.append({'occurrence_id': occ, 'shot_id': shot, 'shot_name': name, 'start': at,
                            'end': end / FPS, 'start_frame': start, 'end_frame': end})
        scripts.append({'start': at, 'end': end / FPS, 'text': f'Line {index + 1} is spoken here.',
                        'shot_id': shot, 'shot_name': name, 'kind': 'voiceover_script', 'occurrence_id': occ,
                        'start_frame': start, 'end_frame': end, 'binding_id': f'b{index}', 'head': 1,
                        'media_id': 'sha256:' + '0' * 64, 'timing_basis': 'shot_script',
                        'label': 'Shot script (not word-aligned)'})
    return dict(project_slug=project, timeline_id='main', timeline_name='Synthetic film',
                render_run_id='capture-run', video_digest='sha256:' + 'b' * 64, fps_rational=[FPS, 1],
                duration_frames=clips * frames_per_clip, clips=rows, scripts=scripts, occurrences=occurrences,
                tracks=[{'id': 'picture', 'kind': 'visual'}, {'id': 'vo', 'kind': 'audio'}],
                registry={'assets': {}}, metadata={'selection': selection})


def write_frame(path: Path, size=(480, 270), colour=(40, 90, 140)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, colour).save(path)


# --- 1. Labels and spoken text reach the cards ------------------------------------------

def runtime_envelope():
    """Runtime-shaped parent expansion: occurrences use ``occurrence_id`` (not the legacy key)."""
    text = b'One year ago, I invited testers into a Discord.\n'
    digest = 'sha256:' + hashlib.sha256(text).hexdigest()
    return {
        'authority_context': {
            'timeline_id': 'tl', 'timeline_slug': 'cut', 'config_version': 1,
            'expansion': {
                'canonical': True,
                'shots': [{'shot_id': 'ch01', 'name': '01 TOMORROW', 'revision_id': 'rev',
                           'text_bindings': [{'binding_id': 'b', 'head': 1, 'media_id': digest,
                                              'content_hash': digest, 'byte_size': len(text),
                                              'kind': 'voiceover_script'}]}],
                'occurrences': [{'occurrence_id': 'occ-ch01', 'shot_id': 'ch01', 'revision_id': 'rev',
                                 'at': 0.0, 'hold': 2.0, 'speed': {'numerator': 1, 'denominator': 1},
                                 'placement': {'start_ms': 0}}],
            },
        },
        'inputs': {
            'profile': {'fps_rational': [24, 1]},
            'timeline_snapshot': {'config': {
                'theme_overrides': {'visual': {'canvas': {'fps': 24}}},
                'tracks': [{'id': 'picture', 'kind': 'visual'}, {'id': 'vo', 'kind': 'audio'}],
                'clips': [
                    {'id': 'occ-ch01:pic', 'track': 'picture', 'at': 0.0, 'hold': 2.0,
                     'shot_occurrence_id': 'occ-ch01', 'shot_id': 'ch01'},
                    {'id': 'occ-ch01:vo', 'track': 'vo', 'at': 0.0, 'from': 0, 'to': 2.0,
                     'shot_occurrence_id': 'occ-ch01', 'shot_id': 'ch01',
                     'app': {'words': [[0.0, 0.5, 'One'], [0.5, 1.0, 'year']]}},
                ]}, 'registry': {'assets': {}}},
        },
    }


class _ScriptClient:
    def __init__(self, raw):
        self.raw = raw

    def get_object(self, object_id):
        return {'data': self.raw}


def test_runtime_occurrence_rows_label_clips_and_carry_the_shot_script():
    envelope = runtime_envelope()
    raw = envelope['authority_context']['expansion']['shots'][0]['text_bindings'][0]['content_hash']
    text = b'One year ago, I invited testers into a Discord.\n'
    assert 'sha256:' + hashlib.sha256(text).hexdigest() == raw
    snapshot = tf.build_filmstrip_snapshot(envelope, client=_ScriptClient(text), project='demo',
                                           run_id='run', video_digest='sha256:' + 'c' * 64)
    occurrence = snapshot['occurrences'][0]
    assert occurrence['occurrence_id'] == 'occ-ch01'
    assert occurrence['shot_name'] == '01 TOMORROW'
    labelled = [clip for clip in snapshot['clips'] if clip.get('shot_occurrence_id') == 'occ-ch01']
    assert labelled and all(clip['shot_name'] == '01 TOMORROW' and clip['shot_id'] == 'ch01'
                            and clip['occurrence_id'] == 'occ-ch01' for clip in labelled)
    assert len(snapshot['scripts']) == 1
    assert snapshot['scripts'][0]['text'] == text.decode('utf-8')
    assert snapshot['scripts'][0]['occurrence_id'] == 'occ-ch01'


def test_word_aligned_vo_is_the_timed_text_when_app_words_exist():
    snapshot = tf.build_filmstrip_snapshot(runtime_envelope(), client=_ScriptClient(
        b'One year ago, I invited testers into a Discord.\n'), project='demo',
        run_id='run', video_digest='sha256:' + 'c' * 64)
    cards = plan_filmstrip(snapshot, {'every': 1})['cards']
    first = cards[0]
    assert first['caption_status'] == 'word-aligned VO (app.words)'
    assert _word_caption_text(first) == 'One year'
    lines, _excerpt = _png_script_lines(ImageDraw.Draw(Image.new('RGB', (1, 1))), first, _png_font(16))
    assert ''.join(lines).replace('“', '').replace('”', '').startswith('One year')


def test_timing_only_words_are_not_captioned_and_the_shot_script_remains():
    clip = {'id': 'vo', 'at': 0.0, 'app': {'words': [[0.0, 0.5], [0.5, 1.0]]}}
    assert _timed_word_records([clip], 0.25) == []


def test_word_records_respect_the_window_and_the_clip_origin():
    clip = {'id': 'vo', 'at': 10.0, 'app': {'words': [[0.0, 0.5, 'early'], [3.0, 3.5, 'late'], {'s': 0.2, 'e': 0.4, 'text': 'dict'}]}}
    records = _timed_word_records([clip], 10.3)
    assert [r['text'] for r in records] == ['early', 'dict']
    assert all(10.0 <= r['start'] < 11.3 for r in records)


# --- 2. Shot names come from the authored payload -----------------------------------------

def test_runtime_expansion_names_shots_from_payload_name_before_shot_id(monkeypatch):
    from astrid.packs.rendering.actions.render import managed_timeline as mt

    shot_rev = {'shot_id': 'ch01', 'revision_id': 'rev1', 'internal_timeline_revision_id': 'int1',
                'content_digest': 'sha256:' + '1' * 64,
                'payload': {'name': '01 TOMORROW', 'metadata': {}, 'text_bindings': []}}

    class Reader:
        def get_project_shot_revision(self, *_):
            return shot_rev

        def get_project_timeline_revision(self, *_):
            return {'revision_id': 'int1', 'content_digest': 'sha256:' + '2' * 64, 'timeline_id': 'internal', 'payload': {}}

    parent = {'revision_id': 'r', 'content_digest': 'sha256:' + '3' * 64, 'payload': {'occurrences': [{'occurrence_id': 'occ-ch01', 'shot_id': 'ch01', 'shot_revision_id': 'rev1'}]}}
    monkeypatch.setattr(mt, '_read_exact_parent_head', lambda **_: parent)
    monkeypatch.setattr(mt, '_exact_revision_reader', lambda _client: Reader())
    import astrid.core.timeline.shot_composition_projection as scp
    monkeypatch.setattr(scp, 'project_runtime_parent_composition',
                        lambda *a, **k: SimpleNamespace(occurrences=(), outputs=(), graph={}, config={}, registry={}))
    _parent, _projected, expansion = mt._project_exact_parent_head(
        client=None, project_id='p', timeline_id='t', parent_revision_id='r')
    assert expansion['shots'][0]['name'] == '01 TOMORROW'

    shot_rev['payload']['metadata'] = {'name': 'Legacy label'}
    _parent, _projected, expansion = mt._project_exact_parent_head(
        client=None, project_id='p', timeline_id='t', parent_revision_id='r')
    assert expansion['shots'][0]['name'] == 'Legacy label'


# --- 3. Card geometry ---------------------------------------------------------------------

def test_preview_height_follows_the_frame_aspect_and_is_bounded():
    assert _png_image_height(9 / 16) == round(_PNG_CARD_WIDTH * 9 / 16)
    assert _png_image_height(1.0) == _PNG_CARD_WIDTH
    assert _png_image_height(16 / 9) == int(_PNG_CARD_WIDTH * 1.25)
    assert _png_image_height(float('nan')) == round(_PNG_CARD_WIDTH * 9 / 16)


def test_card_body_is_sized_to_its_content_not_a_fixed_empty_box():
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    card = {'id': 'frame-1', 'time_label': '1.000s', 'clips': [{'shot_name': '01 TOMORROW'}],
            'scripts': [], 'captions': [], 'caption_status': 'no timed text available'}
    fonts = (_png_font(18), _png_font(14), _png_font(16))
    layout = _png_card_metrics(draw, card, *fonts, image_height=_png_image_height(9 / 16))
    assert layout['height'] == layout['header_height'] + layout['body_height']
    assert layout['body_height'] == (layout['image_height'] + layout['top_gap']
                                     + layout['text_area_height'] + filmstrip_cards._PNG_CARD_BOTTOM_PAD)
    # The preview is the bulk of the card.  The old layout reserved a fixed
    # 314px body, so a 16:9 still filled about half of each card.
    assert layout['image_height'] / layout['height'] >= 0.65
    assert layout['script_lines'] == ['no timed text available']


# --- 4. Overview (contact) view -------------------------------------------------------------

def test_contact_reasons_keep_one_frame_per_cut_and_cap_tiles():
    reasons = {}
    for index in range(300):
        reasons[index * 10 - 1] = {'before_cut'}
        reasons[index * 10] = {'after_cut'}
    kept, thinned = contact_reasons(reasons, mode='cuts', limit=120)
    assert len(kept) == 120
    assert thinned == 300
    assert all('after_cut' in why for why in kept.values())
    interval = {frame: {'interval'} for frame in range(0, 40, 4)}
    assert contact_reasons(interval, mode='interval', limit=120) == (interval, None)


def test_contact_options_default_to_cut_tiles_and_drop_input_lanes():
    options = filmstrip_options({'view': 'contact', 'show': ['output', 'inputs', 'text', 'audio']})
    assert options['view'] == 'contact'
    assert options['sample'] == 'cuts'
    assert options['columns'] == 10
    assert 'inputs' not in options['components']
    assert filmstrip_options({'view': 'contact', 'every': 5})['sample'] == 'interval'
    assert filmstrip_options({'view': 'filmstrip'})['columns'] == 5
    with pytest.raises(ValueError, match='view must be'):
        filmstrip_options({'view': 'structural'})


def test_contact_plan_is_bounded_to_120_tiles():
    snapshot = synthetic_snapshot(clips=240, frames_per_clip=10)
    index = plan_filmstrip(snapshot, filmstrip_options({'view': 'contact'}))
    assert len(index['cards']) == 120
    assert index['contact'] == {'tiles': 120, 'cap': 120, 'sample': 'cuts', 'thinned_from': 240}
    assert index['view'] == 'contact'


def test_contact_sheet_has_one_tile_per_card_on_a_ten_column_grid(tmp_path, monkeypatch):
    cards = []
    for index in range(25):
        name = f'frames/f{index:03d}.png'
        write_frame(tmp_path / name, colour=(20 + index * 7, 80, 120))
        cards.append({'id': f'frame-{index}', 'time_label': f'{index}.000s', 'frame': index * 24,
                      'image': name, 'clips': [{'shot_name': f'Chapter {index % 4}', 'shot_occurrence_id': f'occ{index % 4}'}],
                      'captions': [], 'timed_words': [], 'display_scripts': [{'text': 'Opening words of the voice over'}] if index == 0 else []})
    pastes = []
    original = Image.Image.paste

    def spy(image, source, box=None, *args, **kwargs):
        pastes.append(box)
        return original(image, source, box, *args, **kwargs)

    monkeypatch.setattr(Image.Image, 'paste', spy)
    occurrences = [{'occurrence_id': f'occ{k}', 'shot_name': f'Chapter {k}', 'start': k * 10.0, 'end': (k + 1) * 10.0} for k in range(4)]
    paths = static_contact_png(cards, tmp_path, columns=10, timeline_name='Film', render_run_id='run',
                               render_selection='composed', occurrences=occurrences, duration_seconds=100.0)
    assert len(paths) == 1 and Path(paths[0]).name == 'contact-sheet.png'
    assert len(pastes) == 25
    with Image.open(paths[0]) as sheet:
        assert sheet.size[0] == 2 * contact_sheet.CONTACT_MARGIN + 10 * contact_sheet.CONTACT_TILE_WIDTH + 9 * contact_sheet.CONTACT_GUTTER
        assert sheet.size[1] > 3 * contact_sheet.CONTACT_TILE_WIDTH * 9 // 16


# --- 5. Timing -------------------------------------------------------------------------------

def _execute(tmp_path, snapshot, values, monkeypatch):
    class Provider:
        def __init__(self, *a, **k):
            pass

        def capture(self, cards, out_root, resolution):
            for card in cards:
                write_frame(out_root / card['image'])
            return {'evidence_source': 'fixture', 'frames': len(cards)}

        def close(self, force=False):
            pass

    monkeypatch.setattr(composed_frame, 'RemotionFrameProvider', Provider)
    defaults = dict(
        out=tmp_path / 'run', project_slug='demo', rendered_video=None, filmstrip_authority=None,
        timeline=tmp_path / 'timeline.json', assets_registry=tmp_path / 'assets.json',
        materialized_root=None, materialized_objects=None, timeline_slug=None, render_run=None,
        include_media=False, range_value=None, at=None, frame=None, every=None, every_frames=None,
        sample=None, include_cuts=False, columns=None, page_size=None, resolution=None, shot=None,
        clip=None, occurrence=None, asset=None, context=3.0, neighbors=0, show=None, hide=None,
        track=None, detail=False, formats=None, view='filmstrip',
    )
    defaults.update(values)
    args = SimpleNamespace(**defaults)
    authority = {'mode': 'composed_capture', 'capture_snapshot': snapshot,
                 'capture_identity': 'sha256:' + 'd' * 64}
    return execute_filmstrip(args, authority=authority)


def test_executor_reports_timing_and_the_card_count(tmp_path, monkeypatch):
    result = _execute(tmp_path, synthetic_snapshot(), {'every_frames': 12}, monkeypatch)
    timing = result['timing']
    assert set(timing) >= {'started_at', 'capture_s', 'compose_s', 'total_s', 'frames', 'evidence_source'}
    assert timing['frames'] == 12  # 144 frames at one sample every 12 frames
    assert timing['capture_s'] >= 0 and timing['compose_s'] >= 0
    assert timing['total_s'] >= timing['capture_s']
    assert result['outputs']['timing'] == timing


def test_contact_view_runs_through_the_executor_as_one_page(tmp_path, monkeypatch):
    result = _execute(tmp_path, synthetic_snapshot(clips=6, frames_per_clip=24),
                      {'view': 'contact', 'show': ['output', 'inputs', 'text', 'audio']}, monkeypatch)
    pages = result['outputs']['png']
    assert [Path(page).name for page in pages] == ['contact-sheet.png']
    assert result['outputs']['timing']['frames'] == 6


def test_cli_adds_queue_time_and_prints_one_timing_line(capsys):
    from astrid.packs.timeline import cli

    timing = cli._visualization_timing({'timing': {'started_at': 1320.0, 'capture_s': 41.0, 'frames': 10}}, 1000.0)
    assert timing['queued_s'] == 320.0
    assert timing['wall_s'] >= 0
    cli._print_visualization_timing(timing, 1000.0)
    assert 'captured 10 frames in 41 s (queued 320 s)' in capsys.readouterr().out
    assert cli._visualization_timing({}, 1000.0)['queued_s'] is None
    cli._print_visualization_timing(None, 1000.0)
    assert 'executor timing unavailable' in capsys.readouterr().out
