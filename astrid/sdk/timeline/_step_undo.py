"""Select native inverse stages without rewriting immutable accepted history."""
from __future__ import annotations

import copy

from ._collections import AcceptedRecords
from ._command_state import CommandError, equal


def _commands(entry):
    recorder = entry.get('records')
    if not isinstance(recorder, dict) or not isinstance(recorder.get('commands'), list):
        raise CommandError('complete accepted command history is required', kind='history_unavailable')
    commands = recorder['commands']
    if not commands or any(not isinstance(record, dict) or record.get('ordinal') != ordinal
                           or any(not isinstance(record.get(key), list)
                                  for key in ('writes', 'reads', 'selections', 'created'))
                           for ordinal, record in enumerate(commands)):
        raise CommandError('accepted command ordinals are incomplete', kind='history_unavailable')
    return commands


def _steps(entry, *, required=False):
    commands = _commands(entry)
    steps = entry['records'].get('steps')
    if steps is None and not required:
        # Historical native captures still support their existing whole inverse.
        # They supply no authority to select or invent a historical step.
        return [(None, commands, [])]
    if not isinstance(steps, list) or not steps:
        raise CommandError('accepted step history is unavailable', kind='history_unavailable')
    cursor, seen, result = 0, set(), []
    for step in steps:
        if not isinstance(step, dict):
            raise CommandError('accepted step history is malformed', kind='history_unavailable')
        identity, ordinals = step.get('id'), step.get('command_ordinals')
        if (not isinstance(identity, str) or not identity or identity in seen
                or not isinstance(ordinals, list) or not ordinals
                or any(type(index) is not int for index in ordinals)
                or ordinals != list(range(cursor, cursor + len(ordinals)))
                or cursor + len(ordinals) > len(commands)
                or not isinstance(step.get('patch'), list)
                or not isinstance(step.get('dependencies', []), list)):
            raise CommandError('accepted steps do not partition command history', kind='history_unavailable')
        seen.add(identity)
        cursor += len(ordinals)
        result.append((step, [commands[index] for index in ordinals], step.get('dependencies', [])))
    if cursor != len(commands):
        raise CommandError('accepted steps omit command history', kind='history_unavailable')
    return result


class SelectedRecords(AcceptedRecords):
    """A source-preserving view; entries and command ordinals stay authoritative."""

    def __init__(self, accepted, chosen, address_resolver):
        self.accepted, self.chosen = accepted, chosen
        self.address_resolver = address_resolver
        selected = [record for entry in accepted.entries for record in entry_records(accepted, entry)
                    if record['ordinal'] in chosen[entry['publication_id']]]
        super().__init__(accepted.entries, selected, accepted.load_boundary)

    def selected_for(self, entry):
        ordinals = self.chosen[entry['publication_id']]
        return [record for record in entry_records(self.accepted, entry) if record['ordinal'] in ordinals]

    def boundary(self, record):
        return self.accepted.boundary(record)


def entry_records(records, entry):
    """Return selected captures while retaining complete boundary source entries."""
    if isinstance(records, SelectedRecords):
        return records.selected_for(entry)
    offset = 0
    for source in records.entries:
        count = len(source['records']['commands'])
        if source is entry:
            result = records[offset:offset + count]
            if not equal(result, source['records']['commands']):
                break
            return result
        offset += count
    raise CommandError('inverse records differ from accepted history', kind='history_unavailable')


