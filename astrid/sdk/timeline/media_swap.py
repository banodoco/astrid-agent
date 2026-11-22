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
        if raw not in rows:
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
    for placement in base.get('placements', []):
        owner = 'occurrence:' + placement['occurrence_id']
        if placement not in candidate.get('placements', []):
            continue
        before = {row['id']: row for row in original.collection(owner, 'clip')}
        after = {row['id']: row for row in target.collection(owner, 'clip')}
        for clip_id in before.keys() & after.keys():
            old, new = before[clip_id], after[clip_id]
            if old.get('asset') == new.get('asset') or not isinstance(new.get('asset'), str):
                continue
            old_entry = original.registry(owner)['assets'].get(old.get('asset'))
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
    inputs = _media_admission_inputs(base, candidate, transport)
    verified = _raw_inputs(transport, base['project_id'], inputs, verified_media)
    if inputs:
        commands.append({'schema_version': 1, 'op': 'media.admit', 'version': '1',
                         'select': None, 'args': {'inputs': inputs}})
    plan = execute(base, commands, verified_media=verified)
    return plan.commands, copy.deepcopy(candidate), verified
