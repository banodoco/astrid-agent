"""D72 helper retirement is a guarded commit effect, independent of native preservation."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, create_runtime_commit_service, document_from_bundle
from astrid.sdk.timeline.rebase import replay
from tests.core import test_project_packs
from tests.core.test_timeline_element_publication import setup_owner, update
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    inverse,
)
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi

PATH = ['config', 'app', 'elements', 'reveal']
SOURCE = {'source': 'legacy recovery bytes'}
OTHER = {'source': 'unowned recovery bytes', 'opaque': [None, False, 0]}
configured = test_project_packs.configured


def capture(owner, bundle, ref, **kwargs):
    result = update(owner, ref, **kwargs)
    assert result.ok, result.error
    body = owner.commit.call_args.args[2]
    plan = execute(bundle, body['commands'], commit_request=body,
                   schema_inputs=owner.schema_inputs, realm_id='realm')
    return body, plan


def accepted_realm(owner, bundle):
    """Use the existing provider port with this helper fixture's scope and base."""
    document = copy.deepcopy(bundle['source_mapping']['_runtime_document'])
    document['parent_revision']['payload'] = {
        **copy.deepcopy(bundle['parent']), 'occurrences': document['parent_revision']['payload']['occurrences']}
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    realm = DetachedRealm(document)
    realm.head['publication_id'] = 'publication'
    realm.entries[0]['publication_id'] = 'publication'
    realm.entries[0]['receipt']['receipt_id'] = 'publication'
    realm.entries[0]['receipt']['project_id'] = 'project'
    for source, objects in owner.schema_inputs:
        realm.live_sources[_digest(source['pin'])] = copy.deepcopy(source)
        realm.snapshot_objects.update(copy.deepcopy(objects))

    class Port(DetachedPort):
        def _candidate(self, prepared):
            return {**super()._candidate(prepared), **{
                key: copy.deepcopy(prepared['records'][key]) for key in ('steps', 'merge_mode', 'label')}}

        def accept(self, prepared):
            result = super().accept(prepared)
            result['receipt']['project_id'] = 'project'
            if result['data']['status'] == 'rebased':
                result['data']['status'] = 'merged'
            realm.entries[-1]['receipt'] = copy.deepcopy(result['receipt'])
            return result

    def submit(calls, *, body=None, base=None):
        context = realm.context(calls, base=base)
        context['realm_id'] = 'realm'
        context['request'].update(project_id='project', timeline_id='timeline')
        if body is not None:
            context['request'].update(copy.deepcopy(body))
        context['invocation']['path'] = '/v1/projects/project/timelines/timeline/commit'
        context['invocation']['body'] = copy.deepcopy(context['request'])
        context['request_hash'] = _digest(context['invocation'])
        return create_runtime_commit_service().commit(context, Port(realm, context))

    return realm, submit


@pytest.mark.parametrize('existing', ['absent', None, {}, {'future': [None, False, 0, {}, []]}])
@pytest.mark.parametrize('params', [None, {}, {'future': {'literal': [None, False, 0, {}, []]}}])
def test_last_consumer_cleanup_is_independent_of_literal_params(monkeypatch, configured, existing, params):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    clip = bundle['parent']['clips'][0]
    clip.pop('params')
    if existing != 'absent':
        clip['params'] = copy.deepcopy(existing)
    # The compiler requires a coherent mirror; the helper must not edit either input.
    bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])
    bundle['parent']['config']['app']['elements']['unowned'] = copy.deepcopy(OTHER)
    original = copy.deepcopy(bundle)
    body, plan = capture(owner, bundle, ref, params=params)
    assert [call['op'] for call in body['commands']] == (
        ['clip.set-element', 'clip.set-params', 'doc.patch'] if params else
        ['clip.set-element', 'doc.patch'])
    parent = plan.bundle['parent']
    assert parent['config']['app'] == {'elements': {'unowned': OTHER}, 'other': 'keep'}
    expected = copy.deepcopy(original['parent']['clips'][0])
    expected['elementRef'] = ref
    if params:
        expected['params'] = {**(expected.get('params') or {}), **params}
    assert parent['clips'] == [expected, original['parent']['clips'][1]]
    assert parent['config']['clips'] == parent['clips']
    assert {k: v for k, v in parent.items() if k not in ('clips', 'config')} == {
        k: v for k, v in original['parent'].items() if k not in ('clips', 'config')}
    assert {k: v for k, v in parent['config'].items() if k not in ('clips', 'app')} == {
        k: v for k, v in original['parent']['config'].items() if k not in ('clips', 'app')}
    assert bundle == original
    assert plan.records[-1]['writes'] == [{
        'ref': 'parent', 'path': PATH, 'before': present(SOURCE), 'after': ABSENT}]
    assert plan.records[-1]['derived_writes'] == []
    assert any(read['path'] == PATH and read['state'] == present(SOURCE)
               for read in plan.records[-1]['reads'])
    assert {'op': 'remove', 'ref': 'parent', 'path': PATH, 'before': present(SOURCE)} in plan.steps[0]['patch']
    assert plan.merge_mode == 'fields'