def select_inverse_records(accepted, steps=None, *, address_resolver=None):
    """Select only authenticated command runs; step effect patches are never run."""
    if not isinstance(accepted, AcceptedRecords) or isinstance(accepted, SelectedRecords):
        raise CommandError('inverse selection needs complete accepted records', kind='history_unavailable')
    originals = [record for entry in accepted.entries for record in _commands(entry)]
    if not equal(list(accepted), originals):
        raise CommandError('inverse records differ from accepted history', kind='history_unavailable')
    publications = [entry['publication_id'] for entry in accepted.entries]
    if not publications or len(set(publications)) != len(publications):
        raise CommandError('inverse target history is incomplete', kind='history_unavailable')
    if steps is not None and (not isinstance(steps, dict) or set(steps) != set(publications)):
        raise CommandError('inverse steps must specify every target publication', kind='invalid_steps')
    chosen = {}
    for entry in accepted.entries:
        publication = entry['publication_id']
        groups = _steps(entry, required=steps is not None)
        if steps is None:
            chosen[publication] = {record['ordinal'] for _, commands, _ in groups for record in commands}
            continue
        requested = steps[publication]
        if (not isinstance(requested, list) or not requested
                or any(not isinstance(identity, str) or not identity for identity in requested)
                or len(set(requested)) != len(requested)
                or not set(requested) <= {step['id'] for step, _, _ in groups}):
            raise CommandError('inverse step IDs must be known and unique', kind='invalid_steps')
        chosen[publication] = {record['ordinal'] for step, commands, _ in groups
                               if step['id'] in requested for record in commands}
    return SelectedRecords(accepted, chosen, address_resolver)


def selected_dependencies(records):
    if not isinstance(records, AcceptedRecords):
        return []
    if not equal(list(records), [record for entry in records.entries for record in entry_records(records, entry)]):
        raise CommandError('inverse selection differs from accepted history', kind='history_unavailable')
    result = []
    for entry in records.entries:
        selected = {record['ordinal'] for record in entry_records(records, entry)}
        for _, commands, dependencies in _steps(entry):
            if any(record['ordinal'] in selected for record in commands):
                result.extend(dependencies)
    return result


