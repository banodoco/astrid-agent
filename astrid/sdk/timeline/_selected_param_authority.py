"""Selected PARAM provenance from the existing accepted publication chain."""
from __future__ import annotations

import copy
from functools import lru_cache

from ._command_state import CommandError, State, equal


def _bound(record, expected):
    bindings = [b for b in record['bindings'] if b['key'] == expected['key']]
    if len(bindings) != 1:
        return False
    return equal(bindings[0], expected)


def _association(record, select, source=None, boundary=None):
    from .commit import _document_pin

    def bound(expected):
        if source is not None:
            from ._commit_patch import canonical_site

            physical, path = canonical_site(source, expected['ref'], [])
            expected = {**expected, **source.binding(expected['ref'], physical, **boundary),
                        'path': path}
        return _bound(record, expected)

    command = record['command']
    if command['select'] != select:
        return None
    schemas = [d for d in record['dependencies'] if d['key'] == 'params.schema']
    snapshots = [d for d in record['dependencies'] if d['key'] == 'params.snapshot']
    if len(schemas) != 1 or len(snapshots) != 1 or not bound({
            'key': 'params.element', 'ref': select, 'policy': 'fixed',
            'dependencies': ['params.schema', 'params.snapshot']}):
        return None
    owner = select.rsplit('/', 1)[0]
    required = [{'key': 'scalar.target', 'ref': select, 'policy': 'fixed',
                 'dependencies': ['scalar.images']}]
    if owner != 'parent':
        required.append({'key': 'scalar.owner', 'ref': owner, 'policy': 'fixed', 'dependencies': []})
    if any(not bound(expected) for expected in required):
        return None
    schema, snapshot = schemas[0], snapshots[0]
    pin = snapshot['pin']
    if (schema['kind'] != 'source' or schema['policy'] != 'exact'
            or snapshot['kind'] != 'entity' or snapshot['policy'] != 'exact'
            or set(pin) != {'source', 'element', 'object'}
            or schema['pin'] != pin['source']
            or command['args'].get('schema_source') != pin['source']
            or set(pin['element']) != {'packId', 'id', 'kind', 'revision'}):
        return None
    _document_pin(pin['source'])
    if command['op'] == 'clip.set-params':
        reads = {r['path'][0]: r['state'].get('value') for r in record['reads']
                 if r['ref'] == select and r['path'] in (['elementRef'], ['element_ref'])}
        if (reads.get('elementRef') or reads.get('element_ref')) != pin['element']:
            return None
    return pin


