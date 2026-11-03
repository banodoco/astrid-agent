"""Guarded authored inverses over trusted accepted records, never snapshots."""
from __future__ import annotations

import copy
from contextlib import contextmanager

from ._collections import (
    KINDS,
    AcceptedRecords,
    arrange,
    container,
    member,
    restoration_orders,
    selection_overlap,
)
from ._collections import (
    order as collection_order,
)
from ._command_state import (
    ABSENT,
    CommandError,
    State,
    equal,
    logical_address,
    one,
    overlaps,
    present,
    root_identity,
)
from ._recording import Recording, guard, selection
from ._root_inverse import (
    arrange_restored,
    clip_cache_stages,
    occurrence_orders,
    recover_source,
)


def _inverse_item_path(state, ref, path, records):
    if (not ref.endswith('/shot') or list(path[:2]) != ['payload', 'items']
            or len(path) < 3 or not path[2].startswith('item_id:')
            or not isinstance(records, AcceptedRecords)):
        return path
    from ._step_undo import entry_records

    identity = path[2].split(':', 1)[1]
    current = state.shot_item_map(ref)
    candidates = set()
    for entry in records.entries:
        commands = entry_records(records, entry)
        captures = [capture for command in commands for capture in [*command['writes'], *command['reads']]
                    if capture['ref'] == ref and capture['path'][:3] == list(path[:3])]
        if not captures:
            continue
        before, after = records.load_boundary(entry)
        prior = State(before, closures=state.closures)
        mapping = prior.shot_item_map(ref)
        if identity not in mapping:
            raise CommandError('inverse item is outside its recorded input', kind='history_unavailable', ref=ref)
        # The source codec is placement-local and stable across main's
        # materialization. A different owner's map cannot authorize this path.
        if prior.occurrence(ref.removesuffix('/shot'))['shot_id'] != state.occurrence(
                ref.removesuffix('/shot'))['shot_id']:
            raise CommandError('inverse item owner changed', kind='inverse_conflict', ref=ref)
        resolver = getattr(records, 'address_resolver', None)
        shot = state.occurrence(ref.removesuffix('/shot'))['shot_id']
        source = state.bundle['source_mapping']['shots'][shot]
        exact = any(equal(source, boundary['source_mapping']['shots'].get(shot)) for boundary in (before, after))
        if not exact and resolver is not None:
            physical = 'shot-revision:' + source['revision_id']
            bound = {resolver(capture, ref, path)[0] for capture in captures}
            for command in commands:
                for binding in command.get('bindings', []):
                    if (binding.get('key') == 'commit.materialization' and binding.get('owner') == ref
                            and resolver(binding, binding['physical_ref'], ())[0] in bound):
                        bound.add(resolver(binding, binding['successor'], ())[0])
            if physical not in bound:
                raise CommandError('inverse item binding changed', kind='inverse_conflict', ref=ref)
        elif not exact:
            raise CommandError('inverse item source changed', kind='inverse_conflict', ref=ref)
        candidates.update(key for key, local in current.items() if local == mapping[identity])
    if len(candidates) != 1:
        raise CommandError('inverse item correspondence is unavailable', kind='inverse_conflict', ref=ref)
    return [*path[:2], 'item_id:' + candidates.pop(), *path[3:]]


class _ItemState(State):
    def __init__(self, state, records):
        super().__init__(state.bundle, closures=state.closures)
        self.records = records

    def get(self, ref, path):
        return super().get(ref, _inverse_item_path(self, ref, path, self.records))


def _physical_state(state, ref, path, records):
    if ref.endswith('/shot') and list(path[:2]) == ['payload', 'items'] and len(path) > 2:
        return _ItemState(state, records)
    if (ref != 'parent' or list(path) != ['occurrences']
            or not isinstance(records, AcceptedRecords)
            or getattr(records, 'address_resolver', None) is None):
        return state
    if 'occurrences' in state.bundle['parent']:
        raise CommandError('occurrence aggregate has contradictory storage', kind='inverse_conflict',
                           ref=ref, path=path)
    # A view of the existing canonical root rows, never a stored parent mirror.
    from ._collections import order

    order(state, 'parent', 'occurrence')
    return State({**state.bundle, 'parent': {
        **state.bundle['parent'], 'occurrences': state.bundle['placements']}}, closures=state.closures)


def _input_read_state(state, read, record, records):
    """Authenticate restored item identities at this command's accepted input."""
    if (read['ref'].endswith('/shot') and read['path'][:1] == ['payload']
            and isinstance(records, AcceptedRecords)):
        from ._step_undo import entry_records

        owner = read['ref'].removesuffix('/shot')
        current = state.entity(owner, optional=True)
        for entry in records.entries:
            if not any(item is record for item in entry_records(records, entry)):
                continue
            before, _ = records.load_boundary(entry)
            prior = one(before['placements'], 'occurrence_id', owner.removeprefix('occurrence:'), optional=True)
            if current is not None and prior is not None and current['shot_id'] == prior['shot_id']:
                shot = current['shot_id']
                source = before['source_mapping']['shots'].get(shot)
                sources = state.bundle['source_mapping']['shots']
                if (source is not None and shot not in sources
                        and state.bundle['shots'][shot].get('_created') is True
                        and any(write['ref'] == 'shot:' + shot and not write['path']
                                and write['after'] == ABSENT and write['before'] != ABSENT
                                for write in record['writes'])
                        and equal(state.get('shot:' + shot, []), State(before).get('shot:' + shot, []))):
                    # Redo has recreated this exact recorded root, but has no
                    # compiler source yet. Earlier native writes may precede
                    # its deletion; the accepted input owns this read's codec.
                    sources = {**sources, shot: source}
                if source is not None and equal(source, sources.get(shot)):
                    # This is a read-only codec view, not the compiler baseline.
                    # Keep H and every authored value; only the exact restored
                    # owner/source may use its own authenticated input document.
                    state = State({**state.bundle, 'source_mapping': {
                        **state.bundle['source_mapping'],
                        'shots': sources,
                        '_runtime_document': before['source_mapping']['_runtime_document']}},
                        closures=state.closures)
            break
    return _physical_state(state, read['ref'], read['path'], records)


def _set_physical(state, ref, path, value, records):
    path = _inverse_item_path(state, ref, path, records)
    projected = _physical_state(state, ref, path, records)
    if isinstance(projected, _ItemState):
        projected = state
    if projected is not state:
        _validate_occurrences(state, value)
    projected.set(ref, path, value)
    if ref == 'parent' and list(path) in (['clips'], ['config'], ['config', 'clips']):
        state.parent_clip_path = State(state.bundle).parent_clip_path
    if projected is not state:
        state.bundle['placements'] = projected.bundle['placements']


def _validate_occurrences(state, value):
    rows = value.get('value')
    if value['state'] != 'present' or not isinstance(rows, list):
        raise CommandError('occurrence aggregate requires canonical rows', kind='history_unavailable')
    identities = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('occurrence_id'), str):
            raise CommandError('occurrence aggregate identity is unavailable', kind='history_unavailable')
        state.validate_root('occurrence:' + row['occurrence_id'], row)
        identities.append(row['occurrence_id'])
    if len(set(identities)) != len(identities):
        raise CommandError('occurrence aggregate identity is ambiguous', kind='history_unavailable')


