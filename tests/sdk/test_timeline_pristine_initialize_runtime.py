"""Real in-process Runtime create → init → restart/reload → undo → exact retry."""
from __future__ import annotations

import copy
import io
import subprocess
import wave

import pytest

from tests.editing_conformance.test_pristine_initialize import GOLDEN
from tests.helpers.runtime import use_runtime, vendored_runtime_commit
from tests.v10 import test_timelines_create

runtime_create_case = test_timelines_create.runtime_create_case

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('explicit_tracks', [False, True])
def test_remote_create_with_content_is_one_authored_commit_and_exact_undo(
    runtime_create_case, explicit_tracks,
):
    case = runtime_create_case
    service, project = case.service, case.project
    image = subprocess.run(
        ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=16x16:r=1',
         '-frames:v', '1', '-threads', '1', '-c:v', 'png', '-f', 'image2pipe', 'pipe:1'],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    image_id = service.ingest(
        project, image, media_type='image/png', idempotency_key='content-picture',
    )['data']['object_id']
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as output:
        output.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\x00\x00' * 16000)
    audio_id = service.ingest(
        project, audio.getvalue(), media_type='audio/wav', idempotency_key='content-voice',
    )['data']['object_id']
    service.create_timeline(project, 'film', idempotency_key='content-shell')
    initial = service.get_project_timeline(project, 'film')
    bootstrap = service._timeline_commits()[0].revision_closure(
        project, 'film', initial['head_revision_id'],
    )
    bootstrap = copy.deepcopy(bootstrap)
    original_tracks = bootstrap['parent_revision']['payload']['config']['tracks']
    tracks = ([{'id': 'picture', 'kind': 'visual'}, {'id': 'voice', 'kind': 'audio'}]
              if explicit_tracks else original_tracks)
    picture = next(row['id'] for row in tracks if row['kind'] == 'visual')
    voice = next(row['id'] for row in tracks if row['kind'] == 'audio')
    config = {
        'theme_overrides': {'visual': {'canvas': {'width': 640, 'height': 360, 'fps': 24}}},
        'clips': [
            {'id': 'picture-clip', 'track': picture, 'clipType': 'image',
             'at': 0, 'hold': 2, 'asset': 'picture'},
            {'id': 'voice-clip', 'track': voice, 'clipType': 'audio',
             'at': 0, 'hold': 2, 'asset': 'voice'},
        ],
        'registry': {'assets': {
            'picture': {'media_id': image_id, 'type': 'image/png'},
            'voice': {'media_id': audio_id, 'type': 'audio/wav'},
        }},
    }
    if explicit_tracks:
        config['tracks'] = tracks
    created = case.timelines.create_empty(
        project=project, timeline_id='film', config=config, idempotency_key='content',
    )
    assert created.ok, created.as_dict()
    document = created.data['canonical_document']
    payload = document['parent_revision']['payload']
    assert payload['config']['output']['fps'] == 24
    assert payload['clips'] == config['clips']
    assert payload['config']['tracks'] == tracks
    history = service.list_timeline_commits(project, 'film', authorize=lambda: None)['items']
    assert len(history) == 2
    assert history[0]['intent'] == 'bootstrap'
    assert history[1]['intent'] == 'recorded'
    expected = [*(['parent/track:picture', 'parent/track:voice'] if explicit_tracks else []),
                'parent/clip:picture-clip', 'parent/clip:voice-clip',
                'parent/asset:picture', 'parent/asset:voice']
    patch = next(record['command'] for record in history[1]['records']['commands']
                 if record['command']['op'] == 'doc.patch')
    assert patch['created'] == expected
    pin = service.get_project_timeline(project, 'film')
    request = {
        'api_version': 1, 'project_id': project, 'timeline_id': 'film',
        'base_revision_id': pin['head_revision_id'],
        'base_publication_id': pin['head_publication_id'],
        'commands': [{'schema_version': 1, 'op': 'timeline.inverse', 'version': '1',
                      'select': None, 'args': {'publications': [created.receipt.receipt_id]}}],
    }
    undone = service.commit_timeline(
        project, 'film', request, idempotency_key='undo-content', publisher='editor',
        invocation_path=f'/v1/projects/{project}/timelines/film/commit', authorize=lambda: None,
    )
    assert undone['data']['canonical_document'] == bootstrap
    restored = undone['data']['canonical_document']['parent_revision']['payload']
    assert 'output' not in restored['config']
    assert 'theme_overrides' not in restored['config']


