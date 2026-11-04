"""Ordinary pinned CLI/Python/sheet commands on authenticated Runtime HTTP.

Run only through the central test service. This uses the existing injected
transport, unmodified generated client and startup-loaded semantic provider.
"""
from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path

import pytest

from astrid.core.timeline.authoring_bundle import open_authoring_bundle, publish_authoring_candidate
from astrid.core.timeline.duration import clip_end_frame, clip_start_frame
from astrid.packs.timeline.cli import build_parser
from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._track_commands import capture_track_set
from astrid.sdk.timeline.capture import capture_move, capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.timeline.native_initialize import capture_initialize
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork
from astrid.sdk.timeline.native_occurrence_shared import foreign_shared_command
from astrid.sdk.timeline.native_parent_media_commands import capture_clip_add_media
from astrid.sdk.timeline.sync import CommitConflict, TimelineSession
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_editing import add_track
from astrid.sdk.timeline_sheet import NativeSheet
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.editing_conformance import test_native_batch_ingress
from tests.editing_conformance.test_native_batch_ingress import command, label
from tests.editing_conformance.test_native_sheet_current import property_cell
from tests.editing_conformance.test_recipe_clip_scalar_fields import app_closure
from tests.sdk.test_timeline_sheet_native import edit_cell, remove_row

pytestmark = pytest.mark.integration
runtime = test_native_batch_ingress.runtime
REF = 'parent/clip:title'
RATIONAL_FPS = {'numerator': 30000, 'denominator': 1001}
RATIONAL_FPS_VALUE = Fraction(30000, 1001)
RATIONAL_LATTICE_SECONDS = 1.001
RATIONAL_TARGET = 'parent/clip:b-plate'
OCCURRENCE_TARGET = 'occurrence:f2-occurrence/clip:f2-agent-clip'
OCCURRENCE_CLEAR = 'occurrence:f2-occurrence/clip:f2-agent-clear-clip'
OCCURRENCE_SIBLING = 'occurrence:shared-sibling/clip:f2-agent-clip'
OCCURRENCE_GLOBAL_ON = 9


def run(runtime, *args):
    parsed = build_parser(runtime.client()).parse_args([*args, '--project', runtime.project, '--json'])
    return parsed.handler(parsed)


def export(runtime, capsys, tmp_path):
    assert run(runtime, 'show', 'film', '--as', 'sheet', '--native') == 0
    text = json.loads(capsys.readouterr().out)['data']['text']
    session = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    sheet = NativeSheet.load(session)
    assert text == sheet.text
    text = edit_cell(text, REF, '3.017')
    text = property_cell(text, REF, 'hold', '2.013')
    text = property_cell(text, REF, 'transition', 'clear')
    text = property_cell(text, REF, 'text', '""')
    text = edit_cell(text, 'parent/track:picture', '""', label=True)
    path = tmp_path / 'sheet.txt'
    path.write_text(text)
    return session, sheet, path


def detached_commit_file(expected, head, tmp_path):
    """Write the edited bundle in the ordinary detached commit format."""
    candidate = Checkout(copy.deepcopy(expected.bundle))
    candidate.bundle['base_parent']['revision_id'] = head['revision_id']
    candidate.bundle['base_parent']['publication_id'] = head['publication_id']
    return candidate.save(tmp_path / 'timeline.json', quiet=True)


def seed(runtime):
    runtime.session().commit_many([
        command('track.create', 'parent', {'row': {'id': 'picture', 'kind': 'visual', 'opaque': [None, True, 1]}}),
        label('Old'),
        command('clip.add', 'parent', {'row': {'id': 'title', 'clipType': 'am-type', 'track': 'picture',
            'at': .017, 'hold': 1.013, 'params': {'text': 'Original', 'opaque': {'keep': [None, False]}},
            'opaque': [None, False, 1], 'transition': {'type': 'crossfade', 'duration': .25}}}),
    ])


def raw():
    return [command('clip.move', REF, {'on': 3.017}),
            command('clip.set', REF, {'values': {'hold': 2.013}, 'clear': ['transition']}),
            command('clip.set-params', REF, {'values': {'text': ''}, 'clear': []}), label('')]


def tiny_cut_parent(service, project):
    """Build the saved cut-owned parent from the existing tiny sheet fixture."""
    source = json.loads((Path(__file__).parents[1] / 'fixtures' / 'timeline_editing' / 'tiny.json').read_text())
    timeline = source['shots']['B']['internal_timeline']
    media_ids = {}
    media_files = {
        'P': ('idea-1.wav', 'audio/wav', {'duration_seconds': 1}),
        'Q': ('idea-1.png', 'image/png', {'width': 64, 'height': 64}),
        'R': ('idea-1-alt.png', 'image/png', {'width': 64, 'height': 64}),
    }
    media_root = Path(__file__).parents[1] / 'fixtures' / 'storyboard-assets'
    for asset, (filename, media_type, metadata) in media_files.items():
        result = service.import_media(
            project, (media_root / filename).read_bytes(), media_type=media_type,
            original_name=filename, actor_id='native-sheet-current',
            **metadata,
            idempotency_key=f'native-sheet-cut-media-{asset}',
        )['data']
        selection = service.get_project_media_selection(project, {
            'asset': result['variant_id'], 'generation_id': result['generation_id'],
            'variant_id': result['variant_id'],
        }, authorize=lambda: None)
        verified = service.get_project_verified_media(project, selection, authorize=lambda: None)
        assert verified['selection'] == selection
        media_ids[asset] = verified['selection']['object_id']

    clips = copy.deepcopy(timeline['clips'])
    registry = copy.deepcopy(timeline['registry'])
    replacements = {f'm-{asset.lower()}': media_id for asset, media_id in media_ids.items()}
    _replace_media_ids(clips, replacements)
    _replace_media_ids(registry, replacements)
    tracks = copy.deepcopy(timeline['tracks'])
    payload = {
        'config': {
            'clips': copy.deepcopy(clips),
            'tracks': tracks,
            'output': {'fps': {'numerator': 30000, 'denominator': 1001}},
        },
        'clips': clips,
        'occurrences': [],
        'registry': registry,
    }
    return {
        'project_id': project,
        'timeline_id': 'film',
        'parent_revision_id': 'native-sheet-cut-parent-v1',
        'parent_composition': payload,
        'shot_revisions': [],
        'internal_timeline_revisions': [],
        'expected_head': None,
    }


def _public_document(service, project, timeline):
    """Read one exact Runtime closure through the public revision APIs."""
    page = service.list_timeline_commits(project, timeline, limit=1, authorize=lambda: None)
    head = page['head']
    parent = service.get_project_parent_composition_revision(project, timeline, head['revision_id'])
    document = {
        'schema_version': 1,
        'project_id': project,
        'timeline_id': timeline,
        'parent_revision': {key: parent[key] for key in ('revision_id', 'content_digest', 'payload')},
        'shot_revisions': [],
        'internal_timeline_revisions': [],
        'dependency_manifest': parent['dependency_manifest'],
    }
    for pin in parent['dependency_manifest']['shots']:
        image = service.get_project_shot_revision(project, pin['shot_id'], pin['revision_id'])
        document['shot_revisions'].append({key: image[key] for key in (
            'revision_id', 'shot_id', 'internal_timeline_revision_id', 'content_digest', 'payload')})
    for pin in parent['dependency_manifest']['internal_timelines']:
        image = service.get_project_timeline_revision(project, pin['timeline_id'], pin['revision_id'])
        document['internal_timeline_revisions'].append({key: image[key] for key in (
            'revision_id', 'timeline_id', 'content_digest', 'payload')})
    return document, head


