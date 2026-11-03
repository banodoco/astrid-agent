"""Ordinary checkout publication crosses the real Runtime commit boundary."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk import test_timeline_restructure
from tests.sdk.test_scene_save_core import no_renderer  # noqa: F401

restructure_runtime = test_timeline_restructure.restructure_runtime


def history(env):
    return env.client.timelines.history(env.project, 't', limit=200).data


def test_ordinary_publish_preserves_complete_closure_and_followups(restructure_runtime, tmp_path, request):
    env = restructure_runtime
    original = copy.deepcopy(env.document())
    base = history(env)['head']
    checkout = Checkout.open(env.project, 't', client=env.client)
    checkout.save(tmp_path / 'draft.json', quiet=True)
    checkout.bundle['parent']['opaque']['edited'] = [False, 0, None, {'literal': 'film'}]
    checkout.clip('p1').data['opaque'] = {'null': None, 'ordered': [3, 2, 1]}
    expected = compile_authoring_candidate(checkout.stored_document()).publication
    request.getfixturevalue('no_renderer')
    result = checkout.publish('Ordinary edit', client=env.client, idempotency_key='ordinary')
    assert len(env.calls) == 2
    body = env.calls[0][0]
    assert body['base_revision_id'] == base['revision_id']
    assert body['base_publication_id'] == base['publication_id']
    assert body['label'] == 'Ordinary edit' and body['source'] == {'via': 'python'}
    assert body['merge'] == 'atomic' and 'reads' not in body
    assert body['steps'][0]['command_ordinals'] == list(range(len(body['commands'])))
    assert body['steps'][0]['patch'] == [c for call in body['commands'] for c in call['args']['changes']]
    assert all(call['op'] == 'doc.patch' for call in body['commands'])
    cut = env.replies[0]['data']['canonical_document']
    assert cut['parent_revision']['payload'] == expected['parent_composition']
    assert cut['shot_revisions'] == expected['shot_revisions']
    assert cut['internal_timeline_revisions'] == expected['internal_timeline_revisions']
    assert cut['dependency_manifest'] == {**expected['dependency_manifest'], 'documents': []}
    assert cut['parent_revision']['payload']['opaque']['null'] is None
    assert 'missing' not in cut['parent_revision']['payload']['opaque']
    assert cut['parent_revision']['payload']['occurrences'][0]['shot_id'] == 'film'
    assert original['dependency_manifest']['media'] == cut['dependency_manifest']['media']
    assert result['narration_pinned'] == ['film']
    assert result['transcripts'][0]['status'] == 'created'
    assert result['receipt'] == env.replies[0]['receipt']
    assert checkout.base_revision == result['new_head'] and checkout.changes() == []
    assert checkout._journal == []
    assert Checkout.load(checkout.path).base_revision == result['new_head']


@pytest.mark.parametrize('lost_at', [1, 2])
def test_ordinary_publish_exact_retry_and_narration_resume(restructure_runtime, lost_at):
    env = restructure_runtime
    checkout = Checkout.open(env.project, 't', client=env.client)
    checkout.bundle['parent']['opaque']['edited'] = True
    count = len(history(env)['items'])
    lost = False

    def send(*args, **kwargs):
        nonlocal lost
        result = env.send(*args, **kwargs)
        if len(env.calls) == lost_at and not lost:
            lost = True
            raise WorkspaceClientError(0, 'unavailable', 'lost ACK')
        return result

    env.generated.commit_timeline = send
    if lost_at == 1:
        with pytest.raises(WorkspaceClientError):
            checkout.publish('Ordinary retry', client=env.client, idempotency_key='retry')
    else:
        first = checkout.publish('Ordinary retry', client=env.client, idempotency_key='retry')
        assert first['narration_error']
    reopened = Checkout.open(env.project, 't', client=env.client)
    result = reopened.publish('Ordinary retry', client=env.client, idempotency_key='retry')
    assert env.calls[lost_at - 1] == env.calls[lost_at]
    assert env.replies[lost_at - 1] == env.replies[lost_at]
    assert len(history(env)['items']) == count + 2
    assert len(env.calls) == 3
    assert result['receipt'] == env.replies[0]['receipt']
    assert result['new_head'] == env.document()['parent_revision']['revision_id']
    assert env.document_calls.count('create') == 1


@pytest.mark.parametrize('damage', ['before', 'media', 'base', 'stale', 'preview'])
def test_ordinary_provider_refuses_bad_capture_without_writes(restructure_runtime, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    from astrid.sdk.timeline.emitter import emit_document_commands

    env = restructure_runtime
    base = history(env)['head']
    before = bundle_from_document(env.document())
    after = copy.deepcopy(before)
    after['parent']['opaque']['null'] = 'changed'
    calls = emit_document_commands(before, after)
    body = {'api_version': 1, 'project_id': env.project, 'timeline_id': 't',
            'base_revision_id': base['revision_id'], 'base_publication_id': base['publication_id'],
            'commands': calls, 'label': 'Guarded', 'merge': 'atomic',
            'steps': [{'id': 'edit', 'label': 'Guarded', 'intent': '', 'tags': [],
                       'command_ordinals': [0], 'patch': copy.deepcopy(calls[0]['args']['changes'])}]}
    if damage == 'before':
        calls[0]['args']['changes'][0]['before']['value'] = False
        body['steps'][0]['patch'] = copy.deepcopy(calls[0]['args']['changes'])
    elif damage == 'media':
        body['verified_media'] = [{'object_id': 'sha256:' + '0' * 64}]
    elif damage == 'base':
        body['base_publication_id'] = 'not-the-base'
    elif damage == 'stale':
        from astrid.sdk.timeline import open as open_timeline
        peer = open_timeline(env.project, 't', client=env.client)
        peer.bundle['parent']['opaque']['peer'] = True
        peer.commit('Peer', idempotency_key='peer')
    else:
        body['dry_run'] = True
    original, entries = copy.deepcopy(env.document()), history(env)
    database = tuple(env.service.store.conn.iterdump())
    if damage == 'preview':
        result = env.send(env.project, 't', body, idempotency_key='preview')
        assert result['receipt'] is None
        assert result['data']['status'] == 'preview'
        assert result['data']['would_status'] == 'accepted'
        assert result['data']['steps'][0]['dependencies']
    else:
        with pytest.raises(RuntimeErrorBase):
            env.send(env.project, 't', body, idempotency_key='bad-' + damage)
    assert env.document() == original and history(env) == entries
    assert tuple(env.service.store.conn.iterdump()) == database


def test_ordinary_publish_recovers_narration_before_flight(restructure_runtime):
    env = restructure_runtime
    checkout = Checkout.open(env.project, 't', client=env.client)
    checkout.bundle['parent']['opaque']['edited'] = True
    ingest = env.client.media.import_bytes
    env.client.media.import_bytes = lambda **kw: (_ for _ in ()).throw(WorkspaceClientError(0, 'unavailable', 'ingest failed'))
    first = checkout.publish('Narration recovery', client=env.client)
    assert first['narration_error'] and len(env.calls) == 1
    env.client.media.import_bytes = ingest
    result = checkout.publish('Narration recovery', client=env.client)
    assert len(env.calls) == 2
    assert result['receipt'] == first['receipt']
    assert result['narration_pinned'] == ['film']
    assert env.document_calls.count('create') == 1


def test_ordinary_publish_changed_key_payload_and_force_never_write(restructure_runtime):
    from astrid.sdk.timeline._common import TimelineEditError
    env = restructure_runtime
    checkout = Checkout.open(env.project, 't', client=env.client)
    checkout.bundle['parent']['opaque']['edited'] = True
    checkout.publish('Original', client=env.client, idempotency_key='original')
    before, entries = copy.deepcopy(env.document()), history(env)
    checkout.bundle['parent']['opaque']['edited'] = False
    with pytest.raises((TimelineEditError, WorkspaceClientError)):
        checkout.publish('Original', client=env.client, idempotency_key='original')
    with pytest.raises(TimelineEditError, match='force is unavailable'):
        checkout.publish('Original', client=env.client, force=True)
    assert env.document() == before and history(env) == entries


def test_nudge_disjoint_clips_merge_onto_peer_head(restructure_runtime):
    """A captured operation-time nudge retains the existing disjoint merge behavior."""
    from astrid.sdk.timeline import open as open_timeline
    env = restructure_runtime
    checkout = Checkout.open(env.project, 't', client=env.client)
    base = history(env)['head']
    start = checkout.clip('cross').start
    checkout.clip('cross').nudge(0.5)
    peer = open_timeline(env.project, 't', client=env.client)
    peer.clip('p2').data['opaque'] = {'peer': True}
    peer.commit('Peer clip', idempotency_key='peer-clip')
    result = checkout.publish('Local clip', client=env.client, idempotency_key='local-clip')
    assert result['receipt']['result']['status'] == 'merged'
    body = env.calls[-2][0]
    assert body['merge'] == 'fields' and body['steps'][0]['reads']
    assert body['base_publication_id'] == base['publication_id']
    loaded = Checkout.open(env.project, 't', client=env.client)
    assert loaded.clip('cross').start == pytest.approx(start + 0.5)
    assert loaded.clip('p2').data['opaque'] == {'peer': True}


def test_ordinary_direct_opaque_edit_is_atomic_and_refuses_peer_head(restructure_runtime):
    """An uncaptured direct edit is conservative when another head intervenes."""
    from astrid.sdk.timeline import open as open_timeline
    from astrid.sdk.timeline._common import TimelineEditError

    env = restructure_runtime
    checkout = Checkout.open(env.project, 't', client=env.client)
    checkout.clip('p1').data['opaque'] = {'local': True}
    peer = open_timeline(env.project, 't', client=env.client)
    peer.clip('p2').data['opaque'] = {'peer': True}
    peer.commit('Peer clip', idempotency_key='peer-opaque')

    before_document = copy.deepcopy(env.document())
    before_history = history(env)
    before_head = copy.deepcopy(before_history['head'])
    before_database = tuple(env.service.store.conn.iterdump())
    with pytest.raises((TimelineEditError, WorkspaceClientError)):
        checkout.publish('Local opaque edit', client=env.client, idempotency_key='local-opaque')

    body = env.calls[-1][0]
    assert body['merge'] == 'atomic' and 'reads' not in body
    assert env.document() == before_document
    after_history = history(env)
    assert after_history == before_history and after_history['head'] == before_head
    assert tuple(env.service.store.conn.iterdump()) == before_database
