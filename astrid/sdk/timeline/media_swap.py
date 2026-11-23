"""Scoped occurrence media changes using guarded patches and Runtime admission."""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping

from ._command_state import State
from ..timeline_media_replacement import _verified


def _object_id(entry):
    values = [entry[key] for key in ('media_id', 'object_id', 'digest', 'content_digest', 'content_sha256')
              if key in entry]
    normalized = {value if value.startswith('sha256:') else 'sha256:' + value
                  for value in values if isinstance(value, str)}
    if (len(normalized) != 1 or len(values) != sum(isinstance(value, str) for value in values)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', next(iter(normalized), ''))):
        raise ValueError('replacement media requires one consistent immutable object identity')
    return next(iter(normalized))


def _raw_inputs(transport, project, inputs, verified):
    """Supply preview metadata; Runtime re-reads every input at admission.

    An old authored entry needs no catalog association: it is never rebound.
    New raw objects must appear in the requested project's public object list;
    new catalog associations additionally require their exact verified selection.
    """
    from astrid.sdk.pagination import paged_rows

    rows = list(verified)
    objects = None
    for item in inputs:
        pin = item['pin']
        if 'selection' in item:
            _, selected = _verified(transport, project, item['selection'])
            if selected not in rows:
                rows.append(selected)
            raw = {key: selected[key] for key in ('media_type', 'size')}
            raw['object_id'] = pin['media_id']
        else:
            prior = next((row for row in rows if row.get('pin') == pin
                          and row.get('object_id') == pin['media_id']), None)
            if prior is not None:
                continue
            if objects is None:
                objects = list(paged_rows(transport.list_project_objects, project))
            raw = next((row for row in objects if row.get('object_id', row.get('digest')) == pin['media_id']), None)
            if raw is None:
                raise ValueError('replacement media is not an imported object in this project')
            if _object_id(raw) != pin['media_id']:
                raise ValueError('Runtime object metadata differs from the replacement pin')
            raw = {key: raw[key] for key in ('media_type', 'size')}
            raw['object_id'] = pin['media_id']
        if not isinstance(raw['media_type'], str) or type(raw['size']) is not int or raw['size'] <= 0:
            raise ValueError('replacement media lacks Runtime object metadata')
        raw['pin'] = copy.deepcopy(pin)
        # A local-file registration may already have cached the verified
        # selection together with its pin. Reuse it only if its raw metadata
        # agrees; conflicting rows must still be rejected by admission.
        matching = [row for row in rows if row.get('pin') == pin]
        if not any(all(row.get(key) == raw[key] for key in ('object_id', 'media_type', 'size'))
                   for row in matching):
            rows.append(raw)
    return rows


def capture_document_swaps(base, candidate, transport, verified_media=()):
    """Capture lossless document edits with explicit Runtime media admission.

    The emitter validates the original provenance and emits only authored
    differences. Keep that provenance in the returned candidate: execution's
    private compiler cache belongs to the recorder, never to a detached input.
    Callers give media.admit its own semantic step in the same transaction.
    """
    from .commands import execute
    from .emitter import emit_document_commands
    from .publish import _media_admission_inputs

    commands = emit_document_commands(base, candidate)
    if not commands:
        return [], copy.deepcopy(base), list(verified_media)
    original, target = State(base), State(candidate)
    working = copy.deepcopy(base)
    working_state = State(working)
    replacements = []
    candidate_placements = {row['occurrence_id']: row for row in candidate.get('placements', [])}
    for placement in base.get('placements', []):
        owner = 'occurrence:' + placement['occurrence_id']
        new_placement = candidate_placements.get(placement['occurrence_id'])
        if new_placement is None or new_placement.get('shot_id') != placement.get('shot_id'):
            continue
        before = {row['id']: row for row in original.collection(owner, 'clip')}
        after = {row['id']: row for row in target.collection(owner, 'clip')}
        for clip_id in sorted(before.keys() & after.keys()):
            old, new = before[clip_id], after[clip_id]
            if old.get('asset') == new.get('asset') or not isinstance(new.get('asset'), str):
                continue
            if not isinstance(old.get('asset'), str):
                continue
            old_entry = original.registry(owner)['assets'].get(old['asset'])
            entry = target.registry(owner)['assets'].get(new['asset'])
            if not isinstance(old_entry, Mapping) or not isinstance(entry, Mapping):
                raise ValueError('media swap requires authored registry entries')
            _object_id(entry)
            old_kind, new_kind = (row.get('type', '').split('/')[0] for row in (old_entry, entry))
            if old_kind not in ('image', 'video', 'audio') or old_kind != new_kind:
                raise ValueError('media swap preserves media kind')
            retained = original.registry(owner)['assets'].get(new['asset'])
            if retained is not None and retained != entry:
                raise ValueError('clip media swap cannot rebind an existing asset alias')
            working_state.entity(owner + '/clip:' + clip_id)['asset'] = new['asset']
            working_state.registry(owner)['assets'][new['asset']] = copy.deepcopy(entry)
            replacements.append((_object_id(entry), new_kind))
    if not replacements:
        return [], copy.deepcopy(base), list(verified_media)
    commands = emit_document_commands(base, working)
    inputs = _media_admission_inputs(base, working, transport)
    verified = _raw_inputs(transport, base['project_id'], inputs, verified_media)
    missing = [{'pin': {'media_id': object_id, 'content_digest': object_id}}
               for object_id, _ in replacements
               if not any(row.get('object_id', row.get('selection', {}).get('object_id')) == object_id
                          for row in verified)]
    verified = _raw_inputs(transport, base['project_id'], missing, verified)
    for object_id, kind in replacements:
        for row in verified:
            if row.get('object_id', row.get('selection', {}).get('object_id')) == object_id:
                media_type = row.get('media_type')
                if not isinstance(media_type, str) or media_type.split('/')[0] != kind:
                    raise ValueError('replacement media type differs from Runtime metadata')
    if inputs:
        commands.append({'schema_version': 1, 'op': 'media.admit', 'version': '1',
                         'select': None, 'args': {'inputs': inputs}})
    plan = execute(base, commands, verified_media=verified)
    return plan.commands, working, verified
