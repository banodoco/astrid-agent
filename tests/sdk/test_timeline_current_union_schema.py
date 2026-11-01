"""The next command cohort preserves complete frozen S2 and all published floors."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline.commands import _SCHEMA, operations
from tests.sdk.command_schema_compatibility import compatibility_errors

FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'current-union-command-schemas'
SOURCES = {
    'runtime-0.2.9-94114269': '472622c45fa6b5c7a425c949516a64a52d83192d3749e78a0f000decd088aee9',
    'agent-frozen-s2-26ef2bbb': 'a6f1aaa9dfcc31828bd04c908158fd7fd9a825df6c9f88f79b81779273a190ef',
    'agent-word-caea687e': 'a058aa6ec55d86f84b74360f4e9c01d207ec1db86434170c5d5658b5c809130a',
    'agent-current-c456b328': '40ce6d81487f39236fbfb3adf13e6e0c3f43cfcc375b39b7badc32d65531fd4b',
    'agent-current-df2739f9': '83a476f894f31dc359514e9c081bf28da2b1f8cd515bb30cce14292823753224',
    'agent-frozen-s1-0a0ac7ba': '30da4fde84c328e7cb44035b50eb53fd2cd9637a14a0c948f4412f6469ce34bd',
    'runtime-0.2.10-6023cfb': '472622c45fa6b5c7a425c949516a64a52d83192d3749e78a0f000decd088aee9',
}

INVERSE_STEPS = {
    'type': 'object',
    'description': 'When present, select nonempty unique step IDs for every target publication; omitted selects the whole target commits.',
    'propertyNames': {'$ref': '#/$defs/ID'},
    'additionalProperties': {
        'type': 'array',
        'items': {'$ref': '#/$defs/ID'},
        'minItems': 1,
        'uniqueItems': True,
    },
    'minProperties': 1,
}


def baseline(name):
    raw = (FIXTURES / (name + '.json')).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SOURCES[name]
    return json.loads(raw)


def without_text(schema):
    candidate = copy.deepcopy(schema)
    for name in ('ScopedId', 'EntityRef', 'TargetKey'):
        branches = candidate['$defs'][name]['anyOf']
        assert len(branches) == 3
        legacy = json.loads((FIXTURES / 'runtime-0.2.2-6be97063.json').read_text())
        assert branches[0] == legacy['$defs'][name]
        candidate['$defs'][name] = branches[1]
    del candidate['$defs']['TextBindingSetArgs']
    conditions = candidate['$defs']['EditCommand']['allOf']
    assert len([item for item in conditions if item['if']['properties']['op']['const'] == 'text-binding.set']) == 1
    candidate['$defs']['EditCommand']['allOf'] = [
        item for item in conditions if item['if']['properties']['op']['const'] != 'text-binding.set']
    return candidate


@pytest.mark.parametrize('name', SOURCES)
def test_full_current_candidate_preserves_every_definition_and_registered_operation(name):
    assert compatibility_errors(baseline(name), _SCHEMA) == []


@pytest.mark.parametrize('name', SOURCES)
def test_every_dropped_definition_is_reported_even_when_not_referenced(name):
    previous = baseline(name)
    for definition in previous['$defs']:
        candidate = copy.deepcopy(_SCHEMA)
        del candidate['$defs'][definition]
        assert f'$/$defs/{definition}: missing definition' in compatibility_errors(previous, candidate)


@pytest.mark.parametrize('name', SOURCES)
def test_every_dropped_operation_is_reported_despite_the_open_generic_envelope(name):
    previous = baseline(name)
    for condition in previous['$defs']['EditCommand']['allOf']:
        op = condition['if']['properties']['op']['const']
        candidate = copy.deepcopy(_SCHEMA)
        candidate['$defs']['EditCommand']['allOf'] = [
            item for item in candidate['$defs']['EditCommand']['allOf']
            if item['if']['properties']['op']['const'] != op]
        assert (f'$/$defs/EditCommand/allOf/{op}: missing supported operation'
                in compatibility_errors(previous, candidate))


def test_only_reviewed_producer_and_consumed_additions_change_current_schema():
    previous = baseline('agent-current-c456b328')
    candidate = s2_slice()
    for definition in ('ProducerAdmittedTarget', 'ProducerIdentity', 'ProducerAttemptPin',
                       'ProducerGuardArgs', 'SettledProducerProposal'):
        del candidate['$defs'][definition]
    candidate['$defs']['EditCommand']['allOf'] = [
        item for item in candidate['$defs']['EditCommand']['allOf']
        if item['if']['properties']['op']['const'] != 'producer.guard']
    for definition in ('OccurrenceAddMediaArgs', 'PlacementCreateArgs'):
        del candidate['$defs'][definition]['properties']['consumed']
    candidate['$defs']['ClipSetElementArgs'] = previous['$defs']['ClipSetElementArgs']
    assert candidate == previous


def test_s2_only_adds_the_reviewed_optional_element_source_to_frozen_s1():
    previous = baseline('agent-frozen-s1-0a0ac7ba')
    candidate = s2_slice()
    element = candidate['$defs']['ClipSetElementArgs']
    assert element['properties'].pop('schema_source') == {'$ref': '#/$defs/DocumentPin'}
    assert element['required'] == ['elementRef'] and element['additionalProperties'] is False
    assert element['description'] == (
        'Replace an existing clip elementRef with another revision of the same element id, '
        'kind and selected pack owner. Admitted revisions require the exact retained '
        'schema_source declaration pin. The retired element_ref alias is removed. '
        'There is no clear: a clip keeps its element.')
    element['description'] = previous['$defs']['ClipSetElementArgs']['description']
    assert candidate == previous
    assert len(candidate['$defs']) == 48
    assert len(candidate['$defs']['EditCommand']['allOf']) == 22


@pytest.mark.parametrize('name', SOURCES)
def test_existing_argument_narrowing_still_fails(name):
    candidate = copy.deepcopy(_SCHEMA)
    candidate['$defs']['ClipMoveArgs']['properties']['on']['type'] = 'integer'
    assert compatibility_errors(baseline(name), candidate)


def without_doc_patch(schema):
    from astrid.sdk.timeline._doc_patch_schema import definitions, extend

    candidate = copy.deepcopy(schema)
    for name, definition in definitions().items():
        assert candidate['$defs'].pop(name) == definition
    conditions = candidate['$defs']['EditCommand']['allOf']
    addition = [item for item in conditions if item['if']['properties']['op']['const'] == 'doc.patch']
    assert len(addition) == 1
    candidate['$defs']['EditCommand']['allOf'] = [item for item in conditions if item not in addition]
    assert extend(candidate) == schema
    return candidate


def without_inverse_steps(schema):
    candidate = copy.deepcopy(schema)
    assert candidate['$defs']['InverseArgs']['properties'].pop('steps') == INVERSE_STEPS
    return candidate


def s2_slice():
    candidate = without_text(without_doc_patch(without_inverse_steps(_SCHEMA)))
    for definition in ('WordAnchor', 'ClipAnchorArgs', 'TimelineInitArgs',
                       'AssembledTargetArgs', 'ClipAddMediaArgs', 'OccurrenceCreateArgs'):
        del candidate['$defs'][definition]
    candidate['$defs']['EditCommand']['allOf'] = [
        item for item in candidate['$defs']['EditCommand']['allOf']
        if item['if']['properties']['op']['const'] not in
        ('clip.anchor', 'timeline.init', 'timeline.assembled-target', 'clip.add-media',
         'occurrence.create', 'clip.swap-media')]
    previous = baseline('agent-frozen-s2-26ef2bbb')
    assert candidate['$defs']['Intent']['properties']['ownership']['enum'].pop() == 'parent'
    candidate['$defs']['ClipSetArgs']['description'] = previous['$defs']['ClipSetArgs']['description']
    return candidate


def test_inverse_steps_is_exactly_the_new_optional_shape():
    inverse = _SCHEMA['$defs']['InverseArgs']
    assert inverse == {
        'type': 'object',
        'properties': {
            'publications': {
                'type': 'array',
                'items': {'$ref': '#/$defs/ID'},
                'minItems': 1,
                'uniqueItems': True,
            },
            'steps': INVERSE_STEPS,
        },
        'required': ['publications'],
        'additionalProperties': False,
    }


def test_next_successor_is_exactly_complete_frozen_s2_plus_reviewed_additions():
    assert s2_slice() == baseline('agent-frozen-s2-26ef2bbb')
    assert len(_SCHEMA['$defs']) == 58
    assert len(_SCHEMA['$defs']['EditCommand']['allOf']) == 30


def test_each_added_definition_and_operation_matches_its_approved_source():
    additions = json.loads((FIXTURES / 'next-command-additions.json').read_text())
    conditions = {item['if']['properties']['op']['const']: item
                  for item in _SCHEMA['$defs']['EditCommand']['allOf']}
    additions['text'] = json.loads((FIXTURES / 'text-command-additions.json').read_text())
    for source in additions.values():
        for name, value in source['definitions'].items():
            assert _SCHEMA['$defs'][name] == value
        for name, value in source.get('updates', {}).items():
            assert _SCHEMA['$defs'][name] == value
        for op, condition in source['operations'].items():
            assert conditions[op] == condition


def test_all_published_floors_preserve_every_field_with_only_historical_pattern_widening():
    sources = json.loads((FIXTURES / 'published-sources.json').read_text())
    assert set(sources) == {'0.2.1', '0.2.2', '0.2.3', '0.2.8', '0.2.9', '0.2.10'}
    for version, source in sources.items():
        raw = (FIXTURES / source['file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == source['sha256']
        previous, candidate = json.loads(raw), without_text(_SCHEMA)
        if version in ('0.2.1', '0.2.2'):
            # The conservative checker cannot prove regex inclusion. Verify
            # these exact additive alternatives, then check every other field.
            for definition in ('AnchorIdentity', 'EntityRef', 'ScopedId', 'TargetKey'):
                old = previous['$defs'][definition]['pattern']
                widened = old.replace('(?:clip|track)', '(?:clip|track|asset)').replace(
                    '|occurrence:[^/]+(?:/shot)?|cut:',
                    '|shot:(?:(?!::)[^/])+|occurrence:[^/]+(?:/shot)?|cut:')
                assert candidate['$defs'][definition]['pattern'] == widened
                candidate['$defs'][definition]['pattern'] = old
            if version == '0.2.2':
                old = next(item for item in previous['$defs']['EditCommand']['allOf']
                           if item['if']['properties']['op']['const'] == 'track.set')
                new = next(item for item in candidate['$defs']['EditCommand']['allOf']
                           if item['if']['properties']['op']['const'] == 'track.set')
                pattern = old['then']['properties']['select']['pattern']
                assert new['then']['properties']['select']['pattern'] == pattern.replace(
                    'parent/track:', '(?:parent|occurrence:[^/]+)/track:')
                new['then']['properties']['select']['pattern'] = pattern
        assert compatibility_errors(previous, candidate) == [], version


def test_closed_union_operation_inventory_is_exactly_the_supported_cohort():
    expected = {'doc.patch', 'text-binding.set', 'clip.swap-media', 'occurrence.create', 'clip.add', 'clip.add-media', 'clip.anchor', 'clip.delete', 'clip.move',
                'clip.set', 'clip.set-element', 'clip.set-params', 'clip.split', 'clip.trim',
                'occurrence.add-media', 'occurrence.delete', 'occurrence.duplicate',
                'occurrence.swap-media', 'placement.create', 'placement.move',
                'placement.unplace', 'producer.guard', 'shot.pool', 'shot.set',
                'timeline.assembled-target', 'timeline.init', 'timeline.inverse',
                'timeline.restore', 'track.create', 'track.set'}
    actual = [item['if']['properties']['op']['const']
              for item in _SCHEMA['$defs']['EditCommand']['allOf']]
    assert len(actual) == len(set(actual)) == 30
    # These approved provider commands have no frozen wire-schema tuple; the
    # closed union remains exactly the 30 published tuples below.
    provider_only = {'media.admit', 'occurrence.fork', 'occurrence.place-shared',
                     'timeline.duplicate', 'timeline.restructure', 'clip.duplicate-media'}
    assert set(operations()) - set(actual) == provider_only
    assert set(actual) == expected
    assert set(operations()) == (
        expected - {'timeline.inverse', 'timeline.restore'} | provider_only
    )
