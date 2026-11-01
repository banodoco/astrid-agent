"""clip.set-element: one recorded elementRef write, same element identity, guarded inverse."""
from __future__ import annotations

import copy
import json
from functools import partial
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set_element
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import bundle as _bundle
from tests.editing_conformance.test_recipe_m1_commands import clip, inverse, later

FIXTURES = Path(__file__).parent / 'fixtures'
REF = 'occurrence:occ-1/clip:picture-1'
PATH = ['elementRef']
DRAFT = {'id': 'reveal', 'kind': 'effect', 'revision': 'draft-reveal'}
GOLDEN = json.loads((FIXTURES / 'recipe_m2_clip_set_element.json').read_text())
ADMITTED = GOLDEN['request']['args']['elementRef']
PIN = GOLDEN['request']['args']['schema_source']
INPUTS = {'schema_inputs': GOLDEN['schema_inputs'], 'realm_id': 'realm'}
execute = partial(execute, **INPUTS)
replay = partial(replay, **INPUTS)
capture_clip_set_element = partial(capture_clip_set_element, schema_source=PIN, **INPUTS)
OTHER = {**ADMITTED, 'revision': 'sha256:' + 'b' * 64}


def call(element, ref=REF):
    return {'schema_version': 1, 'op': 'clip.set-element', 'version': '1',
            'select': ref, 'args': {'elementRef': copy.deepcopy(element), 'schema_source': PIN}}


def bundle():
    original = _bundle()
    original['source_mapping']['pinned_documents'] = [copy.deepcopy(GOLDEN['schema_inputs'][0][0])]
    return original


def seeded(field='elementRef', element=DRAFT):
    original = bundle()
    clip(original, REF)[field] = copy.deepcopy(element)
    return original


@pytest.mark.parametrize('field', ['elementRef', 'element_ref'])
def test_roundtrip_replay_and_alias_drop(field):
    original = seeded(field)
    command = capture_clip_set_element(original, REF, element=ADMITTED)
    assert command['expect'] == (
        {REF + '::elementRef': present(DRAFT)} if field == 'elementRef'
        else {REF + '::elementRef': ABSENT, REF + '::element_ref': present(DRAFT)})
    plan = execute(original, [command])
    row = clip(plan.bundle, REF)
    assert row['elementRef'] == ADMITTED and 'element_ref' not in row
    assert [w['path'] for w in plan.records[0]['writes']] == (
        [PATH] if field == 'elementRef' else [PATH, ['element_ref']])
    # Siblings and the other occurrence keep their own rows untouched.
    assert 'elementRef' not in clip(plan.bundle, 'occurrence:occ-2/clip:picture-1')
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle
    assert replay(plan, original).records == plan.records
    assert execute(original, [plan.commands[0]]).records == plan.records


def test_unchanged_write_stays_history_edge_and_aba_guards_inverse():
    original = seeded(element=ADMITTED)
    plan = execute(original, [call(ADMITTED)])
    assert plan.bundle == original
    assert plan.records[0]['writes'] == [
        {'ref': REF, 'path': PATH, 'before': present(ADMITTED), 'after': present(ADMITTED)}]
    for history in ([later(REF, PATH, ADMITTED)],
                    [later(REF, PATH, OTHER), later(REF, PATH, ADMITTED)]):
        with pytest.raises(CommandError, match='intervening'):
            inverse(plan.bundle, plan.records, history)
        assert replay(plan, original, intervening_records=history).records == plan.records


def test_changed_write_aba_refuses_inverse_but_rebases():
    original = seeded()
    plan = execute(original, [call(ADMITTED)])
    history = [later(REF, PATH, OTHER), later(REF, PATH, ADMITTED)]
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, history)
    head = copy.deepcopy(original)
    clip(head, REF)['elementRef'] = copy.deepcopy(OTHER)
    snapshot = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head)
    assert head == snapshot


@pytest.mark.parametrize('element', [
    {**ADMITTED, 'id': 'other'}, {**ADMITTED, 'kind': 'transition'},
    {**ADMITTED, 'packId': 'other-owner'}, {k: v for k, v in ADMITTED.items() if k != 'packId'}])
def test_element_identity_must_not_change(element):
    original = seeded(element=ADMITTED)
    snapshot = copy.deepcopy(original)
    with pytest.raises(CommandError, match='id and kind'):
        execute(original, [call(element)])
    assert original == snapshot


@pytest.mark.parametrize('before', [ABSENT, present(None), present('reveal')])
def test_requires_existing_ref_and_cannot_clear(before):
    original = bundle()
    if before != ABSENT:
        clip(original, REF)['elementRef'] = copy.deepcopy(before['value'])
    with pytest.raises(CommandError, match='existing element ref'):
        execute(original, [call(ADMITTED)])
    command = call(ADMITTED)
    command['args'] = {'elementRef': None}
    with pytest.raises(CommandError):
        execute(seeded(), [command])


@pytest.mark.parametrize('damage', ['unknown', 'missing', 'extra-ref-key', 'no-revision', 'shared',
                                   'unscoped', 'version', 'intent', 'clear'])
