"""Append a placement of an exact, authenticated source occurrence."""
from __future__ import annotations

import copy

from ._command_state import CommandError, State, equal, root_identity
from .capture import number
from .native_occurrence_duplicate import OCCURRENCE_INTENT
from .native_timeline_duplicate import SOURCE_REF

PARAMETERS = {
    'type': 'object', 'required': ['occurrence_id', 'on'],
    'properties': {'occurrence_id': {'type': 'string', 'pattern': r'^(?!.*::)[^/]+$'},
                   'on': {'type': 'number', 'minimum': 0},
                   'source_ref': SOURCE_REF,
                   'source_occurrence_id': {'type': 'string', 'pattern': r'^(?!.*::)[^/]+$'}},
    'dependentRequired': {'source_ref': ['source_occurrence_id'],
                          'source_occurrence_id': ['source_ref']},
    'additionalProperties': False,
}


def _historical_sources(port, records, ancestors=()):
    """Follow selected accepted inverse stages back to their foreign births."""
    from ._collections import AcceptedRecords
    from ._step_undo import entry_records, select_inverse_records, validate_nested_history

    if not isinstance(records, AcceptedRecords):
        if records:
            raise CommandError('shared inverse requires accepted history', kind='history_unavailable')
        return []
    result = []
    for entry in records.entries:
        publication = entry['publication_id']
        if publication in ancestors:
            raise CommandError('cyclic shared source history', kind='history_unavailable')
        validate_nested_history(entry, port.history_entry)
        for record in entry_records(records, entry):
            command = record['command']
            if command['op'] == 'occurrence.place-shared' and 'source_ref' in command['args']:
                result.append((record, records.load_boundary(entry)[1]))
            elif command['op'] == 'timeline.inverse':
                entries = [port.history_entry(identity) for identity in command['args']['publications']]
                if any(source.get('publication_id') != identity or source.get('intent') != 'recorded'
                       for identity, source in zip(command['args']['publications'], entries)):
                    raise CommandError('shared source differs from accepted history', kind='history_unavailable')
                accepted = AcceptedRecords(entries, [item for source in entries
                    for item in source['records']['commands']], records.load_boundary)
                selected = select_inverse_records(accepted, command['args'].get('steps'))
                result.extend(_historical_sources(port, selected, (*ancestors, publication)))
    return result


def _guard_historical_source(record, output, document, row):
    """The accepted birth fixes the owner, source claim and immutable images."""
    from .commit import document_from_bundle

    args = record['command']['args']
    owner = 'occurrence:' + args['occurrence_id']
    claim = {key: args[key] for key in ('source_ref', 'source_occurrence_id')}
    accepted = document_from_bundle(output)
    placed = [item for item in accepted['parent_revision']['payload']['occurrences']
              if item['occurrence_id'] == args['occurrence_id']]
    if (record['command']['select'] != 'parent'
            or record['created'] != [{'ref': owner, 'kind': 'occurrence', 'owner': 'parent'}]
            or [dep['pin'] for dep in record['dependencies'] if dep['key'] == 'shared.foreign'] != [claim]
            or [dep['pin'] for dep in record['dependencies'] if dep['key'] == 'shared.shot'] != [
                {'shot_id': row['shot_id'], 'revision_id': row['shot_revision_id']}]
            or len(placed) != 1
            or (placed[0]['shot_id'], placed[0]['shot_revision_id']) != (row['shot_id'], row['shot_revision_id'])
            or not any(write['ref'] == owner and not write['path']
                       and write['before'] == {'state': 'absent'}
                       and write['after']['state'] == 'present'
                       and write['after']['value']['occurrence_id'] == args['occurrence_id']
                       and write['after']['value']['shot_id'] == row['shot_id']
                       for write in record['writes'])):
        raise CommandError('shared birth differs from accepted source', kind='history_unavailable')
    shot = next(image for image in document['shot_revisions']
                if (image['shot_id'], image['revision_id']) == (row['shot_id'], row['shot_revision_id']))
    internal = next(image for image in document['internal_timeline_revisions']
                    if image['revision_id'] == shot['internal_timeline_revision_id'])
    if (shot not in accepted['shot_revisions'] or internal not in accepted['internal_timeline_revisions']
            or any([dep['pin'] for dep in record['dependencies'] if dep['key'] == 'shared.' + key]
                   != document['dependency_manifest'][key] for key in ('media', 'documents'))):
        raise CommandError('shared physical pins differ from accepted source', kind='history_unavailable')