@contextmanager
def _recording_storage(ctx, ref, path, records):
    path = _inverse_item_path(ctx.state, ref, path, records)
    original, captured = ctx.state, ctx.input_state
    projected = (_physical_state(original, ref, path, records)
                 if ref == 'parent' and list(path) == ['occurrences'] else original)
    captured_view = (_physical_state(captured, ref, path, records)
                     if captured is not None and projected is not original else captured)
    ctx.state, ctx.input_state = projected, captured_view
    try:
        yield path
        if ctx.state is not original:
            value = ctx.state.get(ref, path)
            _validate_occurrences(original, value)
            original.bundle['placements'] = copy.deepcopy(value['value'])
    finally:
        ctx.state, ctx.input_state = original, captured


def _row_identity(row, token):
    if token[0] == 'text-binding':
        from .native_text_binding import binding_key
        return binding_key(row)
    return row.get('id')


def _patch(state, path, replacement, *, insertion_order=None, append_text=False):
    if not path:
        return copy.deepcopy(replacement)
    if state == ABSENT or state.get('value') is None:
        if replacement == ABSENT:
            return copy.deepcopy(state)
        raise CommandError('patch requires historical container structure', kind='history_unavailable')
    value = copy.deepcopy(state['value'])
    node = value
    for token in path[:-1]:
        if isinstance(token, tuple):
            matches = [row for row in node if _row_identity(row, token) == token[1]]
            if len(matches) != 1:
                raise CommandError('ambiguous collection alias', kind='identity_conflict')
            node = matches[0]
        elif isinstance(node, list):
            node = (one(node, 'item_id', token.split(':', 1)[1])
                    if token.startswith('item_id:') else node[int(token)])
        elif isinstance(node, dict):
            if token not in node:
                if replacement == ABSENT:
                    return copy.deepcopy(state)
                raise CommandError('patch requires historical container structure', kind='history_unavailable')
            node = node[token]
        else:
            raise CommandError('patch parent must be an object or array', kind='history_unavailable')
        if node is None:
            if replacement == ABSENT:
                return copy.deepcopy(state)
            raise CommandError('patch requires historical container structure', kind='history_unavailable')
    if isinstance(path[-1], tuple):
        identity = path[-1][1]
        indices = [i for i, row in enumerate(node) if _row_identity(row, path[-1]) == identity]
        if len(indices) > 1:
            raise CommandError('ambiguous collection alias', kind='identity_conflict')
        if not indices and replacement['state'] == 'present' and callable(insertion_order):
            insertion_order = insertion_order()
        if replacement['state'] == 'absent':
            if indices:
                node.pop(indices[0])
        elif indices:
            node[indices[0]] = copy.deepcopy(replacement['value'])
        elif insertion_order is not None and identity in insertion_order:
            identities = [_row_identity(row, path[-1]) for row in node]
            if (len(identities) != len(set(identities))
                    or any(item not in insertion_order for item in identities)
                    or identities != [item for item in insertion_order if item in identities]):
                raise CommandError('collection alias order differs from history', kind='history_unavailable')
            following = set(insertion_order[insertion_order.index(identity) + 1:])
            index = next((i for i, row in enumerate(node) if _row_identity(row, path[-1]) in following), len(node))
            node.insert(index, copy.deepcopy(replacement['value']))
        elif path[-1][0] == 'text-binding' and (not node or append_text):
            node.append(copy.deepcopy(replacement['value']))
        else:
            raise CommandError('collection alias insertion needs historical order', kind='history_unavailable')
        return present(value)
    if isinstance(node, list):
        index = (node.index(one(node, 'item_id', path[-1].split(':', 1)[1]))
                 if path[-1].startswith('item_id:') else int(path[-1]))
        if replacement['state'] == 'absent':
            node.pop(index)
        else:
            node[index] = copy.deepcopy(replacement['value'])
        return present(value)
    if not isinstance(node, dict):
        raise CommandError('patch parent must be an object or array', kind='history_unavailable')
    if replacement['state'] == 'absent':
        node.pop(path[-1], None)
    else:
        node[path[-1]] = copy.deepcopy(replacement['value'])
    return present(value)


def _root_addresses(records, state=None):
    """Resolve aliases by authored owner, never a shared source shot identity."""
    owners = {}
    if state is not None:
        for row in state.bundle['placements']:
            ref = 'occurrence:' + row['occurrence_id']
            if ref in owners:
                raise CommandError('ambiguous root context', kind='history_unavailable', ref=ref)
            owners[ref] = row['shot_id']
    for record in records:
        for write in record['writes']:
            if root_identity(write['ref']) is None or not write['ref'].startswith('occurrence:') or write['path']:
                continue
            for value in (write['before'], write['after']):
                if value['state'] == 'present':
                    shot = value['value'].get('shot_id')
                    if not isinstance(shot, str) or root_identity('shot:' + shot) is None:
                        raise CommandError('root context is unavailable', kind='history_unavailable', ref=write['ref'])
                    if write['ref'] in owners and owners[write['ref']] != shot:
                        if state is None or not state.matches(write['ref'], ['shot_id'], present(shot)):
                            raise CommandError('root context changed', kind='history_unavailable', ref=write['ref'])
                    else:
                        owners[write['ref']] = shot

    def address(ref, path):
        if ref.startswith('occurrence:') and ref.endswith('/registry'):
            ref, path = ref.removesuffix('/registry'), ('source', 'registry', *path)
        if ref.endswith('/shot') and tuple(path[:1]) == ('payload',):
            owner = ref.removesuffix('/shot')
            if owner in owners:
                return 'shot:' + owners[owner], tuple(path)
        root, path = logical_address(ref, path)
        if root.startswith('occurrence:') and path[:1] in (('source',), ('payload',)) and root in owners:
            return 'shot:' + owners[root], (('internal_timeline' if path[0] == 'source' else 'payload'), *path[1:])
        return root, path

    return address


