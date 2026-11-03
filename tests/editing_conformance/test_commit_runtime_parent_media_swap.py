"""Native parent replacement with retained, decodable task media and real storage.

Run against a normally generated private S2 Runtime containing clip.swap-media.
No schema patching, synthetic accepted records, sockets or SQL writes.
"""
from __future__ import annotations

import copy
import io
import json
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from tests.editing_conformance.test_commit_runtime_occurrence_media import (
    commit,
    foreign_pin,
    inverse,
    rows,
)

pytestmark = pytest.mark.integration
TARGET = 'parent/clip:clip-a'
LEGACY_WRITE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})


def legacy_composition_writes(records):
    """Return product-phase legacy composition/publish writes exactly as recorded."""
    return [
        record for record in records
        if record[2] == 'product'
        and record[0] in LEGACY_WRITE_METHODS
        and (record[1].endswith('/composition-revisions')
             or record[1].endswith('/publish'))
    ]


@pytest.fixture
def operation():
    return 'swap'


@pytest.fixture
def media_owner():
    return 'parent'


@pytest.fixture
def source_bytes(tmp_path):
    def source(kind, name):
        color = 'red' if name == 'old' else 'blue'
        if kind == 'image':
            output = io.BytesIO()
            Image.new('RGB', (16, 16), color).save(output, format='PNG')
            return output.getvalue()
        output = tmp_path / (name + '.mp4')
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        f'color=c={color}:s=16x16:r=1:d=6', '-c:v', 'mpeg4', str(output)],
                       check=True, capture_output=True)
        return output.read_bytes()
    return source


@pytest.fixture(params=['image', 'video'])
def editing(request, tmp_path, monkeypatch, operation, media_owner, source_bytes):
    from tests.editing_conformance.test_commit_runtime_occurrence_media import (
        editing as occurrence_editing,
    )

    # The current fixture performs the real empty bootstrap, media admission,
    # native capture and provider acceptance in this process.  Keep the
    # parent-swap scenarios unchanged while removing the retired historical
    # publisher and cross-interpreter seed.
    fixture = occurrence_editing.__wrapped__(
        SimpleNamespace(param=request.param), tmp_path, monkeypatch, operation, media_owner,
        source_bytes,
    )
    try:
        yield next(fixture)
    finally:
        fixture.close()


def body(editing, commands=None, *, dry_run=False):
    commands = commands or [command(editing)]
    head = editing.service.get_project_timeline(editing.project, 'film')
    label = 'Parent media swap'
    changes = [change for call in commands if call['op'] == 'doc.patch'
               for change in call['args']['changes']]
    step = {'id': 'parent-media-swap', 'label': label, 'intent': 'Parent media swap',
            'tags': ['parent-media'], 'command_ordinals': list(range(len(commands))),
            'patch': changes, 'reads': []}
    return {'api_version': 1, 'project_id': editing.project, 'timeline_id': 'film',
            'base_revision_id': head['head_revision_id'],
            'base_publication_id': head['head_publication_id'], 'commands': commands,
            'label': label, 'steps': [step], 'merge': 'fields', 'reads': [],
            'dry_run': dry_run, 'source': {'via': 'python'},
            'rationale': 'Replace one parent media selection.',
            'group_id': 'D86-parent-media-swap'}


@pytest.fixture(params=['parent', 'occurrence:occ-a'])
def http_editing(request, tmp_path, monkeypatch, source_bytes):
    """Use one normal image fixture for the bounded parent/occurrence HTTP proof."""
    from tests.editing_conformance.test_commit_runtime_occurrence_media import (
        editing as occurrence_editing,
    )

    fixture = occurrence_editing.__wrapped__(
        SimpleNamespace(param='image'), tmp_path, monkeypatch, 'swap', request.param,
        source_bytes,
    )
    try:
        state = next(fixture)
        # Preserve the raw owner binding used to construct this exact fixture.
        state.owner = request.param
        yield state
    finally:
        fixture.close()


