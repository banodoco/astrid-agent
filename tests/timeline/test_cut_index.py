"""CUTS current-source proofs; no historical film equivalence is implied."""
from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

from astrid.core.timeline.cut_index import _CACHE, bundle_cut_index
from astrid.core.timeline.cuts import occurrences_from_bundle
from astrid.packs.rendering.actions.timeline_visualize.motion.sheet import select_cut
from astrid.packs.rendering.actions.timeline_visualize.motion.window import resolve_window
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from astrid.sdk.timeline_cuts import build_cut_table
from astrid.sdk.timeline_sheet import apply_sheet, cut_intersections, render_sheet
from tests.sdk.test_timeline_sheet import named


def spanning():
    def clip(id, track, at, hold, **tags):
        return dict(id=id, track=track, at=at, hold=hold, clipType='media', app=tags)
    clips = [clip('p17a', 'plate', 0, 2, cut='c17', layer='plate', sequence='seq', sequence_index=0),
             clip('p17b', 'plate', 2, 2, cut='c17', layer='plate', sequence='seq', sequence_index=1),
             clip('p18', 'plate', 1, 1, cut='c18', layer='plate'),
             clip('p19', 'plate', 4, 1, cut='c19', layer='plate'),
             clip('cover', 'sprite', .5, 4, cut='c17', layer='cover'),
             clip('bed', 'music', 0, 6), clip('voice', 'vo', .25, 5), clip('fx', 'sfx', 1, .5)]
    return dict(parent={}, placements=[dict(shot_id='S', occurrence_id='O', duration_ms=6000)],
                shots={'S': dict(payload={}, internal_timeline=dict(clips=clips, tracks=[
                    dict(id=t, kind='audio' if t in {'music', 'vo', 'sfx'} else 'visual')
                    for t in ('plate', 'sprite', 'music', 'vo', 'sfx')]))})


def snapshot(bundle):
    occurrences = occurrences_from_bundle(bundle)
    return dict(clips=[dict(s['clip'], at=s['start'], occurrence_id=o['occurrence_id'], shot_id=o['shot_id'])
                       for o in occurrences for s in o['spans']],
                occurrences=[dict(occurrence_id=o['occurrence_id'], shot_id=o['shot_id'], start=o['start'], end=o['end']) for o in occurrences],
                tracks=bundle['shots']['S']['internal_timeline']['tracks'])


def projection(row):
    return dict(start=row['start'], end=row['end'], visual=[s['id'] for s in row['layers']], audio=[s['id'] for s in row['audio']])


@pytest.mark.parametrize('selector', ['c17', 'c18', 'c19', 1, 2, 3, 4])
def test_spanning_sequence_gaps_overlaps_and_cut_free_audio_agree(selector):
    data = spanning()
    tl = Checkout(data)
    row = tl.cut_index.resolve(selector)
    expected = projection(row)
    assert cut_intersections(tl, selector) == expected
    table = build_cut_table(data)
    assert table['rows'][row['index'] - 1]['intersections'] == expected
    assert projection(select_cut(snapshot(data), selector, 30)) == expected
    assert projection(resolve_window(snapshot(data), {'cut': selector}, 30, 180)['cut']) == expected
    if selector == 'c17':
        assert (row['start'], row['end']) == (0, 4)  # until c19, across c18
        assert row['sequence']['clip_ids'] == ['p17a', 'p17b']
    assert all(intent.cut_of(tl.clip(id).data) is None for id in ('bed', 'voice', 'fx'))
    if selector == 'c18':
        assert 'c17.cover carries on' in render_sheet(tl)


def test_content_cache_invalidates_on_timing_tags_and_metadata_and_rebinds():
    _CACHE.clear()
    a = spanning()
    i = bundle_cut_index(a)
    b = copy.deepcopy(a)
    j = bundle_cut_index(b)
    assert i.digest == j.digest and len(_CACHE) == 1
    assert j.resolve('c18')['clip'] is b['shots']['S']['internal_timeline']['clips'][2]
    j.rows[0]['start'] = 99
    assert bundle_cut_index(a).rows[0]['start'] == 0
    b['shots']['S']['internal_timeline']['clips'][2]['hold'] = .5
    assert bundle_cut_index(b).digest != i.digest
    tl = Checkout(a)
    before = tl.cut_index
    tl.bundle = copy.deepcopy(tl.bundle)
    assert tl.cut_index is not before
    tl.clip('p18').data['hold'] = .5
    assert tl.cut('c18').end == 1.5