def _publish_current_candidate(runtime, timeline, payload, shots, internals, *, key):
    """Publish a detached closure through the current v2 Runtime caller."""
    service = runtime.service()
    current, head = _public_document(service, runtime.project, timeline)
    target_parent = {
        'project_id': runtime.project,
        'timeline_id': timeline,
        'revision_id': current['parent_revision']['revision_id'],
        'content_digest': _digest(payload),
        'payload': copy.deepcopy(payload),
    }
    candidate = open_authoring_bundle(
        target_parent, shot_revisions=shots, internal_timeline_revisions=internals,
    )
    candidate['base_parent'] = {
        'revision_id': current['parent_revision']['revision_id'],
        'content_digest': current['parent_revision']['content_digest'],
    }
    candidate['base_parent_payload'] = copy.deepcopy(current['parent_revision']['payload'])
    candidate['base_parent_payload'].pop('occurrences', None)
    writer = runtime.client().timelines._client
    publication = publish_authoring_candidate(candidate, writer, idempotency_key=key)['publication']
    assert publication['accepted_revision_id'] == publication['canonical_document']['parent_revision']['revision_id']
    assert publication['previous_revision_id'] == head['revision_id']
    canonical, canonical_head = _public_document(service, runtime.project, timeline)
    assert canonical_head == {
        'revision_id': publication['accepted_revision_id'],
        'publication_id': publication['receipt']['receipt_id'],
    }
    assert canonical == publication['canonical_document']
    publication['canonical_document'] = canonical
    return publication


def _seed_authoring_document(runtime, document, *, key):
    payload = copy.deepcopy(document['parent_composition'])
    payload.setdefault('occurrences', [])
    return _publish_current_candidate(runtime, 'film', payload, document['shot_revisions'],
                                      document['internal_timeline_revisions'], key=key)


def seed_tiny_cut_parent(runtime):
    document = tiny_cut_parent(runtime.service(), runtime.project)
    target = copy.deepcopy(next(row for row in document['parent_composition']['clips']
                               if row['id'] == 'b-type'))
    receipt = _seed_authoring_document(runtime, document, key='native-sheet-cut-parent')
    return document, target, receipt


def seed_labeled_tiny_cut_parent(runtime):
    """Use the real tiny closure while giving the label-clear edit a prior value."""
    document = tiny_cut_parent(runtime.service(), runtime.project)
    payload = document['parent_composition']
    track = next(row for row in payload['config']['tracks'] if row['id'] == 'plate')
    track['label'] = 'Source'
    clip = next(row for row in payload['clips'] if row['id'] == 'b-plate')
    clip['opaque'] = {'source_token': ['fixture', None, False, 7]}
    payload['config']['clips'] = copy.deepcopy(payload['clips'])
    receipt = _seed_authoring_document(runtime, document, key='native-sheet-label-parent')
    return document, receipt


def seed_rational_tiny_cut_parent(runtime):
    """Create a retained media-bearing closure whose move is exactly frame-aligned."""
    document = tiny_cut_parent(runtime.service(), runtime.project)
    payload = document['parent_composition']
    payload['config']['output'] = copy.deepcopy(RATIONAL_FPS)
    target = next(row for row in payload['clips'] if row['id'] == 'b-plate')
    target['hold'] = RATIONAL_LATTICE_SECONDS
    target['opaque'] = {'retained_source': ['fixture', None, False, 7]}
    payload['config']['clips'] = copy.deepcopy(payload['clips'])
    receipt = _seed_authoring_document(runtime, document, key='native-sheet-rational-parent')
    return document, receipt


def _replace_media_ids(value, replacements):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ('media_id', 'content_sha256', 'object_id') and child in replacements:
                value[key] = replacements[child]
            else:
                _replace_media_ids(child, replacements)
    elif isinstance(value, list):
        for child in value:
            _replace_media_ids(child, replacements)