@pytest.mark.parametrize('field', ['elementRef', 'element_ref'])
@pytest.mark.parametrize('identity', ['exact', 'revision', 'pack', 'extra', 'bool-number'])
def test_only_exact_old_reference_consumers_retain_the_entry(monkeypatch, configured, field, identity):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    old = bundle['parent']['clips'][0]['elementRef']
    old['opaque'] = False
    sibling = bundle['parent']['clips'][1]
    sibling[field] = copy.deepcopy(old)
    if identity == 'revision':
        sibling[field]['revision'] = ref['revision']
    elif identity == 'pack':
        sibling[field]['packId'] = 'other-owner'
    elif identity == 'extra':
        sibling[field]['extra'] = 'other identity'
    elif identity == 'bool-number':
        sibling[field]['opaque'] = 0
    bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])
    original = copy.deepcopy(bundle)
    body, plan = capture(owner, bundle, ref)
    assert [call['op'] for call in body['commands']] == (
        ['clip.set-element'] if identity == 'exact' else ['clip.set-element', 'doc.patch'])
    entries = plan.bundle['parent']['config']['app']['elements']
    assert entries == ({'reveal': SOURCE} if identity == 'exact' else {})
    assert plan.bundle['parent']['clips'][1] == sibling
    assert bundle == original


@pytest.mark.parametrize('shape', ['no-config', 'no-app', 'null-app', 'no-elements', 'null-elements',
                                 'other-entry', 'same-ref'])
def test_absent_entry_and_same_reference_do_not_prune(monkeypatch, configured, shape):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    config = bundle['parent']['config']
    if shape == 'no-config':
        bundle['parent'].pop('config')
    elif shape == 'no-app':
        config.pop('app')
    elif shape == 'null-app':
        config['app'] = None
    elif shape == 'no-elements':
        config['app'].pop('elements')
    elif shape == 'null-elements':
        config['app']['elements'] = None
    elif shape == 'other-entry':
        config['app']['elements'] = {'unowned': OTHER}
    else:
        bundle['parent']['clips'][0]['elementRef'] = copy.deepcopy(ref)
        config['clips'] = copy.deepcopy(bundle['parent']['clips'])
    original = copy.deepcopy(bundle)
    body, plan = capture(owner, bundle, ref)
    assert [call['op'] for call in body['commands']] == (
        ['clip.set'] if shape == 'same-ref' else ['clip.set-element'])
    assert plan.bundle['parent'].get('config', {}).get('app') == original['parent'].get('config', {}).get('app')
    assert bundle == original


@pytest.mark.parametrize('value', [None, False, 0, '', [], {'source': 'retained text'}])
def test_cleanup_previous_value_is_exact_even_for_null_and_scalar_entries(monkeypatch, configured, value):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    bundle['parent']['config']['app']['elements']['reveal'] = copy.deepcopy(value)
    body, plan = capture(owner, bundle, ref)
    assert body['commands'][-1]['args']['changes'][0]['before'] == present(value)
    assert plan.bundle['parent']['config']['app']['elements'] == {}