def _native_mirror_partition(record, step, nested):
    """Attribute aggregate and compiler observations in native execution order."""
    from ._command_state import State, present
    from ._doc_patch import replace_at, value_at

    writes = record.get('derived_writes', [])
    relevant = [write for write in record['writes'] if write['ref'].startswith('parent/clip:')
                or (write['ref'] == 'parent' and write['path'] in (['clips'], ['config', 'clips']))]
    def projection(payload):
        return {key: copy.deepcopy(payload[key]) for key in ('clips', 'config') if key in payload}

    first_effect = next((effect for effect in step['patch'] if effect['ref'] == 'parent'
                         and (not effect['path'] or effect['path'] == ['clips']
                              or effect['path'][:1] == ['config'])), None)
    root_input = (first_effect is not None and not first_effect['path']
                  and first_effect['before'].get('state') == 'present'
                  and isinstance(first_effect['before'].get('value'), dict))
    first = relevant[0]
    if root_input:
        # A canonical root effect captures physical storage, including an absent
        # mirror. Prove it against every native write and the landing effect.
        state = State({'parent': projection(first_effect['before']['value'])})
        initial = state.get('parent', ['clips'])
    else:
        initial = (writes[0]['before'] if first['ref'].startswith('parent/clip:') and writes
                   else first['before'])
        if initial.get('state') != 'present' or not isinstance(initial.get('value'), list):
            raise CommandError('nested inverse mirror input is unavailable', kind='history_unavailable')
        state = State({'parent': {'clips': copy.deepcopy(initial['value']),
                                 'config': {'clips': copy.deepcopy(initial['value'])}}})
    config = next((write for write in record['writes'] if write['ref'] == 'parent'
                   and write['path'] == ['config']), None)
    if not root_input and config is not None and (record['writes'].index(config) < record['writes'].index(first)
            or (record['writes'].index(config) < nested[0]['boundary']['native_write_range'][1]
                and config['before'].get('state') == 'present'
                and isinstance(config['before'].get('value'), dict)
                and equal(config['before']['value'].get('clips'), initial['value'])
                and not any(write['ref'].startswith('parent/clip:')
                            or (write['ref'] == 'parent' and write['path'][:1] == ['config'])
                            for write in record['writes'][:record['writes'].index(config)]))):
        # A whole config replacement can enable or disable compiler mirrors.
        # Aggregate clips writes leave config untouched until the stage compiles;
        # its full native input remains valid before that first compiler boundary.
        # A later config input must still contain the initial mirrored clips.
        state.set('parent', ['config'], config['before'])
    elif (not root_input and config is not None
            and record['writes'].index(config) < nested[0]['boundary']['native_write_range'][1]
            and config['before'].get('state') == 'present'
            and isinstance(config['before'].get('value'), dict)
            and all(write['ref'] == 'parent' and write['path'] in (['clips'], ['config', 'clips'])
                    for write in record['writes'][:record['writes'].index(config)])):
        # The ancestor input follows earlier native clip writes in this stage.
        # Reverse those exact descendants to recover its complete initial image.
        image = copy.deepcopy(config['before']['value'])
        for write in reversed(record['writes'][:record['writes'].index(config)]):
            if write['path'] != ['config', 'clips']:
                continue
            if not equal(value_at(image, 'parent', ['clips']), write['after']):
                raise CommandError('nested inverse config input differs from history',
                                   kind='history_unavailable')
            replace_at(image, 'parent', ['clips'], write['before'])
        state.set('parent', ['config'], present(image))
    mirrored = (isinstance(state.bundle['parent'].get('clips'), list)
                and equal(state.get('parent', ['config', 'clips']), state.get('parent', ['clips'])))
    cursor, result = 0, []

    def compile_mirror():
        nonlocal cursor
        before = state.get('parent', ['config', 'clips'])
        after = state.get('parent', ['clips'])
        if not mirrored or equal(before, after):
            return
        expected = {'ref': 'parent', 'path': ['config', 'clips'], 'before': before,
                    'after': after, 'cause': record['ordinal']}
        if cursor == len(writes) or not equal(writes[cursor], expected):
            raise CommandError('nested inverse compiler observation differs from native history',
                               kind='history_unavailable')
        cursor += 1
        state.set('parent', ['config', 'clips'], after)

    for binding in nested:
        start, end = binding['boundary']['native_write_range']
        first, last = binding['boundary']['effect_range']
        before = state.get('parent', ['clips'])
        image = copy.deepcopy(state.bundle['parent'])
        root_effect = False
        for effect in step['patch'][first:last]:
            if effect['ref'] != 'parent':
                continue
            if not effect['path']:
                root_effect = True
                if (effect['op'] != 'set' or effect['before'].get('state') != 'present'
                        or not equal(image, projection(effect['before']['value']))):
                    raise CommandError('nested inverse mirror effects differ from history',
                                       kind='history_unavailable')
                image = projection(effect['value'])
                continue
            if effect['path'] != ['config'] and effect['path'][:2] != ['config', 'clips']:
                continue
            if effect['op'] not in ('set', 'remove') or not equal(
                    value_at(image, 'parent', effect['path']), effect['before']):
                raise CommandError('nested inverse mirror effects differ from history',
                                   kind='history_unavailable')
            replacement = (present(effect['value']) if effect['op'] == 'set' else {'state': 'absent'})
            replace_at(image, 'parent', effect['path'], replacement)
        mirror_start = cursor
        for write in record['writes'][start:end]:
            leaf = write['ref'].startswith('parent/clip:')
            aggregate = write['ref'] == 'parent' and write['path'] in (
                ['clips'], ['config', 'clips'], ['config'])
            if not leaf and not aggregate:
                continue
            if not equal(state.get(write['ref'], write['path']), write['before']):
                raise CommandError('nested inverse native mirror input differs from history',
                                   kind='history_unavailable')
            state.set(write['ref'], write['path'], write['after'])
            if aggregate:
                state.parent_clip_path = State(state.bundle).parent_clip_path
            if write['ref'] == 'parent' and write['path'] == ['config']:
                mirrored = (isinstance(state.bundle['parent'].get('clips'), list)
                            and equal(state.get('parent', ['config', 'clips']), state.get('parent', ['clips'])))
            if leaf:
                compile_mirror()
        # Anchor inverses restore/recompute their literal cache after authored
        # writes. Replay those exact derived observations before the final
        # compiler mirror, and still require the complete accepted effect image.
        while cursor < len(writes) and writes[cursor]['ref'].startswith('parent/clip:'):
            write = writes[cursor]
            if write['path'] not in (['at'], ['hold']) or not equal(
                    state.get(write['ref'], write['path']), write['before']):
                raise CommandError('nested inverse timing differs from history', kind='history_unavailable')
            state.set(write['ref'], write['path'], write['after'])
            cursor += 1
        compile_mirror()
        after = state.get('parent', ['clips'])
        observed = (state.bundle['parent'] if root_effect
                    else state.get('parent', ['config', 'clips']))
        expected = image if root_effect else value_at(image, 'parent', ['config', 'clips'])
        if not equal(observed, expected):
            raise CommandError('nested inverse observations omit accepted effects', kind='history_unavailable')
        result.append((writes[mirror_start:cursor], (before, after)))
    if cursor != len(writes):
        raise CommandError('nested inverse mirrors exceed native observations', kind='history_unavailable')
    return result