def command(editing):
    return {'schema_version': 1, 'op': 'clip.swap-media', 'version': '1', 'select': TARGET,
            'args': {'old_media': editing.old, 'media': editing.new},
            'intent': {'ownership': 'parent'}}


def test_selected_swap_capture_accepted_reload_restart_retry_undo_and_redo(editing, monkeypatch, real_runtime):
    import runtime_protocol.service as service_module
    import runtime_protocol.visual_seam as visual_seam
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline import commands
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_media_commands import (
        capture_parent_swap_media,
    )

    def renderer_unavailable(*args, **kwargs):
        pytest.fail('media replacement called the renderer')

    monkeypatch.setattr(service_module, 'evaluate_closure', renderer_unavailable)
    monkeypatch.setattr(visual_seam, 'evaluate_closure', renderer_unavailable)
    before = rows(editing)
    preview = commit(editing, body(editing, [command(editing)], dry_run=True), 'preview')
    assert preview['receipt'] is None and rows(editing) == before
    baseline = editing.closures[0]
    original = bundle_from_document(baseline)
    captured = capture_parent_swap_media(original, TARGET, editing.old, editing.new,
                                         verified_media=editing.selected)
    assert preview['data']['commands'] == [captured]
    request = body(editing, [captured])
    created = commit(editing, request, 'selected-swap')
    output = created['data']['canonical_document']
    assert output == preview['data']['canonical_document']
    expected = copy.deepcopy(State(original).entity(TARGET))
    expected['asset'] = editing.new['asset']
    assert State(bundle_from_document(output)).entity(TARGET) == expected
    assert output['shot_revisions'] == baseline['shot_revisions']
    assert output['internal_timeline_revisions'] == baseline['internal_timeline_revisions']
    assert output['parent_revision']['payload']['occurrences'] == baseline['parent_revision']['payload']['occurrences']
    assert editing.new['object_id'] not in {pin['media_id'] for pin in baseline['dependency_manifest']['media']}
    assert editing.new['object_id'] in {pin['media_id'] for pin in output['dependency_manifest']['media']}
    editing.service.close()
    editing.service = RuntimeService(editing.root)
    editing.service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
    stored = editing.service.get_project_parent_composition_revision(
        editing.project, 'film', output['parent_revision']['revision_id'])
    for key, value in output['parent_revision'].items():
        assert stored[key] == value
    assert stored['dependency_manifest'] == output['dependency_manifest']
    with monkeypatch.context() as retry:
        retry.setattr(commands, 'execute', lambda *a, **kw: pytest.fail('retry re-executed semantics'))
        assert commit(editing, request, 'selected-swap') == created
    changed = copy.deepcopy(request)
    changed['source'] = {'via': 'cli'}
    with pytest.raises(RuntimeErrorBase) as error:
        commit(editing, changed, 'selected-swap')
    assert error.value.status == 409
    undone = commit(editing, body(editing, [inverse(created)]), 'undo')
    assert undone['data']['canonical_document'] == baseline
    redone = commit(editing, body(editing, [inverse(undone)]), 'redo')
    assert redone['data']['canonical_document'] == output
    assert editing.service.store.integrity_report()['ok']

    # The SDK caller must use the normal supported client transport.  Opening
    # AstridClient performs the real health/handshake exchange; no transport
    # identity or health state is fabricated by this test.
    from astrid.sdk.client import AstridClient

    editing.service.close()
    daemon = real_runtime(editing.root, support_root=editing.root.parent / 'http-support',
                          commit='95bd3c5f', authoring_profile='astrid')
    token, _ = daemon.credentials.provision(
        'media-editor', [
            'handshake',
            'projects:read', 'projects:write', 'objects:read', 'objects:write',
            'tasks:read', 'tasks:write',
        ],
    )
    client = AstridClient.open(
        endpoint=daemon.endpoint, credential=token,
        realm_id=daemon.service.realm['id'], actor_id='media-editor',
        client_name='timeline-selected-media-test', client_version='1',
        protocol_version='workspace.v1',
    )
    head = client.timelines.history(editing.project, 'film', limit=1)
    assert head.ok, head.as_dict()
    head = head.data['head']
    result = client.timelines.replace_parent_media(
        editing.project, 'film', occurrence_id=None, clip_id='clip-a',
        source_object_id=editing.old['object_id'], expected_head=head['revision_id'],
        expected_publication=head['publication_id'], old_media=editing.new,
        media=editing.old, idempotency_key='sdk-swap-back')
    assert result.ok, result.error
    assert State(bundle_from_document(result.data['canonical_document'])).entity(TARGET)['asset'] == editing.old['asset']

    # The WC3 SDK adapter submits the original head pair and selected pins.
    # Use an already existing alias after redo: no new allocation is needed.
    from astrid.sdk.remote import RemoteTimelines

    class Transport:
        def list_timelines(self, project, *, cursor=None, limit=50):
            return ([{'project_id': project, 'timeline_id': 'film', 'slug': 'film'}], None)

        def publish_parent_composition(self, *args, **kwargs):
            pytest.fail('selected SDK adapter published a whole closure')

        def commit_timeline(self, project, timeline, request, *, idempotency_key):
            assert (project, timeline) == (editing.project, 'film')
            return commit(editing, request, idempotency_key)

    head = body(editing)
    result = RemoteTimelines(Transport()).replace_parent_media(
        editing.project, 'film', occurrence_id=None, clip_id='clip-a',
        source_object_id=editing.old['object_id'], expected_head=head['base_revision_id'],
        expected_publication=head['base_publication_id'], old_media=editing.new,
        media=editing.old, idempotency_key='sdk-swap-back')
    assert result.ok, result.error
    assert State(bundle_from_document(result.data['canonical_document'])).entity(TARGET)['asset'] == editing.old['asset']


