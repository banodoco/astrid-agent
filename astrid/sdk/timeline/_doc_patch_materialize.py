"""Retain physical ownership when projecting shared payload patches to main's compiler."""
from __future__ import annotations

import copy

from astrid.core.timeline.authoring_bundle import (
    _digest,
    _identity,
    _remap_known_item_references,
)


def remember(closure, physical, *, only_if_needed=False):
    mapping = closure.bundle['source_mapping']
    targets = mapping.setdefault('_doc_patch_images', {})
    owners = set()
    for owner, local in closure.owners[physical]:
        shot = closure.bundle['shots'][local]
        origin = mapping['shots'].get(local)
        if origin is None:
            continue  # Semantic allocations retain the compiler's established identities.
        owners.add(owner)
        targets[owner] = {
            'shot_id': origin['shot_id'], 'internal_timeline_id': origin['internal_timeline_id'],
            'source_shot_revision': origin['revision_id'],
            'source_internal_revision': origin['internal_timeline_revision_id'],
            'payload': copy.deepcopy(shot['payload']), 'internal_timeline': copy.deepcopy(shot['internal_timeline']),
        }
    if only_if_needed and owners:
        from .commit import document_from_bundle

        retained = {owner: image for owner, image in targets.items() if owner not in owners}
        source = {**mapping}
        if retained:
            source['_doc_patch_images'] = retained
        else:
            source.pop('_doc_patch_images')
        candidate = {**closure.bundle, 'source_mapping': source}
        # This cache supplies compiler projection, never historical authority.
        # Drop only an override whose complete canonical closure is identical.
        if document_from_bundle(candidate) == document_from_bundle(closure.bundle):
            if retained:
                mapping['_doc_patch_images'] = retained
            else:
                mapping.pop('_doc_patch_images')


def project(bundle, publication):
    targets = bundle['source_mapping'].get('_doc_patch_images')
    if not targets:
        return publication
    result = copy.deepcopy(publication)
    originals = bundle['source_mapping']['_runtime_document']
    source_shots = {row['revision_id']: row for row in originals['shot_revisions']}
    source_internals = {row['revision_id']: row for row in originals['internal_timeline_revisions']}
    shot_index = {row['revision_id']: row for row in result['shot_revisions']}
    internal_index = {row['revision_id']: row for row in result['internal_timeline_revisions']}
    shots, internals = {}, {}
    placements = {row['occurrence_id']: row for row in bundle['placements']}
    internal_groups = {}
    for row in result['parent_composition']['occurrences']:
        owner = 'occurrence:' + row['occurrence_id']
        target = targets.get(owner)
        local = placements[row['occurrence_id']]['shot_id']
        authored = bundle['shots'][local]
        # A later semantic edit is occurrence-owned; its compiler projection wins.
        if (target is None or _digest(authored['payload']) != _digest(target['payload'])
                or _digest(authored['internal_timeline']) != _digest(target['internal_timeline'])):
            continue
        origin = bundle['source_mapping']['shots'][local]
        mapping = origin.get('item_id_map', {})
        payload = _remap_known_item_references(authored['payload'], {v: k for k, v in mapping.items()})
        if _digest(authored['internal_timeline']) == _digest(authored['base_internal_timeline']):
            child = source_internals.get(target['source_internal_revision'])
        else:
            group = target['source_internal_revision'], _digest(authored['internal_timeline'])
            child = internal_groups.get(group)
            if child is None:
                child = {'revision_id': _identity('authoring-timeline-revision', bundle['project_id'],
                            bundle['timeline_id'], min(value['shot_id'] for value in targets.values()
                                if value['source_internal_revision'] == target['source_internal_revision']),
                            authored['internal_timeline']),
                         'timeline_id': target['internal_timeline_id'],
                         'payload': copy.deepcopy(authored['internal_timeline']),
                         'content_digest': _digest(authored['internal_timeline'])}
                internal_groups[group] = child
        if child is None:
            continue
        payload['internal_timeline_revision_id'] = child['revision_id']
        old = source_shots.get(target['source_shot_revision'])
        if old is not None and _digest(old['payload']) == _digest(payload):
            image = copy.deepcopy(old)
        else:
            image = {'shot_id': target['shot_id'],
                     'revision_id': _identity('authoring-shot-revision', bundle['project_id'], target['shot_id'], payload),
                     'internal_timeline_revision_id': child['revision_id'],
                     'payload': payload, 'content_digest': _digest(payload)}
        shots[image['revision_id']], internals[child['revision_id']] = image, copy.deepcopy(child)
        row['shot_id'], row['shot_revision_id'] = image['shot_id'], image['revision_id']
    referenced = {row['shot_revision_id'] for row in result['parent_composition']['occurrences']}
    shot_index.update(shots)
    shot_index.update((key, source_shots[key]) for key in referenced if key not in shot_index)
    result['shot_revisions'] = [shot_index[key] for key in sorted(referenced)]
    internal_refs = {row['internal_timeline_revision_id'] for row in result['shot_revisions']}
    internal_index.update(internals)
    internal_index.update((key, source_internals[key]) for key in internal_refs if key not in internal_index)
    result['internal_timeline_revisions'] = [internal_index[key] for key in sorted(internal_refs)]
    return result
