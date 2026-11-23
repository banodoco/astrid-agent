"""Durable admitted-element placement guards."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from unittest.mock import Mock
from pathlib import Path
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


def test_retain_clip_rows_id_comparisons_grow_linearly_and_preserve_duplicates():
    from astrid.sdk.timeline.commit_session import _retain_clip_rows

    comparisons = 0

    class CountedId(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            nonlocal comparisons
            comparisons += 1
            return str.__eq__(self, other)

    def measured(size):
        nonlocal comparisons
        before = {'parent': {'clips': [{'id': CountedId(f'clip-{i}'), 'params': {'old': i}}
                                       for i in range(size)]}}
        after = {'parent': {'clips': [{'id': CountedId(f'clip-{i}'), 'params': {'new': i}}
                                      for i in range(size)]}}
        comparisons = 0
        _retain_clip_rows(before, after)
        return comparisons

    small, large = measured(64), measured(128)
    assert large <= small * 2.5

    old_unique = {'id': CountedId('unique'), 'params': {'held': []}}
    old_duplicate = {'id': CountedId('duplicate'), 'params': {'held': []}}
    before = {'parent': {'clips': [old_unique, old_duplicate,
                                    {'id': CountedId('duplicate'), 'params': {'held': []}}]}}
    unique_new = {'id': CountedId('unique'), 'params': {'held': [], 'written': True}}
    duplicate_new = {'id': CountedId('duplicate'), 'params': {'written': True}}
    duplicate_new_again = {'id': CountedId('duplicate'), 'params': {'written': True}}
    after = {'parent': {'clips': [unique_new, duplicate_new, duplicate_new_again]}}
    held_params = old_unique['params']
    held_list = held_params['held']
    _retain_clip_rows(before, after)
    assert after['parent']['clips'][0] is old_unique
    assert old_unique['params'] is held_params
    assert old_unique['params']['held'] is held_list
    assert old_unique['params']['written'] is True
    assert after['parent']['clips'][1] is duplicate_new
    assert after['parent']['clips'][2] is duplicate_new_again


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


def test_public_commit_targets_shot_named_parent_when_parent_has_same_clip_id(
        monkeypatch, tmp_path, configured):
    """The SDK parent sentinel must not shadow a valid shot ID named parent."""
    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.timeline import commit_session, store
    from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document, document_from_bundle

    # Admission needs a compiled module in its snapshot, but selection here
    # never executes renderer code. Supply a deterministic placeholder so the
    # focused test does not depend on the separate Remotion installation.
    def compile_placeholder(staging, components):
        for component in set(components):
            target = staging / packs._compiled_path(component)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('// selector regression fixture')
    monkeypatch.setattr(packs, '_compile_components', compile_placeholder)
    owner, ref, _pin, initial = _setup(monkeypatch, tmp_path, configured)
    document = copy.deepcopy(initial['source_mapping']['_runtime_document'])
    shot = document['shot_revisions'][0]
    shot['shot_id'] = 'parent'
    parent_payload = document['parent_revision']['payload']
    parent_payload['occurrences'][0]['shot_id'] = 'parent'
    for row in (*parent_payload['clips'], *parent_payload['config']['clips']):
        if row['id'] == 'clip-a':
            row['clipType'] = 'am-discord'
    document['parent_revision']['content_digest'] = _digest(parent_payload)
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], document['internal_timeline_revisions'],
        document['dependency_manifest']['media'], document['dependency_manifest']['documents'])
    bundle = bundle_from_document(document)
    monkeypatch.setattr(store, 'fetch_bundle', lambda *a, **kw: copy.deepcopy(bundle))
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner,
                             documents=configured, media=configured.media)

    def commit(_project, _timeline_id, body, **kwargs):
        plan = execute(bundle, body['commands'], schema_inputs=owner.schema_inputs, realm_id='realm')
        data = {'status': 'accepted', 'base_revision_id': 'head', 'base_publication_id': 'publication',
                'accepted_revision_id': 'committed', 'commands': copy.deepcopy(body['commands']),
                'canonical_document': document_from_bundle(plan.bundle)}
        receipt = CommandReceipt('accepted-receipt', 'timeline.commit', kwargs['idempotency_key'],
                                 'hash', 'project', (1, 1), (), data, '2026-10-26T00:00:00Z')
        return DomainResult.success(data, receipt=receipt, idempotency_key=kwargs['idempotency_key'])

    owner.commit.side_effect = commit
    client._client = owner._client
    timeline = commit_session.open('project', 'film', client=client,
                                   outbox_path=tmp_path / 'parent-shot-outbox.json')

    result = timeline.commit(
        lambda working: next(clip for clip in working.clips
                             if clip.shot_id == 'parent' and clip.id == 'clip-a').set_element(ref),
        key='parent-shot-key')

    assert result['data']['commands'][0]['select'] == 'occurrence:occ-a/clip:clip-a'
    accepted = bundle_from_document(result['data']['canonical_document'])
    assert 'elementRef' not in accepted['parent']['clips'][0]
    shot_id = accepted['placements'][0]['shot_id']
    shot_clip = next(row for row in accepted['shots'][shot_id]['internal_timeline']['clips']
                     if row['id'] == 'clip-a')
    assert shot_clip['elementRef'] == ref


@pytest.mark.parametrize('case', ['disjoint-placement', 'disjoint-doc-patch', 'overlap'])
def test_public_function_stale_edit_uses_conservative_scope_guard(
        monkeypatch, tmp_path, configured, case):
    """Public function commits retry against a real detached provider head."""
    from astrid.core.receipts.canonical import canonical_json
    from astrid.sdk.timeline import commit_session, sync
    from astrid.sdk.timeline.commit import _digest, _manifest, create_runtime_commit_service
    from tests.sdk.test_timeline_commit_provider import DetachedPort, DetachedRealm, detached, runtime_abi

    # The provider module owns the test Runtime ABI exceptions used by its port.
    runtime_abi.__wrapped__(monkeypatch)
    from runtime_protocol.timeline_commit import CommitRejected

    real_ack = sync._ack
    owner, ref, pin, initial = _setup(monkeypatch, tmp_path, configured)
    monkeypatch.setattr(sync, '_ack', real_ack)

    document = copy.deepcopy(initial['source_mapping']['_runtime_document'])
    internal = document['internal_timeline_revisions'][0]
    internal['payload']['clips'][0]['id'] = 'placement-proof-clip'
    internal['content_digest'] = _digest(internal['payload'])
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], document['internal_timeline_revisions'],
        document['dependency_manifest']['media'], document['dependency_manifest']['documents'])
    bundle = bundle_from_document(document)
    target = 'occurrence:occ-a/clip:placement-proof-clip'
    realm = DetachedRealm(document)
    realm.entries[0]['receipt']['project_id'] = 'project'
    source, snapshots = owner.schema_inputs[0]
    realm.live_sources[_digest(pin)] = detached(source)
    realm.snapshot_objects = detached(snapshots)
    owner._client._last_handshake = {'realm_id': 'realm', 'actor_id': 'actor-1'}

    path = '/v1/projects/project/timelines/film/commit'
    service = create_runtime_commit_service()

    def runtime_accept(body, key):
        invocation = {'operation': 'commitTimeline', 'path': path,
                      'publisher': 'actor-1', 'request': copy.deepcopy(body)}
        context = {'request': body, 'invocation': invocation,
                   'idempotency_key': key,
                   'request_hash': hashlib.sha256(canonical_json(invocation).encode()).hexdigest(),
                   'realm_id': 'realm', 'publisher': 'actor-1'}
        port = DetachedPort(realm, context)
        port.context['request'].setdefault('rationale', '')  # Required by the legacy receipt fixture.
        accepted = service.commit(context, port)
        receipt = copy.deepcopy(accepted['receipt'])
        receipt['project_id'] = 'project'
        receipt['project_seq'] = [receipt['project_seq'][0], receipt['project_seq'][0]]
        realm.entries[-1]['receipt']['project_id'] = 'project'
        realm.entries[-1]['receipt']['project_seq'] = copy.deepcopy(receipt['project_seq'])
        realm.accepted[-1]['receipt']['project_id'] = 'project'
        realm.accepted[-1]['receipt']['project_seq'] = copy.deepcopy(receipt['project_seq'])
        return copy.deepcopy(accepted['data']), receipt

    def head_for(revision_id, publication_id):
        assert any(entry['revision_id'] == revision_id
                   and entry['publication_id'] == publication_id for entry in realm.entries)
        current_document = realm.documents[revision_id]
        current_bundle = bundle_from_document(copy.deepcopy(current_document))
        current_bundle['base_parent']['publication_id'] = publication_id
        return current_bundle

    owner.open_bundle.side_effect = lambda _project, _timeline, *, revision_id=None, publication_id=None: (
        DomainResult.success({
            'bundle': head_for(revision_id or realm.head['revision_id'],
                               publication_id or realm.head['publication_id']),
            'project_id': 'project', 'timeline_id': 'film',
            'head_revision_id': realm.head['revision_id'],
            'revision_id': revision_id or realm.head['revision_id'],
        }))

    def history(*_args, **_kwargs):
        return DomainResult.success({'head': copy.deepcopy(realm.head),
                                     'items': copy.deepcopy(realm.entries), 'next_cursor': None})

    owner.history.side_effect = history
    injected = False
    mixed_bodies = []
    mixed_bases = []
    peer_receipts = []
    peer_snapshot = []
    stale_failures = []

    def commit(_project, _timeline, body, *, idempotency_key):
        nonlocal injected
        is_mixed = any(command.get('op') in ('clip.set-element', 'doc.patch')
                       for command in body['commands'])
        if is_mixed:
            mixed_bodies.append(copy.deepcopy(body))
            mixed_bases.append((body['base_revision_id'], body['base_publication_id']))
            if not injected:
                injected = True
                if case == 'overlap':
                    from astrid.sdk.timeline.emitter import emit_document_commands
                    peer_before = head_for(realm.head['revision_id'], realm.head['publication_id'])
                    peer_after = copy.deepcopy(peer_before)
                    peer_after['parent']['config']['opaque']['timestamp'] = 'peer timestamp'
                    peer_commands = emit_document_commands(peer_before, peer_after)
                else:
                    peer_commands = [realm.move('parent/clip:clip-b', 8.008)]
                _peer_data, peer_receipt = runtime_accept({
                    'api_version': 1, 'project_id': 'project', 'timeline_id': 'film',
                    'base_revision_id': realm.head['revision_id'],
                    'base_publication_id': realm.head['publication_id'],
                    'commands': peer_commands, 'source': {'via': 'python'}, 'dry_run': False,
                    'label': 'Peer edit', 'merge': 'fields', 'reads': [],
                    'steps': [{'id': f'step-{i + 1}', 'label': 'Peer edit', 'intent': '',
                               'tags': [], 'command_ordinals': [i],
                               'patch': (copy.deepcopy(command.get('args', {}).get('changes', []))
                                         if command.get('op') == 'doc.patch' else [])}
                              for i, command in enumerate(peer_commands)]},
                    f'peer-{case}')
                peer_receipts.append(peer_receipt['receipt_id'])
                peer_snapshot.append((copy.deepcopy(realm.head), copy.deepcopy(realm.entries),
                                      copy.deepcopy(realm.documents), copy.deepcopy(realm.accepted)))
        try:
            data, receipt = runtime_accept(body, idempotency_key)
        except CommitRejected as exc:
            current_snapshot = (realm.head, realm.entries, realm.documents, realm.accepted)
            assert current_snapshot == peer_snapshot[-1]
            stale_failures.append(copy.deepcopy(exc.failure))
            raise WorkspaceClientError(409, exc.failure['code'], exc.failure['message'],
                                       exc.failure['details']) from exc
        receipt_obj = CommandReceipt.from_dict(receipt)
        return DomainResult.success(data, receipt=receipt_obj, idempotency_key=idempotency_key)

    owner.commit.side_effect = commit
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner,
                             documents=configured, media=configured.media)
    timeline = commit_session.open('project', 'film', client=client,
                                   outbox_path=tmp_path / f'public-{case}.json')
    evaluations = []

    def edit(working):
        evaluations.append(working.head.copy())
        if case != 'disjoint-doc-patch':
            working.clip('placement-proof-clip').set_element(ref)
        working.bundle['parent']['config']['opaque']['timestamp'] = 'function timestamp'

    if case == 'overlap':
        with pytest.raises(sync.CommitConflict) as error:
            timeline.commit(edit, label='Mixed placement and timestamp', key='mixed-overlap')
        assert len(evaluations) == 2
        assert evaluations[0] == {'revision_id': realm.entries[0]['revision_id'],
                                  'publication_id': realm.entries[0]['publication_id']}
        assert evaluations[1] == {'revision_id': realm.entries[1]['revision_id'],
                                  'publication_id': realm.entries[1]['publication_id']}
        assert len(mixed_bodies) == 1
        assert mixed_bases == [(realm.entries[0]['revision_id'], realm.entries[0]['publication_id'])]
        assert mixed_bodies[0]['merge'] == 'atomic'
        assert [command['op'] for command in mixed_bodies[0]['commands']] == ['clip.set-element', 'doc.patch']
        assert mixed_bodies[0]['commands'][0]['args']['schema_source'] == pin
        assert all(any(read == {'kind': 'scope', 'ref': 'timeline', 'path': []}
                       for read in step.get('reads', [])) for step in mixed_bodies[0]['steps'])
        assert len(stale_failures) == 1
        assert stale_failures[0]['details']['reason'] == 'atomic_scope_changed'
        assert stale_failures[0]['details']['current'] == evaluations[1]
        assert error.value.failure['details']['reason'] == 'intervening_write'
        conflicts = error.value.failure['details']['conflicts']
        assert any(row['ref'] == 'parent' and row['path'] == ['config', 'opaque', 'timestamp']
                   for row in conflicts)
        assert len(peer_receipts) == 1
        assert (realm.head, realm.entries, realm.documents, realm.accepted) == peer_snapshot[0]
        assert realm.head == {'revision_id': realm.entries[-1]['revision_id'],
                              'publication_id': peer_receipts[0]}
        landed = bundle_from_document(realm.documents[realm.head['revision_id']])
        assert State(landed).entity(target).get('elementRef') is None
        assert landed['parent']['config']['opaque']['timestamp'] == 'peer timestamp'
        assert State(landed).entity('parent/clip:clip-b')['at'] == State(bundle).entity('parent/clip:clip-b')['at']
        return

    accepted = timeline.commit(edit, label='Mixed placement and timestamp', key=f'mixed-{case}')
    assert len(evaluations) == 2
    assert evaluations[0] == {'revision_id': realm.entries[0]['revision_id'],
                              'publication_id': realm.entries[0]['publication_id']}
    assert evaluations[1] == {'revision_id': realm.entries[1]['revision_id'],
                              'publication_id': realm.entries[1]['publication_id']}
    assert len(mixed_bodies) == 2
    assert accepted['attempts'] == 2
    assert len(stale_failures) == 1
    refusal = stale_failures[0]
    assert refusal['message'] == 'intervening publication changed a guarded address'
    assert refusal['details']['status'] == 'conflict'
    assert refusal['details']['reason'] == 'atomic_scope_changed'
    assert refusal['details']['base'] == evaluations[0]
    assert refusal['details']['current'] == evaluations[1]
    assert any(site['ref'] == 'timeline' and site['path'] == []
               for site in refusal['details']['conflicts'])
    assert mixed_bases == [(realm.entries[0]['revision_id'], realm.entries[0]['publication_id']),
                           (realm.entries[1]['revision_id'], realm.entries[1]['publication_id'])]
    assert all(body['merge'] == 'atomic' for body in mixed_bodies)
    assert all([command['op'] for command in body['commands']] == (
        ['clip.set-element', 'doc.patch'] if case == 'disjoint-placement' else ['doc.patch'])
               for body in mixed_bodies)
    if case == 'disjoint-placement':
        assert all(command['args']['schema_source'] == pin for body in mixed_bodies
                   for command in body['commands'] if command['op'] == 'clip.set-element')
    assert all({'kind': 'scope', 'ref': 'timeline', 'path': []} in step['reads']
               for body in mixed_bodies for step in body['steps'])
    assert len(peer_receipts) == 1
    assert len(realm.accepted) == 2
    assert accepted['receipt']['receipt_id'] == realm.head['publication_id']
    assert len([entry for entry in realm.entries if entry['publication_id'] == accepted['receipt']['receipt_id']]) == 1
    landed = bundle_from_document(realm.documents[realm.head['revision_id']])
    assert landed['parent']['config']['opaque']['timestamp'] == 'function timestamp'
    assert State(landed).entity('parent/clip:clip-b')['at'] == 8.008
    original_rows = {row['id']: row for row in bundle['parent']['clips']}
    final_rows = {row['id']: row for row in landed['parent']['clips']}
    assert all(final_rows[row_id] == row for row_id, row in original_rows.items() if row_id != 'clip-b')
    if case == 'disjoint-placement':
        assert State(landed).entity(target)['elementRef'] == ref
    else:
        assert State(landed).entity(target).get('elementRef') is None


def test_public_held_clip_views_retarget_twice_then_undo(monkeypatch, tmp_path, configured):
    """Two held Clip views remain writable after sequential element retargets."""
    from astrid.sdk.timeline import commit_session, sync
    from astrid.sdk.timeline.commit import _digest, _manifest, create_runtime_commit_service
    from astrid.core.receipts.canonical import canonical_json
    from tests.sdk.test_timeline_commit_provider import DetachedPort, DetachedRealm, detached, runtime_abi

    runtime_abi.__wrapped__(monkeypatch)
    real_ack = sync._ack
    owner, ref, pin, initial = _setup(monkeypatch, tmp_path, configured)
    monkeypatch.setattr(sync, '_ack', real_ack)

    document = copy.deepcopy(initial['source_mapping']['_runtime_document'])
    internal = document['internal_timeline_revisions'][0]
    internal['payload']['clips'][0].update(
        id='effect-a', params={'seed': 4, 'future': {'keep': [None, False, {}]}})
    internal['content_digest'] = _digest(internal['payload'])
    shot = copy.deepcopy(document['shot_revisions'][0])
    shot.update(shot_id='chapter-shot-b', revision_id='shot-2')
    internal_b = copy.deepcopy(internal)
    internal_b.update(timeline_id='chapter-two', revision_id='internal-2')
    internal_b['payload']['clips'][0].update(
        id='effect-b', params={'seed': 4, 'future': {'keep': [None, False, {}]}})
    internal_b['content_digest'] = _digest(internal_b['payload'])
    shot['internal_timeline_revision_id'] = 'internal-2'
    shot['payload']['internal_timeline_revision_id'] = 'internal-2'
    shot['payload']['items'][0]['item_id'] = 'item-2'
    shot['content_digest'] = _digest(shot['payload'])
    document['shot_revisions'].append(shot)
    document['internal_timeline_revisions'].append(internal_b)
    occurrence = copy.deepcopy(document['parent_revision']['payload']['occurrences'][0])
    occurrence.update(occurrence_id='occ-b', shot_id='chapter-shot-b', shot_revision_id='shot-2')
    document['parent_revision']['payload']['occurrences'].append(occurrence)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], document['internal_timeline_revisions'],
        document['dependency_manifest']['media'], document['dependency_manifest']['documents'])
    baseline = bundle_from_document(document)

    realm = DetachedRealm(document)
    realm.entries[0]['receipt']['project_id'] = 'project'
    source, snapshots = owner.schema_inputs[0]
    realm.live_sources[_digest(pin)] = detached(source)
    realm.snapshot_objects = detached(snapshots)
    owner._client._last_handshake = {'realm_id': 'realm', 'actor_id': 'actor-1'}
    service = create_runtime_commit_service()

    def head_bundle(revision_id=None, publication_id=None):
        revision_id = revision_id or realm.head['revision_id']
        publication_id = publication_id or realm.head['publication_id']
        result = bundle_from_document(copy.deepcopy(realm.documents[revision_id]))
        result['base_parent']['publication_id'] = publication_id
        return result

    owner.open_bundle.side_effect = lambda _p, _t, *, revision_id=None, publication_id=None: DomainResult.success({
        'bundle': head_bundle(revision_id, publication_id), 'project_id': 'project',
        'timeline_id': 'film', 'head_revision_id': realm.head['revision_id'],
        'revision_id': revision_id or realm.head['revision_id']})
    owner.history.side_effect = lambda *_a, **_kw: DomainResult.success({
        'head': copy.deepcopy(realm.head), 'items': copy.deepcopy(realm.entries), 'next_cursor': None})

    path = '/v1/projects/project/timelines/film/commit'

    def runtime_accept(body, key):
        invocation = {'operation': 'commitTimeline', 'path': path,
                      'publisher': 'actor-1', 'request': copy.deepcopy(body)}
        context = {'request': body, 'invocation': invocation, 'idempotency_key': key,
                   'request_hash': hashlib.sha256(canonical_json(invocation).encode()).hexdigest(),
                   'realm_id': 'realm', 'publisher': 'actor-1'}
        port = DetachedPort(realm, context)
        port.context['request'].setdefault('rationale', '')
        accepted = service.commit(context, port)
        receipt = copy.deepcopy(accepted['receipt'])
        receipt['project_id'] = 'project'
        receipt['project_seq'] = [receipt['project_seq'][0], receipt['project_seq'][0]]
        realm.entries[-1]['receipt']['project_id'] = 'project'
        realm.entries[-1]['receipt']['project_seq'] = copy.deepcopy(receipt['project_seq'])
        realm.accepted[-1]['receipt']['project_id'] = 'project'
        realm.accepted[-1]['receipt']['project_seq'] = copy.deepcopy(receipt['project_seq'])
        return accepted['data'], receipt

    def commit(_project, _timeline, body, *, idempotency_key):
        data, receipt = runtime_accept(body, idempotency_key)
        return DomainResult.success(data, receipt=CommandReceipt.from_dict(receipt),
                                    idempotency_key=idempotency_key)

    owner.commit.side_effect = commit
    def send(project, timeline_id, body, *, idempotency_key):
        result = owner.commit(project, timeline_id, body, idempotency_key=idempotency_key)
        assert result.ok, result.as_dict()
        return {'data': result.data, 'receipt': result.receipt.as_dict()}

    owner._client.commit_timeline = send
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner,
                             documents=configured, media=configured.media)
    timeline = commit_session.open('project', 'film', client=client,
                                   outbox_path=tmp_path / 'held-clip-views.json')

    held, retained = {}, {}

    def edit(working):
        held['a'] = next(clip for clip in working.clips() if clip.id == 'effect-a')
        held['b'] = next(clip for clip in working.clips() if clip.id == 'effect-b')
        retained['a'] = held['a'].params
        retained['b'] = held['b'].params
        held['a'].set_element(ref)
        held['b'].set_element(ref)
        held['a'].params['seed'] = 8
        held['b'].params['seed'] = 9
        retained['a']['future']['keep'].append('held-a')
        retained['b']['future']['keep'].append('held-b')

    accepted = timeline.commit(edit, label='Retarget both held clips', key='held-views')
    landed = bundle_from_document(accepted['data']['canonical_document'])
    targets = [('occurrence:occ-a/clip:effect-a', 8, 'held-a'),
               ('occurrence:occ-b/clip:effect-b', 9, 'held-b')]
    for target, seed, marker in targets:
        row = State(landed).entity(target)
        assert row['elementRef'] == ref
        assert row['params'] == {'seed': seed, 'future': {'keep': [None, False, {}, marker]}}

    undone = timeline.undo(publications=[accepted['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    restored_document = document_from_bundle(restored)
    original_document = document_from_bundle(baseline)
    expected = copy.deepcopy(original_document)
    expected['dependency_manifest']['documents'] = [pin]
    expected['parent_revision']['revision_id'] = restored_document['parent_revision']['revision_id']
    parent_revision_id = restored_document['parent_revision']['revision_id']
    assert parent_revision_id.startswith('authoring-parent-revision-')
    digest = parent_revision_id.removeprefix('authoring-parent-revision-')
    assert len(digest) == 64 and all(character in '0123456789abcdef' for character in digest)
    assert restored_document['dependency_manifest']['documents'] == [pin]
    assert restored_document['parent_revision']['payload'] == original_document['parent_revision']['payload']
    assert restored_document['parent_revision']['content_digest'] == original_document['parent_revision']['content_digest']
    assert restored_document == expected


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


def test_public_mixed_commit_retries_first_placement_after_pack_install(
        monkeypatch, tmp_path, configured):
    """A newly installed source effect remains placeable in one public mixed commit."""
    import yaml
    from astrid.core.element.registry import clear_default_registry_cache, load_default_registry
    from astrid.sdk.timeline import commit_session, store
    from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document, create_runtime_commit_service
    from tests.core.test_elements_registry import write_pack_element, finalize_v3_pack
    from tests.sdk.test_timeline_commit_provider import DetachedPort, DetachedRealm, detached, runtime_abi

    runtime_abi.__wrapped__(monkeypatch)
    roots = tmp_path / 'personal-packs'
    roots.mkdir()
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(roots))
    from astrid.core.receipts.canonical import canonical_json
    from tests.sdk.test_timeline_commit_provider_document import document_fixture

    # Build the personal project pack and its admission before warming source discovery.
    test_project_packs.seed('project')
    # Admit the project-owned fork before the source pack is made discoverable.
    project_root = Path(packs.inspect_project_pack('realm', 'project')['packRoot'])
    element_dir = project_root / 'rendering/elements/effects/reveal'
    manifest = yaml.safe_load((project_root / 'pack.yaml').read_text())
    definition_path = element_dir / 'element.yaml'
    definition = yaml.safe_load(definition_path.read_text())
    definition['id'] = 'am-footage'
    definition_path.write_text(yaml.safe_dump(definition))
    renamed_element_dir = element_dir.with_name('am-footage')
    element_dir.rename(renamed_element_dir)
    element_dir = renamed_element_dir
    declaration = manifest['rendering'].pop('effects/reveal')
    declaration['path'] = declaration['path'].replace('/reveal/', '/am-footage/')
    for resource in declaration['resources']:
        resource['path'] = resource['path'].replace('/reveal/', '/am-footage/')
    manifest['rendering']['effects/am-footage'] = declaration
    (project_root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    def compile_placeholder(staging, components):
        for component in set(components):
            compiled = staging / packs._compiled_path(component)
            compiled.parent.mkdir(parents=True, exist_ok=True)
            compiled.write_text('// registry placement regression fixture')
    monkeypatch.setattr(packs, '_compile_components', compile_placeholder)
    admission = packs.admit_project_pack('realm', 'project')
    selected_record = next(item for item in admission['elements']
                           if item['descriptor']['id'] == 'am-footage')
    ref = {key: selected_record['descriptor'][key]
           for key in ('id', 'kind', 'packId', 'revision')}
    admitted_row = configured.rows[('project', admission['documentId'])]
    admitted_row['project_id'] = 'project'
    pin = {'document_id': admitted_row['document_id'], 'version': admitted_row['version'],
           'content_digest': packs._json_digest(admitted_row['content'])}
    objects = {}
    for item in admitted_row['content']['admissions']:
        descriptor = item['snapshot_object']
        objects[descriptor['object_id']] = {
            **descriptor, 'bytes_base64': base64.b64encode(
                configured.media.read_bytes(descriptor['object_id'])).decode()}
    schema_inputs = [({'pin': pin, 'document': admitted_row}, objects)]

    monkeypatch.setattr('astrid.sdk.timeline_elements.Documents', lambda _client: configured)
    monkeypatch.setattr('astrid.sdk.timeline_elements.RemoteMedia', lambda _client: configured.media)
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'outbox'))
    document = document_fixture(shared=False)
    document.update(project_id='project', timeline_id='film')
    internal = document['internal_timeline_revisions'][0]
    clip = internal['payload']['clips'][0]
    clip.pop('elementRef', None)
    clip['clipType'] = 'am-footage'
    for row in document['parent_revision']['payload'].get('clips', []):
        if row.get('id') == clip['id']:
            row['id'] = 'parent-' + row['id']
    for row in document['parent_revision']['payload'].get('config', {}).get('clips', []):
        if row.get('id') == clip['id']:
            row['id'] = 'parent-' + row['id']
    internal['content_digest'] = _digest(internal['payload'])
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    document['dependency_manifest'] = _manifest(
        document['shot_revisions'], document['internal_timeline_revisions'],
        document['dependency_manifest']['media'], document['dependency_manifest']['documents'])
    bundle = bundle_from_document(document)
    owner = Mock()
    owner.schema_inputs = schema_inputs
    owner._client._last_handshake = {'realm_id': 'realm', 'actor_id': 'actor-1'}
    owner.open_composition.return_value = DomainResult.success({
        'native_inspection': {'project_id': 'project', 'timeline_id': 'film'},
        'summary': {'head_revision_id': 'head'}})
    owner.open_bundle.return_value = DomainResult.success({
        'bundle': bundle, 'project_id': 'project', 'timeline_id': 'film',
        'head_revision_id': 'head', 'revision_id': 'head'})

    clear_default_registry_cache()
    load_default_registry()
    # Install the synthetic source pack after real default discovery has warmed.
    source = roots / 'astrid_motion'
    write_pack_element(source, 'effects', 'am-footage', pack_id='astrid_motion', label='Footage')
    finalize_v3_pack(source, [('effects', 'am-footage')])
    target = f"occurrence:occ-a/clip:{clip['id']}"
    realm = DetachedRealm(document)
    realm.entries[0]['receipt']['project_id'] = 'project'
    source_doc, snapshots = schema_inputs[0]
    realm.live_sources[_digest(pin)] = detached(source_doc)
    realm.snapshot_objects = detached(snapshots)
    owner._client._last_handshake = {'realm_id': 'realm', 'actor_id': 'actor-1'}
    service = create_runtime_commit_service()

    def accept(body, key):
        invocation = {'operation': 'commitTimeline', 'path': '/v1/projects/project/timelines/film/commit',
                      'publisher': 'actor-1', 'request': copy.deepcopy(body)}
        context = {'request': body, 'invocation': invocation, 'idempotency_key': key,
                   'request_hash': hashlib.sha256(canonical_json(invocation).encode()).hexdigest(),
                   'realm_id': 'realm', 'publisher': 'actor-1'}
        port = DetachedPort(realm, context)
        port.context['request'].setdefault('rationale', '')  # Required by the legacy receipt fixture.
        accepted = service.commit(context, port)
        receipt = copy.deepcopy(accepted['receipt'])
        receipt['project_id'] = 'project'
        receipt['project_seq'] = [receipt['project_seq'][0], receipt['project_seq'][0]]
        realm.entries[-1]['receipt']['project_id'] = 'project'
        realm.entries[-1]['receipt']['project_seq'] = copy.deepcopy(receipt['project_seq'])
        realm.accepted[-1]['receipt']['project_id'] = 'project'
        realm.accepted[-1]['receipt']['project_seq'] = copy.deepcopy(receipt['project_seq'])
        return accepted['data'], receipt

    owner.open_bundle.side_effect = lambda *_a, **_kw: DomainResult.success({
        'bundle': bundle, 'project_id': 'project', 'timeline_id': 'film',
        'head_revision_id': realm.head['revision_id'], 'revision_id': realm.head['revision_id']})
    owner.history.side_effect = lambda *_a, **_kw: DomainResult.success({
        'head': copy.deepcopy(realm.head), 'items': copy.deepcopy(realm.entries), 'next_cursor': None})

    def commit(_project, _timeline, body, *, idempotency_key):
        data, receipt = accept(body, idempotency_key)
        return DomainResult.success(data, receipt=CommandReceipt.from_dict(receipt),
                                    idempotency_key=idempotency_key)

    owner.commit.side_effect = commit
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner,
                             documents=configured, media=configured.media)
    monkeypatch.setattr(store, 'fetch_bundle', lambda *_a, **_kw: copy.deepcopy(bundle))
    timeline = commit_session.open('project', 'film', client=client,
                                   outbox_path=tmp_path / 'warm-corpus-outbox.json')
    def edit(working):
        next(item for item in working.clips if item.id == clip['id']).set_element(ref)
        working.bundle['parent']['config']['opaque'].update({'timestamp': 'mixed edit'})
    accepted = timeline.commit(edit, key='warm-corpus-mixed')
    landed = bundle_from_document(accepted['data']['canonical_document'])
    assert State(landed).entity(target)['elementRef'] == ref
    assert landed['parent']['config']['opaque']['timestamp'] == 'mixed edit'
    assert len(realm.entries) == 2

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


def test_native_first_placement_refuses_ambiguous_builtin_kind(monkeypatch, tmp_path, configured):
    from types import SimpleNamespace

    owner, ref, _, bundle = _setup(monkeypatch, tmp_path, configured)
    registry = Mock()
    registry.list.return_value = [
        SimpleNamespace(id='am-discord', kind='effects'),
        SimpleNamespace(id='am-discord', kind='animations'),
    ]
    monkeypatch.setattr('astrid.core.element.registry.load_default_registry', lambda: registry)
    command = {
        'schema_version': 1,
        'op': 'clip.set-element',
        'version': '1',
        'select': TARGET,
        'args': {'elementRef': ref, 'placement': 'place'},
    }
    before = copy.deepcopy(bundle)

    with pytest.raises(CommandError, match='requires an existing element ref'):
        execute(bundle, [command], schema_inputs=owner.schema_inputs, realm_id='realm')

    assert bundle == before
    owner.commit.assert_not_called()


def test_native_first_placement_refuses_unreadable_builtin_registry(monkeypatch, tmp_path, configured):
    owner, ref, _, bundle = _setup(monkeypatch, tmp_path, configured)
    registry = Mock()
    registry.list.side_effect = RuntimeError('registry unavailable')
    monkeypatch.setattr('astrid.core.element.registry.load_default_registry', lambda: registry)
    command = {
        'schema_version': 1,
        'op': 'clip.set-element',
        'version': '1',
        'select': TARGET,
        'args': {'elementRef': ref, 'placement': 'place'},
    }
    before = copy.deepcopy(bundle)

    with pytest.raises(CommandError, match='requires an existing element ref'):
        execute(bundle, [command], schema_inputs=owner.schema_inputs, realm_id='realm')

    assert bundle == before
    owner.commit.assert_not_called()