def test_selected_swap_over_authenticated_http_preserves_flight_and_guarded_undo(
        http_editing, monkeypatch, real_runtime, tmp_path):
    """Prove one bounded native swap for both raw parent and explicit occurrence owners."""
    from runtime_protocol.server import RuntimeHandler

    from astrid.sdk.client import AstridClient
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline.sync import TimelineSession

    editing = http_editing
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client-data'))
    wire = []
    recorded_requests = []
    phase = 'fixture'
    original_send = RuntimeHandler._send
    lost = [True]

    def send(handler, status, payload=None, **kwargs):
        recorded_requests.append((handler.command, handler.path, phase))
        if (handler.command == 'POST' and handler.path.endswith('/commit')
                and getattr(handler, '_defer_response', False)):
            record = {
                'key': handler.headers['Idempotency-Key'],
                'body': handler._prefetched_body,
                'status': status,
                'response': copy.deepcopy(payload),
            }
            wire.append(record)
            if status == 200 and lost[0]:
                lost[0] = False
                raise ConnectionError('selected swap lost ACK after durable acceptance')
        return original_send(handler, status, payload, **kwargs)

    monkeypatch.setattr(RuntimeHandler, '_send', send)
    # The fixture's in-process service owns this realm until the HTTP daemon takes over.
    editing.service.close()
    daemon = real_runtime(
        editing.root, support_root=tmp_path / 'http-support',
        commit='b92d3e7a05eec65c142294ef4873edd059e9b31d', authoring_profile='astrid',
    )
    token, _ = daemon.credentials.provision(
        'media-editor', [
            'handshake',
            'projects:read', 'projects:write', 'objects:read', 'objects:write',
            'tasks:read', 'tasks:write',
        ],
    )
    # Fixture publication is complete before product transport is observed.
    phase = 'product'
    client = AstridClient.open(
        endpoint=daemon.endpoint, credential=token,
        realm_id=daemon.service.realm['id'], actor_id='media-editor',
        client_name='timeline-selected-media-http-test', client_version='1',
        protocol_version='workspace.v1',
    )
    initial = client.timelines.history(editing.project, 'film', limit=1)
    assert initial.ok, initial.as_dict()
    initial_head = initial.data['head']
    target = editing.owner + '/clip:clip-a'
    key = 'selected-http-' + ('parent' if editing.owner == 'parent' else 'occurrence')

    result = client.timelines.replace_parent_media(
        editing.project, 'film', occurrence_id=None if editing.owner == 'parent' else 'occ-a',
        clip_id='clip-a', source_object_id=editing.new['object_id'],
        expected_head=initial_head['revision_id'],
        expected_publication=initial_head['publication_id'], old_media=editing.old,
        media=editing.new, idempotency_key=key,
    )
    assert not result.ok
    assert result.error.code in {'transport_error', 'service_unavailable'}
    assert len(wire) == 1

    first = copy.deepcopy(wire[0])
    first_body = json.loads(first['body'])
    assert first['key'] == key
    assert first_body['base_revision_id'] == initial_head['revision_id']
    assert first_body['base_publication_id'] == initial_head['publication_id']
    command = first_body['commands'][0]
    assert command['select'] == target
    assert command['op'] == ('clip.swap-media' if editing.owner == 'parent' else 'occurrence.swap-media')
    assert command['args'] == {'old_media': editing.old, 'media': editing.new}
    outbox = next((path for path in (tmp_path / 'client-data' / 'timeline-outbox').glob('*.json')))
    saved = json.loads(outbox.read_text())
    assert saved['flight']['key'] == first['key']
    assert saved['flight']['body'].encode() == first['body']
    assert saved['base'] == {
        'revision_id': initial_head['revision_id'],
        'publication_id': initial_head['publication_id'],
    }

    port, instance = daemon.httpd.server_port, daemon.instance_id
    daemon.stop()
    daemon = real_runtime(
        editing.root, support_root=tmp_path / 'http-support', port=port,
        commit='b92d3e7a05eec65c142294ef4873edd059e9b31d', authoring_profile='astrid',
    )
    assert daemon.instance_id != instance
    client = AstridClient.open(
        endpoint=daemon.endpoint, credential=token,
        realm_id=daemon.service.realm['id'], actor_id='media-editor',
        client_name='timeline-selected-media-http-test', client_version='1',
        protocol_version='workspace.v1',
    )
    retried = client.timelines.replace_parent_media(
        editing.project, 'film', occurrence_id=None if editing.owner == 'parent' else 'occ-a',
        clip_id='clip-a', source_object_id=editing.new['object_id'],
        expected_head=initial_head['revision_id'],
        expected_publication=initial_head['publication_id'], old_media=editing.old,
        media=editing.new, idempotency_key='different-caller-key-is-ignored',
    )
    assert retried.ok, retried.as_dict()
    assert len(wire) == 2
    assert {wire[1][field] for field in ('key', 'body')} == {first[field] for field in ('key', 'body')}
    retry_body = json.loads(wire[1]['body'])
    assert (retry_body['base_revision_id'], retry_body['base_publication_id']) == (
        first_body['base_revision_id'], first_body['base_publication_id'])
    assert retry_body == first_body
    assert not json.loads(outbox.read_text())['pending']

    reloaded = client.timelines.open_bundle(
        editing.project, 'film', revision_id=retried.data['accepted_revision_id'])
    assert reloaded.ok, reloaded.as_dict()
    assert State(reloaded.data['bundle']).entity(target)['asset'] == editing.new['asset']

    session = TimelineSession.open(editing.project, 'film', client=client, outbox_path=outbox)
    undone = session.undo(
        [retried.as_dict()['receipt']['receipt_id']], idempotency_key=key + '-undo',
    )
    assert undone['data']['status'] in {'accepted', 'rebased'}
    undo_body = json.loads(wire[-1]['body'])
    assert undo_body['commands'] == [{
        'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
        'args': {'publications': [retried.as_dict()['receipt']['receipt_id']]},
    }]
    restored = client.timelines.open_bundle(
        editing.project, 'film', revision_id=undone['data']['accepted_revision_id'])
    assert restored.ok, restored.as_dict()
    assert State(restored.data['bundle']).entity(target)['asset'] == editing.old['asset']
    history = client.timelines.history(editing.project, 'film', limit=10)
    assert history.ok, history.as_dict()
    assert history.data['items'][-1]['records']['undo_of'] == [retried.as_dict()['receipt']['receipt_id']]
    accepted_revision_path = (
        f'/v1/projects/{editing.project}/timelines/film/composition-revisions/'
        f'{retried.data["accepted_revision_id"]}'
    )
    assert ('GET', accepted_revision_path, 'product') in recorded_requests
    assert not legacy_composition_writes(recorded_requests)