def _nested_mirrors(record, step, nested):
    return [mirrors for mirrors, _ in _nested_mirror_partition(record, step, nested)]


def _nested_mirror_partition(record, step, nested):
    """Match leaf observations to native ranges; retain structural order history."""
    from ._doc_patch import replace_at, value_at

    writes = record.get('derived_writes', [])
    if any(not ((write.get('ref') == 'parent' and write.get('path') == ['config', 'clips'])
               or (write.get('ref', '').startswith('parent/clip:')
                   and write.get('path') in (['at'], ['hold'])))
           or type(write.get('cause')) is not int or write['cause'] != record['ordinal']
           for write in writes):
        raise CommandError('nested inverse derived attribution differs from history', kind='history_unavailable')
    aggregate = any(write['ref'] == 'parent' and write['path'] in (['clips'], ['config', 'clips'])
                    for write in record['writes'])
    leaves = [write for write in record['writes'] if write['ref'].startswith('parent/clip:')]
    # Structural inverses also observe order restoration between native writes.
    # Property-only stages have no such reorder and can be matched write by write,
    # including empty-effect stages and repeated complete compiler images.
    if aggregate or (writes and leaves and all(write['path'] for write in leaves)):
        return _native_mirror_partition(record, step, nested)
    if not writes:
        return [([], None) for _ in nested]
    cursor, result = 0, []
    for binding in nested:
        first, last = binding['boundary']['effect_range']
        effects = [effect for effect in step['patch'][first:last]
                   if effect['ref'] == 'parent' and effect['path'][:2] == ['config', 'clips']]
        start = cursor
        if effects:
            if cursor == len(writes) or writes[cursor]['before'].get('state') != 'present':
                raise CommandError('nested inverse mirror input is unavailable', kind='history_unavailable')
            state = writes[cursor]['before']
            image = {'config': {'clips': copy.deepcopy(state['value'])}}
            try:
                for effect in effects:
                    if effect['op'] not in ('set', 'remove') or not equal(
                            value_at(image, 'parent', effect['path']), effect['before']):
                        raise CommandError('nested inverse mirror effect input differs from history')
                    replacement = ({'state': 'present', 'value': effect['value']}
                                   if effect['op'] == 'set' else {'state': 'absent'})
                    replace_at(image, 'parent', effect['path'], replacement)
            except CommandError as exc:
                raise CommandError('nested inverse mirror effects differ from history', kind='history_unavailable') from exc
            target = value_at(image, 'parent', ['config', 'clips'])
            while cursor < len(writes):
                write = writes[cursor]
                if not equal(write['before'], state):
                    raise CommandError('nested inverse mirror chain differs from effects', kind='history_unavailable')
                state = write['after']
                cursor += 1
                if equal(state, target):
                    break
            else:
                raise CommandError('nested inverse mirrors omit accepted effects', kind='history_unavailable')
        mirrors = writes[start:cursor]
        result.append((mirrors, (mirrors[0]['before'], mirrors[-1]['after']) if mirrors else None))
    if cursor != len(writes):
        raise CommandError('nested inverse mirrors exceed accepted effects', kind='history_unavailable')
    return result


