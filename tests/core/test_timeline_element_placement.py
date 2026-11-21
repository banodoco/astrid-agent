"""Durable admitted-element placement guards."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from unittest.mock import Mock
from pathlib import Path

import pytest

from astrid.core.element import project_packs as packs
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline_elements import place_element
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.core import test_project_packs
from tests.sdk.test_timeline_commit_provider_document import document_fixture

configured = test_project_packs.configured
TARGET = 'occurrence:occ-a/clip:clip-a'


def _setup(monkeypatch, tmp_path, configured):
    from astrid.core.element.registry import load_default_registry
    fixture_pack = Path(__file__).parents[1] / 'fixtures/packs/astrid_motion'
    registry = load_default_registry(extra_pack_roots=(str(fixture_pack),))
    monkeypatch.setattr('astrid.core.element.registry.load_default_registry', lambda: registry)
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


@pytest.mark.parametrize('target', ['clip:clip-a', 'parent/track:clip-a', 'occurrence:bad',
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
        'args': {'elementRef': {**ref, 'kind': kind}, 'placement': 'place'},
    }
    with pytest.raises(CommandError, match='effect elements on built-in effect clips only'):
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
    assert command['args']['placement'] == 'place'
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


# --- pointing an existing built-in effect clip at an admitted project effect ---------------------------------

def _builtin_clip(bundle, clip_type, **fields):
    clip = State(bundle).entity(TARGET)
    clip.update(clipType=clip_type, params={'seed': 7, 'author_note': 'keep me'}, **fields)
    return clip


@pytest.mark.parametrize('clip_type', ['am-discord', 'am-footage'])
def test_builtin_effect_clip_is_pointed_at_the_admitted_effect(monkeypatch, tmp_path, configured, clip_type):
    from astrid.sdk.timeline.validation import validate_final_candidate

    owner, ref, pin, bundle = _setup(monkeypatch, tmp_path, configured)
    before = copy.deepcopy(_builtin_clip(bundle, clip_type))  # the fixture clip carries an asset
    assert before['asset'] == 'picture'
    baseline = copy.deepcopy(bundle)
    result = _place(owner, ref)
    assert result.ok, result.error
    after = State(owner.last_plan.bundle).entity(TARGET)
    assert (after['clipType'], after['elementRef']) == (ref['id'], ref)
    assert {k: v for k, v in after.items() if k not in ('clipType', 'elementRef')} == {
        k: v for k, v in before.items() if k != 'clipType'}
    validate_final_candidate(owner.last_plan.bundle, baseline=baseline,
                             schema_inputs=owner.schema_inputs, realm_id='realm')
    from tests.editing_conformance.test_recipe_m1_commands import inverse
    restored = inverse(owner.last_plan.bundle, owner.last_plan.records).bundle
    assert State(restored).entity(TARGET) == before


def test_retarget_refuses_params_the_new_element_cannot_hold(monkeypatch, tmp_path, configured):
    owner, ref, _, bundle = _setup(monkeypatch, tmp_path, configured)
    _builtin_clip(bundle, 'am-discord')['params']['seed'] = 'not a number'
    result = _place(owner, ref)
    assert not result.ok
    owner.commit.assert_not_called()


@pytest.mark.parametrize('clip_type', ['media', 'unknown-effect'])
def test_retarget_refuses_a_clip_that_is_not_a_builtin_effect(monkeypatch, tmp_path, configured, clip_type):
    owner, ref, _, bundle = _setup(monkeypatch, tmp_path, configured)
    _builtin_clip(bundle, clip_type)
    result = _place(owner, ref)
    assert not result.ok and 'built-in effect clip' in result.error.message
    owner.commit.assert_not_called()


def test_retarget_refuses_an_unadmitted_ref_and_a_stale_head(monkeypatch, tmp_path, configured):
    owner, ref, _, bundle = _setup(monkeypatch, tmp_path, configured)
    _builtin_clip(bundle, 'am-footage')
    assert not _place(owner, {**ref, 'id': 'not-admitted'}).ok
    assert _place(owner, ref, expected_head='stale').error.code == 'conflict'
    owner.commit.assert_not_called()
    assert State(bundle).entity(TARGET)['clipType'] == 'am-footage'



def test_builtin_effect_parent_clip_is_pointed_at_the_admitted_effect(monkeypatch, tmp_path, configured):
    from astrid.sdk.timeline.validation import validate_final_candidate

    parent_target = 'parent/clip:clip-a'
    owner, ref, _, bundle = _setup(monkeypatch, tmp_path, configured)
    edited = {'clipType': 'am-footage', 'params': {'seed': 7, 'author_note': 'keep me'}}
    State(bundle).entity(parent_target).update(**edited)
    next(c for c in bundle['parent']['config']['clips'] if c['id'] == 'clip-a').update(**edited)  # config mirrors clips
    before = copy.deepcopy(State(bundle).entity(parent_target))
    baseline = copy.deepcopy(bundle)
    assert _place(owner, ref, target=parent_target, expected_head='stale').error.code == 'conflict'
    owner.commit.assert_not_called()
    result = _place(owner, ref, target=parent_target)
    assert result.ok, result.error
    assert owner.commit.call_args.args[2]['commands'][0]['args']['placement'] == 'place'
    after = State(owner.last_plan.bundle).entity(parent_target)
    assert (after['clipType'], after['elementRef']) == (ref['id'], ref)
    assert after['params'] == {'seed': 7, 'author_note': 'keep me'}
    assert {k: v for k, v in after.items() if k not in ('clipType', 'elementRef')} == {
        k: v for k, v in before.items() if k != 'clipType'}
    validate_final_candidate(owner.last_plan.bundle, baseline=baseline,
                             schema_inputs=owner.schema_inputs, realm_id='realm')


def _lose_first_ack(owner):
    """The Runtime accepts the first commit, but its acknowledgement never reaches the client."""
    accept = owner.commit.side_effect
    accepted = []

    def commit(project, timeline, body, **kwargs):
        if not accepted:
            accepted.append(accept(project, timeline, body, **kwargs))
            raise WorkspaceClientError(503, 'unavailable', 'acknowledgement lost')
        return accepted[0]  # replaying the same key returns the original receipt

    owner.commit.side_effect = commit
    return accepted


def test_lost_ack_flight_replays_the_same_request(monkeypatch, tmp_path, configured):
    owner, ref, _, _ = _setup(monkeypatch, tmp_path, configured)
    accepted = _lose_first_ack(owner)
    assert not _place(owner, ref).ok and len(accepted) == 1

    retried = _place(owner, ref)
    assert retried.ok, retried.error
    assert retried.data['element'] == ref
    assert owner.commit.call_count == 2
    assert owner.commit.call_args.kwargs['idempotency_key'] == 'place-key'


def test_lost_ack_flight_settles_before_a_fresh_edit_on_the_runtime_head(monkeypatch, tmp_path, configured):
    owner, ref, _, _ = _setup(monkeypatch, tmp_path, configured)
    accepted = _lose_first_ack(owner)
    assert not _place(owner, ref).ok and len(accepted) == 1

    # The caller reads the Runtime head ('committed') after the lost ack; the outbox base is still 'head'.
    fresh = _place(owner, ref, expected_head='committed', key='next-place')
    assert fresh.ok, fresh.error
    assert fresh.data['element'] == ref
    assert owner.commit.call_count == 2 + 1
    assert owner.commit.call_args.kwargs['idempotency_key'] == 'next-place'

    stale = _place(owner, ref, expected_head='stale', key='stale-place')
    assert not stale.ok and stale.error.code == 'conflict'
    assert stale.error.details == {'expected_head': 'stale', 'actual_head': 'committed',
                                   'outbox_base': 'committed'}
    assert owner.commit.call_count == 3

def test_stale_expected_head_reports_runtime_and_outbox_heads(monkeypatch, tmp_path, configured):
    owner, ref, _, _ = _setup(monkeypatch, tmp_path, configured)
    result = _place(owner, ref, expected_head='stale')
    assert not result.ok and result.error.code == 'conflict'
    assert result.error.details == {'expected_head': 'stale', 'actual_head': 'head', 'outbox_base': 'head'}
    owner.commit.assert_not_called()


def test_unmarked_first_placement_with_valid_pin_is_refused(monkeypatch, tmp_path, configured):
    owner, ref, pin, bundle = _setup(monkeypatch, tmp_path, configured)
    before = copy.deepcopy(bundle)
    command = {'schema_version': 1, 'op': 'clip.set-element', 'version': '1',
               'select': TARGET, 'args': {'elementRef': ref, 'schema_source': pin}}
    with pytest.raises(CommandError, match='requires an existing element ref'):
        execute(bundle, [command], schema_inputs=owner.schema_inputs, realm_id='realm')
    assert bundle == before
    owner.commit.assert_not_called()


def test_authenticated_old_placement_receipt_and_inverse(monkeypatch, tmp_path, configured):
    from astrid.sdk.timeline import _clip_scalar_commands as scalar
    from tests.editing_conformance.test_recipe_m1_commands import inverse

    owner, ref, pin, bundle = _setup(monkeypatch, tmp_path, configured)
    old_command = {'schema_version': 1, 'op': 'clip.set-element', 'version': '1',
                   'select': TARGET, 'args': {'elementRef': ref, 'schema_source': pin}}
    # Reproduce the former native handler to produce an actual old captured command
    # and its inverse, then restore the new handler before acknowledging history.
    with monkeypatch.context() as old:
        old.setattr(scalar, 'clip_set_element', lambda ctx, cmd: scalar._clip_element(ctx, cmd, allow_initial=True))
        plan = execute(bundle, [old_command], schema_inputs=owner.schema_inputs, realm_id='realm')
    assert State(inverse(plan.bundle, plan.records).bundle).entity(TARGET) == State(bundle).entity(TARGET)
    data = {'base_revision_id': 'head', 'base_publication_id': 'publication',
            'accepted_revision_id': 'committed', 'commands': copy.deepcopy(plan.commands),
            'canonical_document': document_from_bundle(plan.bundle)}
    receipt = CommandReceipt('old-receipt', 'timeline.commit', 'place-key', 'hash', 'project',
                             (1, 1), (), data, '2026-10-26T00:00:00Z').as_dict()
    history = {'head': {'revision_id': 'committed', 'publication_id': 'old-receipt'},
               'items': [{'publisher': 'author', 'receipt': receipt}], 'next_cursor': None}
    owner.history.side_effect = lambda *args, **kwargs: DomainResult.success(copy.deepcopy(history))
    result = _place(owner, ref)
    assert result.ok, result.error
    assert result.receipt == receipt
    assert 'placement' not in receipt['result']['commands'][0]['args']
    owner.commit.assert_not_called()


def test_builtin_effect_identity_fails_closed_for_non_effect_and_registry_failure(monkeypatch):
    from types import SimpleNamespace
    from astrid.sdk.timeline._clip_scalar_commands import _builtin_effect

    registry = Mock()
    registry.list.return_value = [SimpleNamespace(id='known-animation', kind='animations')]
    monkeypatch.setattr('astrid.core.element.registry.load_default_registry', lambda: registry)
    assert not _builtin_effect('known-animation')
    assert not _builtin_effect('unknown-effect')
    registry.list.side_effect = RuntimeError('registry unavailable')
    assert not _builtin_effect('known-animation')