def test_legacy_composition_writer_checker_rejects_planted_product_write():
    path = '/v1/projects/project/timelines/film/composition-revisions'
    records = [
        ('POST', path, 'fixture'),
        ('GET', path + '/revision-1', 'product'),
        ('POST', path, 'product'),
    ]

    assert legacy_composition_writes(records) == [('POST', path, 'product')]


@pytest.mark.parametrize('side', ['old_media', 'media'])
@pytest.mark.parametrize('damage', ['stale', 'foreign', 'fingerprint'])
def test_invalid_selected_media_refuses_without_writes(editing, side, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    request = copy.deepcopy(body(editing, [command(editing)]))
    pin = request['commands'][0]['args'][side]
    if damage == 'stale':
        association = next(row for row in editing.associations if row['object_id'] == pin['object_id'])
        editing.service.update_managed_output_lifecycle(association['association_id'], {
            'operation': 'promote', 'expected_version': association['lifecycle']['version'],
        }, idempotency_key='stale')
    elif damage == 'foreign':
        request['commands'][0]['args'][side] = foreign_pin(editing)
    else:
        pin['catalog_digest'] = pin['object_id']
    before = rows(editing)
    with pytest.raises(RuntimeErrorBase):
        commit(editing, request, 'invalid')
    assert editing.candidates == []
    assert rows(editing) == before


@pytest.mark.parametrize('side', ['old_media', 'media'])
def test_lifecycle_race_refuses_at_acceptance(editing, side):
    from runtime_protocol.errors import RuntimeErrorBase

    request = body(editing, [command(editing)])
    pin = request['commands'][0]['args'][side]
    observed = []

    def interfere():
        association = next(row for row in editing.associations if row['object_id'] == pin['object_id'])
        editing.service.update_managed_output_lifecycle(association['association_id'], {
            'operation': 'promote', 'expected_version': association['lifecycle']['version'],
        }, idempotency_key='accept-race')
        observed.append(rows(editing))

    editing.before_accept = interfere
    with pytest.raises(RuntimeErrorBase, match='selected media catalog digest changed'):
        commit(editing, request, 'race')
    assert rows(editing) == observed[0]
    assert len(editing.candidates) == 1


@pytest.mark.parametrize('intervention', ['disjoint', 'aba'])
def test_accepted_history_rebase_and_guarded_aba_undo(editing, intervention):
    from runtime_protocol.errors import RuntimeErrorBase

    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline.commit import bundle_from_document

    request = body(editing, [command(editing)])
    if intervention == 'disjoint':
        sibling = {'schema_version': 1, 'op': 'clip.set', 'version': '1',
                   'select': 'parent/clip:clip-b', 'args': {'values': {'hold': 2.5}, 'clear': []}}
        side = commit(editing, body(editing, [sibling]), 'intervening-sibling')
        swapped = commit(editing, request, 'stale-swap')
        assert State(bundle_from_document(swapped['data']['canonical_document'])).entity('parent/clip:clip-b')['hold'] == 2.5
        undone = commit(editing, body(editing, [inverse(swapped)]), 'undo-rebased')
        assert undone['data']['canonical_document'] == side['data']['canonical_document']
    else:
        swapped = commit(editing, request, 'swap')
        again = copy.deepcopy(command(editing))
        again['args']['old_media'] = editing.new
        repeated = commit(editing, body(editing, [again]), 'same-asset-write')
        assert repeated['data']['canonical_document'] == swapped['data']['canonical_document']
        before = rows(editing)
        with pytest.raises(RuntimeErrorBase) as error:
            commit(editing, body(editing, [inverse(swapped)]), 'guarded-undo')
        assert error.value.details['reason'] == 'intervening_write'
        assert rows(editing) == before
