"""Scoped authored collection identities and verified historical order."""
from __future__ import annotations

from ._command_state import ABSENT, CommandError, State, equal, logical_address, present

KINDS = {'collection.text-bindings': 'text-binding',
         'collection.text-bindings.append': 'text-binding', 'collection.tracks': 'track', 'collection.clips': 'clip',
         'collection.tracks.after': 'track', 'collection.clips.after': 'clip',
         'collection.occurrences': 'occurrence', 'collection.shots': 'shot',
         'track.create': 'track'}


def member(ref):
    if '/clip:' in ref or '/track:' in ref or '/text-binding:' in ref:
        owner, entity = ref.rsplit('/', 1)
        kind, identity = entity.split(':', 1)
        return owner, kind, identity
    return None


def container(owner, kind):
    if kind == 'text-binding':
        return owner, ('payload', 'text_bindings')
    if kind in ('occurrence', 'shot'):
        return 'parent', (kind + 's',)
    if kind == 'asset':
        return owner, ('config' if owner == 'parent' else 'source', 'registry', 'assets')
    return owner, (
        ('config' if owner == 'parent' else 'source', kind + 's'))


def _identity(value, *, literal=False):
    return (isinstance(value, str) and bool(value)
            and (literal or '/' not in value and '::' not in value))


def shape(state, owner, kind):
    def tagged(obj, key):
        if not isinstance(obj, dict) or key not in obj:
            return ABSENT.copy()
        value = obj[key]
        return present([] if isinstance(value, list) else {} if isinstance(value, dict) else value)

    if kind in ('occurrence', 'shot'):
        return {'container': 'array' if kind == 'occurrence' else 'object'}
    if kind == 'text-binding':
        shot = state.occurrence(owner)['shot_id']
        return {'members': tagged(state.bundle['shots'][shot]['payload'], 'text_bindings')}
    config = state.config(owner)
    if kind == 'asset':
        # Shape guards retain storage presence without guarding unrelated aliases.
        containers = [config] if owner != 'parent' else [state.bundle['parent'], config]
        return {str(i): {'registry': tagged(obj, 'registry'),
                        'assets': tagged(obj.get('registry') if isinstance(obj, dict) else {}, 'assets')}
                for i, obj in enumerate(containers)}
    result = {'members': tagged(config if isinstance(config, dict) else {}, kind + 's')}
    if owner == 'parent':
        parent = state.bundle['parent']
        result['config'] = tagged(parent, 'config')
        if kind == 'clip':
            result['payload_members'] = tagged(parent, 'clips')
    return result


def rows(state, owner, kind, *, check_mirrors=True):
    if kind in ('occurrence', 'shot'):
        if owner != 'parent':
            raise CommandError('aggregate collection requires parent ownership', ref=owner)
        result = state.bundle['placements' if kind == 'occurrence' else 'shots']
        if not isinstance(result, list if kind == 'occurrence' else dict):
            raise CommandError('invalid aggregate collection', kind='identity_conflict', ref=owner)
        return result
    if kind == 'text-binding':
        from .native_text_binding import binding_rows
        return binding_rows(state, owner)
    config = state.config(owner)
    if not isinstance(config, dict):
        raise CommandError('collection owner must be an object', kind='identity_conflict', ref=owner)
    if kind == 'clip' and owner == 'parent':
        parent = state.bundle['parent']
        a, b = parent.get('clips'), config.get('clips')
        if check_mirrors and isinstance(a, list) and isinstance(b, list) and not equal(a, b):
            raise CommandError('divergent parent clip mirrors', kind='identity_conflict')
        result = state.collection(owner, kind)
        # Absence and null are not an empty authored list.
        if not isinstance(a, list) and not isinstance(b, list):
            raise CommandError('structural edit requires an authored entity collection', ref=owner)
    else:
        result = config.get(kind + 's')
    if not isinstance(result, list):
        raise CommandError('structural edit requires an authored entity collection', ref=owner)
    return result


def order(state, owner, kind, *, check_mirrors=True, literal=False):
    collection = rows(state, owner, kind, check_mirrors=check_mirrors)
    if kind == 'shot':
        identities = list(collection)
        if any(not isinstance(row, dict) or row.get('shot_id') != identity
               for identity, row in collection.items()):
            raise CommandError('ambiguous shot ownership', kind='identity_conflict', ref=owner)
    elif kind == 'text-binding':
        from .native_text_binding import binding_key
        identities = [binding_key(row) for row in collection]
    else:
        key = 'occurrence_id' if kind == 'occurrence' else 'id'
        identities = [row.get(key) if isinstance(row, dict) else None for row in collection]
    if any(not _identity(identity, literal=literal) for identity in identities) or len(set(identities)) != len(identities):
        raise CommandError('missing or ambiguous collection identity', kind='identity_conflict', ref=owner)
    prefix = kind + ':' if kind in ('occurrence', 'shot') else owner + '/' + kind + ':'
    return [prefix + identity for identity in identities]