def seed_shared_occurrence_parent(runtime, *, prepare=None, identity_scope=None):
    """Seed the existing shared-source App closure through current Runtime commits."""
    parent, shots, internals = app_closure()
    project = runtime.project
    media_ids = set()

    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('media_id', 'object_id') and isinstance(child, str):
                    media_ids.add(child)
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect([parent, *shots, *internals])
    roles = {source_id: set() for source_id in media_ids}
    video_registry_sources = []
    for scope in [parent, *shots, *internals]:
        payload = scope.get('payload', scope)
        for entry in payload.get('registry', {}).get('assets', {}).values():
            source_id = entry.get('media_id')
            media_type = entry.get('type')
            if source_id in roles and media_type in {'image', 'image/png'}:
                roles[source_id].add('image/png')
            elif source_id in roles and media_type in {'video', 'video/mp4'}:
                roles[source_id].add('video/mp4')
            elif source_id in roles and media_type in {'audio', 'audio/wav'}:
                roles[source_id].add('audio/wav')
            if source_id in roles and media_type in {'video', 'video/mp4'}:
                video_registry_sources.append((entry, source_id))
        for binding in payload.get('audio_bindings', []):
            source_id = binding.get('object_id')
            if source_id in roles:
                roles[source_id].add('audio/wav')
        for item in payload.get('items', []):
            source_id = item.get('media_id')
            if source_id in roles:
                roles[source_id].add(
                    'audio/wav' if item.get('metadata', {}).get('role') == 'voiceover'
                    else 'image/png')
        visual = payload.get('generation_inputs', {}).get('visual', {})
        source_id = visual.get('object_id')
        if source_id in roles:
            roles[source_id].add('image/png')

    media_root = Path(__file__).parents[1] / 'fixtures' / 'storyboard-assets'
    video_path = Path(__file__).parents[1] / 'fixtures' / 'reshape' / 'hype_regression' / 'broll.mp4'
    media_files = {
        'image/png': ('idea-1.png', {'width': 64, 'height': 64}),
        'video/mp4': (video_path, {'width': 1280, 'height': 720, 'duration_seconds': 12}),
        'audio/wav': ('idea-1.wav', {'duration_seconds': 1}),
    }
    role_media = {}
    verified_by_object = {}
    for index, source_id in enumerate(sorted(media_ids)):
        role_media[source_id] = {}
        for media_type in sorted(roles[source_id]):
            filename, metadata = media_files[media_type]
            media_path = filename if isinstance(filename, Path) else media_root / filename
            extension = media_path.suffix.lstrip('.')
            imported = runtime.service().import_media(
                project, media_path.read_bytes(), media_type=media_type,
                original_name=f'connected-occurrence-source-{index}-{media_path.name}',
                actor_id='native-sheet-current',
                **metadata,
                idempotency_key=f'connected-occurrence-source-{index}-{extension}',
            )['data']
            selection = runtime.service().get_project_media_selection(project, {
                'asset': imported['variant_id'], 'generation_id': imported['generation_id'],
                'variant_id': imported['variant_id'],
            }, authorize=lambda: None)
            verified = runtime.service().get_project_verified_media(project, selection, authorize=lambda: None)
            assert verified['selection'] == selection
            role_media[source_id][media_type] = verified
            verified_by_object[verified['selection']['object_id']] = verified

    primary_replacements = {
        source_id: (role_media[source_id].get('image/png')
                    or role_media[source_id].get('audio/wav')
                    or role_media[source_id]['video/mp4'])['selection']['object_id']
        for source_id in sorted(media_ids)
    }
    _replace_media_ids(parent, primary_replacements)
    _replace_media_ids(shots, primary_replacements)
    _replace_media_ids(internals, primary_replacements)
    for entry, source_id in video_registry_sources:
        video = role_media[source_id]['video/mp4']['selection']['object_id']
        entry['media_id'] = video
        entry['content_sha256'] = video

    all_verified_media = list(verified_by_object.values())

    if identity_scope is not None:
        shot = shots[0]
        old_shot_id = shot['shot_id']
        old_shot_revision_id = shot['revision_id']
        old_internal_revision_id = shot['internal_timeline_revision_id']
        shot_id = f'{identity_scope}-shot'
        shot_revision_id = f'{identity_scope}-shot-revision'
        internal_revision_id = f'{identity_scope}-internal-revision'
        for occurrence in parent['payload']['occurrences']:
            if occurrence['shot_id'] == old_shot_id:
                occurrence['shot_id'] = shot_id
            if occurrence['shot_revision_id'] == old_shot_revision_id:
                occurrence['shot_revision_id'] = shot_revision_id
        shot['shot_id'] = shot_id
        shot['revision_id'] = shot_revision_id
        shot['internal_timeline_revision_id'] = internal_revision_id
        shot['payload']['internal_timeline_revision_id'] = internal_revision_id
        internal = next(item for item in internals if item['revision_id'] == old_internal_revision_id)
        internal['revision_id'] = internal_revision_id

    if prepare is not None:
        prepare(parent, shots, internals)

    # First create the source occurrence with the semantic birth command.  Its
    # one-occurrence authoring publication only changes authored fields; Runtime
    # keeps the occurrence/child identities and the immutable source authority.
    source_timeline = f'native-sheet-source-{runtime.project}'
    runtime.service().create_timeline(project, source_timeline, idempotency_key='native-sheet-source-bootstrap')
    source_session = TimelineSession.open(project, source_timeline, client=runtime.client())
    source_bundle = source_session._capture_bundle(source_session._state())
    source_commands = []

    def append_source(command_value):
        nonlocal source_bundle
        source_commands.append(command_value)
        source_bundle = execute(source_bundle, [command_value]).bundle

    source_payload = parent['payload']
    append_source(capture_initialize(
        source_bundle, name='Shared source', slug='shared-source',
        # Native initialization accepts only the current App bootstrap output.
        # The source publication below restores the fixture's exact authored
        # output, including its original representation and fps.
        output={
            'resolution': '1280x720', 'fps': 30, 'file': 'output.mp4',
            'background': None, 'background_scale': None,
        },
    ))
    for track in source_payload['config']['tracks']:
        append_source(capture_track_create(source_bundle, 'parent', track))
    source_occurrence = parent['payload']['occurrences'][0]
    source_shot = shots[0]
    append_source(capture_occurrence_create(
        source_bundle, source_occurrence['occurrence_id'], source_occurrence['shot_id'],
        name=source_shot['payload'].get('name', 'Shared source'), track=source_occurrence['track'],
        on=source_occurrence['placement']['start_ms'] / 1000,
        duration=source_occurrence['duration_ms'] / 1000,
    ))
    source_owner = 'occurrence:' + source_occurrence['occurrence_id']
    for track in internals[0]['payload']['tracks']:
        append_source(capture_track_create(source_bundle, source_owner, track))
    for row in source_payload['clips']:
        pin = verified_by_object[source_payload['registry']['assets'][row['asset']]['media_id']]['selection']
        source_commands.append(capture_clip_add_media(
            source_bundle, row, pin, verified_media=all_verified_media,
        ))
        source_bundle = execute(source_bundle, [source_commands[-1]], verified_media=all_verified_media).bundle
    source_session.commit_many(
        source_commands, idempotency_key='native-sheet-source-semantic',
        via='python', label='NativeSheet shared-source setup', merge='atomic',
        verified_media=all_verified_media,
    )

    source_current, _source_head = _public_document(runtime.service(), project, source_timeline)
    actual_occurrence = source_current['parent_revision']['payload']['occurrences'][0]
    actual_shot = source_current['shot_revisions'][0]
    actual_internal = source_current['internal_timeline_revisions'][0]
    source_parent_payload = copy.deepcopy(parent['payload'])
    source_parent_payload['occurrences'] = [copy.deepcopy(source_occurrence)]
    source_parent_payload['occurrences'][0].update(
        shot_id=actual_occurrence['shot_id'], shot_revision_id=actual_occurrence['shot_revision_id'],
    )
    source_shot_image = copy.deepcopy(source_shot)
    source_shot_image.update(
        project_id=project, timeline_id=source_timeline, revision_id=actual_shot['revision_id'],
        internal_timeline_revision_id=actual_internal['revision_id'],
        content_digest=_digest(source_shot_image['payload']),
    )
    source_internal_image = copy.deepcopy(internals[0])
    source_internal_image.update(
        project_id=project, timeline_id=source_timeline, revision_id=actual_internal['revision_id'],
        content_digest=_digest(source_internal_image['payload']),
    )
    source_parent_payload['occurrences'][0]['shot_revision_id'] = source_shot_image['revision_id']
    source_parent_payload['occurrences'][0]['shot_id'] = source_shot_image['shot_id']
    _publish_current_candidate(
        runtime, source_timeline, source_parent_payload, [source_shot_image], [source_internal_image],
        key='native-sheet-source-authoring',
    )
    source_saved, source_head = _public_document(runtime.service(), project, source_timeline)
    source_ref = {
        'project_id': project, 'timeline_id': source_timeline,
        'revision_id': source_saved['parent_revision']['revision_id'],
        'publication_id': source_head['publication_id'],
        'content_digest': source_saved['parent_revision']['content_digest'],
    }

    # Materialize the destination lanes, then place both occurrences through
    # the authenticated foreign-source semantic command.
    destination = runtime.session(name='connected-occurrence-skeleton')
    destination_bundle = destination._capture_bundle(destination._state())
    track_commands = [capture_track_create(destination_bundle, 'parent', row)
                      for row in parent['payload']['config']['tracks']]
    destination.commit_many(
        track_commands, idempotency_key='native-sheet-destination-skeleton',
        via='python', label='NativeSheet destination setup', merge='atomic',
    )
    destination_bundle = destination._capture_bundle(destination._state())
    managed_commands = []
    for row in parent['payload']['clips']:
        pin = verified_by_object[parent['payload']['registry']['assets'][row['asset']]['media_id']]['selection']
        managed_commands.append(capture_clip_add_media(
            destination_bundle, row, pin, verified_media=all_verified_media,
        ))
        destination_bundle = execute(
            destination_bundle, [managed_commands[-1]], verified_media=all_verified_media,
        ).bundle
    destination.commit_many(
        managed_commands, idempotency_key='native-sheet-destination-managed-media',
        via='python', label='NativeSheet managed media setup', merge='atomic',
        verified_media=all_verified_media,
    )
    foreign_commands = [
        foreign_shared_command(source_ref, source_occurrence['occurrence_id'], occurrence_id, on=on)
        for occurrence_id, on in (('f2-occurrence', 0), ('shared-sibling', 20))
    ]
    placed = destination.commit_many(
        foreign_commands, idempotency_key='native-sheet-connected-occurrence-parent',
        via='python', label='NativeSheet shared occurrence setup', merge='atomic',
    )

    current, _head = _public_document(runtime.service(), project, 'film')
    # Emit only authored parent changes against the full Runtime parent, keeping
    # its exact occurrence edges and the authenticated shared child closure.
    before_bundle = bundle_from_document(current)
    before_bundle['parent'] = copy.deepcopy(current['parent_revision']['payload'])
    authored_bundle = copy.deepcopy(before_bundle)
    for field, value in parent['payload'].items():
        if field != 'occurrences':
            authored_bundle['parent'][field] = copy.deepcopy(value)
    authored_commands = emit_document_commands(before_bundle, authored_bundle)
    publication = destination.commit_many(
        authored_commands, idempotency_key=(
            f'{identity_scope}-seed' if identity_scope is not None
            else 'native-sheet-connected-occurrence-authoring'),
        via='python', label='NativeSheet shared occurrence authored fields', merge='atomic',
    )
    final_document = copy.deepcopy(publication['data']['canonical_document'])
    document = {
        'project_id': project, 'timeline_id': 'film', 'expected_head': None,
        'parent_revision_id': final_document['parent_revision']['revision_id'],
        'parent_composition': final_document['parent_revision']['payload'],
        'shot_revisions': final_document['shot_revisions'],
        'internal_timeline_revisions': final_document['internal_timeline_revisions'],
    }
    assert placed['data']['status'] in {'accepted', 'merged', 'rebased'}
    return document, publication


def seed_document_diff_parent(runtime):
    """Seed real App-shaped rows exercised by the current document emitter."""
    def prepare(parent, shots, internals):
        parent_rows = parent['payload']['clips']
        for rows in (parent_rows, parent['payload']['config']['clips']):
            next(row for row in rows if row['id'] == 'f2-app-clip').update(hold=3)
        child = next(row for row in internals[0]['payload']['clips']
                     if row['id'] == 'f2-agent-clip')
        child.update({'clipType': 'am-type', 'from': 0, 'to': 3, 'params': {'text': 'hello'}})
        child.pop('asset', None)

    return seed_shared_occurrence_parent(
        runtime, prepare=prepare, identity_scope=f'emitter-{runtime.project}',
    )


