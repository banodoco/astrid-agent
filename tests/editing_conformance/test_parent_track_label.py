"""Conformance for the bounded native parent-track label command."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline._track_commands import capture_track_set
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute, operations
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import bundle, inverse, later

FIXTURES = Path(__file__).parent / 'fixtures'
REF = 'parent/track:picture'


def call(*, select=REF, values=None, clear=()):
    return {
        'schema_version': 1,
        'op': 'track.set',
        'version': '1',
        'select': select,
        'args': {
            'values': {} if values is None else values,
            'clear': list(clear),
        },
    }


def test_published_track_capture_uses_the_authoritative_scalar_handler():
    from astrid.sdk.timeline import _scalar_commands, _track_commands

    assert _track_commands.capture_track_set is _scalar_commands.capture_track_set
    assert _track_commands.track_set is _scalar_commands.track_set
    assert operations()['track.set'].handler is _scalar_commands.track_set
    assert operations()['clip.set-params'].handler is _scalar_commands.clip_set_params


@pytest.mark.parametrize('before', [ABSENT, present(''), present('Old')])
@pytest.mark.parametrize('clear', [False, True])
def test_parent_label_presence_empty_and_clear_roundtrip(before, clear):
    original = bundle()
    if before != ABSENT:
        State(original).entity(REF)['label'] = copy.deepcopy(before['value'])
    snapshot = copy.deepcopy(original)
    command = capture_track_set(original, REF, values={} if clear else {'label': ''},
                                clear=['label'] if clear else [])
    assert command['expect'] == {REF + '::label': before}
    plan = execute(original, [command])
    assert State(plan.bundle).get(REF, ['label']) == (ABSENT if clear else present(''))
    assert original == snapshot
    assert inverse(plan.bundle, plan.records).bundle == original
    assert replay(plan, original).records == plan.records


def test_app_parent_track_label_capture_golden_is_exact_and_portable():
    fixture = json.loads((FIXTURES / 'recipe_m2_app_track_label.json').read_text())
    command = capture_track_set(fixture['bundle'], fixture['input']['target_ref'],
                                values={'label': fixture['input']['label']})
    plan = execute(fixture['bundle'], [command])

    assert fixture['source'] == 'src/tools/video-editor/commands/fixtures/authoringBundle.json'
    assert fixture['app_case'] == 'parent-only'
    assert command == fixture['command']
    # Full native record from ceb2ab67/77b1f0f7, including the parent pin wrapper.
    assert plan.records == [{
        'ordinal': 0,
        'command': fixture['command'],
        'writes': [{'ref': REF, 'path': ['label'],
                    'before': fixture['before'], 'after': fixture['after']}],
        'reads': fixture['command']['reads'],
        'selections': [],
        'bindings': [{'key': 'scalar.target', 'ref': REF, 'policy': 'fixed',
                      'dependencies': ['scalar.images']}],
        'created': [],
        'dependencies': [{'key': 'scalar.images', 'kind': 'entity',
                          'pin': {'parent': fixture['bundle']['base_parent']}, 'policy': 'exact'}],
        'derived_writes': [],
    }]
    assert execute(fixture['bundle'], [fixture['request']]).commands == [command]
    assert plan.bundle == fixture['after_bundle']
    assert State(fixture['bundle']).get(REF, ['label']) == ABSENT
    assert State(plan.bundle).get(REF, ['label']) == present('Picture é 🎬')
    assert plan.bundle['source_mapping']['_runtime_document'] == (
        fixture['bundle']['source_mapping']['_runtime_document'])
    assert plan.bundle['source_mapping']['_runtime_document']['parent_revision']['payload']['unknown'] == {
        'opaque': [None, {'keep': True}], 'number': 1.25}
    assert inverse(plan.bundle, plan.records).bundle == fixture['bundle']
    schema = json.loads((Path(__file__).parents[2] /
                         'astrid/core/timeline/schema/edit-command.schema.json').read_text())
    Draft202012Validator(schema).validate(command)


@pytest.mark.parametrize('damage', ['deleted', 'duplicate'])
def test_parent_track_identity_deletion_and_ambiguity_fail_closed(damage):
    original = bundle()
    tracks = State(original).config('parent')['tracks']
    if damage == 'deleted':
        tracks.clear()
    else:
        tracks.append(copy.deepcopy(tracks[0]))
    with pytest.raises(CommandError, match='(?:disappeared|ambiguous|missing)'):
        capture_track_set(original, REF, values={'label': 'Picture'})


def test_deleted_parent_track_can_be_recreated_without_stale_capture():
    original = bundle()
    tracks = State(original).config('parent')['tracks']
    row = tracks.pop()
    with pytest.raises(CommandError):
        capture_track_set(original, REF, values={'label': 'Picture'})
    tracks.append(row)
    command = capture_track_set(original, REF, values={'label': 'Picture'})
    assert execute(original, [command]).bundle['parent']['config']['tracks'][0]['label'] == 'Picture'


def test_parent_label_preserves_unicode_opaque_siblings_and_exact_pins():
    original = bundle()
    track = State(original).entity(REF)
    track['opaque'] = {'keep': [None, False, 'opaque']}
    command = capture_track_set(original, REF, values={'label': '画面 🎬'})
    plan = execute(original, [command])
    changed = State(plan.bundle).entity(REF)
    assert changed['label'] == '画面 🎬'
    assert changed['opaque'] == track['opaque']
    dependency = next(row for row in plan.records[0]['dependencies'] if row['key'] == 'scalar.images')
    assert dependency['policy'] == 'exact'
    assert dependency['pin'] == {'parent': original['base_parent']}
    assert plan.records[0]['command']['bindings'] == {'scalar.target': REF}


def test_parent_label_is_independent_of_a_disjoint_clip_move():
    original = bundle()
    moved = execute(original, [capture_move(original, 'parent/clip:direct', 4)])
    command = capture_track_set(moved.bundle, REF, values={'label': 'Picture'})
    result = execute(moved.bundle, [command])
    assert State(result.bundle).entity('parent/clip:direct')['at'] == 4
    assert State(result.bundle).entity(REF)['label'] == 'Picture'
    assert inverse(result.bundle, result.records).bundle == moved.bundle


def test_parent_label_same_path_conflict_refuses_without_partial_write():
    original = bundle()
    command = capture_track_set(original, REF, values={'label': 'Picture'})
    changed = copy.deepcopy(original)
    State(changed).entity(REF)['label'] = 'Other'
    snapshot = copy.deepcopy(changed)
    with pytest.raises(CommandError):
        execute(changed, [command])
    assert changed == snapshot


def test_parent_label_unchanged_write_is_recorded_and_aba_undo_is_guarded():
    original = bundle()
    State(original).entity(REF)['label'] = ''
    command = capture_track_set(original, REF, values={'label': ''})
    plan = execute(original, [command])
    assert plan.bundle == original
    assert any(write['path'] == ['label'] and write['before'] == write['after']
               for write in plan.records[0]['writes'])
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later(REF, ['label'], '')])
    away = execute(plan.bundle, [capture_track_set(plan.bundle, REF, values={'label': 'Away'})])
    back = execute(away.bundle, [capture_track_set(away.bundle, REF, values={'label': ''})])
    with pytest.raises(CommandError, match='intervening'):
        inverse(back.bundle, plan.records, [later(REF, ['label'], '')])


@pytest.mark.parametrize('command', [
    call(),
    call(values={'label': None}),
    call(values={'label': 'Picture'}, clear=['label']),
    call(values={'label': 'Picture'}, clear=['label', 'label']),
    call(values={'other': 'Picture'}),
    call(select='shot:chapter-shot/track:picture', values={'label': 'Picture'}),
])
def test_parent_track_label_closed_args_and_select_refuse_invalid_commands(command):
    with pytest.raises(CommandError):
        execute(bundle(), [command])


def test_published_capture_supports_reviewed_occurrence_labels_and_exact_inverse():
    original = bundle()
    ref = 'occurrence:occ-1/track:picture'
    command = capture_track_set(original, ref, values={'label': 'Picture'})
    plan = execute(original, [command])
    assert State(plan.bundle).get(ref, ['label']) == present('Picture')
    assert State(plan.bundle).get(REF, ['label']) == State(original).get(REF, ['label'])
    assert inverse(plan.bundle, plan.records).bundle == original
    assert replay(plan, original).records == plan.records
