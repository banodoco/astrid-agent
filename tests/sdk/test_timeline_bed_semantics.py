"""D consumes I's exact adopted source contract; synthetic fixtures only."""
import copy
import json

import pytest

from astrid.sdk.timeline import commands, inverse, music_beds
from astrid.sdk.timeline.bed_adoption import adopt_music_bed
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.identity import retain_cut_reservations
from astrid.sdk.timeline.rebase import rebase
from tests.sdk.test_music_beds import enrolled as _enrolled
from tests.sdk.test_music_beds import pinned_music
from tests.sdk.test_timeline_adopt import OWNER, _alignment, _switch, _two_sources, adopt_one
from tests.sdk.test_timeline_owner_modifiers import apply


def enrolled():
    tl = _enrolled()
    retain_cut_reservations(tl.bundle)
    return tl


def multi():
    tl, owners = _two_sources()
    retain_cut_reservations(tl.bundle)
    return Checkout(adopt_music_bed(tl.document(), owners, switches=[_switch()],
                                   alignment=_alignment(owners))), owners


def edit(owner=OWNER, volume=.25):
    return dict(op='clip.set-look', select=owner, args=dict(volume=volume))


def back(before, after, plan):
    return inverse.inverse_command(plan.calls, before, after, 'synthetic-publication',
                                   consequences=plan.consequences)


def test_snapshot_has_one_source_per_owner_and_one_bed_wide_record():
    tl, owners = multi()
    states = commands.entity_states(tl)
    bed = music_beds.adopted(tl)
    metadata = states[OWNER]['music_bed']
    assert 'bed' not in states['parent']['config']['app']['astrid']
    assert 'music_beds' not in states['parent']['config']['app']['astrid']
    assert sum('music_bed' in value for value in states.values()) == 1
    assert metadata['arrangements'] == [dict(id=a['id'], owner=a['owner']) for a in bed['arrangements']]
    assert metadata['switches'] == bed['switches']
    assert metadata['ripple_basis'] == bed['ripple_basis']
    assert metadata['outputs'] == bed['outputs']
    assert metadata['projections'] == {a['id']: {k: a[k] for k in ('asset', 'source_offset_s', 'rate', 'gain')}
                                       for a in bed['arrangements']}
    for arrangement in bed['arrangements']:
        sid = arrangement['owner']
        assert commands.state_at(states, sid, '')['value'] == arrangement['source']
        assert commands.authored_owner(tl, sid).data is music_beds.sources(tl)[sid]['source']
        assert metadata['projection'][arrangement['id']] == {c['id']: c for c in tl._internal('A')['clips']
                                                            if c['id'] in bed['outputs'][arrangement['id']]}
    # No nested source copies or per-arrangement basis in the semantic metadata.
    assert '"source":' not in json.dumps(metadata)
    assert json.dumps(metadata).count('ripple_basis') == 1
    assert 'music_bed' not in states[owners[1]]


def test_multi_source_edit_records_authored_gain_and_projection_consequences_once():
    tl, owners = multi()
    before = tl.document()
    after, plan = apply(before, edit(owners[1]))
    writes = {(w['id'], w['path']) for w in plan.direct_writes}
    assert writes == {(owners[1], 'volume')}
    assert plan.calls[0].resolved == (owners[1],)
    assert f'{OWNER}::music_bed.ripple_basis' in plan.calls[0].expect
    assert any(w['id'] == OWNER and w['path'].startswith('music_bed.projections.') for w in plan.consequences)
    assert all(w['id'] != 'parent' for w in (*plan.direct_writes, *plan.consequences))
    again, replay = apply(before, *[c.to_json() for c in plan.calls])
    assert again == after
    assert replay.direct_writes == plan.direct_writes
    # Replay onto an already changed value conflicts; it cannot apply twice.
    result = rebase(plan.calls, after)
    assert result.conflicts and result.document is None
    restored, undo = apply(after, back(before, after, plan))
    assert restored == before
    redone, _ = apply(restored, back(after, restored, undo))
    assert redone == after


def test_zero_output_owner_edits_replays_and_inverts_without_generated_identity():
    tl = enrolled()
    tl.ripple_delete(0, 9)
    tl = adopt_one(tl)
    before = tl.document()
    assert music_beds.sources(tl)[OWNER]['outputs'] == []
    after, plan = apply(before, edit())
    assert plan.calls[0].resolved == (OWNER,)
    assert music_beds.sources(Checkout(after))[OWNER]['outputs'] == []
    assert apply(before, *plan.calls)[0] == after
    assert apply(after, back(before, after, plan))[0] == before


def test_repeated_occurrence_source_identity_isolated_through_replay_inverse():
    tl = adopt_one(pinned_music(), 'occurrence:first/clip:bed')
    retain_cut_reservations(tl.bundle)
    before = tl.document()
    second = copy.deepcopy(tl.clip('occurrence:second/clip:bed').data)
    after, plan = apply(before, edit('occurrence:first/clip:bed'))
    assert Checkout(after).clip('occurrence:second/clip:bed').data == second
    assert apply(before, *plan.calls)[0] == after
    assert apply(after, back(before, after, plan))[0] == before