def coalesce(records, state=None, *, cache_stages=None):
    """Retain first-before/last-after, folding descendants into authored roots."""
    if (state is None and isinstance(records, AcceptedRecords)
            and any(write['ref'].endswith('/shot') and write['path'][:2] == ['payload', 'items']
                    for record in records for write in record['writes'])):
        _, after = records.load_boundary(records.entries[-1])
        state = State(after)
    root_address = _root_addresses(records, state)
    registry_writes = [write for record in records for write in record['writes']
                       if write['ref'] == 'parent' and write['path'][:1] == ['registry']]
    direct_registry = bool(registry_writes) and (
        registry_writes[0]['path'] != ['registry'] or registry_writes[0]['before'] != ABSENT)
    # An authored absent edge proves that clips acquired independent physical
    # storage. Logical overlap with config is still used by history scans.
    clip_writes = [write for record in records for write in record['writes']
                   if write['ref'] == 'parent' and write['path'] == ['clips']]
    physical_clips = any(ABSENT in (write['before'], write['after']) for write in clip_writes)
    direct_clips = bool(clip_writes) and clip_writes[0]['before'] != ABSENT
    addresses = {}
    origins = {}

    def source_address(write):
        # A direct registry write targets physical storage. Folding it into a
        # config aggregate would invent a mirror absent from that capture.
        if write['ref'] == 'parent' and write['path'][:1] == ['registry']:
            return 'parent', tuple(write['path'])
        if physical_clips and write['ref'] == 'parent' and write['path'][:1] == ['clips']:
            return 'parent', tuple(write['path'])
        if physical_clips and direct_clips and write['ref'].startswith('parent/clip:'):
            return 'parent', ('clips', ('id', write['ref'].removeprefix('parent/clip:')), *write['path'])
        ref, path = root_address(write['ref'], write['path'])
        # Source rows are owned by an occurrence, but aren't JSON children of
        # its placement row. Ownership overlap must not fold them into that row.
        if ref.startswith('occurrence:') and path[:1] == ('source',):
            return ref + '/source', path[1:]
        return ref, path

    def value_address(write):
        captured = addresses.get(id(write))
        return captured[1] if captured is not None else source_address(write)

    def fold(aggregate, leaf, replacement, *, side, insertion_order=None, append_text=False):
        prefix, path = value_address(aggregate)[1], value_address(leaf)[1]
        if (aggregate['ref'].startswith('shot:') and '/' not in aggregate['ref']
                and leaf['ref'].endswith('/shot')
                and (leaf['path'][:2] == ['payload', 'items']
                     or leaf['path'] == ['payload'] and isinstance(replacement.get('value'), dict)
                     and replacement['value'].get('items'))):
            projection = state
            if isinstance(records, AcceptedRecords):
                from ._step_undo import entry_records

                projection = None
                for entry in records.entries:
                    if any(record is origins[id(leaf)] for record in entry_records(records, entry)):
                        before, _ = records.load_boundary(entry)
                        projection = State(before)
                        break
            if projection is None:
                raise CommandError('item folding requires its source state', kind='history_unavailable')
            mapping = projection.shot_item_map(leaf['ref'])
            path = tuple('item_id:' + mapping[token.split(':', 1)[1]]
                         if isinstance(token, str) and token.startswith('item_id:') else token for token in path)
            replacement = projection.project_shot_items(leaf['ref'], leaf['path'], replacement)
        return _patch(aggregate[side], path[len(prefix):], replacement,
                      insertion_order=insertion_order, append_text=append_text)

    result = []
    for record in records:
        writes = copy.deepcopy(record['writes'])
        resolver = getattr(records, 'address_resolver', None)
        aggregate = None
        projecting = isinstance(records, AcceptedRecords) and resolver is not None
        for raw, projected in zip(record['writes'], writes):
            if raw['ref'] == 'parent' and raw['path'] == ['occurrences']:
                aggregate = projected if isinstance(records, AcceptedRecords) and resolver is not None else None
            elif (aggregate is not None and raw['ref'].startswith('occurrence:')
                    and '/' not in raw['ref'] and not raw['path']):
                identity = raw['ref'].removeprefix('occurrence:')
                rows = aggregate['after'].get('value')
                if (resolver(raw, raw['ref'], raw['path'])
                        != ('parent', ('occurrences', ('occurrence_id', identity)))
                        or aggregate['after']['state'] != 'present' or not isinstance(rows, list)
                        or any(not isinstance(row, dict) or not isinstance(row.get('occurrence_id'), str)
                               for row in rows)
                        or len({row['occurrence_id'] for row in rows}) != len(rows)):
                    raise CommandError('occurrence aggregate binding is unavailable', kind='history_unavailable')
                indices = [i for i, row in enumerate(rows) if row['occurrence_id'] == identity]
                if (len(indices) != 1 or not equal(present(rows[indices[0]]), raw['before'])
                        or (raw['after'] != ABSENT
                            and (not isinstance(raw['after'].get('value'), dict)
                                 or raw['after']['value'].get('occurrence_id') != identity))):
                    raise CommandError('occurrence aggregate alias changed', kind='history_unavailable',
                                       ref=raw['ref'])
                # Project only the landing guard. Keep every native alias write
                # for sequential guards, source restoration and history scans.
                if raw['after'] == ABSENT:
                    rows.pop(indices[0])
                else:
                    rows[indices[0]] = copy.deepcopy(raw['after']['value'])
            elif (projecting and raw['ref'].startswith('occurrence:')
                    and '/' not in raw['ref'] and raw['path']):
                # A nested field of an aggregate row (for example its timing) is
                # part of the state the landing guard observes. Fold it into the
                # row; leave fields owned elsewhere (shot payload) untouched.
                identity = raw['ref'].removeprefix('occurrence:')
                address = resolver(raw, raw['ref'], raw['path'])
                prefix = ('occurrences', ('occurrence_id', identity))
                if address[0] != 'parent' or tuple(address[1][:2]) != prefix or len(address[1]) <= 2:
                    continue
                # Within the aggregate's own record, project onto that write;
                # in a later record, onto the aggregate accepted so far.
                target = aggregate if aggregate is not None else next(
                    (old for old in result if old['ref'] == 'parent' and old['path'] == ['occurrences']), None)
                if target is None:
                    continue
                rows = target['after'].get('value')
                if (target['after']['state'] != 'present' or not isinstance(rows, list)
                        or any(not isinstance(row, dict) or not isinstance(row.get('occurrence_id'), str)
                               for row in rows)
                        or len({row['occurrence_id'] for row in rows}) != len(rows)):
                    raise CommandError('occurrence aggregate binding is unavailable', kind='history_unavailable')
                indices = [i for i, row in enumerate(rows) if row['occurrence_id'] == identity]
                if len(indices) != 1:
                    raise CommandError('occurrence aggregate alias changed', kind='history_unavailable',
                                       ref=raw['ref'])
                field = address[1][2:]
                node, current = rows[indices[0]], None
                for token in field:
                    if not isinstance(token, str) or not isinstance(node, dict) or token not in node:
                        node = ABSENT
                        break
                    node = node[token]
                current = ABSENT if node is ABSENT else present(node)
                if not equal(current, raw['before']):
                    raise CommandError('occurrence aggregate alias changed', kind='history_unavailable',
                                       ref=raw['ref'])
                rows[indices[0]] = _patch(present(rows[indices[0]]), field, raw['after'])['value']
        aliases = []
        for write in writes:
            aliases.append(write)
            if write['ref'] == 'parent' and write['path'] == ['registry']:
                direct_registry = write['after'] != ABSENT
            if direct_registry and write['ref'].startswith('parent/asset:'):
                # Project only declared storage at this native boundary. Keep
                # source writes unchanged for sequential reversal and scans.
                aliases.append({**write, 'ref': 'parent',
                                'path': ['registry', 'assets',
                                         write['ref'].removeprefix('parent/asset:'), *write['path']]})
        writes = aliases
        for raw in [*writes, *(cache_stages or {}).get(id(record), [])]:
            write = copy.deepcopy(raw)
            address = source_address(write)
            # Keep the source-stage address even after later storage changes.
            addresses[id(write)] = write, address
            origins[id(write)] = record
            if write['ref'] == 'parent' and write['path'] == ['clips']:
                direct_clips = write['after'] != ABSENT
            if (physical_clips and write['ref'].startswith('parent/clip:')
                    and address[1][:1] == ('clips',)):
                # Update only a config mirror actually captured by an earlier
                # aggregate. The physical clip root remains a separate guard.
                mirror = ('config', *address[1])
                for old in result:
                    root, prefix = value_address(old)
                    if (root == 'parent' and mirror[:len(prefix)] == prefix
                            and old['after']['state'] == 'present'
                            and isinstance(old['after'].get('value'), dict)
                            and 'clips' in old['after']['value']):
                        old['after'] = _patch(old['after'], mirror[len(prefix):], write['after'])
            ancestor = next((old for old in result if value_address(old)[0] == address[0]
                             and address[1][:len(value_address(old)[1])] == value_address(old)[1]), None)
            if ancestor is not None:
                insertion_order = None
                entity = member(write['ref'])
                if entity is not None and not write['path'] and write['before'] == ABSENT:
                    # A root born earlier in the batch can gain child rows. Use
                    # the same recorded append/boundary proof as staged inverse.
                    orders = restoration_orders(records, only=record)
                    insertion_order = [member(ref)[2] for ref in orders[entity[:2]][1]]
                ancestor['after'] = fold(ancestor, write, write['after'], side='after', insertion_order=insertion_order,
                                           append_text=(ancestor['before'] == ABSENT
                                               and ancestor['path'] == ['payload', 'text_bindings']))
                continue
            descendants = [old for old in result
                           if value_address(old)[0] == address[0]
                           and value_address(old)[1][:len(address[1])] == address[1]]
            for old in reversed(descendants):
                entity = member(old['ref'])
                insertion_order = None
                if entity is not None and not old['path'] and old['before'] != ABSENT:
                    def historical_order(entity=entity):
                        return [member(ref)[2] for ref in restoration_orders(records)[entity[:2]][0]]
                    insertion_order = historical_order
                write['before'] = fold(write, old, old['before'], side='before', insertion_order=insertion_order)
                result.remove(old)
            result.append(write)
    return result


