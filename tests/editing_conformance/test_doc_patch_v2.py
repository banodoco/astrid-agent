"""Executable v2 patch cases through the existing command engine and provider."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import ABSENT, CommandError, present
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.commit import (
    _digest,
    bundle_from_document,
    create_runtime_commit_service,
    document_from_bundle,
)
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    inverse,
)
from tests.sdk.test_timeline_commit_provider import (
    runtime_abi as runtime_abi,
)
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def change(path, before, value=None, *, op='set', ref='parent', **extra):
    result = {'op': op, 'ref': ref, 'path': path, 'before': copy.deepcopy(before), **extra}
    if op in ('set', 'list.insert'):
        result['value'] = copy.deepcopy(value)
    return result


def patch(*changes, created=None):
    result = {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
              'args': {'changes': list(changes)}}
    if created is not None:
        result['created'] = created
    return result


def submit(realm, *calls, base=None, **controls):
    context = realm.context(calls, base=base)
    context['request'].update(controls)
    context['invocation']['body'] = copy.deepcopy(context['request'])
    context['request_hash'] = _digest(context['invocation'])
    return create_runtime_commit_service().commit(context, DetachedPort(realm, context))


def bundle():
    return bundle_from_document(document_fixture())


def payload(plan):
    return document_from_bundle(plan.bundle)['parent_revision']['payload']


def step(identity, ordinal, call, *, reads=None):
    result = {'id': identity, 'label': identity, 'intent': '', 'tags': [], 'command_ordinals': [ordinal],
              'patch': copy.deepcopy(call['args']['changes']) if call['op'] == 'doc.patch' else []}
    if reads is not None:
        result['reads'] = reads
    return result


def test_tagged_null_explicit_clear_and_intermediate_equal_writes():
    call = patch(change(['unknown', 'new'], ABSENT, None),
                 change(['unknown', 'new'], present(None), op='remove'))
    original = bundle()
    plan = execute(original, [call], commit_request={'reads': []})
    assert payload(plan) == document_fixture()['parent_revision']['payload']
    assert [write['after'] for write in plan.records[0]['writes']] == [present(None), ABSENT]
    assert len(plan.steps[0]['patch']) == 2
    assert original == bundle()


@pytest.mark.parametrize('value', [False, 0, None, [], {}])
def test_literal_object_keys_and_json_values(value):
    plan = execute(bundle(), [patch(change(['unknown', 'literal.with:punctuation'], ABSENT, value))])
    assert payload(plan)['unknown']['literal.with:punctuation'] == value


@pytest.mark.parametrize('bad', [
    change(['name'], present('wrong'), 'B'),
    change(['unknown', 'missing'], ABSENT, op='remove'),
    change(['unknown', 'missing', 'child'], ABSENT, 'B'),
    change(['clips', '0', 'hold'], present(2), 3),
    change(['clips', 'id:clip-a', 'id'], present('clip-a'), 'other'),
    change(['clips', 'id:new'], ABSENT, {'id': 'new'}),
    change(['clips', 'id:clip-a'], present(document_fixture()['parent_revision']['payload']['clips'][0]), op='remove'),
])
def test_bad_operands_refuse_atomically(bad):
    original = bundle()
    with pytest.raises(CommandError):
        execute(original, [patch(change(['name'], present('Renamed Film'), 'B'), bad)])
    assert original == bundle()


def test_idless_lists_are_atomic_even_when_rows_contain_an_id():
    original = bundle()
    original['parent']['unknown']['rows'] = [{'id': 'a', 'name': 'A'}]
    with pytest.raises(CommandError, match='schema-defined identity') as error:
        execute(original, [patch(change(['unknown', 'rows', 'id:a', 'name'], present('A'), 'B'))])
    assert error.value.kind == 'idless_list_address'
    plan = execute(original, [patch(change(['unknown', 'rows'], present([{'id': 'a', 'name': 'A'}]), []))])
    assert payload(plan)['unknown']['rows'] == []


def test_ordered_insert_move_remove_and_created_claim():
    track = {'id': 'audio', 'kind': 'audio'}
    original = bundle()
    call = patch(change(['config', 'tracks'], ABSENT, track, op='list.insert', id='audio',
                        order_before=['picture'], after_id='picture'),
                 change(['config', 'tracks'], present(track), op='list.move', id='audio',
                        order_before=['picture', 'audio'], after_id=None),
                 change(['config', 'tracks'], present(track), op='list.remove', id='audio',
                        order_before=['audio', 'picture']), created=['parent/track:audio'])
    plan = execute(original, [call], commit_request={'reads': []})
    assert payload(plan) == document_fixture()['parent_revision']['payload']
    assert plan.records[0]['created'] == [{'ref': 'parent/track:audio', 'kind': 'track', 'owner': 'parent'}]
    assert len(plan.steps[0]['patch']) == 3
    with pytest.raises(CommandError, match='declare every created identity'):
        execute(original, [patch(call['args']['changes'][0])])


@pytest.mark.parametrize('damage', ['order', 'anchor', 'item'])
def test_list_guards_and_anchors(damage):
    value = {'id': 'audio', 'kind': 'audio'}
    item = change(['config', 'tracks'], ABSENT, value, op='list.insert', id='audio',
                  order_before=['picture'], after_id=None)
    if damage == 'order':
        item['order_before'] = []
    elif damage == 'anchor':
        item['after_id'] = 'missing'
    else:
        item['value']['id'] = 'different'
    with pytest.raises(CommandError):
        execute(bundle(), [patch(item, created=['parent/track:audio'])])


def test_mirrored_clips_have_one_authored_identity():
    plan = execute(bundle(), [patch(change(['config', 'clips', 'id:clip-a', 'hold'], present(2), 3))])
    parent = payload(plan)
    assert parent['clips'] == parent['config']['clips']
    assert parent['clips'][0]['hold'] == 3


@pytest.mark.parametrize('ref', ['internal:chapter-1', 'internal-revision:internal-1'])
def test_shared_physical_child_edit_updates_every_owner(ref):
    plan = execute(bundle(), [patch(change(['unknown'], present([1, 2]), [3], ref=ref))],
                   commit_request={'reads': []})
    document = document_from_bundle(plan.bundle)
    assert len(document['internal_timeline_revisions']) == 1
    assert document['internal_timeline_revisions'][0]['payload']['unknown'] == [3]
    assert len(plan.records[0]['writes']) == 2
    effects = plan.steps[0]['patch']
    assert any(effect['op'] == 'remove' and effect['ref'] == 'internal-revision:internal-1' for effect in effects)
    assert any(effect['op'] == 'set' and effect['ref'].startswith('internal-revision:') for effect in effects)


@pytest.mark.parametrize('ref', ['shot:chapter-shot', 'shot-revision:shot-1'])
def test_shot_alias_and_revision_edit_one_shared_image(ref):
    plan = execute(bundle(), [patch(change(['name'], present('Chapter One'), 'New Chapter', ref=ref))])
    shots = document_from_bundle(plan.bundle)['shot_revisions']
    assert len(shots) == 1
    assert all(image['payload']['name'] == 'New Chapter' for image in shots)


def test_ambiguous_entity_alias_refuses_and_qualified_revision_selects_one():
    doc = document_fixture()
    second_internal = copy.deepcopy(doc['internal_timeline_revisions'][0])
    second_internal['revision_id'] = 'internal-2'
    second_shot = copy.deepcopy(doc['shot_revisions'][0])
    second_shot.update(revision_id='shot-2', internal_timeline_revision_id='internal-2')
    second_shot['payload']['internal_timeline_revision_id'] = 'internal-2'
    second_shot['content_digest'] = _digest(second_shot['payload'])
    doc['internal_timeline_revisions'].append(second_internal)
    doc['shot_revisions'].append(second_shot)
    doc['parent_revision']['payload']['occurrences'][1]['shot_revision_id'] = 'shot-2'
    doc['parent_revision']['content_digest'] = _digest(doc['parent_revision']['payload'])
    from astrid.sdk.timeline.commit import _manifest
    doc['dependency_manifest'] = _manifest(doc['shot_revisions'], doc['internal_timeline_revisions'],
                                           doc['dependency_manifest']['media'], [])
    original = bundle_from_document(doc)
    for ref in ('internal:chapter-1', 'shot:chapter-shot'):
        with pytest.raises(CommandError) as error:
            execute(original, [patch(change(['name'], ABSENT, 'B', ref=ref))])
        assert error.value.kind == 'ambiguous_ref'
    plan = execute(original, [patch(change(['unknown'], present([1, 2]), [3], ref='internal-revision:internal-1'))])
    images = document_from_bundle(plan.bundle)['internal_timeline_revisions']
    assert sorted(image['payload']['unknown'] for image in images) == [[1, 2], [3]]


@pytest.mark.parametrize('bad', [
    change([], ABSENT, {}, ref='internal-revision:missing'),
    change([], present({}), op='remove', ref='shot-revision:missing'),
    change(['internal_timeline_revision_id'], present('internal-1'), 'forged', ref='shot-revision:shot-1'),
    change(['occurrences', 'occurrence_id:occ-a', 'shot_revision_id'], present('shot-1'), 'forged'),
])
def test_managed_writes_refuse_before_looking_up_absent_refs(bad):
    with pytest.raises(CommandError) as error:
        execute(bundle(), [patch(bad)])
    assert error.value.kind == 'managed_closure_write'


def test_ancestor_replacement_cannot_hide_managed_edges_or_conflicting_mirrors():
    original = document_fixture()['parent_revision']['payload']
    for mutate in ('edge', 'mirror'):
        replacement = copy.deepcopy(original)
        if mutate == 'edge':
            replacement['occurrences'][0]['shot_revision_id'] = 'forged'
        else:
            replacement['clips'][0]['hold'] = 9
        with pytest.raises(CommandError):
            execute(bundle(), [patch(change([], present(original), replacement))])


def test_provider_merges_disjoint_fields_and_inverse_uses_actual_landing_head():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['unknown', 'theirs'], ABSENT, 'kept')), reads=[])
    landed = submit(realm, patch(change(['name'], present('Renamed Film'), 'B')), reads=[], base=base)
    assert realm.prepared[-1]['expected_head'] != base
    restored = realm.call([inverse([landed['receipt']['receipt_id']])])
    parent = restored['data']['canonical_document']['parent_revision']['payload']
    assert parent['name'] == 'Renamed Film'
    assert parent['unknown']['theirs'] == 'kept'


@pytest.mark.parametrize('atomic', ['omitted', 'explicit'])
def test_atomic_fallback_and_explicit_atomic_refuse_disjoint_same_scope(atomic):
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['unknown', 'theirs'], ABSENT, True)), reads=[])
    controls = {} if atomic == 'omitted' else {'merge': 'atomic', 'reads': []}
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(change(['name'], present('Renamed Film'), 'B')), base=base, **controls)
    assert error.value.failure['details']['reason'] == 'atomic_scope_changed'


def test_history_aba_is_a_conflict_even_with_equal_final_value():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['name'], present('Renamed Film'), 'B')), reads=[])
    submit(realm, patch(change(['name'], present('B'), 'Renamed Film')), reads=[])
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(change(['name'], present('Renamed Film'), 'C')), reads=[], base=base)
    assert error.value.failure['details']['reason'] == 'value_conflict'


def test_sequential_step_reads_are_checked_and_persisted():
    realm = DetachedRealm()
    first = patch(change(['name'], present('Renamed Film'), 'B'))
    second = patch(change(['unknown', 'summary'], ABSENT, 'Based on B'))
    read = {'kind': 'field', 'ref': 'parent', 'path': ['name'], 'state': present('B')}
    submit(realm, first, second, reads=[], label='Two notes',
           steps=[step('first', 0, first), step('second', 1, second, reads=[read])])
    steps = realm.prepared[-1]['records']['steps']
    assert read in steps[1]['dependencies']
    assert realm.prepared[-1]['records']['label'] == 'Two notes'
    assert steps[0]['patch'] == first['args']['changes']
    read['state'] = present('Renamed Film')
    with pytest.raises(CommitRejected) as error:
        submit(DetachedRealm(), first, second, reads=[],
               steps=[step('first', 0, first), step('second', 1, second, reads=[read])])
    assert error.value.failure['details']['reason'] == 'previous_value_mismatch'


@pytest.mark.parametrize('merge', ['fields', 'atomic'])
def test_mixed_native_capture_keeps_inherited_reads_at_original_boundaries(merge):
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    move = realm.move()
    note = patch(change(['meta'], ABSENT, {'org.example': {'note': 'moved'}}))
    meta = {'kind': 'field', 'ref': 'parent', 'path': ['meta'], 'state': ABSENT}
    initial = {'kind': 'field', 'ref': 'parent',
               'path': ['config', 'clips', 'id:clip-a', 'at'], 'state': present(1)}
    moved = {**initial, 'state': present(3.003)}
    controls = {'label': 'Move and note', 'merge': merge, 'reads': [meta, initial],
                'steps': [step('move', 0, move, reads=[meta]), step('note', 1, note, reads=[moved])]}
    original = copy.deepcopy([move, note, controls])

    preview = submit(realm, move, note, dry_run=True, **controls)
    assert preview['data']['status'] == 'preview' and preview['receipt'] is None
    assert realm.head == base and not realm.accepted
    accepted = submit(realm, move, note, **controls)
    assert accepted['data']['status'] == 'accepted' and accepted['receipt']
    assert accepted['data']['commands'][0] == move
    assert preview['data']['canonical_document'] == accepted['data']['canonical_document']
    records = realm.entries[-1]['records']
    assert records['merge_mode'] == merge and records['label'] == controls['label']
    for submitted, authoritative in zip(controls['steps'], records['steps']):
        assert all(read in authoritative['dependencies'] for read in controls['reads'] + submitted.get('reads', []))
        assert authoritative['patch']
    assert moved in records['steps'][1]['dependencies']
    assert {'ref': 'parent', 'path': ['meta'], 'state': ABSENT} in records['commands'][0]['reads']
    assert records['commands'][0]['command']['reads'] == move['reads']
    assert all(read in records['commands'][0]['reads'] for read in move['reads'])
    assert {'kind': 'field', 'ref': 'parent', 'path': ['config', 'output', 'fps'],
            'state': present({'numerator': 30000, 'denominator': 1001})} in records['steps'][0]['dependencies']
    bindings = [binding for record in records['commands'] for binding in record['bindings']
                if binding['key'] == 'commit.read']
    assert [(binding['boundary']['phase'], binding['boundary']['command_ordinal'],
             binding['dependency']) for binding in bindings] == [
                 ('batch-read', 0, meta), ('batch-read', 0, initial), ('input', 0, meta), ('input', 1, moved)]
    assert all(binding['boundary']['evaluated_head'] == base for binding in bindings)
    assert accepted['data']['canonical_document']['parent_revision']['payload']['meta'] == note['args']['changes'][0]['value']
    assert [move, note, controls] == original


@pytest.mark.parametrize('merge', ['fields', 'atomic'])
@pytest.mark.parametrize('damage', ['missing', 'inherited'])
def test_inherited_reads_do_not_authorize_forged_native_capture(merge, damage):
    realm = DetachedRealm()
    move = realm.move()
    read = {'kind': 'field', 'ref': 'parent', 'path': ['meta'], 'state': ABSENT}
    if damage == 'missing':
        move['reads'] = []
    else:
        move['reads'].append({key: value for key, value in read.items() if key != 'kind'})
    note = patch(change(['meta'], ABSENT, {'org.example': {'note': 'moved'}}))
    with pytest.raises(CommitRejected) as error:
        submit(realm, move, note, merge=merge, reads=[read],
               steps=[step('move', 0, move, reads=[read]), step('note', 1, note)])
    assert error.value.failure['details']['reason'] == 'capture_mismatch'
    assert realm.head['publication_id'] == 'bootstrap' and not realm.prepared


@pytest.mark.parametrize('merge', ['fields', 'atomic'])
def test_mixed_native_inherited_reads_replay_at_landing_head(merge):
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    move = realm.move()
    note = patch(change(['meta'], ABSENT, {'org.example': {'note': 'moved'}}))
    read = {'kind': 'field', 'ref': 'parent', 'path': ['meta'], 'state': ABSENT}
    controls = {'merge': merge, 'reads': [read],
                'steps': [step('move', 0, move, reads=[read]), step('note', 1, note)]}
    submit(realm, patch(change(['unknown', 'theirs'], ABSENT, 'kept')), reads=[])
    head = copy.deepcopy(realm.head)
    if merge == 'atomic':
        with pytest.raises(CommitRejected) as error:
            submit(realm, move, note, base=base, **controls)
        assert error.value.failure['details']['reason'] == 'atomic_scope_changed'
        assert realm.head == head and len(realm.accepted) == 1
    else:
        result = submit(realm, move, note, base=base, **controls)
        assert result['data']['rebased_onto'] == head and result['receipt']
        assert result['data']['commands'][0] == move
        assert result['data']['canonical_document']['parent_revision']['payload']['unknown']['theirs'] == 'kept'
        bindings = realm.entries[-1]['records']['commands'][0]['bindings']
        assert all(binding['boundary']['evaluated_head'] == head
                   for binding in bindings if binding['key'] == 'commit.read')


@pytest.mark.parametrize('merge', ['fields', 'atomic'])
@pytest.mark.parametrize('declaration', ['request', 'step'])
@pytest.mark.parametrize('aba', [False, True])
def test_inherited_native_read_conflicts_with_later_dependency_writes(merge, declaration, aba):
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    move = realm.move()
    note = patch(change(['meta'], ABSENT, {'org.example': {'note': 'moved'}}))
    read = {'kind': 'field', 'ref': 'parent', 'path': ['unknown', 'consulted'], 'state': ABSENT}
    controls = {'merge': merge, 'reads': [read] if declaration == 'request' else [],
                'steps': [step('move', 0, move, reads=[read] if declaration == 'step' else []),
                          step('note', 1, note)]}
    submit(realm, patch(change(read['path'], ABSENT, True)), reads=[])
    if aba:
        submit(realm, patch(change(read['path'], present(True), op='remove')), reads=[])
    head, entries = copy.deepcopy(realm.head), copy.deepcopy(realm.entries)
    with pytest.raises(CommitRejected) as error:
        submit(realm, move, note, base=base, **controls)
    assert error.value.failure['http_status'] == 409
    assert error.value.failure['details']['status'] == 'conflict'
    assert error.value.failure['details']['reason'] == (
        'atomic_scope_changed' if merge == 'atomic' else 'value_conflict')
    assert realm.head == head and realm.entries == entries


@pytest.mark.parametrize('mutation', ['missing_value', 'extra', 'numeric_path', 'nan', 'tag_extra'])
def test_invalid_wire_shapes_are_deterministic(mutation):
    call = patch(change(['name'], present('Renamed Film'), 'B'))
    item = call['args']['changes'][0]
    if mutation == 'missing_value':
        del item['value']
    elif mutation == 'extra':
        item['force'] = True
    elif mutation == 'numeric_path':
        item['path'] = [0]
    elif mutation == 'nan':
        item['value'] = float('nan')
    else:
        item['before']['forged'] = 1
    with pytest.raises(CommandError):
        validate_command(call)


def test_real_word_compiler_effects_belong_to_anchor_step_before_unrelated_note():
    from tests.editing_conformance.test_native_word_anchor_vectors import fixture, vector

    original, _, _ = fixture()
    anchor = vector('a')['command']
    note = patch(change(['unknown', 'note'], ABSENT, 'unrelated'))
    plan = execute(original, [anchor, note], commit_request={
        'reads': [], 'steps': [step('anchor', 0, anchor), step('note', 1, note)]})
    first, second = plan.steps
    assert any(change['path'][-1:] == ['at'] for change in first['patch'])
    assert all(change['path'][-1:] != ['at'] for change in second['patch'])
    timings = [effect for record in plan.records for effect in record['derived_writes'] if effect['path'] == ['at']]
    assert timings and all(effect['cause'] == 0 for effect in timings)


def test_config_ancestor_updates_required_clip_mirror():
    original = bundle()
    config = copy.deepcopy(original['parent']['config'])
    config['clips'][0]['hold'] = 3
    result = execute(original, [patch(change(['config'], present(original['parent']['config']), config))])
    assert payload(result)['clips'] == config['clips']
    assert payload(result)['config'] == config


def test_ordered_read_detects_shape_aba_and_membership_read_ignores_reorder():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    rows = realm.documents['parent-1']['parent_revision']['payload']['clips']
    submit(realm, patch(change(['clips'], present(rows), list(reversed(rows)))), reads=[])
    read = {'kind': 'collection', 'ref': 'parent', 'path': ['clips'],
            'members': ['clip-a', 'clip-b'], 'order': None}
    submit(realm, patch(change(['name'], present('Renamed Film'), 'B')), reads=[read], base=base)
    read['order'] = ['clip-a', 'clip-b']
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(change(['unknown', 'new'], ABSENT, 'B')), reads=[read], base=base)
    assert error.value.failure['details']['reason'] == 'selection_changed'


def test_list_move_merges_unread_sibling_item_field():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['clips', 'id:clip-a', 'hold'], present(2), 3)), reads=[])
    rows = realm.documents['parent-1']['parent_revision']['payload']['clips']
    submit(realm, patch(change(['clips'], present(rows[1]), op='list.move', id='clip-b',
                              order_before=['clip-a', 'clip-b'], after_id=None)), reads=[], base=base)
    parent = realm.documents[realm.head['revision_id']]['parent_revision']['payload']
    assert [row['id'] for row in parent['clips']] == ['clip-b', 'clip-a']
    assert parent['clips'][1]['hold'] == 3


@pytest.mark.parametrize('merge', ['fields', 'atomic'])
@pytest.mark.parametrize('declared_read', [False, True])
@pytest.mark.parametrize(('operation', 'reason'), [
    ('list.remove', 'selection_changed'),
    ('list.move', 'selection_changed'),
    ('set', 'value_conflict'),
])
def test_list_move_conflict_precedence(operation, reason, declared_read, merge):
    # P1 edcd1b2b uses the byte-identical canonical_clip_trim.json fixture.
    fixture = Path(__file__).parent / 'fixtures' / 'recipe_m2_clip_trim.json'
    realm = DetachedRealm(json.loads(fixture.read_text(encoding='utf-8'))['input'])
    original_head = copy.deepcopy(realm.head)
    row = realm.documents['parent-1']['parent_revision']['payload']['clips'][1]
    move = change(['clips'], present(row), op='list.move', id='clip-b',
                  order_before=['clip-a', 'clip-b'], after_id=None)
    intervening = copy.deepcopy(move)
    intervening['op'] = operation
    if operation == 'list.remove':
        del intervening['after_id']
    elif operation == 'set':
        intervening = change(['clips', 'id:clip-b', 'hold'], present(2), 3)
    submit(realm, patch(intervening), merge='fields', reads=[])
    head_after_intervening = copy.deepcopy(realm.head)
    entries_after_intervening = copy.deepcopy(realm.entries)
    documents_after_intervening = copy.deepcopy(realm.documents)
    reads = [{'kind': 'collection', 'ref': 'parent', 'path': ['clips'],
              'members': ['clip-a', 'clip-b'], 'order': ['clip-a', 'clip-b']}] if declared_read else []

    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(move), merge=merge, reads=reads, base=original_head)

    assert error.value.failure['details']['reason'] == (
        'atomic_scope_changed' if merge == 'atomic' else reason)
    assert realm.head == head_after_intervening
    assert realm.entries == entries_after_intervening
    assert realm.documents == documents_after_intervening


def test_field_edit_merges_intervening_sibling_order_change():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    row = realm.documents['parent-1']['parent_revision']['payload']['clips'][1]
    submit(realm, patch(change(['clips'], present(row), op='list.move', id='clip-b',
                              order_before=['clip-a', 'clip-b'], after_id=None)), reads=[])
    submit(realm, patch(change(['clips', 'id:clip-a', 'hold'], present(2), 3)), reads=[], base=base)
    assert realm.documents[realm.head['revision_id']]['parent_revision']['payload']['clips'][1]['hold'] == 3


def test_patch_after_semantic_created_root_uses_sequential_materialized_boundary():
    create = {'schema_version': 1, 'op': 'occurrence.create', 'version': '1', 'select': 'parent',
              'args': {'occurrence_id': 'new-occ', 'shot_id': 'new-shot', 'name': 'New',
                       'track': 'picture', 'on': 25, 'duration': 5}}
    # One semantic allocation is addressable before the following patch.
    edit = patch(change(['name'], present('New'), 'Edited', ref='shot:new-shot'))
    plan = execute(bundle(), [create, edit], commit_request={
        'reads': [], 'steps': [step('create', 0, create), step('edit', 1, edit)]})
    document = document_from_bundle(plan.bundle)
    assert any(image['payload']['name'] == 'Edited' for image in document['shot_revisions'])
    assert any(effect['path'] == [] and effect['before'] == ABSENT for effect in plan.steps[0]['patch'])
    # A read before creation cannot be satisfied by the future root.
    with pytest.raises(CommandError) as error:
        execute(bundle(), [create, edit], commit_request={'reads': [
            {'kind': 'scope', 'ref': 'shot:new-shot', 'path': []}]})
    assert error.value.kind == 'outside_closure'


def test_two_semantic_children_with_same_timeline_alias_refuse_atomically():
    def create(identity):
        return {'schema_version': 1, 'op': 'occurrence.create', 'version': '1', 'select': 'parent',
                'args': {'occurrence_id': 'occ-' + identity, 'shot_id': 'shot-' + identity,
                         'name': identity, 'track': 'picture', 'on': 25, 'duration': 5}}
    edit = patch(change(['unknown'], ABSENT, True, ref='internal:film'))
    original = bundle()
    with pytest.raises(CommandError) as error:
        execute(original, [create('one'), create('two'), edit])
    assert error.value.kind == 'ambiguous_ref'
    assert original == bundle()


def test_rebase_follows_shared_physical_successor_for_disjoint_child_fields():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['unknown'], present([1, 2]), [3], ref='internal:chapter-1')), reads=[])
    submit(realm, patch(change(['source_window'], present({'start': 4, 'end': 9}), {'start': 4, 'end': 8},
                              ref='internal-revision:internal-1')), reads=[], base=base)
    images = realm.documents[realm.head['revision_id']]['internal_timeline_revisions']
    assert len(images) == 1
    assert images[0]['payload']['unknown'] == [3]
    assert images[0]['payload']['source_window'] == {'start': 4, 'end': 8}


@pytest.mark.parametrize('damage', [
    'missing', 'digest', 'owner', 'edge_before', 'edge_after', 'boundary', 'unrelated', 'effect_before', 'effects_missing',
])
def test_rebase_requires_source_bound_materialization_and_exact_managed_edges(monkeypatch, damage):
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['unknown'], present([1, 2]), [3], ref='internal:chapter-1')), reads=[])
    head, accepted = copy.deepcopy(realm.head), len(realm.accepted)
    history = DetachedPort.history

    def altered_history(port, *args, **kwargs):
        page = history(port, *args, **kwargs)
        for entry in page['items']:
            if damage == 'effects_missing':
                entry['records']['steps'][0].pop('patch')
            elif damage == 'effect_before':
                effect = next(effect for effect in entry['records']['steps'][0]['patch']
                              if effect['ref'] == 'parent' and effect['path'][-1] == 'shot_revision_id')
                effect['before'] = present('unrelated-shot')
            for record in entry['records']['commands']:
                if damage == 'missing':
                    record['bindings'] = [b for b in record['bindings'] if b['key'] != 'commit.materialization']
                for binding in record['bindings']:
                    if binding['key'] != 'commit.materialization':
                        continue
                    if damage == 'digest':
                        binding['content_digest'] = 'sha256:' + '0' * 64
                    elif damage in ('owner', 'edge_before', 'edge_after'):
                        binding[damage] = 'unrelated'
                    elif damage == 'boundary':
                        binding['boundary']['evaluated_head'] = head
        return page

    monkeypatch.setattr(DetachedPort, 'history', altered_history)
    if damage == 'unrelated':
        # An authorized closure may replace pins with byte-equal, unrelated
        # images. The earlier accepted source proof does not authorize them.
        document = realm.documents[head['revision_id']]
        child, shot = document['internal_timeline_revisions'][0], document['shot_revisions'][0]
        child['revision_id'] = 'unrelated-child'
        shot.update(revision_id='unrelated-shot', internal_timeline_revision_id=child['revision_id'])
        shot['payload']['internal_timeline_revision_id'] = child['revision_id']
        shot['content_digest'] = _digest(shot['payload'])
        for row in document['parent_revision']['payload']['occurrences']:
            row['shot_revision_id'] = shot['revision_id']
        document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
        from astrid.sdk.timeline.commit import _manifest
        document['dependency_manifest'] = _manifest([shot], [child], document['dependency_manifest']['media'], [])
    document = copy.deepcopy(realm.documents[head['revision_id']])
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(change(['source_window'], present({'start': 4, 'end': 9}), {'start': 4, 'end': 8},
                                  ref='internal-revision:internal-1')), reads=[], base=base)
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert realm.head == head and len(realm.accepted) == accepted
    assert realm.documents[head['revision_id']] == document


@pytest.mark.parametrize('grouped', [False, True])
def test_authenticated_successor_chain_lands_on_exact_physical_image(grouped):
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    calls = [patch(change(['unknown'], present(old), new, ref='internal:chapter-1'))
             for old, new in (([1, 2], [3]), ([3], [4]))]
    if grouped:
        submit(realm, *calls, reads=[], steps=[step('first', 0, calls[0]), step('second', 1, calls[1])])
    else:
        for call in calls:
            submit(realm, call, reads=[])
    landing = copy.deepcopy(realm.documents[realm.head['revision_id']])
    accepted = submit(realm, patch(change(['source_window'], present({'start': 4, 'end': 9}), {'start': 4, 'end': 8},
                                         ref='internal-revision:internal-1')), reads=[], base=base)
    assert accepted['data']['rebased_onto']['revision_id'] == landing['parent_revision']['revision_id']
    bindings = realm.entries[-1]['records']['commands'][0]['bindings']
    source = next(b for b in bindings if b['key'].startswith('doc.patch:'))
    assert source['physical_ref'] == 'internal-revision:' + landing['internal_timeline_revisions'][0]['revision_id']
    image = accepted['data']['canonical_document']['internal_timeline_revisions'][0]
    assert image['payload']['unknown'] == [4]
    assert image['payload']['source_window'] == {'start': 4, 'end': 8}


def test_native_parent_mirrors_capture_one_logical_effect_and_undo_exactly():
    realm = DetachedRealm()
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    call = {'schema_version': 1, 'op': 'clip.set', 'version': '1', 'select': 'parent/clip:clip-a',
            'args': {'values': {'hold': 3}, 'clear': []}}
    accepted = submit(realm, call, reads=[])
    effects = realm.entries[-1]['records']['steps'][0]['patch']
    assert effects == [change(['config', 'clips', 'id:clip-a', 'hold'], present(2), 3)]
    landed = accepted['data']['canonical_document']['parent_revision']['payload']
    assert landed['clips'] == landed['config']['clips']
    assert landed['clips'][0]['hold'] == 3
    restored = realm.call([inverse([accepted['receipt']['receipt_id']])])['data']['canonical_document']
    assert restored == original


def test_still_reachable_original_image_does_not_follow_other_owners_branch():
    document = document_fixture()
    for occurrence in document['parent_revision']['payload']['occurrences']:
        occurrence.update(source_offset=0, speed=1)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    realm.call([realm.move('occurrence:occ-a/clip:clip-a', 3.003)])
    submit(realm, patch(change(['clips', 'id:clip-a', 'hold'], present(2), 3,
                              ref='internal-revision:internal-1')), reads=[], base=base)
    images = realm.documents[realm.head['revision_id']]['internal_timeline_revisions']
    clips = [image['payload']['clips'][0] for image in images]
    assert sorted((row['at'], row['hold']) for row in clips) == [(1, 3), (3.003, 2)]


def test_created_ids_remain_reserved_after_patch_removal():
    realm = DetachedRealm()
    track = {'id': 'new-track', 'kind': 'visual'}
    insert = patch(change(['config', 'tracks'], ABSENT, track, op='list.insert', id='new-track',
                          order_before=['picture'], after_id=None), created=['parent/track:new-track'])
    submit(realm, insert, reads=[])
    submit(realm, patch(change(['config', 'tracks'], present(track), op='list.remove', id='new-track',
                              order_before=['new-track', 'picture'])), reads=[])
    with pytest.raises(CommitRejected) as error:
        submit(realm, insert, reads=[])
    assert error.value.failure['details']['reason'] == 'identity_collision'


def test_list_inverse_restores_exact_order_and_landing_head_sibling_value():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['clips', 'id:clip-a', 'hold'], present(2), 3)), reads=[])
    item = realm.documents['parent-1']['parent_revision']['payload']['clips'][1]
    accepted = submit(realm, patch(change(['clips'], present(item), op='list.move', id='clip-b',
                                         order_before=['clip-a', 'clip-b'], after_id=None)), reads=[], base=base)
    restored = realm.call([inverse([accepted['receipt']['receipt_id']])])
    rows = restored['data']['canonical_document']['parent_revision']['payload']['clips']
    assert [row['id'] for row in rows] == ['clip-a', 'clip-b']
    assert rows[0]['hold'] == 3


def test_occurrence_item_replacement_preserves_managed_links():
    original = document_fixture()['parent_revision']['payload']['occurrences'][0]
    replacement = {**copy.deepcopy(original), 'unknown': {'note': 'edited'}}
    plan = execute(bundle(), [patch(change(['occurrences', 'occurrence_id:occ-a'], present(original), replacement))])
    assert payload(plan)['occurrences'][0] == replacement


def test_authoritative_effect_inverse_roundtrips_lists_null_and_clear():
    from astrid.sdk.timeline._doc_patch import inverse_changes

    track = {'id': 'audio', 'kind': 'audio'}
    call = patch(change(['config', 'tracks'], ABSENT, track, op='list.insert', id='audio',
                        order_before=['picture'], after_id='picture'),
                 change(['config', 'tracks'], present(track), op='list.move', id='audio',
                        order_before=['picture', 'audio'], after_id=None),
                 change(['unknown', 'nullable'], ABSENT, None), created=['parent/track:audio'])
    forward = execute(bundle(), [call], commit_request={'reads': []})
    reversed_call = patch(*inverse_changes(forward.steps[0]['patch']))
    restored = execute(forward.bundle, [reversed_call])
    assert payload(restored) == document_fixture()['parent_revision']['payload']


def test_effects_store_actual_previous_value_at_landing_boundary():
    original = bundle()
    original['parent']['unknown']['number'] = 1.0
    call = patch(change(['unknown', 'number'], present(1), 2))
    plan = execute(original, [call], commit_request={'reads': []})
    actual = plan.steps[0]['patch'][0]['before']['value']
    assert type(actual) is float


def test_schema_generator_is_idempotent():
    from astrid.sdk.timeline._doc_patch_schema import extend
    from astrid.sdk.timeline.commands import _SCHEMA

    assert extend(_SCHEMA) == _SCHEMA


def test_object_keys_that_look_like_identity_tokens_are_literal():
    original = bundle()
    original['parent']['unknown']['id:a'] = {'id': 'free text'}
    plan = execute(original, [patch(change(['unknown', 'id:a', 'id'], present('free text'), 'changed'))])
    assert payload(plan)['unknown']['id:a']['id'] == 'changed'


def test_alias_previous_value_uses_present_logical_clips_when_one_mirror_is_absent():
    original = bundle()
    original['parent']['config'].pop('clips')
    rows = copy.deepcopy(original['parent']['clips'])
    with pytest.raises(CommandError) as error:
        execute(original, [patch(change(['config', 'clips'], ABSENT, []))])
    assert error.value.kind == 'previous_value_mismatch'
    replacement = copy.deepcopy(rows)
    replacement[0]['hold'] = 3
    plan = execute(original, [patch(change(['config', 'clips'], present(rows), replacement))])
    assert payload(plan)['clips'][0]['hold'] == 3


def test_registry_registration_uses_only_verified_media_inputs():
    from tests.sdk.test_timeline_commit_provider_document import MEDIA

    value = {'media_id': MEDIA, 'type': 'image'}
    call = patch(change(['registry', 'assets', 'extra'], ABSENT, value), created=['parent/asset:extra'])
    plan = execute(bundle(), [call])
    assert payload(plan)['registry']['assets']['extra'] == value
    call['args']['changes'][0]['value']['media_id'] = 'sha256:' + '2' * 64
    with pytest.raises(CommandError, match='Runtime-verified inputs'):
        execute(bundle(), [call])


def test_patch_and_native_entity_aliases_detect_equal_value_history_overlap():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    for hold in (3, 2):
        realm.call([{'schema_version': 1, 'op': 'clip.set', 'version': '1',
                     'select': 'parent/clip:clip-a', 'args': {'values': {'hold': hold}, 'clear': []}}])
    with pytest.raises(CommitRejected) as error:
        submit(realm, patch(change(['config', 'clips', 'id:clip-a', 'hold'], present(2), 4)), reads=[], base=base)
    assert error.value.failure['details']['reason'] == 'value_conflict'


def test_shot_item_replacement_records_physical_before_and_after_values():
    from astrid.sdk.timeline._command_state import State

    original = document_fixture()['shot_revisions'][0]['payload']['items']
    replacement = copy.deepcopy(original)
    replacement[0]['metadata']['note'] = 'edited'
    plan = execute(bundle(), [patch(change(['items'], present(original), replacement,
                                          ref='shot-revision:shot-1'))])
    for write in plan.records[0]['writes']:
        assert State(plan.bundle).get(write['ref'], write['path']) == write['after']
    images = document_from_bundle(plan.bundle)['shot_revisions']
    assert len(images) == 1
    assert images[0]['payload']['items'] == replacement


def test_declared_qualified_scope_read_follows_authenticated_shared_successor():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['unknown'], present([1, 2]), [3], ref='internal:chapter-1')), reads=[])
    # A field read may follow a proven successor when its own path was untouched.
    read = {'kind': 'field', 'ref': 'internal-revision:internal-1', 'path': ['source_window'],
            'state': present({'start': 4, 'end': 9})}
    submit(realm, patch(change(['name'], present('Renamed Film'), 'B')), reads=[read], base=base)
    assert realm.documents[realm.head['revision_id']]['parent_revision']['payload']['name'] == 'B'


def test_semantic_step_cannot_submit_provider_only_edge_effects():
    native = {'schema_version': 1, 'op': 'clip.set', 'version': '1',
              'select': 'parent/clip:clip-a', 'args': {'values': {'hold': 3}, 'clear': []}}
    claimed = step('edit', 0, native)
    claimed['patch'] = [change(['internal_timeline_revision_id'], present('internal-1'), 'forged', ref='shot-revision:missing')]
    with pytest.raises(CommandError) as error:
        execute(bundle(), [native], commit_request={'reads': [], 'steps': [claimed]})
    assert error.value.kind == 'managed_closure_write'


def test_semantic_creation_then_patch_rebases_over_unrelated_parent_field():
    realm = DetachedRealm()
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['name'], present('Renamed Film'), 'Other film title')), reads=[])
    create = {'schema_version': 1, 'op': 'occurrence.create', 'version': '1', 'select': 'parent',
              'args': {'occurrence_id': 'new-occ', 'shot_id': 'new-shot', 'name': 'New',
                       'track': 'picture', 'on': 25, 'duration': 5}}
    edit = patch(change(['name'], present('New'), 'Edited', ref='shot:new-shot'))
    submit(realm, create, edit, reads=[], base=base,
           steps=[step('create', 0, create), step('edit', 1, edit)])
    document = realm.documents[realm.head['revision_id']]
    assert document['parent_revision']['payload']['name'] == 'Other film title'
    assert any(image['payload']['name'] == 'Edited' for image in document['shot_revisions'])
