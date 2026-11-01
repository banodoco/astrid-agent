import base64
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.core.element import project_packs as packs
from astrid.core.receipts.contract import CommandReceipt
from astrid.core.timeline.authoring_bundle import open_authoring_bundle
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline_elements import update_parent_element
from tests.core import test_project_packs

configured = test_project_packs.configured


def setup_owner(monkeypatch, configured=None):
    if configured is None:
        monkeypatch.setattr('astrid.sdk.timeline_elements.resolve_admitted_element',lambda realm,project,ref,**kwargs:{})
        effect={'id':'effect','at':0,'track':'V1','hold':2,'clipType':'reveal','elementRef':{'id':'reveal','kind':'effect','revision':'draft-reveal'},'params':{'color':'#ff6b6b','seed':15}}
        media={'id':'media','at':0,'track':'V1','hold':10,'opaque':{'preserve':True}}
        clips=[effect,media]
        payload={'clips':copy.deepcopy(clips),'config':{'clips':copy.deepcopy(clips),'app':{'elements':{'reveal':{'source':'legacy recovery bytes'}},'other':'keep'},'bundle':{'transcriptReferences':{'associations':[{'id':'saved'}]}}},'registry':{'assets':{}},'occurrences':[]}
        digest='sha256:'+hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        bundle=open_authoring_bundle({'project_id':'project','timeline_id':'timeline','revision_id':'head','payload':payload,'content_digest':digest},shot_revisions=[],internal_timeline_revisions=[])
        owner=Mock()
        owner.open_bundle.return_value=DomainResult.success({'bundle':bundle,'project_id':'project','timeline_id':'timeline','head_revision_id':'head','revision_id':'head'})
        owner._typed.side_effect=lambda operation,project,timeline,publication,**kwargs:DomainResult.success({'new_head':publication['parent_revision_id']})
        ref={'id':'reveal','kind':'effect','packId':'project_owner','revision':'sha256:'+'a'*64}
        return owner,bundle,ref
    test_project_packs.seed()
    admitted = packs.admit_project_pack('realm', 'project')
    ref = {key: admitted['elements'][0]['descriptor'][key]
           for key in ('id', 'kind', 'packId', 'revision')}
    row = configured.rows[('project', admitted['documentId'])]
    row['project_id'] = 'project'
    pin = {'document_id': row['document_id'], 'version': row['version'],
           'content_digest': packs._json_digest(row['content'])}
    objects = {}
    for admission in row['content']['admissions']:
        descriptor = admission['snapshot_object']
        objects[descriptor['object_id']] = {
            **descriptor, 'bytes_base64': base64.b64encode(
                configured.media.read_bytes(descriptor['object_id'])).decode()}
    inputs = copy.deepcopy([({'pin': pin, 'document': {**row, 'project_id': 'project'}}, objects)])
    monkeypatch.setattr('astrid.sdk.timeline_elements.Documents', lambda client: configured)
    monkeypatch.setattr('astrid.sdk.timeline_elements.RemoteMedia', lambda client: configured.media)
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(packs.authoring_root().parent / 'outbox'))
    # Ack validation is covered by sync and the actual-service helper tests.
    monkeypatch.setattr('astrid.sdk.timeline.sync._ack',
                        lambda result, *args: (result['data'], result['receipt']))
    effect = {'id': 'effect', 'at': 0, 'track': 'V1', 'hold': 2, 'clipType': 'reveal',
              'elementRef': {'id': 'reveal', 'kind': 'effect', 'revision': 'draft-reveal'},
              'params': {'color': '#ff6b6b', 'seed': 15}, 'opaque': {'keep': [None, False]}}
    media = {'id': 'media', 'at': 0, 'track': 'V1', 'hold': 10, 'opaque': {'preserve': True}}
    clips = [effect, media]
    payload = {'clips': copy.deepcopy(clips), 'config': {
        'clips': copy.deepcopy(clips), 'output': {'fps': 30},
        'app': {'elements': {'reveal': {'source': 'legacy recovery bytes'}}, 'other': 'keep'},
        'bundle': {'transcriptReferences': {'associations': [{'id': 'saved'}]}}},
        'registry': {'assets': {}}, 'occurrences': []}
    digest = 'sha256:' + hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    # Match open_bundle's canonical source metadata; the original payload is unchanged.
    bundle = bundle_from_document({'schema_version': 1, 'project_id': 'project', 'timeline_id': 'timeline',
        'parent_revision': {'revision_id': 'head', 'payload': payload, 'content_digest': digest},
        'shot_revisions': [], 'internal_timeline_revisions': [],
        'dependency_manifest': {'shots': [], 'internal_timelines': [], 'media': [], 'documents': []}})
    bundle['source_mapping']['_runtime_document'] = copy.deepcopy(bundle['source_mapping']['_runtime_document'])
    owner = Mock()
    owner._client._last_handshake = {'realm_id': 'realm', 'actor_id': 'author'}
    owner.open_composition.return_value = DomainResult.success({
        'native_inspection': {'project_id': 'project', 'timeline_id': 'timeline'},
        'summary': {'head_revision_id': 'head'}})
    owner.open_bundle.return_value = DomainResult.success({
        'bundle': bundle, 'project_id': 'project', 'timeline_id': 'timeline',
        'head_revision_id': 'head', 'revision_id': 'head'})
    owner.history.return_value = DomainResult.success({
        'head': {'revision_id': 'head', 'publication_id': 'publication'},
        'items': [], 'next_cursor': None})

    def commit(project, timeline, body, **kwargs):
        plan = execute(bundle, body['commands'], schema_inputs=inputs, realm_id='realm')
        data = {'accepted_revision_id': 'committed', 'canonical_document': {
            'parent_revision': {'payload': plan.bundle['parent']},
            'shot_revisions': [], 'internal_timeline_revisions': []}}
        owner.last_commit_data = data
        receipt = CommandReceipt('accepted-receipt', 'timeline.commit', kwargs['idempotency_key'],
                                 'hash', project, (1, 1), (), data, '2026-10-20T00:00:00Z')
        return DomainResult.success(data, receipt=receipt, idempotency_key=kwargs['idempotency_key'])

    owner.schema_inputs = inputs
    owner.commit.side_effect = commit
    return owner, bundle, ref