@pytest.mark.parametrize('declaration', [None, ['parent/track:forged']])
def test_remote_creation_refuses_missing_or_forged_created_ids(
    runtime_create_case, monkeypatch, declaration,
):
    case = runtime_create_case
    generated = case.timelines._client._generated
    original = generated.commit_timeline

    def forged_commit(project, timeline, body, **kwargs):
        body = copy.deepcopy(body)
        patch = next(command for command in body['commands'] if command['op'] == 'doc.patch')
        if declaration is None:
            patch.pop('created')
        else:
            patch['created'] = declaration
        return original(project, timeline, body, **kwargs)

    monkeypatch.setattr(generated, 'commit_timeline', forged_commit)
    created = case.timelines.create_empty(
        project=case.project, timeline_id='forged',
        config={'tracks': [{'id': 'picture', 'kind': 'visual'}]},
        idempotency_key='forged-content',
    )
    assert not created.ok
    history = case.service.list_timeline_commits(
        case.project, 'forged', authorize=lambda: None,
    )['items']
    assert len(history) == 1 and history[0]['intent'] == 'bootstrap'


def test_remote_creation_requires_runtime_verified_media(runtime_create_case):
    case = runtime_create_case
    created = case.timelines.create_empty(
        project=case.project, timeline_id='missing-media',
        config={
            'tracks': [{'id': 'voice', 'kind': 'audio'}],
            'clips': [{'id': 'voice', 'track': 'voice', 'asset': 'voice',
                       'clipType': 'audio', 'at': 0, 'hold': 1}],
            'registry': {'assets': {'voice': {'media_id': 'sha256:' + '0' * 64,
                                              'type': 'audio/wav'}}},
        },
        idempotency_key='missing-media',
    )
    assert not created.ok
    history = case.service.list_timeline_commits(
        case.project, 'missing-media', authorize=lambda: None,
    )['items']
    assert len(history) == 1 and history[0]['intent'] == 'bootstrap'


@pytest.mark.parametrize('explicit_metadata', [False, True])
def test_initialize_batch_authenticates_sequential_command_inputs(tmp_path, monkeypatch, explicit_metadata):
    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import route_path
    from runtime_protocol.timeline_commit_baseline import empty_bootstrap_document

    # The recorded App batch supplies its own tracks and targets the legacy
    # trackless bootstrap. Keep exercising that existing publication format.
    monkeypatch.setattr("runtime_protocol.timeline_commit_baseline.default_bootstrap_document", empty_bootstrap_document, raising=False)

    from astrid.sdk.timeline.commit import create_runtime_commit_service

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(create_runtime_commit_service())
    try:
        project = service.create_project({'slug': 'staged-init', 'name': 'Staged init'})['id']
        service.create_timeline(project, 'film', idempotency_key='create-film')
        head = service.get_project_timeline(project, 'film')
        request = {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                   'base_revision_id': head['head_revision_id'],
                   'base_publication_id': head['head_publication_id'],
                   'commands': copy.deepcopy(GOLDEN['commands']), 'dry_run': False}
        if explicit_metadata:
            request.update(label='Initialize', merge='fields', reads=[],
                           source={'via': 'python', 'detail': 'stage-input-test'},
                           rationale='Authenticate each sequential input', group_id='initialize',
                           steps=[{'id': 'initialize', 'label': 'Initialize', 'intent': 'Create film',
                                   'tags': ['bootstrap'], 'command_ordinals': [0, 1, 2], 'patch': [],
                                   'reads': []}])
        original = copy.deepcopy(request)

        def submit():
            return service.commit_timeline(project, 'film', request, idempotency_key='initialize',
                publisher='author', invocation_path=route_path(project, 'film'), authorize=lambda: None)

        result = submit()
        assert result['data']['status'] == 'accepted'
        assert result['data']['canonical_document']['parent_revision']['payload'] == GOLDEN['app_parent']
        assert result['receipt']['result'] == result['data']
        history = service.list_timeline_commits(project, 'film', authorize=lambda: None)['items']
        records = history[-1]['records']
        if explicit_metadata:
            for key in ('label', 'source', 'rationale', 'group_id'):
                assert result['data'][key] == request[key]
            assert result['data']['steps'] == records['steps']
            assert records['merge_mode'] == request['merge']
            for key in ('id', 'label', 'intent', 'tags', 'command_ordinals'):
                assert records['steps'][0][key] == request['steps'][0][key]
        assert records['commands'][0]['command']['op'] == 'timeline.init'
        for record in records['commands'][1:]:
            pins = [binding for binding in record['bindings'] if binding['key'] == 'commit.input']
            assert len(pins) == 1
            assert pins[0]['physical_ref'] == 'parent'
            assert pins[0]['boundary']['phase'] == 'input'
            assert pins[0]['boundary']['command_ordinal'] == record['ordinal']
        assert request == original
        assert submit() == result
        assert service.list_timeline_commits(project, 'film', authorize=lambda: None)['items'] == history
    finally:
        service.close()


