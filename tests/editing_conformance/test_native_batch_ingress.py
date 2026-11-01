"""Pinned CLI/SDK batches through the official pair and real Runtime HTTP handlers.

The generated client's injected transport replaces sockets only. The profile,
provider, schemas, credentials, routing and durable Runtime are unmodified.
"""
from __future__ import annotations

import copy
import io
import json
from http.client import HTTPResponse
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.sync import CommitConflict, TimelineSession
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.helpers.runtime import new_runtime_daemon, use_runtime, vendored_runtime_commit

pytestmark = pytest.mark.integration


class MemorySocket:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, timeout):
        pass


@pytest.fixture
def runtime(tmp_path, monkeypatch, request):
    use_runtime(vendored_runtime_commit())
    from runtime_protocol.server import RuntimeHandler

    from astrid.sdk import workspace_client
    from astrid.sdk.client import AstridClient
    from astrid_runtime_client.generated import WorkspaceClient

    film = None
    root = tmp_path / 'realm'
    if getattr(request, 'param', None) == 'historical':
        root, film = request.getfixturevalue('historical_sheet_film')
    world = SimpleNamespace(calls=[], lost=set(), reads=[])
    support = tmp_path / 'support'
    daemon = new_runtime_daemon(root, commit=vendored_runtime_commit(),
                                support_root=support, authoring_profile='astrid').start()
    server = daemon.httpd
    token = daemon.token

    def restart():
        nonlocal daemon, server
        daemon.stop()
        daemon = new_runtime_daemon(root, commit=vendored_runtime_commit(),
                                    support_root=support, authoring_profile='astrid').start()
        server = daemon.httpd

    def transport(method, path, headers, body):
        body = body or b''
        request = (f'{method} {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n'
                   f'Content-Length: {len(body)}\r\n' +
                   ''.join(f'{key}: {value}\r\n' for key, value in headers.items()) + '\r\n').encode() + body
        sock = MemorySocket(request)
        RuntimeHandler(sock, ('127.0.0.1', 1), server)
        response = HTTPResponse(MemorySocket(bytes(sock.output)))
        response.begin()
        data = response.read()
        if path.endswith('/commit'):
            key = headers['Idempotency-Key']
            world.calls.append((body, key, response.status, json.loads(data)))
            if response.status == 200 and key in world.lost:
                world.lost.remove(key)
                raise URLError('lost ACK after Runtime acceptance')
        if '/composition-revisions/' in path and method == 'GET':
            world.reads.append(path)
        return response.status, dict(response.headers), data

    monkeypatch.setattr(
        workspace_client,
        'GeneratedWorkspaceClient',
        lambda endpoint, credential: (
            WorkspaceClient(endpoint, credential, transport=transport)
            if endpoint == 'http://127.0.0.1:1'
            else WorkspaceClient(endpoint, credential)
        ),
    )
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client'))
    restart()
    world.project = film if film else server.runtime.create_project({'slug': 'batch', 'name': 'Batch'})['id']
    if film is None:
        server.runtime.create_timeline(world.project, 'film', idempotency_key='bootstrap')

    def client(*, real_http=False):
        endpoint = daemon.endpoint if real_http else 'http://127.0.0.1:1'
        return AstridClient.open(endpoint=endpoint, credential=token,
                                 realm_id=server.runtime.realm['id'], actor_id='owner',
                                 client_name='batch-proof', client_version='1', protocol_version='workspace.v1')

    def session(*, name='outbox', base=None):
        return TimelineSession.open(world.project, 'film', client=client(),
                                    outbox_path=tmp_path / (name + '.json'), **(base or {}))

    world.client, world.session, world.restart = client, session, restart
    world.service = lambda: server.runtime
    world.history = lambda: server.runtime.list_timeline_commits(world.project, 'film', authorize=lambda: None)
    # Read without adopting a historical head into command history before its seed.
    resource = server.runtime.get_project_timeline(world.project, 'film')
    world.base = {'base_revision_id': resource['head_revision_id'],
                  'base_publication_id': resource['head_publication_id']}
    world.path = tmp_path / 'outbox.json'
    try:
        yield world
    finally:
        daemon.stop()


def command(op, select, args, **metadata):
    return {'schema_version': 1, 'op': op, 'version': '1', 'select': select, 'args': args, **metadata}


def label(value, track='picture'):
    return command('track.set', 'parent/track:' + track, {'values': {'label': value}, 'clear': []})