def resolve_source(port, resolver, inverse_records=()):
    """Capture foreign authority through the same private port as duplication."""
    from .native_timeline_duplicate import resolve_source as resolve_duplicate

    resolver = resolve_duplicate(port, resolver)

    def resolve(ctx):
        from .commit import _hydrate, _require, bundle_from_document

        inputs = resolver(ctx)
        command = ctx.record['command']
        claims = []
        if command['op'] == 'occurrence.place-shared' and 'source_ref' in command['args']:
            claims.append((command['args'], None, None))
        elif command['op'] == 'timeline.inverse':
            claims.extend((record['command']['args'], record, output)
                          for record, output in _historical_sources(port, inverse_records))
        ctx.shared_sources = []
        for claim, record, output in claims:
            ref = claim['source_ref']
            document = port.source_revision_closure(copy.deepcopy(ref))
            source = bundle_from_document(document)
            _require((document['project_id'], document['timeline_id'],
                      document['parent_revision']['revision_id'],
                      document['parent_revision']['content_digest']) ==
                     tuple(ref[key] for key in ('project_id', 'timeline_id', 'revision_id', 'content_digest')),
                     'Runtime returned a different shared source')
            rows = document['parent_revision']['payload']['occurrences']
            matches = [row for row in rows if row['occurrence_id'] == claim['source_occurrence_id']]
            if len(matches) != 1:
                raise CommandError('shared source occurrence is missing or ambiguous', kind='identity_conflict')
            row = matches[0]
            if record is not None:
                _guard_historical_source(record, output, document, row)
            exact = port.shot_closure(row['shot_id'], row['shot_revision_id'])
            shot = next(image for image in document['shot_revisions']
                        if (image['shot_id'], image['revision_id']) ==
                        (row['shot_id'], row['shot_revision_id']))
            internal = next(image for image in document['internal_timeline_revisions']
                            if image['revision_id'] == shot['internal_timeline_revision_id'])
            _require(exact['shot_revision'] == shot and exact['internal_timeline_revision'] == internal,
                     'Runtime returned different shared child images')
            for pin in document['dependency_manifest']['media']:
                port.raw_media_info(copy.deepcopy(pin))
            for captured in _hydrate(port, source):
                if captured not in inputs:
                    inputs.append(captured)
            ctx.shared_sources.append((claim, source, row))
            if record is not None:
                ctx.dependency('shared.foreign', 'authority', {
                    key: copy.deepcopy(claim[key]) for key in ('source_ref', 'source_occurrence_id')})
        return inputs

    return resolve


def install_shared_source(bundle, document, row):
    """Install exact physical compiler inputs without recording a child birth."""
    identity = row['shot_id']
    image = next(image for image in document['shot_revisions']
                 if (image['shot_id'], image['revision_id']) == (identity, row['shot_revision_id']))
    internal = next(child for child in document['internal_timeline_revisions']
                    if child['revision_id'] == image['internal_timeline_revision_id'])
    shot = {'shot_id': identity, 'payload': copy.deepcopy(image['payload']),
            'base_payload': copy.deepcopy(image['payload']),
            'internal_timeline': copy.deepcopy(internal['payload']),
            'base_internal_timeline': copy.deepcopy(internal['payload'])}
    origin = {'shot_id': identity, 'revision_id': image['revision_id'],
              'content_digest': image['content_digest'], 'internal_timeline_id': internal['timeline_id'],
              'internal_timeline_revision_id': internal['revision_id'],
              'internal_timeline_content_digest': internal['content_digest'], 'item_id_map': {}}
    mapping = bundle['source_mapping']
    if identity in bundle['shots'] and (not equal(bundle['shots'][identity], shot)
                                      or not equal(mapping['shots'].get(identity), origin)):
        raise CommandError('shared physical identity has a different revision', kind='identity_conflict')
    bundle['shots'][identity] = copy.deepcopy(shot)
    mapping['shots'][identity] = copy.deepcopy(origin)
    target = mapping['_runtime_document']
    for key, images in (('shot_revisions', [image]), ('internal_timeline_revisions', [internal])):
        for image in images:
            prior = next((item for item in target[key] if item['revision_id'] == image['revision_id']), None)
            if prior is not None and not equal(prior, image):
                raise CommandError('shared immutable image changed', kind='identity_conflict')
            if prior is None:
                target[key].append(copy.deepcopy(image))
    return identity


