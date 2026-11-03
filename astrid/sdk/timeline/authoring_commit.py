"""Translate a detached authoring candidate onto its authenticated commit base."""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from astrid.core.timeline.authoring_bundle import (
    AuthoringBundleError,
    CandidateCompilation,
    compile_authoring_candidate,
)


def response_data(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthoringBundleError('Runtime response must be an object')
    value = value.get('data', value)
    if not isinstance(value, Mapping):
        raise AuthoringBundleError('Runtime response data must be an object')
    return copy.deepcopy(dict(value))


def _read_base(writer: Any, candidate: Mapping[str, Any]) -> dict[str, Any]:
    from .commit import (
        INTERNAL_IMAGE_FIELDS,
        PARENT_IMAGE_FIELDS,
        SHOT_IMAGE_FIELDS,
        bundle_from_document,
    )

    project, timeline = candidate['project_id'], candidate['timeline_id']
    pin = candidate['base_parent']
    cursor = None
    seen: set[str] = set()
    matches: list[dict[str, Any]] = []
    while True:
        page = response_data(writer.list_timeline_commits(project, timeline, cursor=cursor, limit=100))
        matches.extend(row for row in page['items'] if row['revision_id'] == pin['revision_id']
                       and ('publication_id' not in pin or row['publication_id'] == pin['publication_id']))
        cursor = page.get('next_cursor')
        if cursor is None:
            break
        if cursor in seen:
            raise AuthoringBundleError('Runtime history cursor repeated')
        seen.add(cursor)
    if len(matches) != 1:
        raise AuthoringBundleError('candidate requires one exact original revision/publication pair')
    entry = matches[0]
    if entry['receipt']['receipt_id'] != entry['publication_id']:
        raise AuthoringBundleError('Runtime history receipt differs from the original publication')
    parent = response_data(writer.get_project_parent_composition_revision(project, timeline, pin['revision_id']))
    if (parent.get('project_id'), parent.get('timeline_id'), parent.get('revision_id'), parent.get('content_digest')) != (
            project, timeline, pin['revision_id'], pin['content_digest']):
        raise AuthoringBundleError('Runtime parent differs from the candidate scope or pin')
    manifest = parent['dependency_manifest']
    shots, internals = {}, {}
    owners = {row['revision_id']: row['timeline_id'] for row in manifest['internal_timelines']}
    for occurrence in parent['payload']['occurrences']:
        revision = occurrence['shot_revision_id']
        shot = response_data(writer.get_project_shot_revision(project, occurrence['shot_id'], revision))
        if (shot.get('project_id'), shot.get('shot_id'), shot.get('revision_id')) != (project, occurrence['shot_id'], revision):
            raise AuthoringBundleError('Runtime shot differs from its exact scope or pin')
        shots[revision] = {key: shot[key] for key in SHOT_IMAGE_FIELDS}
        revision = shot['internal_timeline_revision_id']
        owner = owners[revision]
        internal = response_data(writer.get_project_timeline_revision(project, owner, revision))
        if (internal.get('project_id'), internal.get('timeline_id'), internal.get('revision_id')) != (project, owner, revision):
            raise AuthoringBundleError('Runtime internal timeline differs from its exact owner or pin')
        internals[revision] = {key: internal[key] for key in INTERNAL_IMAGE_FIELDS}
    document = {'schema_version': 1, 'project_id': project, 'timeline_id': timeline,
        'parent_revision': {key: parent[key] for key in PARENT_IMAGE_FIELDS},
        'shot_revisions': sorted(shots.values(), key=lambda row: (row['shot_id'], row['revision_id'])),
        'internal_timeline_revisions': sorted(internals.values(), key=lambda row: (row['timeline_id'], row['revision_id'])),
        'dependency_manifest': manifest}
    recorded = entry['receipt']['result'].get('canonical_document')
    if recorded is not None and recorded != document:
        raise AuthoringBundleError('Runtime receipt differs from the immutable closure')
    base = bundle_from_document(document)
    base['base_parent']['publication_id'] = entry['publication_id']
    return base


def authoring_commit_request(candidate: Mapping[str, Any], writer: Any, *,
                             compilation: CandidateCompilation | None = None) -> dict[str, Any]:
    from ._command_state import equal
    from .commands import execute
    from .emitter import emit_document_commands
    from .native_media_admission import registry_selections

    if compilation is None:
        compilation = compile_authoring_candidate(candidate)
    base = _read_base(writer, candidate)
    publication_id = base['base_parent']['publication_id']
    work = copy.deepcopy(dict(candidate))
    # These two missing transport pins may be hydrated only from Runtime. All
    # supplied baseline/provenance fields remain checked by the normal emitter.
    work['base_parent'].setdefault('publication_id', base['base_parent']['publication_id'])
    work['source_mapping'].setdefault('_runtime_document', copy.deepcopy(base['source_mapping']['_runtime_document']))
    base, duplicates = _materialize_duplicates(base, work)
    placements = work['placements']
    originals = {row['occurrence_id']: row for row in base['placements']}
    if any(row['occurrence_id'] not in originals
           or row['shot_id'] != originals[row['occurrence_id']]['shot_id'] for row in placements):
        raise AuthoringBundleError('closure membership edits require registered semantic commands')
    retained = {row['occurrence_id'] for row in placements}
    removals = [{'schema_version': 1, 'op': 'occurrence.delete', 'version': '1',
                 'select': 'occurrence:' + identity, 'args': {}, 'intent': {'ownership': 'occurrence'}}
                for identity in originals if identity not in retained]
    commands: list[dict[str, Any]] = list(duplicates)
    if removals:
        plan = execute(base, removals)
        commands.extend(plan.commands)
        base = plan.bundle
    # Publication contains only placed shots. Keep the exact surviving source
    # mappings and baselines so filtering cannot hide a reachable forged root.
    reachable = {row['shot_id'] for row in placements}
    for bundle in (base, work):
        bundle['shots'] = {key: row for key, row in bundle['shots'].items() if key in reachable}
        mapping = bundle['source_mapping']
        mapping['shots'] = {key: row for key, row in mapping['shots'].items() if key in reachable}
        mapping['placements'] = {key: row for key, row in mapping['placements'].items() if key in retained}
    base, work, forks = _fork_shared_aliases(base, work)
    commands.extend(forks)
    semantic_count = len(commands)
    placements = work['placements']
    # List guards use published physical edges, not local independent-shot IDs.
    placement_commands = physical_occurrence_commands(base, placements)
    work['placements'] = copy.deepcopy(base['placements'])
    payload_commands = emit_document_commands(base, work)
    commands.extend(placement_commands)
    commands.extend(payload_commands)
    if not commands:
        # Reaffirm the authored track collection with an ordinary previous-value
        # guard. Runtime still owns publication, stale-head checks and replay.
        path = ['config', 'tracks'] if 'tracks' in base['parent']['config'] else ['config']
        value = base['parent']['config'].get('tracks') if len(path) == 2 else base['parent']['config']
        commands.append({'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
                         'args': {'changes': [{'op': 'set', 'ref': 'parent', 'path': path,
                             'before': {'state': 'present', 'value': copy.deepcopy(value)},
                             'value': copy.deepcopy(value)}]}})
    source = base['source_mapping']['_runtime_document']
    inputs = [{'pin': copy.deepcopy(pin)} for pin in compilation.publication['dependency_manifest']['media']
              if pin not in source['dependency_manifest']['media']]
    old_selections = list(registry_selections(base))
    for association in registry_selections(work):
        if association in old_selections:
            continue
        selected = response_data(writer.get_project_media_selection(candidate['project_id'],
            {key: association[key] for key in ('asset', 'generation_id', 'variant_id')}))
        if not equal({key: selected.get(key) for key in association}, association):
            raise AuthoringBundleError('Runtime catalog association differs from the candidate media')
        pin = {'media_id': selected['object_id'], 'content_digest': selected['object_id']}
        value = {'pin': pin, 'selection': selected}
        inputs = [row for row in inputs if row != {'pin': pin}]
        if value not in inputs:
            inputs.append(value)
    steps = [{'id': 'authoring', 'label': 'Publish authoring candidate', 'intent': '', 'tags': [],
        'command_ordinals': list(range(semantic_count, len(commands))),
        'patch': [copy.deepcopy(change) for call in commands if call['op'] == 'doc.patch'
                  for change in call['args']['changes']]}]
    if semantic_count:
        steps.insert(0, {'id': 'structure', 'label': 'Materialize authoring structure',
            'intent': '', 'tags': [], 'command_ordinals': list(range(semantic_count)), 'patch': []})
        if not steps[-1]['command_ordinals']:
            steps.pop()
    if inputs:
        # Runtime captures all inputs before execution. Record consumption at
        # the final boundary, where these pins are in the resulting closure.
        steps.append({'id': 'media', 'label': 'Capture authoring media', 'intent': '', 'tags': [],
                      'command_ordinals': [len(commands)], 'patch': []})
        commands.append({'schema_version': 1, 'op': 'media.admit', 'version': '1',
                         'select': None, 'args': {'inputs': inputs}})
    return {'api_version': 1, 'project_id': candidate['project_id'], 'timeline_id': candidate['timeline_id'],
        'base_revision_id': base['base_parent']['revision_id'],
        'base_publication_id': publication_id, 'commands': commands,
        'label': 'Publish authoring candidate', 'steps': steps, 'merge': 'atomic', 'source': {'via': 'python'},
        'rationale': 'Publish authoring candidate ' + compilation.candidate_digest}


def physical_occurrence_commands(bundle: Mapping[str, Any],
                                 placements: list[Mapping[str, Any]] | None = None
                                 ) -> list[dict[str, Any]]:
    """Lower occurrence order and placement edits to guarded physical patches.

    Authoring bundles use local shot aliases. Runtime occurrences instead name
    exact physical shot revisions, so carry those two identity fields forward
    from the current authenticated closure while diffing the caller's ordered
    placement rows. This lets a function stage occurrence edits without
    weakening the emitter's closure ownership guard.

    ``bundle`` must be the current command bundle before the placement change;
    callers should capture preceding free edits before invoking this helper.
    Membership changes remain semantic commands and are rejected here. Field
    changes and collection order changes are separate sequential commands so
    the inverse records each exact boundary.
    """
    from ._doc_patch import Closure
    from .emitter import _diff

    current = Closure(bundle).document['parent_revision']['payload']['occurrences']
    desired = list(bundle['placements'] if placements is None else placements)
    current_by_id = {row['occurrence_id']: row for row in current}
    local_by_id = {row['occurrence_id']: row for row in bundle['placements']}
    desired_ids = [row.get('occurrence_id') for row in desired]
    if (len(current_by_id) != len(current)
            or any(not isinstance(identity, str) or not identity for identity in desired_ids)
            or len(set(desired_ids)) != len(desired_ids)
            or set(desired_ids) != set(current_by_id)):
        raise AuthoringBundleError('occurrence placement edits cannot change closure membership')
    if any(row.get('shot_id') != local_by_id[row['occurrence_id']].get('shot_id') for row in desired):
        raise AuthoringBundleError('occurrence placement edits cannot change shot ownership')
    target = [{**dict(row), **{key: current_by_id[row['occurrence_id']][key]
                               for key in ('shot_id', 'shot_revision_id')}}
              for row in desired]
    changes: list[dict[str, Any]] = []
    _diff(current, target, 'parent', ('occurrences',), changes)
    if not changes:
        return []
    # A list.move records the whole occurrence collection. Apply field edits
    # first so the final collection write captures their final values too;
    # otherwise immediate inverse sees the moved order with stale timing in
    # the aggregate guard. The move still uses its original sequential order
    # guard, but its row operand includes the preceding field edits.
    moves = [change for change in changes if change['op'] == 'list.move']
    fields = [change for change in changes if change['op'] != 'list.move']
    target_by_id = {row['occurrence_id']: row for row in target}
    for change in moves:
        change['before'] = {'state': 'present', 'value': copy.deepcopy(target_by_id[change['id']])}

    def command(group):
        return {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
                'args': {'changes': group}}

    # Keep collection order separate from timing fields so the inverse sees
    # complete timing at its aggregate boundary before restoring row order.
    result = []
    if fields:
        result.append(command(fields))
    if moves:
        result.append(command(moves))
    return result


def _materialize_duplicates(base: dict[str, Any], work: dict[str, Any]) -> tuple[
        dict[str, Any], list[dict[str, Any]]]:
    from astrid.core.timeline.authoring_bundle import (
        _remap_known_item_references,
        _validate_placement_capabilities,
    )
    from astrid.sdk.timeline_editing import add_authoring_shot

    from ._command_state import equal
    from .commands import execute, validate_command

    reservations = work.pop('_authoring_duplicates', [])
    if not isinstance(reservations, list):
        raise AuthoringBundleError('duplicate reservations must be a list')
    commands = []
    for reservation in reservations:
        if not isinstance(reservation, dict) or set(reservation) != {'command', 'start_ms'}:
            raise AuthoringBundleError('invalid duplicate reservation')
        call = reservation['command']
        if (not isinstance(call, dict) or call.get('op') != 'occurrence.duplicate'
                or set(call) != {'schema_version', 'op', 'version', 'select', 'args', 'intent'}):
            raise AuthoringBundleError('expected an occurrence.duplicate reservation')
        validate_command(call)
        shot_id, occurrence_id = call['args']['shot_id'], call['args']['occurrence_id']
        if not any(row['occurrence_id'] == occurrence_id for row in work['placements']):
            continue  # Unplaced local copies are not part of publication.
        # Resolve the explicit owner on the authenticated input, never by equal
        # payloads, source-map similarity, or the first matching physical shot.
        source = next((row for row in base['placements']
                       if 'occurrence:' + row['occurrence_id'] == call['select']), None)
        if source is None:
            raise AuthoringBundleError('duplicate source occurrence is unavailable')
        expected = copy.deepcopy(base)
        add_authoring_shot(expected, template_shot_id=source['shot_id'], shot_id=shot_id,
                           occurrence_id=occurrence_id, start_ms=reservation['start_ms'])
        authored = work['shots'][shot_id]
        if (set(authored) != set(expected['shots'][shot_id])
                or any(not equal(authored[field], expected['shots'][shot_id][field])
                for field in ('shot_id', 'base_payload', 'base_internal_timeline'))
                or not equal(work['source_mapping']['shots'].get(shot_id),
                             expected['source_mapping']['shots'][shot_id])
                or occurrence_id in work['base_placements']):
            raise AuthoringBundleError('duplicate source baseline changed')
        baseline = work['source_mapping']['placements'].get(occurrence_id)
        original = expected['source_mapping']['placements'][occurrence_id]
        if not isinstance(baseline, dict):
            raise AuthoringBundleError('duplicate placement baseline is unavailable')
        changed = {key for key in set(baseline) | set(original)
                   if (key in baseline) != (key in original)
                   or not equal(baseline.get(key), original.get(key))}
        if changed - {'source_offset', 'speed', 'transform'}:
            raise AuthoringBundleError('duplicate placement baseline changed')
        _validate_placement_capabilities(baseline, path='duplicate baseline', fields=changed)
        if not equal(work['base_parent'], base['base_parent']):
            raise AuthoringBundleError('duplicate source parent changed')
        for bundle in (base, work):
            bundle['base_parent'].pop('publication_id', None)
        plan = execute(base, [call])
        native = plan.bundle['shots'][shot_id]
        remap = {old['item_id']: new['item_id'] for old, new in zip(
            authored['base_payload'].get('items', []), native['payload'].get('items', []))}
        copied = copy.deepcopy(native)
        copied['payload'] = _remap_known_item_references(authored['payload'], remap)
        from .native_occurrence_duplicate import remap_copy_cuts

        copied['internal_timeline'] = remap_copy_cuts(
            _remap_known_item_references(authored['internal_timeline'], remap), occurrence_id)
        work['shots'][shot_id] = copied
        if occurrence_id in plan.bundle['base_placements']:
            work['base_placements'][occurrence_id] = copy.deepcopy(plan.bundle['base_placements'][occurrence_id])
        work['source_mapping']['shots'].pop(shot_id)
        work['source_mapping']['placements'].pop(occurrence_id)
        base = plan.bundle
        commands.extend(plan.commands)
    return base, commands


def _fork_shared_aliases(base: dict[str, Any], work: dict[str, Any]) -> tuple[
        dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    from astrid.core.timeline.authoring_bundle import (
        _copy_shot_for_placement,
        _identity,
        _remap_known_item_references,
    )

    from ._command_state import equal
    from .commands import execute
    from .emitter import emit_document_commands
    from .native_occurrence_fork import capture_occurrence_fork

    # Check every supplied baseline and source before replacing any projection
    # with recorder output. Authored payloads alone are allowed to differ.
    provenance = copy.deepcopy(work)
    provenance['parent'] = copy.deepcopy(base['parent'])
    provenance['placements'] = copy.deepcopy(base['placements'])
    for local, shot in provenance['shots'].items():
        for field in ('payload', 'internal_timeline'):
            shot[field] = copy.deepcopy(base['shots'][local][field])
    emit_document_commands(base, provenance)
    document = base['source_mapping']['_runtime_document']
    physical = {row['occurrence_id']: row for row in document['parent_revision']['payload']['occurrences']}
    commands = []
    for placement in work['placements']:
        local = placement['shot_id']
        shot, original = work['shots'][local], base['shots'][local]
        source = physical.get(placement['occurrence_id'])
        if source is None:
            continue  # A preceding registered duplicate owns this new placement.
        shared = sum(row['shot_revision_id'] == source['shot_revision_id']
                     for row in physical.values()) > 1
        if not shared or all(equal(shot[field], original[field])
                             for field in ('payload', 'internal_timeline')):
            continue
        # The virtual authoring alias already occupies a local slot. Reserve a
        # separate deterministic native destination bound to this exact source.
        destination = _identity('authoring-fork', work['project_id'], work['timeline_id'], source)
        for bundle in (base, work):
            bundle['base_parent'].pop('publication_id', None)
        call = capture_occurrence_fork(base, 'occurrence:' + placement['occurrence_id'], destination)
        plan = execute(base, [call])
        raw = next(row['payload'] for row in document['shot_revisions']
                   if row['revision_id'] == source['shot_revision_id'])
        _, remap = _copy_shot_for_placement(raw, project_id=work['project_id'],
            timeline_id=work['timeline_id'], occurrence_id=placement['occurrence_id'],
            authoring_shot_id=destination, source_shot_id=source['shot_id'])
        old_map = base['source_mapping']['shots'][local]['item_id_map']
        authored = copy.deepcopy(plan.bundle['shots'][destination])
        authored['payload'] = _remap_known_item_references(
            shot['payload'], {old_map.get(key, key): value for key, value in remap.items()})
        authored['internal_timeline'] = copy.deepcopy(shot['internal_timeline'])
        work['shots'][destination] = authored
        placement['shot_id'] = destination
        base = plan.bundle
        commands.extend(plan.commands)
        for bundle in (base, work):
            if not any(row['shot_id'] == local for row in bundle['placements']):
                bundle['shots'].pop(local)
                bundle['source_mapping']['shots'].pop(local)
    return base, work, commands
