"""Detached helper capture survives restart and is checked by the real provider."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline import journal
from astrid.sdk.timeline import open as open_timeline
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commands import CommandError, execute
from astrid.sdk.timeline.native_word_anchor import capture_anchor
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk import test_timeline_restructure

restructure_runtime = test_timeline_restructure.restructure_runtime


def draft(env, tmp_path, monkeypatch):
    monkeypatch.setattr('astrid.sdk.timeline_checkout.drafts_root', lambda: tmp_path)
    tl = Checkout.open(env.project, 't', client=env.client)
    tl.save(tmp_path / 'helper.json', quiet=True)
    return tl


def peer_edit(env, clip='p2'):
    peer = open_timeline(env.project, 't', client=env.client)
    peer.clip(clip).data['opaque'] = {'peer': True}
    peer.commit('Peer', idempotency_key='peer')


def _genuine_anchor_capture(env):
    """Build one exact native anchor from the Runtime-owned inline-word source."""
    from runtime_protocol.timeline_commit_inputs import content_digest

    from astrid.sdk.media_transcript import build_transcript, transcript_document_id

    tl = open_timeline(env.project, 't', client=env.client)
    source_bundle = copy.deepcopy(tl.bundle)
    internal = source_bundle['shots']['film']['internal_timeline']
    source = next(row for row in internal['clips'] if row['id'] == 'vo2')
    media_id = internal['registry']['assets'][source['asset']]['media_id']
    content = build_transcript(media_id, 's2', 'second', [[0.1, 0.4, 'second']])
    document_id = transcript_document_id(env.project, media_id, content['source_id'])
    created = env.client.documents.create(document_id=document_id, kind='media.transcript', content=content,
                                          project=env.project)
    assert created.ok
    document = env.service.get_document(env.project, document_id)
    pin = {'document_id': document_id, 'version': document['version'],
           'content_digest': content_digest(document['content'])}
    captured = {'pin': pin, 'document': copy.deepcopy(document)}
    source_bundle['source_mapping']['pinned_documents'] = [captured]

    carrier = {'id': 'anchor-carrier', 'clipType': 'am-type', 'track': 'picture',
               'at': 4.6, 'hold': 1.0, 'params': {}}
    add = capture_clip_add(source_bundle, 'parent', carrier)
    source_bundle = execute(source_bundle, [add]).bundle
    word = document['content']['segments'][0]['words'][0]
    choice = {'source': 'occurrence:occ-film/clip:vo2', 'document': pin,
              'word_ids': [word['id']], 'edge': 'start', 'offset_s': 0, 'offset_frames': 0}
    anchor = capture_anchor(source_bundle, 'parent/clip:anchor-carrier', 'on', '"second"', choice)
    plan = execute(source_bundle, [anchor])
    return source_bundle, pin, choice, anchor, plan.records[0]


def test_genuine_anchor_capture_has_exact_source_scope_and_base_pair(restructure_runtime):
    env = restructure_runtime
    bundle, pin, choice, command, record = _genuine_anchor_capture(env)
    runtime_document = env.document()
    assert {'project_id', 'timeline_id', 'parent_revision', 'dependency_manifest'} <= set(runtime_document)
    assert (runtime_document['project_id'], runtime_document['timeline_id']) == (env.project, 't')
    assert bundle['base_parent']['publication_id']
    assert bundle['base_parent']['revision_id'] == runtime_document['parent_revision']['revision_id']
    assert bundle['source_mapping']['pinned_documents'][0]['pin'] == pin
    assert bundle['source_mapping']['pinned_documents'][0]['document']['project_id'] == env.project
    assert command['op'] == 'clip.anchor'
    assert command['args']['choice'] == choice
    assert any(dep['key'] == 'anchor.transcript' and dep['pin'] == pin
               for dep in record['dependencies'])
    assert command['bindings']['anchor.source'] == choice['source']


@pytest.mark.parametrize('damage', ['missing-pin', 'forged-word'])
def test_missing_or_forged_anchor_binding_never_writes(restructure_runtime, damage):
    env = restructure_runtime
    bundle, pin, choice, _command, _record = _genuine_anchor_capture(env)
    if damage == 'missing-pin':
        bundle['source_mapping']['pinned_documents'] = []
    else:
        choice = copy.deepcopy(choice)
        choice['word_ids'] = ['word:' + '0' * 64]
    before = tuple(env.service.store.conn.iterdump())
    with pytest.raises((CommandError, TimelineEditError), match='(exact transcript pin|stable word)'):
        capture_anchor(bundle, 'parent/clip:anchor-carrier', 'on', '"second"', choice)
    assert tuple(env.service.store.conn.iterdump()) == before


def test_nudge_restart_merges_peer_other_clip(restructure_runtime, tmp_path, monkeypatch):
    env = restructure_runtime
    original_open = env.client.timelines.open_bundle
    opens = []

    def open_bundle(*args, **kwargs):
        result = original_open(*args, **kwargs)
        opens.append(copy.deepcopy(kwargs))
        if 'publication_id' not in kwargs:
            result.data['bundle']['source_mapping'].pop('_runtime_document', None)
        return result

    env.client.timelines.open_bundle = open_bundle
    tl = draft(env, tmp_path, monkeypatch)
    base = copy.deepcopy(tl.bundle['base_parent'])
    start = tl.clip('cross').start
    tl.clip('cross').nudge(0.5)
    tl.save(quiet=True)
    tl = Checkout.load(tl.path)
    assert tl.bundle['base_parent'] == base
    peer_edit(env)
    result = tl.publish('Nudge', client=env.client, idempotency_key='nudge')
    assert result['receipt']['result']['status'] == 'merged'
    body = env.calls[-2][0]
    assert body['merge'] == 'fields' and body['steps'][0]['reads']
    assert body['base_publication_id'] == base['publication_id']
    loaded = Checkout.open(env.project, 't', client=env.client)
    assert loaded.clip('cross').start == pytest.approx(start + 0.5)
    assert loaded.clip('p2').data['opaque'] == {'peer': True}
    assert any(row.get('publication_id') == base['publication_id']
               and row.get('revision_id') == base['revision_id'] for row in opens)


@pytest.mark.parametrize('mode', ['same', 'unknown', 'mixed', 'gap', 'old'])
def test_conflicts_and_uncaptured_edits_never_write(restructure_runtime, tmp_path, monkeypatch, mode):
    env = restructure_runtime
    tl = draft(env, tmp_path, monkeypatch)
    if mode not in {'unknown', 'old'}:
        tl.clip('cross').nudge(0.5)
    if mode in {'unknown', 'mixed', 'gap', 'old'}:
        tl.clip('cross').data['opaque'] = {'local': True}
    if mode == 'gap':
        tl.clip('cross').nudge(0.5)
    tl.save(quiet=True)
    tl = Checkout.load(tl.path)
    peer_edit(env, 'cross' if mode == 'same' else 'p2')
    before = tuple(env.service.store.conn.iterdump())
    with pytest.raises((TimelineEditError, WorkspaceClientError)):
        tl.publish('Refused', client=env.client, idempotency_key='refused')
    assert tuple(env.service.store.conn.iterdump()) == before
    assert env.calls[-1][0]['merge'] == ('fields' if mode == 'same' else 'atomic')


@pytest.mark.parametrize('damage', ['hash', 'commands', 'reads', 'after'])
def test_journal_tamper_and_forged_capture_refuse(restructure_runtime, tmp_path, monkeypatch, damage):
    env = restructure_runtime
    tl = draft(env, tmp_path, monkeypatch)
    tl.clip('cross').nudge(0.5)
    tl.save(quiet=True)
    path = journal.journal_path(env.project, 't')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    row = rows[-1]
    if damage == 'hash':
        row['label'] = 'tampered'
    else:
        call = row['calls'][0]
        if damage == 'commands':
            call['commands'][0]['args']['changes'][0]['value'] = 9
        elif damage == 'reads':
            call['reads'] = []
        else:
            call['after'] = 'sha256:' + '0' * 64
        row['hash'] = journal._hash(row['prev'], {k: v for k, v in row.items() if k != 'hash'})
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    before = tuple(env.service.store.conn.iterdump())
    with pytest.raises(TimelineEditError, match='helper'):
        Checkout.load(tl.path).publish('Tamper', client=env.client)
    assert tuple(env.service.store.conn.iterdump()) == before


def test_helper_lost_ack_retries_exact_request(restructure_runtime, tmp_path, monkeypatch):
    env = restructure_runtime
    tl = draft(env, tmp_path, monkeypatch)
    tl.clip('cross').nudge(0.5)
    tl.save(quiet=True)
    lost = False

    def send(*args, **kwargs):
        nonlocal lost
        result = env.send(*args, **kwargs)
        if not lost:
            lost = True
            raise WorkspaceClientError(0, 'unavailable', 'lost ACK')
        return result

    env.generated.commit_timeline = send
    with pytest.raises(WorkspaceClientError):
        tl.publish('Retry helper', client=env.client, idempotency_key='helper-retry')
    Checkout.load(tl.path).publish('Retry helper', client=env.client, idempotency_key='helper-retry')
    assert env.calls[0] == env.calls[1]
    assert env.replies[0] == env.replies[1]


@pytest.mark.parametrize('moved_head', [False, True])
def test_contiguous_helpers_keep_sequential_steps(restructure_runtime, tmp_path, monkeypatch, moved_head):
    env = restructure_runtime
    tl = draft(env, tmp_path, monkeypatch)
    start = tl.clip('cross').start
    tl.clip('cross').nudge(0.5)
    tl.clip('cross').nudge(frames=3)
    tl.save(quiet=True)
    if moved_head:
        peer_edit(env)
    result = Checkout.load(tl.path).publish('Two nudges', client=env.client)
    assert result['receipt']['result']['status'] == ('merged' if moved_head else 'accepted')
    body = next(body for body, _ in env.calls if body['label'] == 'Two nudges')
    assert [step['command_ordinals'] for step in body['steps']] == [[0], [1]]
    assert Checkout.open(env.project, 't', client=env.client).clip('cross').start == pytest.approx(start + 0.6)


@pytest.mark.parametrize('anchor', [None, '+1s', '"second"'])
def test_cut_nudge_restart_merges_other_cut(restructure_runtime, tmp_path, monkeypatch, anchor):
    env = restructure_runtime
    setup = open_timeline(env.project, 't', client=env.client)
    clip = setup.clip('inside')
    clip.data['app'] = {'astrid': {'cut': 'c2', 'layer': 'sprite'}}
    if anchor:
        clip.data['app']['astrid']['on'] = anchor
    if anchor == '"second"':
        clip.data['at'] = 4.6
    setup.commit('Cut setup', idempotency_key='cut-setup')
    tl = draft(env, tmp_path, monkeypatch)
    start = tl.clip('inside').start
    tl.clip('inside').nudge(0.5)
    tl.save(quiet=True)
    peer = open_timeline(env.project, 't', client=env.client)
    peer.clip('late').data['params']['text'] = 'Peer text'
    peer.commit('Peer cut', idempotency_key='peer-cut')
    result = Checkout.load(tl.path).publish('Cut nudge', client=env.client, idempotency_key='cut-nudge')
    assert result['receipt']['result']['status'] == 'merged'
    body = next(body for body, _ in env.calls if body['label'] == 'Cut nudge')
    assert body['merge'] == 'fields'
    assert any(read['kind'] == 'collection' for read in body['steps'][0]['reads'])
    loaded = Checkout.open(env.project, 't', client=env.client)
    assert loaded.clip('inside').start == pytest.approx(start + 0.5)
    assert loaded.clip('late').params['text'] == 'Peer text'


@pytest.mark.parametrize('mode', ['same', 'word-source', 'collection', 'mixed'])
def test_cut_nudge_guarded_inputs_never_write(restructure_runtime, tmp_path, monkeypatch, mode):
    env = restructure_runtime
    setup = open_timeline(env.project, 't', client=env.client)
    setup.clip('inside').data['app'] = {'astrid': {'cut': 'c2', 'layer': 'sprite'}}
    setup.commit('Cut setup', idempotency_key='cut-setup')
    tl = draft(env, tmp_path, monkeypatch)
    tl.clip('inside').nudge(0.5)
    if mode == 'mixed':
        tl.clip('inside').data['opaque'] = 'uncaptured'
    tl.save(quiet=True)
    peer = open_timeline(env.project, 't', client=env.client)
    if mode == 'same':
        peer.clip('inside').data['params']['x'] = 99
    elif mode == 'word-source':
        peer.clip('vo2').data['app']['astrid']['words'][0][2] = 'changed'
    elif mode == 'collection':
        peer.clip('cross').data['id'] = 'new-member'
    else:
        peer.clip('late').data['params']['text'] = 'Peer text'
    peer.commit('Peer dependency', idempotency_key='peer-dependency')
    before = tuple(env.service.store.conn.iterdump())
    with pytest.raises((TimelineEditError, WorkspaceClientError)):
        Checkout.load(tl.path).publish('Refused cut', client=env.client)
    assert tuple(env.service.store.conn.iterdump()) == before
    assert env.calls[-1][0]['merge'] == ('atomic' if mode == 'mixed' else 'fields')


@pytest.mark.parametrize('damage', ['wrong-pair', 'ambiguous'])
def test_original_publication_pair_is_required(restructure_runtime, tmp_path, monkeypatch, damage):
    from astrid.sdk.contracts import DomainResult

    env = restructure_runtime
    tl = draft(env, tmp_path, monkeypatch)
    tl.clip('cross').nudge(0.5)
    if damage == 'wrong-pair':
        tl.bundle['base_parent']['publication_id'] = 'wrong'
    else:
        tl.bundle['base_parent'].pop('publication_id')
        original = env.client.timelines.history

        def history(*args, **kwargs):
            result = copy.deepcopy(original(*args, **kwargs).data)
            matches = [row for row in result['items'] if row['revision_id'] == tl.base_revision]
            if matches:
                row = copy.deepcopy(matches[0])
                row['publication_id'] = 'another-publication'
                result['items'].append(row)
            return DomainResult.success(result)

        env.client.timelines.history = history
    before = tuple(env.service.store.conn.iterdump())
    with pytest.raises(TimelineEditError, match='unique publication base'):
        tl.publish('Bad base', client=env.client)
    assert tuple(env.service.store.conn.iterdump()) == before