def _effect_writes(record):
    # This mirror has the same logical sites as the authored clip writes. Its
    # aggregate storage copy must not turn disjoint clip fields into conflicts.
    return [*record['writes'], *(write for write in record.get('derived_writes', [])
             if not (write['ref'] == 'parent' and write['path'] == ['config', 'clips']))]


def _dependency_overlap(dependency, write, address):
    if (not isinstance(dependency, dict) or dependency.get('kind') not in ('field', 'collection', 'scope')
            or not isinstance(dependency.get('ref'), str) or not isinstance(dependency.get('path'), list)):
        raise CommandError('accepted step dependency is unavailable', kind='history_unavailable')
    left = address(dependency, dependency['ref'], dependency['path'])
    right = address(write, write['ref'], write['path'])
    if not overlaps(left, right):
        return False
    if dependency['kind'] != 'collection':
        return True
    root, prefix = left
    other, path = right
    if root != other:
        return True  # A whole-closure write overlaps every collection.
    if path == prefix:
        key = 'occurrence_id' if prefix[-1:] == ('occurrences',) else 'id'

        def identities(value):
            rows = value.get('value')
            if value.get('state') != 'present' or not isinstance(rows, list):
                return None
            if any(not isinstance(row, dict) or not isinstance(row.get(key), str) for row in rows):
                return None
            return [row[key] for row in rows]

        before, after = identities(write['before']), identities(write['after'])
        if before is None or after is None:
            return True
        return (set(before) != set(after)
                or (dependency.get('order') is not None and before != after))
    if path[:len(prefix)] != prefix:
        return True  # Replacing a containing object invalidates the shape guard.
    remaining = path[len(prefix):]
    if remaining[:1] == (('shape',),):
        return True
    if len(remaining) == 1:
        return (write['before']['state'] != write['after']['state']
                or (write['before'] != ABSENT and write['after'] != ABSENT
                    and write['before']['value'].get('id') != write['after']['value'].get('id')))
    return remaining[1:] in (('id',), ('occurrence_id',))


def check_intervening(records, intervening_records, *, include_reads=True, state=None, identities_only=False,
                      include_dependents=False, dependencies=(), later_dependencies=(), address_resolver=None):
    if intervening_records is None:
        raise CommandError('complete intervening history is required', kind='history_unavailable')
    guarded, source_roots, writes, bound_owners = [], [], [], []
    selections = []
    for record in records:
        if not isinstance(record, dict) or any(key not in record for key in ('writes', 'reads', 'selections', 'created')):
            raise CommandError('complete accepted recorder history is required', kind='history_unavailable')
        guarded.extend((w['ref'], tuple(w['path'])) for w in _effect_writes(record))
        writes.extend(_effect_writes(record))
        selections.extend(record['selections'])
        if include_reads:
            for read in record['reads']:
                guarded.append((read['ref'], tuple(read['path'])))
                if read['ref'].endswith('/shot'):
                    guarded.append((read['ref'].removesuffix('/shot'), ('shot_id',)))
                    if read['path'] == ['shot_id'] and isinstance(read['state'].get('value'), str):
                        source_roots.append(('shot:' + read['state']['value'], ()))
            for binding in record.get('bindings', []):
                ref = binding['ref']
                for owner in binding.get('owners', []):
                    bound_owners.append((binding, owner.removesuffix('/shot'), ('shot_id',)))
                if member(ref) is not None:
                    guarded.append((ref, ('id',)))
                elif ref.startswith('occurrence:') and '/' not in ref:
                    guarded.append((ref, ('occurrence_id',)))
    if identities_only:
        # Ordinary replay checks values and membership on the final head. Only
        # identity replacement must remain sensitive to intervening history.
        refs = [ref for ref, _ in guarded]
        refs.extend(ref for selected in selections
                    for ref in [selected['owner'], *selected['members'], *selected['boundaries']])
        refs.extend(ref for ref, _ in source_roots)
        source_roots = []
        guarded = []
        for ref in refs:
            entity = member(ref)
            if entity is not None and entity[1] != 'text-binding':
                guarded.append((ref, ('id',)))
            owner = ref.split('/', 1)[0]
            root = root_identity(owner)
            guarded.append((owner, (root[0] + '_id',) if root else ('id',)))
        selections = []
    root_address = _root_addresses(records, state)
    authored = {id(capture) for record in records for capture in
                [*_effect_writes(record), *record['reads'], *record['selections'], *record.get('bindings', [])]}
    authored.update(id(dependency) for dependency in dependencies)

    def address(capture, ref, path):
        if address_resolver is not None:
            resolved = address_resolver(capture, ref, path)
            if id(capture) in authored and ref.startswith('occurrence:') and '/' in ref:
                owner = ref.split('/', 1)[0] + '/shot'
                for record in records:
                    for binding in record.get('bindings', []):
                        if binding.get('key') != 'commit.materialization' or binding.get('owner') != owner:
                            continue
                        source = address_resolver(binding, binding['physical_ref'], ())
                        output = address_resolver(binding, binding['successor'], ())
                        if source != output and resolved[0] == source[0]:
                            # Undo removes this owner's fork, not its retained
                            # sibling's equal input image. Keep the field path.
                            return output[0], resolved[1]
            return resolved
        return root_address(ref, path)

    def selected_overlap(selected, write):
        if address_resolver is None:
            return selection_overlap(selected, write)
        kind = KINDS.get(selected['key'])
        if kind is not None:
            ref, path = container(selected['owner'], kind)
            dependency = {'kind': 'collection', 'ref': ref, 'path': list(path), 'order': selected['order']}
        elif selected['key'] in ('placement.owners', 'text-binding.owners', 'move.blockers'):
            # These queries consume occurrence membership as well as captured
            # fields. Keep the shape in the query's original physical boundary.
            dependency = {'kind': 'collection', 'ref': 'parent', 'path': ['occurrences'],
                          'order': selected['order']}
        else:
            return False

        def selection_address(capture, ref, path):
            return address(selected if capture is dependency else capture, ref, path)

        return _dependency_overlap(dependency, write, selection_address)

    def binding_consumes(binding, changed):
        bound = address(binding, binding['ref'], ())
        return (changed == ('timeline', ()) or bound == ('timeline', ())
                or changed[0] == bound[0] and bound[1][:len(changed[1])] == changed[1])

    normalized_guards = [address(capture, capture['ref'], capture['path'])
                         for record in records for capture in
                         [*_effect_writes(record), *(record['reads'] if include_reads else [])]]
    normalized_guards.extend(address(binding, ref, path) for binding, ref, path in bound_owners)
    # A shared source can remain reachable after its authored fork. Its input
    # address then stays distinct from the authenticated output that undo removes.
    # Guard that output as well; ordinary revision continuations resolve alike.
    output_guards = []
    if address_resolver is not None and not identities_only:
        for record in records:
            for binding in record.get('bindings', []):
                if binding.get('key') != 'commit.materialization':
                    continue
                source = address(binding, binding['physical_ref'], ())
                output = address(binding, binding['successor'], ())
                if source != output:
                    output_guards.append(output)
        normalized_guards.extend(output_guards)
    if identities_only:
        normalized_guards = [root_address(*prior) for prior in guarded]
    for record in intervening_records:
        if not isinstance(record, dict) or 'writes' not in record:
            raise CommandError("complete intervening write history is required", kind='history_unavailable')
        for write in _effect_writes(record):
            location = write['ref'], tuple(write['path'])
            resolved = address(write, *location)
            aliases = [resolved]
            if address_resolver is not None and write['ref'].startswith('shot:') and not write['path']:
                # A native shot aggregate writes its physical shot as well as
                # its child. Resolve at this capture's boundary, not the head.
                aliases.append(address(write, write['ref'], ['payload']))
            if ((address_resolver is None and any(overlaps(location, prior) for prior in [*guarded, *source_roots]))
                    or any(overlaps(alias, prior) for alias in aliases for prior in normalized_guards)
                    or any(selected_overlap(s, write) for s in selections)
                    or any(_dependency_overlap(dependency, write, address) for dependency in dependencies)):
                raise CommandError("an intervening publication wrote a guarded address",
                                   kind='intervening_write', ref=location[0], path=location[1])
            if not write['path']:
                if write['ref'] == 'parent' and guarded:
                    raise CommandError('an intervening publication replaced the timeline',
                                       kind='intervening_write', ref='parent')
                # Removing an owner also invalidates every scoped descendant.
                if any(ref.startswith(write['ref'] + '/') for ref, _ in guarded):
                    raise CommandError("an intervening publication replaced an owner",
                                       kind='intervening_write', ref=location[0])
        if include_dependents:
            if any(any(overlaps(changed, address(read, read['ref'], read['path']))
                       for read in record.get('reads', []))
                   or any(binding_consumes(binding, changed) for binding in record.get('bindings', []))
                   for changed in output_guards):
                raise CommandError('a surviving command consumed the materialized output',
                                   kind='inverse_conflict')
            for write in writes:
                changed = address(write, write['ref'], write['path'])
                if (any(overlaps(changed, address(read, read['ref'], read['path']))
                        for read in record.get('reads', []))
                        or any(selected_overlap(selected, write) for selected in record.get('selections', []))
                        or ((not write['path'] or write['path'] in (['id'], ['occurrence_id'], ['shot_id']))
                            and any(binding_consumes(binding, changed)
                                    for binding in record.get('bindings', [])))):
                    raise CommandError('a surviving command consumed the selected output',
                                       kind=_consumer_conflict(write), ref=write['ref'], path=write['path'])
    if include_dependents:
        if any(overlaps(changed, address(dependency, dependency['ref'], dependency['path']))
               for changed in output_guards for dependency in later_dependencies):
            raise CommandError('a surviving step consumed the materialized output', kind='inverse_conflict')
        for write in writes:
            if any(_dependency_overlap(dependency, write, address) for dependency in later_dependencies):
                raise CommandError('a surviving step consumed the selected output',
                                   kind=_consumer_conflict(write), ref=write['ref'], path=write['path'])


