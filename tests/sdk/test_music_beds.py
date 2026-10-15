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
from astrid.sdk.timeline_checkout import Checkout
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
    return collection(tl)['a-music']


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
    row = copy.deepcopy(tl.bundle['placements'][0])
    row['occurrence_id'] = 'repeat-A'
    row['placement']['start_ms'] = 8000
    tl.bundle['placements'].append(row)
    before = tl.document()
    with pytest.raises(MusicBedError, match='occurrence-local'):
        tl.insert_time(2, 1)
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
    other_before = copy.deepcopy(collection(tl)['other-music'])
    tl.clip('a-music').set_look(volume=.2)
    assert collection(tl)['other-music'] == other_before
    assert len(music(tl)) == 4


def test_existing_remove_then_add_replaces_with_new_authored_state():
    tl = enrolled()
    tl.clip('a-music').remove()
    replacement = tl.add('media', asset='Q', at=0, hold=8, track='music', clip_id='replacement')
    assert replacement.asset == 'Q' and not collection(tl)
    tl.insert_time(3, 1)
    assert collection(tl)['replacement']['ripple_basis'] == [dict(at_s=3., delta_s=1.)]


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


def test_unrepresentable_empty_materialization_refuses_without_losing_carrier():
    tl = enrolled()
    before = tl.document()
    with pytest.raises(MusicBedError, match='occurrence-scoped owner'):
        tl.ripple_delete(0, 9)
    assert tl.document() == before


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