def batch():
    return [command('track.create', 'parent', {'row': {'id': 'picture', 'kind': 'visual', 'opaque': True}}),
            label('Picture'),
            command('clip.add', 'parent', {'row': {'id': 'title', 'clipType': 'text', 'track': 'picture',
                                                  'at': .017, 'hold': 1.013, 'params': {'text': 'First'}}}),
            command('clip.set-params', 'parent/clip:title',
                    {'values': {'text': 'Edited', 'nullable': None}, 'clear': []}, rationale='Opening title'),
            command('clip.add', 'parent', {'row': {'id': 'temporary', 'clipType': 'text', 'track': 'picture',
                                                  'at': 2, 'hold': 1, 'params': {'text': 'Remove'}}}),
            command('clip.delete', 'parent/clip:temporary', {})]


def cli(runtime, capsys, path, *args, base=None):
    """Submit the file's batch through the current Runtime-backed commit API.

    ``edit commands-file`` was retired by G5. Keep the original file input
    and CLI provenance while routing the batch through the public session
    caller, which uses Runtime's canonical ``commitTimeline`` door.
    """
    pins = base or runtime.base
    idempotency_key = None
    for index, value in enumerate(args):
        if value == '--idempotency-key' and index + 1 < len(args):
            idempotency_key = args[index + 1]
            break
    dry_run = '--dry-run' in args
    try:
        commands = json.loads(path.read_bytes())
        accepted = runtime.session(base=pins).commit_many(
            commands, idempotency_key=idempotency_key, dry_run=dry_run, via='cli'
        )
        payload = {'ok': True, **accepted}
        print(json.dumps(payload))
        return 0, payload
    except (TimelineEditError, WorkspaceClientError, CommitConflict, URLError,
            OSError, TypeError, ValueError, KeyError) as error:
        failure = getattr(error, 'failure', None)
        if isinstance(failure, dict):
            payload = {'ok': False, 'error': {
                'code': failure.get('code', 'timeline_commit_error'),
                'message': failure.get('message', str(error)),
                'details': failure.get('details', {}),
            }}
        else:
            payload = {'ok': False, 'error': {
                'code': getattr(error, 'code', 'timeline_commit_error'),
                'message': str(getattr(error, 'message', error)),
                'details': dict(getattr(error, 'details', {}) or {}),
            }}
        print(json.dumps(payload))
        return 1, payload