def update(owner, ref, **kwargs):
    return update_parent_element(owner, 'project', 'timeline', realm='realm', clip_id='effect',
                                 element=ref, expected_head='head', **kwargs)


def test_sameclip_exacthead_publication_preserves_closure_and_user_params(monkeypatch, configured):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    original = copy.deepcopy(bundle)
    result = update(owner, ref, duration=6, params={'chaos': .8}, idempotency_key='edit-element')
    assert result.ok, result.error
    body = owner.commit.call_args.args[2]
    assert body['base_revision_id'] == 'head' and body['base_publication_id'] == 'publication'
    assert [c['op'] for c in body['commands']] == [
        'clip.set-element', 'clip.set', 'clip.set-params', 'doc.patch']
    assert body['commands'][0]['args'] == {
        'elementRef': ref, 'schema_source': body['commands'][2]['args']['schema_source']}
    assert body['commands'][1]['args'] == {'values': {'hold': 6}, 'clear': []}
    assert body['commands'][2]['args']['values'] == {'chaos': .8}
    assert body['commands'][2]['args']['schema_source']['document_id'] in {
        document for project, document in configured.rows}
    assert body['commands'][3]['args'] == {'changes': [{
        'op': 'remove', 'ref': 'parent', 'path': ['config', 'app', 'elements', 'reveal'],
        'before': {'state': 'present', 'value': {'source': 'legacy recovery bytes'}}}]}
    assert body['reads'] == [{
        'kind': 'collection', 'ref': 'parent', 'path': ['clips'],
        'members': ['effect', 'media'], 'order': ['effect', 'media']}, *[
        {'kind': 'field', 'ref': 'parent', 'path': ['clips', 'id:' + row['id'], field],
         'state': {'state': 'present', 'value': row[field]} if field in row else {'state': 'absent'}}
        for row in original['parent']['clips'] for field in ('elementRef', 'element_ref')]]
    assert owner.commit.call_args.kwargs == {'idempotency_key': 'edit-element'}
    assert result.data == {
        'project_id': 'project', 'timeline_id': 'timeline', 'expected_head': 'head',
        'new_head': 'committed', 'clip_id': 'effect', 'element': ref, 'duration': 6,
        'params': {'color': '#ff6b6b', 'seed': 15, 'chaos': .8}, 'before': {
            'element': original['parent']['clips'][0]['elementRef'], 'duration': 2,
            'params': {'color': '#ff6b6b', 'seed': 15}}}
    assert result.receipt.receipt_id == 'accepted-receipt'
    assert result.idempotency_key == 'edit-element'
    canonical_document = result.receipt.result['canonical_document']
    assert canonical_document['shot_revisions'] == []
    assert canonical_document['internal_timeline_revisions'] == []
    # The native prefix preserves open config; only the final patch retires the orphan.
    plan = execute(bundle, body['commands'][:2], schema_inputs=owner.schema_inputs, realm_id='realm')
    assert plan.bundle['parent']['clips'][1] == bundle['parent']['clips'][1]
    assert plan.bundle['parent']['clips'][0]['opaque'] == {'keep': [None, False]}
    config = plan.bundle['parent']['config']
    assert config['clips'] == plan.bundle['parent']['clips']
    assert {k: v for k, v in config.items() if k != 'clips'} == {
        k: v for k, v in bundle['parent']['config'].items() if k != 'clips'}
    assert plan.bundle['parent']['registry'] == bundle['parent']['registry']
    assert bundle == original
    owner._typed.assert_not_called()