def _consumer_conflict(write):
    return ('surviving_dependents' if not write['path'] and write['before'] == ABSENT
            and write['after'] != ABSENT else 'inverse_conflict')


def _dependents(state, ref):
    owner, identity = ref.rsplit('/track:', 1)

    def references(row, other):
        if not isinstance(row, dict):
            raise CommandError('unsupported declared reference row', kind='surviving_dependents', ref=ref)
        for key in ('track_ref', 'owner_ref', 'track', 'track_id'):
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                raise CommandError('unsupported declared reference edge', kind='surviving_dependents', ref=ref)
            if key in ('track_ref', 'owner_ref') and value == ref:
                return True
        return other == owner and (row.get('track') == identity or row.get('track_id') == identity)

    found = []
    owners = ['parent'] + ['occurrence:' + row['occurrence_id'] for row in state.bundle['placements']]
    for other in owners:
        for clip in state.collection(other, 'clip'):
            if references(clip, other):
                found.append(other + '/clip:' + clip['id'])
        config = state.config(other)
        for key in ('audio', 'effects'):
            for row in config.get(key, []):
                if references(row, other):
                    found.append(other + '/' + key)
        if other != 'parent':
            shot = state.bundle['shots'][state.occurrence(other)['shot_id']]['payload']
            for row in shot.get('audio_bindings', []):
                if references(row, other):
                    found.append(other + '/shot')
    for row in state.bundle['placements']:
        if references(row, 'parent') or references(row.get('placement') or {}, 'parent'):
            found.append('occurrence:' + row['occurrence_id'])
    for shot in state.bundle['shots'].values():
        for row in shot['payload'].get('audio_bindings', []):
            if references(row, None):
                found.append('shot:' + shot['shot_id'])
    return found


def _asset_dependents(state, ref):
    owner, identity = ref.rsplit('/asset:', 1)

    def references(value, local):
        if isinstance(value, dict):
            return ((local and any(value.get(key) == identity for key in ('asset', 'asset_id')))
                    or any(value.get(key) == ref for key in ('asset_ref', 'owner_ref'))
                    or any(references(value.get(key), local) for key in
                           ('clips', 'children', 'audio', 'effects', 'audio_bindings')))
        return isinstance(value, list) and any(references(child, local) for child in value)

    owners = ['parent', *('occurrence:' + row['occurrence_id'] for row in state.bundle['placements'])]
    # During reversal the parent mirror may still contain the swapped asset.
    # Dependents use the authoritative raw rows, just as normal selection does.
    parent = {**state.bundle['parent'], 'clips': state.collection('parent', 'clip'),
              'config': {**state.config('parent'), 'clips': state.collection('parent', 'clip')}}
    return (any(references(node, owner == 'parent') for node in (parent, parent['config']))
            or any(references(state.config(other), other == owner) for other in owners[1:])
            or any(references(shot['payload'], False) for shot in state.bundle['shots'].values()))


def _legacy_inverse_sites(records, record):
    """Recover only known v1 inverse read/create sites from its verified input."""
    if record['command']['op'] != 'timeline.inverse' or not isinstance(records, AcceptedRecords):
        raise CommandError('original read stage is unavailable', kind='history_unavailable')
    state, after = records.boundary(record)
    from ._root_inverse import normalize_root_inputs

    normalize_root_inputs(state.bundle, [record])
    reads = [read for read in record['reads']
             if _physical_state(state, read['ref'], read['path'], records).matches(
                 read['ref'], read['path'], read['state'])]
    selections = [selected for selected in record['selections']
                  if equal(selection(state, selected['key'], selected['owner'], selected['members']), selected)]
    for write in record['writes']:
        guard(_physical_state(state, write['ref'], write['path'], records),
              write['ref'], write['path'], write['before'], kind='history_unavailable')
        # The old inverse read immediately before each authored reversal.
        reads.append({'ref': write['ref'], 'path': write['path'], 'state': write['before']})
        entity = member(write['ref'])
        if entity is not None and not write['path'] and write['before'] == ABSENT:
            owner, kind, _ = entity
            if kind == 'track':
                selections.append(selection(state, 'track.create', owner))
        if (root_identity(write['ref']) is not None and write['ref'].startswith('occurrence:')
                and not write['path'] and write['after'] != ABSENT):
            recover_source(state, write['after']['value'], after.bundle)
        _set_physical(state, write['ref'], write['path'], write['after'], records)
    return reads, selections


