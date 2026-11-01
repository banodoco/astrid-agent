"""Durable admitted-element placement guards."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from unittest.mock import Mock

import pytest

from astrid.core.element import project_packs as packs
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline_elements import place_element
from tests.core import test_project_packs
from tests.sdk.test_timeline_commit_provider_document import document_fixture

configured = test_project_packs.configured
TARGET = 'occurrence:occ-a/clip:clip-a'


def _setup(monkeypatch, tmp_path, configured):
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
    inputs = [({'pin': pin, 'document': {**row, 'project_id': 'project'}}, objects)]
    monkeypatch.setattr('astrid.sdk.timeline_elements.Documents', lambda client: configured)
    monkeypatch.setattr('astrid.sdk.timeline_elements.RemoteMedia', lambda client: configured.media)
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'outbox'))
    monkeypatch.setattr('astrid.sdk.timeline.sync._ack',
                        lambda result, *args: (result['data'], result['receipt']))

    document = document_fixture(shared=False)
    document['project_id'] = 'project'
    document['timeline_id'] = 'film'
    clip = document['internal_timeline_revisions'][0]['payload']['clips'][0]
    clip.pop('elementRef', None)
    clip['clipType'] = 'am-discord'
    digest = 'sha256:' + hashlib.sha256(
        json.dumps(document['internal_timeline_revisions'][0]['payload'],
                   sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    document['internal_timeline_revisions'][0]['content_digest'] = digest
    document['dependency_manifest']['internal_timelines'][0]['content_digest'] = digest
    bundle = bundle_from_document(document)
    owner = Mock()
    owner._client._last_handshake = {'realm_id': 'realm', 'actor_id': 'author'}
    owner.open_composition.return_value = DomainResult.success({
        'native_inspection': {'project_id': 'project', 'timeline_id': 'film'},
        'summary': {'head_revision_id': 'head'},
    })
    owner.open_bundle.return_value = DomainResult.success({
        'bundle': bundle, 'project_id': 'project', 'timeline_id': 'film',
        'head_revision_id': 'head', 'revision_id': 'head',
    })
    history = {'head': {'revision_id': 'head', 'publication_id': 'publication'},
               'items': [], 'next_cursor': None}
    owner.history.side_effect = lambda *args, **kwargs: DomainResult.success(copy.deepcopy(history))

    def commit(project, timeline, body, **kwargs):
        plan = execute(bundle, body['commands'], schema_inputs=inputs, realm_id='realm')
        owner.last_plan = plan
        data = {
            'base_revision_id': 'head', 'base_publication_id': 'publication',
            'accepted_revision_id': 'committed', 'commands': copy.deepcopy(body['commands']),
            'canonical_document': document_from_bundle(plan.bundle),
        }
        receipt = CommandReceipt('accepted-receipt', 'timeline.commit', kwargs['idempotency_key'],
                                 'hash', project, (1, 1), (), data, '2026-10-26T00:00:00Z')
        history['items'].append({
            'revision_id': 'committed', 'publication_id': 'accepted-receipt',
            'publisher': 'author', 'receipt': receipt.as_dict(),
        })
        history['head'] = {'revision_id': 'committed', 'publication_id': 'accepted-receipt'}
        return DomainResult.success(data, receipt=receipt, idempotency_key=kwargs['idempotency_key'])

    owner.schema_inputs = inputs
    owner.commit.side_effect = commit
    return owner, ref, pin, bundle


def _place(owner, ref, *, target=TARGET, expected_head='head', key='place-key'):
    return place_element(owner, 'project', 'film', realm='realm', target=target,
                         element=ref, expected_head=expected_head, idempotency_key=key)


@pytest.mark.parametrize('target', ['parent/clip:clip-a', 'occurrence:bad',
                                    'occurrence:occ-a/clip:', 'occurrence:occ-a::x/clip:clip-a'])
def test_malformed_occurrence_target_refuses_before_commit(monkeypatch, tmp_path, configured, target):
    owner, ref, _, _ = _setup(monkeypatch, tmp_path, configured)
    result = _place(owner, ref, target=target)
    assert not result.ok and result.error.code == 'validation_error'
    owner.commit.assert_not_called()


def test_missing_admission_refuses_before_commit(monkeypatch, tmp_path, configured):
    owner, ref, _, _ = _setup(monkeypatch, tmp_path, configured)
    configured.rows[('project', next(iter(configured.rows))[1])]['content']['admissions'] = []
    result = _place(owner, ref)
    assert not result.ok and result.error.code == 'validation_error'
    owner.commit.assert_not_called()


def test_wrong_admitted_revision_refuses_before_commit(monkeypatch, tmp_path, configured):
    owner, ref, _, _ = _setup(monkeypatch, tmp_path, configured)
    result = _place(owner, {**ref, 'revision': 'sha256:' + '0' * 64})
    assert not result.ok and result.error.code == 'validation_error'
    owner.commit.assert_not_called()


@pytest.mark.parametrize('kind', ['animation', 'transition'])
def test_sdk_first_placement_rejects_unsupported_element_kinds(monkeypatch, tmp_path, configured, kind):
    owner, ref, _, _ = _setup(monkeypatch, tmp_path, configured)
    result = _place(owner, {**ref, 'kind': kind})
    assert not result.ok and result.error.code == 'validation_error'
    assert 'effect elements only' in result.error.message
    owner.commit.assert_not_called()


@pytest.mark.parametrize('kind', ['animation', 'transition'])
def test_native_first_placement_rejects_unsupported_element_kinds(monkeypatch, tmp_path, configured, kind):
    owner, ref, _, bundle = _setup(monkeypatch, tmp_path, configured)
    command = {
        'schema_version': 1,
        'op': 'clip.set-element',
        'version': '1',
        'select': TARGET,
        'args': {'elementRef': {**ref, 'kind': kind}},
    }
    with pytest.raises(CommandError, match='effect elements on am-discord clips only'):
        execute(bundle, [command], schema_inputs=owner.schema_inputs, realm_id='realm')


def test_stale_expected_head_refuses_before_commit(monkeypatch, tmp_path, configured):
    owner, ref, _, _ = _setup(monkeypatch, tmp_path, configured)
    result = _place(owner, ref, expected_head='stale')
    assert not result.ok and result.error.code == 'conflict'
    owner.commit.assert_not_called()


def test_placement_writes_exact_admission_pin_and_retries_by_identity(monkeypatch, tmp_path, configured):
    owner, ref, pin, bundle = _setup(monkeypatch, tmp_path, configured)
    first = _place(owner, ref)
    assert first.ok, first.error
    assert owner.commit.call_count == 1
    body = owner.commit.call_args.args[2]
    command = body['commands'][0]
    assert command['select'] == TARGET
    assert command['args']['elementRef'] == ref
    assert command['args']['schema_source'] == pin
    assert first.data['element'] == ref
    assert first.data['before'] == {'element': None}
    assert execute(bundle, body['commands'], schema_inputs=owner.schema_inputs,
                   realm_id='realm').bundle
    from tests.editing_conformance.test_recipe_m1_commands import inverse
    restored = inverse(owner.last_plan.bundle, owner.last_plan.records).bundle
    assert State(restored).entity(TARGET) == State(bundle).entity(TARGET)

    retried = _place(owner, ref)
    assert retried == first
    assert owner.commit.call_count == 1
