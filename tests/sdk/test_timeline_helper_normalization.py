"""Saved numeric helpers replay against the authenticated, unmigrated base."""
from __future__ import annotations

import copy
import json
import subprocess
import sys

import pytest

from astrid.sdk.timeline import journal
from astrid.sdk.timeline import open as open_timeline
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk.test_timeline_restructure import restructure_runtime  # noqa: F401


@pytest.fixture
def numeric_helper(restructure_runtime, tmp_path, monkeypatch):  # noqa: F811
    env = restructure_runtime
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path))
    head = env.client.timelines.history(env.project, 't', limit=200).data['head']
    original = env.client.timelines.open_bundle(
        env.project, 't', revision_id=head['revision_id'], publication_id=head['publication_id'],
    ).data['bundle']
    candidate = copy.deepcopy(original)
    clip = next(row for row in candidate['shots']['film']['internal_timeline']['clips']
                if row['id'] == 'cross')
    clip['app'] = {'why': 'numeric timing', 'opaque': {'null': None}}
    commands = emit_document_commands(original, candidate)
    result = env.client.timelines.commit(env.project, 't', {
        'api_version': 1, 'project_id': env.project, 'timeline_id': 't',
        'base_revision_id': original['base_parent']['revision_id'],
        'base_publication_id': original['base_parent']['publication_id'],
        'commands': commands, 'label': 'Legacy numeric input', 'merge': 'atomic',
        'source': {'via': 'python'},
        'steps': [{'id': 'setup', 'label': 'Legacy numeric input', 'intent': '', 'tags': [],
                   'command_ordinals': list(range(len(commands))),
                   'patch': [change for call in commands for change in call['args']['changes']],
                   'reads': [{'kind': 'scope', 'ref': 'timeline', 'path': []}]}],
    }, idempotency_key='legacy-numeric')
    assert result.ok, result.as_dict()
    head = env.client.timelines.history(env.project, 't', limit=200).data['head']
    base = env.client.timelines.open_bundle(
        env.project, 't', revision_id=head['revision_id'], publication_id=head['publication_id'],
    ).data['bundle']
    assert base['shots']['film']['internal_timeline']['clips'][3]['app']['why'] == 'numeric timing'
    tl = Checkout.open(env.project, 't', client=env.client)
    assert tl.clip('cross').data['app']['astrid']['why'] == 'numeric timing'
    drafts = tmp_path / 'drafts'
    drafts.mkdir()
    tl.save(drafts / 'numeric.json', quiet=True)
    env.calls.clear()
    env.replies.clear()
    return env, tl, base


def peer_edit(env, clip='late', value='peer', key='peer'):
    peer = open_timeline(env.project, 't', client=env.client)
    peer.clip(clip).data['params']['text'] = value
    return peer.commit(key, idempotency_key=key)


def test_numeric_helper_normalization_survives_restart_and_merges(numeric_helper, tmp_path):
    env, tl, base = numeric_helper
    subprocess.run([
        sys.executable, '-c',
        'import sys; from astrid.sdk.timeline.checkout import Checkout; '
        'tl = Checkout.load(sys.argv[1]); tl.clip("cross").nudge(0.5); tl.save(quiet=True)',
        str(tl.path),
    ], check=True, capture_output=True, text=True, timeout=45)
    path = journal.journal_path(env.project, 't')
    saved = journal.read(path)[-1]
    assert journal.verify(path) == []
    assert saved.calls[0]['base'] == {key: base['base_parent'][key]
                                      for key in ('revision_id', 'publication_id')}
    assert saved.calls[0]['reads']
    assert saved.calls[0]['commands']
    tl = Checkout.load(tl.path)
    assert not tl._helper_calls
    peer_edit(env)
    result = tl.publish('Numeric helper', client=env.client, idempotency_key='numeric-helper')
    assert result['receipt']['result']['status'] == 'merged'
    assert saved.calls[0]['before'] == journal.authored_digest(base)
    body = next(body for body, _key in env.calls if body['label'] == 'Numeric helper')
    assert body['merge'] == 'fields'
    assert body['base_revision_id'] == base['base_parent']['revision_id']
    assert body['base_publication_id'] == base['base_parent']['publication_id']
    assert body['steps'][0]['intent'] == 'nudge'
    assert body['steps'][0]['reads'] == saved.calls[0]['reads']
    assert body['reads'] == saved.calls[0]['reads']
    assert body['commands'] == saved.calls[0]['commands']
    loaded = Checkout.open(env.project, 't', client=env.client)
    assert loaded.clip('cross').start == pytest.approx(4.0)
    assert loaded.clip('late').params['text'] == 'peer'
    assert loaded.clip('cross').data['app']['opaque'] == {'null': None}
    history = env.client.timelines.history(env.project, 't', limit=200).data
    assert history['head']['revision_id'] == result['new_head']
    entry = next(row for row in history['items']
                 if row['publication_id'] == result['receipt']['receipt_id'])
    assert entry['receipt'] == result['receipt']
    (tmp_path / 'numeric-capture.json').write_text(json.dumps({
        'base': base, 'journal': [row.to_json() for row in journal.read(path)],
        'requests': env.calls, 'replies': env.replies, 'history': history,
        'canonical_document': env.document(),
    }, sort_keys=True))


