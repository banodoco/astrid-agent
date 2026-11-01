"""Shared leaf clears preserve ancestor presence and retain guarded intent."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline._recording import Recording
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.staging_recipes import install_staging_operations
from tests.editing_conformance.test_recipe_m1_commands import bundle, clip, inverse

REF = 'occurrence:occ-1/clip:picture-1'
PATH = ['params', 'nested', 'leaf']
CONTAINERS = [
    (ABSENT, ['params']),
    (present(None), ['params']),
    (present({}), ['params', 'nested']),
    (present({'nested': None}), ['params', 'nested']),
    (present({'nested': {'other': [False, None]}}), None),
    (present({'nested': {'leaf': None, 'other': [False, None]}}), None),
]


def set_container(row, container):
    if container != ABSENT:
        row['params'] = copy.deepcopy(container['value'])


def test_recorded_dict_insertion_detaches_nested_setter_from_state():
    original = {'parent': {'config': {'app': {
        'before': {'keep': True}, 'after': {'keep': True}}}}}
    inserted = present({'nested': {'leaf': 'before'}})
    ctx = Recording(original, {}, 0)

    ctx.assign('parent', ['config', 'app', 'target'], inserted)
    ctx.assign('parent', ['config', 'app', 'target', 'nested', 'leaf'], present('scratch'))

    assert list(original['parent']['config']['app']) == ['before', 'after', 'target']
    assert inserted == present({'nested': {'leaf': 'before'}})


def clear_call():
    return {'schema_version': 1, 'op': 'fixture.properties', 'version': '1', 'select': REF,
            'args': {'changes': [{'path': PATH, 'state': ABSENT}]}}


@pytest.mark.parametrize('container,guard_path', CONTAINERS)
@pytest.mark.parametrize('primitive', ['set', 'clear', 'assign'])
def test_shared_clear_records_only_leaf_and_needed_ancestor(container, guard_path, primitive):
    original = {'parent': {}}
    set_container(original['parent'], container)
    candidate = copy.deepcopy(original)
    state = State(candidate)
    before = state.get('parent', PATH)
    if primitive == 'set':
        state.set('parent', PATH, ABSENT)
    else:
        ctx = Recording(candidate, {}, 0)
        if primitive == 'clear':
            ctx.clear('parent', PATH)
        else:
            ctx.assign('parent', PATH, ABSENT)
        assert ctx.record['writes'] == [
            {'ref': 'parent', 'path': PATH, 'before': before, 'after': ABSENT}]
        assert ctx.record['reads'] == ([] if guard_path is None else [
            {'ref': 'parent', 'path': guard_path, 'state': State(original).get('parent', guard_path)}])
        assert ctx.finish(capture=True)['command']['expect'] == {'parent::params.nested.leaf': before}
    expected = copy.deepcopy(original)
    if before != ABSENT:
        del expected['parent']['params']['nested']['leaf']
    assert candidate == expected


@pytest.mark.parametrize('value', [False, 0, '', [], ['object'], 'scalar'])
@pytest.mark.parametrize('depth', [0, 1])
def test_shared_clear_rejects_invalid_scalar_or_array_ancestors(value, depth):
    original = {'parent': {'params': value if depth == 0 else {'nested': value}}}
    for primitive in ('set', 'clear', 'assign'):
        candidate = copy.deepcopy(original)
        with pytest.raises(CommandError):
            if primitive == 'set':
                State(candidate).set('parent', PATH, ABSENT)
            elif primitive == 'clear':
                Recording(candidate, {}, 0).clear('parent', PATH)
            else:
                Recording(candidate, {}, 0).assign('parent', PATH, ABSENT)
        assert candidate == original


@pytest.mark.parametrize('container', [ABSENT, present(None), present({}), present({'nested': None})])
def test_shared_set_still_requires_recorded_container_creation(container):
    original = {'parent': {}}
    set_container(original['parent'], container)
    candidate = copy.deepcopy(original)
    with pytest.raises(CommandError):
        State(candidate).set('parent', PATH, present(1))
    assert candidate == original


@pytest.mark.parametrize('container,guard_path', CONTAINERS)
def test_generic_clear_replay_exact_inverse_and_redo(monkeypatch, container, guard_path):
    install_staging_operations(monkeypatch)
    original = bundle()
    set_container(clip(original, REF), container)
    plan = execute(original, [clear_call()])
    assert replay(plan, original).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    redone = inverse(undone.bundle, undone.records)
    assert redone.bundle == plan.bundle
    assert replay(undone, plan.bundle, inverse_records=plan.records).records == undone.records
    assert replay(redone, original, inverse_records=undone.records).records == redone.records


@pytest.mark.parametrize('container,guard_path', CONTAINERS[:4])
@pytest.mark.parametrize('replacement', [ABSENT, present(None), present({}), present(False)])
def test_generic_clear_guards_first_absent_or_null_ancestor(monkeypatch, container, guard_path, replacement):
    install_staging_operations(monkeypatch)
    original = bundle()
    set_container(clip(original, REF), container)
    plan = execute(original, [clear_call()])
    expected = State(original).get(REF, guard_path)
    assert {'ref': REF, 'path': guard_path, 'state': expected} in plan.records[0]['reads']
    if replacement == expected:
        return
    head = copy.deepcopy(original)
    State(head).set(REF, guard_path, replacement)
    snapshot = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head)
    with pytest.raises(CommandError):
        inverse(head, plan.records)
    assert head == snapshot


def test_generic_clear_preserves_disjoint_existing_leaves(monkeypatch):
    install_staging_operations(monkeypatch)
    original = bundle()
    clip(original, REF)['params'] = {'nested': {'other': None}, 'opaque': [False]}
    plan = execute(original, [clear_call()])
    assert plan.records[0]['reads'] == [{'ref': REF, 'path': PATH, 'state': ABSENT}]
    head = copy.deepcopy(original)
    clip(head, REF)['params']['nested']['other'] = 'concurrent'
    later = [{'writes': [{'ref': REF, 'path': ['params', 'nested', 'other'],
                          'before': present(None), 'after': present('concurrent')}]}]
    assert replay(plan, head, intervening_records=later).bundle == head
    assert inverse(head, plan.records, later).bundle == head


@pytest.mark.parametrize('container,guard_path', CONTAINERS[:5])
@pytest.mark.parametrize('aba', [False, True])
def test_generic_clear_retains_unchanged_and_aba_publication_guards(monkeypatch, container, guard_path, aba):
    install_staging_operations(monkeypatch)
    original = bundle()
    set_container(clip(original, REF), container)
    plan = execute(original, [clear_call()])
    paths = [PATH] + ([] if guard_path is None else [guard_path])
    for path in paths:
        before = State(original).get(REF, path)
        writes = ([{'ref': REF, 'path': path, 'before': before, 'after': present('away')}]
                  if aba else [])
        writes.append({'ref': REF, 'path': path,
                       'before': present('away') if aba else before, 'after': before})
        assert replay(plan, original, intervening_records=[{'writes': writes}]).records == plan.records
        with pytest.raises(CommandError) as error:
            inverse(original, plan.records, [{'writes': writes}])
        assert error.value.kind == 'intervening_write'