def test_named_sequence_does_not_merge_distinct_cut_ids():
    data = spanning()
    data['shots']['S']['internal_timeline']['clips'][1]['app']['cut'] = 'c20'
    rows = bundle_cut_index(data).rows
    assert {'c17', 'c18', 'c19', 'c20'} <= {r.get('cut_id') for r in rows}


def test_missing_picture_orphan_and_carried_alias_resolve_same_cut():
    data = spanning()
    clips = data['shots']['S']['internal_timeline']['clips']
    clips[:] = [c for c in clips if c['id'] not in {'p17a', 'p17b'}]
    cover = next(c for c in clips if c['id'] == 'cover')
    cover['app'].update(cut='c06', orphan='removed picture')
    tl = Checkout(data)
    assert tl.clip('c06.cover').id == 'cover'
    cut = tl.cut('c06')
    assert cut.id == 'c06' and cut.picture is None
    assert 'cover' in [c.id for c in cut.layers]
    assert tl.time('c06') == cut.start
    assert tl.clip('c18.cover').id == 'cover'


@pytest.mark.parametrize('layers', ['stay', 'remove'])
def test_lift_preserves_film_and_cut_free_sound_and_reservations(layers):
    tl = Checkout(named())
    tl.resolve()
    original_duration = tl.duration
    audio = {c.id: copy.deepcopy(c.data) for c in tl.clips(audio=True)}
    original_ids = {c.id for c in tl.clips()}
    intent.set_chapters(tl.bundle, [{'name': 'next', 'from': 'c2'}])
    tl.cut('c2').lift(layers=layers)
    assert tl.duration == original_duration
    assert tl.cut('c1').end == original_duration
    assert tl.clip('c1.plate').end == original_duration
    assert {c.id: c.data for c in tl.clips(audio=True)} == audio
    assert intent.chapters(tl.bundle) == [{'name': 'next', 'from': 'c1'}]
    assert {c.id for c in tl.clips()} <= original_ids
    if layers == 'stay':
        assert tl.clip('c1.card').start == 4
    else:
        assert not tl.clips(text='Live.')
    from astrid.sdk.timeline.identity import reserve_cut_id
    with pytest.raises(TimelineEditError, match='reserved'):
        reserve_cut_id(tl.bundle, 'c2')


def test_split_keeps_all_preexisting_layer_ids_and_does_not_clone_sound():
    tl = Checkout(named())
    tl.resolve()
    before_ids = {c.id for c in tl.clips()}
    audio = {c.id: copy.deepcopy(c.data) for c in tl.clips(audio=True)}
    duration = tl.duration
    tl.cut('c1').split('"went"')
    assert before_ids <= {c.id for c in tl.clips()}
    assert {c.id: c.data for c in tl.clips(audio=True)} == audio
    assert tl.duration == duration
    assert tl.clip('c1a.rocket').start == 1.3


@pytest.mark.parametrize('surface', ['sdk', 'command', 'sheet'])
def test_picture_remove_refusal_is_atomic_on_each_path(surface):
    from astrid.sdk.timeline.commands import run
    tl = Checkout(named())
    tl.resolve()
    before = tl.document()
    with pytest.raises((ValueError, TimelineEditError), match='lift the cut'):
        if surface == 'sdk':
            tl.clip('c2.plate').remove()
        elif surface == 'command':
            run(tl, {'op': 'clip.remove', 'select': 'c2.plate', 'args': {}})
        else:
            apply_sheet(tl, 'edit {"op":"clip.remove","select":"c2.plate","args":{}}')
    assert tl.document() == before


FORBIDDEN = {'picture_cuts', 'find_cut', '_cut_groups'}


def direct_calls(source):
    tree = ast.parse(source)
    aliases = {a.asname or a.name: a.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               for a in node.names if a.name in FORBIDDEN}
    return [(node.lineno, node.func.attr if isinstance(node.func, ast.Attribute) else aliases.get(node.func.id, node.func.id))
            for node in ast.walk(tree) if isinstance(node, ast.Call) and
            ((isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN) or
             (isinstance(node.func, ast.Name) and aliases.get(node.func.id, node.func.id) in FORBIDDEN))]


def test_only_index_can_call_legacy_derivations():
    root = Path(__file__).resolve().parents[2] / 'astrid'
    failures = {str(p.relative_to(root)): direct_calls(p.read_text()) for p in root.rglob('*.py')
                if p != root / 'core/timeline/cut_index.py'}
    assert {p: calls for p, calls in failures.items() if calls} == {}