def arrange(state, owner, kind, expected, *, literal=False):
    current = order(state, owner, kind, check_mirrors=False, literal=literal)
    if len(expected) != len(set(expected)) or set(current) != set(expected):
        raise CommandError('restoration membership differs from historical order',
                           kind='selection_changed', ref=owner)
    collection = rows(state, owner, kind, check_mirrors=False)
    indexed = dict(zip(current, collection.values() if isinstance(collection, dict) else collection))
    if isinstance(collection, dict):
        collection.clear()
        collection.update((ref.removeprefix('shot:'), indexed[ref]) for ref in expected)
    else:
        collection[:] = [indexed[ref] for ref in expected]


def selection_overlap(selected, write):
    if selected['key'] in ('placement.owners', 'text-binding.owners'):
        if (write['ref'].startswith('occurrence:') and '/' not in write['ref']
                and write['path'] in ([], ['shot_id'])):
            if any(key not in write for key in ('before', 'after')):
                raise CommandError('owner membership requires complete write history',
                                   kind='history_unavailable')
            for state in (write['before'], write['after']):
                value = state.get('value')
                shot = value.get('shot_id') if not write['path'] and isinstance(value, dict) else value
                if isinstance(shot, str) and 'shot:' + shot in selected['boundaries']:
                    return True
        return write['ref'] == 'parent' and write['path'] == ['occurrences']
    kind = KINDS.get(selected['key'])
    if kind is None:
        return False
    entity = member(write['ref'])
    if kind == 'text-binding' and entity is not None and entity[:2] == (selected['owner'], kind):
        if any(key not in write for key in ('before', 'after')):
            raise CommandError('binding membership requires complete write history', kind='history_unavailable')
        if all(write[key]['state'] == 'present' for key in ('before', 'after')):
            return False
    if kind in ('occurrence', 'shot') and write['ref'].startswith(kind + ':'):
        return not write['path'] or write['path'] == [kind + '_id']
    ref, path = logical_address(write['ref'], write['path'])
    owner, prefix = container(selected['owner'], kind)
    if ref != owner:
        return False
    if prefix[:len(path)] == path:
        return True
    rest = path[len(prefix):] if path[:len(prefix)] == prefix else ()
    return bool(rest and isinstance(rest[0], tuple) and
                (len(rest) == 1 or rest[1:] == ('id',)))


class AcceptedRecords(list):
    """Accepted trace plus lazy verified boundaries; never rewritten recorder data."""

    def __init__(self, entries, records, load_boundary):
        super().__init__(records)
        self.entries, self.load_boundary = entries, load_boundary

    def boundary(self, record):
        index = next(i for i, item in enumerate(self) if item is record)
        offset = 0
        for entry in self.entries:
            commands = entry['records']['commands']
            if index < offset + len(commands):
                if (len(commands) != 1 or record['command']['op'] != 'timeline.inverse'
                        or entry['records'].get('undo_of') != record['command']['args']['publications']):
                    break
                before, after = self.load_boundary(entry)
                return State(before), State(after)
            offset += len(commands)
        raise CommandError('inverse collection boundary is unavailable', kind='history_unavailable')


def _ordinary_create(record, writes, owner, kind):
    """Trusted scoped creates use append or a recorded stable boundary."""
    if kind == 'text-binding':
        return (record['command']['op'] == 'text-binding.set'
                and all(w['before'] == ABSENT and w['after'] != ABSENT for w in writes))
    if record['command']['op'] in ('timeline.inverse', 'timeline.restore'):
        return False
    created = {item['ref'] for item in record['created']
               if item['owner'] == owner and item['kind'] == kind}
    return created == {w['ref'] for w in writes} and all(
        w['before'] == ABSENT and w['after']['state'] == 'present'
        and isinstance(w['after']['value'], dict)
        and w['after']['value'].get('id') == member(w['ref'])[2] for w in writes)


def insertion_boundary(ordered, members):
    if len(members) != 1 or members[0] not in ordered:
        raise CommandError('create-after requires one existing stable member', kind='selection_changed')
    index = ordered.index(members[0])
    return ordered[index:index + 2]


def _boundary_order(state, owner, kind, record):
    if owner != 'parent' and state.entity(owner, optional=True) is None:
        if any(write['ref'] == owner and not write['path']
               and ABSENT in (write['before'], write['after']) for write in record['writes']):
            return []
        raise CommandError('historical collection owner is unavailable', kind='history_unavailable', ref=owner)
    return order(state, owner, kind)


def _track_order_refs(value):
    if (not isinstance(value, dict) or value.get('state') != 'present'
            or not isinstance(value.get('value'), list)):
        raise CommandError('track collection order image is unavailable', kind='history_unavailable')
    refs = []
    for row in value['value']:
        if not isinstance(row, dict) or not _identity(row.get('id')):
            raise CommandError('track collection order image is malformed', kind='history_unavailable')
        refs.append('parent/track:' + row['id'])
    if len(refs) != len(set(refs)):
        raise CommandError('duplicate track collection identity', kind='history_unavailable')
    return refs


