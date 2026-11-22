"""Durable admitted-element placement guards."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from unittest.mock import Mock
from pathlib import Path
from types import SimpleNamespace
from types import SimpleNamespace

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


def test_clip_add_refuses_an_occurrence_target(monkeypatch, tmp_path, configured):
    _, _, _, bundle = _setup(monkeypatch, tmp_path, configured)
    command = {
        'schema_version': 1,
        'op': 'clip.add',
        'version': '1',
        'select': 'occurrence:occ-a',
        'args': {'row': {'id': 'caption', 'track': 'titles', 'clipType': 'media',
                         'at': 0, 'hold': 1}},
    }
    with pytest.raises(CommandError, match='parent'):
        execute(bundle, [command])


def test_commit_function_batches_occurrence_placements_and_document_edits(
        monkeypatch, tmp_path, configured):
    """Native placements and free edits share one Timeline.commit request."""
    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.timeline import commit_session, store
    from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document, document_from_bundle

    owner, ref, pin, initial = _setup(monkeypatch, tmp_path, configured)
    document = copy.deepcopy(initial['source_mapping']['_runtime_document'])
    internal = document['internal_timeline_revisions'][0]
    internal['payload']['clips'][0]['id'] = 'effect-a'
    internal['payload']['clips'][0]['params'] = {'seed': 'ƒ(words of n05b)'}
    internal['content_digest'] = _digest(internal['payload'])
    shot = copy.deepcopy(document['shot_revisions'][0])
    shot.update(shot_id='chapter-shot-b', revision_id='shot-2')
    internal_b = copy.deepcopy(internal)
    internal_b.update(timeline_id='chapter-two', revision_id='internal-2')
    internal_b['payload']['clips'][0]['id'] = 'effect-b'
    internal_b['payload']['clips'][0]['params'] = {'seed': 7}
    internal_b['content_digest'] = _digest(internal_b['payload'])
    shot['internal_timeline_revision_id'] = 'internal-2'
    shot['payload']['internal_timeline_revision_id'] = 'internal-2'
    shot['payload']['items'][0]['item_id'] = 'item-2'
    shot['content_digest'] = _digest(shot['payload'])
    document['shot_revisions'].append(shot)
    document['internal_timeline_revisions'].append(internal_b)
    second_occurrence = copy.deepcopy(document['parent_revision']['payload']['occurrences'][0])
    second_occurrence.update(occurrence_id='occ-b', shot_id='chapter-shot-b',
                             shot_revision_id='shot-2')
    document['parent_revision']['payload']['occurrences'].append(second_occurrence)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], document['internal_timeline_revisions'],
        document['dependency_manifest']['media'], document['dependency_manifest']['documents'])
    bundle = bundle_from_document(document)
    baseline = copy.deepcopy(bundle)
    placements = bundle['placements']
    targets = [f"occurrence:{row['occurrence_id']}/clip:{'effect-a' if row['occurrence_id'] == 'occ-a' else 'effect-b'}"
               for row in placements]
    assert len(set(row['shot_id'] for row in placements)) == 2
    assert len({bundle['source_mapping']['shots'][row['shot_id']]['internal_timeline_revision_id']
                for row in placements}) == 2

    client = SimpleNamespace(
        _remote=SimpleNamespace(_transport=owner._client),
        timelines=owner,
    )
    client.documents = configured
    client.media = configured.media
    monkeypatch.setattr(store, 'fetch_bundle', lambda *a, **kw: copy.deepcopy(bundle))
    history_state = {'head': {'revision_id': 'head', 'publication_id': 'publication'},
                     'items': [], 'next_cursor': None}
    owner.history.side_effect = lambda *a, **kw: DomainResult.success(copy.deepcopy(history_state))

    accepted_requests = {}

    def commit(project, timeline_id, body, **kwargs):
        key = kwargs['idempotency_key']
        if key in accepted_requests:
            saved_body, saved_result = accepted_requests[key]
            assert body == saved_body
            return saved_result
        evaluated_head = {'revision_id': body['base_revision_id'],
                          'publication_id': body['base_publication_id']}
        plan = execute(bundle, body['commands'], schema_inputs=owner.schema_inputs,
                       realm_id='realm', commit_request=body, evaluated_head=evaluated_head)
        owner.last_plan = plan
        data = {'status': 'accepted', 'base_revision_id': 'head', 'base_publication_id': 'publication',
                'accepted_revision_id': 'committed', 'commands': copy.deepcopy(body['commands']),
                'canonical_document': document_from_bundle(plan.bundle)}
        result_receipt = CommandReceipt('accepted-receipt', 'timeline.commit', kwargs['idempotency_key'],
                                        'hash', 'project', (1, 1), (), data,
                                        '2026-10-26T00:00:00Z')
        history_state['items'].append({'revision_id': 'committed', 'publication_id': 'accepted-receipt',
                                       'publisher': 'author', 'receipt': result_receipt.as_dict()})
        history_state['head'] = {'revision_id': 'committed', 'publication_id': 'accepted-receipt'}
        result = DomainResult.success(data, receipt=result_receipt, idempotency_key=key)
        accepted_requests[key] = (copy.deepcopy(body), result)
        return result

    owner.commit.side_effect = commit
    def commit_timeline(project, timeline_id, body, *, idempotency_key):
        result = owner.commit(project, timeline_id, body, idempotency_key=idempotency_key)
        assert result.ok, result.as_dict()
        return {'data': result.data, 'receipt': result.receipt.as_dict()}

    owner._client.commit_timeline = commit_timeline
    timeline = commit_session.open('project', 'film', client=client,
                                   outbox_path=tmp_path / 'commit-outbox.json')

    def occurrence_clip(working, occurrence_id):
        return working.clip('effect-a' if occurrence_id == 'occ-a' else 'effect-b')

    with pytest.raises(ValueError, match='re-admit pack'):
        timeline.commit(lambda working: occurrence_clip(
            working, 'occ-a').set_element({**ref, 'revision': 'sha256:' + '0' * 64}),
            key='wrong-revision')
    assert owner.commit.call_count == 0
    assert history_state['head'] == {'revision_id': 'head', 'publication_id': 'publication'}
    assert history_state['items'] == []
    assert bundle == baseline

    def edit(working):
        # Remove a formula before retargeting its clip, then place on both
        # independently owned occurrences and set a parameter on one.
        occurrence_clip(working, 'occ-a').params.pop('seed')
        occurrence_clip(working, 'occ-a').set_element(ref)
        placed = occurrence_clip(working, 'occ-b').set_element(ref)
        placed.params.update({'seed': 9})
        target_shot = working.bundle['shots'][placed.shot_id]['internal_timeline']
        text_row = {'id': 'chapter-title', 'track': 'picture', 'clipType': 'text-card',
                    'at': 0, 'hold': 1, 'params': {'content': 'Chapter title'}}
        target_shot['clips'].append(copy.deepcopy(text_row))

    first = timeline.commit(edit, key='mixed-key')
    assert first['receipt']['receipt_id'] == 'accepted-receipt'
    assert owner.commit.call_count == 1
    body = owner.commit.call_args.args[2]
    commands = body['commands']
    operations = [command['op'] for command in commands]
    assert body['merge'] == 'atomic'
    assert operations.count('clip.set-element') == 2
    assert 'doc.patch' in operations
    assert [command['select'] for command in commands if command['op'] == 'clip.set-element'] == targets
    assert all(command['args']['schema_source'] == pin for command in commands
               if command['op'] == 'clip.set-element')
    accepted = bundle_from_document(first['data']['canonical_document'])
    assert State(accepted).entity(targets[0])['elementRef'] == ref
    assert State(accepted).entity(targets[1])['elementRef'] == ref
    assert 'seed' not in State(accepted).entity(targets[0])['params']
    assert State(accepted).entity(targets[1])['params']['seed'] == 9
    second_shot = accepted['shots'][next(row['shot_id'] for row in accepted['placements']
                                          if row['occurrence_id'] == 'occ-b')]
    assert any(row['id'] == 'chapter-title' and row['params']['content'] == 'Chapter title'
               for row in second_shot['internal_timeline']['clips'])
    assert baseline != accepted

    retried = timeline.commit(lambda _working: pytest.fail('same-key retry must reuse its receipt'),
                              key='mixed-key')
    assert retried['receipt'] == first['receipt']
    assert owner.commit.call_count == 2
    assert len(history_state['items']) == 1


def _scope_test_realm(realm):
    """Match the admitted test pack's realm/project scope to DetachedRealm."""
    from astrid.sdk.timeline.commit import _digest
    from astrid.sdk.timeline._scalar_commands import capture_track_set

    original_context = realm.context

    def context(calls, **kwargs):
        value = original_context(calls, **kwargs)
        value['request']['project_id'] = 'project'
        value['invocation']['body']['project_id'] = 'project'
        value['realm_id'] = 'realm'
        value['request_hash'] = _digest(value['invocation'])
        return value

    realm.context = context