def _place_foreign(ctx, command):
    args, target = command['args'], ctx.state.bundle
    ref = args['source_ref']
    if (command['select'] != 'parent' or ref['project_id'] != target['project_id']
            or ref['timeline_id'] == target['timeline_id']):
        raise CommandError('shared source and destination scopes disagree', kind='dependency_changed')
    sources = getattr(ctx, 'shared_sources', [])
    if len(sources) != 1:
        raise CommandError('shared placement requires Runtime-authenticated source', kind='dependency_changed')
    _, source, row = sources[0]
    if ctx.read('parent', ['config', 'pinnedShotGroups']) not in (None, []):
        raise CommandError('legacy groups require recorded migration', kind='migration_needed')
    ctx.select('collection.occurrences', 'parent')
    track = row.get('track', row.get('placement', {}).get('track'))
    destination_track = ctx.read('parent/track:' + str(track))
    if not isinstance(destination_track, dict):
        raise CommandError('shared destination track is unavailable', kind='identity_conflict')
    document = source['source_mapping']['_runtime_document']
    source_tracks = document['parent_revision']['payload'].get('config', {}).get('tracks', [])
    source_track = next((item for item in source_tracks if item['id'] == track), None)
    from ._common import AUDIO_KINDS

    if source_track is None or ((source_track.get('kind') in AUDIO_KINDS)
                                != (destination_track.get('kind') in AUDIO_KINDS)):
        raise CommandError('shared destination track is incompatible', kind='identity_conflict')
    identity = install_shared_source(target, document, row)
    created = copy.deepcopy(row)
    created.pop('shot_revision_id', None)
    created.pop('revision_id', None)
    created['occurrence_id'] = args['occurrence_id']
    created['shot_id'] = identity
    created.setdefault('placement', {})['start_ms'] = number(args['on'] * 1000, 'start_ms')
    from astrid.core.timeline.authoring_bundle import _rederive_root_identity

    _rederive_root_identity(created, target['project_id'], target['timeline_id'], row['shot_revision_id'])
    claim = {key: copy.deepcopy(args[key]) for key in ('source_ref', 'source_occurrence_id')}
    ctx.dependency('shared.foreign', 'authority', claim)
    ctx.dependency('shared.shot', 'entity', {'shot_id': identity, 'revision_id': row['shot_revision_id']})
    mapping = target['source_mapping']
    manifest = mapping.setdefault('_copy_manifest', copy.deepcopy(mapping['_runtime_document']['dependency_manifest']))
    for key in ('media', 'documents'):
        for pin in document['dependency_manifest'][key]:
            if pin not in manifest[key]:
                manifest[key].append(copy.deepcopy(pin))
            kind = ('media' if key == 'media' else
                    'transcript' if pin['document_id'].startswith('transcript:') else 'source')
            ctx.dependency('shared.' + key, kind, pin)
    captures = mapping.setdefault('pinned_documents', [])
    for capture in source['source_mapping'].get('pinned_documents', []):
        if capture not in captures:
            captures.append(copy.deepcopy(capture))
    ctx.create_entity('occurrence:' + created['occurrence_id'], kind='occurrence', owner='parent', value=created)


def reopen_shared_source(bundle, owner):
    """Recover an unchanged physical compiler input from this exact closure."""
    state = State(bundle)
    row = state.entity(owner)
    identity = state.entity(owner + '/shot')['shot_id']
    shot = bundle['shots'][row['shot_id']]
    if (not equal(shot['payload'], shot.get('base_payload'))
            or not equal(shot['internal_timeline'], shot.get('base_internal_timeline'))):
        raise CommandError('shared placement requires an unchanged published source',
                           kind='identity_conflict', ref=owner)
    if row['shot_id'] != identity:
        origin = bundle['source_mapping']['shots'][row['shot_id']]
        document = bundle['source_mapping']['_runtime_document']
        image = next(image for image in document['shot_revisions']
                     if (image['shot_id'], image['revision_id']) == (identity, origin['revision_id']))
        internal = next(image for image in document['internal_timeline_revisions']
                        if image['revision_id'] == origin['internal_timeline_revision_id'])
        physical = {'shot_id': identity, 'payload': copy.deepcopy(image['payload']),
                    'base_payload': copy.deepcopy(image['payload']),
                    'internal_timeline': copy.deepcopy(internal['payload']),
                    'base_internal_timeline': copy.deepcopy(internal['payload'])}
        if identity in bundle['shots'] and not equal(bundle['shots'][identity], physical):
            raise CommandError('shared physical source changed', kind='identity_conflict', ref=owner)
        bundle['shots'][identity] = physical
        bundle['source_mapping']['shots'][identity] = {**copy.deepcopy(origin), 'item_id_map': {}}
    return identity


