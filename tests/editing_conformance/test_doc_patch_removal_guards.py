"""Emitted member removals retain item, shape and authenticated inverse guards."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.timeline.inverse import check_intervening
from astrid.sdk.timeline.validation import validate_final_candidate
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def document(mirror='both', app='opaque'):
    result = document_fixture()
    parent = result['parent_revision']['payload']
    for rows in (parent['clips'], parent['config']['clips']):
        rows.append({**copy.deepcopy(rows[1]), 'id': 'clip-c', 'at': 8})
        if app == 'absent':
            rows[0].pop('app')
        elif app == 'null':
            rows[0]['app'] = None
    if mirror == 'config':
        parent.pop('clips')
    elif mirror == 'payload':
        parent['config'].pop('clips')
    result['parent_revision']['content_digest'] = _digest(parent)
    return result


def request(bundle, calls, *, reads=True):
    result = {
        'api_version': 1, 'project_id': bundle['project_id'], 'timeline_id': bundle['timeline_id'],
        'base_revision_id': bundle['base_parent']['revision_id'],
        'base_publication_id': 'compiler-input', 'commands': calls, 'label': 'Remove member',
        'steps': [step('edit' if len(calls) == 1 else 'edit-' + str(ordinal), ordinal, call, reads=[])
                  for ordinal, call in enumerate(calls)],
        'merge': 'fields', 'dry_run': False, 'source': {'via': 'python'},
        'rationale': 'Preserve unrelated authored rows', 'group_id': 'removal-guards',
    }
    if reads:
        result['reads'] = []
    return result


def compile_edit(before, after):
    calls = emit_document_commands(before, after)
    controls = request(before, calls)
    plan = execute(before, calls, commit_request=controls, evaluated_head={
        'revision_id': controls['base_revision_id'],
        'publication_id': controls['base_publication_id'],
    })
    validate_final_candidate(plan.bundle, baseline=before)
    return plan


def remove_call(bundle, path=None, ref='parent'):
    path = path or ['clips']
    rows = Closure(bundle).payload(Closure(bundle).resolve(ref))
    for token in path:
        rows = rows[token]
    return patch(change(path, present(rows[0]), op='list.remove', id=rows[0]['id'],
                        order_before=[row['id'] for row in rows], ref=ref))


def send(realm, *calls, **controls):
    bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    options = request(bundle, list(calls))
    for key in ('api_version', 'project_id', 'timeline_id', 'base_revision_id',
                'base_publication_id', 'commands'):
        options.pop(key)
    options.update(controls)
    return submit(realm, *calls, **options)


@pytest.mark.parametrize('scope', ['both', 'config', 'payload', 'shared', 'tracks'])
def test_emitted_removal_allows_disjoint_surviving_row_guard(scope):
    original = bundle_from_document(document(scope if scope in ('config', 'payload') else 'both'))
    after = copy.deepcopy(original)
    owner, kind = ('occurrence:occ-a', 'clip') if scope == 'shared' else ('parent', 'clip')
    if scope == 'tracks':
        kind = 'track'
        original['parent']['config']['tracks'].extend([
            {'id': 'unused-a', 'kind': 'audio'}, {'id': 'unused-b', 'kind': 'audio'}])
        after = copy.deepcopy(original)
    rows = State(after).collection(owner, kind)
    removed = copy.deepcopy(rows.pop(1 if scope == 'tracks' else 0))
    if scope == 'shared':
        for placement in after['placements'][1:]:
            after['shots'][placement['shot_id']]['internal_timeline']['clips'] = copy.deepcopy(rows)
    if owner == 'parent' and kind == 'clip' and scope == 'both':
        after['parent']['config']['clips'] = copy.deepcopy(rows)
    plan = compile_edit(original, after)
    assert plan.commands[0]['args']['changes'][0]['op'] == 'list.remove'
    peer_before = bundle_from_document(document_from_bundle(plan.bundle))
    peer_after = copy.deepcopy(peer_before)
    survivor = State(peer_after).collection(owner, kind)[-1]
    survivor['opaque'] = {'literal': 'later author', 'null': None, 'values': [False, 0]}
    if scope == 'shared':
        for placement in peer_after['placements'][1:]:
            peer_after['shots'][placement['shot_id']]['internal_timeline']['clips'] = copy.deepcopy(
                State(peer_after).collection(owner, kind))
    if owner == 'parent' and kind == 'clip' and scope == 'both':
        peer_after['parent']['config']['clips'] = copy.deepcopy(peer_after['parent']['clips'])
    peer = compile_edit(peer_before, peer_after)
    check_intervening(plan.records, peer.records, state=State(peer.bundle), include_dependents=True)
    writes = plan.records[0]['writes']
    assert all(write['path'] == [] and write['before'] == present(removed)
               and write['after'] == ABSENT for write in writes)
    assert len(writes) == (2 if scope == 'shared' else 1)
    assert all(write['ref'].endswith('/' + kind + ':' + removed['id']) for write in writes)
    assert original != after


@pytest.mark.parametrize('mirror', ['both', 'config', 'payload'])
@pytest.mark.parametrize('app', ['absent', 'null', 'opaque'])
def test_provider_emitted_removal_undo_redo_preserves_surviving_fields(mirror, app):
    original = document(mirror, app)
    realm = DetachedRealm(original)
    before = bundle_from_document(original)
    after = copy.deepcopy(before)
    rows = State(after).collection('parent', 'clip')
    rows.pop(0)
    if mirror == 'both':
        after['parent']['config']['clips'] = copy.deepcopy(rows)
    calls = emit_document_commands(before, after)
    landed = send(realm, *calls)
    entry = copy.deepcopy(realm.entries[-1])
    assert landed['data']['canonical_document'] == document_from_bundle(compile_edit(before, after).bundle)
    assert entry['receipt'] == landed['receipt']
    assert entry['publisher'] == 'actor-1'
    assert entry['records']['steps'][0]['patch'][0]['op'] == 'list.remove'
    record = entry['records']['commands'][0]
    binding = next(binding for binding in record['bindings'] if binding['key'] == 'doc.patch:0')
    assert binding['write_range'] == [0, 1]
    assert binding['change'] == calls[0]['args']['changes'][0]
    peer = patch(change(['clips', 'id:clip-b', 'app', 'org.example', 'literal'],
                        present('clip-a'), 'later author'))
    send(realm, peer)
    current = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    inverse = undo(landed['receipt']['receipt_id'], 'edit')
    preview = send(realm, inverse, dry_run=True)
    assert preview['receipt'] is None and preview['data']['would_status'] == 'accepted'
    assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == current
    undone = send(realm, inverse)
    expected = copy.deepcopy(original['parent_revision']['payload'])
    for scope in (expected, expected['config']):
        if 'clips' in scope:
            scope['clips'][1]['app']['org.example']['literal'] = 'later author'
    assert undone['data']['canonical_document']['parent_revision']['payload'] == expected
    assert realm.entries[-1]['records']['undo_of'] == [landed['receipt']['receipt_id']]
    redone = send(realm, undo(undone['receipt']['receipt_id']))
    assert redone['data']['canonical_document'] == realm.documents[current[0]['revision_id']]
    assert realm.entries[1] == entry


@pytest.mark.parametrize('damage', ['item', 'item-aba', 'equal', 'order', 'order-aba', 'member'])
def test_stale_removal_retains_full_item_and_order_guards(damage):
    realm = DetachedRealm(document())
    base = copy.deepcopy(realm.head)
    bundle = bundle_from_document(realm.documents[base['revision_id']])
    removal = remove_call(bundle)
    if damage in ('item', 'item-aba', 'equal'):
        value = 'clip-a' if damage == 'equal' else 'changed'
        send(realm, patch(change(['clips', 'id:clip-a', 'app', 'org.example', 'literal'],
                                 present('clip-a'), value)))
        if damage == 'item-aba':
            send(realm, patch(change(['clips', 'id:clip-a', 'app', 'org.example', 'literal'],
                                     present(value), 'clip-a')))
    elif damage.startswith('order'):
        row = bundle['parent']['clips'][2]
        send(realm, patch(change(['config', 'clips'], present(row), op='list.move', id='clip-c',
                                 order_before=['clip-a', 'clip-b', 'clip-c'], after_id=None)))
        if damage == 'order-aba':
            send(realm, patch(change(['clips'], present(row), op='list.move', id='clip-c',
                                     order_before=['clip-c', 'clip-a', 'clip-b'], after_id='clip-b')))
    else:
        send(realm, patch(change(['clips'], present(bundle['parent']['clips'][2]),
                                 op='list.remove', id='clip-c', order_before=['clip-a', 'clip-b', 'clip-c'])))
    snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        send(realm, removal, base=base)
    assert error.value.failure['details']['reason'] == (
        'value_conflict' if damage in ('item', 'item-aba', 'equal') else 'selection_changed')
    assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == snapshot


@pytest.mark.parametrize('mode', ['fields', 'atomic', 'omitted'])
def test_disjoint_stale_removal_preserves_atomic_scope_fallback(mode):
    realm = DetachedRealm(document())
    base = copy.deepcopy(realm.head)
    removal = remove_call(bundle_from_document(realm.documents[base['revision_id']]))
    send(realm, patch(change(['clips', 'id:clip-b', 'app', 'org.example', 'literal'],
                             present('clip-a'), 'later author')))
    if mode == 'fields':
        landing = copy.deepcopy(realm.documents[realm.head['revision_id']])
        landed = send(realm, removal, base=base)
        assert landed['data']['status'] == 'rebased'
        undone = send(realm, undo(landed['receipt']['receipt_id']))
        assert undone['data']['canonical_document'] == landing
    else:
        controls = {'merge': 'atomic'} if mode == 'atomic' else {}
        options = request(bundle_from_document(realm.documents[base['revision_id']]), [removal], reads=False)
        options = {key: value for key, value in options.items() if key not in (
            'api_version', 'project_id', 'timeline_id', 'base_revision_id', 'base_publication_id', 'commands')}
        options.update(controls)
        with pytest.raises(CommitRejected) as error:
            submit(realm, removal, base=base, **options)
        assert error.value.failure['details']['reason'] == 'atomic_scope_changed'


def test_registered_native_delete_passes_same_disjoint_guard():
    original = bundle_from_document(document())
    native = {'schema_version': 1, 'op': 'clip.delete', 'version': '1',
              'select': 'parent/clip:clip-a', 'args': {}}
    removed = execute(original, [native], commit_request=request(original, [native]))
    validate_final_candidate(removed.bundle, baseline=original)
    peer = execute(removed.bundle, [patch(change(['clips', 'id:clip-b', 'app', 'org.example', 'literal'],
                                                present('clip-a'), 'later author'))], commit_request={'reads': []})
    check_intervening(removed.records, peer.records, state=State(peer.bundle), include_dependents=True)


def test_raw_member_records_still_cannot_authorize_inverse():
    original = bundle_from_document(document())
    plan = execute(original, [remove_call(original)], commit_request={'reads': []})
    with pytest.raises(CommandError, match='authenticated physical binding normalization'):
        execute(plan.bundle, [undo('untrusted')], inverse_records=plan.records)


@pytest.mark.parametrize('mixed', ['remove', 'insert-first', 'insert-last', 'move-first', 'move-last',
                                   'ancestor-first', 'ancestor-last'])
def test_mixed_list_operations_keep_exact_inverse_and_redo_boundaries(mixed):
    original = document()
    realm = DetachedRealm(original)
    bundle = bundle_from_document(original)
    rows = bundle['parent']['clips']
    removal = remove_call(bundle)['args']['changes'][0]
    created = []
    if mixed == 'remove':
        extra = change(['config', 'clips'], present(rows[2]), op='list.remove', id='clip-c',
                       order_before=['clip-b', 'clip-c'])
        changes = [removal, extra]
    else:
        first = mixed.endswith('first')
        order = ['clip-a', 'clip-b', 'clip-c'] if first else ['clip-b', 'clip-c']
        if mixed.startswith('insert'):
            value = {**copy.deepcopy(rows[0]), 'id': 'new', 'at': 11}
            extra = change(['config', 'clips'], ABSENT, value, op='list.insert', id='new',
                           order_before=order, after_id='clip-c')
            created = ['parent/clip:new']
            if first:
                removal['order_before'].append('new')
        elif mixed.startswith('move'):
            extra = change(['config', 'clips'], present(rows[2]), op='list.move', id='clip-c',
                           order_before=order, after_id=None)
            if first:
                removal['order_before'] = ['clip-c', 'clip-a', 'clip-b']
        else:
            before = copy.deepcopy(bundle['parent']['config'])
            if not first:
                before['clips'].pop(0)
            after = copy.deepcopy(before)
            after['opaque']['extra'] = None
            extra = change(['config'], present(before), after)
        changes = [extra, removal] if first else [removal, extra]
    call = patch(*changes, created=created)
    landed = send(realm, call)
    record = realm.entries[-1]['records']['commands'][0]
    if mixed == 'remove':
        assert [write['ref'] for write in record['writes']] == ['parent/clip:clip-a', 'parent/clip:clip-c']
        assert len(record['selections']) == 1
    else:
        assert all(write['ref'] == 'parent' for write in record['writes'])
    bindings = [binding for binding in record['bindings'] if binding['key'].startswith('doc.patch:')]
    assert len(bindings) == 2
    assert bindings[0]['write_range'][0] == 0
    assert bindings[0]['write_range'][1] == bindings[1]['write_range'][0]
    assert bindings[1]['write_range'][1] == len(record['writes'])
    undone = send(realm, undo(landed['receipt']['receipt_id']))
    assert undone['data']['canonical_document'] == original
    redone = send(realm, undo(undone['receipt']['receipt_id']))
    assert redone['data']['canonical_document'] == landed['data']['canonical_document']


@pytest.mark.parametrize('damage', ['order', 'order-aba', 'member', 'consumer'])
def test_inverse_retains_collection_and_surviving_consumer_guards(damage):
    realm = DetachedRealm(document())
    bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    landed = send(realm, remove_call(bundle))
    row = bundle['parent']['clips'][2]
    if damage.startswith('order'):
        send(realm, patch(change(['config', 'clips'], present(row), op='list.move', id='clip-c',
                                 order_before=['clip-b', 'clip-c'], after_id=None)))
        if damage == 'order-aba':
            send(realm, patch(change(['clips'], present(row), op='list.move', id='clip-c',
                                     order_before=['clip-c', 'clip-b'], after_id='clip-b')))
    elif damage == 'member':
        send(realm, patch(change(['clips'], present(row), op='list.remove', id='clip-c',
                                 order_before=['clip-b', 'clip-c'])))
    else:
        send(realm, patch(change(['unknown', 'consumer'], ABSENT, True)), reads=[
            {'kind': 'collection', 'ref': 'parent', 'path': ['clips'],
             'members': ['clip-b', 'clip-c'], 'order': ['clip-b', 'clip-c']}])
    snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        send(realm, undo(landed['receipt']['receipt_id']))
    assert error.value.failure['details']['reason'] == (
        'inverse_conflict' if damage == 'consumer' else 'intervening_write')
    assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == snapshot


@pytest.mark.parametrize('ref', ['internal:chapter-1', 'internal-revision:internal-1'])
def test_shared_physical_removal_inverse_and_redo_keep_exact_owner_pins(ref):
    original = document()
    realm = DetachedRealm(original)
    bundle = bundle_from_document(original)
    landed = send(realm, remove_call(bundle, ref=ref))
    result = landed['data']['canonical_document']
    assert len(result['internal_timeline_revisions']) == 1
    internal = result['internal_timeline_revisions'][0]
    assert [row['id'] for row in internal['payload']['clips']] == ['clip-b']
    record = realm.entries[-1]['records']['commands'][0]
    assert {write['ref'] for write in record['writes']} == {
        'occurrence:occ-a/clip:clip-a', 'occurrence:occ-b/clip:clip-a'}
    peer = patch(change(['clips', 'id:clip-b', 'app', 'org.example', 'literal'],
                        present('clip-a'), 'later author', ref='internal-revision:' + internal['revision_id']))
    send(realm, peer)
    before_undo = copy.deepcopy(realm.documents[realm.head['revision_id']])
    undone = send(realm, undo(landed['receipt']['receipt_id']))
    restored = undone['data']['canonical_document']
    assert len(restored['internal_timeline_revisions']) == 1
    clips = restored['internal_timeline_revisions'][0]['payload']['clips']
    assert clips[0] == original['internal_timeline_revisions'][0]['payload']['clips'][0]
    assert clips[1]['app']['org.example']['literal'] == 'later author'
    redone = send(realm, undo(undone['receipt']['receipt_id']))
    assert redone['data']['canonical_document'] == before_undo


@pytest.mark.parametrize('mode', ['atomic', 'omitted'])
def test_accepted_atomic_removal_scope_remains_an_inverse_guard(mode):
    realm = DetachedRealm(document())
    bundle = bundle_from_document(realm.documents[realm.head['revision_id']])
    call = remove_call(bundle)
    controls = request(bundle, [call], reads=False)
    for key in ('api_version', 'project_id', 'timeline_id', 'base_revision_id',
                'base_publication_id', 'commands'):
        controls.pop(key)
    if mode == 'atomic':
        controls.update(merge='atomic', reads=[])
    landed = submit(realm, call, **controls)
    records = realm.entries[-1]['records']
    assert records['merge_mode'] == 'atomic'
    assert {'kind': 'scope', 'ref': 'parent', 'path': []} in records['steps'][0]['dependencies']
    send(realm, patch(change(['clips', 'id:clip-b', 'app', 'org.example', 'literal'],
                             present('clip-a'), 'later author')))
    snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        send(realm, undo(landed['receipt']['receipt_id']))
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == snapshot


@pytest.mark.parametrize('identity', ['clip/a', 'clip::a', 'clip:a'])
def test_literal_patch_identities_remain_supported(identity):
    original = document()
    parent = original['parent_revision']['payload']
    for scope in (parent, parent['config']):
        scope['clips'][0]['id'] = identity
    original['parent_revision']['content_digest'] = _digest(parent)
    bundle = bundle_from_document(original)
    call = remove_call(bundle)
    plan = execute(bundle, [call], commit_request=request(bundle, [call]))
    validate_final_candidate(plan.bundle, baseline=bundle)
    assert [row['id'] for row in plan.bundle['parent']['clips']] == ['clip-b', 'clip-c']
