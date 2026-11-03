"""Authoritative doc.patch argument schema; emitted by gen_doc_patch_schema."""
from __future__ import annotations

import copy


def definitions():
    ref = {'type': 'string', 'pattern': r'^(parent|(?:shot|shot-revision|internal|internal-revision):.+)$'}
    path = {'type': 'array', 'items': {'type': 'string'}}
    identity = {'type': 'string', 'minLength': 1}
    common = {'ref': ref, 'path': path, 'before': {'$ref': '#/$defs/AgentState'}}
    variants = []
    for op in ('set', 'remove', 'list.insert', 'list.remove', 'list.move'):
        properties = {**copy.deepcopy(common), 'op': {'const': op}}
        if op in ('set', 'list.insert'):
            properties['value'] = {'$ref': '#/$defs/JSON'}
        if op.startswith('list.'):
            properties.update(id=identity, order_before={'type': 'array', 'items': identity, 'uniqueItems': True})
            if op != 'list.remove':
                properties['after_id'] = {'anyOf': [identity, {'type': 'null'}]}
        variants.append({'type': 'object', 'properties': properties,
                         'required': list(properties), 'additionalProperties': False})
    read_common = {'ref': {**ref, 'pattern': r'^(timeline|parent|(?:shot|shot-revision|internal|internal-revision):.+)$'},
                   'path': path}
    reads = []
    for kind in ('field', 'collection', 'scope'):
        properties = {**copy.deepcopy(read_common), 'kind': {'const': kind}}
        if kind == 'field':
            properties['state'] = {'$ref': '#/$defs/AgentState'}
        elif kind == 'collection':
            properties['members'] = {'type': 'array', 'items': identity, 'uniqueItems': True}
            properties['order'] = {'anyOf': [{'type': 'null'}, properties['members']]}
        else:
            properties['path'] = {'const': []}
        reads.append({'type': 'object', 'properties': properties, 'required': list(properties),
                      'additionalProperties': False})
    return {'DocPatchChange': {'oneOf': variants},
            'DocPatchArgs': {'type': 'object', 'properties': {
                'changes': {'type': 'array', 'minItems': 1, 'items': {'$ref': '#/$defs/DocPatchChange'}}},
                'required': ['changes'], 'additionalProperties': False},
            'CommitRead': {'oneOf': reads}}


def extend(schema):
    schema = copy.deepcopy(schema)
    schema['$defs'].update(definitions())
    clauses = schema['$defs']['EditCommand']['allOf']
    clauses[:] = [item for item in clauses if item['if']['properties']['op'].get('const') != 'doc.patch']
    clauses.append({'if': {'properties': {'op': {'const': 'doc.patch'}}, 'required': ['op']},
                    'then': {'properties': {'select': {'type': 'null'}, 'version': {'const': '1'},
                                            'args': {'$ref': '#/$defs/DocPatchArgs'},
                                            'intent': {'type': 'object', 'additionalProperties': False}}}})
    return schema