@pytest.mark.parametrize('captured', [False, True])
@pytest.mark.parametrize('entry', ['queue', 'commit', 'cli'])
def test_batch_original_claims_one_publication_reload_inverse(runtime, tmp_path, capsys, captured, entry):
    original = runtime.client().timelines.open_bundle(runtime.project, 'film',
                                                     revision_id=runtime.base['base_revision_id']).data['bundle']
    plan = execute(original, batch())
    supplied = plan.commands if captured else batch()
    frozen = copy.deepcopy(supplied)
    if entry == 'cli':
        path = tmp_path / 'commands.json'
        path.write_text(json.dumps(supplied))
        before = path.read_bytes()
        code, accepted = cli(runtime, capsys, path)
        assert code == 0, accepted
        assert path.read_bytes() == before
    else:
        session = runtime.session(base=runtime.base)
        if entry == 'queue':
            assert session.queue_many(supplied) == plan.commands
            accepted = session.flush()
        else:
            accepted = session.commit_many(supplied)
    assert supplied == frozen
    assert len(runtime.history()['items']) == 2
    assert accepted['data']['commands'] == plan.commands
    after = bundle_from_document(accepted['data']['canonical_document'])
    assert State(after).entity('parent/clip:title')['params'] == {'text': 'Edited', 'nullable': None}
    assert State(after).entity('parent/clip:temporary', optional=True) is None
    assert accepted['data']['commands'][3]['rationale'] == 'Opening title'
    assert json.loads(runtime.calls[-1][0])['source'] == {'via': 'cli' if entry == 'cli' else 'python'}
    runtime.restart()
    session = runtime.session(name='reloaded')
    stored = session.client.timelines.open_bundle(runtime.project, 'film',
                                                  revision_id=accepted['data']['accepted_revision_id'])
    expected = copy.deepcopy(after)
    expected['source_mapping'].pop('_runtime_document')  # Provider-only immutable closure annotation.
    assert stored.ok and stored.data['bundle'] == expected
    undone = session.undo([accepted['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    restored['source_mapping'].pop('_runtime_document')
    assert restored == original
    redone = session.undo([undone['receipt']['receipt_id']])
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']


@pytest.mark.parametrize('captured', [False, True])
@pytest.mark.parametrize('peer', ['disjoint', 'relevant', 'aba', 'delete-recreate'])
def test_file_lost_ack_restart_exact_retry_before_original_suffix(runtime, tmp_path, capsys, captured, peer):
    runtime.session().commit_many(batch())
    base = runtime.history()['head']
    pins = {'base_revision_id': base['revision_id'], 'base_publication_id': base['publication_id']}
    commands = [command('track.create', 'parent', {'row': {'id': 'dependent', 'kind': 'visual'}}),
                label('A'), command('clip.set-params', 'parent/clip:title',
                                  {'values': {'text': 'A'}, 'clear': []}, rationale='Batch A')]
    original = runtime.client().timelines.open_bundle(runtime.project, 'film', revision_id=base['revision_id'])
    expected = execute(original.data['bundle'], commands).commands
    path = tmp_path / 'commands.json'
    path.write_text(json.dumps(expected if captured else commands))
    file_bytes = path.read_bytes()
    runtime.lost.add('batch-A')
    code, failure = cli(runtime, capsys, path, '--idempotency-key', 'batch-A', base=pins)
    assert code == 1 and not failure['ok']
    original_call = copy.deepcopy(runtime.calls[-1])
    session = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    suffix = session.queue_many([label('B'), label('B', track='dependent'),
                                command('clip.set-params', 'parent/clip:title',
                                                   {'values': {'text': 'B'}, 'clear': []})])
    other = runtime.session(name='peer')
    if peer == 'disjoint':
        other.commit(command('track.create', 'parent', {'row': {'id': 'peer', 'kind': 'audio'}}))
    elif peer == 'delete-recreate':
        deleted = other.commit(command('clip.delete', 'parent/clip:title', {}))
        other.undo([deleted['receipt']['receipt_id']])
    else:
        other.commit(label('Peer'))
        if peer == 'aba':
            other.commit(label('A'))
    count = len(runtime.history()['items'])
    runtime.restart()
    reads = len(runtime.reads)
    code, retry = cli(runtime, capsys, path, base=pins)
    assert code == 0, retry
    assert retry['receipt'] == original_call[3]['receipt']
    assert runtime.calls[-1][:2] == original_call[:2]
    assert len(runtime.reads) == reads  # No recapture or inferred new head.
    assert len(runtime.history()['items']) == count
    assert path.read_bytes() == file_bytes
    session = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    if peer in ('relevant', 'delete-recreate'):
        with pytest.raises(CommitConflict):
            session.flush()
        assert json.loads(session.outbox.path.read_bytes())['pending'] == suffix
    else:
        accepted = session.flush()
        assert accepted['data']['commands'] == suffix
        assert accepted['data']['status'] == 'rebased'
        assert State(bundle_from_document(accepted['data']['canonical_document'])).entity(
            'parent/track:picture')['label'] == 'B'
        assert State(bundle_from_document(accepted['data']['canonical_document'])).entity(
            'parent/track:dependent')['label'] == 'B'
    request = json.loads(runtime.calls[-1][0])
    assert request['commands'] == suffix
    assert request['base_revision_id'] == retry['data']['accepted_revision_id']
    assert request['base_publication_id'] == retry['receipt']['receipt_id']
    assert runtime.calls[-1][1] != 'batch-A'


@pytest.mark.parametrize('aba', [False, True])
def test_guarded_undo_still_refuses_intervening_equal_state_writes(runtime, aba):
    runtime.session().commit_many(batch())
    target = runtime.session().commit(label('A'))
    peer = runtime.session(name='peer')
    if aba:
        peer.commit(label('Away'))
    peer.commit(label('A'))
    before = copy.deepcopy(runtime.history())
    with pytest.raises(CommitConflict) as error:
        runtime.session().undo([target['receipt']['receipt_id']])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert runtime.history() == before


@pytest.mark.parametrize('captured', [False, True])
@pytest.mark.parametrize('peer', ['disjoint', 'relevant'])
def test_file_uses_original_pair_after_peer_advances_head(runtime, tmp_path, capsys, captured, peer):
    runtime.session().commit_many(batch())
    head = runtime.history()['head']
    pins = {'base_revision_id': head['revision_id'], 'base_publication_id': head['publication_id']}
    bundle = runtime.client().timelines.open_bundle(runtime.project, 'film', revision_id=head['revision_id']).data['bundle']
    raw = [label('File'), command('clip.set-params', 'parent/clip:title',
                                {'values': {'text': 'File'}, 'clear': []}, rationale='Original file')]
    expected = execute(bundle, raw).commands
    path = tmp_path / 'commands.json'
    path.write_text(json.dumps(expected if captured else raw))
    frozen = path.read_bytes()
    peer_command = label('Peer') if peer == 'relevant' else command(
        'track.create', 'parent', {'row': {'id': 'peer', 'kind': 'audio'}})
    runtime.session(name='peer').commit(peer_command)
    before = copy.deepcopy(runtime.history())
    code, result = cli(runtime, capsys, path, base=pins)
    sent = json.loads(runtime.calls[-1][0])
    assert sent['commands'] == expected
    assert {key: sent[key] for key in pins} == pins
    assert path.read_bytes() == frozen
    if peer == 'relevant':
        assert code == 1 and not result['ok']
        assert runtime.history() == before
        session = TimelineSession.open(runtime.project, 'film', client=runtime.client())
        pending = json.loads(session.outbox.path.read_bytes())
        assert pending['pending'] == expected and pending['flight'] is not None
    else:
        assert code == 0 and result['data']['status'] == 'rebased'
        assert len(runtime.history()['items']) == len(before['items']) + 1
        assert State(bundle_from_document(result['data']['canonical_document'])).entity(
            'parent/track:peer')['kind'] == 'audio'


@pytest.mark.parametrize('changed', [1, 1.0, False])
@pytest.mark.parametrize('nested', [False, True])
def test_batch_retry_distinguishes_boolean_capture_and_intent(runtime, changed, nested):
    calls = batch()[:2]
    row = calls[0]['args']['row']
    row['opaque'] = {'items': [True]} if nested else True
    runtime.lost.add('boolean')
    with pytest.raises(WorkspaceClientError):
        runtime.session(base=runtime.base).commit_many(calls, idempotency_key='boolean')
    original = runtime.path.read_bytes()
    count, reads = len(runtime.calls), len(runtime.reads)
    altered = copy.deepcopy(calls)
    altered[0]['args']['row']['opaque'] = {'items': [changed]} if nested else changed
    runtime.restart()
    with pytest.raises(TimelineEditError, match='pending intent differs'):
        runtime.session(base=runtime.base).commit_many(altered)
    assert runtime.path.read_bytes() == original and len(runtime.calls) == count
    assert len(runtime.reads) == reads
    accepted = runtime.session(base=runtime.base).commit_many(calls)
    assert accepted['receipt'] == runtime.calls[-2][3]['receipt']
    assert runtime.calls[-1][:2] == runtime.calls[-2][:2]


def test_batch_retry_numeric_equivalence_sends_original_bytes(runtime):
    calls = batch()[:2]
    calls[0]['args']['row']['opaque'] = 2
    runtime.lost.add('numeric')
    with pytest.raises(WorkspaceClientError):
        runtime.session().commit_many(calls, idempotency_key='numeric')
    original = runtime.calls[-1]
    calls[0]['args']['row']['opaque'] = 2.0
    runtime.session().commit_many(calls)
    assert runtime.calls[-1][:2] == original[:2]
    assert b'"opaque":2}' in runtime.calls[-1][0]


@pytest.mark.parametrize('damage', ['claims', 'late-invalid', 'media', 'empty', 'envelope'])
def test_file_refusal_is_atomic_and_keeps_file(runtime, tmp_path, capsys, damage):
    raw = batch()
    if damage == 'claims':
        bundle = runtime.client().timelines.open_bundle(runtime.project, 'film',
                                                        revision_id=runtime.base['base_revision_id']).data['bundle']
        raw = execute(bundle, raw).commands
        raw[1]['expect']['parent/track:picture::label'] = {'state': 'present', 'value': 'Stale'}
    elif damage == 'late-invalid':
        raw[-1]['select'] = 'parent/clip:missing'
    elif damage == 'media':
        raw.append(command('occurrence.add-media', 'parent', {}))
    elif damage == 'empty':
        raw = []
    else:
        raw = {'commands': raw, **runtime.base}
    path = tmp_path / 'commands.json'
    path.write_text(json.dumps(raw))
    frozen = path.read_bytes()
    before = copy.deepcopy(runtime.history())
    code, failure = cli(runtime, capsys, path)
    assert code == 1 and not failure['ok']
    assert failure['error']['message']
    assert path.read_bytes() == frozen
    assert runtime.history() == before and runtime.calls == []
    session = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    state = json.loads(session.outbox.path.read_bytes())
    assert state['pending'] == [] and state['flight'] is None


@pytest.mark.parametrize('missing', ['base_revision_id', 'base_publication_id'])
def test_sdk_requires_both_explicit_pins(runtime, missing):
    pins = dict(runtime.base)
    pins.pop(missing)
    with pytest.raises(TimelineEditError, match='no verified publication'):
        runtime.session(base=pins)
    assert not runtime.path.exists() and not runtime.calls


def test_explicit_pin_mismatch_cannot_rebase_pending_intent_locally(runtime):
    runtime.session(base=runtime.base).queue_many(batch())
    original = runtime.path.read_bytes()
    pins = {**runtime.base, 'base_revision_id': 'different'}
    with pytest.raises(TimelineEditError, match='different original base'):
        runtime.session(base=pins)
    assert runtime.path.read_bytes() == original and not runtime.calls


def test_file_parser_requires_both_original_pins_and_describes_registry(runtime, capsys):
    from astrid.packs.timeline.cli import build_parser
    from astrid.sdk.timeline.commands import operations

    parser = build_parser(runtime.client())
    for pins in [[], ['--base-revision-id', 'r'], ['--base-publication-id', 'p']]:
        with pytest.raises(SystemExit) as error:
            parser.parse_args(['edit', 'commands-file', 'film', 'commands.json', '--project', runtime.project, *pins])
        assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        parser.parse_args(['edit', 'commands-file', '--help'])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert all(op in help_text for op in operations())
    assert '--base-revision-id' in help_text and '--base-publication-id' in help_text
    assert runtime.calls == []


@pytest.mark.parametrize('field', ['reads', 'expect'])
def test_captured_file_retry_refuses_boolean_to_number_claim(runtime, tmp_path, capsys, field):
    initial = batch()
    initial[2]['args']['row']['enabled'] = True
    runtime.session().commit_many(initial)
    runtime.session().move('parent/clip:title', 1)
    head = runtime.history()['head']
    pins = {'base_revision_id': head['revision_id'], 'base_publication_id': head['publication_id']}
    bundle = runtime.client().timelines.open_bundle(runtime.project, 'film', revision_id=head['revision_id']).data['bundle']
    calls = execute(bundle, [command('clip.move', 'parent/clip:title', {'on': 2}), label('Moved')]).commands
    path = tmp_path / 'commands.json'
    path.write_text(json.dumps(calls))
    runtime.lost.add('claims')
    assert cli(runtime, capsys, path, '--idempotency-key', 'claims', base=pins)[0] == 1
    original = copy.deepcopy(runtime.calls[-1])
    altered = copy.deepcopy(calls)
    if field == 'reads':
        claim = next(read['state'] for read in altered[0]['reads']
                     if read['ref'] == 'parent/clip:title' and read['path'] == ['enabled'])
    else:
        claim = altered[0]['expect']['parent/clip:title::app.astrid.fixed_start']
    assert claim['value'] is True
    claim['value'] = 1
    path.write_text(json.dumps(altered))
    session = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    persisted = session.outbox.path.read_bytes()
    reads, count = len(runtime.reads), len(runtime.calls)
    code, failure = cli(runtime, capsys, path, base=pins)
    assert code == 1 and 'pending intent differs' in failure['error']['message']
    assert session.outbox.path.read_bytes() == persisted
    assert len(runtime.reads) == reads and len(runtime.calls) == count
    path.write_text(json.dumps(calls))
    runtime.restart()
    assert cli(runtime, capsys, path, base=pins)[0] == 0
    assert runtime.calls[-1][:2] == original[:2]


def test_file_preview_keeps_batch_for_one_real_publication(runtime, tmp_path, capsys):
    path = tmp_path / 'commands.json'
    path.write_text(json.dumps(batch()))
    before = copy.deepcopy(runtime.history())
    code, preview = cli(runtime, capsys, path, '--dry-run')
    assert code == 0 and preview['data']['status'] == 'preview' and preview['receipt'] is None
    assert runtime.history() == before
    code, accepted = cli(runtime, capsys, path)
    assert code == 0 and len(runtime.history()['items']) == len(before['items']) + 1
    assert accepted['data']['commands'] == preview['data']['commands']
    assert runtime.calls[-1][1] != runtime.calls[-2][1]


def test_batch_capture_never_refreshes_after_concurrent_ack(runtime, monkeypatch):
    session = runtime.session(base=runtime.base)
    session.queue_many(batch())
    snapshots = []
    original_bundle = session._bundle

    def acknowledged_during_read(state):
        snapshots.append(copy.deepcopy(state['base']))
        bundle = original_bundle(state)
        session.flush()  # Completes while capture is outside the outbox lock.
        return bundle

    monkeypatch.setattr(session, '_bundle', acknowledged_during_read)
    with pytest.raises(TimelineEditError, match='outbox changed during capture'):
        session.queue_many([label('Suffix')])
    assert snapshots == [{'revision_id': runtime.base['base_revision_id'],
                          'publication_id': runtime.base['base_publication_id']}]
    state = json.loads(runtime.path.read_bytes())
    assert state['pending'] == [] and state['flight'] is None
    assert state['base'] == runtime.history()['head']
    assert len(runtime.history()['items']) == 2
