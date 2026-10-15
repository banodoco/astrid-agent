"""Managed music contract on the existing tiny fixture; no historical film claim."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from astrid.core.timeline.music_bed import canonical_basis, move_point
from astrid.sdk.timeline.compiler import compile_timing
from astrid.sdk.timeline.music_beds import MusicBedError, collection, materialize
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from astrid.sdk.timeline.model import retime_scope

TINY = Path(__file__).resolve().parents[1] / 'fixtures/timeline_editing/tiny.json'


def tiny():
    data = json.loads(TINY.read_text())
    next(c for c in data['shots']['A']['internal_timeline']['clips'] if c['id'] == 'a-music')['to'] = 8.
    return Checkout(data)


def music(tl):
    return [copy.deepcopy(c.data) for c in tl.clips(audio=True) if c.track == 'music']


def enrolled():
    tl = tiny()
    tl.insert_time(2, 1)
    return tl


def entry(tl):
    return collection(tl)['occurrence:occ-A/clip:a-music']


def test_original_source_and_no_new_edit_compilation_are_idempotent():
    tl = tiny()
    original = copy.deepcopy(tl.clip('a-music').data)
    tl.insert_time(2, 1)
    tl.insert_time(2, 1)
    assert entry(tl)['source'] == original
    assert entry(tl)['ripple_basis'] == [dict(at_s=2., delta_s=2.)]
    assert entry(tl)['outputs'] == ['a-music', 'a-music:b']
    before = tl.document()
    assert compile_timing(before).bundle == before
    tl.resolve()
    tl.resolve()
    assert tl.document() == before


def test_real_save_load_and_fresh_process_match_two_in_process_edits(tmp_path):
    continuous = enrolled()
    continuous.insert_time(2, 1)
    tl = enrolled()
    path = tmp_path / 'bed.checkout.json'
    tl.save(path, quiet=True)
    # Actual existing load/save APIs in a new interpreter, not a JSON substitute.
    subprocess.run([sys.executable, '-c', "from astrid.sdk.timeline_checkout import Checkout; import sys; t=Checkout.load(sys.argv[1]); t.resolve(); t.insert_time(2,1); t.save(quiet=True)", str(path)], check=True)
    loaded = Checkout.load(path)
    assert collection(loaded) == collection(continuous)
    assert music(loaded) == music(continuous)
    before = loaded.document()
    loaded.resolve()
    assert before == loaded.document()


def test_equal_total_delta_separated_locations_have_distinct_plans():
    one, two = tiny(), tiny()
    one.insert_time(2, 1)
    one.insert_time(2, 1)
    two.insert_time(2, 1)
    two.insert_time(6, 1)
    assert sum(p['delta_s'] for p in entry(one)['ripple_basis']) == sum(p['delta_s'] for p in entry(two)['ripple_basis'])
    assert len(music(one)) == 2 and len(music(two)) == 3
    assert entry(one)['outputs'] != entry(two)['outputs']
    ids = entry(two)['outputs'][:]
    two.resolve()
    assert entry(two)['outputs'] == ids


def test_canonical_coincident_cancellation_and_subframe_cumulative_inputs():
    assert canonical_basis([dict(at_s=2, delta_s=.01), dict(at_s=2, delta_s=.02)]) == [dict(at_s=2, delta_s=.03)]
    assert canonical_basis([dict(at_s=2, delta_s=1), dict(at_s=2, delta_s=-1)]) == []
    tl = enrolled()
    tl.ripple_delete(2, 3)
    assert entry(tl)['ripple_basis'] == []
    assert len(music(tl)) == 1 and music(tl)[0] == entry(tl)['source']


def test_prior_location_moves_through_insertion_and_removal():
    tl = tiny()
    tl.insert_time(6, 1)
    tl.insert_time(2, 1)
    assert entry(tl)['ripple_basis'] == [dict(at_s=2., delta_s=1.), dict(at_s=7., delta_s=1.)]
    tl.ripple_delete(3, 4)
    assert entry(tl)['ripple_basis'] == [dict(at_s=2., delta_s=1.), dict(at_s=3., delta_s=-1.), dict(at_s=6., delta_s=1.)]
    assert move_point(3.5, 3, 4, -1) == 3


def test_output_exclusion_and_temporary_plays_on_preserve_mapping():
    tl = enrolled()
    source, basis, output = copy.deepcopy(entry(tl)['source']), copy.deepcopy(entry(tl)['ripple_basis']), music(tl)
    tl.insert_time(2, 1, skip=[tl.clip('a-music:b').data])
    assert entry(tl)['source'] == source and entry(tl)['ripple_basis'] == basis and music(tl) == output
    tl.insert_time(2, 1, music='plays_on')
    assert tl.music == 'keep_end'
    assert entry(tl)['ripple_basis'] == basis and music(tl) == output


def test_authored_gain_trim_beats_metadata_and_whole_bed_translation():
    tl = enrolled()
    basis = copy.deepcopy(entry(tl)['ripple_basis'])
    tl._registry('A')['Q']['duration'] = 8.
    tl.clip('a-music').set_look(volume=.3)
    assert entry(tl)['source']['volume'] == .3
    assert all(c['volume'] == .3 for c in music(tl))
    tl.clip('a-music').trim(.5, 7)
    assert entry(tl)['source']['from'] == .5 and entry(tl)['source']['to'] == 7
    assert entry(tl)['ripple_basis'] == basis
    tl.clip('a-music').set_beats({'beats': [1., 2., 3., 4., 5., 6.], 'downbeats': [1., 3., 5.], 'time': 'cue_seconds'})
    tl.clip('a-music').tag(style='warm')
    assert entry(tl)['source']['app']['astrid']['style'] == 'warm'
    tl.clip('a-music').nudge(1)
    assert entry(tl)['source']['at'] == 1
    assert entry(tl)['ripple_basis'] == [dict(at_s=3., delta_s=1.)]


def test_removal_does_not_resurrect_source_and_existing_rollback_restores_carrier():
    tl = enrolled()
    before = tl.document()
    with pytest.raises(RuntimeError):
        with tl.edit('rollback source'):
            tl.clip('a-music').set_look(volume=.2)
            tl.insert_time(2, 1)
            raise RuntimeError('rollback')
    assert tl.document() == before
    tl.clip('a-music').remove()
    assert not collection(tl) and not music(tl)
    tl.resolve()
    assert not music(tl)


def test_existing_disk_undo_redo_restores_same_carrier(tmp_path):
    tl = tiny()
    path = tmp_path / 'undo.checkout.json'
    tl.save(path, quiet=True)
    tl.insert_time(2, 1)
    tl.save(quiet=True)
    once = copy.deepcopy(collection(tl)), music(tl)
    tl.insert_time(2, 1)
    tl.save(quiet=True)
    twice = copy.deepcopy(collection(tl)), music(tl)
    tl.undo()
    assert (collection(tl), music(tl)) == once
    tl.redo()
    assert (collection(tl), music(tl)) == twice


@pytest.mark.parametrize('operation', ['trim', 'fade', 'placement'])
def test_generated_output_edits_refuse_atomically_with_source_route(operation):
    tl = enrolled()
    before = tl.document()
    with pytest.raises(MusicBedError, match='authored source'):
        output = tl.clip('a-music:b')
        if operation == 'trim':
            output.trim(0, 4)
        elif operation == 'fade':
            output.set_look(fade_in=.1)
        else:
            output.nudge(1)
    assert tl.document() == before


@pytest.mark.parametrize('damage', ['external', 'basis', 'outputs', 'owner'])
def test_inconsistent_carrier_refuses_without_partial_mutation(damage):
    tl = enrolled()
    if damage == 'external':
        tl.clip('a-music:b').data['from'] += .5
    elif damage == 'basis':
        entry(tl)['ripple_basis'] = [dict(at_s=2, delta_s=float('nan'))]
    elif damage == 'outputs':
        entry(tl)['outputs'] = ['unrelated']
    else:
        entry(tl)['owner'] = 'unrelated'
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(MusicBedError):
        compile_timing(tl.bundle)
    # Compare JSON to preserve NaN equality in the malformed witness.
    assert json.dumps(tl.bundle, sort_keys=True) == json.dumps(before, sort_keys=True)


def test_collision_and_ambiguous_physical_occurrence_refuse_atomically():
    tl = tiny()
    tl._internal('A')['clips'].append(dict(id='a-music:b', clipType='media', track='sfx', asset='Q', at=0, hold=1))
    before = tl.document()
    with pytest.raises(MusicBedError, match='collision'):
        tl.insert_time(2, 1)
    assert tl.document() == before
    tl = tiny()
    tl._registry('A')['Q']['duration'] = 20.
    row = copy.deepcopy(tl.bundle['placements'][0])
    row.pop('occurrence_id', None)
    row['placement']['start_ms'] = 8000
    tl.bundle['placements'].append(row)
    before = tl.document()
    with pytest.raises(MusicBedError, match='occurrence-local'):
        tl.insert_time(2, 1)
    assert tl.document() == before


@pytest.mark.parametrize('count', [1, 3])
def test_unrelated_aliases_allocate_stably_through_compile_save_undo_redo(tmp_path, count):
    from astrid.sdk.timeline.identity import retain_cut_reservations
    tl = tiny()
    retain_cut_reservations(tl.bundle)
    unrelated = [dict(id='a-music:b' + (f'-{n}' if n > 1 else ''),
                      clipType='am-sprite', track='sprite', asset='R', at=0., hold=.5,
                      params={})
                 for n in range(1, count + 1)]
    tl._internal('A')['clips'].extend(copy.deepcopy(unrelated))
    before = tl.document()
    path = tmp_path / 'collision.checkout.json'
    tl.save(path, quiet=True)
    tl.insert_time(2, 1)
    assert entry(tl)['outputs'] == ['a-music', f'a-music:b-{count + 1}']
    for clip in unrelated:
        assert tl.clip(clip['id']).data == clip
    after = tl.document()
    assert compile_timing(after).bundle == after
    assert compile_timing(after).bundle == after
    tl.resolve()
    assert tl.document() == after
    tl.save(quiet=True)
    loaded = Checkout.load(path)
    assert loaded.document() == after
    loaded.undo()
    assert loaded.document() == before
    loaded.redo()
    assert loaded.document() == after
    ids = entry(loaded)['outputs'][:]
    for clip in unrelated:
        loaded.clip(clip['id']).remove()
    assert entry(loaded)['outputs'] == ids
    before = loaded.document()
    assert compile_timing(before).bundle == before


@pytest.mark.parametrize('damage', ['missing', 'replacement', 'duplicate'])
def test_unexplained_pending_source_loss_or_alias_replacement_refuses_atomically(damage):
    tl = tiny()
    before = tl.document()
    with pytest.raises(MusicBedError, match='source disappeared'):
        with tl.edit('damaged pending source', quiet=True):
            with retime_scope(tl):
                tl.insert_time(2, 1)
                clips = tl._internal('A')['clips']
                source = next(c for c in clips if c['id'] == 'a-music')
                if damage != 'duplicate':
                    clips.remove(source)
                if damage != 'missing':
                    clips.append(copy.deepcopy(source))
    assert tl.document() == before
    assert tl._scope is None and not getattr(tl, '_music_pending', {})


def test_deleted_pending_source_does_not_authorize_replacement_alias():
    tl = tiny()
    tl.clip('a-music').data['to'] = 2.
    before = tl.document()
    with pytest.raises(MusicBedError, match='source disappeared'):
        with tl.edit('replace removed pending source', quiet=True):
            with retime_scope(tl):
                tl.ripple_delete(0, 4)
                tl._internal('A')['clips'].append(dict(id='a-music', clipType='am-sprite',
                                                      track='sprite', at=0., hold=1.))
    assert tl.document() == before


def test_partial_removal_does_not_authorize_pending_source_loss():
    tl = tiny()
    before = tl.document()
    with pytest.raises(MusicBedError, match='source disappeared'):
        with tl.edit('lost source spanning the removal', quiet=True):
            with retime_scope(tl):
                tl.ripple_delete(2, 3)
                clips = tl._internal('A')['clips']
                clips[:] = [c for c in clips if c['id'] != 'a-music']
    assert tl.document() == before


def test_multi_span_plan_allocates_around_occupied_hashed_identity():
    tl = tiny()
    owner = 'occurrence:occ-A/clip:a-music'
    basis = [dict(at_s=2., delta_s=1.), dict(at_s=6., delta_s=1.)]
    expected = materialize(tl.clip('a-music').data, basis, owner)[0]
    assert len(expected) == 3
    unrelated = dict(id=expected[1]['id'], clipType='am-sprite', track='sprite',
                     asset='R', at=0., hold=.5, params={})
    tl._internal('A')['clips'].append(copy.deepcopy(unrelated))
    tl.insert_time(2, 1)
    tl.insert_time(6, 1)
    assert entry(tl)['outputs'] == ['a-music', expected[1]['id'] + '-2', expected[2]['id']]
    assert tl.clip(unrelated['id']).data == unrelated
    before = tl.document()
    assert compile_timing(before).bundle == before
    tl.resolve()
    assert tl.document() == before


def test_unmanaged_resolve_keeps_clip_dictionary_bytes_and_does_not_enroll():
    tl = tiny()
    before = json.dumps(tl.clip('a-music').data, separators=(',', ':'))
    tl.resolve()
    assert json.dumps(tl.clip('a-music').data, separators=(',', ':')) == before
    assert not collection(tl) and 'v' not in tl.bundle['parent']['config']['app']['astrid']


def test_multiple_sources_compile_and_edit_independently():
    tl = tiny()
    other = copy.deepcopy(tl.clip('a-music').data)
    other['id'] = 'other-music'
    tl._internal('A')['clips'].append(other)
    tl.insert_time(2, 1)
    other_before = copy.deepcopy(collection(tl)['occurrence:occ-A/clip:other-music'])
    tl.clip('a-music').set_look(volume=.2)
    assert collection(tl)['occurrence:occ-A/clip:other-music'] == other_before
    assert len(music(tl)) == 4


def test_existing_remove_then_add_replaces_with_new_authored_state():
    tl = enrolled()
    tl.clip('a-music').remove()
    replacement = tl.add('media', asset='Q', at=0, hold=8, track='music', clip_id='replacement')
    assert replacement.asset == 'Q' and not collection(tl)
    tl.insert_time(3, 1)
    assert collection(tl)['occurrence:occ-A/clip:replacement']['ripple_basis'] == [dict(at_s=3., delta_s=1.)]


def test_unenrolled_already_segmented_legacy_path_is_not_promoted():
    from astrid.core.timeline.music_edit import plan_keep_end
    tl = tiny()
    root = tl.clip('a-music').data
    clips = tl._internal('A')['clips']
    index = clips.index(root)
    legacy = plan_keep_end(root, at=2, delta=1).pieces
    clips[index:index+1] = legacy
    tl.insert_time(2, 1)
    assert not collection(tl)
    assert len(music(tl)) == 4  # legacy cuts each of the two spans that overlap this point


def test_opaque_source_values_and_both_format_fields_survive_save_load(tmp_path):
    tl = tiny()
    opaque = {'_timeline': {'start': 1, 'end': 2}, 'unknown': [None, False, {'v': 123}]}
    tl.clip('a-music').data['opaque'] = copy.deepcopy(opaque)
    tl.insert_time(2, 1)
    path = tmp_path / 'opaque.checkout.json'
    tl.save(path, quiet=True)
    loaded = Checkout.load(path)
    assert entry(loaded)['source']['opaque'] == opaque
    assert all(c['opaque'] == opaque for c in music(loaded))
    assert loaded.bundle['parent']['config']['app']['astrid']['v'] == 2
    assert collection(loaded) == collection(tl)


def test_existing_version_hook_detects_undeclared_carrier_and_unsupported_writer(monkeypatch):
    from astrid.core.timeline import compatibility
    tl = enrolled()
    compatibility.require_writable_candidate(tl.document())
    parent = copy.deepcopy(tl.bundle['parent'])
    del parent['config']['app']['astrid']['v']
    with pytest.raises(compatibility.TimelineCompatibilityError, match='requires'):
        compatibility.require_writable_parent(parent)
    monkeypatch.setattr(compatibility, 'SUPPORTED_VERSIONS', frozenset({0, 1}))
    with pytest.raises(compatibility.TimelineCompatibilityError, match='unknown'):
        compatibility.require_writable_candidate(tl.document())


def test_subframe_inputs_accumulate_before_materialization_in_existing_scope():
    tl = tiny()
    with retime_scope(tl):
        tl._shift_after(2, .02, cut_window=None)
        tl._shift_after(2, .02, cut_window=None)
    assert entry(tl)['ripple_basis'] == [dict(at_s=2., delta_s=.04)]
    assert len(music(tl)) == 2


def test_source_rate_asset_and_relational_placement_keep_editorial_basis():
    tl = enrolled()
    tl.clip('a-music').set_look(speed=2.)
    assert entry(tl)['source']['speed'] == 2.
    assert entry(tl)['ripple_basis'] == [dict(at_s=2., delta_s=1.)]
    tl.clip('a-music').swap_asset('Q')
    assert entry(tl)['source']['asset'] == 'Q'
    tl.clip('a-music').on('c1 +1s')
    assert entry(tl)['source']['app']['astrid']['on'] == 'c1 +1s'
    assert entry(tl)['source']['at'] == 1
    assert entry(tl)['ripple_basis'] == [dict(at_s=3., delta_s=1.)]
    before = tl.document()
    tl.resolve()
    assert tl.document() == before


def test_single_cut_source_interval_projection_matches_existing_audio_fields():
    from astrid.core.timeline.music_edit import plan_keep_end
    for delta in (-.5, .5, 4.):
        for points in ([], [0., 1., 2., 3., 4., 5., 6.]):
            source = dict(id='bed', at=0., **{'from': 0., 'to': 8.}, params={'volume': .4}, opaque={'x': None})
            from astrid.sdk import timeline_intent as intent
            if points:
                intent.set_beats(source, {'beats': points})
            output, _, _ = materialize(source, [dict(at_s=2., delta_s=delta)], 'bed')
            expected = plan_keep_end(source, at=2., delta=delta, beats=points).pieces
            assert output == expected


def test_ripple_before_whole_bed_translates_source_and_basis_together():
    tl = tiny()
    tl.clip('a-music').data['at'] = 1.
    tl.insert_time(3, 1)
    source = copy.deepcopy(entry(tl)['source'])
    tl.insert_time(.5, .5)
    assert entry(tl)['source']['at'] == source['at'] + .5
    assert entry(tl)['ripple_basis'] == [dict(at_s=3.5, delta_s=1.)]
    assert music(tl)[0]['at'] == 1.5


def test_missing_secondary_output_rebuilds_from_source_without_history():
    tl = enrolled()
    before = tl.document()
    tl._internal('A')['clips'][:] = [c for c in tl._internal('A')['clips'] if c.get('id') != 'a-music:b']
    tl.resolve()
    assert tl.document() == before


def test_existing_scope_exception_leaves_managed_audio_untouched():
    tl = enrolled()
    before = copy.deepcopy(entry(tl)), music(tl)
    with pytest.raises(RuntimeError):
        with tl.edit('outer rollback'):
            with retime_scope(tl):
                tl.insert_time(2, 1)
                raise RuntimeError('abort')
    assert (entry(tl), music(tl)) == before
    assert tl._scope is None and tl._music_pending == {}


def test_first_enrollment_in_open_scope_resolves_later_exclusion_to_same_source():
    tl = tiny()
    source = tl.clip('a-music').data
    with retime_scope(tl):
        tl.insert_time(2, .5)
        tl.insert_time(6, .5, skip=[source])
    assert entry(tl)['ripple_basis'] == [dict(at_s=2., delta_s=.5)]


def test_managed_global_asset_replacement_uses_source_once(monkeypatch):
    from astrid.sdk.timeline import store
    tl = tiny()
    tl._registry('A')['MUSIC'] = dict(type='audio', media_id='old', duration=8.)
    tl.clip('a-music').data['asset'] = 'MUSIC'
    tl.insert_time(2, 1)
    monkeypatch.setattr(store, 'resolve_handle_entry', lambda *a, **kw: dict(type='audio', media_id='new', content_sha256='new-digest'))
    monkeypatch.setattr(store, 'media_seconds', lambda project, value: 8. if value.get('media_id') == 'old' else 10.)
    monkeypatch.setattr(store, 'handle_beats', lambda *a, **kw: None)
    changed = tl.replace_asset('MUSIC', 'run:new/music')
    assert changed == ['a-music']
    assert entry(tl)['source']['to'] == 10.
    assert entry(tl)['ripple_basis'] == [dict(at_s=2., delta_s=1.)]
    assert music(tl)[-1]['to'] == 10.


def test_original_source_address_still_edits_when_first_derived_output_is_missing():
    tl = enrolled()
    tl._internal('A')['clips'][:] = [c for c in tl._internal('A')['clips'] if c.get('id') != 'a-music']
    tl.clip('a-music').set_look(volume=.6)
    assert entry(tl)['source']['volume'] == .6
    assert all(c['volume'] == .6 for c in music(tl))
    assert entry(tl)['outputs'] == ['a-music', 'a-music:b']


def test_actual_load_refuses_hand_edited_materialization(tmp_path):
    tl = enrolled()
    path = tmp_path / 'external.checkout.json'
    tl.save(path, quiet=True)
    raw = json.loads(path.read_text())
    output = next(c for c in raw['shots']['A']['internal_timeline']['clips'] if c['id'] == 'a-music:b')
    output['from'] += .5
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    with pytest.raises(MusicBedError, match='inconsistent supplied'):
        Checkout.load(path)
    assert path.read_bytes() == before


def test_existing_film_intent_schema_admits_exact_carrier_shape():
    from jsonschema import Draft202012Validator
    doc = json.loads((Path(__file__).resolve().parents[2] / 'schemas/astrid-intent/astrid-intent.schema.json').read_text())
    validator = Draft202012Validator({'$ref': '#/$defs/filmIntent', '$defs': doc['$defs']})
    tl = enrolled()
    validator.validate(tl.bundle['parent']['config']['app']['astrid'])


def test_known_empty_materialization_retains_authored_carrier():
    tl = enrolled()
    source = copy.deepcopy(entry(tl)['source'])
    tl.ripple_delete(0, 9)
    assert entry(tl)['outputs'] == []
    assert entry(tl)['source'] == source
    assert tl.clip('occurrence:occ-A/clip:a-music').data is entry(tl)['source']
    assert not music(tl)


def test_removal_over_source_start_maps_prior_location_and_trims_projection():
    tl = enrolled()
    original = copy.deepcopy(entry(tl)['source'])
    tl.ripple_delete(0, 2)
    assert entry(tl)['source'] == original
    assert entry(tl)['ripple_basis'] == [dict(at_s=0., delta_s=-1.)]
    assert len(music(tl)) == 1
    assert music(tl)[0]['from'] == 1. and music(tl)[0]['to'] == 8.
    assert music(tl)[0]['at'] == 0.
    before = tl.document()
    tl.resolve()
    assert tl.document() == before


def test_original_source_address_reads_authored_span_while_clip_list_is_materialized():
    tl = enrolled()
    assert tl.clip('a-music').duration == 8.
    assert tl.clip('a-music').data is entry(tl)['source']
    assert music(tl)[0]['to'] == 2.125


def test_actual_concurrent_save_preserves_carrier_version_and_unrelated_clip_edit(tmp_path):
    path = tmp_path / 'concurrent.checkout.json'
    tl = tiny()
    tl.save(path, quiet=True)
    other = Checkout.load(path)
    tl.insert_time(2, 1)
    other.clip('b-type').set(text='Concurrent title')
    other.save(quiet=True)
    tl.save(quiet=True)
    loaded = Checkout.load(path)
    assert loaded.bundle['parent']['config']['app']['astrid']['v'] == 2
    assert collection(loaded) == collection(tl)
    assert loaded.clip('b-type').text == 'Concurrent title'


def test_actual_concurrent_same_bed_edits_conflict_without_concatenating_basis(tmp_path):
    path = tmp_path / 'same-bed.checkout.json'
    first = enrolled()
    first.save(path, quiet=True)
    second = Checkout.load(path)
    first.insert_time(2, 1)
    second.insert_time(6, 1)
    second.save(quiet=True)
    before = path.read_bytes()
    from astrid.sdk.timeline._common import TimelineEditError
    with pytest.raises(TimelineEditError):
        first.save(quiet=True)
    assert path.read_bytes() == before
    assert entry(Checkout.load(path))['ripple_basis'] == [dict(at_s=2., delta_s=1.), dict(at_s=6., delta_s=1.)]


def repeated(*, closed=True):
    tl = tiny()
    tl._registry('A')['Q']['duration'] = 20.
    row = copy.deepcopy(tl.bundle['placements'][0])
    row['occurrence_id'] = 'repeat-A'
    row['placement']['start_ms'] = 8000
    tl.bundle['placements'].append(row)
    if not closed:
        row.pop('occurrence_id')
    return tl


def test_known_repeated_occurrences_materialize_and_edit_independently():
    tl = repeated()
    tl.insert_time(2, 1)
    a = 'occurrence:occ-A/clip:a-music'
    b = 'occurrence:repeat-A/clip:a-music'
    rows = {r['occurrence_id']: r for r in tl.bundle['placements']}
    assert rows['occ-A']['shot_id'] != rows['repeat-A']['shot_id']
    b_sid = rows['repeat-A']['shot_id']
    b_before = copy.deepcopy(tl.bundle['shots'][b_sid])
    assert collection(tl)[a]['ripple_basis'] == [dict(at_s=2., delta_s=1.)]
    assert b not in collection(tl)
    tl.clip(a).set_look(volume=.2)
    assert tl.bundle['shots'][b_sid] == b_before
    assert tl.clip(b).data.get('volume') != .2
    tl.insert_time(11, 1)
    assert collection(tl)[b]['ripple_basis'] == [dict(at_s=11., delta_s=1.)]
    assert collection(tl)[b]['source']['at'] == 0
    assert tl.clip(b).start == 9
    assert music(tl)[-1]['at'] == pytest.approx(1.875)
    b_before = copy.deepcopy(collection(tl)[b]), copy.deepcopy(tl.bundle['shots'][b_sid])
    tl.clip(a).trim(.5, 7)
    assert (collection(tl)[b], tl.bundle['shots'][b_sid]) == b_before
    # Both beds span film second 6 when the second occurrence is placed at 4.
    from astrid.sdk.timeline.doc import _set_row_start
    from astrid.sdk.timeline.music_beds import occurrence_changes
    with occurrence_changes(tl.bundle):
        _set_row_start(rows['repeat-A'], 4)
    before_bases = {key: copy.deepcopy(value['ripple_basis']) for key, value in collection(tl).items()}
    tl.insert_time(6, 1)
    assert collection(tl)[a]['ripple_basis'] != before_bases[a]
    assert collection(tl)[b]['ripple_basis'] != before_bases[b]
    for key, value in collection(tl).items():
        from astrid.sdk.timeline.music_beds import locate
        sid, clips = locate(tl, key, value['source'])
        expected = materialize(value['source'], value['ripple_basis'], key, start=tl._shot_start(sid), fps=tl.fps)[0]
        assert [c for c in clips if c['id'] in value['outputs']] == expected


def test_unqualified_shared_input_and_duplicate_occurrence_collision_refuse_bytes():
    for tl in (repeated(), repeated(closed=False)):
        before = json.dumps(tl.bundle, sort_keys=True)
        with pytest.raises(ValueError, match='ambiguous|occurrence-local|matches 2'):
            tl.clip('a-music').set_look(volume=.2)
        assert json.dumps(tl.bundle, sort_keys=True) == before
    tl = repeated()
    tl.bundle['placements'][-1]['occurrence_id'] = 'occ-A'
    before = json.dumps(tl.bundle, sort_keys=True)
    with pytest.raises(MusicBedError, match='occurrence-local'):
        tl.insert_time(2, 1)
    assert json.dumps(tl.bundle, sort_keys=True) == before


def test_zero_outputs_fresh_load_edit_regenerate_and_remove_preserve_list_order(tmp_path):
    tl = enrolled()
    tl._registry('A')['Q']['duration'] = 20.
    tl.ripple_delete(0, 9)
    owner = 'occurrence:occ-A/clip:a-music'
    assert entry(tl)['outputs'] == []
    original = copy.deepcopy(entry(tl)['source'])
    unrelated = copy.deepcopy(tl._internal('A')['clips'])
    path = tmp_path / 'empty.checkout.json'
    tl.save(path, quiet=True)
    subprocess.run([sys.executable, '-c', '''from astrid.sdk.timeline_checkout import Checkout
import sys
t=Checkout.load(sys.argv[1]); t.resolve()
assert t.clip('occurrence:occ-A/clip:a-music').data['to']==8
t.clip('occurrence:occ-A/clip:a-music').set_look(volume=.4)
t.save(quiet=True)
''', str(path)], check=True)
    loaded = Checkout.load(path)
    assert entry(loaded)['outputs'] == []
    assert entry(loaded)['source']['volume'] == .4
    assert entry(loaded)['ripple_basis'] == entry(tl)['ripple_basis']
    # A longer source now extends beyond the removed interval and emits audio.
    basis = copy.deepcopy(entry(loaded)['ripple_basis'])
    loaded.clip(owner).trim(0, 12)
    assert entry(loaded)['ripple_basis'] == basis
    assert entry(loaded)['outputs'] == ['a-music']
    assert loaded._internal('A')['clips'][:-1] == unrelated
    assert loaded._internal('A')['clips'][-1]['id'] == 'a-music'
    ids = entry(loaded)['outputs'][:]
    loaded.resolve()
    assert entry(loaded)['outputs'] == ids
    loaded.clip(owner).remove()
    assert not collection(loaded)
    assert loaded._internal('A')['clips'] == unrelated
    tl.clip(owner).remove()  # also remove while outputs remain empty
    assert not collection(tl) and tl._internal('A')['clips'] == unrelated
    assert original['to'] == 8.


def pinned_music():
    """Synthetic immutable closure opened through the actual authoring adapter."""
    from astrid.core.timeline.authoring_bundle import _digest, open_authoring_bundle
    internal = dict(tracks=[dict(id='music', kind='audio', label='Music')],
                    clips=[dict(id='bed', clipType='media', track='music', asset='Q', at=0.,
                                **{'from': 0., 'to': 8.})],
                    registry=dict(assets=dict(Q=dict(type='audio', media_id='sha256:' + '1' * 64, duration=20.))))
    shot = dict(items=[], metadata={}, internal_timeline_revision_id='internal-r1')
    parent = dict(config={}, clips=[], occurrences=[
        dict(occurrence_id=oid, shot_id='shared', shot_revision_id='shot-r1',
             placement=dict(start_ms=start), duration_ms=8000,
             source_offset=dict(start=0, end=0), speed=dict(numerator=1, denominator=1))
        for oid, start in [('first', 0), ('second', 8000)]])
    return Checkout(open_authoring_bundle(
        dict(project_id='synthetic', timeline_id='music', revision_id='parent-r1',
             content_digest=_digest(parent), payload=parent),
        shot_revisions=[dict(project_id='synthetic', shot_id='shared', revision_id='shot-r1',
                             content_digest=_digest(shot), payload=shot,
                             internal_timeline_revision_id='internal-r1')],
        internal_timeline_revisions=[dict(project_id='synthetic', timeline_id='internal', revision_id='internal-r1',
                                          content_digest=_digest(internal), payload=internal)]))


def test_real_authoring_closure_independent_edits_move_duplicate_remove_and_compile():
    from astrid.sdk.timeline_editing import duplicate_authoring_shot, move_occurrence_group, remove_authoring_shot
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
    tl = pinned_music()
    tl.insert_time(2, 1)
    tl.insert_time(11, 1)
    first, second = 'occurrence:first/clip:bed', 'occurrence:second/clip:bed'
    rows = {r['occurrence_id']: r for r in tl.bundle['placements']}
    first_sid, second_sid = rows['first']['shot_id'], rows['second']['shot_id']
    assert first_sid != second_sid
    other = copy.deepcopy(tl.bundle['shots'][second_sid]), copy.deepcopy(collection(tl)[second])
    tl.clip(first).set_look(volume=.25)
    assert (tl.bundle['shots'][second_sid], collection(tl)[second]) == other
    sources = {key: copy.deepcopy(value['source']) for key, value in collection(tl).items()}
    basis = {key: copy.deepcopy(value['ripple_basis']) for key, value in collection(tl).items()}
    move_occurrence_group(tl.bundle, 'second', before_occurrence_id='first')
    assert set(collection(tl)) == {first, second}
    assert collection(tl)[first]['source'] == sources[first]
    assert collection(tl)[second]['source'] == sources[second]
    assert collection(tl)[first]['ripple_basis'] == [dict(at_s=p['at_s'] + 9, delta_s=p['delta_s']) for p in basis[first]]
    assert collection(tl)[second]['ripple_basis'] == [dict(at_s=p['at_s'] - 9, delta_s=p['delta_s']) for p in basis[second]]
    originals = copy.deepcopy(collection(tl))
    duplicated = duplicate_authoring_shot(tl.bundle, second_sid, new_shot_id='copy', occurrence_id='third', start_ms=20000)
    third = 'occurrence:third/clip:bed'
    assert duplicated['shot_id'] == 'copy'
    assert collection(tl)[third]['source'] == sources[second]
    assert collection(tl)[third]['ripple_basis'] == [dict(at_s=p['at_s'] + 20, delta_s=p['delta_s']) for p in collection(tl)[second]['ripple_basis']]
    assert {key: collection(tl)[key] for key in originals} == originals
    tl.clip(third).set_look(volume=.8)
    assert collection(tl)[second]['source'] == sources[second]
    before = json.dumps(tl.bundle, sort_keys=True)
    with pytest.raises(ValueError, match='already exists'):
        duplicate_authoring_shot(tl.bundle, first_sid, new_shot_id='collision', occurrence_id='third')
    assert json.dumps(tl.bundle, sort_keys=True) == before
    remove_authoring_shot(tl.bundle, first_sid)
    assert first not in collection(tl) and set(collection(tl)) == {second, third}
    assert first_sid not in tl.bundle['shots']
    compilation = compile_authoring_candidate(tl.document())
    assert compilation.publication['parent_composition']['config']['app']['astrid']['music_beds'] == collection(tl)
    assert compilation.identity_mapping['placements']['third']['shot_id'] == 'copy'


def test_scoped_empty_state_actual_disk_undo_redo_and_publication_payload(tmp_path):
    from astrid.core.timeline.authoring_bundle import publish_authoring_candidate
    tl = pinned_music()
    owner = 'occurrence:first/clip:bed'
    tl.insert_time(2, 1)
    path = tmp_path / 'scoped-history.checkout.json'
    tl.save(path, quiet=True)
    before = copy.deepcopy(collection(tl))
    tl.ripple_delete(0, 9)
    assert collection(tl)[owner]['outputs'] == []
    tl.save(quiet=True)
    empty = copy.deepcopy(collection(tl))
    tl.undo()
    assert collection(tl) == before
    tl.redo()
    assert collection(tl) == empty
    empty_document = tl.document()
    tl.clip(owner).trim(0, 12)
    tl.save(quiet=True)
    regenerated = copy.deepcopy(collection(tl))
    tl.undo()
    assert collection(tl) == empty
    tl.redo()
    assert collection(tl) == regenerated
    class Writer:
        def __init__(self):
            self.calls = []
        def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
            self.calls.append(copy.deepcopy(publication))
            return {'data': {'new_head': publication['parent_revision_id']}}
    writer = Writer()
    publish_authoring_candidate(empty_document, writer, idempotency_key='synthetic-empty')
    publish_authoring_candidate(tl.document(), writer, idempotency_key='synthetic-regenerated')
    assert len(writer.calls) == 2
    assert writer.calls[0]['parent_composition']['config']['app']['astrid']['music_beds'] == empty
    assert writer.calls[1]['parent_composition']['config']['app']['astrid']['music_beds'] == regenerated


def test_empty_owner_collisions_and_invalid_structural_move_refuse_atomically():
    from astrid.sdk.timeline_editing import move_occurrence_group
    tl = pinned_music()
    tl.insert_time(2, 1)
    before = json.dumps(tl.bundle, sort_keys=True)
    with pytest.raises(ValueError, match='itself'):
        move_occurrence_group(tl.bundle, 'first', before_occurrence_id='first')
    assert json.dumps(tl.bundle, sort_keys=True) == before
    tl.ripple_delete(0, 9)
    owner = 'occurrence:first/clip:bed'
    source = copy.deepcopy(collection(tl)[owner]['source'])
    sid = tl.bundle['placements'][0]['shot_id']
    tl._internal(sid)['clips'].append(source)
    before = json.dumps(tl.bundle, sort_keys=True)
    with pytest.raises(TimelineEditError, match='collides'):
        tl.resolve()
    assert json.dumps(tl.bundle, sort_keys=True) == before


def test_existing_parent_scope_retains_zero_output_source_and_regenerates():
    tl = tiny()
    owner = 'parent/clip:parent-bed'
    source = dict(id='parent-bed', clipType='media', track='music', asset='Q', at=0., **{'from': 0., 'to': 8.})
    tl.bundle['parent']['tracks'] = [dict(id='music', kind='audio')]
    basis = [dict(at_s=0., delta_s=-8.)]
    collection(tl, create=True)[owner] = dict(owner=owner, source=source, ripple_basis=basis, outputs=[])
    tl.resolve()
    tl.clip(owner).set_look(volume=.7)
    assert collection(tl)[owner]['outputs'] == []
    assert collection(tl)[owner]['source']['volume'] == .7
    assert collection(tl)[owner]['ripple_basis'] == basis
    # A basis update uses the same canonical projector, not a parent compiler.
    collection(tl)[owner]['ripple_basis'] = []
    from astrid.sdk.timeline.music_beds import project
    project(tl)
    assert tl.bundle['parent']['clips'] == [source]
    tl.clip(owner).remove()
    assert not collection(tl) and tl.bundle['parent']['clips'] == []


def test_known_scoped_shared_property_edit_materializes_inside_existing_transaction():
    tl = repeated()
    owner = 'occurrence:repeat-A/clip:a-music'
    before = json.dumps(tl.bundle, sort_keys=True)
    handle = tl.clip(owner)
    assert handle.start == 8.
    assert json.dumps(tl.bundle, sort_keys=True) == before
    with pytest.raises(ValueError):
        handle.set_look(speed=-1)
    assert json.dumps(tl.bundle, sort_keys=True) == before
    assert handle.start == 8.
    handle.set_look(volume=.33)
    assert handle.start == 8.
    assert tl.clip(owner).data['volume'] == .33
    assert tl.clip('occurrence:occ-A/clip:a-music').data.get('volume') != .33
    assert not collection(tl)
    before = json.dumps(tl.bundle, sort_keys=True)
    with pytest.raises(ValueError, match='ambiguous|matches 2'):
        tl.clip('a-music')
    assert json.dumps(tl.bundle, sort_keys=True) == before


def test_all_missing_known_outputs_regenerate_at_end_without_unrelated_reorder():
    tl = enrolled()
    owned = set(entry(tl)['outputs'])
    tl._internal('A')['clips'][:] = [c for c in tl._internal('A')['clips'] if c['id'] not in owned]
    unrelated = copy.deepcopy(tl._internal('A')['clips'])
    tl.clip('occurrence:occ-A/clip:a-music').set_look(volume=.8)
    assert tl._internal('A')['clips'][:-2] == unrelated
    assert [c['id'] for c in tl._internal('A')['clips'][-2:]] == entry(tl)['outputs']


def test_scoped_explicit_remove_then_add_replacement_starts_without_basis():
    tl = pinned_music()
    tl.insert_time(2, 1)
    first, second = 'occurrence:first/clip:bed', 'occurrence:second/clip:bed'
    tl.insert_time(11, 1)
    other = copy.deepcopy(collection(tl)[second])
    tl.clip(first).remove()
    tl.add('media', asset='Q', at=0., hold=8., track='music', clip_id='replacement')
    assert first not in collection(tl)
    assert collection(tl)[second] == other
    tl.insert_time(3, 1)
    replacement = 'occurrence:first/clip:replacement'
    assert collection(tl)[replacement]['ripple_basis'] == [dict(at_s=3., delta_s=1.)]
    assert collection(tl)[replacement]['source']['hold'] == 8.


def test_two_pinned_occurrences_zero_output_fresh_process_regeneration_is_local(tmp_path):
    tl = pinned_music()
    first, second = 'occurrence:first/clip:bed', 'occurrence:second/clip:bed'
    tl.insert_time(2, 1)
    tl.insert_time(11, 1)
    tl.ripple_delete(0, 9)
    assert collection(tl)[first]['outputs'] == []
    from astrid.sdk.timeline.music_beds import locate
    sid, _ = locate(tl, second, collection(tl)[second]['source'])
    before = copy.deepcopy(collection(tl)[second]), copy.deepcopy(tl.bundle['shots'][sid]['internal_timeline'])
    path = tmp_path / 'two-pinned.checkout.json'
    tl.save(path, quiet=True)
    subprocess.run([sys.executable, '-c', '''from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline.music_beds import collection
import sys
t=Checkout.load(sys.argv[1])
owner='occurrence:first/clip:bed'
assert collection(t)[owner]['outputs']==[]
t.clip(owner).swap_asset('Q')
assert collection(t)[owner]['outputs']==[]
t.clip(owner).trim(0,12)
assert collection(t)[owner]['outputs']
before=t.document();t.resolve();assert before==t.document()
t.save(quiet=True)
''', str(path)], check=True)
    loaded = Checkout.load(path)
    assert (collection(loaded)[second], loaded.bundle['shots'][sid]['internal_timeline']) == before
    assert collection(loaded)[first]['outputs'] == ['bed']
    assert collection(loaded)[first]['ripple_basis'] == collection(tl)[first]['ripple_basis']


def test_zero_output_occurrence_duplicate_and_removal_use_existing_authoring_mapping():
    from astrid.sdk.timeline_editing import duplicate_authoring_shot, remove_authoring_shot
    tl = pinned_music()
    tl.insert_time(2, 1)
    tl.insert_time(11, 1)
    tl.ripple_delete(0, 9)
    first, second, third = 'occurrence:first/clip:bed', 'occurrence:second/clip:bed', 'occurrence:third/clip:bed'
    assert collection(tl)[first]['outputs'] == []
    before = copy.deepcopy(collection(tl))
    sid = tl.bundle['placements'][0]['shot_id']
    duplicate_authoring_shot(tl.bundle, sid, new_shot_id='empty-copy', occurrence_id='third', start_ms=12000)
    assert collection(tl)[third]['source'] == before[first]['source']
    assert collection(tl)[third]['outputs'] == []
    assert collection(tl)[third]['ripple_basis'] == [dict(at_s=p['at_s'] + 12, delta_s=p['delta_s']) for p in before[first]['ripple_basis']]
    assert {key: collection(tl)[key] for key in before} == before
    remove_authoring_shot(tl.bundle, sid)
    assert set(collection(tl)) == {second, third}
    assert collection(tl)[second] == before[second]
    tl.clip(third).trim(0, 12)
    assert collection(tl)[third]['outputs'] == ['bed']
    assert collection(tl)[second] == before[second]
