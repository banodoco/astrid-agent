"""Direct real Runtime acceptance; no daemon, sockets or validator substitution."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._doc_patch import Closure, allocations, apply_change, value_at
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    create_runtime_commit_service,
)
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared
from tests.editing_conformance.test_recipe_clip_scalar_fields import (
    CHILD,
    CLEAR,
    PARENT,
    app_calls,
    clip,
)
from tests.helpers.runtime import use_runtime
from tests.sdk.test_timeline_clip_scalar_provider import app_document
from tests.timeline.test_authoring_bundle import AUDIO, OLD, _replace_values

pytestmark = pytest.mark.integration


def _patch(bundle, ref, path, value, *, verified_media=()):
    physical = Closure(bundle).resolve(ref)
    before = value_at(Closure(bundle).payload(physical), physical, path)
    change = {'op': 'set', 'ref': ref, 'path': list(path),
              'before': before, 'value': copy.deepcopy(value)}
    closure = Closure(bundle)
    root = closure.payload(physical)
    after, _ = apply_change(copy.deepcopy(root), change, physical=physical)
    return execute(bundle, [{
        'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
        'created': [row['ref'] for row in allocations(closure, physical, root, after)],
        'args': {'changes': [change]},
    }], verified_media=verified_media).commands[0]


def _seed_current_app_document(service, project, document):
    """Author the App closure through current Runtime semantic commands."""
    from runtime_protocol.timeline_commit_port import MediaReads, route_path

    timeline = 'film'
    service.create_timeline(project, timeline, idempotency_key='create-film')
    head = service.get_project_timeline(project, timeline)
    bootstrap = service.get_project_parent_composition_revision(
        project, timeline, head['head_revision_id'])
    empty = {'schema_version': 1, 'project_id': project, 'timeline_id': timeline,
             'parent_revision': {key: bootstrap[key] for key in
                                 ('revision_id', 'content_digest', 'payload')},
             'shot_revisions': [], 'internal_timeline_revisions': [],
             'dependency_manifest': bootstrap['dependency_manifest']}
    bundle = bundle_from_document(empty)
    target_parent = document['parent_revision']['payload']
    inputs = [{'pin': copy.deepcopy(pin)} for pin in document['dependency_manifest']['media']]
    # Use Runtime's managed-project/CAS read locally; the provider recaptures
    # these exact inputs through media.admit when it accepts the commit.
    verified_media = [{**MediaReads(service).raw_media_info(project, row['pin']),
                       'pin': copy.deepcopy(row['pin'])} for row in inputs]
    commands = [{'schema_version': 1, 'op': 'media.admit', 'version': '1', 'select': None,
                 'args': {'inputs': inputs}}]

    def append(command):
        nonlocal bundle
        commands.append(command)
        bundle = execute(bundle, [command], verified_media=verified_media).bundle

    parent_payload = Closure(bundle).payload('parent')
    parent_payload.update({key: copy.deepcopy(target_parent[key]) for key in
                           ('config', 'registry', 'clips', 'opaque_parent')})
    append(_patch(bundle, 'parent', [], parent_payload, verified_media=verified_media))

    first, second = target_parent['occurrences']
    append(capture_occurrence_create(
        bundle, first['occurrence_id'], first['shot_id'], name='F2 agent shot',
        track=first['track'], on=first['placement']['start_ms'] / 1000,
        duration=first['duration_ms'] / 1000))
    internal = document['internal_timeline_revisions'][0]
    for row in internal['payload']['tracks']:
        append(capture_track_create(bundle, 'occurrence:' + first['occurrence_id'], row))
    internal_ref = next(ref for ref in Closure(bundle).images if ref.startswith('internal-revision:'))
    append(_patch(bundle, internal_ref, [], internal['payload'], verified_media=verified_media))

    shot = document['shot_revisions'][0]
    shot_payload = copy.deepcopy(shot['payload'])
    shot_payload.pop('internal_timeline_revision_id', None)
    shot_ref = next(ref for ref in Closure(bundle).images if ref.startswith('shot-revision:'))
    append(_patch(bundle, shot_ref, [], shot_payload))

    def occurrence(row):
        actual = next(item for item in Closure(bundle).payload('parent')['occurrences']
                      if item['occurrence_id'] == row['occurrence_id'])
        value = {**copy.deepcopy(row), **{key: actual[key] for key in
                                        ('shot_id', 'shot_revision_id')}}
        append(_patch(bundle, 'parent', ['occurrences', 'occurrence_id:' + row['occurrence_id']], value))

    def submit(key):
        head = service.get_project_timeline(project, timeline)
        request = {
            'api_version': 1, 'project_id': project, 'timeline_id': timeline,
            'base_revision_id': head['head_revision_id'],
            'base_publication_id': head['head_publication_id'], 'commands': commands,
            'label': 'App scalar fixture',
            'steps': [{'id': str(index), 'label': 'App scalar fixture',
                       'intent': 'Prepare current scalar fixture', 'tags': ['fixture'],
                       'command_ordinals': [index],
                       'patch': (copy.deepcopy(command['args']['changes'])
                                 if command['op'] == 'doc.patch' else []),
                       'reads': []}
                      for index, command in enumerate(commands)],
            'merge': 'atomic', 'reads': [], 'dry_run': False,
            'source': {'via': 'python'}, 'rationale': 'Current scalar fixture',
            'group_id': 'app-scalar-fixture',
        }
        return service.commit_timeline(
            project, timeline, request, idempotency_key=key,
            publisher='scalar-author', invocation_path=route_path(project, timeline),
            authorize=lambda: None)

    occurrence(first)
    accepted = submit('seed-app-scalars')
    # Shared placement requires the unchanged, published physical source.
    bundle = bundle_from_document(accepted['data']['canonical_document'])
    commands = []
    append(capture_occurrence_place_shared(
        bundle, 'occurrence:' + first['occurrence_id'], second['occurrence_id'],
        on=second['placement']['start_ms'] / 1000))
    occurrence(second)
    return submit('seed-app-scalar-sibling')


@pytest.mark.parametrize('mode', ['scalar', 'compound', 'separate'])
def test_app_hold_asset_transition_commit_reload_inverse_redo_and_aba(tmp_path, mode):
    use_runtime()
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import route_path

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(create_runtime_commit_service())
    project = service.create_project({'slug': 'clip-scalars', 'name': 'Clip scalars'})['id']
    service.create_timeline(project, 'film', idempotency_key='create-film')
    media = service.ingest(project, b'clip scalar fixture', media_type='image/png',
                           original_name='fixture.png', idempotency_key='fixture')['data']['object_id']
    document = _replace_values(app_document(), {OLD: media, AUDIO: media})
    parent, shots, internals = document['parent_revision'], document['shot_revisions'], document['internal_timeline_revisions']
    for row in internals:
        row['timeline_id'] = 'film'
        row['payload'].update(effects=[], audio=[], layout={})
    for row in shots:
        row['payload'] = {'metadata': {'title': 'F2 agent shot'}, 'items': [], 'pools': [],
            'selected_variants': {}, 'provenance': {}, 'generation_inputs': {},
            'audio_bindings': [], 'text_bindings': [],
            'internal_timeline_revision_id': row['internal_timeline_revision_id']}
    for row in [parent, *shots, *internals]:
        row['content_digest'] = _digest(row['payload'])
    document['dependency_manifest'] = _manifest(shots, internals, [{'media_id': media, 'content_digest': media}], [])
    _seed_current_app_document(service, project, {
        'schema_version': 1, 'project_id': project, 'timeline_id': 'film',
        'parent_revision': parent, 'shot_revisions': shots,
        'internal_timeline_revisions': internals,
        'dependency_manifest': document['dependency_manifest'],
    })

    def head():
        row = service.get_project_timeline(project, 'film')
        return {'revision_id': row['head_revision_id'], 'publication_id': row['head_publication_id']}

    def current():
        return service._timeline_commits()[0].revision_closure(project, 'film', head()['revision_id'])

    def request(calls):
        pin = head()
        return {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                'base_revision_id': pin['revision_id'], 'base_publication_id': pin['publication_id'],
                'commands': calls, 'dry_run': False}

    def submit(body, key):
        return service.commit_timeline(project, 'film', body, idempotency_key=key,
            publisher='scalar-author', invocation_path=route_path(project, 'film'), authorize=lambda: None)

    def undo(publication, key):
        return submit(request([{'schema_version': 1, 'op': 'timeline.inverse', 'version': '1',
            'select': None, 'args': {'publications': [publication]}}]), key)

    try:
        original = current()
        state = bundle_from_document(original)
        moves = []
        if mode != 'scalar':
            from astrid.sdk.timeline.capture import capture_move
            from astrid.sdk.timeline.commands import execute

            moves = [capture_move(state, CHILD, 4)]
            state = execute(state, moves).bundle
        calls = app_calls(state)
        if mode == 'separate':
            submit(request(moves), 'move-before-scalars')
            original = current()
        elif mode == 'compound':
            calls = moves + calls
        body = request(calls)
        accepted = submit(body, 'app-scalars')
        assert accepted['data']['commands'] == calls
        document = accepted['data']['canonical_document']
        assert current() == document
        state = bundle_from_document(document)
        assert clip(state, CHILD)['hold'] == 4
        if mode != 'scalar':
            assert clip(state, CHILD)['at'] == 4
            assert clip(state, CHILD)['app']['astrid'] == {'fixed_start': True, 'for': 4}
        assert 'asset' not in clip(state, CLEAR)
        assert 'transition' not in clip(state, PARENT)
        service.close()
        service = RuntimeService(root)
        service.register_timeline_commit_provider(create_runtime_commit_service())
        assert current() == document
        assert submit(body, 'app-scalars') == accepted
        undone = undo(accepted['receipt']['receipt_id'], 'undo-app-scalars')
        assert undone['data']['canonical_document'] == original
        redone = undo(undone['receipt']['receipt_id'], 'redo-app-scalars')
        assert redone['data']['canonical_document'] == document
        repeated = submit(request([capture_clip_set(bundle_from_document(document), CLEAR, clear=['asset'])]), 'unchanged-clear')
        assert repeated['data']['canonical_document'] == document
        before = copy.deepcopy(head())
        with pytest.raises(RuntimeErrorBase) as error:
            undo(accepted['receipt']['receipt_id'], 'refuse-aba')
        assert error.value.details['reason'] == 'intervening_write'
        assert head() == before
    finally:
        service.close()
