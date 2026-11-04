"""Copy one exact Runtime-authenticated source closure into an empty timeline."""
from __future__ import annotations

import copy

from ._command_state import ABSENT, CommandError, equal, present

SOURCE_REF = {
    'type': 'object', 'additionalProperties': False,
    'required': ['project_id', 'timeline_id', 'revision_id', 'publication_id', 'content_digest'],
    'properties': {key: {'type': 'string', 'minLength': 1} for key in
                   ('project_id', 'timeline_id', 'revision_id', 'publication_id', 'content_digest')},
}
PARAMETERS = {
    'type': 'object', 'additionalProperties': False,
    'required': ['source_ref', 'shots', 'occurrences'],
    'properties': {'source_ref': SOURCE_REF, **{key: {
        'type': 'object', 'additionalProperties': {'type': 'string', 'minLength': 1}}
        for key in ('shots', 'occurrences')}},
}


def resolve_source(port, resolver):
    """The private port, never command bytes, supplies the source image."""
    def resolve(ctx):
        inputs = resolver(ctx)
        command = ctx.record['command']
        if command['op'] != 'timeline.duplicate':
            return inputs
        from .commit import _hydrate, _require, bundle_from_document

        ref = command['args']['source_ref']
        document = port.revision_closure(copy.deepcopy(ref))
        source = bundle_from_document(document)
        image = document['parent_revision']
        _require((document['project_id'], document['timeline_id'], image['revision_id'], image['content_digest']) ==
                 (ref['project_id'], ref['timeline_id'], ref['revision_id'], ref['content_digest']),
                 'Runtime returned a different source closure')
        for pin in document['dependency_manifest']['media']:
            port.raw_media_info(copy.deepcopy(pin))
        for captured in _hydrate(port, source):
            if captured not in inputs:
                inputs.append(captured)
        ctx.source_copy = source
        return inputs
    return resolve


def duplicate(ctx, command):
    from astrid.core.timeline.authoring_bundle import _copy_shot_for_placement

    args = command['args']
    source = getattr(ctx, 'source_copy', None)
    if source is None:
        raise CommandError('duplicate requires Runtime-authenticated source input', kind='dependency_changed')
    target = ctx.state.bundle
    ref = args['source_ref']
    if (ref['project_id'] != target['project_id'] or ref['timeline_id'] == target['timeline_id']
            or command['select'] != 'parent'):
        raise CommandError('duplicate source and destination scopes disagree', kind='dependency_changed')
    ctx.read('timeline')
    ctx.dependency('copy.source', 'authority', ref)
    if target['placements'] or target['shots']:
        raise CommandError('duplicate requires an empty destination', kind='identity_collision')
    document = source['source_mapping']['_runtime_document']
    rows = document['parent_revision']['payload']['occurrences']
    shots = {row['shot_id']: row for row in document['shot_revisions']}
    if len(shots) != len(document['shot_revisions']):
        raise CommandError('source has multiple revisions of one shot', kind='identity_conflict')
    for key, identities in (('shots', set(shots)), ('occurrences', {row['occurrence_id'] for row in rows})):
        mapping = args[key]
        if (set(mapping) != identities or len(set(mapping.values())) != len(mapping)
                or set(mapping.values()) & identities):
            raise CommandError('copy requires complete fresh injective identity maps', kind='identity_collision')
    parent = copy.deepcopy(document['parent_revision']['payload'])
    internals = {row['revision_id']: row['payload'] for row in document['internal_timeline_revisions']}
    parent.pop('occurrences')
    # Config follows registry aliases so guarded aggregate inversion observes
    # the exact authored config rather than synthesizing a registry mirror.
    for key in sorted(set(target['parent']) | set(parent), key=lambda key: (key == 'config', key)):
        before = ctx.state.get('parent', [key])
        after = present(parent[key]) if key in parent else ABSENT
        if not equal(before, after):
            ctx.assign('parent', [key], after)
    for identity, shot in sorted(shots.items()):
        occurrence = next(row['occurrence_id'] for row in rows if row['shot_id'] == identity)
        payload, _ = _copy_shot_for_placement(
            shot['payload'], project_id=target['project_id'], timeline_id=target['timeline_id'],
            occurrence_id=args['occurrences'][occurrence], authoring_shot_id=args['shots'][identity],
            source_shot_id=identity)
        payload.pop('internal_timeline_revision_id', None)
        internal = adopt_parent_assets(internals[shot['internal_timeline_revision_id']], parent)
        ctx.create_entity('shot:' + args['shots'][identity], kind='shot', owner=None,
                          value={'payload': payload, 'internal_timeline': internal})
    for row in rows:
        copied = copy.deepcopy(row)
        copied.pop('shot_revision_id', None)
        copied.pop('revision_id', None)
        copied['shot_id'] = args['shots'][row['shot_id']]
        copied['occurrence_id'] = args['occurrences'][row['occurrence_id']]
        ctx.create_entity('occurrence:' + copied['occurrence_id'], kind='occurrence', owner='parent', value=copied)
    if any('stable_deep_link' in row or 'output_identity' in row for row in rows):
        from astrid.core.timeline.authoring_bundle import compile_authoring_candidate

        compiled = compile_authoring_candidate(target).publication['parent_composition']['occurrences']
        for row in compiled:
            owner = 'occurrence:' + row['occurrence_id']
            for key in ('stable_deep_link', 'output_identity'):
                if key in row and not equal(ctx.state.get(owner, [key]), present(row[key])):
                    ctx.write(owner, [key], row[key])
    # Preserve retained inputs as well as those directly referenced by payloads.
    mapping = target['source_mapping']
    mapping['pinned_documents'] = copy.deepcopy(source['source_mapping'].get('pinned_documents', []))
    mapping['_copy_manifest'] = copy.deepcopy(document['dependency_manifest'])
    for pin in document['dependency_manifest']['media']:
        ctx.dependency('copy.media:' + pin['media_id'], 'media', pin)
    for capture in mapping['pinned_documents']:
        kind = 'transcript' if capture['document']['kind'] == 'media.transcript' else 'source'
        ctx.dependency('copy.document:' + capture['pin']['document_id'], kind, capture['pin'])