def test_stale_head_and_concurrent_cas_reject_without_rebase(monkeypatch, configured):
    owner, _, ref = setup_owner(monkeypatch, configured)
    stale = update_parent_element(owner, 'project', 'timeline', realm='realm', clip_id='effect',
                                  element=ref, expected_head='stale')
    assert not stale.ok and stale.error.code == 'conflict'
    owner.commit.assert_not_called()
    owner.commit.return_value = DomainResult.failure(ErrorObject('conflict', 'CAS rejected', {'status': 'conflict'}))
    owner.commit.side_effect = None
    rejected = update(owner, ref, duration=6)
    assert not rejected.ok and rejected.error.code == 'conflict'
    assert owner.commit.call_count == 1
    owner._typed.assert_not_called()


@pytest.mark.parametrize('duration', [0, -1, float('inf'), float('nan'), True])
def test_bad_duration_never_publishes(monkeypatch, configured, duration):
    owner, _, ref = setup_owner(monkeypatch, configured)
    result = update(owner, ref, duration=duration)
    assert not result.ok
    owner.commit.assert_not_called()


def test_unadmitted_owner_and_sourceclip_reject(monkeypatch, configured):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    result = update(owner, {**ref, 'packId': 'unadmitted'})
    assert not result.ok
    owner.commit.assert_not_called()
    bundle['parent']['clips'][0]['asset'] = 'source'
    result = update(owner, ref)
    assert not result.ok
    owner.commit.assert_not_called()


