"""Ordinary native structural stages share one guarded authored inverse."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline.capture import capture_clip_delete
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.inverse import coalesce
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_clip_delete import delete_document, reload
from tests.editing_conformance.test_recipe_m1_commands import accepted_boundaries, inverse, later
from tests.editing_conformance.test_recipe_m2_clip_add import literal_row, with_track


def add_delete(bundle, row):
    add = capture_clip_add(bundle, 'parent', row)
    added = execute(bundle, [add])
    delete = capture_clip_delete(added.bundle, 'parent/clip:' + row['id'])
    return execute(bundle, [add, delete])


def authored(bundle):
    document = document_from_bundle(bundle)
    # Synthetic fixture revision names need not be regenerated after reload.
    document['parent_revision'].pop('revision_id')
    return document


@pytest.mark.parametrize('populated', [False, True])
def test_native_add_delete_net_zero_inverse_redo_retains_unchanged_write(populated):
    original = bundle_from_document(delete_document()) if populated else with_track()
    row = literal_row(track='picture' if populated else 'first-track')
    plan = add_delete(original, row)
    immutable = copy.deepcopy(plan.records)
    ref = 'parent/clip:first-clip'
    unchanged = {'ref': ref, 'path': [], 'before': ABSENT, 'after': ABSENT}
    assert plan.bundle == original
    assert coalesce(plan.records) == [unchanged]
    assert plan.records[0]['created'] == [{'ref': ref, 'kind': 'clip', 'owner': 'parent'}]
    assert plan.records[0]['writes'] == [dict(unchanged, after=present(row))]
    assert plan.records[1]['writes'] == [dict(unchanged, before=present(row))]
    assert replay(plan, original).records == plan.records
    undone = inverse(reload(plan.bundle), plan.records)
    assert authored(undone.bundle) == authored(original)
    # C0 retains intermediate reversals and restoration facts in the recorder;
    # only the boundary projection is absent-to-absent.
    sequential = [dict(unchanged, after=present(row)), dict(unchanged, before=present(row))]
    created = [{'ref': ref, 'kind': 'clip', 'owner': 'parent'}]
    assert undone.records[0]['writes'] == sequential
    assert coalesce(undone.records) == [unchanged]
    assert undone.records[0]['created'] == created
    accepted = accepted_boundaries(undone.records, plan.bundle, undone.bundle)
    redone = inverse(reload(undone.bundle), accepted)
    assert authored(redone.bundle) == authored(original)
    assert redone.records[0]['writes'] == sequential
    assert coalesce(redone.records) == [unchanged]
    assert redone.records[0]['created'] == created
    for result, input_bundle in ((plan, original), (undone, plan.bundle), (redone, undone.bundle)):
        assert State(result.bundle).get(ref, []) == ABSENT
        assert {'ref': ref, 'path': [], 'state': ABSENT} in result.records[0]['reads']
        damaged = copy.deepcopy(result.records)
        damaged[0]['writes'][0]['after']['value']['opaque']['intermediate'] = 'changed'
        if result is not plan:
            damaged = accepted_boundaries(damaged, input_bundle, result.bundle)
        head = reload(result.bundle)
        before = copy.deepcopy(head)
        with pytest.raises(CommandError) as error:
            inverse(head, damaged)
        assert error.value.kind == 'inverse_conflict'
        assert head == before
    assert plan.records == immutable


@pytest.mark.parametrize('deleted', ['clip-a', 'clip-b', 'clip-c'])
@pytest.mark.parametrize('add_first', [False, True])
def test_mixed_existing_delete_new_add_preserves_order_opaque_clears_and_pins(deleted, add_first):
    original = bundle_from_document(delete_document())
    row = literal_row(track='picture')
    if add_first:
        add = capture_clip_add(original, 'parent', row)
        middle = execute(original, [add]).bundle
        calls = [add, capture_clip_delete(middle, 'parent/clip:' + deleted)]
    else:
        delete = capture_clip_delete(original, 'parent/clip:' + deleted)
        middle = execute(original, [delete]).bundle
        calls = [delete, capture_clip_add(middle, 'parent', row)]
    plan = execute(original, calls)
    assert State(plan.bundle).collection('parent', 'clip') == [
        clip for clip in original['parent']['clips'] if clip['id'] != deleted] + [row]
    expected = authored(plan.bundle)
    undone = inverse(reload(plan.bundle), plan.records)
    assert authored(undone.bundle) == authored(original)
    accepted = accepted_boundaries(undone.records, plan.bundle, undone.bundle)
    assert authored(inverse(reload(undone.bundle), accepted).bundle) == expected
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert expected[key] == document_from_bundle(original)[key]


def test_net_zero_preserves_disjoint_change_and_guards_unchanged_address_history():
    original = bundle_from_document(delete_document())
    plan = add_delete(original, literal_row(track='picture'))
    head = copy.deepcopy(original)
    State(head).entity('parent/clip:clip-a')['opaque']['newer'] = None
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    disjoint = [later('parent/clip:clip-a', ['opaque', 'newer'], None)]
    assert replay(plan, head, intervening_records=disjoint).bundle == head
    undone = inverse(head, plan.records, disjoint)
    assert undone.bundle == head
    for records in (plan.records, undone.records):
        aba = [later('parent/clip:first-clip', [], None)]
        with pytest.raises(CommandError) as error:
            inverse(head, records, aba)
        assert error.value.kind == 'intervening_write'


@pytest.mark.parametrize('change', ['order', 'membership', 'dependent'])
def test_net_zero_refuses_changed_selection_or_declared_dependent_atomically(change):
    original = bundle_from_document(delete_document())
    plan = add_delete(original, literal_row(track='picture'))
    head = copy.deepcopy(original)
    rows = head['parent']['clips']
    if change == 'order':
        rows.reverse()
    elif change == 'membership':
        rows.append(literal_row(id='other', track='picture'))
    else:
        rows[0]['app'] = {'astrid': {'on': 'first-clip'}}
    head['parent']['config']['clips'] = copy.deepcopy(rows)
    immutable = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head)
    with pytest.raises(CommandError):
        inverse(head, plan.records)
    assert head == immutable


@pytest.mark.parametrize('net_zero', [False, True])
def test_native_explicit_clear_between_add_and_delete_restores_original_presence(net_zero):
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    original = with_track()
    row = literal_row(params={'text': 'First', 'nullable': None, 'opaque': {'keep': False}})
    add = capture_clip_add(original, 'parent', row)
    added = execute(original, [add]).bundle
    ref = 'parent/clip:first-clip'
    clear = capture_clip_set_params(added, ref, clear=['nullable', 'missing'])
    cleared = execute(added, [clear]).bundle
    assert State(cleared).entity(ref)['params'] == {'text': 'First', 'opaque': {'keep': False}}
    delete = capture_clip_delete(cleared, ref)
    before, calls = (original, [add, clear, delete]) if net_zero else (added, [clear, delete])
    plan = execute(before, calls)
    cleared_writes = plan.records[-2]['writes']
    assert cleared_writes == [
        {'ref': ref, 'path': ['params', 'missing'], 'before': ABSENT, 'after': ABSENT},
        {'ref': ref, 'path': ['params', 'nullable'], 'before': present(None), 'after': ABSENT}]
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    accepted = accepted_boundaries(undone.records, plan.bundle, undone.bundle)
    assert inverse(undone.bundle, accepted).bundle == plan.bundle
