"""Generic clear/aggregate composition keeps tagged presence and guarded intent."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.inverse import _patch, coalesce
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.staging_recipes import install_staging_operations
from tests.editing_conformance.test_recipe_m1_commands import bundle, clip, inverse

REF = 'occurrence:occ-1/clip:picture-1'
PATH = ['params', 'nested', 'leaf']
CONTAINERS = [ABSENT, present(None), present({}), present({'nested': None}),
              present({'nested': {'leaf': None, 'sibling': [False, None]}, 'opaque': {}})]


def test_matching_child_write_cannot_prove_absent_ancestor_read():
    state = State(bundle())
    state.set(REF, ['params'], present({'nested': {'leaf': 1}, 'opaque': True}))
    record = {'command': {'op': 'fixture.properties'}, 'ordinal': 0,
              'reads': [{'ref': REF, 'path': ['params'], 'state': ABSENT}],
              'writes': [{'ref': REF, 'path': PATH, 'before': present(1), 'after': present(1)}],
              'selections': [], 'bindings': [], 'created': []}
    before = copy.deepcopy(state.bundle)
    from astrid.sdk.timeline.inverse import _validate_inputs
    with pytest.raises(CommandError) as error:
        _validate_inputs(state, record, [record])
    assert error.value.kind == 'inverse_dependency_changed'
    assert state.bundle == before


def change(path, state, ref=REF):
    return {'schema_version': 1, 'op': 'fixture.properties', 'version': '1', 'select': ref,
            'args': {'changes': [{'path': path, 'state': state}]}}


def roundtrip(original, plan):
    snapshot = copy.deepcopy(original)
    replayed = replay(plan, original)
    assert replayed.bundle == plan.bundle
    assert replayed.records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    redone = inverse(undone.bundle, undone.records)
    assert redone.bundle == plan.bundle
    assert replay(undone, plan.bundle, inverse_records=plan.records).records == undone.records
    assert replay(redone, original, inverse_records=undone.records).records == redone.records
    assert original == snapshot


@pytest.mark.parametrize('container', CONTAINERS[:4])
def test_patch_absent_descendant_preserves_aggregate_presence(container):
    snapshot = copy.deepcopy(container)
    path = ['nested', 'leaf'] if isinstance(container.get('value'), dict) else ['leaf']
    patched = _patch(container, path, ABSENT)
    assert patched == container == snapshot
    assert patched is not container


@pytest.mark.parametrize('container', CONTAINERS[:4])
def test_patch_present_descendant_requires_historical_containers(container):
    snapshot = copy.deepcopy(container)
    path = ['nested', 'leaf'] if isinstance(container.get('value'), dict) else ['leaf']
    with pytest.raises(CommandError) as error:
        _patch(container, path, present(None))
    assert error.value.kind == 'history_unavailable'
    assert container == snapshot


def test_patch_present_leaf_and_collection_alias_keep_historical_structure():
    container = present({'rows': [{'id': 'a', 'nested': {}}, {'id': 'b', 'nested': None}]})
    patched = _patch(container, ['rows', ('id', 'a'), 'nested', 'leaf'], present(None))
    assert patched['value']['rows'][0]['nested'] == {'leaf': None}
    assert container['value']['rows'][0]['nested'] == {}
    assert _patch(container, ['rows', ('id', 'b'), 'nested', 'leaf'], ABSENT) == container
    with pytest.raises(CommandError, match='historical container'):
        _patch(container, ['rows', ('id', 'b'), 'nested', 'leaf'], present(1))
    with pytest.raises(CommandError, match='historical order'):
        _patch(container, ['rows', ('id', 'missing')], present({'id': 'missing'}))


def test_patch_null_in_array_preserves_presence_without_inventing_container():
    container = present({'rows': [None, {'opaque': False}]})
    assert _patch(container, ['rows', '0', 'leaf'], ABSENT) == container
    with pytest.raises(CommandError, match='historical container'):
        _patch(container, ['rows', '0', 'leaf'], present(None))


@pytest.mark.parametrize('before', CONTAINERS)
@pytest.mark.parametrize('after', CONTAINERS)
@pytest.mark.parametrize('ordering', ['clear-set', 'set-clear', 'clear-set-clear'])
def test_nested_clear_aggregate_orderings_roundtrip(monkeypatch, before, after, ordering):
    install_staging_operations(monkeypatch)
    original = bundle()
    State(original).set(REF, ['params'], before)
    clear, aggregate = change(PATH, ABSENT), change(['params'], after)
    commands = {'clear-set': [clear, aggregate], 'set-clear': [aggregate, clear],
                'clear-set-clear': [clear, aggregate, clear]}[ordering]
    plan = execute(original, commands)
    expected = copy.deepcopy(original)
    for command in commands:
        write = command['args']['changes'][0]
        State(expected).set(REF, write['path'], write['state'])
    assert plan.bundle == expected
    combined = coalesce(plan.records)
    assert combined == [{'ref': REF, 'path': ['params'], 'before': before,
                         'after': State(expected).get(REF, ['params'])}]
    assert any(w['path'] == PATH for record in plan.records for w in record['writes'])
    roundtrip(original, plan)


@pytest.mark.parametrize('container', [ABSENT, present(None)])
@pytest.mark.parametrize('ordering', ['clear-set', 'set-clear', 'clear-set-clear'])
def test_native_clear_set_orderings_exact_replay_inverse_redo(container, ordering):
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    original = bundle()
    State(original).set(REF, ['params'], container)
    clear = {'schema_version': 1, 'op': 'clip.set-params', 'version': '1', 'select': REF,
             'args': {'values': {}, 'clear': ['x']}}
    set_value = capture_clip_set_params(original, REF, values={'x': None})
    commands = {'clear-set': [clear, set_value], 'set-clear': [set_value, clear],
                'clear-set-clear': [clear, set_value, clear]}[ordering]
    plan = execute(original, commands)
    assert clip(plan.bundle, REF)['params'] == ({'x': None} if ordering == 'clear-set' else {})
    roundtrip(original, plan)


@pytest.mark.parametrize('container', CONTAINERS[:4])
@pytest.mark.parametrize('aba', [False, True])
def test_noop_batch_allows_equal_state_rebase_and_keeps_inverse_publication_guards(monkeypatch, container, aba):
    install_staging_operations(monkeypatch)
    original = bundle()
    State(original).set(REF, ['params'], container)
    plan = execute(original, [change(PATH, ABSENT), change(PATH, ABSENT)])
    assert plan.bundle == original
    assert coalesce(plan.records) == [{'ref': REF, 'path': PATH, 'before': ABSENT, 'after': ABSENT}]
    roundtrip(original, plan)
    ancestor = ['params', 'nested'] if isinstance(container.get('value'), dict) else ['params']
    for path in (PATH, ancestor):
        before = State(original).get(REF, path)
        writes = ([{'ref': REF, 'path': path, 'before': before, 'after': present('away')}]
                  if aba else [])
        writes.append({'ref': REF, 'path': path,
                       'before': present('away') if aba else before, 'after': before})
        history = [{'writes': writes}]
        assert replay(plan, original, intervening_records=history).records == plan.records
        with pytest.raises(CommandError) as error:
            inverse(original, plan.records, history)
        assert error.value.kind == 'intervening_write'


def test_leaf_batch_preserves_disjoint_concurrent_edits_and_refuses_guarded_changes(monkeypatch):
    install_staging_operations(monkeypatch)
    original = bundle()
    clip(original, REF)['params'] = {'nested': {'sibling': None}, 'opaque': [False]}
    plan = execute(original, [change(PATH, ABSENT), change(PATH, present(None)), change(PATH, ABSENT)])
    assert plan.bundle == original
    assert all(read['path'] == PATH for record in plan.records for read in record['reads'])
    head = copy.deepcopy(original)
    clip(head, REF)['params']['nested']['sibling'] = 'concurrent'
    history = [{'writes': [{'ref': REF, 'path': ['params', 'nested', 'sibling'],
                           'before': present(None), 'after': present('concurrent')}]}]
    assert replay(plan, head, intervening_records=history).bundle == head
    undone = inverse(head, plan.records, history)
    assert undone.bundle == head
    assert inverse(undone.bundle, undone.records).bundle == head
    history[0]['writes'][0]['path'] = PATH
    assert replay(plan, original, intervening_records=history).records == plan.records
    with pytest.raises(CommandError) as error:
        inverse(original, plan.records, history)
    assert error.value.kind == 'intervening_write'