def test_pristine_init_crash_resume_reload_undo_retry_and_redo(tmp_path, monkeypatch):
    use_runtime(commit=vendored_runtime_commit())
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import route_path
    from runtime_protocol.timeline_commit_baseline import empty_bootstrap_document

    # This persisted golden intent predates Runtime-owned starter tracks.
    monkeypatch.setattr("runtime_protocol.timeline_commit_baseline.default_bootstrap_document", empty_bootstrap_document, raising=False)

    from astrid.sdk.timeline import commands, commit
    from astrid.sdk.timeline.outbox import Outbox

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    project = service.create_project({'slug': 'initialize', 'name': 'Initialize'})['id']

    def head():
        row = service.get_project_timeline(project, 'film')
        return {'revision_id': row['head_revision_id'], 'publication_id': row['head_publication_id']}

    def request(calls):
        current = head()
        return {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                'base_revision_id': current['revision_id'],
                'base_publication_id': current['publication_id'], 'commands': calls, 'dry_run': False}

    def submit(body, key):
        return service.commit_timeline(project, 'film', body, idempotency_key=key, publisher='author',
                                       invocation_path=route_path(project, 'film'), authorize=lambda: None)

    def inverse(result):
        return {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
                'args': {'publications': [result['receipt']['receipt_id']]}}

    def reopen():
        service.close()
        reopened = RuntimeService(root)
        reopened.register_timeline_commit_provider(commit.create_runtime_commit_service())
        return reopened

    try:
        # This models a client persisting its original create/init intent before
        # either call. App must wire its own recovery store to the same sequence.
        outbox = Outbox(tmp_path / 'initialize-outbox.json')
        with outbox.locked():
            outbox.write({'create_key': 'create-film', 'init_key': 'init-film',
                          'commands': copy.deepcopy(GOLDEN['commands'])})
        created = service.create_timeline(project, 'film', idempotency_key='create-film', name='Main')
        initial = head()
        # Persisted caller intent already includes the name/slug/defaults; a
        # restart between legal create and init must retain the empty basis.
        service = reopen()
        recovered = Outbox(outbox.path).read()
        intent = recovered['commands']
        assert head() == initial
        assert service.create_timeline(project, 'film', idempotency_key='create-film', name='Main') == created
        original = request(intent)
        with outbox.locked():
            outbox.write({**recovered, 'in_flight': original})
        initialized = submit(original, 'init-film')
        document = initialized['data']['canonical_document']
        assert document['parent_revision']['payload'] == GOLDEN['app_parent']
        # Independent immutable read, then process restart after a lost ACK.
        revision = head()['revision_id']
        assert service.get_project_parent_composition_revision(project, 'film', revision)['payload'] == GOLDEN['app_parent']
        service = reopen()
        assert submit(Outbox(outbox.path).read()['in_flight'], recovered['init_key']) == initialized
        undone = submit(request([inverse(initialized)]), 'undo-init')
        assert undone['data']['canonical_document']['parent_revision']['payload'] == {
            'config': {'clips': [], 'tracks': []}, 'registry': {}, 'clips': [], 'occurrences': []}
        retired = head()
        assert retired['revision_id'] == initial['revision_id'] and retired != initial
        service = reopen()

        def forbidden(*args, **kwargs):
            raise AssertionError('exact accepted retry reapplied initializer or checked new semantics')

        with monkeypatch.context() as retry:
            retry.setattr(commands, 'execute', forbidden)
            retry.setattr(commit.RuntimeCommitService, 'commit', forbidden)
            assert submit(original, 'init-film') == initialized
        assert head() == retired
        for body in (request(intent), original):
            with pytest.raises(RuntimeErrorBase) as error:
                submit(body, 'new-init-' + body['base_publication_id'])
            assert error.value.details['reason'] == 'not_pristine' and head() == retired
        changed_payload = copy.deepcopy(original)
        changed_payload['commands'][0]['args']['name'] = 'Changed'
        with pytest.raises(RuntimeErrorBase):
            submit(changed_payload, 'init-film')
        assert head() == retired
        redone = submit(request([inverse(undone)]), 'redo-init')
        assert redone['data']['canonical_document'] == document
        assert len(service.list_timeline_commits(project, 'film', authorize=lambda: None)['items']) == 4
    finally:
        service.close()