@pytest.mark.parametrize('field', ['elementRef', 'element_ref'])
def test_element_only_and_same_revision_keep_unknown_props(monkeypatch, configured, field):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    clip = bundle['parent']['clips'][0]
    clip.pop('elementRef')
    clip[field] = ref
    clip['params']['future'] = {'literal': [None, {}, False]}
    result = update(owner, ref)
    assert result.ok, result.error
    assert result.data['duration'] == 2 and result.data['params'] == clip['params']
    calls = owner.commit.call_args.args[2]['commands']
    assert [c['op'] for c in calls] == ['clip.set']
    assert calls[0]['args'] == {'values': {'hold': 2}, 'clear': []}
    plan = execute(bundle, calls, schema_inputs=owner.schema_inputs, realm_id='realm')
    assert all(write['path'][0] != 'params' for record in plan.records for write in record['writes'])


@pytest.mark.parametrize('params', [{'seed': float('nan')}, {'seed': object()}, [], {'large': 'x' * 16385}])
def test_invalid_json_params_refuse_before_commit(monkeypatch, configured, params):
    owner, _, ref = setup_owner(monkeypatch, configured)
    result = update(owner, ref, params=params)
    assert not result.ok and result.error.code == 'validation_error'
    owner.commit.assert_not_called()


@pytest.mark.parametrize('params', [None, {}])
@pytest.mark.parametrize('existing', [None, {}])
def test_element_only_without_existing_params(monkeypatch, configured, params, existing):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    clip = bundle['parent']['clips'][0]
    clip.pop('params')
    if existing is not None:
        clip['params'] = existing
    bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])
    result = update(owner, ref, params=params)
    assert result.ok, result.error
    assert result.data['params'] == {}
    calls = owner.commit.call_args.args[2]['commands']
    assert [call['op'] for call in calls] == ['clip.set-element', 'doc.patch']
    assert calls[1]['args'] == {'changes': [{
        'op': 'remove', 'ref': 'parent', 'path': ['config', 'app', 'elements', 'reveal'],
        'before': {'state': 'present', 'value': {'source': 'legacy recovery bytes'}}}]}
    plan = execute(bundle, calls, schema_inputs=owner.schema_inputs, realm_id='realm')
    assert ('params' in plan.bundle['parent']['clips'][0]) == (existing is not None)
    assert all(write['path'][0] != 'params' for record in plan.records for write in record['writes'])


def test_invalid_known_param_is_validated_in_native_commit(monkeypatch, configured):
    owner, _, ref = setup_owner(monkeypatch, configured)
    result = update(owner, ref, params={'seed': 'invalid number'})
    assert not result.ok and result.error.code == 'validation_error'
    owner.commit.assert_not_called()
    owner._typed.assert_not_called()


@pytest.mark.parametrize('params', [None, {}, {'seed': 8}])
def test_wrong_revision_cannot_use_an_admitted_sibling_schema(monkeypatch, configured, params):
    owner, _, ref = setup_owner(monkeypatch, configured)
    result = update(owner, {**ref, 'revision': 'sha256:' + '0' * 64}, params=params)
    assert not result.ok and result.error.code == 'validation_error'
    owner.commit.assert_not_called()


def test_history_head_race_never_submits(monkeypatch, configured):
    owner, _, ref = setup_owner(monkeypatch, configured)
    owner.history.return_value = DomainResult.success({
        'head': {'revision_id': 'new-head', 'publication_id': 'new-publication'},
        'items': [], 'next_cursor': None})
    result = update(owner, ref, params={'seed': 8})
    assert not result.ok and result.error.code == 'conflict'
    owner.commit.assert_not_called()


@pytest.mark.parametrize('clear_params', [['seed', 'seed'], ['seed'], [None], 'seed'])
def test_invalid_clear_params_never_submits(monkeypatch, configured, clear_params):
    owner, _, ref = setup_owner(monkeypatch, configured)
    result = update(owner, ref, params={'seed': 8}, clear_params=clear_params)
    assert not result.ok and result.error.code == 'validation_error'
    owner.commit.assert_not_called()


