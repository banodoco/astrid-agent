"""Historical fork reads use their own authenticated input without relaxing State."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline import inverse as inverse_module
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commit import _digest, _manifest
from tests.editing_conformance.test_doc_patch_v2 import step, submit
from tests.editing_conformance.test_provider_occurrence_fork import fork
from tests.editing_conformance.test_recipe_occurrence_fork import fork_document
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi

OWNER = 'occurrence:occ-0'


def document():
    value = fork_document()
    shot, = value['shot_revisions']
    shot['payload']['items'].append({**copy.deepcopy(shot['payload']['items'][0]), 'item_id': 'second'})
    shot['content_digest'] = _digest(shot['payload'])
    value['dependency_manifest'] = _manifest(value['shot_revisions'], value['internal_timeline_revisions'],
                                            value['dependency_manifest']['media'], [])
    return value


@pytest.mark.parametrize('count', [1, 2])
@pytest.mark.parametrize('dry_run', [False, True])
def test_production_undo_authenticates_restored_source_without_changing_publication(
        monkeypatch, request, count, dry_run):
    baseline = fork_document(count=count)
    realm = DetachedRealm(baseline)
    call = fork(realm)
    created = submit(realm, call, reads=[], steps=[step('fork', 0, call)])
    accepted = created['data']['canonical_document']
    captured = []
    validate = inverse_module._validate_inputs

    def observe(state, record, records):
        if record['command']['op'] == 'occurrence.fork':
            before, after = records.load_boundary(records.entries[0])
            original = State(before)
            shot = original.occurrence(OWNER)['shot_id']
            assert state.occurrence(OWNER)['shot_id'] == shot
            assert state.bundle['source_mapping']['shots'][shot] == before['source_mapping']['shots'][shot]
            assert after['source_mapping']['_runtime_document'] == accepted
            assert state.bundle['source_mapping']['_runtime_document'] == accepted
            # Ordinary state still refuses this historical source/publication pair.
            with pytest.raises(CommandError, match='shot item source changed'):
                state.get(OWNER + '/shot', ['payload'])
            snapshot = copy.deepcopy(state.bundle)
            validate(state, record, records)
            assert state.bundle == snapshot
            captured.append({'publication_id': records.entries[0]['publication_id'],
                'command_ordinal': record['ordinal'], 'source': before['source_mapping']['shots'][shot],
                'input_revision': baseline['parent_revision']['revision_id'],
                'accepted_revision': accepted['parent_revision']['revision_id']})
            return
        if record['command']['op'] == 'timeline.inverse':
            shot = state.occurrence(OWNER)['shot_id']
            assert shot == 'shot-fork' and shot not in state.bundle['source_mapping']['shots']
            assert state.bundle['shots'][shot]['_created'] is True
            snapshot = copy.deepcopy(state.bundle)
            validate(state, record, records)
            assert state.bundle == snapshot
            return
        validate(state, record, records)

    monkeypatch.setattr(inverse_module, '_validate_inputs', observe)
    head = copy.deepcopy((realm.head, realm.entries))
    undone = submit(realm, inverse([created['receipt']['receipt_id']]), reads=[], dry_run=dry_run)
    assert captured
    request.node.user_properties.append(('authenticated_source_boundary', json.dumps(captured, sort_keys=True)))
    assert undone['data']['canonical_document'] == baseline
    if dry_run:
        assert undone['receipt'] is None and undone['data']['would_status'] == 'accepted'
        assert (realm.head, realm.entries) == head
    else:
        redone = submit(realm, inverse([undone['receipt']['receipt_id']]), reads=[])
        assert redone['data']['canonical_document'] == accepted


@pytest.mark.parametrize('damage', ['shot_id', 'revision_id', 'content_digest', 'internal_timeline_id',
    'internal_timeline_revision_id', 'internal_timeline_content_digest', 'foreign-map', 'empty-map',
    'extra-map', 'nonbijective', 'wrong-owner', 'missing-item', 'duplicate-item', 'order'])
def test_production_historical_read_refuses_corruption_atomically(monkeypatch, damage):
    realm = DetachedRealm(document())
    created = submit(realm, fork(realm), reads=[])
    head = copy.deepcopy((realm.head, realm.entries, realm.documents))
    validate = inverse_module._validate_inputs
    corrupted = []

    def corrupt(state, record, records):
        if record['command']['op'] == 'occurrence.fork':
            shot = state.occurrence(OWNER)['shot_id']
            source = state.bundle['source_mapping']['shots'][shot]
            rows = state.bundle['shots'][shot]['payload']['items']
            if damage == 'foreign-map':
                sibling = state.occurrence('occurrence:occ-1')['shot_id']
                source['item_id_map'] = copy.deepcopy(state.bundle['source_mapping']['shots'][sibling]['item_id_map'])
            elif damage == 'empty-map':
                source['item_id_map'] = {}
            elif damage == 'extra-map':
                source['item_id_map']['foreign'] = 'unbound'
            elif damage == 'nonbijective':
                source['item_id_map']['second'] = source['item_id_map']['item-1']
            elif damage == 'wrong-owner':
                state.occurrence(OWNER)['shot_id'] = state.occurrence('occurrence:occ-1')['shot_id']
            elif damage == 'missing-item':
                rows.pop()
            elif damage == 'duplicate-item':
                rows[1]['item_id'] = rows[0]['item_id']
            elif damage == 'order':
                rows.reverse()
            else:
                source[damage] = 'unverified'
            corrupted.append(damage)
        validate(state, record, records)

    monkeypatch.setattr(inverse_module, '_validate_inputs', corrupt)
    with pytest.raises(CommitRejected):
        submit(realm, inverse([created['receipt']['receipt_id']]), reads=[])
    assert corrupted == [damage]
    assert (realm.head, realm.entries, realm.documents) == head


def test_historical_read_requires_accepted_boundary(monkeypatch):
    realm = DetachedRealm(fork_document())
    created = submit(realm, fork(realm), reads=[])
    validate = inverse_module._validate_inputs
    checked = []

    def without_boundary(state, record, records):
        if record['command']['op'] == 'occurrence.fork':
            with pytest.raises(CommandError, match='shot item source changed'):
                validate(state, record, list(records))
            with pytest.raises(CommandError, match='shot item source changed'):
                validate(state, copy.deepcopy(record), records)
            checked.append(True)
        validate(state, record, records)

    monkeypatch.setattr(inverse_module, '_validate_inputs', without_boundary)
    undone = submit(realm, inverse([created['receipt']['receipt_id']]), reads=[])
    assert checked and undone['data']['canonical_document'] == fork_document()


@pytest.mark.parametrize('damage', ['not-created', 'content', 'missing-item', 'item-order', 'foreign-item'])
def test_production_redo_codec_requires_exact_recorded_root(monkeypatch, damage):
    realm = DetachedRealm(document())
    created = submit(realm, fork(realm), reads=[])
    undone = submit(realm, inverse([created['receipt']['receipt_id']]), reads=[])
    head = copy.deepcopy((realm.head, realm.entries, realm.documents))
    validate = inverse_module._validate_inputs
    corrupted = []

    def corrupt(state, record, records):
        assert record['command']['op'] == 'timeline.inverse'
        shot = state.occurrence(OWNER)['shot_id']
        assert shot == 'shot-fork' and shot not in state.bundle['source_mapping']['shots']
        root = state.bundle['shots'][shot]
        rows = root['payload']['items']
        if damage == 'not-created':
            root['_created'] = False
        elif damage == 'content':
            root['payload']['name'] = 'unrecorded'
        elif damage == 'missing-item':
            rows.pop()
        elif damage == 'item-order':
            rows.reverse()
        else:
            rows[0]['item_id'] = 'unrecorded'
        corrupted.append(damage)
        validate(state, record, records)

    monkeypatch.setattr(inverse_module, '_validate_inputs', corrupt)
    with pytest.raises(CommitRejected):
        submit(realm, inverse([undone['receipt']['receipt_id']]), reads=[])
    assert corrupted == [damage]
    assert (realm.head, realm.entries, realm.documents) == head
