"""Accepted cache stages reconcile clip roots without relaxing authored guards."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError, State, present
from astrid.sdk.timeline.commands import execute
from tests.editing_conformance.test_native_word_anchor_vectors import fixture
from tests.editing_conformance.test_recipe_m1_commands import inverse

REF = 'parent/clip:born'


def history(plans):
    entries = [{'records': {'commands': plan.records, 'undo_of': (
        plan.commands[0]['args']['publications'] if plan.commands[0]['op'] == 'timeline.inverse' else [])},
        'before': before, 'after': plan.bundle} for before, plan in plans]
    return AcceptedRecords(entries, [record for entry in entries for record in entry['records']['commands']],
                           lambda entry: (entry['before'], entry['after']))


def cohort():
    original, pin, transcript = fixture()
    row = {'id': 'born', 'clipType': 'am-type', 'track': original['parent']['clips'][0]['track'],
           'at': 0, 'hold': .5, 'opaque': [None, False, 0, {}, []]}
    born = execute(original, [{'schema_version': 1, 'op': 'clip.add', 'version': '1',
        'select': 'parent', 'args': {'row': row}}])
    plans = [(original, born)]
    for index in (0, 1):
        before = plans[-1][1].bundle
        plan = execute(before, [{'schema_version': 1, 'op': 'clip.anchor', 'version': '1',
            'select': REF, 'args': {'field': 'on', 'declaration': 'again', 'choice': {
                'source': 'occurrence:occ-a/clip:voice', 'document': pin,
                'word_ids': [transcript['segments'][0]['words'][index]['id']]}}}])
        plans.append((before, plan))
    before = plans[-1][1].bundle
    undone_b = inverse(before, history(plans[-1:]))
    plans.append((before, undone_b))
    return original, plans


def mirrored_write(bundle, path, value):
    state = State(bundle)
    state.set(REF, path, present(value))
    bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])


def test_grouped_birth_word_a_b_and_inverse_b_restores_raw_bundle():
    original, plans = cohort()
    assert State(plans[0][1].bundle).get(REF, ['at']) != State(plans[-1][1].bundle).get(REF, ['at'])
    accepted = history(plans)
    snapshot = copy.deepcopy(list(accepted))
    undone = inverse(plans[-1][1].bundle, accepted)
    assert json.dumps(undone.bundle, sort_keys=True) == json.dumps(original, sort_keys=True)
    assert list(accepted) == snapshot


def test_one_batch_birth_and_both_choices_reverses_the_final_cache_stage():
    original, plans = cohort()
    calls = [{key: value for key, value in plan.commands[0].items()
              if key in ('schema_version', 'op', 'version', 'select', 'args')}
             for _, plan in plans[:3]]
    plan = execute(original, calls)
    undone = inverse(plan.bundle, history([(original, plan)]))
    assert json.dumps(undone.bundle, sort_keys=True) == json.dumps(original, sort_keys=True)


@pytest.mark.parametrize('path,value', [
    (['at'], 99), (['hold'], 99), (['opaque'], [None, True]), (['track'], 'other'),
    (['app', 'astrid', 'word_anchors', 'on', 'source'], 'occurrence:other/clip:voice'),
])
def test_unrecorded_cache_or_authored_root_changes_still_refuse_atomically(path, value):
    _, plans = cohort()
    head = copy.deepcopy(plans[-1][1].bundle)
    mirrored_write(head, path, value)
    snapshot = copy.deepcopy(head)
    with pytest.raises(CommandError) as error:
        inverse(head, history(plans))
    assert error.value.kind == 'inverse_conflict'
    assert head == snapshot


@pytest.mark.parametrize('aba', [False, True])
def test_later_explicit_write_even_of_same_cache_value_still_refuses(aba):
    _, plans = cohort()
    head = plans[-1][1].bundle
    value = State(head).get(REF, ['at'])
    writes = [{'ref': REF, 'path': ['at'], 'before': value, 'after': value}]
    if aba:
        writes = [{**writes[0], 'after': present(99)}, {**writes[0], 'before': present(99)}]
    with pytest.raises(CommandError) as error:
        inverse(head, history(plans), [{'writes': writes}])
    assert error.value.kind == 'intervening_write'


def test_unauthenticated_or_changed_cache_history_is_not_a_root_exemption():
    _, plans = cohort()
    accepted = history(plans)
    with pytest.raises(CommandError) as error:
        inverse(plans[-1][1].bundle, list(accepted))
    assert error.value.kind == 'history_unavailable'
    changed = copy.deepcopy(list(accepted))
    changed[1]['derived_writes'][0]['before'] = present(99)
    forged = AcceptedRecords(accepted.entries, changed, accepted.load_boundary)
    with pytest.raises(CommandError) as error:
        inverse(plans[-1][1].bundle, forged)
    assert error.value.kind == 'history_unavailable'
