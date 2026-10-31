"""Independent copy of one canonical placement under explicit reserved identities."""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import replace

from ._command_state import CommandError, root_identity

OCCURRENCE_INTENT = {'ownership': 'occurrence'}


def remap_copy_cuts(internal, occurrence_id):
    """Give the independent copy its own cuts and retarget its local moments."""
    from astrid.core.timeline import moments
    from astrid.sdk import timeline_intent as intent

    result = copy.deepcopy(internal)
    cuts = {intent.cut_of(clip) for clip in result.get('clips', [])} - {None}
    mapping = {cut: 'c' + str(int(hashlib.sha256(
        (occurrence_id + '\0' + cut).encode()).hexdigest(), 16)) for cut in cuts}

    def remap_moment(moment):
        base = remap_moment(moment.base) if moment.base else None
        cut = moment.cut
        if cut:
            # Moment spelling permits zero-padded aliases for the same cut.
            def canonical(value):
                match = re.fullmatch(r'c0*(\d+)([a-z]?)', value.lower())
                return (int(match.group(1)), match.group(2)) if match else None

            if cut in mapping:
                cut = mapping[cut]
            else:
                alias = canonical(cut)
                matches = [new for old, new in mapping.items()
                           if alias is not None and canonical(old) == alias]
                if len(matches) == 1:
                    cut = matches[0]
        return replace(moment, cut=cut, base=base)

    for clip in result.get('clips', []):
        cut = intent.cut_of(clip)
        if cut:
            intent.set_cut(clip, mapping[cut])
        for read, write in ((intent.on, intent.set_on), (intent.until, intent.set_until)):
            value = read(clip)
            if value:
                try:
                    parsed = moments.parse(value)
                except moments.MomentError:
                    continue
                updated = remap_moment(parsed)
                if updated != parsed:
                    write(clip, str(updated))
    return result


def occurrence_duplicate(ctx, command):
    owner = command['select']
    if root_identity(owner) is None or not owner.startswith('occurrence:'):
        raise CommandError('duplicate requires one explicit occurrence', kind='unsupported_ownership')
    if ctx.read('parent', ['config', 'pinnedShotGroups']) not in (None, []):
        raise CommandError('legacy groups require recorded migration', kind='migration_needed')
    row = ctx.read(owner)
    if not isinstance(row, dict):
        raise CommandError('duplicate source disappeared', kind='identity_conflict', ref=owner)
    source_shot = ctx.read(owner + '/shot', ['shot_id'])
    ctx.select('collection.clips', owner)
    ctx.select('collection.tracks', owner)
    internal = ctx.read(owner + '/source')
    payload = ctx.read(owner + '/shot', ['payload'])
    occurrence_id, shot_id = command['args']['occurrence_id'], command['args']['shot_id']
    if occurrence_id == row['occurrence_id'] or shot_id == source_shot:
        raise CommandError('duplicate requires reserved identities distinct from the source',
                           kind='identity_collision', ref=owner)
    copy_child(ctx, owner, occurrence_id, shot_id, source_shot, payload,
               remap_copy_cuts(internal, occurrence_id))
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate

    bundle = ctx.state.bundle
    ctx.bind('duplicate.owner', owner)
    created = copy.deepcopy(row)
    created.pop('shot_revision_id', None)
    created.pop('revision_id', None)
    created.update(occurrence_id=occurrence_id, shot_id=shot_id)
    fields = [key for key in ('stable_deep_link', 'output_identity') if key in created]
    if fields and ctx.state.entity('occurrence:' + occurrence_id, optional=True) is None:
        # Record the same factory-derived destination values as immutable compilation.
        candidate = copy.deepcopy(bundle)
        candidate['placements'].append(created)
        compiled = compile_authoring_candidate(candidate).publication['parent_composition']['occurrences'][-1]
        created.update({key: compiled[key] for key in fields})
    ctx.create_entity('occurrence:' + occurrence_id, kind='occurrence', owner='parent', value=created)


def copy_child(ctx, owner, occurrence_id, shot_id, source_shot, payload, internal):
    """Allocate a native child copy without creating or replacing an occurrence."""
    from astrid.core.timeline.authoring_bundle import _copy_shot_for_placement

    bundle = ctx.state.bundle
    copied, remap = _copy_shot_for_placement(
        payload, project_id=bundle['project_id'], timeline_id=bundle['timeline_id'],
        occurrence_id=occurrence_id, authoring_shot_id=shot_id, source_shot_id=source_shot)
    occupied = {item.get('item_id', item.get('id')) for shot in bundle['shots'].values()
                for item in shot['payload'].get('items', [])}
    if occupied & set(remap.values()):
        raise CommandError('duplicate item identity is occupied', kind='identity_collision', ref=owner)
    ctx.create_entity('shot:' + shot_id, kind='shot', owner=None,
                      value={'payload': copied, 'internal_timeline': copy.deepcopy(internal)})
    document = bundle['source_mapping'].get('_runtime_document')
    if document is not None:
        from astrid.core.timeline.authoring_bundle import _CopiedShot

        source = next((row for row in document['parent_revision']['payload']['occurrences']
                       if row['occurrence_id'] == owner.removeprefix('occurrence:')), None)
        if source is not None and source['shot_id'] == source_shot:
            bundle['shots'][shot_id] = _CopiedShot(bundle['shots'][shot_id], {
                'source_occurrence_id': source['occurrence_id'], 'source_shot_id': source_shot,
                'source_revision_id': source['shot_revision_id'], 'occurrence_id': occurrence_id,
                'shot_id': shot_id})


def capture_occurrence_duplicate(bundle, owner, occurrence_id, shot_id):
    """Capture a copy of the raw canonical owner with caller-reserved target identities."""
    from .commands import execute

    return execute(bundle, [{'schema_version': 1, 'op': 'occurrence.duplicate', 'version': '1',
                             'select': owner, 'args': {'occurrence_id': occurrence_id, 'shot_id': shot_id},
                             'intent': OCCURRENCE_INTENT}]).commands[0]