@pytest.mark.parametrize('mode', ['same', 'aba', 'free', 'mixed', 'gap'])
def test_numeric_helper_conflicts_are_atomic_no_write(numeric_helper, mode):
    env, tl, _base = numeric_helper
    if mode != 'free':
        tl.clip('cross').nudge(0.5)
    if mode in {'free', 'mixed', 'gap'}:
        tl.clip('cross').data['opaque'] = {'uncaptured': True}
    if mode == 'gap':
        tl.clip('cross').nudge(frames=3)
    tl.save(quiet=True)
    if mode in {'same', 'aba'}:
        peer_edit(env, clip='cross')
        if mode == 'aba':
            peer = open_timeline(env.project, 't', client=env.client)
            peer.clip('cross').data['params'].pop('text')
            peer.commit('Restore', idempotency_key='restore')
    else:
        peer_edit(env)
    before = tuple(env.service.store.conn.iterdump())
    history = copy.deepcopy(env.client.timelines.history(env.project, 't', limit=200).data)
    with pytest.raises((TimelineEditError, WorkspaceClientError)):
        Checkout.load(tl.path).publish('Refused numeric helper', client=env.client)
    assert tuple(env.service.store.conn.iterdump()) == before
    assert env.client.timelines.history(env.project, 't', limit=200).data == history
    assert env.calls[-1][0]['merge'] == ('fields' if mode in {'same', 'aba'} else 'atomic')


@pytest.mark.parametrize('damage', ['hash', 'reads', 'before', 'after', 'base', 'commands', 'order', 'op'])
def test_numeric_helper_journal_claims_are_not_authority(numeric_helper, damage):
    env, tl, _base = numeric_helper
    tl.clip('cross').nudge(0.5)
    tl.clip('cross').nudge(frames=3)
    tl.save(quiet=True)
    path = journal.journal_path(env.project, 't')
    rows = [row.to_json() for row in journal.read(path)]
    row = rows[-1]
    call = row['calls'][0]
    if damage == 'hash':
        row['label'] = 'damaged'
    elif damage == 'reads':
        call['reads'][0]['state']['value']['at'] = 99
    elif damage in {'before', 'after'}:
        call[damage] = 'sha256:' + '0' * 64
    elif damage == 'base':
        call['base']['publication_id'] = 'wrong-publication'
    elif damage == 'commands':
        call['commands'][0]['args']['changes'][0]['value'] = 99
    elif damage == 'order':
        row['calls'].reverse()
    else:
        call['op'] = 'unknown'
    if damage != 'hash':
        row['hash'] = journal._hash(row['prev'], {k: v for k, v in row.items() if k != 'hash'})
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    peer_edit(env)
    before = tuple(env.service.store.conn.iterdump())
    history = copy.deepcopy(env.client.timelines.history(env.project, 't', limit=200).data)
    with pytest.raises((TimelineEditError, WorkspaceClientError)):
        Checkout.load(tl.path).publish('Forged helper', client=env.client)
    assert tuple(env.service.store.conn.iterdump()) == before
    assert env.client.timelines.history(env.project, 't', limit=200).data == history
