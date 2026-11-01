"""Registry container guards include their sequential native asset writes."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.inverse import coalesce
from tests.editing_conformance.test_recipe_m1_commands import accepted_boundaries, inverse
from tests.editing_conformance.test_recipe_m2_parent_media import (
    ASSET,
    CHILD,
    args,
    capture,
    parent_inputs,
)


def registry_birth(storage, kind='image'):
    document, _, new, verified = parent_inputs(kind)
    bundle = bundle_from_document(document)
    parent = bundle['parent']
    parent['clips'] = parent['config']['clips'] = []
    parent.pop('registry')
    if storage != 'absent':
        parent['registry'] = {'opaque': [None, False, {}, []]}
    if storage == 'mirrored':
        parent['config']['registry'] = copy.deepcopy(parent['registry'])
    command = capture(bundle, args(new, kind), verified)
    plan = execute(bundle, [command], verified_media=verified)
    head = bundle_from_document(document_from_bundle(plan.bundle))
    return bundle, plan, head


@pytest.mark.parametrize('storage', ['absent', 'direct', 'mirrored'])
def test_direct_container_landing_guard_contains_sequential_asset(storage):
    _, plan, head = registry_birth(storage)
    original = copy.deepcopy(plan.records)
    changes = coalesce(plan.records, State(head))
    direct = [write for write in changes
              if write['ref'] == 'parent' and write['path'][:1] == ['registry']]
    assert len(direct) == 1
    write = direct[0]
    assert write['after'] == State(head).get(write['ref'], write['path'])
    assert plan.records == original


@pytest.mark.parametrize('storage', ['absent', 'direct', 'mirrored'])
@pytest.mark.parametrize('kind', ['image', 'video'])
def test_registry_reload_inverse_redo_retains_every_native_boundary(storage, kind):
    before, plan, head = registry_birth(storage, kind)
    records = copy.deepcopy(plan.records)
    undone = inverse(head, plan.records)
    assert undone.bundle['parent'] == before['parent']
    assert undone.records[0]['writes'] == [
        {**write, 'before': write['after'], 'after': write['before']}
        for write in reversed(plan.records[0]['writes'])]
    redone = inverse(undone.bundle, accepted_boundaries(undone.records, head, undone.bundle))
    assert document_from_bundle(redone.bundle) == document_from_bundle(plan.bundle)
    assert plan.records == records


@pytest.mark.parametrize('storage', ['absent', 'direct', 'mirrored'])
@pytest.mark.parametrize('damage', ['ancestor', 'asset', 'clip', 'foreign', 'order', 'asset-aba',
                                   'ancestor-aba', 'occupied-id', 'source-order', 'source-before'])
def test_registry_inverse_refuses_changed_guards_without_writes(storage, damage):
    _, plan, head = registry_birth(storage)
    records = copy.deepcopy(plan.records)
    history = []
    state = State(head)
    if damage == 'ancestor':
        path = records[0]['writes'][0]['path']
        value = state.get('parent', path)['value']
        state.set('parent', path, present({**value, 'intruder': {'opaque': True}}))
    elif damage == 'asset':
        state.set(ASSET, ['media_id'], present('foreign-media'))
    elif damage == 'clip':
        state.set(CHILD, ['hold'], present(9))
    elif damage == 'foreign':
        state.set('parent/asset:foreign', [], present({'media_id': 'foreign-media', 'type': 'image'}))
    elif damage == 'order':
        state.collection('parent', 'track').reverse()
    elif damage.endswith('-aba'):
        ref, path = (ASSET, ['media_id']) if damage == 'asset-aba' else ('parent', ['registry'])
        value = state.get(ref, path)
        history = [{'writes': [{'ref': ref, 'path': path, 'before': value, 'after': present('away')},
                              {'ref': ref, 'path': path, 'before': present('away'), 'after': value}]}]
    elif damage == 'occupied-id':
        state.collection('parent', 'clip').append(copy.deepcopy(state.entity(CHILD)))
    elif damage == 'source-order':
        writes = records[0]['writes']
        writes[0], writes[-2] = writes[-2], writes[0]
    else:
        records[0]['writes'][-2]['before'] = present({'media_id': 'foreign-media', 'type': 'image'})
    immutable = copy.deepcopy((head, records, history))
    with pytest.raises(CommandError):
        inverse(head, records, history)
    assert (head, records, history) == immutable


def test_direct_registry_inverse_preserves_disjoint_sibling_change():
    before, plan, head = registry_birth('direct')
    State(head).set('parent', ['registry', 'opaque'], present('concurrent'))
    history = [{'writes': [{'ref': 'parent', 'path': ['registry', 'opaque'],
                           'before': present(before['parent']['registry']['opaque']),
                           'after': present('concurrent')}]}]
    immutable = copy.deepcopy(head)
    undone = inverse(head, plan.records, history)
    assert undone.bundle['parent']['registry'] == {'opaque': 'concurrent'}
    assert head == immutable


def test_asset_before_direct_registry_creation_does_not_invent_earlier_alias():
    asset = {'ref': 'parent/asset:a', 'path': [],
             'before': present({'media_id': 'old'}), 'after': present({'media_id': 'new'})}
    registry = {'ref': 'parent', 'path': ['registry'], 'before': ABSENT,
                'after': present({'assets': {'a': {'media_id': 'new'}}})}
    records = [{'writes': [asset]}, {'writes': [registry]}]
    immutable = copy.deepcopy(records)
    assert coalesce(records) == [asset, registry]
    assert records == immutable
