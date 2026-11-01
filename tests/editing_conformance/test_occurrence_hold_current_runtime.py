"""Bounded current-Runtime proof for authentic image hold ADD/SWAP."""
from __future__ import annotations

import base64
import copy
import inspect
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration
OWNER = 'occurrence:occ-a'
TARGET = OWNER + '/clip:clip-a'
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='
)


def _video_bytes():
    missing = [tool for tool in ('ffmpeg', 'ffprobe') if shutil.which(tool) is None]
    assert not missing, f'required media tools are unavailable: {", ".join(missing)}'
    return subprocess.run(
        ['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=16x16:r=1',
         '-t', '1', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
         '-movflags', '+frag_keyframe+empty_moov', '-f', 'mp4', 'pipe:1'],
        check=True, stdout=subprocess.PIPE,
    ).stdout


def _commit_body(editing, commands, *, label, key, dry_run=False):
    head = editing.service.get_project_timeline(editing.project, 'film')
    ordinals = list(range(len(commands)))
    return {
        'api_version': 1,
        'project_id': editing.project,
        'timeline_id': 'film',
        'base_revision_id': head['head_revision_id'],
        'base_publication_id': head['head_publication_id'],
        'commands': copy.deepcopy(commands),
        'label': label,
        'merge': 'fields',
        'reads': [],
        'dry_run': dry_run,
        'steps': [{'id': key, 'label': label, 'intent': label, 'tags': ['G4', 'hold-image'],
                   'command_ordinals': ordinals, 'patch': [], 'reads': []}],
        'source': {'via': 'python'},
        'rationale': 'Authenticate the public image hold occurrence edit',
        'group_id': key,
    }


def _commit(editing, body, key):
    return editing.service.commit_timeline(
        editing.project, 'film', body, idempotency_key=key, publisher='g4-held-image',
        invocation_path=f'/v1/projects/{editing.project}/timelines/film/commit',
        authorize=lambda: None,
    )


def _write_evidence(editing, *, request, prepared, output, history_before, history_after, command):
    root = os.environ.get('ORCH')
    if not root:
        return
    from astrid.sdk.timeline import commit as commit_module
    from astrid.sdk.timeline import native_occurrence_media_commands as media_module

    evidence = {
        'request': request,
        'prepared': prepared,
        'input': {'baseline': editing.baseline, 'command': command},
        'output': output,
        'history_before': history_before,
        'history_after': history_after,
        'records': prepared['records'],
        'bindings': prepared['records']['commands'][0].get('bindings', []),
        'effects': prepared['records']['steps'][0].get('patch', []),
        'origins': {
            'commit': inspect.getsourcefile(commit_module),
            'native_occurrence_media': inspect.getsourcefile(media_module),
        },
    }
    destination = Path(root) / 'workers' / 'agt-G4-held-image-current-1240' / 'evidence'
    destination.mkdir(parents=True, exist_ok=True)
    (destination / f'current-held-image-{editing.operation}.json').write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding='utf-8',
    )


def _import_image(service, project, alias):
    result = service.import_media(
        project, PNG, media_type='image/png', original_name=f'{alias}.png',
        actor_id='g4-held-image', idempotency_key=f'import-{alias}',
    )['data']
    selection = service.get_project_media_selection(project, {
        'asset': result['variant_id'], 'generation_id': result['generation_id'],
        'variant_id': result['variant_id'],
    }, authorize=lambda: None)
    verified = service.get_project_verified_media(project, selection, authorize=lambda: None)
    assert verified['selection'] == selection
    return selection, verified


@pytest.fixture(params=['add', 'swap'])
def editing(request, tmp_path, monkeypatch):
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / 'tests'))
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import RequestCommitPort
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from test_timeline_commit_read_basis import _canonical_from_public_reads

    from astrid.sdk.timeline.capture import capture_track_create
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from astrid.sdk.timeline.native_occurrence_media_commands import (
        capture_occurrence_add_media,
    )

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    project = service.create_project({'slug': 'g4-held-image', 'name': 'G4 held image'})['id']
    service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
    service.create_timeline(project, 'film', idempotency_key='timeline')
    old, old_verified = _import_image(service, project, 'old')
    new, new_verified = _import_image(service, project, 'new')

    def document():
        page = service.list_timeline_commits(project, 'film', authorize=lambda: None)
        return _canonical_from_public_reads(service, project, 'film', page)

    try:
        accepted = []
        real_accept = RequestCommitPort.accept

        def capture_accept(port, prepared):
            accepted.append(copy.deepcopy(prepared))
            return real_accept(port, prepared)

        monkeypatch.setattr(RequestCommitPort, 'accept', capture_accept)
        initial = document()
        bundle = bundle_from_document(initial)
        commands = []
        parent_track = capture_track_create(bundle, 'parent', {'id': 'picture', 'kind': 'visual'})
        commands.append(parent_track)
        bundle = execute(bundle, commands[-1:]).bundle
        birth = capture_occurrence_create(
            bundle, 'occ-a', 'shot-a', name='Opening', track='picture', on=0, duration=5,
        )
        commands.append(birth)
        bundle = execute(bundle, commands[-1:]).bundle
        child_track = capture_track_create(bundle, OWNER, {'id': 'picture', 'kind': 'visual'})
        commands.append(child_track)
        bundle = execute(bundle, commands[-1:]).bundle
        if request.param == 'swap':
            old_row = {'id': 'clip-a', 'clipType': 'hold', 'asset': old['asset'], 'track': 'picture',
                       'at': 2, 'hold': 2, 'params': None, 'opaque': [None, False, {'keep': None}]}
            old_add = capture_occurrence_add_media(
                bundle, OWNER, old_row, old, verified_media=[old_verified],
            )
            commands.append(old_add)
            bundle = execute(bundle, commands[-1:], verified_media=[old_verified]).bundle

        setup = _commit_body(SimpleNamespace(service=service, project=project), commands,
                             label='G4 held-image setup', key='setup')
        _commit(SimpleNamespace(service=service, project=project), setup, 'setup')
        baseline = document()
        yield SimpleNamespace(
            service=service, project=project, operation=request.param, baseline=baseline,
            old=old, old_verified=old_verified, new=new, new_verified=new_verified,
            bundle=bundle_from_document(baseline), document=document,
            add_media=capture_occurrence_add_media, accepted=accepted,
        )
    finally:
        service.close()