def _materialized_payload_read(state, read, record, records):
    """Compare the entire payload in its accepted input's physical identities."""
    if (not isinstance(records, AcceptedRecords) or not read['ref'].endswith('/shot')
            or read['path'] != ['payload'] or read['state']['state'] != 'present'):
        return False
    from ._step_undo import entry_records

    for entry in records.entries:
        commands = entry_records(records, entry)
        if not any(item is record for item in commands):
            continue
        before, _ = records.load_boundary(entry)
        owner = read['ref'].removesuffix('/shot')
        prior = State(before, closures=state.closures)
        shot = state.occurrence(owner)['shot_id']
        if prior.occurrence(owner)['shot_id'] != shot:
            return False
        source = before['source_mapping']['shots'].get(shot)
        current = state.bundle['source_mapping']['shots'].get(shot)
        if source is None or current is None:
            return False
        from astrid.core.timeline.authoring_bundle import _remap_known_item_references

        expected = _remap_known_item_references(read['state']['value'], source['item_id_map'])
        actual = state.bundle['shots'][shot]['payload']
        link = 'internal_timeline_revision_id'
        # Only the exact compiler link verified by each immutable closure may
        # change. Every other payload field, including namespaces, stays exact.
        original = before['shots'][shot]['base_payload']
        if ((link in expected) != (link in original)
                or expected.get(link) != original.get(link)
                or (link in expected and expected[link] != source[link])
                or actual.get(link) != current[link]):
            return False
        expected[link] = current[link]
        return equal(expected, actual)
    return False


def _authenticated_timeline_scope_binding(record, binding):
    """Recognize the closure scope binding without treating it as an entity."""
    if (binding.get('key') not in {'commit.read', 'commit.atomic'}
            or binding.get('ref') != 'timeline' or binding.get('path') != []
            or not isinstance(binding.get('physical_ref'), str)
            or not isinstance(binding.get('revision_id'), str)
            or not isinstance(binding.get('content_digest'), str)):
        return False
    pin = {'revision_id': binding['revision_id'], 'content_digest': binding['content_digest']}
    return any(read['ref'] == 'timeline' and read['path'] == []
               and equal(read['state'], present(pin)) for read in record['reads'])


def _authenticated_timeline_scope_read(record, read):
    if read['ref'] != 'timeline' or read['path'] != [] or read['state']['state'] != 'present':
        return False
    return any(_authenticated_timeline_scope_binding(record, binding)
               and equal(read['state'], present({key: binding[key] for key in ('revision_id', 'content_digest')}))
               for binding in record.get('bindings', []))


def _validate_inputs(state, record, records):
    sites = None

    def proven_sites():
        if record['command']['op'] == 'timeline.inverse':
            return (*_legacy_inverse_sites(records, record), set())
        # Recorder writes are ordered even though reads and selections are not.
        # Reconstruct only those stages, retaining unrelated current authored data.
        staged = Recording(copy.deepcopy(state.bundle), record['command'], record['ordinal'],
                           closures=state.closures)
        stage = staged.state
        reads, selections, bindings = [], [], set()
        for write in [None, *record['writes']]:
            if write is not None:
                guard(_physical_state(stage, write['ref'], write['path'], records),
                      write['ref'], write['path'], write['before'], kind='history_unavailable')
                root = root_identity(write['ref'])
                if root is not None and not write['path'] and write['before'] == ABSENT and write['after'] != ABSENT:
                    created = {'ref': write['ref'], 'kind': root[0],
                               'owner': None if root[0] == 'shot' else 'parent'}
                    if record['created'].count(created) != 1:
                        raise CommandError('root creation context is unavailable', kind='history_unavailable',
                                           ref=write['ref'])
                    staged.create_entity(**created, value=write['after']['value'], restoring=True)
                else:
                    _set_physical(stage, write['ref'], write['path'], write['after'], records)
            reads.extend(read for read in record['reads']
                         if _input_read_state(stage, read, record, records).matches(
                             read['ref'], read['path'], read['state']))
            selections.extend(selected for selected in record['selections']
                              if equal(selection(stage, selected['key'], selected['owner'],
                                                 selected['members']), selected))
            bindings.update(binding['ref'] for binding in record.get('bindings', [])
                            if (_authenticated_timeline_scope_binding(record, binding)
                                or ('physical_ref' not in binding
                                    and stage.entity(binding['ref'], optional=True) is not None)))
        return reads, selections, bindings

    for read in record['reads']:
        if (_authenticated_timeline_scope_read(record, read)
                or _input_read_state(state, read, record, records).matches(
                    read['ref'], read['path'], read['state'])):
            continue
        if sites is None:
            sites = proven_sites()
        if (not any(equal(read, site) for site in sites[0])
                and not _materialized_payload_read(state, read, record, records)):
            raise CommandError('original inverse read changed', kind='inverse_dependency_changed',
                               ref=read['ref'], path=read['path'])
    for selected in record['selections']:
        actual = selection(state, selected['key'], selected['owner'], selected['members'])
        if equal(actual, selected):
            continue
        if sites is None:
            sites = proven_sites()
        if not any(equal(selected, site) for site in sites[1]):
            raise CommandError('inverse selection changed', kind='selection_changed', ref=selected['owner'])
    created = {item['ref'] for item in record['created']}
    for binding in record.get('bindings', []):
        if 'physical_ref' in binding and isinstance(records, AcceptedRecords):
            # Accepted physical bindings use the resolver required by
            # apply_inverse, not entity aliases after historical reversal.
            continue
        ref = binding['ref']
        if (ref not in created and not _authenticated_timeline_scope_binding(record, binding)
                and state.entity(ref, optional=True) is None):
            if sites is None:
                sites = proven_sites()
            if ref not in sites[2]:
                raise CommandError('bound identity disappeared', kind='inverse_dependency_changed', ref=ref)

def _restore_write(ctx, write):
    ctx.read(write['ref'], write['path'])
    # An absent-to-absent edge still records authored intent, but restores
    # no entity and must not allocate an identity or dereference a value.
    if not write['path'] and write['after'] == ABSENT and write['before']['state'] == 'present':
        root = root_identity(write['ref'])
        if root is not None:
            ctx.create_entity(write['ref'], kind=root[0],
                              owner=None if root[0] == 'shot' else 'parent',
                              value=write['before']['value'], restoring=True)
        elif '/text-binding:' in write['ref']:
            ctx.assign(write['ref'], write['path'], write['before'])
        elif '/asset:' in write['ref']:
            owner, identity = write['ref'].rsplit('/asset:', 1)
            ctx.create_asset(owner, identity, write['before']['value'])
        else:
            owner, entity = write['ref'].rsplit('/', 1)
            ctx.create(owner, entity.split(':', 1)[0], write['before']['value'], restoring=True)
    else:
        ctx.assign(write['ref'], write['path'], write['before'])
    if write['ref'] == 'parent' and write['path'] in (['clips'], ['config'], ['config', 'clips']):
        ctx.state.parent_clip_path = State(ctx.state.bundle).parent_clip_path
    if write['ref'].startswith('parent/clip:'):
        ctx.compile_mirrors()


