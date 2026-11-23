"""Capture a batch of CLI media additions in one timeline commit."""
from __future__ import annotations

import copy
import json
from pathlib import Path


def commit_operations(session, parsed, options):
    """Evaluate a strictly specified file as one SDK commit."""
    import math

    from astrid.sdk.timeline import store
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.commit_session import Timeline
    from astrid.sdk.timeline_media_replacement import _plain

    accepted = {'project', 'ref', 'operations', 'dry_run', 'idempotency_key', 'json', 'no_sweep'}
    unsupported = [name for name, default in parsed.native_trim_defaults.items()
                   if name != 'help' and name not in accepted and getattr(parsed, name) != default]
    if unsupported:
        raise TimelineEditError('operations does not support ' + ', '.join('--' + name.replace('_', '-')
                                for name in unsupported))
    draft = store.draft_path(session.project_id, session.timeline_id,
                             store.current_draft(session.project_id, session.timeline_id))
    if draft.is_file():
        raise TimelineEditError('a detached working copy is pending; commit it first, or run '
                                f'astrid timelines discard {parsed.ref} --project {parsed.project}')

    operations = None

    def read_operations():
        nonlocal operations
        if operations is not None:
            return operations

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f'duplicate key: {key}')
                result[key] = value
            return result

        try:
            document = json.loads(Path(parsed.operations).read_text(encoding='utf-8'),
                                  object_pairs_hook=unique_object)
        except (OSError, ValueError) as exc:
            raise TimelineEditError(f'cannot read operations JSON: {exc}') from exc
        if not isinstance(document, dict) or set(document) != {'operations'} or not isinstance(document['operations'], list) or not document['operations']:
            raise TimelineEditError('operations file must contain a nonempty operations array')
        entries = document['operations']
        ids = set()
        for index, op in enumerate(entries, 1):
            prefix = f'operation {index}'
            if not isinstance(op, dict) or set(op) - {'op', 'source', 'at', 'hold', 'track', 'id'} or op.get('op') != 'add':
                raise TimelineEditError(f'{prefix}: expected an add with only source, at, hold, track and id')
            if not isinstance(op.get('source'), str) or not op['source'].strip():
                raise TimelineEditError(f'{prefix}: source must be a nonempty string')
            for field, positive in (('at', False), ('hold', True)):
                value = op.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or (value <= 0 if positive else value < 0):
                    raise TimelineEditError(f'{prefix}: {field} must be a finite {"positive" if positive else "nonnegative"} number')
            for field in ('track', 'id'):
                if field in op and (not isinstance(op[field], str) or not op[field].strip()):
                    raise TimelineEditError(f'{prefix}: {field} must be a nonempty string')
            if 'id' in op:
                if op['id'] in ids:
                    raise TimelineEditError(f'{prefix}: duplicate id {op["id"]!r}')
                ids.add(op['id'])

        operations = entries
        return operations

    timeline = Timeline.open(session.project_id, session.timeline_id, client=session.client)
    transport = session.client._remote._transport
    original_import = transport.import_project_media
    imports = []

    def observe_import(*args, **kwargs):
        result = original_import(*args, **kwargs)
        record = _plain(result)
        imports.append({'object_id': record.get('object_id') or kwargs.get('expected_digest'),
                        'generation_id': record.get('generation_id'), 'variant_id': record.get('variant_id'),
                        'catalog_write': True, 'retained_on_undo': True})
        return result

    def apply(working):
        for op in read_operations():
            working.add(op['source'], at=op['at'], hold=op['hold'],
                        track=op.get('track'), id=op.get('id'))

    # The CLI owns this client for the duration of the synchronous command;
    # concurrent use of the same transport could mix import attribution.
    transport.import_project_media = observe_import
    try:
        return timeline.commit(apply, label='Add media from operations file',
                               key=options['idempotency_key'], dry_run=options['dry_run'])
    except Exception as exc:
        if imports:
            exc._cli_batch_imports = copy.deepcopy(imports)
        raise
    finally:
        transport.import_project_media = original_import