def copy_command(source_ref, shots, occurrences):
    """Reserve destination roots; Runtime/provider verify every submitted claim."""
    return {'schema_version': 1, 'op': 'timeline.duplicate', 'version': '1', 'select': 'parent',
            'args': {'source_ref': copy.deepcopy(source_ref), 'shots': dict(shots), 'occurrences': dict(occurrences)},
            'created': [*('shot:' + shots[key] for key in sorted(shots)),
                        *('occurrence:' + identity for identity in occurrences.values())]}


def adopt_parent_assets(internal, parent):
    """Materialize authenticated parent asset aliases used by a copied child timeline.

    Some accepted closures keep a selected asset in the parent registry while
    the child timeline's own registry omits it. That fallback is valid for the
    source, but a new shot no longer has the source closure as its authority.
    Copy only referenced aliases into the child registry so the duplicate is
    self-contained while retaining the authenticated media identity.
    """
    from astrid.core.timeline.parent_registry import effective_parent_registry

    result = copy.deepcopy(internal)
    parent_assets = effective_parent_registry(parent).get('assets', {})
    if not isinstance(parent_assets, dict):
        return result
    registry = result.setdefault('registry', {})
    assets = registry.setdefault('assets', {})
    for clip in result.get('clips') or []:
        key = clip.get('asset', clip.get('asset_id'))
        if isinstance(key, str) and key not in assets and key in parent_assets:
            assets[key] = copy.deepcopy(parent_assets[key])
    return result


def physical_content(bundle):
    """Recover physical sharing from authenticated closure links, never alias equality."""
    document = bundle.get('source_mapping', {}).get('_runtime_document')
    if document is None:
        return bundle
    result = copy.deepcopy(bundle)
    result['parent'] = copy.deepcopy(document['parent_revision']['payload'])
    result['placements'] = result['parent'].pop('occurrences')
    internals = {row['revision_id']: row for row in document['internal_timeline_revisions']}
    result['shots'] = {}
    result['source_mapping']['shots'] = {}
    for image in document['shot_revisions']:
        identity = image['shot_id']
        internal = internals[image['internal_timeline_revision_id']]
        result['source_mapping']['shots'][identity] = {
            'shot_id': identity, 'revision_id': image['revision_id'],
            'content_digest': image['content_digest'], 'internal_timeline_id': internal['timeline_id'],
            'internal_timeline_revision_id': internal['revision_id'],
            'internal_timeline_content_digest': internal['content_digest'], 'item_id_map': {}}
        result['shots'][identity] = {
            'shot_id': identity, 'payload': copy.deepcopy(image['payload']),
            'base_payload': copy.deepcopy(image['payload']),
            'internal_timeline': copy.deepcopy(internal['payload']),
            'base_internal_timeline': copy.deepcopy(internal['payload'])}
    for row in result['placements']:
        row.pop('shot_revision_id', None)
        row.pop('revision_id', None)
    result['base_placements'] = {row['occurrence_id']: copy.deepcopy(row) for row in result['placements']}
    result['source_mapping']['placements'] = copy.deepcopy(result['base_placements'])
    result['base_parent_payload'] = copy.deepcopy(result['parent'])
    return result