def _stage_restoration_orders(records, record):
    """Recover a nested slice's order from its authenticated compiler mirror."""
    if '_inverse_stage' not in record:
        return restoration_orders(records, only=record)
    from ._collections import order
    from ._doc_patch import inverse_changes
    from ._step_undo import _nested_mirror_partition

    source, step, boundary = record['_inverse_stage']
    if (not isinstance(records, AcceptedRecords) or not any(source is item for item in records)
            or not any(any(source is item for item in entry['records']['commands'])
                       and any(step is item for item in entry['records'].get('steps', []))
                       for entry in records.entries)):
        raise CommandError('inverse stage source is unavailable', kind='history_unavailable')
    nested = [binding for binding in source.get('bindings', []) if binding['key'] == 'inverse.stage']
    index = next((i for i, binding in enumerate(nested) if binding['boundary'] is boundary), None)
    if index is None:
        raise CommandError('inverse stage boundary is unavailable', kind='history_unavailable')
    start, end = boundary['native_write_range']
    first, last = boundary['effect_range']
    mirrors, images = _nested_mirror_partition(source, step, nested)[index]
    if (any(not equal(record.get(key), value) for key, value in source.items()
            if key not in ('writes', 'derived_writes'))
            or not equal(record['writes'], source['writes'][start:end])
            or not equal(record.get('derived_writes'), mirrors)
            or not equal(record.get('_inverse_effects'), inverse_changes(step['patch'][first:last]))):
        raise CommandError('inverse stage differs from accepted partition', kind='history_unavailable')
    result_orders = {}
    for effect in step['patch'][first:last]:
        if effect['ref'] == 'parent' and effect['path'] == ['occurrences']:
            if (effect['op'] != 'set' or effect['before'].get('state') != 'present'
                    or ('parent', 'occurrence') in result_orders):
                raise CommandError('inverse stage occurrence order is unavailable', kind='history_unavailable')
            result_orders[('parent', 'occurrence')] = tuple(
                order(State({'parent': {}, 'placements': rows}), 'parent', 'occurrence')
                for rows in (effect['before']['value'], effect['value']))
    if images is None:
        return result_orders
    result = []
    for image in images:
        if image == ABSENT:
            result.append(None)
            continue
        if image.get('state') != 'present' or not isinstance(image.get('value'), list):
            raise CommandError('inverse stage order is unavailable', kind='history_unavailable')
        result.append(order(State({'parent': {'clips': image['value'],
                                             'config': {'clips': image['value']}}}), 'parent', 'clip', literal=True))
    result_orders[('parent', 'clip')] = tuple(result)
    return result_orders


def _validate_materializations(records):
    """Require every managed edge proof, even while its source stays reachable."""
    if not isinstance(records, AcceptedRecords):
        return
    from ._commit_patch import _managed_boundaries, _materialization_sites
    from ._doc_patch import Closure

    for entry in records.entries:
        before, after = (Closure(bundle) for bundle in records.load_boundary(entry))
        expected, missing = [], set()
        for old_images, old_rows, new_images, new_rows, commands, step in _managed_boundaries(entry, before, after):
            transitions = []
            # Legacy inverse steps emit materializations alongside effect pins.
            # Require their complete stage proof set when that representation is
            # present; v2 effect-only stages have no materialization claims.
            materialized = any(binding.get('key') == 'commit.materialization'
                               for command in commands for binding in command['bindings'])
            _materialization_sites(entry, old_images, old_rows, new_images, new_rows, commands, step,
                                   transitions, missing, [], include_inverse_stages=materialized)
            expected.extend((old, new, owner + '/shot', step['id'], step['command_ordinals'][-1])
                            for old, new, owner in transitions)
        if missing:
            raise CommandError('missing authenticated fork materialization', kind='history_unavailable')
        for record in entry['records']['commands']:
            for binding in record.get('bindings', []):
                if binding.get('key') != 'commit.materialization':
                    continue
                identity = (binding.get('physical_ref'), binding.get('successor'), binding.get('owner'),
                            binding.get('step_id'), record['ordinal'])
                if (identity not in expected or binding.get('policy') != 'fixed'
                        or binding.get('dependencies') != []):
                    raise CommandError('ambiguous or unexpected fork materialization', kind='history_unavailable')
                expected.remove(identity)
        if expected:
            raise CommandError('incomplete authenticated fork materialization', kind='history_unavailable')


