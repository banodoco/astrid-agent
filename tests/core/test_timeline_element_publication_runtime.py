"""Native element batches against the actual retained-schema Runtime provider.

Explicit native requests and the public helper share the normal commit provider.
Helper recovery uses the existing session/outbox and accepted Runtime history.
"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.element import project_packs as packs
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import commands, commit
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._scalar_commands import capture_track_set
from tests.helpers.runtime import use_runtime
from tests.sdk import test_timeline_selected_schema_runtime as selected_schema

REF = selected_schema.REF
inverse = selected_schema.inverse
configured_project_pack = selected_schema.configured_project_pack
retained_pack = selected_schema.retained_pack
selected_runtime_env = selected_schema.selected_runtime_env

pytestmark = pytest.mark.integration
use_runtime()


def native_submitter(case):
    requests = []

    def submit(*, element, clip_id, expected_head, idempotency_key, duration=None, params=None):
        head = case.head()
        assert head['revision_id'] == expected_head
        source = case.service.get_document(case.project, case.pin['document_id'])
        pin = {'document_id': source['document_id'], 'version': source['version'],
               'content_digest': commit._digest(source['content'])}
        target = f'parent/clip:{clip_id}'
        calls = [{'schema_version': 1, 'op': 'clip.set-element', 'version': '1', 'select': target,
                  'args': {'elementRef': copy.deepcopy(element), 'schema_source': pin}}]
        if duration is not None:
            calls.append({'schema_version': 1, 'op': 'clip.set', 'version': '1', 'select': target,
                          'args': {'values': {'hold': duration}, 'clear': []}})
        if params:
            calls.append({'schema_version': 1, 'op': 'clip.set-params', 'version': '1', 'select': target,
                          'args': {'values': copy.deepcopy(params), 'clear': [], 'schema_source': pin}})
        body = case.request(calls, head)
        requests.append(copy.deepcopy(body))
        result = case.submit(body, idempotency_key)
        return DomainResult.success(result['data'], receipt=CommandReceipt.from_dict(result['receipt']),
                                    idempotency_key=idempotency_key)

    return submit, requests


@pytest.mark.parametrize('same_revision', [False, True])
def test_native_element_update_reload_guarded_inverse_and_exact_retry(retained_pack, monkeypatch,
                                                                     same_revision):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService

    case = retained_pack
    original = copy.deepcopy(case.bundle()['source_mapping']['_runtime_document'])
    before = copy.deepcopy(State(case.bundle()).entity(REF))
    selected = case.selected
    if not same_revision:
        case.definition['schema']['properties']['seed']['maximum'] = 20
        case.path.write_text(yaml.safe_dump(case.definition))
        admitted = packs.admit_project_pack('realm', case.project)
        selected = {key: admitted['elements'][0]['descriptor'][key]
                    for key in ('id', 'kind', 'packId', 'revision')}
        assert selected['revision'] != before['elementRef']['revision']
    # Source and build-cache availability never establish save-time authority.
    shutil.rmtree(case.source_root)
    shutil.rmtree(case.artifact_root)

    def forbidden(*args, **kwargs):
        raise AssertionError('element save used launcher, local source, cache, or renderer')

    monkeypatch.setattr(packs, '_open_client', forbidden)
    monkeypatch.setattr(packs, 'resolve_admitted_element', forbidden)
    monkeypatch.setattr(packs, 'artifact_root', forbidden)
    monkeypatch.setattr(packs, 'authoring_root', forbidden)
    submit, requests = native_submitter(case)
    result = submit(clip_id='effect',
        element=selected, expected_head=case.head()['revision_id'], duration=6,
        params={'seed': 8 if same_revision else 16, 'opaque': {'keep': [None, False, {}]}},
        idempotency_key='native-element-update')
    assert result.ok, result.error
    accepted = case.submit(requests[0], 'native-element-update')
    document = accepted['data']['canonical_document']
    row = State(commit.bundle_from_document(document)).entity(REF)
    assert result.data['accepted_revision_id'] == accepted['data']['accepted_revision_id']
    assert result.receipt.as_dict() == accepted['receipt']
    assert row['elementRef'] == selected and row['hold'] == 6
    assert row['params'] == {**before['params'], 'seed': 8 if same_revision else 16,
                             'opaque': {'keep': [None, False, {}]}}
    assert row['params']['future'] == before['params']['future']
    assert document['shot_revisions'] == original['shot_revisions']
    assert document['internal_timeline_revisions'] == original['internal_timeline_revisions']
    case.service.close()
    case.service = RuntimeService(case.root)
    case.service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    assert case.bundle()['source_mapping']['_runtime_document'] == document
    with monkeypatch.context() as retry:
        retry.setattr(commands, 'execute', forbidden)
        assert case.submit(requests[0], 'native-element-update') == accepted
    undone = case.submit(case.request([inverse(result.receipt.receipt_id)]), 'undo-element-update')
    restored = undone['data']['canonical_document']
    assert restored['parent_revision']['payload'] == original['parent_revision']['payload']
    assert restored['shot_revisions'] == original['shot_revisions']
    assert restored['internal_timeline_revisions'] == original['internal_timeline_revisions']
    # The native provider retains immutable schema provenance for redo.
    assert restored['dependency_manifest'] == {
        **original['dependency_manifest'],
        'documents': document['dependency_manifest']['documents']}
    redone = case.submit(case.request([inverse(undone['receipt']['receipt_id'])]), 'redo-element-update')
    assert redone['data']['canonical_document'] == document
    # A newer disjoint label survives the guarded inverse.
    case.submit(case.request([capture_track_set(case.bundle(), 'parent/track:picture',
                                               values={'label': 'Newer'})]), 'newer-label')
    disjoint = case.submit(case.request([inverse(redone['receipt']['receipt_id'])]), 'undo-after-label')
    restored_state = State(commit.bundle_from_document(disjoint['data']['canonical_document']))
    assert restored_state.entity(REF) == before
    assert restored_state.entity('parent/track:picture')['label'] == 'Newer'
    # The redo wrote the same element again; its history edge guards old undo.
    head = case.head()
    with pytest.raises(RuntimeErrorBase) as error:
        case.submit(case.request([inverse(result.receipt.receipt_id)]), 'refuse-element-aba')
    assert error.value.details['reason'] == 'intervening_write'
    assert case.head() == head


@pytest.mark.parametrize('damage', ['params', 'revision', 'element-only-revision'])
def test_native_element_batch_refusal_is_atomic(retained_pack, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    case = retained_pack
    submit, requests = native_submitter(case)
    original, head = copy.deepcopy(case.bundle()), case.head()
    selected = case.selected if damage == 'params' else {
        **case.selected, 'revision': 'sha256:' + '0' * 64}
    with pytest.raises(RuntimeErrorBase):
        submit(clip_id='effect',
            element=selected, expected_head=head['revision_id'], duration=6,
            params=(None if damage == 'element-only-revision' else
                    {'seed': 11 if damage == 'params' else 8}), idempotency_key='refused-element')
    assert len(requests) == 1
    assert case.head() == head and case.bundle() == original


@pytest.mark.parametrize('existing', [None, {}])
@pytest.mark.parametrize('params', [None, {}])
def test_element_only_retained_declaration_survives_removal_restart_and_replay(
        retained_pack, monkeypatch, existing, params):
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    case = retained_pack
    row = copy.deepcopy(State(case.bundle()).entity(REF))
    row.update(id='empty-effect', at=3, opaque={'keep': [None, {}, False]})
    row.pop('params')
    if existing is not None:
        row['params'] = existing
    ref = 'parent/clip:empty-effect'
    case.submit(case.request([capture_clip_add(case.bundle(), 'parent', row)]), 'empty-effect')
    before = copy.deepcopy(case.bundle()['source_mapping']['_runtime_document'])
    case.definition['schema']['properties']['seed']['maximum'] = 20
    case.path.write_text(yaml.safe_dump(case.definition))
    admitted = packs.admit_project_pack('realm', case.project)
    selected = {key: admitted['elements'][0]['descriptor'][key]
                for key in ('id', 'kind', 'packId', 'revision')}
    assert selected['revision'] != row['elementRef']['revision']
    source = case.service.get_document(case.project, admitted['documentId'])
    pin = {'document_id': source['document_id'], 'version': source['version'],
           'content_digest': commit._digest(source['content'])}
    snapshot = next(item['snapshot_object'] for item in source['content']['admissions']
                    if item['revision'] == selected['revision'])
    reads = []
    original_object = RequestCommitPort.document_object

    def read(port, source_pin, object_id):
        reads.append((copy.deepcopy(source_pin), object_id))
        return original_object(port, source_pin, object_id)

    monkeypatch.setattr(RequestCommitPort, 'document_object', read)
    submit, requests = native_submitter(case)
    result = submit(clip_id='empty-effect',
        element=selected, expected_head=case.head()['revision_id'], params=params,
        idempotency_key='element-only')
    assert result.ok, result.error
    assert [call['op'] for call in requests[0]['commands']] == ['clip.set-element']
    assert requests[0]['commands'][0]['args']['schema_source'] == pin
    assert reads == [(pin, snapshot['object_id'])]
    accepted = case.submit(requests[0], 'element-only')
    document = accepted['data']['canonical_document']
    assert State(commit.bundle_from_document(document)).entity(ref) == {**row, 'elementRef': selected}
    assert document['dependency_manifest']['documents'] == [case.pin, pin]
    entry = case.service._timeline_commits()[0].history_entry(
        case.project, 'film', accepted['receipt']['receipt_id'])
    record = entry['records']['commands'][0]
    assert [write['path'] for write in record['writes']] == [['elementRef']]
    assert next(dep['pin'] for dep in record['dependencies'] if dep['key'] == 'params.snapshot') == {
        'source': pin, 'element': selected, 'object': snapshot}
    # A later declaration and removal of current selection cannot retarget history.
    case.definition['schema']['properties']['seed']['maximum'] = 30
    case.path.write_text(yaml.safe_dump(case.definition))
    newer = packs.admit_project_pack('realm', case.project)
    assert newer['elements'][0]['descriptor']['revision'] != selected['revision']
    current = case.service.get_document(case.project, source['document_id'])
    case.service.update_document(case.project, current['document_id'], {
        'expected_version': current['version'], 'content': {**current['content'], 'admissions': []}},
        idempotency_key='remove-current-selection')
    shutil.rmtree(case.source_root)
    shutil.rmtree(case.artifact_root)
    case.service.close()
    case.service = RuntimeService(case.root)
    case.service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    assert case.bundle()['source_mapping']['_runtime_document'] == document

    def forbidden(*args, **kwargs):
        raise AssertionError('element-only used launcher, local source, cache, or renderer')

    monkeypatch.setattr(packs, '_open_client', forbidden)
    monkeypatch.setattr(packs, 'resolve_admitted_element', forbidden)
    monkeypatch.setattr(packs, 'artifact_root', forbidden)
    monkeypatch.setattr(packs, 'authoring_root', forbidden)
    with monkeypatch.context() as retry:
        retry.setattr(commands, 'execute', forbidden)
        assert case.submit(requests[0], 'element-only') == accepted
    reads.clear()
    undone = case.submit(case.request([inverse(result.receipt.receipt_id)]), 'undo-element-only')
    restored = undone['data']['canonical_document']
    assert restored['parent_revision']['payload'] == before['parent_revision']['payload']
    assert restored['dependency_manifest']['documents'] == [case.pin, pin]
    assert reads == [(pin, snapshot['object_id'])]
    # A fresh intent is based on a closure that authorizes this retained pin.
    replay_body = case.request(requests[0]['commands'])
    case.submit(case.request([capture_track_set(case.bundle(), 'parent/track:picture',
                                               values={'label': 'Newer'})]), 'concurrent-label')
    reads.clear()
    replayed = case.submit(replay_body, 'replay-element-only')
    assert replayed['data']['status'] == 'merged'
    replayed_document = replayed['data']['canonical_document']
    replayed_state = State(commit.bundle_from_document(replayed_document))
    assert replayed_state.entity(ref) == {**row, 'elementRef': selected}
    assert replayed_state.entity('parent/track:picture')['label'] == 'Newer'
    assert replayed_document['dependency_manifest']['documents'] == [case.pin, pin]
    assert reads and all(item == (pin, snapshot['object_id']) for item in reads)
    undone = case.submit(case.request([inverse(replayed['receipt']['receipt_id'])]), 'undo-replay')
    redone = case.submit(case.request([inverse(undone['receipt']['receipt_id'])]), 'redo-element-only')
    assert redone['data']['canonical_document'] == replayed_document


def raw_element(case):
    return {'schema_version': 1, 'op': 'clip.set-element', 'version': '1',
            'select': 'parent/clip:effect',
            'args': {'elementRef': copy.deepcopy(case.selected), 'schema_source': copy.deepcopy(case.pin)}}


@pytest.mark.parametrize('damage', ['missing-pin', 'inline-schema', 'pin-version', 'pin-digest',
    'id', 'kind', 'owner', 'revision', 'revoked', 'legacy', 'missing-object', 'corrupt-object',
    'foreign-project', 'authorization'])
def test_raw_element_only_refusal_is_atomic(retained_pack, damage):
    from runtime_protocol.errors import ForbiddenError, RuntimeErrorBase

    case = retained_pack
    command = raw_element(case)
    def authorize():
        pass
    if damage == 'missing-pin':
        del command['args']['schema_source']
    elif damage == 'inline-schema':
        command['args']['schema_source']['schema'] = {'type': 'object'}
    elif damage == 'pin-version':
        command['args']['schema_source']['version'] += 1
    elif damage == 'pin-digest':
        command['args']['schema_source']['content_digest'] = 'sha256:' + '0' * 64
    elif damage == 'revision':
        command['args']['elementRef']['revision'] = 'sha256:' + '0' * 64
    elif damage in ('id', 'kind', 'owner'):
        command['args']['elementRef'][{'owner': 'packId'}.get(damage, damage)] = (
            'transition' if damage == 'kind' else 'wrong')
    elif damage in ('revoked', 'legacy'):
        row = case.service.get_document(case.project, case.pin['document_id'])
        content = copy.deepcopy(row['content'])
        if damage == 'revoked':
            content['admissions'] = []
        else:
            for admission in content['admissions']:
                del admission['snapshot_object']
        updated = case.service.update_document(case.project, row['document_id'],
            {'expected_version': row['version'], 'content': content}, idempotency_key='damage')['data']
        command['args']['schema_source'] = {'document_id': updated['document_id'],
            'version': updated['version'], 'content_digest': commit._digest(updated['content'])}
    elif damage in ('missing-object', 'corrupt-object'):
        obj = next(iter(case.options['schema_inputs'][0][1]))
        path = case.service.cas.path_for(obj.removeprefix('sha256:'))
        if damage == 'missing-object':
            path.unlink()
        else:
            path.write_bytes(b'corrupt')
    elif damage == 'foreign-project':
        foreign = case.service.create_project({'slug': 'foreign', 'name': 'Foreign'})['id']
        from tests.core.test_project_packs import seed
        seed(foreign)
        admitted = packs.admit_project_pack('realm', foreign)
        row = case.service.get_document(foreign, admitted['documentId'])
        command['args']['schema_source'] = {'document_id': row['document_id'],
            'version': row['version'], 'content_digest': commit._digest(row['content'])}
    else:
        def authorize():
            raise ForbiddenError('Element authorization refusal')
    head, bundle = case.head(), copy.deepcopy(case.bundle())
    history = case.service.list_timeline_commits(case.project, 'film', authorize=lambda: None)
    with pytest.raises(RuntimeErrorBase):
        case.submit(case.request([command]), 'raw-element-refusal', authorize=authorize)
    assert case.head() == head and case.bundle() == bundle
    assert case.service.list_timeline_commits(
        case.project, 'film', authorize=lambda: None) == history


def test_another_actually_admitted_owner_cannot_replace_selected_owner(retained_pack):
    from runtime_protocol.errors import RuntimeErrorBase

    case = retained_pack
    location = packs.init_project_pack('realm', case.project, collection='other_owner')
    root = Path(location['packRoot'])
    shutil.copytree(case.source_root / 'rendering', root / 'rendering', dirs_exist_ok=True)
    definition = copy.deepcopy(case.definition)
    definition['pack_id'] = 'other_owner'
    (root / 'rendering/elements/effects/reveal/element.yaml').write_text(yaml.safe_dump(definition))
    manifest = yaml.safe_load((root / 'pack.yaml').read_text())
    manifest['rendering'] = yaml.safe_load((case.source_root / 'pack.yaml').read_text())['rendering']
    (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    admitted = packs.admit_project_pack('realm', case.project, collection='other_owner')
    selected = {key: admitted['elements'][0]['descriptor'][key]
                for key in ('id', 'kind', 'packId', 'revision')}
    assert selected['id'] == case.selected['id'] and selected['kind'] == case.selected['kind']
    assert selected['packId'] != case.selected['packId']
    row = case.service.get_document(case.project, admitted['documentId'])
    command = raw_element(case)
    command['args'] = {'elementRef': selected, 'schema_source': {'document_id': row['document_id'],
        'version': row['version'], 'content_digest': commit._digest(row['content'])}}
    head, bundle = case.head(), copy.deepcopy(case.bundle())
    history = case.service.list_timeline_commits(case.project, 'film', authorize=lambda: None)
    with pytest.raises(RuntimeErrorBase, match='pack owner'):
        case.submit(case.request([command]), 'other-admitted-owner')
    assert case.head() == head and case.bundle() == bundle
    assert case.service.list_timeline_commits(
        case.project, 'film', authorize=lambda: None) == history



@pytest.fixture
def helper_client(retained_pack, monkeypatch, tmp_path):
    """Generated method boundary over the actual immutable NORMAL Runtime service."""
    import json
    from types import SimpleNamespace

    from runtime_protocol.errors import RuntimeErrorBase

    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline.outbox import request_bytes
    from astrid.sdk.workspace_client import WorkspaceClient
    from astrid_runtime_client import ApiError

    case = retained_pack
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'support'))
    calls, lost_ack, before_submit = [], [False], [None]

    class Generated:
        def get_project(self, project):
            return case.service.get_project(project)

        def get_document(self, project, document):
            try:
                return case.service.get_document(project, document)
            except RuntimeErrorBase as exc:
                raise ApiError(404, exc.code, str(exc), details=exc.details) from exc

        def get_project_verified_document(self, project, pin):
            return case.service.get_project_verified_document(project, pin, authorize=lambda: None)

        def get_project_verified_document_object(self, project, pin, object_id):
            return case.service.get_project_verified_document_object(
                project, pin, object_id, authorize=lambda: None)

        def get_object(self, object_id):
            return {'data': case.service.cas.read(object_id.removeprefix('sha256:'))}

        def list_timeline_commits(self, project, timeline, *, cursor=None, limit=50):
            return case.service.list_timeline_commits(
                project, timeline, cursor=cursor, limit=limit, authorize=lambda: None)

        def commit_timeline(self, project, timeline, body, *, idempotency_key):
            outbox = next((tmp_path / 'support/timeline-outbox').glob('*.json'))
            durable = json.loads(outbox.read_bytes())
            raw = request_bytes(body)
            assert durable['flight']['body'].encode() == raw
            assert durable['flight']['key'] == idempotency_key
            calls.append((raw, idempotency_key))
            if before_submit[0] is not None:
                callback, before_submit[0] = before_submit[0], None
                callback()
            try:
                result = case.submit(body, idempotency_key)
            except RuntimeErrorBase as exc:
                raise ApiError(409, exc.code, str(exc), details=exc.details) from exc
            if lost_ack[0]:
                lost_ack[0] = False
                raise ConnectionError('lost acknowledgement after durable acceptance')
            return result

    def connect():
        transport = WorkspaceClient.__new__(WorkspaceClient)
        transport._generated = Generated()
        transport.endpoint = 'http://127.0.0.1:1'
        transport._last_handshake = {'realm_id': 'realm', 'actor_id': 'author'}
        owner = RemoteTimelines(transport)
        owner._resolve_timeline = lambda *a, **kw: DomainResult.success({
            'project_id': case.project, 'timeline_id': 'film'})
        owner.open_composition = lambda *a, **kw: DomainResult.success({
            'native_inspection': {'project_id': case.project, 'timeline_id': 'film'},
            'summary': {'head_revision_id': case.head()['revision_id']}})

        def opened(project, timeline, *, revision_id=None):
            head = case.head()['revision_id']
            revision = revision_id or head
            document = case.service._timeline_commits()[0].revision_closure(project, timeline, revision)
            return DomainResult.success({'project_id': project, 'timeline_id': timeline,
                'head_revision_id': head, 'revision_id': revision,
                'bundle': commit.bundle_from_document(document)})

        owner.open_bundle = opened
        return owner

    for name in ('publish_parent_composition', 'save_timeline', 'update_timeline'):
        monkeypatch.setattr(case.service, name, lambda *a, **kw: pytest.fail('legacy writer called'),
                            raising=False)
    return SimpleNamespace(connect=connect, calls=calls, lost_ack=lost_ack, before_submit=before_submit,
                           outbox=lambda: next((tmp_path / 'support/timeline-outbox').glob('*.json')))


@pytest.mark.parametrize('key', [None, 'public-element'])
@pytest.mark.parametrize('same_revision', [False, True])
def test_public_helper_restart_exact_retry_later_head_inverse_redo(
        retained_pack, helper_client, monkeypatch, same_revision, key):
    import json

    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService

    from astrid.sdk.timeline_elements import update_parent_element

    case, client = retained_pack, helper_client
    original = copy.deepcopy(case.bundle()['source_mapping']['_runtime_document'])
    before = copy.deepcopy(State(case.bundle()).entity(REF))
    selected = case.selected
    if not same_revision:
        case.definition['schema']['properties']['seed']['maximum'] = 20
        case.path.write_text(yaml.safe_dump(case.definition))
        admission = packs.admit_project_pack('realm', case.project)
        selected = {k: admission['elements'][0]['descriptor'][k] for k in ('id', 'kind', 'packId', 'revision')}
    expected = case.head()['revision_id']
    kwargs = dict(realm='realm', clip_id='effect', element=selected, expected_head=expected,
                  duration=6, params={'seed': 8, 'opaque': {'keep': [None, False, {}]}},
                  clear_params=['future'], idempotency_key=key)
    client.lost_ack[0] = True
    failed = update_parent_element(client.connect(), case.project, 'film', **kwargs)
    assert not failed.ok
    flight = json.loads(client.outbox().read_bytes())['flight']
    assert flight['key'] == (key or failed.idempotency_key)
    request = json.loads(flight['body'])
    assert [call['op'] for call in request['commands']] == (
        ['clip.set', 'clip.set-params'] if same_revision else
        ['clip.set-element', 'clip.set', 'clip.set-params'])
    if not same_revision:
        assert request['commands'][0]['selections'] == [{
            'key': 'element.targets', 'query_version': 1, 'owner': 'parent',
            'members': [REF], 'order': [REF], 'boundaries': [REF], 'policy': 'guard-membership'}]
    assert request['commands'][-1]['bindings']['scalar.target'] == REF
    assert request['commands'][-1]['bindings']['params.element'] == REF
    for changed in ({'duration': 7}, {'idempotency_key': 'another-key'},
                    {'clip_id': 'missing'}, {'expected_head': 'another-head'}):
        refused = update_parent_element(client.connect(), case.project, 'film', **{**kwargs, **changed})
        assert not refused.ok and len(client.calls) == 1
        assert json.loads(client.outbox().read_bytes())['flight'] == flight
    accepted = case.submit(request, flight['key'])
    pin = request['commands'][-1]['args']['schema_source']
    document = accepted['data']['canonical_document']
    row = State(commit.bundle_from_document(document)).entity(REF)
    assert row == {**before, 'elementRef': selected, 'hold': 6,
                   'params': {'seed': 8, 'opaque': {'keep': [None, False, {}]}}}
    # Advance both the timeline and the declaration, then remove current selection.
    case.submit(case.request([capture_track_set(case.bundle(), 'parent/track:picture',
                                               values={'label': 'Later'})]), 'later-head')
    case.definition['schema']['properties']['seed']['maximum'] = 30
    case.path.write_text(yaml.safe_dump(case.definition))
    packs.admit_project_pack('realm', case.project)
    current = case.service.get_document(case.project, pin['document_id'])
    assert current['version'] > pin['version']
    case.service.update_document(case.project, pin['document_id'], {
        'expected_version': current['version'], 'content': {**current['content'], 'admissions': []}},
        idempotency_key='remove-helper-selection')
    shutil.rmtree(case.source_root)
    shutil.rmtree(case.artifact_root)
    case.service.close()
    case.service = RuntimeService(case.root)
    case.service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    later = case.head()

    def forbidden(*a, **kw):
        pytest.fail('helper recaptured original intent, read current admission, or called legacy writer')

    with monkeypatch.context() as retry:
        retry.setattr('astrid.sdk.timeline_elements.execute', forbidden)
        retry.setattr('astrid.sdk.timeline_elements._read_document', forbidden)
        retry.setattr(commands, 'execute', forbidden)
        result = update_parent_element(client.connect(), case.project, 'film', **kwargs)
        assert result.ok, result.error
        assert client.calls == [(flight['body'].encode(), flight['key'])] * 2
        assert result.receipt.as_dict() == accepted['receipt']
        assert result.data['new_head'] == accepted['data']['accepted_revision_id']
        assert result.data['before'] == {'element': before['elementRef'], 'duration': 2,
                                          'params': before['params']}
        assert json.loads(client.outbox().read_bytes())['flight'] is None
        assert json.loads(client.outbox().read_bytes())['pending'] == []
        # Acknowledged invocation uses accepted Runtime history, never recapture.
        kwargs['idempotency_key'] = flight['key']
        again = update_parent_element(client.connect(), case.project, 'film', **kwargs)
        assert again == result and len(client.calls) == 2
        changed = update_parent_element(client.connect(), case.project, 'film', **{**kwargs, 'duration': 7})
        assert not changed.ok and len(client.calls) == 2
    assert case.head() == later
    undone = case.submit(case.request([inverse(result.receipt.receipt_id)]), 'undo-helper')
    restored = State(commit.bundle_from_document(undone['data']['canonical_document']))
    assert restored.entity(REF) == before
    assert restored.entity('parent/track:picture')['label'] == 'Later'
    expected_pins = [case.pin] if pin == case.pin else [case.pin, pin]
    assert undone['data']['canonical_document']['dependency_manifest']['documents'] == expected_pins
    redone = case.submit(case.request([inverse(undone['receipt']['receipt_id'])]), 'redo-helper')
    redone_state = State(commit.bundle_from_document(redone['data']['canonical_document']))
    assert redone_state.entity(REF) == row
    assert redone['data']['canonical_document']['shot_revisions'] == original['shot_revisions']
    assert redone['data']['canonical_document']['internal_timeline_revisions'] == original['internal_timeline_revisions']
    head = case.head()
    with pytest.raises(RuntimeErrorBase) as error:
        case.submit(case.request([inverse(result.receipt.receipt_id)]), 'helper-aba')
    assert error.value.details['reason'] == 'intervening_write'
    assert case.head() == head


@pytest.mark.parametrize('existing', [None, {}])
@pytest.mark.parametrize('params', [None, {}])
def test_public_helper_element_only_keeps_literal_params_and_exact_inverse(
        retained_pack, helper_client, existing, params):
    case = retained_pack
    row = copy.deepcopy(State(case.bundle()).entity(REF))
    row['id'] = 'empty-effect'
    row.pop('params')
    if existing is not None:
        row['params'] = existing
    case.submit(case.request([capture_clip_add(case.bundle(), 'parent', row)]), 'helper-empty-row')
    original = copy.deepcopy(case.bundle()['source_mapping']['_runtime_document'])
    result = helper_client.connect().update_parent_element(
        case.project, 'film', realm='realm', clip_id='empty-effect', element=case.selected,
        expected_head=case.head()['revision_id'], params=params)
    assert result.ok, result.error
    accepted = result.receipt.result
    commands_sent = accepted['commands']
    assert [command['op'] for command in commands_sent] == ['clip.set']
    assert State(commit.bundle_from_document(accepted['canonical_document'])).entity(
        'parent/clip:empty-effect') == row
    undone = case.submit(case.request([inverse(result.receipt.receipt_id)]), 'undo-helper-empty')
    assert undone['data']['canonical_document']['parent_revision']['payload'] == original['parent_revision']['payload']
    redone = case.submit(case.request([inverse(undone['receipt']['receipt_id'])]), 'redo-helper-empty')
    assert redone['data']['canonical_document'] == accepted['canonical_document']


def test_public_helper_optional_session_rebases_over_disjoint_edit(retained_pack, helper_client):
    from types import SimpleNamespace

    from astrid.sdk.timeline.sync import TimelineSession

    case, client = retained_pack, helper_client
    owner = client.connect()
    client_facade = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner)
    session = TimelineSession.open(case.project, 'film', client=client_facade)
    expected = case.head()['revision_id']
    client.before_submit[0] = lambda: case.submit(case.request([
        capture_track_set(case.bundle(), 'parent/track:picture', values={'label': 'Concurrent'})]),
        'concurrent-helper-label')
    result = owner.update_parent_element(case.project, 'film', realm='realm', clip_id='effect',
        element=case.selected, expected_head=expected, duration=3, session=session)
    assert result.ok, result.error
    assert result.receipt.result['status'] == 'merged'
    state = State(case.bundle())
    assert state.entity(REF)['hold'] == 3
    assert state.entity('parent/track:picture')['label'] == 'Concurrent'
    undone = case.submit(case.request([inverse(result.receipt.receipt_id)]), 'undo-rebased-helper')
    restored = State(commit.bundle_from_document(undone['data']['canonical_document']))
    assert restored.entity(REF)['hold'] == 2
    assert restored.entity('parent/track:picture')['label'] == 'Concurrent'


@pytest.mark.parametrize('damage', ['realm', 'id', 'kind', 'owner', 'revision', 'session-scope'])
def test_public_helper_scope_and_identity_refusal_is_atomic(retained_pack, helper_client, damage):
    from types import SimpleNamespace

    from astrid.sdk.timeline.sync import TimelineSession

    case, client = retained_pack, helper_client
    owner = client.connect()
    kwargs = dict(realm='realm', clip_id='effect', element=case.selected,
                  expected_head=case.head()['revision_id'])
    if damage == 'realm':
        kwargs['realm'] = 'foreign'
    elif damage == 'session-scope':
        facade = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner)
        session = TimelineSession.open(case.project, 'film', client=facade)
        session.project_id = 'foreign'
        kwargs['session'] = session
    else:
        kwargs['element'] = {**case.selected, {'owner': 'packId'}.get(damage, damage): 'wrong'}
    before, head = copy.deepcopy(case.bundle()), case.head()
    result = owner.update_parent_element(case.project, 'film', **kwargs)
    assert not result.ok and result.error.code == 'validation_error'
    assert client.calls == [] and case.head() == head and case.bundle() == before


def test_public_helper_native_refusal_preserves_error_and_flight(retained_pack, helper_client, monkeypatch):
    import json

    from runtime_protocol.errors import ForbiddenError

    case = retained_pack
    before, head = copy.deepcopy(case.bundle()), case.head()
    error = ForbiddenError('Element commit denied', details={'reason': 'helper_authorization'})
    monkeypatch.setattr(case, 'submit', lambda *a, **kw: (_ for _ in ()).throw(error))
    result = helper_client.connect().update_parent_element(
        case.project, 'film', realm='realm', clip_id='effect', element=case.selected,
        expected_head=head['revision_id'], duration=3, idempotency_key='helper-denied')
    assert not result.ok and result.error.code == error.code
    assert result.error.details['reason'] == 'helper_authorization'
    assert result.idempotency_key == 'helper-denied'
    assert case.head() == head and case.bundle() == before
    assert json.loads(helper_client.outbox().read_bytes())['flight']['key'] == 'helper-denied'


@pytest.mark.parametrize('existing', [None, {}, {'seed': 4, 'opaque': [None, False, {}]}])
@pytest.mark.parametrize('params', [None, {}])
@pytest.mark.parametrize('retained_pack', [False], indirect=True)
def test_legacy_same_selected_hold_reload_inverse_retry(
        retained_pack, helper_client, monkeypatch, existing, params):
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    from astrid.sdk.timeline_elements import update_parent_element

    case, client = retained_pack, helper_client
    row = copy.deepcopy(State(case.bundle()).entity(REF))
    row['id'] = 'legacy-effect'
    row.pop('params')
    if existing is not None:
        row['params'] = existing
    case.submit(case.request([capture_clip_add(case.bundle(), 'parent', row)]), 'legacy-effect')
    current = case.service.get_document(case.project, case.pin['document_id'])
    content = copy.deepcopy(current['content'])
    for admission in content['admissions']:
        admission.pop('snapshot_object')
    case.service.update_document(case.project, current['document_id'], {
        'expected_version': current['version'], 'content': content}, idempotency_key='legacy-admission')
    assert packs.resolve_admitted_element(
        'realm', case.project, case.selected)['descriptor']['revision'] == case.selected['revision']
    original = copy.deepcopy(case.bundle()['source_mapping']['_runtime_document'])
    kwargs = dict(realm='realm', clip_id='legacy-effect', element=case.selected,
                  expected_head=case.head()['revision_id'], duration=6, params=params,
                  idempotency_key='legacy-hold')

    def forbidden(*args, **kwargs):
        pytest.fail('legacy HOLD requested schema, snapshot, local disk, or re-admission')

    monkeypatch.setattr('astrid.sdk.timeline._selected_param_authority.resolve_selected_param_source', forbidden)
    monkeypatch.setattr('astrid.sdk.timeline_elements._read_document', forbidden)
    monkeypatch.setattr(RequestCommitPort, 'config_source', forbidden)
    monkeypatch.setattr(RequestCommitPort, 'document_object', forbidden)
    monkeypatch.setattr(packs, 'resolve_admitted_element', forbidden)
    monkeypatch.setattr(packs, 'admit_project_pack', forbidden)
    client.lost_ack[0] = True
    assert not update_parent_element(client.connect(), case.project, 'film', **kwargs).ok
    flight = json.loads(client.outbox().read_bytes())['flight']
    body = json.loads(flight['body'])
    for changed in ({'duration': 7}, {'idempotency_key': 'another-key'},
                    {'element': {**case.selected, 'revision': 'another-revision'}}):
        refused = update_parent_element(client.connect(), case.project, 'film', **{**kwargs, **changed})
        assert not refused.ok and len(client.calls) == 1
        assert json.loads(client.outbox().read_bytes())['flight'] == flight
    assert [command['op'] for command in body['commands']] == ['clip.set']
    assert body['commands'][0]['args'] == {'values': {'hold': 6}, 'clear': []}
    assert body['commands'][0]['selections'][0]['key'] == 'element.targets'
    accepted = case.submit(body, flight['key'])
    document = accepted['data']['canonical_document']
    assert State(commit.bundle_from_document(document)).entity('parent/clip:legacy-effect') == {**row, 'hold': 6}
    assert document['dependency_manifest'] == original['dependency_manifest']
    case.service.close()
    case.service = RuntimeService(case.root)
    case.service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    assert case.bundle()['source_mapping']['_runtime_document'] == document
    with monkeypatch.context() as retry:
        retry.setattr('astrid.sdk.timeline_elements.execute', forbidden)
        retry.setattr(commands, 'execute', forbidden)
        result = update_parent_element(client.connect(), case.project, 'film', **kwargs)
        assert result.ok, result.error
        assert result.receipt.as_dict() == accepted['receipt']
        assert update_parent_element(client.connect(), case.project, 'film', **kwargs) == result
        assert client.calls == [(flight['body'].encode(), flight['key'])] * 2
    undone = case.submit(case.request([inverse(result.receipt.receipt_id)]), 'undo-legacy')
    assert undone['data']['canonical_document']['parent_revision']['payload'] == original['parent_revision']['payload']
    redone = case.submit(case.request([inverse(undone['receipt']['receipt_id'])]), 'redo-legacy')
    assert redone['data']['canonical_document'] == document


@pytest.mark.parametrize('params', [{'seed': 4.0}, {'future': {'keep': [None, False, {}]}}])
def test_legacy_unchanged_params_noop_needs_no_snapshot(retained_pack, helper_client, monkeypatch, params):
    case = retained_pack
    current = case.service.get_document(case.project, case.pin['document_id'])
    content = copy.deepcopy(current['content'])
    for admission in content['admissions']:
        admission.pop('snapshot_object')
    case.service.update_document(case.project, current['document_id'], {
        'expected_version': current['version'], 'content': content}, idempotency_key='legacy-admission')
    assert packs.resolve_admitted_element(
        'realm', case.project, case.selected)['descriptor']['revision'] == case.selected['revision']
    original = copy.deepcopy(case.bundle()['source_mapping']['_runtime_document'])

    def forbidden(*args, **kwargs):
        pytest.fail('unchanged params requested a schema snapshot')

    monkeypatch.setattr('astrid.sdk.timeline_elements._read_document', forbidden)
    result = helper_client.connect().update_parent_element(
        case.project, 'film', realm='realm', clip_id='effect', element=case.selected,
        expected_head=case.head()['revision_id'], params=params)
    assert result.ok, result.error
    calls = result.receipt.result['commands']
    assert [call['op'] for call in calls] == ['clip.set-params']
    assert calls[0]['args'] == {'values': params, 'clear': [], 'schema_source': case.pin}
    document = result.receipt.result['canonical_document']
    assert document['parent_revision']['payload'] == original['parent_revision']['payload']
    assert document['dependency_manifest'] == original['dependency_manifest']
    entry = case.service._timeline_commits()[0].history_entry(case.project, 'film', result.receipt.receipt_id)
    assert [write['path'] for write in entry['records']['commands'][0]['writes']] == [
        ['params', key] for key in sorted(params)]


@pytest.mark.parametrize('params', [None, {'future': {'keep': [None, 0, {}]}}])
def test_legacy_reselection_refuses_while_original_pin_params_succeed(
        retained_pack, helper_client, params):
    case = retained_pack
    selected = case.selected
    if params is None:
        case.definition['schema']['properties']['seed']['maximum'] = 20
        case.path.write_text(yaml.safe_dump(case.definition))
        admission = packs.admit_project_pack('realm', case.project)
        selected = {key: admission['elements'][0]['descriptor'][key]
                    for key in ('id', 'kind', 'packId', 'revision')}
        assert selected['revision'] != case.selected['revision']
    current = case.service.get_document(case.project, case.pin['document_id'])
    content = copy.deepcopy(current['content'])
    for admission in content['admissions']:
        admission.pop('snapshot_object')
    case.service.update_document(case.project, current['document_id'], {
        'expected_version': current['version'], 'content': content}, idempotency_key='legacy-admission')
    assert packs.resolve_admitted_element(
        'realm', case.project, selected)['descriptor']['revision'] == selected['revision']
    before, head = copy.deepcopy(case.bundle()), case.head()
    history = case.service.list_timeline_commits(case.project, 'film', authorize=lambda: None)
    result = helper_client.connect().update_parent_element(
        case.project, 'film', realm='realm', clip_id='effect', element=selected,
        expected_head=head['revision_id'], duration=6, params=params)
    if params is None:
        assert not result.ok and result.error.message == 'pack snapshot unavailable; re-admit pack'
        assert helper_client.calls == [] and case.head() == head and case.bundle() == before
        assert case.service.list_timeline_commits(case.project, 'film', authorize=lambda: None) == history
    else:
        # The accepted P1 now authorizes PARAM even after the live admission
        # loses its portable snapshot. Preserve the bool/number distinction.
        assert result.ok, result.error
        assert result.data['params']['future'] == params['future']
        calls = result.receipt.result['commands']
        assert [call['op'] for call in calls] == ['clip.set', 'clip.set-params']
        assert calls[-1]['args']['schema_source'] == case.pin
    assert case.service.get_document(case.project, current['document_id'])['content'] == content


def test_preexisting_element_hold_flight_retries_exact_body(retained_pack, helper_client, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase

    from astrid.sdk.timeline.sync import TimelineSession
    from astrid.sdk.timeline_elements import _calls
    from astrid.sdk.workspace_client import WorkspaceClientError

    case, client = retained_pack, helper_client
    owner = client.connect()
    facade = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner)
    session = TimelineSession.open(case.project, 'film', client=facade)
    expected = case.head()['revision_id']
    # Freeze the helper's pre-fix batch; the new HOLD shortcut must not replace it.
    calls = _calls('effect', case.selected, 3, None, (), case.pin)
    session._stage(lambda state: commands.execute(case.bundle(), calls,
                   schema_inputs=case.options['schema_inputs'], realm_id='realm').commands)
    client.lost_ack[0] = True
    with pytest.raises(WorkspaceClientError, match='lost acknowledgement'):
        session.flush(idempotency_key='old-flight')
    flight = json.loads(client.outbox().read_bytes())['flight']
    changed = json.loads(flight['body'])['commands']
    changed[0]['args']['schema_source']['version'] += 1
    with pytest.raises(ValueError, match='pending intent differs'):
        session.commit_many(changed, idempotency_key='old-flight')
    assert json.loads(client.outbox().read_bytes())['flight'] == flight
    assert len(client.calls) == 1

    def forbidden(*args, **kwargs):
        pytest.fail('preexisting flight recaptured or reread snapshot')

    monkeypatch.setattr('astrid.sdk.timeline_elements.execute', forbidden)
    monkeypatch.setattr('astrid.sdk.timeline_elements._read_document', forbidden)
    result = owner.update_parent_element(
        case.project, 'film', realm='realm', clip_id='effect', element=case.selected,
        expected_head=expected, duration=3, idempotency_key='old-flight')
    assert result.ok, result.error
    assert client.calls == [(flight['body'].encode(), flight['key'])] * 2
    changed_body = json.loads(flight['body'])
    changed_body['commands'][0]['args']['schema_source']['version'] += 1
    head = case.head()
    with pytest.raises(RuntimeErrorBase):
        case.submit(changed_body, 'old-flight')
    assert case.head() == head


def test_acknowledged_rebased_lost_ack_restart_pagination_and_typed_intent(
        retained_pack, helper_client, monkeypatch):
    from runtime_protocol.service import RuntimeService

    from astrid.sdk.timeline_elements import update_parent_element

    case, client = retained_pack, helper_client
    expected = case.head()['revision_id']
    kwargs = dict(realm='realm', clip_id='effect', element=case.selected,
                  expected_head=expected, duration=3,
                  params={'opaque': {'nested': [True, {'value': False}]}},
                  idempotency_key='review-original')
    client.before_submit[0] = lambda: case.submit(case.request([
        capture_track_set(case.bundle(), 'parent/track:picture', values={'label': 'Before accept'})]),
        'review-before-accept')
    client.lost_ack[0] = True
    failed = update_parent_element(client.connect(), case.project, 'film', **kwargs)
    assert not failed.ok
    flight = json.loads(client.outbox().read_bytes())['flight']
    accepted = case.submit(json.loads(flight['body']), flight['key'])
    assert accepted['data']['status'] == 'rebased'
    assert accepted['data']['base_revision_id'] == expected
    assert accepted['data']['previous_revision_id'] != expected
    case.submit(case.request([capture_track_set(case.bundle(), 'parent/track:picture',
                        values={'label': 'After accept'})]), 'review-after-accept')
    case.service.close()
    case.service = RuntimeService(case.root)
    case.service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    head = case.head()

    def forbidden(*a, **kw):
        pytest.fail('recovered helper recaptured or read current admission')

    monkeypatch.setattr('astrid.sdk.timeline_elements.execute', forbidden)
    monkeypatch.setattr('astrid.sdk.timeline_elements._read_document', forbidden)
    result = update_parent_element(client.connect(), case.project, 'film', **kwargs)
    assert result.ok, result.error
    assert result.receipt.as_dict() == accepted['receipt']
    assert client.calls == [(flight['body'].encode(), flight['key'])] * 2
    owner = client.connect()
    history = owner.history
    cursors = []

    def paginated(project, timeline, *, cursor=None, limit=50):
        cursors.append(cursor)
        return history(project, timeline, cursor=cursor, limit=1)

    owner.history = paginated
    again = update_parent_element(owner, case.project, 'film', **kwargs)
    assert again == result
    assert any(cursor is not None for cursor in cursors)
    equivalent = update_parent_element(owner, case.project, 'film', **{**kwargs, 'duration': 3.0})
    assert equivalent == result
    changed = update_parent_element(owner, case.project, 'film', **{
        **kwargs, 'params': {'opaque': {'nested': [1, {'value': 0}]}}})
    assert not changed.ok and 'different original intent' in changed.error.message
    assert len(client.calls) == 2 and case.head() == head


@pytest.mark.parametrize('clear_only', [False, True])
def test_param_only_helper_preserves_original_pin_without_reselection(retained_pack, helper_client, clear_only):
    from tests.core import test_project_packs

    case = retained_pack
    selection = {'schema_version': 1, 'op': 'clip.set-element', 'version': '1', 'select': REF,
                 'args': {'elementRef': case.selected, 'schema_source': case.pin}}
    case.submit(case.request([selection]), 'original-explicit-selection')
    manifest = yaml.safe_load((case.source_root / 'pack.yaml').read_text())
    test_project_packs.add_second_element(case.source_root, case.path.parent, manifest)
    admission = packs.admit_project_pack('realm', case.project)
    current = case.service.get_document(case.project, admission['documentId'])
    assert current['version'] > case.pin['version']
    assert next(item for item in current['content']['admissions'] if item['id'] == case.selected['id']) == (
        case.options['schema_inputs'][0][0]['document']['content']['admissions'][0])
    result = helper_client.connect().update_parent_element(
        case.project, 'film', realm='realm', clip_id='effect', element=case.selected,
        expected_head=case.head()['revision_id'], params=None if clear_only else {'seed': 9},
        clear_params=['future'] if clear_only else (), idempotency_key='ordinary-param')
    assert result.ok, result.error
    calls = result.receipt.result['commands']
    assert calls[-1]['args']['schema_source'] == case.pin
    assert [call['op'] for call in calls] == ['clip.set-params']


@pytest.mark.parametrize('remove', [False, True])
def test_helper_retains_max10_after_max20_restart_and_explicit_revision_reselection(
        retained_pack, helper_client, monkeypatch, remove):
    from runtime_protocol.service import RuntimeService

    case, client = retained_pack, helper_client
    first = copy.deepcopy(case.pin)
    case.definition['schema']['properties']['seed']['maximum'] = 20
    case.path.write_text(yaml.safe_dump(case.definition))
    admitted = packs.admit_project_pack('realm', case.project)
    newer = {key: admitted['elements'][0]['descriptor'][key] for key in case.selected}
    assert newer['revision'] != case.selected['revision']
    document = case.service.get_document(case.project, first['document_id'])
    second = {'document_id': document['document_id'], 'version': document['version'],
              'content_digest': commit._digest(document['content'])}
    if remove:
        case.service.update_document(case.project, document['document_id'], {
            'expected_version': document['version'], 'content': {**document['content'], 'admissions': []}},
            idempotency_key='remove-before-helper')
    shutil.rmtree(case.source_root)
    shutil.rmtree(case.artifact_root)
    case.service.close()
    case.service = RuntimeService(case.root)
    case.service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    owner = client.connect()
    history = owner.history
    owner.history = lambda project, timeline, **kw: history(
        project, timeline, cursor=kw.get('cursor'), limit=1)

    def forbidden(*a, **kw):
        pytest.fail('ordinary PARAM used local admission or element resolution')

    monkeypatch.setattr(packs, 'resolve_admitted_element', forbidden)
    monkeypatch.setattr(packs, 'admit_project_pack', forbidden)
    monkeypatch.setattr(packs, 'artifact_root', forbidden)
    monkeypatch.setattr(packs, 'authoring_root', forbidden)
    from astrid.sdk.timeline_elements import _read_document

    monkeypatch.setattr('astrid.sdk.timeline_elements._read_document', forbidden)
    head = case.head()
    changes = case.service.store.conn.total_changes
    rejected = owner.update_parent_element(
        case.project, 'film', realm='realm', clip_id='effect', element=case.selected,
        expected_head=head['revision_id'], params={'seed': 16}, idempotency_key='helper-invalid16')
    assert not rejected.ok and '10' in rejected.error.message
    assert case.head() == head and client.calls == []
    assert case.service.store.conn.total_changes == changes
    accepted = owner.update_parent_element(
        case.project, 'film', realm='realm', clip_id='effect', element=case.selected,
        expected_head=head['revision_id'], params={'seed': 8}, idempotency_key='helper-valid8')
    assert accepted.ok, accepted.error
    assert [call['op'] for call in accepted.receipt.result['commands']] == ['clip.set-params']
    assert accepted.receipt.result['commands'][0]['args']['schema_source'] == first
    cleared = owner.update_parent_element(
        case.project, 'film', realm='realm', clip_id='effect', element=case.selected,
        expected_head=case.head()['revision_id'], clear_params=['seed'], idempotency_key='helper-clear')
    assert cleared.ok, cleared.error
    assert [call['op'] for call in cleared.receipt.result['commands']] == ['clip.set-params']
    assert cleared.receipt.result['commands'][0]['args']['schema_source'] == first
    assert 'seed' not in cleared.data['params']
    if not remove:
        # An actual revision change expresses selection intent; save reads the
        # already admitted P2 and never calls the admission producer.
        monkeypatch.setattr('astrid.sdk.timeline_elements._read_document', _read_document)
        reselected = client.connect().update_parent_element(
            case.project, 'film', realm='realm', clip_id='effect', element=newer,
            expected_head=case.head()['revision_id'], params={'seed': 16}, idempotency_key='helper-reselect')
        assert reselected.ok, reselected.error
        assert reselected.data['params']['seed'] == 16
        assert [call['op'] for call in reselected.receipt.result['commands']] == [
            'clip.set-element', 'clip.set-params']
        assert all(call['args']['schema_source'] == second for call in reselected.receipt.result['commands'])


def test_attached_selected_param_p1_survives_lost_ack_restart_and_undo(
        retained_pack, helper_client, monkeypatch, tmp_path):
    """Attached PARAM capture keeps the accepted P1 declaration, not live V2."""
    from runtime_protocol.service import RuntimeService

    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.sync import TimelineSession
    from astrid.sdk.timeline_checkout import Checkout
    from astrid.sdk.workspace_client import WorkspaceClientError

    case, client = retained_pack, helper_client
    first = copy.deepcopy(case.pin)
    owner = client.connect()
    facade = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner)
    outbox = tmp_path / 'support/timeline-outbox/attached-param.json'
    session = TimelineSession.open(case.project, 'film', client=facade, outbox_path=outbox)
    editing = Checkout.attach(session)
    editing.clip(REF).set(seed=8)
    draft = json.loads(outbox.read_bytes())
    assert client.calls == []
    assert draft['pending'][0]['op'] == 'clip.set-params'
    assert draft['pending'][0]['args']['schema_source'] == first

    client.lost_ack[0] = True
    with pytest.raises(WorkspaceClientError):
        editing.publish('keep the selected P1 effect', idempotency_key='attached-param-p1')
    flight = json.loads(outbox.read_bytes())['flight']
    assert json.loads(flight['body'])['commands'][0]['args']['schema_source'] == first
    accepted = case.submit(json.loads(flight['body']), flight['key'])

    case.definition['schema']['properties']['seed']['maximum'] = 20
    case.path.write_text(yaml.safe_dump(case.definition))
    packs.admit_project_pack('realm', case.project)
    document = case.service.get_document(case.project, first['document_id'])
    case.service.update_document(case.project, first['document_id'], {
        'expected_version': document['version'],
        'content': {**document['content'], 'admissions': []}},
        idempotency_key='remove-attached-param-selection')
    shutil.rmtree(case.source_root)
    shutil.rmtree(case.artifact_root)
    case.service.close()
    case.service = RuntimeService(case.root)
    case.service.register_timeline_commit_provider(commit.create_runtime_commit_service())

    restarted_owner = client.connect()
    restarted_facade = SimpleNamespace(
        _remote=SimpleNamespace(_transport=restarted_owner._client), timelines=restarted_owner)
    restarted = Checkout.attach(TimelineSession.open(
        case.project, 'film', client=restarted_facade, outbox_path=outbox))
    with monkeypatch.context() as retry:
        retry.setattr('astrid.sdk.timeline_elements.execute',
                      lambda *a, **kw: pytest.fail('retry recaptured selected PARAM'))
        retried = restarted.publish()
    assert retried['receipt'] == accepted['receipt']
    assert client.calls[0] == client.calls[1]
    assert restarted.clip(REF).params['seed'] == 8

    before_refusal = copy.deepcopy(json.loads(outbox.read_bytes()))
    with pytest.raises(TimelineEditError, match='10|16'):
        restarted.clip(REF).set(seed=16)
    assert json.loads(outbox.read_bytes()) == before_refusal
    undone = restarted.undo([retried['receipt']['receipt_id']])
    assert restarted.clip(REF).params == State(case.bundle()).entity(REF)['params']
    assert undone['receipt']['receipt_id'] != retried['receipt']['receipt_id']


@pytest.mark.parametrize('retained_pack', [False], indirect=True)
@pytest.mark.parametrize('params,clear', [({'seed': 8}, []), ({'seed': 4}, []), ({}, ['seed'])])
def test_helper_param_cannot_bootstrap_unselected_clip(retained_pack, helper_client, params, clear):
    case = retained_pack
    head = case.head()
    before = copy.deepcopy(case.bundle())
    changes = case.service.store.conn.total_changes
    result = helper_client.connect().update_parent_element(
        case.project, 'film', realm='realm', clip_id='effect', element=case.selected,
        expected_head=head['revision_id'], params=params, clear_params=clear)
    assert not result.ok and 'authority is unavailable' in result.error.message
    assert helper_client.calls == [] and case.head() == head and case.bundle() == before
    assert case.service.store.conn.total_changes == changes
