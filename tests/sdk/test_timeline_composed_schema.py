"""The Runtime generator consumes the same closed args as the native registry."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError

from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.commands import _SCHEMA, operations, validate_command

DOC_PATCH_SAMPLE = {
    'schema_version': 1,
    'op': 'doc.patch',
    'version': '1',
    'select': None,
    'args': {'changes': [{
        'ref': 'parent',
        'path': ['opaque', 'schema-census'],
        'before': {'state': 'absent'},
        'op': 'set',
        'value': True,
    }]},
}

PROVIDER_ONLY_ARGS = {
    'media.admit': {
        'inputs': [{'pin': {
            'media_id': 'sha256:' + '0' * 64,
            'content_digest': 'sha256:' + '0' * 64,
        }}],
    },
    'timeline.duplicate': {
        'source_ref': {
            'project_id': 'source-project',
            'timeline_id': 'source-timeline',
            'revision_id': 'source-revision',
            'publication_id': 'source-publication',
            'content_digest': 'source-digest',
        },
        'shots': {'source-shot': 'copied-shot'},
        'occurrences': {'source-occurrence': 'copied-occurrence'},
    },
    'occurrence.place-shared': {'occurrence_id': 'copied-occurrence', 'on': 0},
    'occurrence.fork': {'shot_id': 'shot-fork'},
    'timeline.restructure': {},
    'clip.duplicate-media': {
        'id': 'copied-media',
        'old_media': {
            'asset': 'old-asset',
            'generation_id': 'old-generation',
            'variant_id': 'old-variant',
            'object_id': 'sha256:' + '0' * 64,
            'catalog_digest': 'sha256:' + '1' * 64,
        },
        'media': {
            'asset': 'new-asset',
            'generation_id': 'new-generation',
            'variant_id': 'new-variant',
            'object_id': 'sha256:' + '2' * 64,
            'catalog_digest': 'sha256:' + '3' * 64,
        },
    },
}


def fixture_commands(value):
    if isinstance(value, dict):
        if value.get('schema_version') == 1 and 'op' in value and 'args' in value:
            yield value
        for child in value.values():
            yield from fixture_commands(child)
    elif isinstance(value, list):
        for child in value:
            yield from fixture_commands(child)


def test_every_registered_operation_has_closed_canonical_args_and_a_valid_golden():
    Draft202012Validator.check_schema(_SCHEMA)
    validator = Draft202012Validator(_SCHEMA)
    fixtures = Path(__file__).parents[1] / 'editing_conformance' / 'fixtures'
    commands = {command['op']: command for path in sorted(fixtures.glob('*.json'))
                for command in fixture_commands(json.loads(path.read_text()))}
    commands['doc.patch'] = DOC_PATCH_SAMPLE
    conditions = {condition['if']['properties']['op']['const']: condition['then']['properties']
                  for condition in _SCHEMA['$defs']['EditCommand']['allOf']}
    for op, operation in operations().items():
        assert operation.parameters['additionalProperties'] is False
        # These approved semantic commands are registered in the provider but
        # have no frozen wire-schema tuple. Validate their canonical closed
        # argument objects directly so the schema gap remains visible.
        if op in PROVIDER_ONLY_ARGS:
            assert op not in conditions
            command = {
                'schema_version': 1,
                'op': op,
                'version': '1',
                'select': (None if op == 'media.admit' else
                           'parent' if op == 'timeline.duplicate' else
                           'occurrence:occ-0'),
                'args': copy.deepcopy(PROVIDER_ONLY_ARGS[op]),
            }
            arg_validator = validator.evolve(schema=operation.parameters)
            arg_validator.validate(command['args'])
            unknown = copy.deepcopy(command['args'])
            unknown['unsupported_argument'] = None
            with pytest.raises(ValidationError):
                arg_validator.validate(unknown)
            continue
        ref = conditions[op]['args']['$ref'].removeprefix('#/$defs/')
        assert _SCHEMA['$defs'][ref] == operation.parameters
        validator.validate(commands[op])
        unknown = copy.deepcopy(commands[op])
        unknown['args']['unsupported_argument'] = None
        with pytest.raises(ValidationError):
            validator.validate(unknown)


@pytest.mark.parametrize('ref', ['shot:raw-shot', 'occurrence:raw-occurrence',
                                'parent/asset:alias', 'occurrence:raw-occurrence/asset:alias'])
def test_root_and_scoped_asset_references_share_the_canonical_patterns(ref):
    for name in ('ScopedId', 'EntityRef', 'AnchorIdentity'):
        Draft202012Validator(_SCHEMA).evolve(
            schema={'$ref': '#/$defs/' + name}).validate(ref)
    Draft202012Validator(_SCHEMA).evolve(
        schema={'$ref': '#/$defs/TargetKey'}).validate(ref + '::')


def test_inverse_schema_accepts_whole_and_selected_step_forms_and_rejects_bad_shapes():
    whole = {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
             'args': {'publications': ['pub-notes']}}
    selected = copy.deepcopy(whole)
    selected['args']['steps'] = {'pub-notes': ['note-1', 'note-2']}

    for command in (whole, selected):
        Draft202012Validator(_SCHEMA).validate(command)
        validate_command(command)

    invalid = [
        {**whole, 'args': {**whole['args'], 'steps': None}},
        {**whole, 'args': {**whole['args'], 'steps': {}}},
        {**whole, 'args': {**whole['args'], 'steps': {'pub-notes': []}}},
        {**whole, 'args': {**whole['args'], 'steps': {'pub-notes': ['note-1', 'note-1']}}},
        {**whole, 'args': {**whole['args'], 'steps': {'': ['note-1']}}},
        {**whole, 'args': {**whole['args'], 'steps': {'pub-notes': ['']}}},
        {**whole, 'args': {**whole['args'], 'extra': None}},
    ]
    for command in invalid:
        with pytest.raises((ValidationError, CommandError)):
            validate_command(command)
