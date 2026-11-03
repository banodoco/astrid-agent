"""Restore removed placement order and immutable inputs from accepted boundaries."""
from __future__ import annotations

import copy

from ._collections import AcceptedRecords
from ._command_state import ABSENT, CommandError, State, equal, root_identity
from ._recording import guard
from ._step_undo import entry_records


def normalize_root_inputs(bundle, records):
    """Rejoin unchanged working aliases for an authenticated aggregate reversal."""
    roots = {write['ref'][5:] for record in records for write in record['writes']
             if write['ref'].startswith('shot:') and not write['path']}
    if not roots:
        return
    from .native_timeline_duplicate import physical_content

    physical = physical_content(bundle)
    mapping = bundle['source_mapping']['shots']
    for identity in roots - bundle['shots'].keys():
        aliases = [local for local, origin in mapping.items()
                   if origin['shot_id'] == identity and local in bundle['shots']]
        if not aliases:
            continue
        for local in aliases:
            shot = bundle['shots'][local]
            if (not equal(shot['payload'], shot['base_payload'])
                    or not equal(shot['internal_timeline'], shot['base_internal_timeline'])):
                raise CommandError('aggregate working input changed', kind='inverse_conflict',
                                   ref='shot:' + identity)
        if identity not in physical['shots']:
            raise CommandError('aggregate source is unavailable', kind='history_unavailable')
        bundle['shots'][identity] = physical['shots'][identity]
        mapping[identity] = physical['source_mapping']['shots'][identity]
        for local in aliases:
            del bundle['shots'][local]
            del mapping[local]
        for rows in (bundle['placements'], bundle['base_placements'].values(),
                     bundle['source_mapping']['placements'].values()):
            for row in rows:
                if row['shot_id'] in aliases:
                    row['shot_id'] = identity


def inverse_root_manifest(bundle, records):
    """Reverse retained-input deltas only for complete accepted root commands."""
    if not isinstance(records, AcceptedRecords):
        return None
    if not records or not equal(list(records), [record for entry in records.entries
            for record in entry['records']['commands']]):
        return None
    if '_runtime_document' not in bundle['source_mapping']:
        return None
    if any(record['command']['op'] not in ('timeline.duplicate', 'timeline.inverse')
           or not any(write['ref'].startswith('shot:') and not write['path']
                      for write in record['writes'])
           or not any(read['ref'] == 'timeline' and not read['path']
                      for read in record['reads']) for record in records):
        return None
    manifest = copy.deepcopy(bundle['source_mapping']['_runtime_document']['dependency_manifest'])
    for entry in reversed(records.entries):
        before, after = records.load_boundary(entry)
        for key in ('media', 'documents'):
            prior = before['source_mapping']['_runtime_document']['dependency_manifest'][key]
            accepted = after['source_mapping']['_runtime_document']['dependency_manifest'][key]
            manifest[key] = [pin for pin in manifest[key] if pin not in accepted or pin in prior]
            manifest[key].extend(copy.deepcopy(pin) for pin in prior
                                 if pin not in accepted and pin not in manifest[key])
    return manifest


def clip_cache_stages(records):
    """Prove word-cache stages inside guarded clip roots from accepted boundaries."""
    roots = {w['ref'] for record in records for w in record['writes']
             if w['ref'].startswith('parent/clip:') and not w['path']}
    complete = ([record for entry in records.entries for record in entry['records']['commands']]
                if isinstance(records, AcceptedRecords) else records)
    if not any(w['ref'] in roots and w['path'] in (['at'], ['hold'])
               for record in complete for w in record['derived_writes']):
        return {}
    if not isinstance(records, AcceptedRecords) or not equal(list(records), [
            record for entry in records.entries for record in entry_records(records, entry)]):
        raise CommandError('clip cache stages need accepted records', kind='history_unavailable')
    stages = {}
    for entry in records.entries:
        before, after = records.load_boundary(entry)
        state, accepted = State(copy.deepcopy(before)), State(after)
        for record in entry['records']['commands']:
            anchored = {ref for ref in roots if state.get(
                ref, ['app', 'astrid', 'word_anchors']).get('value')}
            for write in record['writes']:
                if write['ref'] in roots:
                    guard(state, write['ref'], write['path'], write['before'], kind='history_unavailable')
                    state.set(write['ref'], write['path'], write['after'])
            anchored.update(ref for ref in roots if state.get(
                ref, ['app', 'astrid', 'word_anchors']).get('value'))
            stages.setdefault(id(record), [])
            for write in record['derived_writes']:
                if write['ref'] not in roots or write['path'] not in (['at'], ['hold']):
                    continue
                if write['ref'] not in anchored:
                    raise CommandError('clip cache has no word owner', kind='history_unavailable')
                guard(state, write['ref'], write['path'], write['before'], kind='history_unavailable')
                state.set(write['ref'], write['path'], write['after'])
                cause = write.get('cause', record['ordinal'])
                if type(cause) is not int or not 0 <= cause <= record['ordinal']:
                    raise CommandError('clip cache cause is unavailable', kind='history_unavailable')
                stages.setdefault(id(entry['records']['commands'][cause]), []).append(write)
        for ref in roots:
            guard(state, ref, [], accepted.get(ref, []), kind='history_unavailable')
    # Entry commands and the flattened records compare equal but need not share identity.
    return {id(record): stages[id(entry['records']['commands'][record['ordinal']])]
            for entry in records.entries for record in entry_records(records, entry)}