def test_provider_stale_placement_and_free_patch_rebases_disjoint_but_conflicts_same_key(
        monkeypatch, tmp_path, configured):
    """A staged admitted placement stays atomic with a free patch through stale replay."""
    from astrid.sdk.timeline import commit_session, store
    from astrid.sdk.timeline.commit import _digest
    from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm, detached

    def public_staged_commands(owner, element_ref, starting_bundle, fps, outbox, *, place=True):
        client = SimpleNamespace(
            _remote=SimpleNamespace(_transport=owner._client), timelines=owner,
            documents=configured, media=configured.media)
        monkeypatch.setattr(store, 'fetch_bundle',
                            lambda *a, **kw: copy.deepcopy(starting_bundle))
        history_state = {'head': {'revision_id': 'head', 'publication_id': 'publication'},
                         'items': [], 'next_cursor': None}
        owner.history.side_effect = lambda *a, **kw: DomainResult.success(copy.deepcopy(history_state))
        timeline = commit_session.open('project', 'film', client=client, outbox_path=outbox)
        if place:
            timeline.clip('placement-proof-clip').set_element(element_ref)
        if fps is not None:
            timeline.bundle['parent']['config']['output']['fps'] = {
                'numerator': fps, 'denominator': 1}
        else:
            other = next(row for row in timeline.bundle['placements']
                         if row['occurrence_id'] != 'occ-a')
            shot = timeline.bundle['shots'][other['shot_id']]
            shot['internal_timeline']['clips'][0]['at'] += 0.5
        timeline._capture_free_edits()
        commands = copy.deepcopy(timeline._commands)
        assert [command['op'] for command in commands] == (
            ['clip.set-element', 'doc.patch'] if place else ['doc.patch'])
        return commands

    unique_target = 'occurrence:occ-a/clip:placement-proof-clip'
    owner, ref, pin, initial = _setup(monkeypatch, tmp_path, configured)
    document = copy.deepcopy(initial['source_mapping']['_runtime_document'])
    internal = document['internal_timeline_revisions'][0]
    internal['payload']['clips'][0]['id'] = 'placement-proof-clip'
    internal['content_digest'] = _digest(internal['payload'])
    document['dependency_manifest']['internal_timelines'][0]['content_digest'] = internal['content_digest']
    shot2 = copy.deepcopy(document['shot_revisions'][0])
    shot2.update(shot_id='chapter-shot-b', revision_id='shot-2')
    internal2 = copy.deepcopy(internal)
    internal2.update(timeline_id='chapter-two', revision_id='internal-2')
    internal2['payload']['clips'][0]['id'] = 'chapter-two-clip'
    internal2['content_digest'] = _digest(internal2['payload'])
    shot2['internal_timeline_revision_id'] = 'internal-2'
    shot2['payload']['internal_timeline_revision_id'] = 'internal-2'
    shot2['payload']['items'][0]['item_id'] = 'item-2'
    shot2['content_digest'] = _digest(shot2['payload'])
    document['shot_revisions'].append(shot2)
    document['internal_timeline_revisions'].append(internal2)
    second_occurrence = copy.deepcopy(document['parent_revision']['payload']['occurrences'][0])
    second_occurrence.update(occurrence_id='occ-b', shot_id='chapter-shot-b',
                             shot_revision_id='shot-2')
    document['parent_revision']['payload']['occurrences'].append(second_occurrence)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    from astrid.sdk.timeline.commit import _manifest
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], document['internal_timeline_revisions'],
        document['dependency_manifest']['media'], document['dependency_manifest']['documents'])
    bundle = bundle_from_document(document)
    owner.open_bundle.return_value = DomainResult.success({
        'bundle': bundle, 'project_id': 'project', 'timeline_id': 'film',
        'head_revision_id': 'head', 'revision_id': 'head',
    })
    realm = DetachedRealm(document)
    source, snapshots = owner.schema_inputs[0]
    realm.live_sources[_digest(pin)] = detached(source)
    realm.snapshot_objects = detached(snapshots)
    _scope_test_realm(realm)
    base = copy.deepcopy(realm.head)
    stale = public_staged_commands(owner, ref, bundle, 24,
                                   tmp_path / 'public-stage-a.json')
    concurrent = public_staged_commands(
        owner, ref, bundle, None, tmp_path / 'public-stage-concurrent.json', place=False)
    realm.call(concurrent)
    rebased = realm.call(stale, base=base)
    final = bundle_from_document(rebased['data']['canonical_document'])
    assert rebased['data']['status'] == 'rebased'
    assert State(final).entity(unique_target)['elementRef'] == ref
    assert final['parent']['config']['output']['fps'] == {'numerator': 24, 'denominator': 1}
    other_shot_id = next(row['shot_id'] for row in final['placements']
                        if row['occurrence_id'] == 'occ-b')
    internal = final['shots'][other_shot_id]['internal_timeline']
    assert internal['clips'][0]['at'] == 1.5
    assert State(final).entity('parent/clip:clip-b')['at'] == 8.008

    # Repeat from a clean base with the exact free-patch key changed by the
    # intervening publisher. The staged element write must not partially land.
    owner2, ref2, pin2, initial2 = _setup(monkeypatch, tmp_path / 'conflict', configured)
    document2 = copy.deepcopy(initial2['source_mapping']['_runtime_document'])
    internal2 = document2['internal_timeline_revisions'][0]
    internal2['payload']['clips'][0]['id'] = 'placement-proof-clip'
    internal2['content_digest'] = _digest(internal2['payload'])
    document2['dependency_manifest']['internal_timelines'][0]['content_digest'] = internal2['content_digest']
    shot_b = copy.deepcopy(document2['shot_revisions'][0])
    shot_b.update(shot_id='chapter-shot-b', revision_id='shot-2')
    internal_b = copy.deepcopy(internal2)
    internal_b.update(timeline_id='chapter-two', revision_id='internal-2')
    internal_b['payload']['clips'][0]['id'] = 'chapter-two-clip'
    internal_b['content_digest'] = _digest(internal_b['payload'])
    shot_b['internal_timeline_revision_id'] = 'internal-2'
    shot_b['payload']['internal_timeline_revision_id'] = 'internal-2'
    shot_b['payload']['items'][0]['item_id'] = 'item-2'
    shot_b['content_digest'] = _digest(shot_b['payload'])
    document2['shot_revisions'].append(shot_b)
    document2['internal_timeline_revisions'].append(internal_b)
    occurrence_b = copy.deepcopy(document2['parent_revision']['payload']['occurrences'][0])
    occurrence_b.update(occurrence_id='occ-b', shot_id='chapter-shot-b', shot_revision_id='shot-2')
    document2['parent_revision']['payload']['occurrences'].append(occurrence_b)
    document2['parent_revision']['content_digest'] = _digest(document2['parent_revision']['payload'])
    document2['dependency_manifest'] = _manifest(
        document2['shot_revisions'], document2['internal_timeline_revisions'],
        document2['dependency_manifest']['media'], document2['dependency_manifest']['documents'])
    bundle2 = bundle_from_document(document2)
    owner2.open_bundle.return_value = DomainResult.success({
        'bundle': bundle2, 'project_id': 'project', 'timeline_id': 'film',
        'head_revision_id': 'head', 'revision_id': 'head',
    })
    realm2 = DetachedRealm(document2)
    source2, snapshots2 = owner2.schema_inputs[0]
    realm2.live_sources[_digest(pin2)] = detached(source2)
    realm2.snapshot_objects = detached(snapshots2)
    _scope_test_realm(realm2)
    base2 = copy.deepcopy(realm2.head)
    stale2 = public_staged_commands(owner2, ref2, bundle2, 25,
                                    tmp_path / 'public-stage-b.json')
    competing_patch = copy.deepcopy(stale2[1])
    competing_patch['args']['changes'][0]['value'] = {'numerator': 60, 'denominator': 1}
    realm2.call([competing_patch])
    before = (copy.deepcopy(realm2.head), copy.deepcopy(realm2.entries),
              copy.deepcopy(realm2.documents))
    accepted_count = len(realm2.accepted)
    with pytest.raises(CommitRejected) as error:
        realm2.call(stale2, base=base2)
    assert error.value.failure['details']['status'] == 'conflict'
    assert any(conflict['ref'] == 'parent'
               and conflict['path'] == ['config', 'output', 'fps']
               for conflict in error.value.failure['details']['conflicts'])
    assert (realm2.head, realm2.entries, realm2.documents) == before
    assert len(realm2.accepted) == accepted_count
    landed = bundle_from_document(realm2.documents[realm2.head['revision_id']])
    assert State(landed).entity(unique_target)['elementRef'] is None
    assert landed['parent']['config']['output']['fps'] == {'numerator': 60, 'denominator': 1}


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
    assert result.receipt.as_dict() == receipt
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

