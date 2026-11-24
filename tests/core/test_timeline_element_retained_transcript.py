"""Public element placement captures the transcript retained by its base publication."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult
from astrid.sdk.media_transcript import transcript_document_id
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.timeline_elements import place_element
from tests.core import test_timeline_element_placement as placement_fixture
from tests.timeline.test_occurrence_media_swap import _retained_anchor_swap

configured = placement_fixture.configured


@pytest.mark.parametrize('failure', [None, 'mismatch', 'revoked'])
def test_public_element_placement_authenticates_retained_parent_anchor(monkeypatch, tmp_path, configured, failure):
    owner, element, pack_pin, _ = placement_fixture._setup(monkeypatch, tmp_path, configured)
    original, transcript = _retained_anchor_swap()
    content = transcript['document']['content']
    doc_id = transcript_document_id('project', content['media_id'], content['source_id'])
    transcript['pin']['document_id'] = doc_id
    transcript['document'].update(document_id=doc_id, project_id='project')
    pin = transcript['pin']

    document = original['source_mapping']['_runtime_document']
    document['project_id'] = 'project'
    parent = document['parent_revision']
    for clips in (parent['payload']['clips'], parent['payload']['config']['clips']):
        clips[0]['app']['astrid']['word_anchors']['on']['document'] = copy.deepcopy(pin)
    parent['content_digest'] = _digest(parent['payload'])
    internal = document['internal_timeline_revisions'][0]
    visual = next(row for row in internal['payload']['clips'] if row['id'] == 'visual')
    visual['clipType'] = 'am-discord'
    internal['content_digest'] = _digest(internal['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'],
        document['internal_timeline_revisions'], document['dependency_manifest']['media'], [pin])
    bundle = bundle_from_document(document)
    original_bundle = copy.deepcopy(bundle)
    target = 'occurrence:occ-a/clip:visual'
    basis = {'revision_id': parent['revision_id'], 'publication_id': 'publication'}
    history = {'head': copy.deepcopy(basis), 'items': [], 'next_cursor': None}
    owner.history.side_effect = lambda *args, **kwargs: DomainResult.success(copy.deepcopy(history))
    owner.open_bundle.return_value = DomainResult.success({'bundle': bundle})

    def read_transcript(project, exact_pin):
        assert (project, exact_pin) == ('project', pin)
        if failure == 'revoked':
            error = RuntimeError('retained transcript revoked')
            error.code = 'not_found'
            raise error
        verified = copy.deepcopy(transcript)
        if failure == 'mismatch':
            verified['document']['version'] += 1
        return verified

    owner._client.get_project_verified_document.side_effect = read_transcript
    monkeypatch.setattr('astrid.sdk.media_transcript.read_transcript',
                        lambda *args, **kwargs: pytest.fail('latest transcript lookup'))

    def commit(project, timeline, body, *, idempotency_key):
        assert (project, timeline) == ('project', 'film')
        assert body['base_revision_id'] == basis['revision_id']
        assert body['base_publication_id'] == basis['publication_id']
        assert len(body['commands']) == 1
        command = body['commands'][0]
        assert command['args']['schema_source'] == pack_pin
        assert command['select'] == target
        assert 'pinned_documents' not in str(body) and 'segments' not in str(body)
        provider_input = copy.deepcopy(bundle)
        provider_input['source_mapping']['pinned_documents'] = [transcript]
        plan = execute(provider_input, body['commands'], schema_inputs=owner.schema_inputs, realm_id='realm')
        assert plan.bundle['parent']['clips'] == bundle['parent']['clips']
        assert State(plan.bundle).entity(target)['elementRef'] == element
        data = {'base_revision_id': basis['revision_id'], 'base_publication_id': basis['publication_id'],
                'accepted_revision_id': 'committed', 'commands': plan.commands,
                'canonical_document': document_from_bundle(plan.bundle)}
        receipt = CommandReceipt('accepted-receipt', 'timeline.commit', idempotency_key,
                                 'hash', project, (1, 1), (), data, '2026-10-26T00:00:00Z')
        history['head'] = {'revision_id': 'committed', 'publication_id': 'accepted-receipt'}
        return DomainResult.success(data, receipt=receipt, idempotency_key=idempotency_key)

    owner.commit.side_effect = commit
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner)
    path = tmp_path / 'element-outbox.json'
    session = TimelineSession.open('project', 'film', client=client, outbox_path=path)
    before = path.read_bytes()
    result = place_element(owner, 'project', 'film', realm='realm', target=target, element=element,
                           expected_head=basis['revision_id'], idempotency_key='place-retained', session=session)

    if failure is None:
        assert result.ok, result.error
        assert result.data['element'] == element
        assert history['head']['revision_id'] == result.data['new_head'] == 'committed'
        owner.commit.assert_called_once()
        assert b'pinned_documents' not in path.read_bytes()
        assert b'"segments"' not in path.read_bytes()
    else:
        assert not result.ok and result.error.code == 'validation_error'
        expected = {'mismatch': 'scope or version does not match its pin',
                    'revoked': 'exact transcript pin is missing or unavailable'}
        assert expected[failure] in result.error.message
        assert path.read_bytes() == before
        assert history['head'] == basis
        owner.commit.assert_not_called()
    owner._client.get_project_verified_document.assert_called_once_with('project', pin)
    assert bundle == original_bundle
    assert any(call.kwargs.get('publication_id') == basis['publication_id']
               for call in owner.open_bundle.call_args_list)