@pytest.mark.parametrize('source', ['tl._cut_groups()', 'cuts.picture_cuts(xs)',
                                  'from x import find_cut as hidden\nhidden(xs, 1)'])
def test_guard_detects_attribute_and_import_alias_bypasses(source):
    assert direct_calls(source)


def test_timeline_owned_audio_intersects_all_cuts_without_a_picture_cut():
    data = spanning()
    data['parent'] = dict(tracks=[dict(id='music', kind='audio')], clips=[
        dict(id='global-bed', track='music', at=.5, hold=5, clipType='media')])
    rows = bundle_cut_index(data).rows
    assert len(rows) == 4
    assert all('parent:global-bed' in [s['id'] for s in r['audio']] for r in rows)
    assert all(read.get('cut_id') != 'global-bed' for read in rows)
    table = build_cut_table(data)
    for row, table_row in zip(rows, table['rows']):
        assert table_row['intersections'] == projection(row)
    snap = snapshot(data)
    for c in snap['clips']:
        if c['id'] == 'global-bed':
            c['id'] = 'parent:global-bed'
    # A true global capture clip has no shot occurrence; it must not become
    # an extra picture gap in the visualize index.
    snap['clips'] = [c for c in snap['clips'] if c['id'] != 'parent:global-bed'] + [
        dict(id='parent:global-bed', track='music', at=.5, hold=5, kind='audio')]
    for row in rows:
        assert projection(select_cut(snap, row['index'], 30)) == projection(row)


def test_lift_first_cut_refuses_without_touching_audio():
    tl = Checkout(named())
    before = tl.document()
    with pytest.raises(TimelineEditError, match='preceding'):
        tl.cut('c1').lift()
    assert tl.document() == before


@pytest.mark.parametrize('workflow', ['replacement-later', 'generation-placeholder', 'swap-empty-side'])
def test_cut_record_demonstrations(workflow):
    """Use a picture slot or a surviving layer, never a persistent cut record."""
    tl = Checkout(named())
    picture = tl.clip('c2.plate')
    # A placeholder is an existing picture slot with no asset. It preserves
    # its boundary and identity while a generator supplies the new footage.
    picture.clear_asset()
    assert tl.cut('c2').id == 'c2' and tl.cut('c2').picture.id == picture.id
    assert not picture.asset
    if workflow == 'replacement-later':
        picture.swap_asset('Q')
        assert tl.cut('c2').picture.asset == 'Q'
    elif workflow == 'swap-empty-side':
        other = tl.clip('c1.plate')
        asset = other.asset
        other.clear_asset()
        picture.swap_asset(asset)
        assert not other.asset and picture.asset == asset
    assert 'cuts' not in intent._film(tl.bundle)


def test_ordinals_and_picture_prefixes_resolve_through_index_without_storage():
    tl = Checkout(spanning())
    before = tl.document()
    for n, row in enumerate(tl.cut_index.rows, 1):
        assert tl.cut(n).raw == row
        assert tl.cut(str(n)).raw == row
    assert tl.cut('p17a').id == 'c17'
    assert tl.document() == before
    assert '# picture gap · cut 4 · 5.00–6.00 s' in render_sheet(tl)


def test_lint_diff_sweep_filmstrip_and_editorial_share_windows(monkeypatch):
    from fractions import Fraction

    from astrid.packs.editorial.shared.rhythm import build_rhythm
    from astrid.packs.rendering.actions.timeline_visualize import sweep
    from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import picture_cut_frames
    from astrid.packs.rendering.actions.timeline_visualize.motion import conditions, diffview
    data = spanning()
    data['placements'][0]['placement'] = {'start_ms': 0}
    tl = Checkout(data)
    expected = [(r['start'], r['end']) for r in tl.cut_index.rows]
    seen = []
    def check(cuts, *args, **kwargs):
        seen.extend((r['start'], r['end']) for r in cuts)
        return [], []
    monkeypatch.setattr(conditions, 'run_checks', check)
    tl.lint()
    assert seen == expected
    rows, _, _ = diffview._index(data)
    assert [(r['start'], r['end']) for r in rows] == expected
    model = build_rhythm(data)
    assert [(r['start'], r['end']) for r in model['cuts']] == expected
    snap = snapshot(data)
    frames = picture_cut_frames(snap, Fraction(30), 180)
    assert len(frames) == len(expected)
    assert len(sweep.plan_sweep(snap, snap, 30)) >= len(expected)