def resolve_selected_param_source(port, *, context, through, select, staged=()):
    """Return the proven P/E/O association, or permanently refuse this edit.

    Publication identity orders effects even when complete revision bytes are
    equal. No mutable admission binding or manifest order selects an old pin.
    Unsupported structural producers deliberately leave authority unavailable.
    """
    from .commit import _history, _load, _records

    def rejected(failure):
        return CommandError(failure['message'], kind='history_unavailable', ref=select)

    entries = _history(port, through, None, rejected, through)
    indices = {e['publication_id']: i for i, e in enumerate(entries)}
    owner = select.rsplit('/', 1)[0]

    @lru_cache(None)
    def state(index):
        return State(_load(port, context, entries[index]['revision_id']))

    def relevant(record, index):
        for write in record['writes']:
            ref, path = write['ref'], write['path']
            if ref == select and (not path or path[0] in (
                    'params', 'elementRef', 'element_ref', 'id', 'clipType', 'clip_type',
                    'asset', 'asset_id', 'media_id', 'object_id', 'file', 'src', 'source')):
                return True
            if ref == 'timeline' or ref == owner and (not path or path[0] in (
                    'clips', 'occurrence_id', 'shot_id', 'shot_revision_id', 'internal_timeline_revision_id')):
                return True
            if owner == 'parent' and ref == 'parent' and path[:2] == ['config', 'clips']:
                return True
            if owner != 'parent' and ref in (owner + '/source', owner + '/shot'):
                return True
            if owner != 'parent' and ref.startswith('shot:'):
                current = state(min(index, len(entries) - 1))
                shot = (current.entity(owner + '/shot')
                        if current.entity(owner, optional=True) is not None else None)
                if shot and ref == 'shot:' + shot['shot_id']:
                    return True
        return record['command']['select'] == select and record['command']['op'] in (
            'clip.set-element', 'clip.set-params')

    def physical_context(records, record, index, envelope):
        from ._commit_patch import normalize_steps
        from ._doc_patch import Closure

        staged = index == len(entries)
        request = context['request'] if staged else entries[index]['receipt']['result']
        v2 = (any(key in request for key in ('label', 'steps', 'merge', 'reads'))
              or any(call['op'] == 'doc.patch' for call in request['commands'])
              if staged else any('physical_ref' in binding and binding['key'] in (
                  'params.element', 'scalar.target', 'scalar.owner', 'commit.effect')
                  for item in records for binding in item['bindings']))
        if not v2:
            return None, None
        head = through if staged else entries[index]['previous']
        steps = (normalize_steps(request, request['commands'])[1] if staged else envelope['steps'])
        step, = [step for step in steps if record['ordinal'] in step['command_ordinals']]
        boundary = {key: request[key] for key in ('base_revision_id', 'base_publication_id')}
        boundary.update(evaluated_head=copy.deepcopy(head), step_id=step['id'],
                        command_ordinal=record['ordinal'], phase='input')
        current = State(copy.deepcopy(_load(port, context, head['revision_id'])))
        mapping = current.bundle['source_mapping']
        mapping['_copy_manifest'] = copy.deepcopy(mapping['_runtime_document']['dependency_manifest'])
        # Project recorder effects over the sealed input, without executing
        # handlers or trusting an image/owner claimed by a binding. Each command
        # may legitimately have a different physical revision from the head.
        for ordinal, prior in enumerate(records[:record['ordinal']]):
            if prior['ordinal'] != ordinal:
                raise ValueError('unordered staged records')
            for write in [*prior['writes'], *prior['derived_writes']]:
                if not current.matches(write['ref'], write['path'], write['before']):
                    raise ValueError('staged input differs from recorded before value')
                current.set(write['ref'], write['path'], write['after'])
            for dependency in prior['dependencies']:
                if dependency['kind'] in ('source', 'transcript'):
                    documents = mapping['_copy_manifest']['documents']
                    if dependency['pin'] not in documents:
                        documents.append(copy.deepcopy(dependency['pin']))
        return Closure(current.bundle), boundary

    def effect(value, record, index, envelope=None, records=()):
        if not relevant(record, index):
            return value
        command = record['command']
        if command['op'] in ('clip.set-element', 'clip.set-params'):
            source, boundary = physical_context(records, record, index, envelope)
            association = _association(record, select, source, boundary)
            if association is None:
                return None
            element = association['element']
            if command['op'] == 'clip.set-element':
                return association if command['args']['elementRef'] == element else None
            # Accepted PARAM records are retained selection proof, including
            # records predating enforcement. New PARAM is checked by the caller
            # before it can produce such a record.
            return association
        if command['op'] == 'timeline.inverse':
            publications = command['args']['publications']
            if envelope is not None and envelope['undo_of'] != publications:
                return None
            for publication in reversed(publications):
                target = indices.get(publication)
                if target is None or target >= index:
                    return None
                entry = entries[target]
                if port.history_entry(publication) != entry or entry['previous'] is None:
                    return None
                records = _records([entry], rejected, through, through)
                if any(relevant(r, target) for r in records):
                    value = at(target - 1)
            return value
        if command['op'] == 'timeline.restore':
            pin = command['args']
            if envelope is None or envelope['restored_from'] != pin:
                return None
            candidates = [i for i in range(index) if entries[i]['revision_id'] == pin['revision_id']]
            values = [at(i) for i in candidates if state(i).bundle['source_mapping'][
                '_runtime_document']['parent_revision']['content_digest'] == pin['content_digest']]
            return values[0] if values and all(v == values[0] for v in values) else None
        return None

    @lru_cache(None)
    def at(end):
        value = None
        for index in range(end + 1):
            entry = entries[index]
            if entry.get('intent') != 'recorded':
                value = None
                continue
            records = _records([entry], rejected, through, through)
            if index and owner != 'parent':
                before, after = state(index - 1), state(index)
                # State.matches admits only the compiler's exact occurrence
                # materialization, never an ID prefix or equal sibling JSON.
                if (before.entity(owner, optional=True) is None
                        or after.entity(owner, optional=True) is None
                        or not after.matches(owner + '/shot', ['shot_id'],
                                             before.get(owner + '/shot', ['shot_id']))):
                    value = None
            for record in records:
                value = effect(value, record, index, entry['records'], records)
            row = state(index).entity(select, optional=True)
            element = (row.get('elementRef') or row.get('element_ref')) if row else None
            if value is not None and element != value['element']:
                value = None
        return value

    try:
        value = at(len(entries) - 1)
        for record in staged:
            value = effect(value, record, len(entries), records=staged)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise CommandError('selected PARAM history is malformed', kind='history_unavailable', ref=select) from exc
    if value is None:
        raise CommandError('selected PARAM authority is unavailable', kind='history_unavailable', ref=select)
    return value