def normalize_shared_roots(bundle, records):
    """Keep native inverse occurrence roots in their authenticated physical spelling."""
    roots = {write['ref'] for record in records for write in record['writes']
             if not write['path'] and write['ref'].startswith('occurrence:')}
    state = State(bundle)
    for owner in roots:
        row = state.entity(owner, optional=True)
        if row is not None:
            row['shot_id'] = reopen_shared_source(bundle, owner)


def prepare_shared_inverse(ctx, records):
    """Normalize only accepted shared-occurrence stages, including their redo input."""
    from ._collections import AcceptedRecords

    if not isinstance(records, AcceptedRecords):
        return
    load_boundary = records.load_boundary
    sources = {binding['ref'] for record in records for binding in record['bindings']
               if binding['key'] == 'shared.source'}
    foreign = bool(getattr(ctx, 'shared_sources', [])) or any(record['command']['op'] == 'occurrence.place-shared'
                  and 'source_ref' in record['command']['args'] for record in records)
    if not sources and not foreign:
        return
    for source in sorted(sources):
        ctx.bind('shared.source', source)
    normalize_shared_roots(ctx.state.bundle, records)

    def normalized_boundary(entry):
        before, after = load_boundary(entry)
        for bundle in (before, after):
            normalize_shared_roots(bundle, records)
        return before, after

    records.load_boundary = normalized_boundary


def occurrence_place_shared(ctx, command):
    if 'source_ref' in command['args']:
        return _place_foreign(ctx, command)
    owner = command['select']
    root = root_identity(owner)
    if root is None or root[0] != 'occurrence':
        raise CommandError('shared placement requires one explicit occurrence', kind='unsupported_ownership')
    if ctx.read('parent', ['config', 'pinnedShotGroups']) not in (None, []):
        raise CommandError('legacy groups require recorded migration', kind='migration_needed')
    ctx.select('collection.occurrences', 'parent')
    row = ctx.read(owner)
    if not isinstance(row, dict):
        raise CommandError('shared source disappeared', kind='identity_conflict', ref=owner)
    ctx.read(owner + '/shot', ['shot_id'])
    ctx.read(owner + '/shot', ['payload'])
    ctx.read(owner + '/source')
    on = number(command['args']['on'], 'on')
    created = copy.deepcopy(row)
    created.pop('shot_revision_id', None)
    created.pop('revision_id', None)
    created['occurrence_id'] = command['args']['occurrence_id']
    created['shot_id'] = reopen_shared_source(ctx.state.bundle, owner)
    created.setdefault('placement', {})['start_ms'] = number(on * 1000, 'start_ms')
    from astrid.core.timeline.authoring_bundle import _rederive_root_identity

    origin = ctx.state.bundle['source_mapping']['shots'][created['shot_id']]
    _rederive_root_identity(created, ctx.state.bundle['project_id'], ctx.state.bundle['timeline_id'],
                            origin['revision_id'])
    ctx.bind('shared.source', owner)
    ctx.create_entity('occurrence:' + created['occurrence_id'], kind='occurrence',
                      owner='parent', value=created)


def capture_occurrence_place_shared(bundle, owner, occurrence_id, *, on):
    """Capture against the source closure; on is seconds and the ID is reserved once."""
    from .commands import execute

    return execute(bundle, [{'schema_version': 1, 'op': 'occurrence.place-shared', 'version': '1',
                             'select': owner, 'args': {'occurrence_id': occurrence_id, 'on': on},
                             'intent': OCCURRENCE_INTENT}]).commands[0]


def foreign_shared_command(source_ref, source_occurrence_id, occurrence_id, *, on):
    """Reserve only a destination occurrence; Runtime captures the child inputs."""
    return {'schema_version': 1, 'op': 'occurrence.place-shared', 'version': '1', 'select': 'parent',
            'args': {'source_ref': copy.deepcopy(source_ref), 'source_occurrence_id': source_occurrence_id,
                     'occurrence_id': occurrence_id, 'on': on},
            'created': ['occurrence:' + occurrence_id]}


def restore_shared_record(bundle, record, output):
    """Replay an accepted birth using its authenticated physical output closure."""
    if record['command']['op'] != 'occurrence.place-shared':
        return
    if 'source_ref' not in record['command']['args']:
        reopen_shared_source(bundle, record['command']['select'])
        return
    from .commit import document_from_bundle

    document = document_from_bundle(output)
    identity = record['command']['args']['occurrence_id']
    row = next(row for row in document['parent_revision']['payload']['occurrences']
               if row['occurrence_id'] == identity)
    install_shared_source(bundle, document, row)
