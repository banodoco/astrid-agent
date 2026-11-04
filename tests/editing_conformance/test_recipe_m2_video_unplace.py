"""Video unplace uses raw occurrence ownership and the existing guarded inverse/pool."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._recording import Recording
from astrid.sdk.timeline.commands import execute as _execute
from astrid.sdk.timeline.commands import validate_command
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_auto_placement import unplace_shared
from astrid.sdk.timeline.native_placement_commands import capture_placement_unplace
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.m2_authenticated_redo import AuthenticatedRedo
from tests.editing_conformance.test_recipe_m1_commands import inverse as _inverse
from tests.editing_conformance.test_recipe_m1_commands import later
from tests.editing_conformance.test_recipe_m2_shared_auto_placement import CHILD, OWNER, build
from tests.editing_conformance.test_recipe_m2_shot_pool import pins, step
from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    runtime_abi,  # noqa: F401 -- detached ABI fixture
)
from tests.sdk.test_timeline_commit_provider import inverse as inverse_call

_AUTHENTICATED_REDO = AuthenticatedRedo(_execute, _inverse)
execute = _AUTHENTICATED_REDO.execute
inverse = _AUTHENTICATED_REDO.inverse
accepted_boundaries = _AUTHENTICATED_REDO.accepted_boundaries

FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_video_unplace.json'


def placed_case(kind='video', *, trimmed=False, pooled=False):
    _, bundle, result, verified = build(kind)
    placed = execute(bundle, result['commands'], verified_media=verified).bundle
    state = State(placed)
    row = state.entity(CHILD)
    row['opaque'] = {'literal': CHILD, 'clear': None, 'values': [False, 0, {}]}
    if trimmed:
        row.update({'from': 2, 'to': 6, 'hold': 4, 'speed': 2})
    # Keep the removed row between surviving rows to exercise the inverse's order.
    clips = state.config(OWNER)['clips']
    clips.insert(1, clips.pop())
    state.entity(OWNER + '/registry')['assets']['reserved-new']['opaque'] = {'keep': [None, False]}
    if pooled:
        _, _, [_, b, c], extra = pins(kind)
        verified += extra[2:]
        for pin in (b, c):
            placed = step(placed, 'add', pin, verified)[1].bundle
    return placed, verified


def removal(bundle, verified, keep=False):
    return unplace_shared(bundle, OWNER, 'placed', ownership='occurrence', keep_as_pooled=keep,
                          media=verified[1]['selection'], verified_media=verified)['commands']


@pytest.mark.parametrize('trimmed', [False, True])
@pytest.mark.parametrize('keep', [False, True])
def test_raw_media_golden_exact_inverse_replay_and_redo(trimmed, keep):
    bundle, verified = placed_case(trimmed=trimmed, pooled=True)
    before = copy.deepcopy(bundle)
    commands = removal(bundle, verified, keep)
    plan = execute(bundle, commands, verified_media=verified)
    for command in commands:
        validate_command(command)
    assert json.loads(json.dumps(commands, allow_nan=False)) == commands
    assert plan.commands == commands and bundle == before
    assert commands[0]['args'] == {} and commands[0]['created'] == []
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    state, original = State(plan.bundle), State(bundle)
    assert state.config(OWNER)['output']['fps'] == {'numerator': 30000, 'denominator': 1001}
    assert state.entity(OWNER + '/registry') == original.entity(OWNER + '/registry')
    assert plan.bundle['parent'] == bundle['parent']
    assert state.config('occurrence:occ-b') == original.config('occurrence:occ-b')
    assert [row['id'] for row in state.collection(OWNER, 'clip')] == ['clip-a', 'clip-b', 'clip-c']
    assert state.config(OWNER)['app']['astrid']['pool'] == ['pool-b', 'pool-c'] + (['reserved-new'] if keep else [])
    golden = json.loads(FIXTURE.read_text())[f'{trimmed}-{keep}']
    assert golden == {'row': original.entity(CHILD), 'registry': state.entity(OWNER + '/registry'),
                      'writes': [record['writes'] for record in plan.records],
                      'order': [row['id'] for row in state.collection(OWNER, 'clip')]}
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    redone = inverse(undone.bundle, accepted_boundaries(undone.records, plan.bundle, undone.bundle))
    assert redone.bundle == plan.bundle


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('keep', [False, True])
def test_provider_round_trip_preserves_opaque_registry_and_order(kind, keep):
    bundle, verified = placed_case(kind, pooled=True)
    document = document_from_bundle(bundle)
    bundle = bundle_from_document(document)
    realm = MediaRealm(document, verified)
    removed = realm.call(removal(bundle, verified, keep))
    entry = copy.deepcopy(realm.entries[-1])
    proofs = [binding for record in entry['records']['commands'] for binding in record['bindings']
              if binding['key'] == 'commit.materialization']
    assert {binding['physical_ref'].split(':', 1)[0] for binding in proofs} == {
        'shot-revision', 'internal-revision'}
    if keep:
        # The pooled step consumes an intermediate physical image; its inverse
        # must authenticate that image's continuation through the native edit.
        for damage in ('missing', 'successor'):
            realm.entries[-1] = copy.deepcopy(entry)
            for record in realm.entries[-1]['records']['commands']:
                if damage == 'missing':
                    record['bindings'] = [binding for binding in record['bindings']
                                          if binding['key'] != 'commit.materialization']
                else:
                    for binding in record['bindings']:
                        if binding['key'] == 'commit.materialization':
                            binding['successor'] = binding['physical_ref']
            before = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted))
            with pytest.raises(CommitRejected) as error:
                realm.call([inverse_call([removed['receipt']['receipt_id']])])
            assert error.value.failure['details']['reason'] == 'history_unavailable'
            assert (realm.head, realm.entries, realm.documents, realm.accepted) == before
        realm.entries[-1] = entry
    undone = realm.call([inverse_call([removed['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == document
    redone = realm.call([inverse_call([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == removed['data']['canonical_document']


@pytest.mark.parametrize('field,value', [('from', 1), ('to', 3), ('speed', 2), ('hold', 3),
                                       ('opaque', {'changed': True})])
def test_full_raw_source_row_is_guarded_and_aba_blocks_undo(field, value):
    bundle, verified = placed_case()
    plan = execute(bundle, removal(bundle, verified))
    head = copy.deepcopy(bundle)
    State(head).entity(CHILD)[field] = value
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head)
    assert head == before
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later(CHILD, [field], State(bundle).entity(CHILD)[field])])


@pytest.mark.parametrize('field,value', [('media_id', 'sha256:' + '9' * 64),
    ('generationId', 'changed'), ('variantId', 'changed'), ('opaque', {'changed': True})])
def test_exact_registry_binding_and_aba_are_guarded(field, value):
    bundle, verified = placed_case()
    plan = execute(bundle, removal(bundle, verified))
    head = copy.deepcopy(bundle)
    State(head).entity(OWNER + '/registry')['assets']['reserved-new'][field] = value
    with pytest.raises(CommandError):
        replay(plan, head)
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later(OWNER + '/source',
            ['registry', 'assets', 'reserved-new', field], value)])


def test_replay_refuses_shared_owner_and_order_changes_but_preserves_unrelated_edits():
    bundle, verified = placed_case()
    plan = execute(bundle, removal(bundle, verified))
    for change in ('owner', 'order'):
        head = copy.deepcopy(bundle)
        if change == 'owner':
            head['placements'][1]['shot_id'] = head['placements'][0]['shot_id']
        else:
            State(head).config(OWNER)['clips'].reverse()
        with pytest.raises(CommandError):
            replay(plan, head)
    head = copy.deepcopy(bundle)
    State(head).entity('occurrence:occ-b/clip:clip-a')['at'] = 3
    head['parent']['name'] = 'Unrelated rename'
    rebased = replay(plan, head)
    assert inverse(rebased.bundle, rebased.records).bundle == head
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later(OWNER + '/collection:clip', [], [])])


@pytest.mark.parametrize('owner', ['parent', OWNER, 'occurrence:occ-b'])
@pytest.mark.parametrize('dependency', ['clip', 'config'])
def test_real_dependency_in_any_scope_refuses_before_writing(owner, dependency, monkeypatch):
    bundle, verified = placed_case()
    if dependency == 'clip':
        State(bundle).entity(owner + '/clip:clip-a')['app']['astrid'] = {'until': CHILD}
        if owner == 'parent':
            bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])
    else:
        State(bundle).config(owner)['app'] = {'astrid': {'sequences': [{'clips': [CHILD]}]}}
    before = copy.deepcopy(bundle)
    monkeypatch.setattr(Recording, 'assign', lambda *a, **kw: pytest.fail('wrote before dependency refusal'))
    with pytest.raises(CommandError, match='semantic dependencies'):
        removal(bundle, verified)
    assert bundle == before


@pytest.mark.parametrize('identity', ['occ-a::placed', 'occ-a:placed', CHILD, 'absent'])
def test_display_ids_are_never_inferred(identity):
    bundle, _ = placed_case()
    with pytest.raises(CommandError):
        capture_placement_unplace(bundle, OWNER, identity, ownership='occurrence')


@pytest.mark.parametrize('damage', ['still-placed', 'wrong-object', 'wrong-generation', 'wrong-variant',
                                    'wrong-fingerprint', 'unverified'])
def test_keep_as_pooled_requires_exact_selected_media_and_no_surviving_placement(damage):
    bundle, verified = placed_case()
    pin = copy.deepcopy(verified[1]['selection'])
    if damage == 'still-placed':
        State(bundle).config(OWNER)['clips'].append(dict(State(bundle).entity(CHILD), id='another'))
    elif damage == 'unverified':
        verified = []
    else:
        key = {'wrong-object': 'object_id', 'wrong-generation': 'generation_id',
               'wrong-variant': 'variant_id', 'wrong-fingerprint': 'catalog_digest'}[damage]
        pin[key] = 'sha256:' + '9' * 64 if key in ('object_id', 'catalog_digest') else 'different'
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        unplace_shared(bundle, OWNER, 'placed', ownership='occurrence', keep_as_pooled=True,
                       media=pin, verified_media=verified)
    assert bundle == before


def test_keep_as_pooled_replay_guards_membership_binding_and_surviving_placements():
    bundle, verified = placed_case(pooled=True)
    plan = execute(bundle, removal(bundle, verified, True), verified_media=verified)
    for change in ('pool', 'placed', 'binding'):
        head = copy.deepcopy(bundle)
        state = State(head)
        if change == 'pool':
            state.config(OWNER)['app']['astrid']['pool'].reverse()
        elif change == 'placed':
            state.entity(OWNER + '/clip:clip-b')['asset'] = 'reserved-new'
        else:
            state.entity(OWNER + '/registry')['assets']['reserved-new']['variantId'] = 'changed'
        with pytest.raises(CommandError):
            replay(plan, head, verified_media=verified)
