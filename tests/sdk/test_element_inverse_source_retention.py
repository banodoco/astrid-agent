"""Changed-revision inverse consumes the exact historical declaration object."""
from __future__ import annotations

import base64
import copy
import hashlib
import json

import pytest

from astrid.core.element import project_packs as packs
from astrid.sdk.timeline import commit
from astrid.sdk.timeline._command_state import State
from tests.helpers.runtime import use_runtime
from tests.sdk import test_timeline_selected_schema_runtime as selected_schema

REF = selected_schema.REF
configured_project_pack = selected_schema.configured_project_pack
selected_runtime_env = selected_schema.selected_runtime_env
retained_pack = selected_schema.retained_pack
pytestmark = pytest.mark.integration
use_runtime()


def change_revision(case):
    before = copy.deepcopy(case.bundle()['source_mapping']['_runtime_document'])
    case.definition['schema']['properties']['seed']['maximum'] = 20
    case.path.write_text(json.dumps(case.definition))
    admitted = packs.admit_project_pack('realm', case.project)
    selected = {key: admitted['elements'][0]['descriptor'][key]
                for key in ('packId', 'id', 'kind', 'revision')}
    assert selected['revision'] != case.selected['revision']
    document = case.service.get_document(case.project, case.pin['document_id'])
    pin = {'document_id': document['document_id'], 'version': document['version'],
           'content_digest': commit._digest(document['content'])}
    calls = [selected_schema.select_source(case, pin, element=selected),
             {**selected_schema.raw_params(case, {'seed': 16}),
              'args': {'values': {'seed': 16}, 'clear': [], 'schema_source': pin}}]
    accepted = case.submit(case.request(calls), 'change-revision')
    return before, accepted, pin


def test_inverse_loads_original_object_after_readmission_and_restart(
        retained_pack, monkeypatch, record_property):
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    case = retained_pack
    boundary = case.head()
    before, accepted, pin = change_revision(case)
    old_objects = case.options['schema_inputs'][0][1]
    old_object = next(iter(old_objects))
    source = case.service.get_document(case.project, pin['document_id'])
    case.service.update_document(case.project, pin['document_id'], {
        'expected_version': source['version'], 'content': {**source['content'], 'admissions': []}},
        idempotency_key='remove-current-admissions')
    case.service.close()
    case.service = RuntimeService(case.root)
    case.service.register_timeline_commit_provider(commit.create_runtime_commit_service())

    def forbidden(*args, **kwargs):
        raise AssertionError('inverse used ambient element definitions')

    monkeypatch.setattr(packs, 'resolve_admitted_element', forbidden)
    monkeypatch.setattr(packs, 'artifact_root', forbidden)
    monkeypatch.setattr(packs, 'authoring_root', forbidden)
    reads = []
    original = RequestCommitPort.document_object

    def read(port, source_pin, object_id):
        result = original(port, source_pin, object_id)
        reads.append((copy.deepcopy(source_pin), object_id, copy.deepcopy(result)))
        return result

    monkeypatch.setattr(RequestCommitPort, 'document_object', read)
    undone = case.submit(case.request([selected_schema.inverse(accepted['receipt']['receipt_id'])]),
                         'restore-original-declaration')
    restored = undone['data']['canonical_document']
    assert restored['parent_revision']['payload'] == before['parent_revision']['payload']
    assert restored['shot_revisions'] == before['shot_revisions']
    assert restored['internal_timeline_revisions'] == before['internal_timeline_revisions']
    assert restored['dependency_manifest']['documents'] == [case.pin, pin]
    assert [(p, obj) for p, obj, _ in reads if p == case.pin] == [(case.pin, old_object)]
    assert next(value for p, _, value in reads if p == case.pin) == old_objects[old_object]
    redone = case.submit(case.request([selected_schema.inverse(undone['receipt']['receipt_id'])]),
                         'redo-changed-declaration')
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']
    record_property('restoration_boundary', json.dumps({
        'realm_id': 'realm', 'project_id': case.project, 'timeline_id': 'film',
        'head': boundary, 'parent': before['parent_revision']['content_digest'],
        'source': case.pin, 'element': case.selected}, sort_keys=True))
    record_property('snapshot_reads', json.dumps([
        {'pin': p, 'object_id': obj,
         'bytes_sha256': hashlib.sha256(base64.b64decode(value['bytes_base64'])).hexdigest()}
        for p, obj, value in reads], sort_keys=True))


@pytest.mark.parametrize('damage', ['wrong-pin', 'wrong-scope', 'different-object'])
def test_inverse_refuses_forged_historical_declaration_without_writes(
        retained_pack, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    case = retained_pack
    _, accepted, pin = change_revision(case)
    original_source = RequestCommitPort.config_source
    original_object = RequestCommitPort.document_object

    def source(port, source_pin):
        result = original_source(port, source_pin)
        if source_pin == case.pin:
            if damage == 'wrong-pin':
                result['pin'] = pin
            elif damage == 'wrong-scope':
                result['document']['project_id'] = 'foreign-project'
        return result

    def object_source(port, source_pin, object_id):
        if source_pin == case.pin:
            document = original_source(port, pin)['document']
            other = next(row['snapshot_object']['object_id']
                         for row in document['content']['admissions']
                         if row['snapshot_object']['object_id'] != object_id)
            assert other != object_id
            return original_object(port, pin, other)
        return original_object(port, source_pin, object_id)

    monkeypatch.setattr(RequestCommitPort, 'config_source', source)
    if damage == 'different-object':
        monkeypatch.setattr(RequestCommitPort, 'document_object', object_source)
    head, document = case.head(), copy.deepcopy(case.bundle())
    with pytest.raises(RuntimeErrorBase, match='scoped pin|snapshot object'):
        case.submit(case.request([selected_schema.inverse(accepted['receipt']['receipt_id'])]),
                    'refuse-forged-' + damage)
    assert case.head() == head
    assert case.bundle() == document
    assert State(case.bundle()).entity(REF)['params']['seed'] == 16
