"""Bounded first native creation: literal parent rows and real recorder facts."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from astrid.core.timeline.duration import clip_end_frame, clip_start_frame
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._collections import restoration_orders
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import accepted_boundaries, inverse
from tests.sdk.test_timeline_commit_provider_document import document_fixture

FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_clip_add.json'


def empty_document():
    payload = {'config': {'tracks': [], 'clips': [],
                          'output': {'fps': {'numerator': 30000, 'denominator': 1001}},
                          'unknown': [None, False, {'literal': 'first-clip'}]},
               'clips': [], 'occurrences': [], 'registry': {'assets': {}},
               'opaque': {'keep': None}}
    return {'schema_version': 1, 'project_id': 'project-1', 'timeline_id': 'film',
            'parent_revision': {'revision_id': 'empty-1', 'payload': payload,
                                'content_digest': _digest(payload)},
            'shot_revisions': [], 'internal_timeline_revisions': [],
            'dependency_manifest': _manifest([], [], [], [])}


def literal_row(**changes):
    return {'id': 'first-clip', 'clipType': 'text', 'track': 'first-track',
            'at': .017, 'hold': 1.013, 'params': {'text': 'First'},
            'app': {'astrid': {'on': None, 'opaque': ['first-clip', None]}, 'future': False},
            'nullable': None, 'sourceURL': None, 'from': None,
            'opaque': {'id': 'first-clip', 'ordered': [None, False, 0, {}, []]}, **changes}


def with_track():
    original = bundle_from_document(empty_document())
    row = {'id': 'first-track', 'kind': 'visual', 'role': 'output', 'unknown': [None, False]}
    return execute(original, [capture_track_create(original, 'parent', row)]).bundle


def test_empty_bootstrap_explicit_track_then_first_clip_golden():
    golden = load_fixture(FIXTURE)
    original = bundle_from_document(golden['input'])
    plan = execute(original, golden['commands'])
    assert plan.records == golden['records']
    assert document_from_bundle(plan.bundle) == golden['output']
    assert original == bundle_from_document(golden['input'])
    assert plan.commands[1] == capture_clip_add(
        execute(original, [golden['commands'][0]]).bundle, 'parent', literal_row())
    assert plan.records[1]['created'] == [{'ref': 'parent/clip:first-clip',
                                         'kind': 'clip', 'owner': 'parent'}]
    assert plan.records[1]['writes'] == [{'ref': 'parent/clip:first-clip', 'path': [],
                                        'before': ABSENT, 'after': {'state': 'present', 'value': literal_row()}}]
    assert json.loads(json.dumps(plan.commands, allow_nan=False)) == plan.commands


def test_literal_seconds_and_opaque_json_exact_reload_inverse_redo():
    original = with_track()
    row = literal_row()
    command = capture_clip_add(original, 'parent', row)
    row['opaque']['ordered'].append('caller mutation')
    plan = execute(original, [command])
    assert State(plan.bundle).entity('parent/clip:first-clip') == literal_row()
    assert not any(r['path'] == ['config', 'output', 'fps'] for r in command['reads'])
    created = document_from_bundle(plan.bundle)
    reloaded = bundle_from_document(created)
    assert document_from_bundle(reloaded) == created
    seconds = State(reloaded).entity('parent/clip:first-clip')
    assert (seconds['at'], seconds['hold']) == (.017, 1.013)
    assert clip_start_frame(seconds, 30000 / 1001) == 1
    assert clip_end_frame(seconds, 30000 / 1001) == 31
    undone = inverse(reloaded, plan.records)
    assert document_from_bundle(undone.bundle) == document_from_bundle(original)
    redone = inverse(undone.bundle, undone.records)
    assert document_from_bundle(redone.bundle) == created
    # An accepted inverse insertion keeps the historical boundary requirement.
    accepted = accepted_boundaries(redone.records, undone.bundle, redone.bundle)
    assert document_from_bundle(inverse(redone.bundle, accepted).bundle) == document_from_bundle(original)
    with pytest.raises(CommandError, match='accepted boundary'):
        inverse(redone.bundle, redone.records)
    assert replay(plan, original).records == plan.records


def test_explicit_null_speed_is_preserved_by_native_creation_and_compilation():
    original = with_track()
    row = literal_row(speed=None)
    plan = execute(original, [capture_clip_add(original, 'parent', row)])
    assert State(plan.bundle).entity('parent/clip:first-clip') == row
    assert document_from_bundle(plan.bundle)['parent_revision']['payload']['clips'] == [row]
    assert inverse(plan.bundle, plan.records).bundle == original


def test_append_preserves_existing_rows_order_and_closed_source_pins_on_rebase_and_inverse():
    original = bundle_from_document(document_fixture())
    row = literal_row(track='picture')
    command = capture_clip_add(original, 'parent', row)
    plan = execute(original, [command])
    head = copy.deepcopy(original)
    for rows in (head['parent']['clips'], head['parent']['config']['clips']):
        rows[0]['opaque'] = {'other-author': [None, False]}
    rebased = replay(plan, head)
    assert rebased.bundle['parent']['clips'] == head['parent']['clips'] + [row]
    assert inverse(rebased.bundle, plan.records).bundle == head
    result = document_from_bundle(rebased.bundle)
    baseline = document_from_bundle(head)
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert result[key] == baseline[key]
    assert result['parent_revision']['payload']['occurrences'] == baseline['parent_revision']['payload']['occurrences']
    assert all(r['ref'] != 'parent/clip:clip-a' for r in command['reads'])


@pytest.mark.parametrize('field,value', [('id', ''), ('id', 'bad/id'), ('id', 'bad::id'),
    ('track', ''), ('track', 'bad/id'), ('clipType', ''), ('at', -1), ('at', True),
    ('at', None), ('at', float('inf')), ('hold', 0), ('hold', -1), ('hold', True),
    ('hold', None), ('hold', float('nan'))])
def test_invalid_rows_refuse_without_mutation(field, value):
    original = with_track()
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        capture_clip_add(original, 'parent', literal_row(**{field: value}))
    assert original == before


@pytest.mark.parametrize('field', ['id', 'clipType', 'track', 'at', 'hold'])
def test_required_complete_row_and_closed_args(field):
    row = literal_row()
    del row[field]
    with pytest.raises(CommandError):
        capture_clip_add(with_track(), 'parent', row)
    command = {'schema_version': 1, 'op': 'clip.add', 'version': '1', 'select': 'parent',
               'args': {'row': literal_row(), 'fallbackTrack': True}}
    with pytest.raises(CommandError):
        execute(with_track(), [command])


@pytest.mark.parametrize('changes', [{'clipType': 'media'}, {'clipType': 'audio'}, {'asset': 'x'},
    {'asset': None}, {'media_id': 'sha256:' + '1' * 64}, {'sourceURL': 'https://example.invalid/x'},
    {'from': 0}, {'to': 2}, {'speed': 2}, {'speed': True}, {'at_ms': 17}, {'duration': 1.013},
    {'time_map': {}}, {'children': []}, {'anchor': 'word:x:1'}, {'app': {'on': 'word:x:1'}},
    {'app': {'astrid': {'until_film_end': True}}}, {'app': {'astrid': {'formulas': {}}}}])
def test_unsupported_media_source_window_and_follow_refuse_without_mutation(changes):
    original = with_track()
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        capture_clip_add(original, 'parent', literal_row(**changes))
    assert original == before


@pytest.mark.parametrize('changes', [{'id': 'different'}, {'role': 'source'}, {'kind': 'other'},
    {'enabled': False}, {'active': False}, {'disabled': True}, {'hidden': True}, {'deleted': True}])
def test_destination_identity_kind_role_activity_guarded(changes):
    original = with_track()
    command = capture_clip_add(original, 'parent', literal_row())
    head = copy.deepcopy(original)
    State(head).entity('parent/track:first-track').update(changes)
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(execute(original, [command]), head)
    with pytest.raises(CommandError):
        capture_clip_add(head, 'parent', literal_row())
    assert head == before


def test_membership_order_new_id_absence_and_intent_claims_are_authoritative():
    original = with_track()
    command = capture_clip_add(original, 'parent', literal_row())
    plan = execute(original, [command])
    with pytest.raises(CommandError) as error:
        capture_clip_add(plan.bundle, 'parent', literal_row())
    assert error.value.kind == 'identity_collision'
    for edit in ('member', 'order'):
        head = copy.deepcopy(plan.bundle)
        extra = literal_row(id='other')
        for rows in (head['parent']['clips'], head['parent']['config']['clips']):
            rows.insert(0 if edit == 'order' else len(rows), extra)
        with pytest.raises(CommandError):
            inverse(head, plan.records)
    forged = copy.deepcopy(command)
    forged['created'] = []
    with pytest.raises(CommandError, match='created'):
        execute(original, [forged])
    forged = copy.deepcopy(command)
    forged['intent'] = {'anchor': 'follow'}
    with pytest.raises(CommandError):
        execute(original, [forged])
    with pytest.raises(CommandError):
        capture_clip_add(original, 'occurrence:missing', literal_row())
    with pytest.raises(CommandError):
        capture_clip_add(bundle_from_document(empty_document()), 'parent', literal_row())


def test_ordinary_append_proof_requires_created_primitive_facts():
    original = with_track()
    record = execute(original, [capture_clip_add(original, 'parent', literal_row())]).records[0]
    assert restoration_orders([record])['parent', 'clip'][1] == ['parent/clip:first-clip']
    for change in ('missing-created', 'wrong-owner', 'wrong-kind', 'wrong-row'):
        forged = copy.deepcopy(record)
        if change == 'missing-created':
            forged['created'] = []
        elif change == 'wrong-owner':
            forged['created'][0]['owner'] = 'occurrence:other'
        elif change == 'wrong-kind':
            forged['created'][0]['kind'] = 'track'
        else:
            forged['writes'][0]['after']['value']['id'] = 'other'
        with pytest.raises(CommandError, match='accepted boundary'):
            restoration_orders([forged])


def test_canonical_schema_closes_the_operation_and_preserves_opaque_row_fields():
    schema = json.loads((Path(__file__).parents[2] / 'astrid/core/timeline/schema/edit-command.schema.json').read_text())
    validator = Draft202012Validator(schema)
    valid = capture_clip_add(with_track(), 'parent', literal_row())
    validator.validate(valid)
    invalid = copy.deepcopy(valid)
    invalid['args']['unknown'] = None
    assert list(validator.iter_errors(invalid))
    invalid = copy.deepcopy(valid)
    invalid['select'] = 'occurrence:other'
    assert list(validator.iter_errors(invalid))


def test_new_track_and_first_clip_batch_has_one_exact_inverse():
    original = bundle_from_document(empty_document())
    track = capture_track_create(original, 'parent', {'id': 'first-track', 'kind': 'visual'})
    after_track = execute(original, [track]).bundle
    add = capture_clip_add(after_track, 'parent', literal_row())
    plan = execute(original, [track, add])
    assert inverse(plan.bundle, plan.records).bundle == original
