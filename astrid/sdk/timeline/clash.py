"""Explain a saved timeline edit conflict from verified Runtime history."""
from __future__ import annotations

import copy
import re

from ._command_state import CommandError
from ._commit_patch import _prefix, canonical_site, record_sites
from ._doc_patch import Closure, value_at
from .sync import _pin, _response


class _HeadMoved(Exception):
    pass


def _history(session, base):
    """Verify public history pages and return the entries after ``base``."""
    cursor, cursors, publications = None, set(), set()
    head, previous, found, entries = None, None, False, []
    while True:
        page = _response(session.client.timelines.history(
            session.project_id, session.timeline_id, cursor=cursor, limit=100))['data']
        current = _pin(page['head'])
        if head is not None and current != head:
            raise _HeadMoved('history head changed')
        head = current
        for entry in page['items']:
            pin = _pin(entry)
            if previous is None and not publications:
                previous = entry['previous']
                found = previous == base
            if entry['previous'] != previous or pin['publication_id'] in publications:
                raise ValueError('incomplete publication ancestry')
            publications.add(pin['publication_id'])
            if found:
                entries.append(copy.deepcopy(entry))
            if pin == base:
                found = True
            previous = pin
        cursor = page['next_cursor']
        if cursor is None:
            if previous != head and not (not publications and head == base):
                raise ValueError('missing publication ancestry')
            if not found and head != base:
                raise ValueError('base is outside publication ancestry')
            return head, entries
        if cursor in cursors or previous == head:
            raise ValueError('invalid history cursor')
        cursors.add(cursor)


def _addresses(ref, path, closure):
    """Include stable aliases for a write across physical revision changes."""
    result = [(ref, list(path))]
    try:
        physical, normalized = canonical_site(closure, ref, path)
    except CommandError:
        return result
    result.append((physical, normalized))
    if physical in closure.images:
        try:
            result.extend(closure.native_sites(physical, normalized))
        except CommandError:
            pass
    return result


def _touches(left, right):
    return any(a == b and _prefix(p, q) for a, p in left for b, q in right)


def _receipt_sites(entries, base):
    """Summarize each intervening receipt by its touched refs and paths."""
    receipts, sites, before = [], [], base
    for entry in entries:
        recorder = entry.get('records')
        if (entry.get('intent') != 'recorded' or not isinstance(recorder, dict)
                or recorder.get('schema_version') != 1 or not recorder.get('commands')):
            raise ValueError('intervening write intent is unavailable')
        document = entry['receipt']['result'].get('canonical_document')
        if document is None:
            raise ValueError('intervening closure is unavailable')
        from .commit import bundle_from_document

        after = Closure(bundle_from_document(document))
        touched, addresses = [], []
        for record in recorder['commands']:
            for site in record_sites(record, before):
                ref, path = site['ref'], copy.deepcopy(site['path'])
                address = _addresses(ref, path, before) + _addresses(ref, path, after)
                addresses.extend(address)
            for write in [*record.get('writes', []), *record.get('derived_writes', [])]:
                ref, path = write['ref'], copy.deepcopy(write['path'])
                public = {'ref': ref, 'path': path}
                if public not in touched:
                    touched.append(public)
        data = entry['receipt']['result']
        receipts.append({'publication_id': entry['publication_id'],
                         'label': data.get('label', recorder.get('label')),
                         'publisher': entry['publisher'], 'touched': touched,
                         'touches_conflict': False})
        sites.append(addresses)
        before = after
    return receipts, sites


def _part(record):
    parts = record.get('parts')
    if isinstance(parts, list) and parts:
        index = record.get('part', 0)
        if type(index) is int and 0 <= index < len(parts):
            return parts[index]
    return record


def _clip_row(closure, ref, path=()):
    """Find a parent clip row for either public or canonical clip addresses."""
    identity = None
    if isinstance(ref, str) and ref.startswith('parent/clip:'):
        identity = ref.partition('parent/clip:')[2]
    if identity is None and isinstance(path, (list, tuple)):
        identity = next((token.partition(':')[2] for token in path
                         if isinstance(token, str) and token.startswith('id:')), None)
    if identity is None:
        return None
    payload = closure.document.get('parent_revision', {}).get('payload', {})
    config = payload.get('config') if isinstance(payload, dict) else None
    rows = payload.get('clips') if isinstance(payload, dict) else None
    if not isinstance(rows, list) and isinstance(config, dict):
        rows = config.get('clips')
    return next((row for row in rows or []
                 if isinstance(row, dict) and row.get('id') == identity), None)