def test_remote_create_empty_uses_live_commit_bootstrap_and_exact_replay(
    tmp_path, monkeypatch, real_runtime,
):
    """The public creator must reach the native commit path on a normal client."""
    use_runtime(commit=vendored_runtime_commit())
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    daemon = real_runtime(authoring_profile="astrid")
    project = daemon.service.create_project({"slug": "live-create", "name": "Live create"})["id"]
    client = open_client(daemon)
    config = {
        "tracks": [],
        "theme_overrides": {"visual": {"canvas": {"width": 640, "height": 360, "fps": 24}}},
        "meta": {"org.example": {"opaque": [None, False], "empty": None}},
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("create_empty used the retired parent publisher")

    monkeypatch.setattr(daemon.service, "publish_parent_composition", forbidden)
    created = client.timelines.create_empty(
        project=project, timeline_id="film", name="Main", config=config,
        idempotency_key="live-create",
    )
    assert created.ok, created.as_dict()
    document = created.data["canonical_document"]
    assert document["parent_revision"]["payload"]["name"] == "Main"
    assert document["parent_revision"]["payload"]["config"]["meta"] == config["meta"]
    assert document["parent_revision"]["payload"]["config"]["output"] == {
        "resolution": "640x360", "fps": 24, "file": "output.mp4",
        "background": None, "background_scale": None,
    }
    # Explicit empty tracks remain a caller choice, even when Runtime now
    # supplies canonical tracks in the bootstrap head.
    assert document["parent_revision"]["payload"]["config"]["tracks"] == []
    opened = client.timelines.open_bundle(project, "film")
    assert opened.ok, opened.as_dict()
    assert opened.data["bundle"]["parent"]["config"]["meta"] == config["meta"]

    from astrid.sdk.timeline import open as open_timeline

    outbox = tmp_path / "reload-outbox.json"
    timeline = open_timeline(project, "film", client=client, outbox_path=outbox)
    assert timeline.bundle["parent"]["config"]["meta"] == config["meta"]
    head = client.timelines.history(project, "film", limit=1)
    assert head.ok, head.as_dict()
    pin = head.data["head"]
    later_change = {
        "op": "set", "ref": "parent",
        "path": ["config", "meta", "org.example", "later"],
        "before": {"state": "absent"}, "value": "edit",
    }
    later_command = {
        "schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
        "args": {"changes": [later_change]},
    }
    later = client.timelines.commit(
        project, "film", {
            "api_version": 1, "project_id": project, "timeline_id": "film",
            "base_revision_id": pin["revision_id"], "base_publication_id": pin["publication_id"],
            "commands": [later_command], "label": "later edit", "merge": "atomic",
            "steps": [{"id": "later", "label": "later edit", "intent": "later edit",
                        "tags": [], "command_ordinals": [0], "patch": [later_change]}],
        }, idempotency_key="live-later-edit",
    )
    assert later.ok, later.as_dict()
    reloaded = open_timeline(project, "film", client=client, outbox_path=tmp_path / "reload-2.json")
    assert reloaded.bundle["parent"]["config"]["meta"]["org.example"]["later"] == "edit"

    transport = client._remote._transport

    def forbidden_commit(*args, **kwargs):
        raise AssertionError("exact create retry submitted a second commit")

    monkeypatch.setattr(transport, "commit_timeline", forbidden_commit)
    replayed = client.timelines.create_empty(
        project=project, timeline_id="film", name="Main", config=config,
        idempotency_key="live-create",
    )
    assert replayed.ok, replayed.as_dict()
    assert replayed.receipt == created.receipt
    assert replayed.data["canonical_document"] == created.data["canonical_document"]
    after_retry = open_timeline(project, "film", client=client, outbox_path=tmp_path / "reload-3.json")
    assert after_retry.bundle["parent"]["config"]["meta"]["org.example"]["later"] == "edit"