def test_snapshot_read_failure_preserves_public_error(monkeypatch, configured):
    from astrid.sdk.exceptions import ServiceNotFoundError

    owner, _, ref = setup_owner(monkeypatch, configured)
    error = ServiceNotFoundError('Snapshot unavailable', details={'reason': 'missing_object'})
    monkeypatch.setattr(configured.media, 'read_bytes', Mock(side_effect=error))
    result = update(owner, ref, idempotency_key='missing-snapshot')
    assert not result.ok and result.error == error.to_error_object()
    assert result.idempotency_key == 'missing-snapshot'
    owner.commit.assert_not_called()


@pytest.mark.parametrize('params', [None, {}, {'seed': 15.0}])
def test_digest_only_admission_preserves_same_selected_hold(monkeypatch, configured, params):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    for row in configured.rows.values():
        for admission in row['content']['admissions']:
            admission.pop('snapshot_object', None)
    # Keep the real local closure and build receipt from the original regression.
    resolved = packs.resolve_admitted_element(
        'realm', 'project', ref, client=SimpleNamespace(documents=configured))
    assert resolved['descriptor']['revision'] == ref['revision']
    bundle['parent']['clips'][0]['elementRef'] = ref
    if params:
        # This transport unit supplies an accepted selection. The native
        # companion qualifies the real history and retained-object reads.
        verified, objects = owner.schema_inputs[0]
        descriptor = next(item['snapshot_object'] for item in verified['document']['content']['admissions']
                          if item['revision'] == ref['revision'])
        monkeypatch.setattr('astrid.sdk.timeline._selected_param_authority.resolve_selected_param_source',
                            Mock(return_value={'source': verified['pin'], 'element': ref, 'object': descriptor}))
        owner._client.get_project_verified_document.return_value = verified
        owner._client.get_project_verified_document_object.return_value = objects[descriptor['object_id']]
    original = copy.deepcopy(bundle)
    documents = copy.deepcopy(configured.rows)

    def forbidden(*args, **kwargs):
        pytest.fail('unchanged selected element requested snapshot or re-admission')

    monkeypatch.setattr('astrid.sdk.timeline_elements._read_document', forbidden)
    monkeypatch.setattr(configured.media, 'read_bytes', forbidden)
    monkeypatch.setattr(packs, 'admit_project_pack', forbidden)
    result = update(owner, ref, duration=6, params=params)
    assert result.ok, result.error
    calls = owner.commit.call_args.args[2]['commands']
    assert [call['op'] for call in calls] == ['clip.set'] + (['clip.set-params'] if params else [])
    assert calls[0]['args'] == {'values': {'hold': 6}, 'clear': []}
    assert result.data['params'] == original['parent']['clips'][0]['params']
    assert bundle == original and configured.rows == documents


def test_digest_only_revision_reselection_refuses_without_writes(monkeypatch, configured):
    owner, bundle, ref = setup_owner(monkeypatch, configured)
    for row in configured.rows.values():
        for admission in row['content']['admissions']:
            admission.pop('snapshot_object', None)
    assert packs.resolve_admitted_element(
        'realm', 'project', ref, client=SimpleNamespace(documents=configured))['descriptor']['revision'] == ref['revision']
    original, documents = copy.deepcopy(bundle), copy.deepcopy(configured.rows)
    # The retained review's draft -> admitted revision is a real reselection.
    result = update(owner, ref, duration=6)
    assert not result.ok and result.error.message == 'pack snapshot unavailable; re-admit pack'
    owner.commit.assert_not_called()
    assert bundle == original and configured.rows == documents