def test_http_emitter_null_presence_and_track_roundtrip_is_authenticated(runtime):
    _document, setup = seed_document_diff_parent(runtime)
    runtime.restart()
    session = runtime.session(name='emitter')
    loaded = session.client.timelines.open_bundle(runtime.project, 'film')
    assert loaded.ok
    public_before = loaded.data['bundle']
    assert '_runtime_document' not in public_before['source_mapping']
    base = {
        'revision_id': setup['data']['accepted_revision_id'],
        'publication_id': setup['receipt']['receipt_id'],
    }
    assert base == runtime.history()['head']
    # Production Timeline._execution_bundle hydrates this exact pair before
    # invoking the pure emitter for child edits.
    hydrated = session.client.timelines.open_bundle(
        runtime.project, 'film', revision_id=base['revision_id'],
        publication_id=base['publication_id'],
    )
    assert hydrated.ok
    assert hydrated.data['project_id'] == runtime.project
    assert hydrated.data['timeline_id'] == 'film'
    assert hydrated.data['revision_id'] == base['revision_id']
    assert hydrated.data['is_current_head'] is True
    original_document = hydrated.data['bundle']['source_mapping']['_runtime_document']
    assert original_document == setup['data']['canonical_document']
    assert _digest(original_document) == setup['data']['document_digest']
    before = bundle_from_document(original_document)
    assert hydrated.data['bundle'] == {
        **before,
        'base_parent': {**before['base_parent'], 'publication_id': base['publication_id']},
    }
    assert persisted_bundle(before) == public_before
    source = occurrence_source_pin(before, OCCURRENCE_TARGET)
    assert source['text_bindings'] == [{'slot': 'voiceover', 'text': 'hello'}]
    fork = capture_occurrence_fork(before, 'occurrence:f2-occurrence', 'emitter-selected-shot')
    # Project the registered fork so the emitter addresses its unique physical
    # source. Both commands still land in the original single transaction.
    projected = bundle_from_document(document_from_bundle(execute(before, [fork]).bundle))

    def child(candidate):
        return State(candidate).entity(OCCURRENCE_TARGET)

    unsupported = copy.deepcopy(projected)
    child(unsupported)['params']['text'] = None
    head_before_refusal = copy.deepcopy(runtime.history()['head'])
    history_before_refusal = copy.deepcopy(runtime.history()['items'])
    calls_before_refusal = len(runtime.calls)
    invalid_commands = [fork, *emit_document_commands(projected, unsupported)]
    assert invalid_commands == [fork, {
        'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
        'args': {'changes': [{
            'op': 'set', 'ref': 'internal-revision:' + projected['source_mapping']['shots'][
                next(row for row in projected['placements']
                     if row['occurrence_id'] == 'f2-occurrence')['shot_id']
            ]['internal_timeline_revision_id'],
            'path': ['clips', 'id:f2-agent-clip', 'params', 'text'],
            'before': {'state': 'present', 'value': 'hello'}, 'value': None,
        }]},
    }]
    invalid_session = runtime.session(name='emitter-invalid')
    with pytest.raises(CommitConflict) as refusal:
        invalid_session.commit_many(invalid_commands, idempotency_key='emitter-invalid-param',
                                    label='Invalid occurrence-local parameter')
    details = refusal.value.failure['details']
    assert refusal.value.failure['code'] == 'validation_error'
    assert details['reason'] == details['code'] == 'invalid_document'
    assert details['path'] == []
    assert details['hint'] == 'Read the timeline and submit a valid canonical document change'
    assert details['suggested_command'] == 'timeline edit'
    assert runtime.history()['head'] == head_before_refusal
    assert runtime.history()['items'] == history_before_refusal
    assert len(runtime.calls) == calls_before_refusal + 1

    candidate = copy.deepcopy(projected)
    for rows in (candidate['parent']['clips'], candidate['parent']['config']['clips']):
        next(row for row in rows if row['id'] == 'f2-app-clip').pop('hold')
    edited_child = child(candidate)
    edited_child.pop('hold')
    edited_child['params'] = {}
    candidate['parent']['config']['tracks'][0]['label'] = 'Renamed'

    commands = [fork, *emit_document_commands(projected, candidate)]
    target_shot = next(row for row in projected['placements']
                       if row['occurrence_id'] == 'f2-occurrence')['shot_id']
    internal_ref = 'internal-revision:' + projected['source_mapping']['shots'][target_shot][
        'internal_timeline_revision_id']
    assert [command['op'] for command in commands] == ['occurrence.fork', 'doc.patch', 'doc.patch']
    assert [change for command in commands[1:] for change in command['args']['changes']] == [
        {'op': 'set', 'ref': 'parent', 'path': ['config', 'tracks', 'id:V1', 'label'],
         'before': {'state': 'absent'}, 'value': 'Renamed'},
        {'op': 'remove', 'ref': 'parent', 'path': ['clips', 'id:f2-app-clip', 'hold'],
         'before': {'state': 'present', 'value': 3}},
        {'op': 'remove', 'ref': internal_ref,
         'path': ['clips', 'id:f2-agent-clip', 'hold'],
         'before': {'state': 'present', 'value': 3}},
        {'op': 'remove', 'ref': internal_ref,
         'path': ['clips', 'id:f2-agent-clip', 'params', 'text'],
         'before': {'state': 'present', 'value': 'hello'}},
    ]
    # Match the session's normal command capture, then compare the complete v2
    # recorder against the exact submitted request and authenticated base pair.
    commands = execute(before, commands).commands
    result = session.commit_many(commands, idempotency_key='emitter-roundtrip',
                                 label='Occurrence-local emitter roundtrip')
    assert result['data']['commands'] == commands
    body = json.loads(runtime.calls[-1][0])
    assert body['commands'] == commands
    assert body['source'] == {'via': 'python'}
    assert body['base_revision_id'] == base['revision_id']
    assert body['base_publication_id'] == base['publication_id']
    expected_records = execute(before, commands, commit_request=body, evaluated_head=base).records

    expected_input = execute(before, commands, commit_request=body, evaluated_head=base).bundle
    expected_document = document_from_bundle(expected_input)
    assert result['data']['canonical_document'] == expected_document
    accepted = bundle_from_document(result['data']['canonical_document'])
    expected = bundle_from_document(expected_document)
    assert persisted_bundle(accepted) == persisted_bundle(expected)
    accepted_parent = next(row for row in accepted['parent']['clips'] if row['id'] == 'f2-app-clip')
    accepted_child = State(accepted).entity(OCCURRENCE_TARGET)
    assert 'hold' not in accepted_parent and accepted_parent['from'] == 0 and accepted_parent['to'] == 3
    assert 'hold' not in accepted_child and accepted_child['from'] == 0 and accepted_child['to'] == 3
    assert accepted_child['params'] == {}
    assert accepted_child['app'] == {'f2Opaque': {'owner': 'f2', 'nested': [1, {'k': 'v'}]}}
    assert accepted['parent']['config']['tracks'][0]['label'] == 'Renamed'
    assert accepted['parent']['config']['x_f2_opaque'] == before['parent']['config']['x_f2_opaque']
    # Compile materialization gives only the edited occurrence its fresh expected source pin.
    assert occurrence_source_pin(accepted, OCCURRENCE_TARGET) == occurrence_source_pin(
        expected, OCCURRENCE_TARGET)
    target_pin = occurrence_source_pin(accepted, OCCURRENCE_TARGET)
    sibling_pin = occurrence_source_pin(accepted, OCCURRENCE_SIBLING)
    assert target_pin['shot_id'] != source['shot_id']
    assert target_pin['revision_id'] != source['revision_id']
    assert target_pin['internal_timeline_revision_id'] != source['internal_timeline_revision_id']
    assert sibling_pin == occurrence_source_pin(before, OCCURRENCE_SIBLING)
    assert State(accepted).entity(OCCURRENCE_SIBLING) == State(before).entity(OCCURRENCE_SIBLING)
    assert original_document['shot_revisions'][0] in result['data']['canonical_document']['shot_revisions']
    assert original_document['internal_timeline_revisions'][0] in result['data'][
        'canonical_document']['internal_timeline_revisions']
    assert [row['occurrence_id'] for row in accepted['placements']] == [
        row['occurrence_id'] for row in before['placements']]
    before_occurrences = {row['occurrence_id']: row for row in before['placements']}
    for row in accepted['placements']:
        original = before_occurrences[row['occurrence_id']]
        assert {key: row[key] for key in ('placement', 'source_offset', 'duration_ms', 'speed', 'track')} == {
            key: original[key] for key in ('placement', 'source_offset', 'duration_ms', 'speed', 'track')
        }

    history = runtime.history()
    assert history['head']['revision_id'] == result['data']['accepted_revision_id']
    entry = next(item for item in history['items']
                 if item['publication_id'] == result['receipt']['receipt_id'])
    assert entry['records']['commands'] == expected_records

    runtime.restart()
    reloaded = runtime.session(name='document-diff-reloaded')
    stored = reloaded.client.timelines.open_bundle(
        runtime.project, 'film', revision_id=result['data']['accepted_revision_id'],
    )
    assert stored.ok
    assert persisted_bundle(stored.data['bundle']) == persisted_bundle(accepted)
    # Reload preserves the accepted materialized source pin for the edited occurrence.
    assert occurrence_source_pin(stored.data['bundle'], OCCURRENCE_TARGET) == target_pin

    undone = reloaded.undo([result['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert State(restored).entity(OCCURRENCE_TARGET) == State(before).entity(OCCURRENCE_TARGET)
    assert State(restored).entity(OCCURRENCE_SIBLING) == State(before).entity(OCCURRENCE_SIBLING)
    assert occurrence_source_pin(restored, OCCURRENCE_TARGET) == source
    assert restored['parent']['config']['x_f2_opaque'] == before['parent']['config']['x_f2_opaque']
    redone = reloaded.undo([undone['receipt']['receipt_id']])
    assert redone['data']['canonical_document'] == result['data']['canonical_document']


def occurrence_source_pin(bundle, ref):
    owner = ref.rsplit('/', 1)[0]
    occurrence = next(row for row in bundle['placements']
                      if 'occurrence:' + row['occurrence_id'] == owner)
    shot_id = occurrence['shot_id']
    source = bundle['source_mapping']['shots'][shot_id]
    return {
        'shot_id': source['shot_id'],
        'revision_id': source['revision_id'],
        'internal_timeline_revision_id': source['internal_timeline_revision_id'],
        'internal_timeline_id': source['internal_timeline_id'],
        'text_bindings': copy.deepcopy(bundle['shots'][shot_id]['payload']['text_bindings']),
    }


def persisted_bundle(bundle):
    """Drop the in-memory Runtime document hint before comparing persisted closure state."""
    value = copy.deepcopy(bundle)
    value.get('source_mapping', {}).pop('_runtime_document', None)
    return value


def test_http_omitted_native_clip_row_delete_retries_rebases_and_refuses_changed_selection(
        runtime, capsys):
    target, sibling = 'parent/clip:title-a', 'parent/clip:title-b'
    runtime.session().commit_many([
        command('track.create', 'parent', {'row': {'id': 'picture', 'kind': 'visual', 'opaque': [None, True, 1]}}),
        label('Old'),
        command('clip.add', 'parent', {'row': {
            'id': 'title-a', 'clipType': 'am-type', 'track': 'picture', 'at': .017, 'hold': 1.013,
            'params': {'text': 'Same title'}, 'opaque': {'a': [None, False, 1]}}}),
        command('clip.add', 'parent', {'row': {
            'id': 'title-b', 'clipType': 'am-type', 'track': 'picture', 'at': 2.017, 'hold': 1.013,
            'params': {'text': 'Same title'}, 'opaque': {'b': [None, False, 2]}}}),
    ])
    assert run(runtime, 'show', 'film', '--as', 'sheet', '--native') == 0
    text = json.loads(capsys.readouterr().out)['data']['text']
    session = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    sheet = NativeSheet.load(session)
    assert text == sheet.text and f'"{target}"' in text and f'"{sibling}"' in text
    original = copy.deepcopy(sheet._bundle)
    edited = remove_row(sheet.text, target)
    assert [row['id'] for row in original['parent']['clips']] == ['title-a', 'title-b']
    assert State(original).entity(target)['params'] == State(original).entity(sibling)['params']
    expected = sheet.preview(edited)
    assert [item['op'] for item in expected.commands] == ['clip.delete']

    runtime.lost.add('delete-once')
    with pytest.raises(WorkspaceClientError):
        sheet.apply(edited, session=session, dry_run=False, idempotency_key='delete-once')
    first = copy.deepcopy(runtime.calls[-1][:2])
    runtime.restart()
    reloaded = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    result = NativeSheet.load(reloaded).apply(edited, session=reloaded, dry_run=False)
    assert runtime.calls[-1][:2] == first
    stored = reloaded.client.timelines.open_bundle(
        runtime.project, 'film', revision_id=result['data']['accepted_revision_id']).data['bundle']
    assert [row['id'] for row in stored['parent']['clips']] == ['title-b']
    assert State(stored).entity(sibling) == State(original).entity(sibling)
    undone = reloaded.undo([result['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert [row['id'] for row in restored['parent']['clips']] == ['title-a', 'title-b']
    assert State(restored).entity(sibling) == State(original).entity(sibling)
    redone = reloaded.undo([undone['receipt']['receipt_id']])
    assert redone['data']['canonical_document'] == result['data']['canonical_document']
    reloaded.undo([redone['receipt']['receipt_id']])

    old = NativeSheet.export(reloaded)
    old_edit = remove_row(old.text, target)
    runtime.session(name='peer').commit(command('clip.set-params', sibling,
                                                {'values': {'text': 'Peer edit'}, 'clear': []}))
    rebased = old.apply(old_edit, session=reloaded, dry_run=False)
    rebased_bundle = bundle_from_document(rebased['data']['canonical_document'])
    assert rebased['data']['status'] == 'rebased'
    assert State(rebased_bundle).entity(sibling)['params']['text'] == 'Peer edit'
    reloaded.undo([rebased['receipt']['receipt_id']])

    membership = runtime.session(name='membership')
    membership_sheet = NativeSheet.export(membership)
    runtime.session(name='membership-peer').commit(command('clip.add', 'parent', {'row': {
        'id': 'title-c', 'clipType': 'am-type', 'track': 'picture', 'at': 4.017, 'hold': 1.013,
        'params': {'text': 'Another title'}}}))
    head = copy.deepcopy(runtime.history()['head'])
    count = len(runtime.history()['items'])
    with pytest.raises(CommitConflict):
        membership_sheet.apply(remove_row(membership_sheet.text, target), session=membership, dry_run=False)
    assert runtime.history()['head'] == head and len(runtime.history()['items']) == count

    selected = runtime.session(name='selected')
    selected_sheet = NativeSheet.export(selected)
    runtime.session(name='selected-peer').commit(command('clip.delete', target, {}))
    head = copy.deepcopy(runtime.history()['head'])
    count = len(runtime.history()['items'])
    with pytest.raises(CommitConflict):
        selected_sheet.apply(remove_row(selected_sheet.text, target), session=selected, dry_run=False)
    assert runtime.history()['head'] == head and len(runtime.history()['items']) == count


def test_http_cut_owned_native_sheet_hold_reloads_and_undoes(runtime):
    document, original_target, setup = seed_tiny_cut_parent(runtime)

    # The seed is setup-only. The edit starts after a normal Runtime restart and
    # reads the accepted closure through the authenticated public client.
    runtime.restart()
    session = runtime.session(name='cut-owned-native')
    admitted = session.client.timelines.open_bundle(runtime.project, 'film')
    assert admitted.ok
    admitted_bundle = admitted.data['bundle']
    assert State(admitted_bundle).entity('parent/clip:b-type') == original_target
    assert admitted_bundle['parent']['config']['clips'] == document['parent_composition']['clips']
    assert runtime.history()['head']['revision_id'] == setup['data']['accepted_revision_id']
    assert len(runtime.history()['items']) == 2

    sheet = NativeSheet.export(session)
    target = 'parent/clip:b-type'
    edited = property_cell(sheet.text, target, 'hold', '1.5')
    plan = sheet.preview(edited)
    assert plan.commands == [capture_clip_set(sheet._bundle, target, values={'hold': 1.5})]
    assert State(plan.bundle).entity(target)['app'] == {
        'cut': 'c2', 'layer': 'card', 'for': 1.5,
    }

    peer = runtime.session(name='cut-owned-peer')
    peer.commit(label('Peer', track='type'))
    result = sheet.apply(edited, session=session, dry_run=False)
    assert result['data']['status'] == 'rebased'
    assert json.loads(runtime.calls[-1][0])['commands'] == plan.commands
    accepted = bundle_from_document(result['data']['canonical_document'])
    assert State(accepted).entity(target)['app'] == {
        'cut': 'c2', 'layer': 'card', 'for': 1.5,
    }
    assert State(accepted).entity('parent/track:type')['label'] == 'Peer'

    runtime.restart()
    reloaded = runtime.session(name='cut-owned-native-reloaded')
    stored = reloaded.client.timelines.open_bundle(
        runtime.project, 'film', revision_id=result['data']['accepted_revision_id'])
    assert stored.ok
    stored_state = State(stored.data['bundle'])
    assert stored_state.entity(target)['app'] == {
        'cut': 'c2', 'layer': 'card', 'for': 1.5,
    }
    assert stored_state.entity('parent/track:type')['label'] == 'Peer'

    undone = reloaded.undo([result['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert State(restored).entity(target) == original_target
    assert State(restored).entity('parent/track:type')['label'] == 'Peer'


@pytest.mark.parametrize('entry', ['python', 'cli', 'sheet'])
def test_http_connected_label_clear_is_lossless_across_native_frontends(
        runtime, capsys, entry, monkeypatch):
    _document, setup = seed_labeled_tiny_cut_parent(runtime)
    runtime.restart()
    session = runtime.session(name=entry)
    loaded = session.client.timelines.open_bundle(runtime.project, 'film')
    assert loaded.ok
    before = loaded.data['bundle']
    track_ref = 'parent/track:plate'
    clip_ref = 'parent/clip:b-plate'
    original_clip = copy.deepcopy(State(before).entity(clip_ref))
    original_registry = copy.deepcopy(before['parent']['registry'])
    assert State(before).entity(track_ref)['label'] == 'Source'
    assert original_clip['asset'] == 'Q'
    assert original_clip['opaque'] == {'source_token': ['fixture', None, False, 7]}
    assert setup['data']['accepted_revision_id'] == runtime.history()['head']['revision_id']

    def forbidden_legacy_publish(*args, **kwargs):
        pytest.fail('connected frontend used retired publish_parent_composition')

    # The guard applies to the restarted RuntimeService instances too.
    monkeypatch.setattr(type(runtime.service()), 'publish_parent_composition', forbidden_legacy_publish)

    clear = capture_track_set(before, track_ref, clear=['label'])
    if entry == 'python':
        result = session.commit(clear)
    elif entry == 'cli':
        assert run(runtime, 'edit', 'track-label', 'film', '--track', track_ref, '--clear') == 0
        result = json.loads(capsys.readouterr().out)
    else:
        sheet = NativeSheet.export(session)
        edited = edit_cell(sheet.text, track_ref, 'clear', label=True)
        assert sheet.preview(edited).commands == [clear]
        result = sheet.apply(edited, session=session, dry_run=False)

    body = json.loads(runtime.calls[-1][0])
    assert body['commands'] == [clear]
    assert body['source'] == {'via': entry}
    accepted = bundle_from_document(result['data']['canonical_document'])
    assert State(accepted).entity(track_ref).get('label') is None
    assert State(accepted).entity(clip_ref) == original_clip
    assert accepted['parent']['registry'] == original_registry
    assert len(runtime.calls) == 1

    runtime.restart()
    reloaded = runtime.session(name=f'{entry}-reloaded')
    stored = reloaded.client.timelines.open_bundle(
        runtime.project, 'film', revision_id=result['data']['accepted_revision_id'])
    assert stored.ok
    stored_bundle = stored.data['bundle']
    assert State(stored_bundle).entity(track_ref).get('label') is None
    assert State(stored_bundle).entity(clip_ref) == original_clip
    assert stored_bundle['parent']['registry'] == original_registry

    undone = reloaded.undo([result['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert State(restored).entity(track_ref)['label'] == 'Source'
    assert State(restored).entity(clip_ref) == original_clip
    assert restored['parent']['registry'] == original_registry
    assert len(runtime.calls) == 2


def test_http_attached_parent_label_publish_retries_exact_capture_and_inverse(
        runtime, monkeypatch):
    _document, setup = seed_labeled_tiny_cut_parent(runtime)
    runtime.restart()
    session = runtime.session(name='attached-label')
    loaded = session.client.timelines.open_bundle(runtime.project, 'film')
    assert loaded.ok
    before = loaded.data['bundle']
    track_ref = 'parent/track:plate'
    original_track = copy.deepcopy(State(before).entity(track_ref))
    original_clip = copy.deepcopy(State(before).entity('parent/clip:b-plate'))
    command = capture_track_set(before, track_ref, values={'label': 'Renamed'})
    assert session.queue(command) == command
    attached = Checkout.attach(session)
    assert json.loads(session.outbox.path.read_bytes())['pending'] == [command]
    assert setup['data']['accepted_revision_id'] == session._state()['base']['revision_id']

    def forbidden_legacy_publish(*args, **kwargs):
        pytest.fail('attached Checkout.publish used retired publish_parent_composition')

    monkeypatch.setattr(type(runtime.service()), 'publish_parent_composition', forbidden_legacy_publish)
    key = 'attached-label-lost-ack'
    runtime.lost.add(key)
    with pytest.raises(WorkspaceClientError):
        attached.publish('Rename the plate track', idempotency_key=key)

    first = copy.deepcopy(runtime.calls[-1])
    state = json.loads(session.outbox.path.read_bytes())
    assert state['pending'] == [command]
    assert state['flight']['key'] == key
    assert state['flight']['body'].encode() == first[0]
    body = json.loads(first[0])
    assert body['rationale'] == 'Rename the plate track'
    assert body['commands'] == [command]
    assert command['select'] == track_ref
    assert command['resolved'] == [track_ref]
    assert command['bindings']['scalar.target'] == track_ref
    assert command['expect'][track_ref + '::label']['state'] == 'present'

    runtime.restart()
    retry = Checkout.attach(runtime.session(name='attached-label'))
    accepted = retry.publish()
    assert runtime.calls[-1][:2] == first[:2]
    assert accepted['receipt']['idempotency_key'] == key

    runtime.restart()
    reloaded = Checkout.attach(runtime.session(name='attached-label-after-reload'))
    stored = reloaded.session.client.timelines.open_bundle(runtime.project, 'film')
    assert stored.ok
    assert State(stored.data['bundle']).entity(track_ref)['label'] == 'Renamed'
    assert State(stored.data['bundle']).entity('parent/clip:b-plate') == original_clip

    undone = reloaded.undo([accepted['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert State(restored).entity(track_ref) == original_track
    assert State(restored).entity('parent/clip:b-plate') == original_clip
    assert len(runtime.calls) == 3


def test_http_original_attached_add_track_publish_retries_exact_capture_and_guarded_inverse(
        runtime, monkeypatch):
    _document, _target, setup = seed_tiny_cut_parent(runtime)
    runtime.restart()
    session = runtime.session(name='attached-track-create')
    loaded = session.client.timelines.open_bundle(runtime.project, 'film')
    assert loaded.ok
    before = loaded.data['bundle']
    original_tracks = copy.deepcopy(State(before).config('parent')['tracks'])
    assert setup['data']['accepted_revision_id'] == session._state()['base']['revision_id']

    attached = Checkout.attach(session)
    track = add_track(attached, kind='visual', name='Overlay', track_id='overlay')
    command = json.loads(session.outbox.path.read_bytes())['pending'][0]
    assert track == {'id': 'overlay', 'kind': 'visual', 'label': 'Overlay', 'name': 'Overlay'}
    assert command['op'] == 'track.create'
    assert command['select'] == 'parent'
    assert command['args']['row'] == track

    def forbidden_legacy_publish(*args, **kwargs):
        pytest.fail('attached Checkout.publish used retired publish_parent_composition')

    monkeypatch.setattr(type(runtime.service()), 'publish_parent_composition', forbidden_legacy_publish)
    key = 'attached-track-create-lost-ack'
    runtime.lost.add(key)
    with pytest.raises(WorkspaceClientError):
        attached.publish('Create the overlay track', idempotency_key=key)

    first = copy.deepcopy(runtime.calls[-1])
    state = json.loads(session.outbox.path.read_bytes())
    assert state['pending'] == [command]
    assert state['flight']['key'] == key
    assert state['flight']['body'].encode() == first[0]
    body = json.loads(first[0])
    assert body['rationale'] == 'Create the overlay track'
    assert body['commands'] == [command]

    runtime.restart()
    retry = Checkout.attach(runtime.session(name='attached-track-create'))
    accepted = retry.publish()
    assert runtime.calls[-1][:2] == first[:2]
    assert accepted['receipt']['idempotency_key'] == key

    runtime.restart()
    reloaded = Checkout.attach(runtime.session(name='attached-track-create'))
    stored = reloaded.session.client.timelines.open_bundle(runtime.project, 'film')
    assert stored.ok
    stored_bundle = stored.data['bundle']
    assert State(stored_bundle).entity('parent/track:overlay') == track
    assert State(stored_bundle).config('parent')['tracks'] == [*original_tracks, track]

    undone = reloaded.undo([accepted['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert State(restored).config('parent')['tracks'] == original_tracks
    assert State(restored).entity('parent/track:overlay', optional=True) is None
    assert len(runtime.calls) == 3


@pytest.mark.parametrize('entry', ['python', 'cli', 'sheet'])
def test_http_connected_rational_move_is_lossless_across_native_frontends(
        runtime, capsys, entry, monkeypatch):
    _document, setup = seed_rational_tiny_cut_parent(runtime)
    runtime.restart()
    session = runtime.session(name=entry)
    loaded = session.client.timelines.open_bundle(runtime.project, 'film')
    assert loaded.ok
    before = loaded.data['bundle']
    target = State(before).entity(RATIONAL_TARGET)
    original_target = copy.deepcopy(target)
    original_registry = copy.deepcopy(before['parent']['registry'])
    expected = capture_move(before, RATIONAL_TARGET, RATIONAL_LATTICE_SECONDS)
    assert before['parent']['config']['output'] == RATIONAL_FPS
    assert setup['data']['accepted_revision_id'] == runtime.history()['head']['revision_id']
    assert target['asset'] == 'Q'
    assert target['opaque'] == {'retained_source': ['fixture', None, False, 7]}

    def forbidden_legacy_publish(*args, **kwargs):
        pytest.fail('connected frontend used retired publish_parent_composition')

    monkeypatch.setattr(type(runtime.service()), 'publish_parent_composition', forbidden_legacy_publish)

    if entry == 'python':
        result = session.move(RATIONAL_TARGET, RATIONAL_LATTICE_SECONDS)
    elif entry == 'cli':
        assert run(runtime, 'edit', 'film', '--clip', RATIONAL_TARGET,
                   '--on', str(RATIONAL_LATTICE_SECONDS)) == 0
        result = json.loads(capsys.readouterr().out)
    else:
        sheet = NativeSheet.export(session)
        edited = edit_cell(sheet.text, RATIONAL_TARGET, str(RATIONAL_LATTICE_SECONDS))
        assert sheet.preview(edited).commands == [expected]
        result = sheet.apply(edited, session=session, dry_run=False)

    body = json.loads(runtime.calls[-1][0])
    assert body['commands'] == [expected]
    assert result['data']['commands'] == [expected]
    assert body['source'] == {'via': entry}
    assert len(runtime.calls) == 1

    accepted = bundle_from_document(result['data']['canonical_document'])
    moved = State(accepted).entity(RATIONAL_TARGET)
    assert moved['at'] == pytest.approx(RATIONAL_LATTICE_SECONDS)
    assert moved['hold'] == pytest.approx(RATIONAL_LATTICE_SECONDS)
    assert moved['asset'] == original_target['asset']
    assert moved['opaque'] == original_target['opaque']
    assert moved['app']['cut'] == original_target['app']['cut']
    assert moved['app']['layer'] == original_target['app']['layer']
    assert moved['app']['why'] == original_target['app']['why']
    assert accepted['parent']['config']['output'] == RATIONAL_FPS
    assert accepted['parent']['registry'] == original_registry

    start_frame = clip_start_frame(moved, float(RATIONAL_FPS_VALUE))
    end_frame = clip_end_frame(moved, float(RATIONAL_FPS_VALUE))
    assert (start_frame, end_frame) == (30, 60)
    assert list(range(start_frame, end_frame))[-1] == 59
    assert end_frame not in range(start_frame, end_frame)

    history_entry = next(item for item in runtime.history()['items']
                         if item['publication_id'] == result['receipt']['receipt_id'])
    assert [row['command'] for row in history_entry['records']['commands']] == [expected]

    runtime.restart()
    reloaded = runtime.session(name=f'{entry}-reloaded')
    stored = reloaded.client.timelines.open_bundle(
        runtime.project, 'film', revision_id=result['data']['accepted_revision_id'])
    assert stored.ok
    assert persisted_bundle(stored.data['bundle']) == persisted_bundle(accepted)

    undone = reloaded.undo([result['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert persisted_bundle(restored) == persisted_bundle(before)
    redone = reloaded.undo([undone['receipt']['receipt_id']])
    assert redone['data']['canonical_document'] == result['data']['canonical_document']
    assert len(runtime.calls) == 3


@pytest.mark.parametrize('entry', ['python', 'cli', 'sheet'])
def test_http_connected_occurrence_move_preserves_shared_source_across_native_frontends(
        runtime, capsys, entry, monkeypatch):
    _document, setup = seed_shared_occurrence_parent(runtime)
    runtime.restart()
    session = runtime.session(name=entry)
    loaded = session.client.timelines.open_bundle(runtime.project, 'film')
    assert loaded.ok
    before = loaded.data['bundle']
    original_target = copy.deepcopy(State(before).entity(OCCURRENCE_TARGET))
    original_clear = copy.deepcopy(State(before).entity(OCCURRENCE_CLEAR))
    original_sibling = copy.deepcopy(State(before).entity(OCCURRENCE_SIBLING))
    source_pin = occurrence_source_pin(before, OCCURRENCE_TARGET)
    expected = capture_move(before, OCCURRENCE_TARGET, OCCURRENCE_GLOBAL_ON)
    assert setup['data']['accepted_revision_id'] == runtime.history()['head']['revision_id']
    assert source_pin['shot_id'] == 'shot-shared'
    assert source_pin['text_bindings'] == [{'slot': 'voiceover', 'text': 'hello'}]
    assert original_target['app'] == {
        'f2Opaque': {'owner': 'f2', 'nested': [1, {'k': 'v'}]},
    }

    def forbidden_legacy_publish(*args, **kwargs):
        pytest.fail('connected occurrence frontend used retired publish_parent_composition')

    monkeypatch.setattr(type(runtime.service()), 'publish_parent_composition', forbidden_legacy_publish)

    if entry == 'python':
        result = session.move(OCCURRENCE_TARGET, OCCURRENCE_GLOBAL_ON)
    elif entry == 'cli':
        assert run(runtime, 'edit', 'film', '--clip', OCCURRENCE_TARGET,
                   '--on', str(OCCURRENCE_GLOBAL_ON)) == 0
        result = json.loads(capsys.readouterr().out)
    else:
        sheet = NativeSheet.export(session, occurrence='f2-occurrence')
        edited = edit_cell(sheet.text, OCCURRENCE_TARGET, str(OCCURRENCE_GLOBAL_ON))
        assert sheet.preview(edited).commands == [expected]
        result = sheet.apply(edited, session=session, dry_run=False)

    body = json.loads(runtime.calls[-1][0])
    assert body['commands'] == [expected]
    assert result['data']['commands'] == [expected]
    assert body['source'] == {'via': entry}
    assert len(runtime.calls) == 1

    accepted = bundle_from_document(result['data']['canonical_document'])
    moved = State(accepted).entity(OCCURRENCE_TARGET)
    clear = State(accepted).entity(OCCURRENCE_CLEAR)
    sibling = State(accepted).entity(OCCURRENCE_SIBLING)
    assert moved['at'] == OCCURRENCE_GLOBAL_ON
    assert moved['hold'] == original_target['hold']
    assert moved['asset'] == original_target['asset']
    assert moved['app']['f2Opaque'] == original_target['app']['f2Opaque']
    assert clear == original_clear
    assert sibling == original_sibling
    assert occurrence_source_pin(accepted, OCCURRENCE_TARGET)['text_bindings'] == source_pin['text_bindings']
    assert occurrence_source_pin(accepted, OCCURRENCE_SIBLING) == occurrence_source_pin(
        before, OCCURRENCE_SIBLING)

    start_frame = clip_start_frame(moved, 30)
    end_frame = clip_end_frame(moved, 30)
    assert (start_frame, end_frame) == (270, 360)
    assert list(range(start_frame, end_frame))[-1] == 359
    assert end_frame not in range(start_frame, end_frame)

    history_entry = next(item for item in runtime.history()['items']
                         if item['publication_id'] == result['receipt']['receipt_id'])
    record = history_entry['records']['commands'][0]
    source_dependency = next(dep for dep in record['dependencies'] if dep['key'] == 'move.images')
    assert source_dependency['pin']['source']['shot_id'] == source_pin['shot_id']
    assert source_dependency['pin']['source']['revision_id'] == source_pin['revision_id']
    assert source_dependency['pin']['source']['internal_timeline_revision_id'] == (
        source_pin['internal_timeline_revision_id'])

    runtime.restart()
    reloaded = runtime.session(name=f'{entry}-reloaded')
    stored = reloaded.client.timelines.open_bundle(
        runtime.project, 'film', revision_id=result['data']['accepted_revision_id'])
    assert stored.ok
    stored_bundle = stored.data['bundle']
    assert State(stored_bundle).entity(OCCURRENCE_TARGET) == moved
    assert State(stored_bundle).entity(OCCURRENCE_CLEAR) == original_clear
    assert State(stored_bundle).entity(OCCURRENCE_SIBLING) == original_sibling
    assert occurrence_source_pin(stored_bundle, OCCURRENCE_TARGET)['text_bindings'] == (
        source_pin['text_bindings'])

    undone = reloaded.undo([result['receipt']['receipt_id']])
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert State(restored).entity(OCCURRENCE_TARGET) == original_target
    assert State(restored).entity(OCCURRENCE_CLEAR) == original_clear
    assert State(restored).entity(OCCURRENCE_SIBLING) == original_sibling
    assert occurrence_source_pin(restored, OCCURRENCE_TARGET) == source_pin
    redone = reloaded.undo([undone['receipt']['receipt_id']])
    assert redone['data']['canonical_document'] == result['data']['canonical_document']
    assert len(runtime.calls) == 3


@pytest.mark.parametrize('entry', ['python', 'cli', 'sheet-python', 'sheet-cli'])
def test_http_semantic_batch_original_pins_reload_inverse_redo(runtime, tmp_path, capsys, entry):
    seed(runtime)
    session, sheet, path = export(runtime, capsys, tmp_path)
    head = copy.deepcopy(runtime.history()['head'])
    before = session.client.timelines.open_bundle(
        runtime.project, 'film', revision_id=head['revision_id'], publication_id=head['publication_id'],
    ).data['bundle']
    expected = execute(before, raw())
    assert sheet.preview(path.read_text()).commands == expected.commands
    cli_commands = None
    if entry == 'python':
        result = session.commit_many(raw())
    elif entry == 'cli':
        commit_path = detached_commit_file(expected, head, tmp_path)
        cli_commands = emit_document_commands(before, Checkout.load(commit_path).bundle)
        assert run(runtime, 'commit', 'film', '--file', str(commit_path),
                   '--message', 'Native sheet batch', '--idempotency-key', 'sheet-cli-batch') == 0
        result = json.loads(capsys.readouterr().out)
    elif entry == 'sheet-python':
        result = sheet.apply(path.read_text(), session=session, dry_run=False)
    else:
        assert run(runtime, 'apply', 'film', str(path), '--idempotency-key', 'sheet-cli-batch') == 0
        result = json.loads(capsys.readouterr().out)
    body = json.loads(runtime.calls[-1][0])
    assert body['base_revision_id'] == head['revision_id'] and body['base_publication_id'] == head['publication_id']
    if entry == 'cli':
        assert body['commands'] == cli_commands
    else:
        assert body['commands'] == expected.commands
    actual = bundle_from_document(result['data']['canonical_document'])
    assert State(actual).entity(REF) == State(expected.bundle).entity(REF)
    assert len(runtime.history()['items']) == 3
    runtime.restart()
    reloaded = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    stored = reloaded.client.timelines.open_bundle(runtime.project, 'film', revision_id=result['data']['accepted_revision_id'])
    assert State(stored.data['bundle']).entity(REF) == State(expected.bundle).entity(REF)
    undone = reloaded.undo([result['receipt']['receipt_id']])
    assert State(bundle_from_document(undone['data']['canonical_document'])).entity(REF) == State(before).entity(REF)
    redone = reloaded.undo([undone['receipt']['receipt_id']])
    assert redone['data']['canonical_document'] == result['data']['canonical_document']


def test_http_sheet_lost_ack_restart_exact_retry_original_guards_and_aba(runtime, tmp_path, capsys, monkeypatch):
    seed(runtime)
    session, sheet, path = export(runtime, capsys, tmp_path)
    runtime.lost.add('sheet-once')
    assert run(runtime, 'apply', 'film', str(path), '--idempotency-key', 'sheet-once') == 1
    assert 'edited sheet kept' in capsys.readouterr().out
    first = copy.deepcopy(runtime.calls[-1][:2])
    durable = session.outbox.path.read_bytes()
    runtime.restart()
    assert run(runtime, 'apply', 'film', str(path), '--idempotency-key', 'changed') == 1
    assert 'immutable' in capsys.readouterr().out
    assert session.outbox.path.read_bytes() == durable
    before = path.read_text()
    path.write_text(property_cell(before, REF, 'hold', '4.013'))
    assert run(runtime, 'apply', 'film', str(path)) == 1
    assert 'pending intent' in capsys.readouterr().out
    assert session.outbox.path.read_bytes() == durable
    path.write_text(before)

    def no_read(*args, **kwargs):
        pytest.fail('retry must not recapture or read the bundle')

    with monkeypatch.context() as retry_patch:
        retry_patch.setattr(TimelineSession, '_bundle', no_read)
        assert run(runtime, 'apply', 'film', str(path)) == 0
        accepted = json.loads(capsys.readouterr().out)
        assert runtime.calls[-1][:2] == first and len(runtime.history()['items']) == 3
    peer = runtime.session(name='peer')
    peer.commit(label('away'))
    peer.commit(label(''))
    current = copy.deepcopy(runtime.history()['head'])
    reloaded = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    with pytest.raises(CommitConflict) as refused:
        reloaded.undo([accepted['receipt']['receipt_id']], idempotency_key='sheet-undo')
    assert 'intervening' in str(refused.value)
    assert runtime.history()['head'] == current
    assert path.read_text() == before
    assert sheet.preview(path.read_text()).commands == json.loads(first[0])['commands']
