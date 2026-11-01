"""Provider-side completion of Runtime's existing timeline preview."""
from __future__ import annotations

import copy


def prepared_records(plan, *, undo_of, restored_from):
    """Add v2 metadata to the existing authenticated recorder, when available."""
    records = {
        "schema_version": 1,
        "commands": copy.deepcopy(plan.records),
        "undo_of": copy.deepcopy(undo_of),
        "restored_from": copy.deepcopy(restored_from),
    }
    # These attributes are supplied by R1.  Keeping the fallback allows the
    # legacy provider ABI and its historical tests to remain unchanged until
    # the v2 schema is adopted by Runtime.
    for key in ("label", "steps", "merge_mode"):
        value = getattr(plan, key, None)
        if value is not None:
            records[key] = copy.deepcopy(value)
    return records


def capture_sequential_execution(port, context, original, plan, replay_bindings, prepared):
    """Carry generated original/landing execution to the request-scoped port."""
    from ._command_state import CommandError
    from .commit import _digest

    boundaries = getattr(original, '_sequential_execution', [])
    if not boundaries:
        return
    base = {'revision_id': context['request']['base_revision_id'],
            'publication_id': context['request']['base_publication_id']}
    staged = [binding for record in original.records for binding in record['bindings']
              if binding.get('key') == 'commit.materialization']
    required = prepared['expected_head'] != base and any(
        source['successor'] == binding.get('physical_ref')
        and source['boundary']['command_ordinal'] < record['ordinal']
        for source in staged
        for record in original.records for binding in record['bindings']
        if binding.get('key') == 'commit.read' or binding.get('key', '').startswith('doc.patch:'))
    capture = getattr(port, 'capture_sequential_execution', None)
    if not callable(capture):
        if required:
            raise CommandError('Runtime port cannot authenticate original sequential execution',
                               kind='sequential_proof_unavailable')
        return

    def source(value):
        if isinstance(value, str):
            return value
        step_id, previous, owner, owners = value
        return {'step_id': step_id, 'source': source(previous), 'owner': owner,
                'owners': copy.deepcopy(owners)}

    bindings = []
    for key, value in replay_bindings.items():
        if len(key) == 2:
            item = {'command_ordinal': key[0], 'kind': 'patch', 'change_ordinal': key[1]}
        else:
            ordinal, _, ref, path, phase = key
            def read_binding(executed):
                return next(binding for binding in executed.records[ordinal]['bindings']
                            if binding.get('key') == 'commit.read' and binding['ref'] == ref
                            and tuple(binding['path']) == path
                            and binding['boundary']['phase'] == phase)

            # Reads retain their complete execution captures. The replay map
            # indexes only physical turnover, including staged step outputs.
            if read_binding(original)['physical_ref'] == read_binding(plan)['physical_ref']:
                continue
            item = {'command_ordinal': ordinal, 'kind': 'read', 'ref': ref,
                    'path': list(path), 'phase': phase}
        bindings.append({**item, 'source': source(value)})

    def execution(executed, head):
        records = prepared_records(executed, undo_of=prepared['records']['undo_of'],
                                   restored_from=prepared['records']['restored_from'])
        records['undo_steps'] = copy.deepcopy(prepared['records']['undo_steps'])
        return {'evaluated_head': copy.deepcopy(head),
                'commands': copy.deepcopy(getattr(executed, '_sequential_execution', [])),
                'records': records}

    capture(copy.deepcopy({
        'schema_version': 1, 'context': context,
        'original': execution(original, base),
        'landing': execution(plan, prepared['expected_head']),
        'replay_bindings': bindings, 'prepared_digest': _digest(prepared),
    }))


def complete_preview(result, prepared, request, evaluated_head):
    """Expose the landing head and authoritative v2 candidate metadata.

    The Runtime port remains the authority and performs all guarded preview
    validation.  This only projects metadata already sealed in ``prepared``;
    it never synthesizes a client-side candidate or writes durable state.
    """
    result = copy.deepcopy(result)
    data = result.get("data")
    if not isinstance(data, dict):
        return result
    data.setdefault("evaluated_head", copy.deepcopy(evaluated_head))
    data.setdefault("base_revision_id", request["base_revision_id"])
    data.setdefault("base_publication_id", request["base_publication_id"])
    records = prepared.get("records", {})
    for key in ("steps", "merge_mode", "label"):
        if key in records:
            data[key] = copy.deepcopy(records[key])
    base = {"revision_id": request["base_revision_id"],
            "publication_id": request["base_publication_id"]}
    data.setdefault("would_status", "accepted" if evaluated_head == base else "merged")
    result["receipt"] = None
    return result