@pytest.mark.parametrize('next_start', [2., 4.])
def test_split_preserves_picture_gap_or_overlap(next_start):
    data = named()
    # Keep all clips in one shot and explicit picture coverage. c1 ends at
    # 3, while c2 either overlaps it from 2 or follows a one-second gap.
    from astrid.sdk.timeline_structure import one_shot
    tl = Checkout(data)
    data['source_mapping'] = {'shots': {}, 'placements': {}}
    tl = Checkout(data)
    one_shot(tl, shot_id='film', name='FILM')
    p1, p2 = tl.clip('c1.plate'), tl.clip('c2.plate')
    p1.data['hold'] = 3
    intent.set_for(p1.data, 3)
    p2.data['at'] = next_start
    intent.set_for(p2.data, 8 - next_start)
    p2.data['hold'] = 8 - next_start
    before_duration = tl.duration
    tl.cut('c1').split(1.0)
    assert tl.clip('c1.plate').end == 1
    assert tl.clip('c1a.plate').end == 3
    assert tl.clip('c2.plate').start == next_start
    assert tl.duration == before_duration


def test_lift_extends_an_explicit_picture_end_and_rebinds_layer_cut_anchors():
    tl = Checkout(named())
    intent.set_for(tl.clip('c1.plate').data, 4)
    card = tl.clip('c2.card')
    intent.set_on(card.data, 'c2 +0.5s')
    intent.set_until(card.data, 'end of c2')
    tl.resolve()
    at, end, id = card.start, card.end, card.id
    tl.cut('c2').lift()
    assert tl.clip('c1.plate').end == 8
    assert tl.clip(id).start == at and tl.clip(id).end == end
    assert tl.clip(id).cut_id == 'c1'


def test_spanning_sequence_does_not_hide_a_physical_picture_gap():
    data = spanning()
    clips = data['shots']['S']['internal_timeline']['clips']
    clips[0]['hold'] = .5
    clips[1]['at'], clips[1]['hold'] = 3, 1
    rows = bundle_cut_index(data).rows
    gaps = [(r['start'], r['end']) for r in rows if r['clip'] is None]
    assert (.5, 1) in gaps and (2, 3) in gaps and (5, 6) in gaps
    assert bundle_cut_index(data).resolve('c17')['end'] == 4


@pytest.mark.parametrize('visuals', ['orphan', 'remove'])
def test_remove_line_visuals_choice_uses_index_and_keeps_sound_cut_free(visuals):
    from tests.sdk.test_timeline_reflow_golden import film
    tl = Checkout(film())
    tl.remove_line('s2', visuals=visuals)
    assert not any(intent.line(c.data) == 's2' for c in tl.clips(audio=True))
    assert all(c.cut_id is None for c in tl.clips(audio=True))
    if visuals == 'orphan':
        assert tl.cut('c2').id == 'c2'
        assert intent.orphan(tl.cut('c2').picture.data)
    else:
        assert not tl._cut_clips('c2')
        assert tl.clip('c1.field').end == tl.clip('c3.field').start


@pytest.mark.parametrize('segment, cid', [('s1', 'c1'), ('s3', 'c3')])
def test_remove_line_visuals_covers_opening_and_terminal_word_anchored_cuts(segment, cid):
    from tests.sdk.test_timeline_reflow_golden import film
    tl = Checkout(film())
    tl.remove_line(segment, visuals='remove')
    assert not tl._cut_clips(cid)
    assert tl.duration < 9
    assert all(c.cut_id is None for c in tl.clips(audio=True))


def test_sheet_split_preserves_every_existing_layer_id_and_cut_free_sound():
    import json

    from tests.editing_conformance.runner import SCENARIOS, load_scenario, run_scenario
    scenario = load_scenario(SCENARIOS / 'structure/structure.split.c1-went.yaml')
    # Check the actual outputs of all existing front ends. The harness's
    # separate journal/undo failures remain visible in the unchanged tests.
    for result in run_scenario(scenario).values():
        before, after = json.loads(result.base), json.loads(result.after)
        def clips(data):
            return {c['id']: c for shot in data['shots'].values() for c in shot['internal_timeline']['clips']}
        old, new = clips(before), clips(after)
        assert set(old) <= set(new)
        assert len(new) == len(old) + 1  # exactly the copied picture
        assert after['placements'] == before['placements']
        for id, c in old.items():
            if c.get('track') in {'music', 'vo', 'sfx'}:
                assert new[id] == c