def restoration_orders(records, *, only=None):
    """Project only structural membership; inverse insert positions need a boundary."""
    touched = {member(w['ref'])[:2] for record in records for w in record['writes']
               if not w['path'] and member(w['ref']) is not None
               and (member(w['ref'])[1] != 'text-binding' or ABSENT in (w['before'], w['after']))}
    result = {}
    for record in records:
        if only is not None and record is not only:
            continue
        for owner, kind in sorted(touched):
            writes = [w for w in record['writes'] if not w['path'] and
                      member(w['ref']) is not None and member(w['ref'])[:2] == (owner, kind)
                      and (kind != 'text-binding' or ABSENT in (w['before'], w['after']))]
            if not writes:
                continue
            selected = [s for s in record['selections'] if s['owner'] == owner
                        and KINDS.get(s['key']) == kind]
            boundary = None
            legacy_inverse = (record['command']['op'] == 'timeline.inverse'
                              and not any(s['key'].startswith('collection.') for s in selected))
            if legacy_inverse or not selected:
                if not isinstance(records, AcceptedRecords):
                    raise CommandError('original collection order is unavailable', kind='history_unavailable')
                boundary = records.boundary(record)
                original = _boundary_order(boundary[0], owner, kind, record)
            else:
                original = list(selected[0]['order'])
                if any((s['boundaries'] != insertion_boundary(s['order'], s['members'])
                        if s['key'].endswith('.after') else
                        s['members'] != s['order'] or s['boundaries'] != s['order'])
                       or s['query_version'] != 1
                       or s['policy'] != ('include-concurrent' if s['key'] == 'collection.text-bindings.append'
                                           else 'guard-membership') for s in selected):
                    raise CommandError('inconsistent structural order proof', kind='history_unavailable')
            if len(original) != len(set(original)) or any(
                    member(ref) is None or member(ref)[:2] != (owner, kind) for ref in original):
                raise CommandError('foreign or duplicate historical members', kind='history_unavailable')
            prior = result.get((owner, kind))
            if prior is not None and prior[1] != original:
                raise CommandError('dependent structural stages require staged reversal', kind='history_unavailable')
            final = list(original)
            inserts = False
            after = [s for s in selected if s['key'].endswith('.after')]
            positioned = bool(after) and record['command']['op'] not in ('timeline.inverse', 'timeline.restore')
            if positioned and (len(after) != 1 or len(writes) != 1
                               or not _ordinary_create(record, writes, owner, kind)):
                raise CommandError('ambiguous create-after proof', kind='history_unavailable')
            stages = [list(final)]
            for write in writes:
                ref = write['ref']
                if write['before'] == ABSENT:
                    if ref in final:
                        raise CommandError('created identity is occupied', kind='identity_conflict', ref=ref)
                    if write['after'] != ABSENT:
                        if positioned:
                            final.insert(final.index(after[0]['members'][0]) + 1, ref)
                        else:
                            final.append(ref)
                        inserts = True
                elif ref not in final:
                    raise CommandError('historical member is missing', kind='history_unavailable', ref=ref)
                elif write['after'] == ABSENT:
                    final.remove(ref)
                stages.append(list(final))
            if not legacy_inverse and any(s['order'] != original for s in selected):
                if not _ordinary_create(record, writes, owner, kind) or any(
                        s['order'] not in stages for s in selected):
                    raise CommandError('unrecoverable creation selection stage', kind='history_unavailable')
            if legacy_inverse and any(s['members'] != s['order'] or s['boundaries'] != s['order']
                                      or s['order'] not in stages for s in selected):
                raise CommandError('unrecoverable legacy selection stage', kind='history_unavailable')
            # Inverse insertion needs accepted order; native creates already
            # prove append or create-after through their recorded selections.
            if inserts and not _ordinary_create(record, writes, owner, kind):
                if not isinstance(records, AcceptedRecords):
                    raise CommandError('inverse insertion order needs an accepted boundary', kind='history_unavailable')
                boundary = boundary or records.boundary(record)
            if boundary is not None:
                if _boundary_order(boundary[0], owner, kind, record) != original:
                    raise CommandError('accepted input order differs from selection', kind='history_unavailable')
                proven = _boundary_order(boundary[1], owner, kind, record)
                if set(proven) != set(final):
                    raise CommandError('accepted boundary membership differs from writes', kind='history_unavailable')
                final = proven
            result[owner, kind] = (prior[0] if prior else original, final)
        # A document patch can reorder existing track rows between native
        # track creates. Project only authenticated parent track-list writes,
        # and only when they preserve the exact currently projected members.
        # Membership still comes from native structural writes above.
        if only is None:
            for write in record['writes']:
                if (record['command']['op'] != 'doc.patch' or write['ref'] != 'parent'
                        or write['path'] != ['config', 'tracks']):
                    continue
                prior = result.get(('parent', 'track'))
                if prior is None:
                    continue
                before = _track_order_refs(write['before'])
                after = _track_order_refs(write['after'])
                expected = prior[1]
                if before != expected or len(after) != len(expected) or set(after) != set(expected):
                    raise CommandError('track list patch is not a proven order permutation',
                                       kind='history_unavailable')
                result['parent', 'track'] = (prior[0], after)
    return result