def _target(conflict, base_closure, head_closure=None):
    """Return a clip-facing conflict label, using history only for verified claims."""
    ref = str(conflict.get('ref', 'the changed part'))
    path = list(conflict.get('path') or [])
    site_path = path
    row = _clip_row(base_closure, ref, site_path)
    if row is None and head_closure is not None:
        row = _clip_row(head_closure, ref, site_path)
    clip_label = None
    if row:
        cut = row.get('cut_id') or row.get('cut')
        layer = row.get('layer_name') or row.get('layer') or row.get('track')
        identity = f'{cut}.{layer}' if cut and layer else row.get('id')
        clip_label = ref = f'clip {identity}'
    else:
        ref = ref.rsplit('/', 1)[-1]
        if ref.startswith('clip:'):
            ref = 'clip ' + ref.partition(':')[2]
        elif ':' in ref:
            ref = ref.split(':', 1)[0] + ' ' + ref.split(':', 1)[1]
    path = [part for part in path if not str(part).startswith('id:')]
    field = path[-1] if path else None
    if field == 'at':
        field = 'start time'
    if field:
        ref += f' {field}'

    # A removed clip is stated only when the verified head no longer contains it.
    if (row and head_closure is not None
            and _clip_row(head_closure, conflict.get('ref'), site_path) is None):
        return f'{clip_label} was removed'

    ours = conflict.get('ours')
    theirs = conflict.get('theirs')
    current = theirs if isinstance(theirs, dict) and theirs.get('state') == 'present' else None
    if current is None and head_closure is not None:
        try:
            physical, normalized = canonical_site(
                head_closure, conflict['ref'], conflict.get('path', []))
            image = head_closure.images[physical]
            current = value_at(image['payload'], physical, normalized)
        except (KeyError, CommandError):
            current = None
    if (isinstance(ours, dict) and ours.get('state') == 'present'
            and isinstance(current, dict) and current.get('state') == 'present'
            and ours.get('value') == current.get('value')):
        return f'{ref} already has {ours.get("value")!r}'
    return ref


def _closures(session, base, head, entries):
    """Build friendly-name lookups from the verified base and latest history image."""
    from .commit import bundle_from_document

    base_closure = Closure(session._capture_bundle({'base': base, 'pending': []}))
    head_closure = None
    document = entries[-1]['receipt']['result'].get('canonical_document') if entries else None
    if document is not None:
        head_closure = Closure(bundle_from_document(document))
    return base_closure, head_closure


def friendly_target(session, record, conflict):
    """Resolve a conflict label from the saved base and verified public history."""
    part = _part(record)
    base = _pin(part.get('base') or record.get('base'))
    head, entries = _history(session, base)
    base_closure, head_closure = _closures(session, base, head, entries)
    return _target(conflict, base_closure, head_closure)


def publisher_name(publisher):
    """Avoid showing generated local actor ids in user-facing summaries."""
    if isinstance(publisher, str) and re.fullmatch(r'astrid-[0-9a-fA-F]+', publisher):
        return 'another writer'
    return publisher


def _summary(record, receipts, targets=()):
    label = record.get('label') or 'Timeline edit'
    pending_id = record.get('id') or 'unknown'
    match = next((receipt for receipt in receipts if receipt.get('touches_conflict')), None)
    target = targets[0] if targets else None
    where = f' ({target})' if target else ''
    if match:
        publisher = publisher_name(match.get('publisher'))
        who = f' ({publisher})' if publisher else ''
        return (f'"{label}" was not committed: "{match.get("label") or "Another edit"}"{who} '
                f'changed a conflicting part{where} after your edit was made. Your edit is saved as '
                f'{pending_id}; nothing is lost.')
    return (f'"{label}" was not committed: the timeline changed{where} after your edit was made. '
            f'Your edit is saved as {pending_id}; nothing is lost.')


def explain(session, record):
    """Build an ``astrid.clash/2`` summary for a durably saved pending record."""
    record = copy.deepcopy(record)
    part = _part(record)
    base = _pin(part.get('base') or record.get('base'))
    failure = record.get('failure') or {}
    details = failure.get('details', failure)
    conflicts = copy.deepcopy(details.get('conflicts', []))
    packet = {'schema': 'astrid.clash/2', 'pending_id': record.get('id'),
              'label': record.get('label', 'Timeline edit'), 'key': record.get('key'),
              'origin': record.get('origin', 'list'), 'base': base,
              'head': copy.deepcopy(details.get('current') or record.get('head')),
              'conflicts': conflicts, 'receipts': None}
    try:
        head, entries = _history(session, base)
        base_bundle = session._capture_bundle({'base': base, 'pending': []})
        receipts, sites = _receipt_sites(entries, Closure(base_bundle))
        for conflict in conflicts:
            try:
                target = _addresses(conflict['ref'], conflict.get('path', []), Closure(base_bundle))
            except (KeyError, CommandError):
                continue
            for receipt, touched in zip(receipts, sites):
                if _touches(touched, target) and not receipt.get('touches_conflict'):
                    receipt['touches_conflict'] = True
        packet['head'] = head
        packet['receipts'] = receipts
    except Exception:  # noqa: BLE001 - enrichment must not disturb the saved edit
        pass
    try:
        base_closure, head_closure = _closures(session, base, packet['head'], entries)
        targets = [_target(conflict, base_closure, head_closure) for conflict in conflicts]
    except Exception:  # noqa: BLE001 - summaries remain available without friendly enrichment
        targets = []
    packet['summary'] = _summary(record, packet['receipts'] or [], targets)
    pending_id = record.get('id')
    packet['next'] = (f'tl.retry({pending_id!r}, your_function)' if record.get('origin') == 'function'
                      else f'tl.retry({pending_id!r})') if pending_id else 'tl.pending_list()'
    return packet