def apply_inverse(ctx, records, intervening_records, *, staged_records=()):
    if not records:
        raise CommandError("inverse requires trusted accepted records", kind='history_unavailable')
    if any(not isinstance(record, dict) or 'writes' not in record for record in records):
        raise CommandError("inverse history lacks authored writes", kind='history_unavailable')
    from ._step_undo import check_step_dependents, inverse_stages, selected_dependencies

    dependencies = selected_dependencies(records)
    address_resolver = getattr(records, 'address_resolver', None)
    if any('physical_ref' in binding for record in records for binding in record.get('bindings', [])):
        if not isinstance(records, AcceptedRecords) or not callable(address_resolver):
            raise CommandError('inverse needs authenticated physical binding normalization', kind='history_unavailable')
    _validate_materializations(records)
    check_step_dependents(records, state=ctx.state)
    check_intervening(records, intervening_records, state=ctx.state, include_dependents=True,
                      dependencies=dependencies, later_dependencies=selected_dependencies(intervening_records),
                      address_resolver=address_resolver)
    # Earlier batch commands are intervening writes, not part of this undo group.
    check_intervening(records, staged_records, state=ctx.state, include_dependents=True,
                      dependencies=dependencies, address_resolver=address_resolver)
    changes = coalesce(records, ctx.state)
    caches = clip_cache_stages(records)
    guarded_changes = coalesce(records, ctx.state, cache_stages=caches)
    ctx.capture_input()
    root_orders = occurrence_orders(ctx.state, records, changes)
    orders = restoration_orders(records)
    for (owner, kind), (_, expected) in orders.items():
        actual = ctx.select('collection.' + kind + 's', owner)
        if actual != expected:
            # Later accepted list moves can alter landing order while native
            # delete writes still prove the same membership. Trust only the
            # exact accepted output order, and only for that same membership.
            authenticated = None
            if isinstance(records, AcceptedRecords) and records.entries:
                try:
                    _, after = records.load_boundary(records.entries[-1])
                    authenticated = collection_order(State(after), owner, kind)
                except CommandError:
                    authenticated = None
            if (authenticated is None or actual != authenticated
                    or len(actual) != len(expected) or set(actual) != set(expected)):
                raise CommandError('inverse collection membership/order changed',
                                   kind='selection_changed', ref=owner)
    for write in guarded_changes:
        guard(_physical_state(ctx.state, write['ref'], write['path'], records),
              write['ref'], write['path'], write['after'], kind='inverse_conflict')
    from ._root_inverse import recover_rebound_sources

    recover_rebound_sources(ctx.state, records)
    scratch = State(copy.deepcopy(ctx.state.bundle), closures=ctx.state.closures)
    for record in reversed(records):
        staged = Recording(scratch.bundle, record['command'], record['ordinal'], closures=scratch.closures)
        scratch = staged.state
        staged_orders = restoration_orders(records, only=record)
        for write in reversed(caches.get(id(record), [])):
            guard(_physical_state(scratch, write['ref'], write['path'], records),
                  write['ref'], write['path'], write['after'], kind='inverse_conflict')
            _set_physical(scratch, write['ref'], write['path'], write['before'], records)
        for write in reversed(record['writes']):
            guard(_physical_state(scratch, write['ref'], write['path'], records),
                  write['ref'], write['path'], write['after'], kind='inverse_conflict')
            if not write['path'] and write['before'] == ABSENT:
                if '/asset:' in write['ref'] and _asset_dependents(scratch, write['ref']):
                    raise CommandError('created asset has surviving dependents', kind='surviving_dependents',
                                       ref=write['ref'])
                entity = member(write['ref'])
                if entity is not None and entity[1] == 'track' and _dependents(scratch, write['ref']):
                    raise CommandError('created track has surviving dependents', kind='surviving_dependents',
                                       ref=write['ref'])
            root = root_identity(write['ref'])
            if (root is not None and not write['path'] and write['after'] == ABSENT
                    and write['before']['state'] == 'present'):
                staged.create_entity(write['ref'], kind=root[0],
                                     owner=None if root[0] == 'shot' else 'parent',
                                     value=write['before']['value'], restoring=True)
            else:
                _set_physical(scratch, write['ref'], write['path'], write['before'], records)
            if write['ref'].startswith('parent/clip:'):
                # The next authored aggregate guard observes this restored leaf.
                staged.compile_mirrors()
        for (owner, kind), (original, _) in staged_orders.items():
            arrange(scratch, owner, kind, original)
        arrange_restored(scratch, record, root_orders.get(id(record)))
        staged.compile_mirrors()
        _validate_inputs(scratch, record, records)

    # Historical checks above are internal. Public claims describe H even when
    # the same address is read after another reversal during installation.
    deferred_selections = []
    for record in records:
        for read in record['reads']:
            with _recording_storage(ctx, read['ref'], read['path'], records) as path:
                ctx.read(read['ref'], path)
        for selected in record['selections']:
            # A grouped inverse can restore a track after its create selection
            # is replayed. Keep the original membership claim, but defer only
            # that selection until the staged writes have recreated the track.
            if (selected['key'] == 'track.create'
                    and any(ctx.state.entity(ref, optional=True) is None
                            for ref in selected['members'])):
                deferred_selections.append(selected)
                continue
            if selected['key'].endswith('.after'):
                ctx.select(selected['key'].removesuffix('.after'), selected['owner'])
            else:
                ctx.select(selected['key'], selected['owner'], selected['members'])
    # Keep sequential effect guards, including repeated/equal-state writes.
    # Coalescing is useful for the landing guard, never as inverse execution.
    ctx.inverse_sources = ([source for entry in records.entries for source in records.load_boundary(entry)]
                           if isinstance(records, AcceptedRecords) else [])
    ctx.inverse_stages = []
    ctx.inverse_stage_has_writes = []
    ctx.inverse_effects = []
    for publication, step_id, commands in inverse_stages(records):
        stage_before = copy.deepcopy(ctx.state.bundle)
        physical = None
        if address_resolver is not None:
            from ._commit_patch import canonical_site
            from ._doc_patch import Closure

            physical = Closure(stage_before)
            touched = {canonical_site(physical, write['ref'], write['path'])[0]
                       for record in commands for write in record['writes']
                       if write['ref'].startswith('occurrence:') and '/' in write['ref']}
        start = len(ctx.record['writes'])
        for record in reversed(commands):
            stage_orders = _stage_restoration_orders(records, record)
            if '_inverse_stage' in record:
                from ._collections import order

                for (owner, kind), (_, expected) in stage_orders.items():
                    actual = (None if owner == 'parent' and kind == 'clip'
                              and ctx.state.get('parent', ['clips']) == ABSENT and expected is None
                              else order(ctx.state, owner, kind, literal=True))
                    if actual != expected:
                        raise CommandError('inverse stage membership/order changed',
                                           kind='selection_changed', ref=owner)
            for write in reversed([*record['writes'], *caches.get(id(record), [])]):
                with _recording_storage(ctx, write['ref'], write['path'], records) as path:
                    _restore_write(ctx, {**write, 'path': path})
            for (owner, kind), (original, _) in stage_orders.items():
                if original is None:
                    guard(ctx.state, 'parent', ['clips'], ABSENT, kind='history_unavailable')
                    continue
                if ctx.state.entity(owner, optional=True) is not None:
                    arrange(ctx.state, owner, kind, original, literal=True)
            arrange_restored(ctx.state, record, root_orders.get(id(record)))
            ctx.compile_mirrors()
        from ._root_inverse import restore_root_baselines

        restore_root_baselines(ctx.state, records, publication, commands)
        if physical is not None:
            from ._doc_patch_materialize import remember

            physical.bundle = ctx.state.bundle
            live = {'occurrence:' + row['occurrence_id'] for row in ctx.state.bundle['placements']}
            for ref in touched - {'parent', 'timeline'}:
                physical.owners[ref] = [(owner, local) for owner, local in physical.owners[ref]
                                         if owner in live and local in ctx.state.bundle['shots']]
                remember(physical, ref, only_if_needed=True)
        ctx.inverse_stages.append((stage_before, copy.deepcopy(ctx.state.bundle)))
        ctx.inverse_stage_has_writes.append(any(
            record['writes'] or record.get('derived_writes') for record in commands))
        ctx.inverse_effects.append(commands[0].get('_inverse_effects') if len(commands) == 1 else None)
        if step_id is not None:
            # Keep native segment boundaries in the authenticated recorder. The
            # provider's effect projection uses these to retain transient roots.
            ctx.record['bindings'].append({'key': 'inverse.stage', 'ref': 'parent', 'policy': 'fixed',
                'dependencies': [], 'boundary': {'publication_id': publication, 'step_id': step_id,
                    'command_ordinals': [record['ordinal'] for record in commands],
                    'native_write_range': [start, len(ctx.record['writes'])]}})
    for selected in deferred_selections:
        # Deferred structural claims describe the restored head, so they must
        # not be read from the inverse's pre-write input snapshot.
        original_input = ctx.input_state
        ctx.input_state = None
        try:
            ctx.select(selected['key'], selected['owner'], selected['members'])
        finally:
            ctx.input_state = original_input
    from ._root_inverse import inverse_root_manifest

    manifest = inverse_root_manifest(ctx.state.bundle, records)
    if manifest is not None:
        from astrid.core.timeline.authoring_bundle import _collect_media

        source = ctx.state.bundle['source_mapping']['_runtime_document']['dependency_manifest']
        removed = {pin['media_id'] for pin in source['media'] if pin not in manifest['media']}
        authored = [ctx.state.bundle['parent'], ctx.state.bundle['shots'], ctx.state.bundle['placements']]
        captures = ctx.state.bundle['source_mapping'].get('pinned_documents', [])
        authored.extend(capture['document']['content'] for capture in captures
                        if capture['pin'] in manifest['documents'])
        if removed & _collect_media(authored):
            raise CommandError('introduced media has surviving dependents', kind='surviving_dependents')
        def contains(value, pin):
            if equal(value, pin):
                return True
            if isinstance(value, dict):
                return any(contains(child, pin) for child in value.values())
            return isinstance(value, list) and any(contains(child, pin) for child in value)
        if any(contains(authored, pin) for pin in source['documents'] if pin not in manifest['documents']):
            raise CommandError('introduced document has surviving dependents', kind='surviving_dependents')
        ctx.state.bundle['source_mapping']['_copy_manifest'] = manifest