def test_image_hold_add_or_swap_is_accepted_with_exact_public_selection(editing):
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import document_from_bundle
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_swap_media
    from tests.editing_conformance.test_recipe_occurrence_media import inverse

    if editing.operation == 'add':
        row = {'id': 'clip-a', 'clipType': 'hold', 'asset': editing.new['asset'], 'track': 'picture',
               'at': 2, 'hold': 2, 'params': None, 'opaque': [None, False, {'keep': None}]}
        command = editing.add_media(editing.bundle, OWNER, row, editing.new,
                                    verified_media=[editing.new_verified])
        verified = [editing.new_verified]
    else:
        command = capture_occurrence_swap_media(
            editing.bundle, TARGET, editing.old, editing.new,
            verified_media=[editing.old_verified, editing.new_verified],
        )
        verified = [editing.old_verified, editing.new_verified]

    expected = execute(editing.bundle, [command], verified_media=verified)
    preview_before = tuple(editing.service.store.conn.iterdump())
    preview = _commit(editing, _commit_body(editing, [command], label='Held image', key='preview', dry_run=True), 'preview')
    assert preview['receipt'] is None
    assert preview['data']['commands'] == [command]
    assert tuple(editing.service.store.conn.iterdump()) == preview_before

    request = _commit_body(editing, [command], label='Held image', key='held-image')
    history_before = editing.service.list_timeline_commits(editing.project, 'film', authorize=lambda: None)
    created = _commit(editing, request, 'held-image')
    output = created['data']['canonical_document']
    assert output == document_from_bundle(expected.bundle)
    raw = State(expected.bundle).entity(OWNER + '/clip:clip-a')
    assert raw['clipType'] == 'hold'
    assert raw['hold'] == 2
    assert raw['params'] is None
    assert raw['opaque'] == [None, False, {'keep': None}]
    assert all(key not in raw for key in ('from', 'to', 'speed'))
    assert editing.new_verified['selection'] == editing.new
    assert editing.service.get_project_media_selection(
        editing.project, {key: editing.new[key] for key in ('asset', 'generation_id', 'variant_id')},
        authorize=lambda: None,
    ) == editing.new
    assert editing.service.get_project_verified_media(
        editing.project, editing.new, authorize=lambda: None,
    ) == editing.new_verified
    media_ids = {pin['media_id'] for pin in output['dependency_manifest']['media']}
    assert editing.new['object_id'] in media_ids
    if editing.operation == 'swap':
        assert editing.old['object_id'] in media_ids

    history_after = editing.service.list_timeline_commits(editing.project, 'film', authorize=lambda: None)
    _write_evidence(editing, request=request, prepared=editing.accepted[-1], output=output,
                    history_before=history_before, history_after=history_after, command=command)
    assert inverse(expected.bundle, expected.records).bundle == editing.bundle


def test_video_hold_is_refused_by_current_runtime_without_a_write(editing):
    from runtime_protocol.errors import RuntimeErrorBase

    video_result = editing.service.import_media(
        editing.project, _video_bytes(), media_type='video/mp4', original_name='video.mp4',
        actor_id='g4-held-image', duration_seconds=1, idempotency_key='import-video',
    )['data']
    video = editing.service.get_project_media_selection(editing.project, {
        'asset': video_result['variant_id'], 'generation_id': video_result['generation_id'],
        'variant_id': video_result['variant_id'],
    }, authorize=lambda: None)
    command = {'schema_version': 1, 'op': 'occurrence.add-media', 'version': '1', 'select': OWNER,
               'args': {'media': video, 'row': {
                   'id': 'video-hold', 'clipType': 'hold', 'asset': video['asset'], 'track': 'picture',
                   'at': 1, 'hold': 2, 'params': None, 'opaque': [None, False, {'keep': None}],
               }}, 'intent': {'ownership': 'occurrence'}}
    before = tuple(editing.service.store.conn.iterdump())
    history = editing.service.list_timeline_commits(editing.project, 'film', authorize=lambda: None)
    with pytest.raises(RuntimeErrorBase) as error:
        _commit(editing, _commit_body(editing, [command], label='Video hold refusal', key='video-hold'), 'video-hold')
    assert error.value.status == 422
    assert editing.service.list_timeline_commits(editing.project, 'film', authorize=lambda: None) == history
    assert tuple(editing.service.store.conn.iterdump()) == before
