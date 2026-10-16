"""Instance-addressed clip fields consumed by the existing setters and runner.

Wire clips retain their ordered list. Semantic paths use modifiers["instance"]
and modifiers.order; numeric array positions are deliberately not accepted.
"""
from __future__ import annotations

import copy
import json
import re

import jsonschema

from astrid.sdk.timeline._common import TimelineEditError

_INSTANCE = re.compile(r'^modifiers\[("(?:[^"\\]|\\.)*")\](.*)$')


def handles(path):
    return path == 'modifiers' or path.startswith(('modifiers.', 'modifiers['))


def address(path):
    match = _INSTANCE.fullmatch(path)
    if not match or not json.loads(match[1]):
        raise TimelineEditError('use modifiers["instance-id"] or modifiers.order; array position is not identity')
    suffix = match[2]
    if suffix and not re.fullmatch(r'\.params(?:\.[A-Za-z_][\w-]*|\[\d+\])*', suffix):
        raise TimelineEditError('a modifier edit addresses its instance or params property path')
    return json.loads(match[1]), suffix


def instances(data):
    rows = data.get('modifiers', [])
    if not isinstance(rows, list):
        raise TimelineEditError('modifiers must be an ordered list')
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'id', 'element', 'params'}
                or not isinstance(row['id'], str) or not row['id'].strip()
                or not isinstance(row['element'], str) or not isinstance(row['params'], dict)):
            raise TimelineEditError('a modifier requires exactly id, element and object params')
        if row['id'] in result:
            raise TimelineEditError(f'duplicate modifier instance {row["id"]!r}')
        result[row['id']] = row
    return result


def validate(row):
    if row['element'].startswith('timeline.component/'):
        from astrid.core.pack.components import validate_component_reference
        validate_component_reference(row)
        return
    # Read declarations through the existing catalog. No component code or I/O
    # implementation is loaded. Unknown/unavailable declarations fail closed.
    from astrid.core.element.catalog import read_element_schema, read_element_meta
    from astrid.core.pack import qualified_id_pack_id
    try:
        qualified_id_pack_id(row['element'])
        schema = read_element_schema(row['element'], kind='components')
        meta = read_element_meta(row['element'], kind='components')
        # Same declaration fields as I's admitted ModifierDeclaration; the
        # catalog supplies data only, never a second renderer/implementation.
        if meta.get('kind') != 'modifier' or meta.get('element') != row['element']:
            raise ValueError('modifier declaration identity/subtype mismatch')
        if any(meta.get(field) != 'preserve' for field in ('bounds', 'alpha', 'support')):
            raise ValueError('modifier declaration must preserve bounds, alpha and support')
        if not isinstance(meta.get('hosts'), list) or not meta['hosts']:
            raise ValueError('modifier declaration must name its supported hosts')
        jsonschema.Draft202012Validator(schema).validate(row['params'])
    except (KeyError, ValueError, jsonschema.ValidationError) as exc:
        raise TimelineEditError(f'{row["element"]}: invalid modifier: {exc}') from None


def set_value(clip, path, value):
    from astrid.sdk.timeline.doc import _set_path
    from astrid.sdk.timeline.commands import _path_state
    if clip.is_audio:
        raise TimelineEditError('modifiers require a clip visual subtree')
    rows = copy.deepcopy(clip.data.get('modifiers', []))
    by_id = instances({'modifiers': rows})
    if path == 'modifiers.order':
        if (not isinstance(value, list) or any(not isinstance(x, str) for x in value)
                or len(value) != len(by_id) or set(value) != set(by_id)):
            raise TimelineEditError('modifier order must contain each existing instance ID exactly once')
        rows = [by_id[key] for key in value]
    else:
        mid, suffix = address(path)
        if not suffix:
            row = copy.deepcopy(value)
            instances({'modifiers': [row]})
            if row['id'] != mid:
                raise TimelineEditError('modifier path and instance ID must agree')
            if mid in by_id:
                rows[rows.index(by_id[mid])] = row
            else:
                rows.append(row)
        else:
            if mid not in by_id:
                raise TimelineEditError(f'missing modifier instance {mid!r}')
            row = by_id[mid]
            # Validate the complete result; nested values never bypass schema.
            _path_state(row, suffix[1:])  # grammar validation before mutation
            try:
                _set_path(row, suffix[1:], copy.deepcopy(value))
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise TimelineEditError(f'invalid modifier property path: {exc}') from None
        validate(row)
    clip.data['modifiers'] = rows
    return clip


def clear_value(clip, path):
    mid, suffix = address(path)
    if suffix:
        raise TimelineEditError('clear a modifier by its instance ID')
    by_id = instances(clip.data)
    if mid in by_id:
        clip.data['modifiers'] = [row for row in clip.data['modifiers'] if row['id'] != mid]
    return clip


def paths(before, after):
    """Diff list contents by ID; order is an independent authored property."""
    left, right = instances({'modifiers': before}), instances({'modifiers': after})
    yield 'modifiers.order', list(left), list(right)
    for mid in sorted(set(left) | set(right)):
        yield 'modifiers[' + json.dumps(mid) + ']', left.get(mid), right.get(mid)
