"""Conservative test-only containment; unsupported changes require explicit review."""
from __future__ import annotations

import json

from jsonschema import Draft202012Validator


def compatibility_errors(previous, current):
    """Return every incompatible path. Both arguments are complete canonical schemas."""
    for schema in (previous, current):
        Draft202012Validator.check_schema(schema)

    def same(a, b):
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def operations(conditions):
        result = {}
        for condition in conditions or []:
            op = condition['if']['properties']['op']['const']
            expected = {'if': {'properties': {'op': {'const': op}}, 'required': ['op']},
                        'then': condition['then']}
            if not same(condition, expected) or op in result:
                raise ValueError('unsupported operation conditional')
            result[op] = condition['then']
        return result

    def resolve(node, root):
        seen = set()
        while isinstance(node, dict) and set(node) == {'$ref'}:
            ref = node['$ref']
            if not ref.startswith('#/$defs/') or ref in seen:
                raise ValueError('unsupported or cyclic reference: ' + ref)
            seen.add(ref)
            node = root['$defs'][ref.removeprefix('#/$defs/')]
        return node

    def compare(old, new, path, seen=frozenset()):
        if same(old, new) or new is True or old is False:
            return []
        pair = (id(old), id(new))
        if pair in seen:
            return []
        seen = seen | {pair}
        try:
            old, new = resolve(old, previous), resolve(new, current)
            if isinstance(new, dict) and set(new) == {'anyOf'} and any(
                    same(old, resolve(branch, current)) for branch in new['anyOf']):
                return []  # A complete old branch survives, including all its constraints.
        except (KeyError, ValueError) as error:
            return [f'{path}: review {error}']
        if same(old, new):
            return []
        if not isinstance(old, dict) or not isinstance(new, dict):
            return [f'{path}: narrowed boolean schema']
        errors = []
        for key in old.keys() | new.keys():
            a, b = old.get(key), new.get(key)
            at = f'{path}/{key}'
            if same(a, b) or key in {'title', 'description', '$comment', 'examples', 'default'}:
                continue
            if key == '$defs':
                for name, value in (a or {}).items():
                    errors += (compare(value, (b or {})[name], at + '/' + name, seen)
                               if name in (b or {}) else [at + '/' + name + ': missing definition'])
            elif key == 'properties':
                for name in (a or {}).keys() | (b or {}).keys():
                    errors += compare((a or {}).get(name, old.get('additionalProperties', True)),
                                      (b or {}).get(name, new.get('additionalProperties', True)),
                                      at + '/' + name, seen)
            elif key == 'additionalProperties':
                errors += compare(old.get(key, True), new.get(key, True), at, seen)
            elif key in {'items', 'propertyNames'} and a is not None:
                errors += compare(a, new.get(key, True), at, seen)
            elif key == 'required' and set(b or []) <= set(a or []):
                continue
            elif key in {'enum', 'type'} and a is not None and (
                    b is None or all(any(same(x, y) for y in (b if isinstance(b, list) else [b]))
                                     for x in (a if isinstance(a, list) else [a]))):
                continue
            elif key in {'minimum', 'exclusiveMinimum', 'minItems', 'minLength', 'minProperties'} and (
                    b is None or (a is not None and b <= a)):
                continue
            elif key in {'maximum', 'exclusiveMaximum', 'maxItems', 'maxLength', 'maxProperties'} and (
                    b is None or (a is not None and b >= a)):
                continue
            elif key == 'uniqueItems' and not b:
                continue
            elif key == 'pattern' and (b is None or (
                    a is not None and a.startswith('^') and a.endswith('$')
                    and b.startswith(f'(?:{a})|(?:^') and b.endswith('$)'))):
                continue
            elif key == 'anyOf' and a is not None and b is not None and all(
                    any(not compare(x, y, at, seen) for y in b) for x in a):
                continue
            elif key == 'allOf' and path == '$/$defs/EditCommand':
                continue  # Required operation inventory/args are checked unconditionally below.
            else:
                errors.append(at + ': narrowing or unsupported change; review required')
        return sorted(errors)

    errors = compare(previous, current, '$')
    definitions = current.get('$defs', {}) if isinstance(current, dict) else {}
    errors += ['$/$defs/' + name + ': missing definition'
               for name in previous['$defs'] if name not in definitions]
    command = definitions.get('EditCommand', {})
    try:
        before = operations(previous['$defs']['EditCommand']['allOf'])
        after = operations(command.get('allOf', []) if isinstance(command, dict) else [])
        for op, value in before.items():
            path = '$/$defs/EditCommand/allOf/' + op
            errors += (compare(value, after[op], path) if op in after
                       else [path + ': missing supported operation'])
    except (KeyError, TypeError, ValueError) as error:
        errors.append(f'$/$defs/EditCommand/allOf: review {error}')
    return sorted(set(errors))