@pytest.mark.parametrize('key', ['place-retry', None])
@pytest.mark.parametrize('race', ['suffix', 'settled', 'new-flight'])
def test_lost_ack_captured_flight_reconciles_without_flushing_suffix(
        monkeypatch, tmp_path, configured, key, race):
    from astrid.sdk.timeline.sync import TimelineSession

    owner, ref, _, _ = _setup(monkeypatch, tmp_path, configured)
    accepted = _lose_first_ack(owner)
    assert not _place(owner, ref, key=key).ok
    original_body = copy.deepcopy(owner.commit.call_args.args[2])
    original_key = owner.commit.call_args.kwargs['idempotency_key']
    original_flush = TimelineSession._flush
    suffix = {'op': 'unrelated', 'args': {'value': 1}}

    def racing_flush(session, **kwargs):
        if kwargs.get('expected_flight') is not None:
            if race != 'suffix':
                original_flush(session)
                with session.outbox.locked():
                    state = session._state()
                    state['pending'].append(copy.deepcopy(suffix))
                    if race == 'new-flight':
                        state['flight'] = {'key': 'different-flight'}
                    session.outbox.write(state)
            else:
                with session.outbox.locked():
                    state = session._state()
                    state['pending'].append(copy.deepcopy(suffix))
                    session.outbox.write(state)
        return original_flush(session, **kwargs)

    monkeypatch.setattr(TimelineSession, '_flush', racing_flush)
    monkeypatch.setattr('astrid.sdk.timeline_elements.execute',
                        lambda *args, **kwargs: pytest.fail('retry recaptured'))
    result = _place(owner, ref, key=key)
    assert result.ok, result.error
    assert result.idempotency_key == original_key
    assert result.receipt.receipt_id == accepted[0].receipt.receipt_id
    assert owner.commit.call_count == 2
    assert owner.commit.call_args.args[2] == original_body
    assert owner.commit.call_args.kwargs['idempotency_key'] == original_key
    session = TimelineSession.open('project', 'film', client=SimpleNamespace(
        _remote=SimpleNamespace(_transport=owner._client), timelines=owner))
    with session.outbox.locked():
        state = session._state()
    assert state['pending'] == [suffix]
    assert state['flight'] == ({'key': 'different-flight'} if race == 'new-flight' else None)