def test_closed_schema_refuses(damage):
    command = call(ADMITTED)
    if damage == 'unknown':
        command['args']['config'] = {}
    elif damage == 'missing':
        command['args'] = {}
    elif damage == 'extra-ref-key':
        command['args']['elementRef']['owner'] = 'x'
    elif damage == 'no-revision':
        del command['args']['elementRef']['revision']
    elif damage == 'shared':
        command['select'] = 'shot:source/clip:picture-1'
    elif damage == 'unscoped':
        command['select'] = 'clip:picture-1'
    elif damage == 'version':
        command['version'] = '2'
    elif damage == 'intent':
        command['intent'] = {'ownership': 'shared'}
    else:
        command['args']['clear'] = ['elementRef']
    with pytest.raises(CommandError):
        execute(seeded(), [command])


def test_disjoint_rebase_over_params_and_other_fields():
    original = seeded()
    clip(original, REF)['params'] = {'seed': 1}
    mine = execute(original, [call(ADMITTED)])
    theirs = execute(original, [capture_clip_set_params(original, REF, values={'seed': 2})])
    result = replay(mine, theirs.bundle, intervening_records=theirs.records)
    assert clip(result.bundle, REF)['params'] == {'seed': 2}
    assert clip(result.bundle, REF)['elementRef'] == ADMITTED
    assert result.commands == mine.commands
    assert inverse(result.bundle, result.records).bundle == theirs.bundle
    # Params were validated against the old element: moving the ref under them conflicts.
    with pytest.raises(CommandError):
        replay(theirs, mine.bundle, intervening_records=mine.records)


def test_grouped_with_params_roundtrips_in_one_batch():
    original = seeded()
    plan = execute(original, [capture_clip_set_params(original, REF, values={'seed': 3}),
                              capture_clip_set_element(original, REF, element=ADMITTED)])
    assert clip(plan.bundle, REF)['elementRef'] == ADMITTED
    assert clip(plan.bundle, REF)['params'] == {'seed': 3}
    assert replay(plan, original).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle


def test_parent_clip_leaves_legacy_mirror_and_app_elements_alone():
    original = bundle()
    ref = 'parent/clip:direct'
    clip(original, ref)['elementRef'] = copy.deepcopy(DRAFT)
    plan = execute(original, [call(ADMITTED, ref)])
    # The legacy parent-bundle shape is the caller's concern, not this command's.
    assert State(plan.bundle).get(ref, PATH) == present(ADMITTED)
    assert original['parent']['config'] == plan.bundle['parent']['config']


def test_portable_capture_golden():
    fixture = json.loads((FIXTURES / 'recipe_m2_clip_set_element.json').read_text())
    plan = execute(fixture['bundle'], [fixture['request']])
    assert plan.commands == [fixture['command']]
    assert plan.bundle == fixture['after_bundle']
    schema = json.loads((Path(__file__).parents[2] / 'astrid/core/timeline/schema/edit-command.schema.json').read_text())
    Draft202012Validator(schema).validate(fixture['command'])
    assert inverse(plan.bundle, plan.records).bundle == fixture['bundle']


@pytest.mark.parametrize('damage', ['missing-pin', 'stale-pin', 'missing-input', 'missing-object',
                                   'corrupt-object', 'unadmitted-revision'])
def test_selected_declaration_is_required_without_param_writes(damage):
    original = seeded()
    command = call(ADMITTED)
    inputs = copy.deepcopy(INPUTS)
    if damage == 'missing-pin':
        del command['args']['schema_source']
    elif damage == 'stale-pin':
        command['args']['schema_source'] = {**PIN, 'version': PIN['version'] + 1}
    elif damage == 'missing-input':
        inputs['schema_inputs'] = []
    elif damage == 'missing-object':
        inputs['schema_inputs'][0][1].clear()
    elif damage == 'corrupt-object':
        for obj in inputs['schema_inputs'][0][1].values():
            obj['bytes_base64'] = 'e30='
    else:
        command['args']['elementRef']['revision'] = 'sha256:' + '0' * 64
    snapshot = copy.deepcopy(original)
    with pytest.raises(CommandError):
        execute(original, [command], **inputs)
    assert original == snapshot


@pytest.mark.parametrize('change', ['membership', 'order'])
def test_element_source_scan_captures_membership_and_order(change):
    original = seeded()
    plan = execute(original, [call(ADMITTED)])
    selection = plan.commands[0]['selections'][0]
    assert selection['key'] == 'element.targets'
    assert selection['members'] == selection['order'] == [REF, 'occurrence:occ-1/clip:voice-1']
    head = copy.deepcopy(original)
    rows = State(head).collection('occurrence:occ-1', 'clip')
    if change == 'membership':
        rows.append({**copy.deepcopy(rows[0]), 'id': 'new-sibling'})
    else:
        rows.reverse()
    with pytest.raises(CommandError, match='selection'):
        replay(plan, head)
    with pytest.raises(CommandError):
        inverse(head, plan.records)