def inverse_stages(records):
    """Yield reverse execution groups with their original immutable step IDs."""
    if not isinstance(records, AcceptedRecords):
        for record in reversed(records):
            yield None, None, [record]
        return
    for entry in reversed(records.entries):
        commands = {record['ordinal']: record for record in entry_records(records, entry)}
        for step, originals, _ in reversed(_steps(entry)):
            chosen = [commands[record['ordinal']] for record in originals if record['ordinal'] in commands]
            if chosen:
                nested = ([binding for binding in chosen[0].get('bindings', [])
                           if binding['key'] == 'inverse.stage'] if len(chosen) == 1
                          and chosen[0]['command']['op'] == 'timeline.inverse' else [])
                native_only = (nested and all('effect_range' not in binding['boundary'] for binding in nested)
                               and not any(binding['key'] == 'commit.effect'
                                           for binding in chosen[0].get('bindings', [])))
                if not nested or native_only:
                    # Native-only history predates canonical effect partitions.
                    # Keep its whole native reversal without inventing v2 facts.
                    yield entry.get('publication_id'), step['id'] if step is not None else None, chosen
                    continue
                record = chosen[0]
                cursor = 0
                effect_cursor = 0
                for binding in nested:
                    boundary = binding['boundary']
                    if (step is None or any(not isinstance(boundary.get(key), list)
                            or len(boundary[key]) != 2 or any(type(index) is not int for index in boundary[key])
                            for key in ('native_write_range', 'effect_range'))):
                        raise CommandError('nested inverse ranges differ from accepted history',
                                           kind='history_unavailable')
                    start, end = boundary['native_write_range']
                    first, last = boundary['effect_range']
                    if (binding['ref'] != 'parent' or binding['policy'] != 'fixed'
                            or binding['dependencies'] != []
                            or boundary['publication_id'] not in record['command']['args']['publications']
                            or start != cursor or not start < end <= len(record['writes'])
                            or first != effect_cursor or not first <= last <= len(step['patch'])):
                        raise CommandError('nested inverse ranges differ from accepted history',
                                           kind='history_unavailable')
                    cursor, effect_cursor = end, last
                if cursor != len(record['writes']) or effect_cursor != len(step['patch']):
                    raise CommandError('nested inverse ranges omit accepted history', kind='history_unavailable')
                mirrors = _nested_mirrors(record, step, nested)
                for binding, derived in reversed(list(zip(nested, mirrors))):
                    start, end = binding['boundary']['native_write_range']
                    first, last = binding['boundary']['effect_range']
                    from ._doc_patch import inverse_changes

                    yield entry['publication_id'], step['id'], [{**record, 'writes': record['writes'][start:end],
                        'derived_writes': derived,
                        '_inverse_stage': (record, step, binding['boundary']),
                        '_inverse_effects': inverse_changes(step['patch'][first:last])}]


def validate_nested_history(entry, load_history, *, ancestors=()):
    """Authenticate inherited partitions against the original accepted records."""
    publication = entry['publication_id']
    if publication in ancestors:
        raise CommandError('cyclic inverse source history', kind='history_unavailable')
    for record in _commands(entry):
        nested = [binding['boundary'] for binding in record.get('bindings', [])
                  if binding['key'] == 'inverse.stage']
        if not nested or (all('effect_range' not in boundary for boundary in nested)
                          and not any(binding['key'] == 'commit.effect' for binding in record['bindings'])):
            continue
        if load_history is None or record['command']['op'] != 'timeline.inverse':
            raise CommandError('nested inverse source history is unavailable', kind='history_unavailable')
        # Validate immediate ranges before consulting their inherited identities.
        list(inverse_stages(AcceptedRecords([entry], _commands(entry), None)))
        args = record['command']['args']
        sources = [load_history(identity) for identity in args['publications']]
        for identity, source in zip(args['publications'], sources):
            if source.get('publication_id') != identity or source.get('intent') != 'recorded':
                raise CommandError('nested inverse source differs from history', kind='history_unavailable')
            _steps(source, required=True)
            validate_nested_history(source, load_history, ancestors=(*ancestors, publication))
        selected = select_inverse_records(AcceptedRecords(sources,
            [command for source in sources for command in _commands(source)], None), args.get('steps'))
        expected = list(inverse_stages(selected))
        if len(nested) != len(expected):
            raise CommandError('nested inverse partitions differ from source history', kind='history_unavailable')
        for boundary, (identity, step_id, commands) in zip(nested, expected):
            ordinals = boundary.get('command_ordinals')
            start, end = boundary['native_write_range']
            if (boundary.get('publication_id') != identity or boundary.get('step_id') != step_id
                    or not isinstance(ordinals, list) or any(type(index) is not int for index in ordinals)
                    or ordinals != [command['ordinal'] for command in commands]
                    or end - start != sum(len(command['writes']) for command in commands)):
                raise CommandError('nested inverse identity differs from source history', kind='history_unavailable')
            # Independent parent writes can commute without changing any image.
            # Child item paths may be re-encoded by the authenticated item codec;
            # native refs and parent storage paths retain their source order.
            expected_writes = [write for command in reversed(commands) for write in reversed(command['writes'])]
            actual_writes = record['writes'][start:end]
            if ([write['ref'] for write in actual_writes] != [write['ref'] for write in expected_writes]
                    or [write['path'] for write in actual_writes if write['ref'] == 'parent'] !=
                    [write['path'] for write in expected_writes if write['ref'] == 'parent']):
                raise CommandError('nested inverse native order differs from source history',
                                   kind='history_unavailable')