@pytest.mark.parametrize('alias', [False, True])
@pytest.mark.parametrize('change', ['membership', 'order', 'identity'])
def test_same_selected_hold_retains_raw_target_and_selection_guards(monkeypatch, configured, alias, change):
    from astrid.sdk.timeline._command_state import CommandError
    from astrid.sdk.timeline.rebase import replay
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    owner, bundle, ref = setup_owner(monkeypatch, configured)
    row = bundle['parent']['clips'][0]
    row.pop('elementRef')
    row['element_ref' if alias else 'elementRef'] = copy.deepcopy(ref)
    original = copy.deepcopy(bundle)
    monkeypatch.setattr('astrid.sdk.timeline_elements._read_document',
                        lambda *a, **kw: pytest.fail('HOLD read admission'))
    result = update(owner, ref, duration=6)
    assert result.ok, result.error
    calls = owner.commit.call_args.args[2]['commands']
    assert [call['op'] for call in calls] == ['clip.set']
    plan = execute(bundle, calls, realm_id='realm')
    head = copy.deepcopy(bundle)
    if change == 'membership':
        head['parent']['clips'].append({'id': 'new-raw-sibling', 'hold': 1, 'at': 0})
    elif change == 'order':
        head['parent']['clips'].reverse()
    else:
        head['parent']['clips'][0]['element_ref' if alias else 'elementRef']['revision'] = 'changed'
    with pytest.raises(CommandError):
        replay(plan, head)
    with pytest.raises(CommandError):
        inverse(head, plan.records)
    assert bundle == original


# D72 authorized caller migration for this restored group: the removed resolver
# and direct publication transport are represented by the admitted document read
# and current TimelineSession commit seam; old transport predicates are retained
# at that seam, with canonical_document carrying the committed closure.
def test_d69_upstream_sameclip_exacthead_publication_preserves_closure_and_user_params(monkeypatch, configured):
    owner,bundle,ref=setup_owner(monkeypatch, configured)
    result=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='head',duration=6,params={'chaos':.8})
    assert result.ok, result.error
    body=owner.commit.call_args.args[2]
    document=owner.last_commit_data['canonical_document']
    parent=document['parent_revision']['payload']
    assert body['base_revision_id']=='head' and body['base_publication_id']=='publication'
    assert parent['clips'][0]['id']=='effect' and parent['clips'][0]['hold']==6
    assert parent['clips'][0]['params']=={'color':'#ff6b6b','seed':15,'chaos':.8}
    assert parent['clips'][1]==bundle['parent']['clips'][1]
    assert parent['config']['clips']==parent['clips']
    assert parent['config']['app']=={'elements':{},'other':'keep'}
    assert parent['config']['bundle']==bundle['parent']['config']['bundle']
    assert parent['registry']==bundle['parent']['registry']
    assert document['shot_revisions']==[] and document['internal_timeline_revisions']==[]
    assert bundle['parent']['clips'][0]['hold']==2
    assert result.data['new_head'] and result.data['clip_id']=='effect'


def test_d69_upstream_stale_head_and_concurrent_cas_reject_without_rebase(monkeypatch, configured):
    owner,_,ref=setup_owner(monkeypatch, configured)
    stale=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='stale')
    assert not stale.ok
    owner.commit.assert_not_called()
    owner.commit.return_value=DomainResult.failure(ErrorObject('conflict','CAS rejected',{'status':'conflict'}))
    owner.commit.side_effect=None
    rejected=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='head',duration=6)
    assert not rejected.ok and rejected.error.code=='conflict'
    assert owner.commit.call_count==1


@pytest.mark.parametrize('duration',[0,-1,float('inf'),float('nan')])
def test_d69_upstream_bad_duration_never_publishes(monkeypatch, configured, duration):
    owner,_,ref=setup_owner(monkeypatch, configured)
    result=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='head',duration=duration)
    assert not result.ok
    owner.commit.assert_not_called()


def test_d69_upstream_unadmitted_owner_and_sourceclip_reject(monkeypatch, configured):
    owner,bundle,ref=setup_owner(monkeypatch, configured)
    result=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element={**ref,'packId':'unadmitted'},expected_head='head')
    assert not result.ok
    owner.commit.assert_not_called()
    bundle['parent']['clips'][0]['asset']='source'
    result=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='head')
    assert not result.ok
    owner.commit.assert_not_called()
