"""Real runner identity proofs; synthetic data only, no Runtime/catalog access."""
import copy
import json
import random
from pathlib import Path

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout, Clip, Cut, TimelineEditError
from astrid.sdk.timeline import commands, store
from astrid.sdk.timeline.identity import (KEY, cut_reservations, next_cut_id,
                                          reserve_cut_id, retain_cut_reservations)
from astrid.sdk.timeline.rebase import rebase


@pytest.fixture
def tl(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    data = json.loads((Path(__file__).parents[1] / 'fixtures/timeline_editing/tiny_publishable.json').read_text())
    return Checkout(data, path=tmp_path / "identity.json")


def apply(tl, op, select, **args):
    with tl.step("identity proof") as _:
        commands.execute(tl, dict(op=op, select=select, args=args))
    return tl._pending_calls[-1]


def test_reservation_survives_remove_save_reload(tl):
    call = apply(tl, 'cut.split', 'c1', on=2.0)
    assert 'cut:c1a' in call.created
    old = tl.cut('c1a').picture.id
    tl.cut('c1a').lift(layers='remove')
    tl.save(quiet=True)
    fresh = Checkout.load(tl.path)
    assert fresh._next_cut_id('c1') == 'c1b'
    assert old not in [c.id for c in fresh.clips()]
    with pytest.raises(TimelineEditError, match='reserved'):
        fresh.cut('c1').split(2.0, id='c1a')
    assert fresh.cut('c1').split(2.0).id == 'c1b'


def test_legacy_reservation_reads_without_mutating_and_validates_opaque_collision(tl):
    before = tl.document()
    assert next_cut_id(tl.bundle, 'c1') == 'c1a'
    assert tl.document() == before
    tl.bundle['parent']['config'].setdefault('app', {}).setdefault('astrid', {})[KEY] = 'opaque'
    with pytest.raises(TimelineEditError, match='reservation object'):
        tl.cut('c1').split(2.0)
    assert tl.bundle['parent']['config']['app']['astrid'][KEY] == 'opaque'


def test_reservation_high_water_joins_without_a_conflict(tl):
    base = tl.document()
    a, b = copy.deepcopy(base), copy.deepcopy(base)
    reserve_cut_id(a, 'c1b')
    reserve_cut_id(b, 'c2d')
    merged, conflicts = store.three_way(base, a, b, whole=True)
    assert not conflicts
    assert cut_reservations(merged)['suffixes'] == {'1': 2, '2': 4}
    assert next_cut_id(merged, 'c1') == 'c1c'
    reserve_cut_id(merged, 'c1z')
    assert next_cut_id(merged, 'c1') == 'c3'
    with pytest.raises(TimelineEditError, match='reserved'):
        reserve_cut_id(merged, 'c01a')


def test_live_merge_is_keyed_by_occurrence_for_a_shared_shot(tl):
    base = tl.document()
    second = copy.deepcopy(base['placements'][0])
    second['occurrence_id'] = 'occ-A-copy'
    second['placement']['start_ms'] = 8000
    base['placements'].append(second)
    head, ours = copy.deepcopy(base), copy.deepcopy(base)
    head['placements'][0]['duration_ms'] = 3500
    ours['placements'][-1]['duration_ms'] = 2500
    merged, conflicts = store.three_way(base, head, ours, whole=True)
    assert not conflicts
    rows = {r['occurrence_id']: r for r in merged['placements']}
    assert rows['occ-A']['duration_ms'] == 3500
    assert rows['occ-A-copy']['duration_ms'] == 2500
    assert len(rows) == 3
    assert merged['shots'] == base['shots']


def test_ambiguous_legacy_occurrence_merge_refuses_instead_of_collapsing(tl):
    data = tl.document()
    data['placements'] = [{'shot_id': 'A'}, {'shot_id': 'A'}]
    with pytest.raises(TimelineEditError, match='ambiguous placement'):
        store.three_way(data, data, data)


def test_orphan_is_addressable_by_exact_cut_id(tl):
    for c in tl._cut_clips('c2'):
        intent.set_cut(c.data, 'c06')
        intent.set_orphan(c.data, 'its line was removed')
    assert 'c06' not in [g['id'] for g in tl._cut_groups()]
    orphan = tl.cut('c06')
    assert orphan.id == 'c06'
    assert orphan.picture is not None
    assert set(c.id for c in orphan.clips) == set(c.id for c in tl._cut_clips('c06'))
    assert intent.orphan(orphan.picture.data)


def test_duplicate_uses_real_runner_and_new_cut_and_clip_identities(tl):
    before = {c.id: copy.deepcopy(c.data) for c in tl._cut_clips('c1')}
    call = apply(tl, 'cut.duplicate', 'c1', at=0.0)
    assert call.op == 'cut.duplicate'
    assert 'cut:c1a' in call.created
    assert call.args['id'] == 'c1a'
    assert all(c.id not in before for c in tl._cut_clips('c1a'))
    # Preserve opaque payloads/keyframes as ordinary authored data.
    assert commands.unregistered_mutators([Clip, Cut, Checkout]) == []
    assert tl.cut('c1').picture.id in before


def test_duplicate_is_replayable_and_collision_forks_are_in_id_map(tl):
    base = tl.document()
    call = apply(tl, 'cut.duplicate', 'c1', at=0.0)
    head = tl.document()
    result = rebase([call], base)
    assert not result.conflicts
    assert result.document == head
    forked = rebase([call], head)
    assert not forked.conflicts
    assert forked.id_map['cut:c1a'] == 'cut:c1b'
    assert any('/clip:' in sid for sid in forked.id_map)
    assert {'c1', 'c1a', 'c1b'} <= {g['id'] for g in Checkout(forked.document)._cut_groups()}


def test_detach_audio_keeps_source_identity_range_and_replays(tl):
    pic = tl.cut('c1').picture
    pic.data.update(clipType='media', asset='video', **{'from': 1.0, 'to': 5.0, 'volume': 0.6})
    pic.data.pop('hold', None)
    tl._internal(pic.shot_id)['tracks'].append({'id': 'audio', 'kind': 'audio'})
    tl._registry(pic.shot_id)['video'] = {**copy.deepcopy(tl._registry(pic.shot_id)['P']), 'type': 'video'}
    base, original_id = tl.document(), pic.id
    call = apply(tl, 'clip.detach-audio', pic.id)
    assert call.resolved == (f'occurrence:occ-A/clip:{original_id}',)
    assert len(call.created) == 1
    audio = next(c for c in tl.clips() if c.id == call.created[0].split('/clip:')[1])
    assert audio.is_audio and not intent.cut_of(audio.data)
    assert (audio.data['from'], audio.data['to'], audio.data['volume']) == (1.0, 5.0, 0.6)
    assert tl.clip(original_id).data['volume'] == 0
    result = rebase([call], base)
    assert not result.conflicts
    assert result.document == tl.document()


def test_history_owner_can_join_reservations_without_replacing_content(tl):
    original = tl.document()
    tl.cut('c1').split(2.0)
    restored = retain_cut_reservations(copy.deepcopy(original), tl.document())
    assert restored['shots'] == original['shots']
    assert next_cut_id(restored, 'c1') == 'c1b'


class ReissuedCutId(AssertionError):
    """A cut identity was reissued across a real storage transition."""


# The real history writer retains reservations; keep every transition unmasked.
@pytest.mark.parametrize('seed', range(200))
def test_fixed_seed_full_transition_sequences_do_not_reissue_cut_ids(tl, seed):
    rng = random.Random(20261015 + seed)
    tl.save(quiet=True)
    issued = set()
    # Each sequence contains all required transitions, in a seeded random order
    # after the mandatory initial allocation. A second split after undo must
    # fork the retired reservation rather than issue the same identity again.
    for cycle in range(3):
        new = tl.cut('c1').split(rng.choice([0.5, 1.0, 1.5, 2.0]))
        if new.id in issued:
            raise ReissuedCutId(f'seed={seed}, cycle={cycle}: reissued {new.id}')
        issued.add(new.id)
        tl.save(quiet=True)
        transitions = ['remove', 'save', 'reload', 'merge']
        rng.shuffle(transitions)
        for action in transitions:
            if action == 'remove':
                tl.cut(new.id).lift(layers='remove')
            elif action == 'save':
                tl.save(quiet=True)
            elif action == 'reload':
                # Persist first so this is a real saved content transition.
                tl.save(quiet=True)
                tl = Checkout.load(tl.path)
            else:
                base = tl.document()
                branch = copy.deepcopy(base)
                # The merge branch must allocate above retained numeric reservations.
                fresh_number = cut_reservations(base)['number'] + 1 + rng.randrange(100)
                reserve_cut_id(branch, f'c{fresh_number}')
                merged, conflicts = store.three_way(base, base, branch, whole=True)
                assert not conflicts
                tl.bundle = merged
        tl.save(quiet=True)
        # Restore through actual undo, including the snapshot before allocation.
        tl.undo(len(list(store._history_dir(tl.path).glob('*.json'))))
        tl = Checkout.load(tl.path)


def test_add_cannot_recreate_a_reserved_removed_cut(tl):
    tl.cut('c2').lift(layers='remove')
    before = tl.document()
    with pytest.raises(TimelineEditError, match='reserved'):
        tl.add('am-type', at=5.0, hold=1.0, cut='c2', params={'text': 'synthetic'})
    assert tl.document() == before


def test_duplicate_preserves_keyframes_and_opaque_payloads(tl):
    source = tl.clip('c1.rocket')
    source.data['vendorIdentitySentinel'] = {'id': source.id, 'keep': True}
    source.data['params']['keyframes'] = [{'frame': 0, 'x': 12}, {'frame': 5, 'x': 36}]
    original = copy.deepcopy(source.data)
    apply(tl, 'cut.duplicate', 'c1', at=0.0)
    duplicate = next(c for c in tl._cut_clips('c1a') if intent.layer_of(c.data) == intent.layer_of(source.data))
    assert duplicate.id != source.id
    assert duplicate.params == original['params']
    assert duplicate.data['vendorIdentitySentinel'] == original['vendorIdentitySentinel']
    assert source.data == original


def test_split_fork_producer_reports_the_new_id_and_keeps_head(tl):
    base = tl.document()
    call = apply(tl, 'cut.split', 'c1', on=2.0)
    head = copy.deepcopy(base)
    reserve_cut_id(head, 'c1a')  # head allocated and retired this identity
    result = rebase([call], head)
    assert not result.conflicts
    assert result.id_map == {'cut:c1a': 'cut:c1b'}
    assert 'cut:c1b' in result.plan.calls[0].created
    assert 'c1a' not in [g['id'] for g in Checkout(result.document)._cut_groups()]
    assert head != result.document
    assert head['shots'] == base['shots']


def test_split_fork_rewrites_bound_timerefs_and_owned_preconditions(tl):
    tl.clip('c1.rocket').data['hold'] = 3.0
    base = tl.document()
    split = apply(tl, 'cut.split', 'c1', on=2.0)
    assert intent.until(tl.clip('c1.rocket').data) == 'end of c1a'
    followup = apply(tl, 'clip.set-duration', 'c1.rocket', seconds=1.5)
    head = copy.deepcopy(base)
    reserve_cut_id(head, 'c1a')
    result = rebase([split, followup], head)
    assert not result.conflicts
    assert result.id_map == {'cut:c1a': 'cut:c1b'}
    assert Checkout(result.document).clip('c1.rocket').duration == 1.5


def test_runner_reserves_legacy_cuts_removed_by_direct_ripple_list_edits(tl):
    tl.cut('c1').split(2.0)
    tl.bundle['parent']['config']['app']['astrid'].pop(KEY)
    tl.ripple_delete(2.0, 4.0)
    assert not tl._cut_clips('c1a')
    assert tl.cut('c1').split(1.0).id == 'c1b'


def test_noop_runner_call_does_not_materialize_a_legacy_reservation(tl):
    before = tl.document()
    tl.clip('c1.rocket').set(x=tl.clip('c1.rocket').get('x'))
    assert tl.document() == before