@pytest.mark.parametrize('change', ['membership', 'order', 'sibling-ref', 'sibling-alias', 'source'])
def test_concurrent_scan_or_source_changes_refuse_without_mutation(monkeypatch, configured, change):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    body, plan = capture(owner, bundle, ref)
    head = copy.deepcopy(bundle)
    if change == 'membership':
        head['parent']['clips'].append({'id': 'concurrent', 'at': 0, 'hold': 1})
    elif change == 'order':
        head['parent']['clips'].reverse()
    elif change == 'sibling-ref':
        head['parent']['clips'][1]['elementRef'] = copy.deepcopy(bundle['parent']['clips'][0]['elementRef'])
    elif change == 'sibling-alias':
        head['parent']['clips'][1]['element_ref'] = copy.deepcopy(bundle['parent']['clips'][0]['elementRef'])
    else:
        head['parent']['config']['app']['elements']['reveal'] = {'source': 'new recovery bytes'}
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    original = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head, commit_request=body, schema_inputs=owner.schema_inputs, realm_id='realm')
    assert head == original


def test_exact_inverse_redo_and_source_aba_guards(monkeypatch, configured):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    bundle['parent']['config']['tracks'] = [{'id': 'V1', 'kind': 'visual'}]
    bundle['parent']['config']['app']['elements']['unowned'] = copy.deepcopy(OTHER)
    body, plan = capture(owner, bundle, ref, duration=6)
    realm, submit = accepted_realm(owner, bundle)
    accepted = submit(body['commands'], body=body)
    document = accepted['data']['canonical_document']
    assert document['parent_revision']['payload'] == document_from_bundle(plan.bundle)['parent_revision']['payload']
    entry = realm.entries[-1]
    assert entry['records']['commands'][-1]['writes'] == plan.records[-1]['writes']
    assert entry['records']['steps'][0]['patch'] == accepted['data']['steps'][0]['patch']
    undone = submit([inverse([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document']['parent_revision']['payload'] == {
        **bundle['parent'], 'occurrences': []}
    inverse_entry = realm.entries[-1]['records']
    ranges = [binding['boundary']['effect_range'] for record in inverse_entry['commands']
              for binding in record['bindings'] if binding['key'] == 'inverse.stage']
    assert [i for first, last in ranges for i in range(first, last)] == list(
        range(len(inverse_entry['steps'][0]['patch'])))
    redone = submit([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == document
    with pytest.raises(CommitRejected, match='intervening'):
        submit([inverse([accepted['receipt']['receipt_id']])])


@pytest.mark.parametrize('field', ['source', 'elementRef', 'element_ref'])
def test_accepted_aba_in_cleanup_inputs_refuses_rebase(monkeypatch, configured, field):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    bundle['parent']['config']['tracks'] = [{'id': 'V1', 'kind': 'visual'}]
    body, _ = capture(owner, bundle, ref)
    realm, submit = accepted_realm(owner, bundle)
    if field == 'source':
        changes = [{'op': 'set', 'ref': 'parent', 'path': PATH,
                    'before': present(SOURCE), 'value': {'source': 'intermediate bytes'}}]
        restore = [{**changes[0], 'before': present(changes[0]['value']), 'value': SOURCE}]
    else:
        changes = [{'op': 'set', 'ref': 'parent', 'path': ['clips', 'id:media', field],
                    'before': ABSENT, 'value': bundle['parent']['clips'][0]['elementRef']},
                   {'op': 'set', 'ref': 'parent', 'path': ['clips', 'id:media', 'clipType'],
                    'before': ABSENT, 'value': 'reveal'}]
        restore = [{'op': 'remove', 'ref': change['ref'], 'path': change['path'],
                    'before': present(change['value'])} for change in changes]
    for effects in (changes, restore):
        submit([{'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
                 'args': {'changes': effects}}])
    head = copy.deepcopy(realm.head)
    assert realm.documents[head['revision_id']]['parent_revision']['payload'] == {
        **bundle['parent'], 'occurrences': []}
    with pytest.raises(CommitRejected, match='intervening|dependency'):
        submit(body['commands'], body=body)
    assert realm.head == head


def test_disjoint_unowned_source_edits_merge_and_survive_inverse(monkeypatch, configured):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    bundle['parent']['config']['tracks'] = [{'id': 'V1', 'kind': 'visual'}]
    bundle['parent']['config']['app']['elements']['unowned'] = copy.deepcopy(OTHER)
    body, plan = capture(owner, bundle, ref)
    head = copy.deepcopy(bundle)
    head['parent']['config']['app']['elements']['unowned']['source'] = 'concurrent unowned bytes'
    merged = replay(plan, head, commit_request=body, schema_inputs=owner.schema_inputs, realm_id='realm')
    assert merged.bundle['parent']['config']['app']['elements'] == {
        'unowned': {**OTHER, 'source': 'concurrent unowned bytes'}}
    realm, submit = accepted_realm(owner, bundle)
    base = copy.deepcopy(realm.head)
    peer = {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
            'args': {'changes': [{'op': 'set', 'ref': 'parent',
                'path': ['config', 'app', 'elements', 'unowned', 'source'],
                'before': present(OTHER['source']), 'value': 'concurrent unowned bytes'}]}}
    theirs = submit([peer])
    accepted = submit(body['commands'], body=body, base=base)
    assert accepted['data']['status'] == 'merged'
    assert accepted['data']['rebased_onto'] == {
        'revision_id': theirs['data']['accepted_revision_id'],
        'publication_id': theirs['receipt']['receipt_id']}
    undone = submit([inverse([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document']['parent_revision']['payload'] == theirs['data'][
        'canonical_document']['parent_revision']['payload']


def test_wrong_cleanup_previous_value_is_refused_atomically(monkeypatch, configured):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    body, _ = capture(owner, bundle, ref)
    body['commands'][-1]['args']['changes'][0]['before'] = present({'source': 'forged'})
    original = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        execute(bundle, body['commands'], commit_request=body,
                schema_inputs=owner.schema_inputs, realm_id='realm')
    assert bundle == original


def test_cleanup_flight_retries_exact_body_without_recapture(monkeypatch, configured):
    from astrid.sdk.workspace_client import WorkspaceClientError

    owner, bundle, ref = setup_owner(monkeypatch, configured)
    commit = owner.commit.side_effect
    owner.commit.side_effect = WorkspaceClientError(503, 'not_ready', 'lost acknowledgement', {})
    failed = update(owner, ref, idempotency_key='cleanup-flight')
    assert not failed.ok
    first = copy.deepcopy(owner.commit.call_args.args)
    first_options = copy.deepcopy(owner.commit.call_args.kwargs)
    assert first[2]['commands'][-1]['op'] == 'doc.patch'
    owner.commit.side_effect = commit

    def forbidden(*args, **kwargs):
        pytest.fail('cleanup flight was recaptured')

    monkeypatch.setattr('astrid.sdk.timeline_elements.execute', forbidden)
    monkeypatch.setattr('astrid.sdk.timeline_elements._read_document', forbidden)
    result = update(owner, ref, idempotency_key='cleanup-flight')
    assert result.ok, result.error
    assert owner.commit.call_args.args == first
    assert owner.commit.call_args.kwargs == first_options
    assert owner.last_commit_data['canonical_document']['parent_revision']['payload']['config']['app'] == {
        'elements': {}, 'other': 'keep'}


def test_acknowledged_cleanup_uses_original_history_and_checks_intent(monkeypatch, configured):
    from astrid.sdk.contracts import DomainResult

    owner, bundle, ref = setup_owner(monkeypatch, configured)
    result = update(owner, ref, idempotency_key='cleanup-history')
    assert result.ok, result.error
    body = owner.commit.call_args.args[2]
    receipt = result.receipt.as_dict()
    receipt['result'].update(base_revision_id='head', commands=copy.deepcopy(body['commands']))
    owner.history.return_value = DomainResult.success({
        'head': {'revision_id': 'later', 'publication_id': 'later-publication'},
        'items': [{'publisher': 'author', 'receipt': receipt}], 'next_cursor': None})

    def forbidden(*args, **kwargs):
        pytest.fail('acknowledged cleanup was recaptured')

    monkeypatch.setattr('astrid.sdk.timeline_elements.execute', forbidden)
    monkeypatch.setattr('astrid.sdk.timeline_elements._read_document', forbidden)
    retried = update(owner, ref, idempotency_key='cleanup-history')
    assert retried.ok, retried.error
    assert retried.data == result.data
    assert retried.receipt.as_dict() == receipt
    assert owner.commit.call_count == 1
    changed = update(owner, ref, duration=7, idempotency_key='cleanup-history')
    assert not changed.ok and changed.error.message == 'Acknowledged request identity has different original intent'
    assert owner.commit.call_count == 1