def recover_source(state, value, source):
    """Reopen a missing compiler input, without creating or editing shot content."""
    identity, occurrence = value['shot_id'], value['occurrence_id']
    if identity in state.bundle['shots'] and (
            occurrence in state.bundle['base_placements'] or identity not in state.bundle['source_mapping']['shots']):
        return
    if (identity not in source['shots'] or identity not in source['source_mapping']['shots']
            or not any(row['occurrence_id'] == value['occurrence_id'] and row['shot_id'] == identity
                       for row in source['placements'])):
        raise CommandError('removed occurrence source is unavailable', kind='history_unavailable')
    if identity not in state.bundle['shots']:
        state.bundle['shots'][identity] = copy.deepcopy(source['shots'][identity])
        state.bundle['source_mapping']['shots'][identity] = copy.deepcopy(source['source_mapping']['shots'][identity])
    for target, original in ((state.bundle['base_placements'], source['base_placements']),
                             (state.bundle['source_mapping']['placements'], source['source_mapping']['placements'])):
        if occurrence not in target and occurrence in original:
            target[occurrence] = copy.deepcopy(original[occurrence])


def recover_rebound_sources(state, records):
    """Recover compiler inputs for an authenticated native ownership reversal."""
    for record in records:
        for write in record['writes']:
            root = root_identity(write['ref'])
            if (root is None or root[0] != 'occurrence' or write['path'] != ['shot_id']
                    or write['before']['state'] != 'present'
                    or write['before']['value'] in state.bundle['shots']
                    or any(other['ref'] == 'shot:' + write['before']['value']
                           and not other['path'] and other['after'] == ABSENT
                           and other['before']['state'] == 'present'
                           for candidate in records for other in candidate['writes'])):
                continue
            if not isinstance(records, AcceptedRecords):
                raise CommandError('ownership reversal needs accepted source boundaries',
                                   kind='history_unavailable', ref=write['ref'])
            value = {'occurrence_id': root[1], 'shot_id': write['before']['value']}
            sources = [source for entry in records.entries if any(
                           item is record for item in entry_records(records, entry))
                       for source in records.load_boundary(entry)
                       if any(row['occurrence_id'] == root[1] and row['shot_id'] == value['shot_id']
                              for row in source['placements'])]
            if not sources:
                raise CommandError('ownership reversal source is unavailable', kind='history_unavailable',
                                   ref=write['ref'])
            recover_source(state, value, sources[0])