@pytest.mark.parametrize('damage', ['duplicate-owner', 'duplicate-authority', 'projection'])
def test_malformed_ownership_or_projection_refuses_atomically(damage):
    tl, owners = multi()
    bed = music_beds.adopted(tl)
    if damage == 'duplicate-owner':
        bed['arrangements'][1]['owner'] = OWNER
    elif damage == 'duplicate-authority':
        music_beds.collection(tl, create=True)[OWNER] = dict(music_beds.sources(tl)[OWNER])
    else:
        bed['arrangements'][0]['gain'] = .125
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(commands.TimelineEditError):
        commands.run(tl, edit())
    assert tl.bundle == before
    assert not tl._journal


@pytest.mark.parametrize('stale', ['source', 'basis'])
def test_stale_source_or_shared_basis_conflicts_without_head_mutation(stale):
    tl = adopt_one(enrolled())
    before = tl.document()
    _, plan = apply(before, edit())
    head = Checkout(copy.deepcopy(before))
    if stale == 'source':
        head, _ = apply(head.document(), edit(volume=.5))
    else:
        music_beds.adopted(head)['ripple_basis'] = []
        music_beds.project(head)
        head = head.document()
    original = copy.deepcopy(head)
    result = rebase(plan.calls, head)
    assert result.conflicts and result.document is None
    assert head == original
    assert result.conflicts[0]['path'] == ('volume' if stale == 'source' else 'music_bed.ripple_basis')


def test_transfer_uses_existing_adopt_command_one_history_step_replay_undo_redo(tmp_path):
    tl = enrolled()
    path = tmp_path / 'source.json'
    tl.save(path, quiet=True)
    before = tl.document()
    plan = commands.run(tl, dict(op='timeline.adopt', select=None, args=dict(sources=[OWNER], alignment=_alignment([OWNER]))))
    after = tl.document()
    assert len(plan.calls) == 1 and plan.calls[0].op == 'timeline.adopt'
    assert not music_beds.collection(tl)
    assert music_beds.adopted(tl)
    assert all(w['id'] != 'parent' for w in (*plan.direct_writes, *plan.consequences))
    assert any(w['path'] == 'music_bed.arrangements' for w in plan.direct_writes)
    assert any(w['path'].startswith('music_bed.projection') for w in plan.consequences)
    assert apply(before, *plan.calls)[0] == after
    assert rebase(plan.calls, after).conflicts
    inverse_call = back(before, after, plan)
    restored, undo = apply(after, inverse_call)
    assert restored == before
    assert apply(restored, back(after, restored, undo))[0] == after
    assert len(tl.undo()) == 1
    assert tl.document() == before
    assert len(tl.redo()) == 1
    assert tl.document() == after


def test_legacy_carrier_consumer_still_records_source_and_projection():
    tl = enrolled()
    states = commands.entity_states(tl)
    assert states[OWNER]['music_bed']['outputs'] == ['a-music', 'a-music:b']
    before = tl.document()
    after, plan = apply(before, edit())
    assert apply(before, *plan.calls)[0] == after
    assert apply(after, back(before, after, plan))[0] == before


def test_multi_source_transfer_guards_all_sources_and_switch_bindings():
    tl, owners = _two_sources()
    retain_cut_reservations(tl.bundle)
    before = tl.document()
    args = dict(sources=owners, alignment=_alignment(owners), switches=[_switch()])
    after, plan = apply(before, dict(op='timeline.adopt', select=None, args=args))
    assert plan.calls[0].resolved == (OWNER,)
    assert commands.state_at(commands.entity_states(Checkout(before)), owners[1], '') == plan.calls[0].expect[f'{owners[1]}::']
    assert apply(before, *plan.calls)[0] == after
    restored, undo = apply(after, back(before, after, plan))
    assert restored == before
    assert apply(restored, back(after, restored, undo))[0] == after
    head, _ = apply(before, edit(owners[1]))
    assert rebase(plan.calls, head).conflicts


def test_transfer_and_source_edit_are_one_atomic_transaction():
    tl = enrolled()
    before = tl.document()
    args = dict(sources=[OWNER], alignment=_alignment([OWNER]))
    after, plan = apply(before, dict(op='timeline.adopt', select=None, args=args), edit())
    assert len(plan.calls) == 2
    assert apply(before, *plan.calls)[0] == after
    assert apply(after, back(before, after, plan))[0] == before
    current = Checkout(copy.deepcopy(before))
    with pytest.raises(commands.TimelineEditError):
        with current.edit('refuse together', quiet=True):
            commands.execute(current, dict(op='timeline.adopt', select=None, args=args))
            commands.execute(current, edit(volume=-2))
    assert current.document() == before
    assert not current._journal


def test_adopted_ripple_restores_one_common_basis_through_recorded_inverse():
    tl = adopt_one(enrolled())
    retain_cut_reservations(tl.bundle)
    before = tl.document()
    after, plan = apply(before, dict(op='timeline.insert-time', select=None, args=dict(at=2, seconds=1)))
    assert music_beds.adopted(Checkout(after))['ripple_basis'] == [dict(at_s=2., delta_s=2.)]
    assert apply(before, *plan.calls)[0] == after
    restored, undo = apply(after, back(before, after, plan))
    assert restored == before
    assert apply(restored, back(after, restored, undo))[0] == after