def _causal_commands(entry, commands):
    """Project causal compiler ownership without rewriting accepted captures."""
    originals = entry['records']['commands']
    for source in originals:
        for write in source.get('derived_writes', []):
            cause = write.get('cause', source['ordinal'])
            if type(cause) is not int or not 0 <= cause <= source['ordinal']:
                raise CommandError('compiler effect cause is unavailable', kind='history_unavailable')
    return [{**record, 'derived_writes': [write for source in originals
        for write in source.get('derived_writes', [])
        if write.get('cause', source['ordinal']) == record['ordinal']]}
        for record in commands]


def _check_groups(groups, *, state, address_resolver):
    from .inverse import check_intervening

    for index, (commands, dependencies, active) in enumerate(groups):
        if not active:
            continue
        later = [group for group in groups[index + 1:] if not group[2]]
        check_intervening(commands, [record for group in later for record in group[0]],
                          include_dependents=True, state=state, dependencies=dependencies,
                          later_dependencies=[dependency for group in later for dependency in group[1]],
                          address_resolver=address_resolver)


def check_step_dependents(records, *, state=None):
    """Recheck same-publication consumers at every inverse execution boundary."""
    if not isinstance(records, SelectedRecords):
        return
    for entry in records.entries:
        chosen = records.chosen[entry['publication_id']]
        groups = [(_causal_commands(entry, commands), dependencies, any(record['ordinal'] in chosen for record in commands))
                  for _, commands, dependencies in _steps(entry)]
        _check_groups(groups, state=state, address_resolver=records.address_resolver)


def check_later_steps(selected, history, *, state=None, address_resolver=None):
    """Check each selected step against its own complete, unselected suffix.

    The provider authenticates ancestry before calling this helper. A gap before
    a newer selected target belongs to that target's input, not its later tail.
    """
    if not isinstance(selected, SelectedRecords) or history is None:
        raise CommandError('inverse scan needs selected accepted history', kind='history_unavailable')
    selected_dependencies(selected)
    address_resolver = address_resolver or selected.address_resolver
    groups, found = [], []
    previous = None
    for entry in history:
        publication = entry['publication_id']
        if previous is not None and entry.get('previous') != previous:
            raise CommandError('inverse history is not contiguous', kind='history_unavailable')
        previous = {key: entry[key] for key in ('revision_id', 'publication_id')}
        source = next((source for source in selected.entries if source['publication_id'] == publication), None)
        if source is not None:
            if not equal(source, entry):
                raise CommandError('inverse target differs from accepted history', kind='history_unavailable')
            found.append(publication)
        chosen = selected.chosen.get(publication, set())
        for step, commands, dependencies in _steps(entry):
            active = any(record['ordinal'] in chosen for record in commands)
            groups.append((_causal_commands(entry, commands), dependencies, active))
    if found != [entry['publication_id'] for entry in selected.entries]:
        raise CommandError('inverse scan omits accepted targets', kind='history_unavailable')
    _check_groups(groups, state=state, address_resolver=address_resolver)