def restore_root_baselines(state, records, publication, commands):
    """Keep a restored native root on its immediate accepted compiler baseline."""
    if not isinstance(records, AcceptedRecords) or not any('_inverse_effects' in record for record in commands):
        return
    from astrid.core.timeline.authoring_bundle import _RestoredShot

    entry = next(entry for entry in records.entries if entry['publication_id'] == publication)
    before, after = records.load_boundary(entry)
    local_roots = set(before['shots'])
    before, after = copy.deepcopy(before), copy.deepcopy(after)
    normalize_root_inputs(before, entry['records']['commands'])
    normalize_root_inputs(after, entry['records']['commands'])
    for record in commands:
        if record['command']['op'] != 'timeline.inverse' or '_inverse_effects' not in record:
            continue
        for write in record['writes']:
            if (not write['ref'].startswith('shot:') or write['path']
                    or write['after'] != ABSENT or write['before'] == ABSENT):
                continue
            identity = write['ref'][5:]
            prior = before['shots'].get(identity)
            expected = State(before)
            if '_inverse_stage' in record:
                original, _, boundary = record['_inverse_stage']
                if not any(equal(original, item) for item in entry_records(records, entry)):
                    raise CommandError('root stage differs from accepted source', kind='history_unavailable')
                start, end = boundary['native_write_range']
                if not equal(record['writes'], original['writes'][start:end]):
                    raise CommandError('root stage writes differ from accepted source', kind='history_unavailable')
                # Native stages can precede later edits to the restored root.
                # Project only into a detached guard state; retain the full
                # accepted document as the compiler's source baseline below.
                projected = State(copy.deepcopy(before))
                for source_record in entry['records']['commands']:
                    for index, change in enumerate(source_record['writes']):
                        if equal(source_record, original) and index == start:
                            expected = State(copy.deepcopy(projected.bundle))
                        guard(projected, change['ref'], change['path'], change['before'],
                              kind='history_unavailable')
                        projected.set(change['ref'], change['path'], change['after'])
                guard(projected, write['ref'], [], State(after).get(write['ref'], []),
                      kind='history_unavailable')
                if ({row['occurrence_id'] for row in projected.bundle['placements']
                     if row['shot_id'] == identity} !=
                        {row['occurrence_id'] for row in after['placements'] if row['shot_id'] == identity}):
                    raise CommandError('root source owners changed', kind='history_unavailable')
            shot = state.bundle['shots'].get(identity)
            owners = {row['occurrence_id'] for row in state.bundle['placements']
                      if row['shot_id'] == identity}
            if (prior is None or shot is None or not owners
                    or owners != {row['occurrence_id'] for row in before['placements']
                                  if row['shot_id'] == identity}
                    or owners != {row['occurrence_id'] for row in expected.bundle['placements']
                                  if row['shot_id'] == identity}
                    or not equal(state.get(write['ref'], []), expected.get(write['ref'], []))
                    # A nested stage restores its entry image, before child
                    # edits. The projection above guards every intermediate
                    # before value, including the root's later deletion input.
                    or ('_inverse_stage' not in record
                        and not state.matches(write['ref'], [], write['before']))):
                raise CommandError('restored root differs from accepted source', kind='history_unavailable')
            if identity not in local_roots:
                # Shared physical roots have no occurrence-local restored
                # baseline. Keep their authenticated native creation so the
                # compiler emits the same shared immutable image again.
                if (shot.get('_created') is not True
                        or identity in state.bundle['source_mapping']['shots']):
                    raise CommandError('restored shared root source changed', kind='history_unavailable')
            else:
                state.bundle['shots'][identity] = _RestoredShot(
                    shot, copy.deepcopy(before['source_mapping']['_runtime_document']))


def occurrence_orders(state, records, changes):
    """Only absent-to-present reversals need an authenticated insertion position."""
    removed = {w['ref'] for w in changes if not w['path'] and w['before'] != ABSENT
               and w['after'] == ABSENT and root_identity(w['ref']) is not None
               and w['ref'].startswith('occurrence:')
               and not any(other['ref'] == 'shot:' + w['before']['value']['shot_id']
                           and not other['path'] for other in changes)}
    needed = {id(record) for record in records if any(
        w['ref'] in removed and not w['path'] and w['before'] != ABSENT and w['after'] == ABSENT
        for w in record['writes'])}
    if not needed:
        return {}
    if not isinstance(records, AcceptedRecords):
        raise CommandError('removed occurrence needs accepted order/source boundaries', kind='history_unavailable')
    result = {}
    for entry in records.entries:
        commands = entry_records(records, entry)
        if not any(id(record) in needed for record in commands):
            continue
        before, after = records.load_boundary(entry)
        ordered = [row['occurrence_id'] for row in before['placements']]
        selected = {record['ordinal']: record for record in commands}
        for original_record in entry['records']['commands']:
            record = selected.get(original_record['ordinal'], original_record)
            original = list(ordered)
            for write in record['writes']:
                if root_identity(write['ref']) is None or not write['ref'].startswith('occurrence:') or write['path']:
                    continue
                identity = write['ref'].removeprefix('occurrence:')
                if write['before'] != ABSENT and write['after'] == ABSENT:
                    if id(record) in needed:
                        source = next((image for image in (before, after)
                                       if any(equal(row, write['before']['value']) for row in image['placements'])), None)
                        if source is None:
                            raise CommandError('removed occurrence boundary changed', kind='history_unavailable')
                        recover_source(state, write['before']['value'], source)
                    ordered.remove(identity)
                elif write['before'] == ABSENT and write['after'] != ABSENT:
                    ordered.append(identity)
            if id(record) in needed:
                result[id(record)] = original
    if set(result) != needed:
        raise CommandError('removed occurrence order is unavailable', kind='history_unavailable')
    return result


def arrange_restored(state, record, original):
    """Insert restored rows relative to surviving history; leave other rows alone."""
    if original is None:
        return
    rows = state.bundle['placements']
    restored = {w['before']['value']['occurrence_id'] for w in record['writes']
                if w['ref'].startswith('occurrence:') and '/' not in w['ref']
                and not w['path'] and w['before'] != ABSENT and w['after'] == ABSENT}
    for identity in reversed(original):
        if identity not in restored:
            continue
        row = next(row for row in rows if row['occurrence_id'] == identity)
        rows.remove(row)
        following = set(original[original.index(identity) + 1:])
        index = next((i for i, other in enumerate(rows) if other['occurrence_id'] in following), len(rows))
        rows.insert(index, row)
