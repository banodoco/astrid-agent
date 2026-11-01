"""Native staged inverse and step guards over immutable accepted captures."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import (
    _collect_media,
    _timeline_media_view,
    _without_meta,
)
from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import ABSENT, CommandError, present
from astrid.sdk.timeline._commit_patch import effect_diff
from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline._step_undo import check_later_steps, select_inverse_records
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _manifest, bundle_from_document
from astrid.sdk.timeline.inverse import check_intervening
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import bundle, clip, inverse
from tests.sdk.test_p1_doc_patch_provider import _runtime_document_for_bundle

REF = 'parent/clip:direct'


def note(key, value, *, ref=REF):
    return {'schema_version': 1, 'op': 'clip.set-params', 'version': '1', 'select': ref,
            'args': {'values': {key: value}, 'clear': []}}


def original():
    result = bundle()
    clip(result)['params'] = {f'n{i}': 'A' for i in range(10)}
    result['parent']['config']['clips'] = copy.deepcopy(result['parent']['clips'])
    runtime_document = _runtime_document_for_bundle(result)
    media = (_collect_media(_timeline_media_view(runtime_document['parent_revision']['payload'],
                                                  include_parent_locations=True))
             | set().union(*(_collect_media(_without_meta(row['payload']))
                             for row in runtime_document['shot_revisions']))
             | set().union(*(_collect_media(_timeline_media_view(row['payload']))
                             for row in runtime_document['internal_timeline_revisions'])))
    runtime_document['dependency_manifest'] = _manifest(
        runtime_document['shot_revisions'], runtime_document['internal_timeline_revisions'],
        [{'media_id': media_id, 'content_digest': media_id} for media_id in media], [])
    return bundle_from_document(runtime_document)


def entry(before, plan, publication='p1', previous='p0', *, dependencies=None, groups=None):
    # This is accepted-history input to the inverse core, not a substitute for
    # Runtime authentication or R1's provider attribution/step persistence tests.
    groups = groups or [[i] for i in range(len(plan.records))]
    accepted = None
    if not any(record['command']['op'] == 'timeline.inverse' for record in plan.records):
        request = {
            'api_version': 1, 'project_id': before['project_id'],
            'timeline_id': before['timeline_id'],
            'base_revision_id': before['source_mapping']['_runtime_document']['parent_revision'][
                'revision_id'],
            'base_publication_id': previous, 'commands': plan.commands,
            'label': 'Accepted fixture', 'merge': 'fields', 'reads': [], 'dry_run': False,
            'source': {'via': 'fixture'}, 'rationale': 'accepted fixture boundary',
            'group_id': 'step-undo-v2',
            'steps': [{'id': f's{index}', 'label': 'Note', 'intent': '', 'tags': [],
                       'command_ordinals': ordinals, 'patch': []}
                      for index, ordinals in enumerate(groups)],
        }
        accepted = execute(before, plan.commands, commit_request=request,
                           evaluated_head={'revision_id': request['base_revision_id'],
                                           'publication_id': previous})
    if accepted is not None:
        records = copy.deepcopy(plan.records)
        steps = copy.deepcopy(accepted.steps)
    else:
        records = copy.deepcopy(plan.records)
        steps = []
        for index, ordinals in enumerate(groups):
            patch = (effect_diff(Closure(before), Closure(plan.bundle)) if any(
                record['command']['op'] == 'timeline.inverse' for record in records) else [])
            if not patch:
                for ordinal in ordinals:
                    for write in records[ordinal]['writes']:
                        change = {'op': 'remove' if write['after'] == ABSENT else 'set',
                                  'ref': write['ref'], 'path': write['path'], 'before': write['before']}
                        if write['after'] != ABSENT:
                            change['value'] = write['after']['value']
                        patch.append(change)
            steps.append({'id': f's{index}', 'label': 'Note', 'intent': '', 'tags': [],
                          'command_ordinals': ordinals, 'patch': patch,
                          'dependencies': (dependencies or {}).get(index, [])})
    for index, step in enumerate(steps):
        step['dependencies'] = (dependencies or {}).get(index, step.get('dependencies', []))
    return {'publication_id': publication, 'revision_id': 'rev-' + publication,
            'previous': {'publication_id': previous, 'revision_id': 'rev-' + previous},
            'intent': 'recorded', 'publisher': 'fixture-provider',
            'source': {'via': 'fixture'}, 'rationale': 'accepted fixture boundary',
            'receipt': {'receipt_id': publication, 'command_kind': 'timeline.commit',
                        'idempotency_key': 'fixture-' + publication,
                        'result': {'status': 'accepted', 'publication_id': publication}},
            '_fixture_authoritative_steps': copy.deepcopy(steps),
            'records': {'schema_version': 1, 'commands': records, 'steps': steps,
                        'label': 'Accepted fixture', 'merge_mode': 'fields',
                        'undo_of': plan.commands[0]['args'].get('publications', [])},
            'before': before, 'after': plan.bundle}


def accepted(entries):
    source_entries = []
    for item in entries:
        current = item
        authoritative = item.get('_fixture_authoritative_steps')
        steps = item.get('records', {}).get('steps')
        if (authoritative is not None and isinstance(steps, list)
                and len(steps) == len(authoritative)
                and all(current_step.get('id') == original_step.get('id')
                        and current_step.get('command_ordinals') == original_step.get('command_ordinals')
                        for current_step, original_step in zip(steps, authoritative))
                and any(current_step.get('patch') != original_step.get('patch')
                        for current_step, original_step in zip(steps, authoritative))):
            current = copy.deepcopy(item)
            current['records']['steps'] = copy.deepcopy(authoritative)
        source_entries.append(current)
    records = AcceptedRecords(source_entries,
                              [record for item in source_entries for record in item['records']['commands']],
                              lambda item: (item['before'], item['after']))
    return records


def selected(entries, steps=None, *, history=None):
    accepted_records = accepted(entries)
    records = select_inverse_records(accepted_records, steps)
    check_later_steps(records, accepted_records.entries if history is None else history)
    return records


def test_one_of_ten_notes_preserves_other_steps_and_redoes_exactly():
    before = original()
    plan = execute(before, [note(f'n{i}', 'B') for i in range(10)])
    item = entry(before, plan)
    snapshot = copy.deepcopy(item)
    records = selected([item], {'p1': ['s3']})
    undone = inverse(plan.bundle, records)
    expected = {f'n{i}': 'A' if i == 3 else 'B' for i in range(10)}
    assert clip(undone.bundle)['params'] == expected
    redone = inverse(undone.bundle, accepted([entry(plan.bundle, undone, 'p2', 'p1')]))
    assert redone.bundle == plan.bundle
    assert records.entries[0] is item
    assert item == snapshot


def test_whole_inverse_records_each_sequential_reversal_instead_of_coalescing():
    before = original()
    plan = execute(before, [note('n0', 'B'), note('n0', 'C'), note('n0', 'A')])
    records = selected([entry(before, plan)])
    undone = inverse(plan.bundle, records)
    writes = [write for write in undone.records[0]['writes'] if write['path'] == ['params', 'n0']]
    assert [(write['before']['value'], write['after']['value']) for write in writes] == [
        ('A', 'C'), ('C', 'B'), ('B', 'A')]
    boundaries = [binding['boundary'] for binding in undone.records[0]['bindings']
                  if binding['key'] == 'inverse.stage']
    assert [(boundary['step_id'], boundary['native_write_range']) for boundary in boundaries] == [
        ('s2', [0, 1]), ('s1', [1, 2]), ('s0', [2, 3])]
    assert undone.bundle == before
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle


def test_last_step_restores_its_own_input_not_commit_input():
    before = original()
    plan = execute(before, [note('n0', 'B'), note('n0', 'C')])
    undone = inverse(plan.bundle, selected([entry(before, plan)], {'p1': ['s1']}))
    assert clip(undone.bundle)['params']['n0'] == 'B'
    assert clip(before)['params']['n0'] == 'A'


def test_step_contains_contiguous_command_run_and_reverses_all_its_commands():
    before = original()
    plan = execute(before, [note('n0', 'B'), note('n0', 'C'), note('n1', 'B')])
    item = entry(before, plan, groups=[[0, 1], [2]])
    records = selected([item], {'p1': ['s0']})
    undone = inverse(plan.bundle, records)
    assert clip(undone.bundle)['params']['n0'] == 'A'
    assert clip(undone.bundle)['params']['n1'] == 'B'


@pytest.mark.parametrize('dependency', [
    {'kind': 'field', 'ref': REF, 'path': ['params', 'n0'], 'state': present('B')},
    {'kind': 'scope', 'ref': 'parent', 'path': []},
    {'kind': 'scope', 'ref': 'timeline', 'path': []},
])
def test_later_step_dependency_refuses_subset_but_all_steps_reverse(dependency):
    before = original()
    plan = execute(before, [note('n0', 'B'), note('n1', 'B')])
    item = entry(before, plan, dependencies={1: [dependency]})
    snapshot = copy.deepcopy(item)
    with pytest.raises(CommandError) as error:
        selected([item], {'p1': ['s0']})
    assert error.value.kind == 'inverse_conflict'
    undone = inverse(plan.bundle, selected([item]))
    assert undone.bundle == before
    assert item == snapshot


def test_inverse_execution_rechecks_surviving_steps_without_a_preflight_scan():
    before = original()
    plan = execute(before, [note('n0', 'B'), note('n1', 'B')])
    item = entry(before, plan, dependencies={1: [
        {'kind': 'field', 'ref': REF, 'path': ['params', 'n0'], 'state': present('B')}]})
    records = select_inverse_records(accepted([item]), {'p1': ['s0']})
    with pytest.raises(CommandError) as error:
        inverse(plan.bundle, records)
    assert error.value.kind == 'inverse_conflict'


def test_later_captured_read_blocks_whole_undo_even_if_its_write_is_disjoint(monkeypatch):
    from astrid.sdk.timeline import commands

    def summarize(ctx, command):
        value = ctx.read(REF, ['params', 'n0'])
        ctx.write(REF, ['params', 'summary'], 'Note: ' + value)

    operations = commands.operations()
    operations['test.summary'] = commands.Operation({'type': 'object'}, summarize, {})
    monkeypatch.setattr(commands, 'operations', lambda: operations)
    before = original()
    plan = execute(before, [note('n0', 'B')])
    later = execute(plan.bundle, [{'schema_version': 1, 'op': 'test.summary',
                                 'version': '1', 'select': None, 'args': {}}])
    with pytest.raises(CommandError) as error:
        inverse(later.bundle, plan.records, later.records)
    assert error.value.kind == 'inverse_conflict'
    assert clip(later.bundle)['params']['summary'] == 'Note: B'


def test_later_declared_read_in_another_publication_blocks_whole_undo():
    before = original()
    plan = execute(before, [note('n0', 'B')])
    later = execute(plan.bundle, [note('n1', 'B')])
    first = entry(before, plan)
    second = entry(plan.bundle, later, 'p2', 'p1', dependencies={0: [
        {'kind': 'field', 'ref': REF, 'path': ['params', 'n0'], 'state': present('B')}]})
    with pytest.raises(CommandError) as error:
        selected([first], history=[first, second])
    assert error.value.kind == 'inverse_conflict'


def test_aba_tail_refuses_even_when_the_current_guard_matches():
    before = original()
    plan = execute(before, [note('n0', 'B')])
    changed = execute(plan.bundle, [note('n0', 'C')])
    restored = execute(changed.bundle, [note('n0', 'B')])
    records = accepted([entry(before, plan)])
    assert clip(restored.bundle)['params']['n0'] == 'B'
    with pytest.raises(CommandError) as error:
        inverse(restored.bundle, records, [*changed.records, *restored.records])
    assert error.value.kind == 'intervening_write'


def test_later_nonselected_same_commit_aba_writes_refuse_subset():
    before = original()
    plan = execute(before, [note('n0', 'B'), note('n0', 'C'), note('n0', 'B')])
    with pytest.raises(CommandError) as error:
        selected([entry(before, plan)], {'p1': ['s0']})
    assert error.value.kind == 'intervening_write'


def test_disjoint_later_commit_survives_and_inverse_uses_actual_landing_head():
    before = original()
    ours = execute(before, [note('n0', 'B')])
    theirs = execute(before, [note('n1', 'C')])
    landed = replay(ours, theirs.bundle, intervening_records=theirs.records)
    item = entry(theirs.bundle, landed)
    undone = inverse(landed.bundle, selected([item]))
    assert undone.bundle == theirs.bundle
    later = execute(landed.bundle, [note('n2', 'D')])
    tail = entry(landed.bundle, later, 'p2', 'p1')
    undone = inverse(later.bundle, selected([item], history=[item, tail]), later.records)
    assert clip(undone.bundle)['params'] == {**clip(theirs.bundle)['params'], 'n2': 'D'}


def test_gap_before_newer_target_is_its_input_but_not_earlier_targets_tail():
    before = original()
    first = execute(before, [note('n0', 'B')])
    gap = execute(first.bundle, [note('n1', 'B')])
    last = execute(gap.bundle, [note('n1', 'C')])
    items = [entry(before, first), entry(first.bundle, gap, 'p2', 'p1'),
             entry(gap.bundle, last, 'p3', 'p2')]
    records = selected([items[0], items[2]], history=items)
    undone = inverse(last.bundle, records)
    assert clip(undone.bundle)['params']['n0'] == 'A'
    assert clip(undone.bundle)['params']['n1'] == 'B'
    conflicting = execute(first.bundle, [note('n0', 'C')])
    last = execute(conflicting.bundle, [note('n1', 'C')])
    items[1:] = [entry(first.bundle, conflicting, 'p2', 'p1'), entry(conflicting.bundle, last, 'p3', 'p2')]
    with pytest.raises(CommandError) as error:
        selected([items[0], items[2]], history=items)
    assert error.value.kind == 'intervening_write'


@pytest.mark.parametrize('steps', [{}, {'other': ['s0']}, {'p1': []}, {'p1': ['missing']},
                                  {'p1': ['s0', 's0']}, {'p1': None}, {'p1': [True]}])
def test_invalid_step_selectors_refuse(steps):
    before = original()
    plan = execute(before, [note('n0', 'B')])
    with pytest.raises(CommandError) as error:
        select_inverse_records(accepted([entry(before, plan)]), steps)
    assert error.value.kind == 'invalid_steps'


@pytest.mark.parametrize('corruption', ['missing-steps', 'partition', 'ordinals', 'mutated-command', 'legacy-tail'])
def test_missing_or_corrupt_authority_never_authorizes_partial_inverse(corruption):
    before = original()
    plan = execute(before, [note('n0', 'B'), note('n1', 'C')])
    item = entry(before, plan)
    records = accepted([item])
    if corruption == 'missing-steps':
        item['records'].pop('steps')
    elif corruption == 'partition':
        item['records']['steps'][1]['command_ordinals'] = [0]
    elif corruption == 'ordinals':
        item['records']['commands'][0]['ordinal'] = 2
    elif corruption == 'mutated-command':
        records = AcceptedRecords([item], copy.deepcopy(plan.records), records.load_boundary)
        records[0]['writes'][0]['after'] = present('forged')
    if corruption == 'legacy-tail':
        tail = {'publication_id': 'p2', 'revision_id': 'rev-p2', 'previous': {
            'publication_id': 'p1', 'revision_id': 'rev-p1'}, 'records': None}
        with pytest.raises(CommandError) as error:
            selected([item], history=[item, tail])
    else:
        with pytest.raises(CommandError) as error:
            select_inverse_records(records, {'p1': ['s0']})
    assert error.value.kind == 'history_unavailable'


def test_step_patch_is_never_executed_as_inverse_instructions():
    before = original()
    plan = execute(before, [note('n0', 'B')])
    item = entry(before, plan)
    item['records']['steps'][0]['patch'] = [{'op': 'remove', 'ref': 'untrusted', 'path': []}]
    assert inverse(plan.bundle, selected([item])).bundle == before


def test_semantic_creation_reversal_preserves_unselected_root_and_redoes():
    before = original()
    calls = [{'schema_version': 1, 'op': 'occurrence.create', 'version': '1', 'select': 'parent',
              'args': {'occurrence_id': f'new-{i}', 'shot_id': f'shot-{i}', 'name': f'Shot {i}',
                       'track': 'picture', 'on': 30 + i * 3, 'duration': 2}}
             for i in range(2)]
    plan = execute(before, calls)
    item = entry(before, plan)
    undone = inverse(plan.bundle, selected([item], {'p1': ['s1']}))
    assert 'shot-0' in undone.bundle['shots']
    assert 'shot-1' not in undone.bundle['shots']
    assert [row['occurrence_id'] for row in undone.bundle['placements']] == [
        *[row['occurrence_id'] for row in before['placements']], 'new-0']
    redone = inverse(undone.bundle, selected([entry(plan.bundle, undone, 'p2', 'p1')]))
    assert redone.bundle == plan.bundle
    assert inverse(plan.bundle, selected([item])).bundle == before


def test_partial_semantic_deletion_restores_source_and_order_from_accepted_boundary():
    before = original()
    plan = execute(before, [
        {'schema_version': 1, 'op': 'occurrence.delete', 'version': '1',
         'select': 'occurrence:occ-1', 'args': {}}, note('n0', 'B')])
    item = entry(before, plan)
    undone = inverse(plan.bundle, selected([item], {'p1': ['s0']}))
    assert undone.bundle['placements'] == before['placements']
    assert undone.bundle['shots'] == before['shots']
    assert clip(undone.bundle)['params']['n0'] == 'B'
    assert item['after'] == plan.bundle


def test_collection_read_guards_shape_without_reading_item_properties():
    record = {'writes': [], 'reads': [], 'selections': [], 'created': []}
    dependency = {'kind': 'collection', 'ref': 'parent', 'path': ['clips'],
                  'members': ['a', 'b'], 'order': ['a', 'b']}
    rows = [{'id': 'a', 'name': 'A'}, {'id': 'b', 'name': 'B'}]
    write = {'ref': 'parent', 'path': ['clips'], 'before': present(rows),
             'after': present([{**rows[0], 'name': 'C'}, rows[1]])}
    check_intervening([record], [{'writes': [write]}], dependencies=[dependency])
    write['after'] = present(list(reversed(rows)))
    with pytest.raises(CommandError) as error:
        check_intervening([record], [{'writes': [write]}], dependencies=[dependency])
    assert error.value.kind == 'intervening_write'
    check_intervening([record], [{'writes': [write]}], dependencies=[{**dependency, 'order': None}])


def test_normalization_seam_uses_source_bound_physical_roots():
    target = {'ref': 'internal:film', 'path': ['output', 'fps'], 'before': present(30), 'after': present(24)}
    record = {'writes': [target], 'reads': [], 'selections': [], 'created': []}
    sibling = {**target, 'ref': 'internal-revision:ib1'}
    successor = {**target, 'ref': 'internal-revision:ia2'}
    bindings = {id(target): 'internal-revision:ia2', id(sibling): 'internal-revision:ib1',
                id(successor): 'internal-revision:ia2'}

    def resolved(capture, ref, path):
        return bindings[id(capture)], tuple(path)

    check_intervening([record], [{'writes': [sibling]}], address_resolver=resolved)
    with pytest.raises(CommandError) as error:
        check_intervening([record], [{'writes': [successor]}], address_resolver=resolved)
    assert error.value.kind == 'intervening_write'


def test_compiler_effects_participate_in_later_write_and_read_scans():
    derived = {'ref': REF, 'path': ['at'], 'before': present(0), 'after': present(1)}
    target = {'writes': [], 'reads': [], 'selections': [], 'created': [], 'derived_writes': [derived]}
    consumer = {'writes': [], 'reads': [{'ref': REF, 'path': ['at'], 'state': present(1)}]}
    with pytest.raises(CommandError) as error:
        check_intervening([target], [consumer], include_dependents=True)
    assert error.value.kind == 'inverse_conflict'
    tail = {'writes': [], 'derived_writes': [{**derived, 'before': present(1), 'after': present(2)},
                                          {**derived, 'before': present(2), 'after': present(1)}]}
    with pytest.raises(CommandError) as error:
        check_intervening([target], [tail])
    assert error.value.kind == 'intervening_write'


def test_physical_history_requires_provider_normalization_before_execution():
    before = original()
    plan = execute(before, [note('n0', 'B')])
    plan.records[0]['bindings'].append({'key': 'commit.read', 'ref': 'internal:film',
                                      'physical_ref': 'internal-revision:ia1'})
    records = selected([entry(before, plan)])
    with pytest.raises(CommandError) as error:
        inverse(plan.bundle, records)
    assert error.value.kind == 'history_unavailable'


def test_bound_owner_replacement_is_a_conflict_even_with_equal_child_bytes():
    write = {'ref': 'internal:film', 'path': ['output', 'fps'],
             'before': present(30), 'after': present(24)}
    binding = {'ref': 'internal:film', 'physical_ref': 'internal-revision:ia1',
               'owners': ['occurrence:a/shot']}
    record = {'writes': [write], 'reads': [], 'selections': [], 'created': [], 'bindings': [binding]}
    edge = {'ref': 'occurrence:a', 'path': ['shot_id'], 'before': present('a'), 'after': present('b')}

    def resolved(capture, ref, path):
        if capture is write:
            return 'internal-revision:ia1', tuple(path)
        return 'parent', ('occurrences', ('id', 'a'), *path)

    with pytest.raises(CommandError) as error:
        check_intervening([record], [{'writes': [edge]}], address_resolver=resolved)
    assert error.value.kind == 'intervening_write'


def test_mutating_a_selected_view_does_not_create_inverse_authority():
    before = original()
    plan = execute(before, [note('n0', 'B'), note('n1', 'B')])
    records = selected([entry(before, plan)], {'p1': ['s0']})
    records.append(plan.records[1])
    with pytest.raises(CommandError) as error:
        inverse(plan.bundle, records)
    assert error.value.kind == 'history_unavailable'