def test_runtime_provider_admits_local_transfer_and_keeps_external_adoption_closed():
    from astrid.sdk.timeline.commit import RuntimeCommitService, _Refusal
    args = dict(sources=[OWNER], alignment=_alignment([OWNER]))
    RuntimeCommitService._check_calls([dict(op='timeline.adopt', select=None, args=args)])
    for extra in (dict(timeline='other'), dict(revision_id='revision'), dict(client={})):
        with pytest.raises(_Refusal, match='admitted scoped'):
            RuntimeCommitService._check_calls([dict(op='timeline.adopt', select=None, args={**args, **extra})])


def test_runtime_provider_records_transfer_source_edit_and_inverse_on_pinned_fixture():
    from pathlib import Path

    from astrid.sdk.timeline.commit import RuntimeCommitService
    before = Checkout(json.loads((Path(__file__).parents[1] / 'fixtures/timeline_editing/tiny_publishable.json').read_text())).document()
    retain_cut_reservations(before)
    transfer = dict(op='timeline.adopt', select=None, args=dict(sources=[OWNER], alignment=_alignment([OWNER])))
    after, plan, _ = RuntimeCommitService._apply(before, [transfer, edit()])
    assert len(plan.calls) == 2
    assert RuntimeCommitService._apply(before, plan.calls)[0] == after
    assert RuntimeCommitService._apply(after, [back(before, after, plan)])[0] == before


def test_stale_occurrence_requires_explicit_identity_map_and_preserves_opaque_source():
    from pathlib import Path
    tl = adopt_one(Checkout(json.loads((Path(__file__).parents[1] / 'fixtures/timeline_editing/tiny_publishable.json').read_text())))
    music_beds.sources(tl)[OWNER]['source']['opaque'] = OWNER
    music_beds.project(tl)
    before = tl.document()
    _, plan = apply(before, edit())
    head = Checkout(copy.deepcopy(before))
    new = 'occurrence:renamed/clip:a-music'
    next(r for r in head.bundle['placements'] if r['occurrence_id'] == 'occ-A')['occurrence_id'] = 'renamed'
    for name in ('base_placements',):
        row = head.bundle[name].pop('occ-A')
        row['occurrence_id'] = 'renamed'
        head.bundle[name]['renamed'] = row
    row = head.bundle['source_mapping']['placements'].pop('occ-A')
    row['occurrence_id'] = 'renamed'
    head.bundle['source_mapping']['placements']['renamed'] = row
    bed = music_beds.adopted(head)
    bed['owner'] = new
    bed['arrangements'][0]['owner'] = new
    exact_head = head.document()
    assert rebase(plan.calls, exact_head).conflicts
    result = rebase(plan.calls, exact_head, id_map={OWNER: new})
    assert not result.conflicts and result.id_map == {OWNER: new}
    assert music_beds.sources(Checkout(result.document))[new]['source']['opaque'] == OWNER
    assert music_beds.sources(Checkout(result.document))[new]['source']['volume'] == .25
    assert head.document() == exact_head


def test_transfer_records_switch_timeref_binding_and_refuses_missing_anchor_atomically():
    tl, owners = _two_sources()
    words = tl.clip('a-vo').data['app']['astrid']['words']
    words.append([5.9, 6.1, 'SwitchWord'])
    query = '"SwitchWord" in s1 #1'
    before = tl.document()
    call = dict(op='timeline.adopt', select=None, args=dict(sources=owners,
                alignment=_alignment(owners), switches=[_switch(query, 2)]))
    after, plan = apply(before, call)
    assert plan.calls[0].bindings[query].startswith('word:')
    assert apply(before, *plan.calls)[0] == after
    stale = Checkout(copy.deepcopy(before))
    stale.clip('a-vo').data['app']['astrid']['words'].pop()
    original = stale.document()
    with pytest.raises(commands.TimelineEditError):
        commands.run(stale, plan.calls[0])
    assert stale.document() == original and not stale._journal


def test_multi_arrangement_ripple_inverse_and_redo_keep_common_basis_once():
    tl, _ = multi()
    before = tl.document()
    after, plan = apply(before, dict(op='timeline.insert-time', select=None, args=dict(at=6, seconds=1)))
    assert music_beds.adopted(Checkout(after))['ripple_basis'] == [dict(at_s=6., delta_s=1.)]
    restored, undo = apply(after, back(before, after, plan))
    assert restored == before
    assert apply(restored, back(after, restored, undo))[0] == after


def test_ripple_that_invalidates_switch_coverage_refuses_the_entire_edit():
    tl, _ = multi()
    before = tl.document()
    with pytest.raises(music_beds.MusicBedError, match='outside adjacent source coverage'):
        commands.run(tl, dict(op='timeline.insert-time', select=None, args=dict(at=2, seconds=1)))
    assert tl.document() == before and not tl._journal
