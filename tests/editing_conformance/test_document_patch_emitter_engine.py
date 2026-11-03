"""G3 losslessness against R1's real executor and physical closure converter."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import _remap_known_item_references
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    _json,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.emitter import DocumentPatchError, emit_document_commands
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def source(*, shared=False):
    return bundle_from_document(document_fixture(shared=shared))


def roundtrip(before, after):
    original = copy.deepcopy(before)
    commands = emit_document_commands(before, after)
    assert commands
    plan = execute(before, commands, commit_request={})
    landed = document_from_bundle(plan.bundle)
    # Verify the complete physical closure roundtrip. R1 owns successor IDs;
    # compare every authored payload and exact owner link independently of its
    # materialization spelling (the detached compiler uses occurrence-local IDs).
    assert _json(document_from_bundle(bundle_from_document(landed))) == _json(landed)
    parent = copy.deepcopy(landed['parent_revision']['payload'])
    occurrences = parent.pop('occurrences')
    assert _json(parent) == _json(after['parent'])
    assert len(occurrences) == len(after['placements'])
    shots = {row['revision_id']: row for row in landed['shot_revisions']}
    internals = {row['revision_id']: row for row in landed['internal_timeline_revisions']}
    for placement, occurrence in zip(after['placements'], occurrences):
        local = placement['shot_id']
        origin = before['source_mapping']['shots'][local]
        image = shots[occurrence['shot_revision_id']]
        child = internals[image['internal_timeline_revision_id']]
        assert image['shot_id'] == origin['shot_id']
        assert child['timeline_id'] == origin['internal_timeline_id']
        expected = copy.deepcopy(placement)
        expected.update(shot_id=image['shot_id'], shot_revision_id=image['revision_id'])
        assert _json(expected) == _json(occurrence)
        authored = after['shots'][local]
        payload = _remap_known_item_references(authored['payload'],
                    {v: k for k, v in origin.get('item_id_map', {}).items()})
        payload['internal_timeline_revision_id'] = child['revision_id']
        assert _json(image['payload']) == _json(payload)
        assert _json(child['payload']) == _json(authored['internal_timeline'])
    assert before == original
    assert plan.merge_mode == 'atomic'
    assert any(dep['kind'] == 'scope' for dep in plan.steps[0]['dependencies'])
    return commands, plan


def multi_source():
    doc = document_fixture()
    internal = copy.deepcopy(doc['internal_timeline_revisions'][0])
    internal.update(revision_id='internal:2', timeline_id='film')
    doc['internal_timeline_revisions'][0]['timeline_id'] = 'film'
    shot = copy.deepcopy(doc['shot_revisions'][0])
    shot.update(revision_id='shot:2', internal_timeline_revision_id='internal:2')
    shot['payload']['internal_timeline_revision_id'] = 'internal:2'
    shot['content_digest'] = _digest(shot['payload'])
    doc['internal_timeline_revisions'].append(internal)
    doc['shot_revisions'].append(shot)
    doc['parent_revision']['payload']['occurrences'][1]['shot_revision_id'] = 'shot:2'
    doc['parent_revision']['content_digest'] = _digest(doc['parent_revision']['payload'])
    doc['dependency_manifest'] = _manifest(doc['shot_revisions'], doc['internal_timeline_revisions'],
                                           doc['dependency_manifest']['media'], [])
    return bundle_from_document(doc)


def test_multichild_edits_use_source_revisions_even_when_entity_ids_collide():
    before = multi_source()
    after = copy.deepcopy(before)
    for index, shot in enumerate(after['shots'].values()):
        shot['internal_timeline']['unknown'] = [index, None, False]
        shot['payload']['name'] = f'Chapter {index}'
    commands, _ = roundtrip(before, after)
    assert {change['ref'] for change in commands[0]['args']['changes']} == {
        'shot-revision:shot-1', 'shot-revision:shot:2',
        'internal-revision:internal-1', 'internal-revision:internal:2',
    }


def test_one_physical_child_edit_preserves_the_other_exact_image():
    before = multi_source()
    after = copy.deepcopy(before)
    local = next(iter(after['shots']))
    after['shots'][local]['internal_timeline']['unknown'] = [None]
    commands, plan = roundtrip(before, after)
    expected = before['source_mapping']['shots'][local]['internal_timeline_revision_id']
    assert {row['ref'] for row in commands[0]['args']['changes']} == {'internal-revision:' + expected}
    untouched = [image for image in before['source_mapping']['_runtime_document']['internal_timeline_revisions']
                 if image['revision_id'] != expected][0]
    assert untouched in document_from_bundle(plan.bundle)['internal_timeline_revisions']


def test_shared_root_aliases_normalize_once_and_remap_only_known_item_references():
    before = source(shared=True)
    after = copy.deepcopy(before)
    for shot in after['shots'].values():
        shot['payload']['items'][0]['metadata']['opaque_literal'] = 'literal:keep'
        shot['internal_timeline']['unknown'] = [None, {'id': 'opaque'}]
    commands, _ = roundtrip(before, after)
    changes = commands[0]['args']['changes']
    assert len(changes) == 2
    assert {row['ref'] for row in changes} == {'shot-revision:shot-1', 'internal-revision:internal-1'}
    assert next(row for row in changes if row['ref'].startswith('shot'))['value'][0]['item_id'] == 'item-1'


def test_divergent_shared_authoring_aliases_require_explicit_fork():
    before = source(shared=True)
    after = copy.deepcopy(before)
    next(iter(after['shots'].values()))['internal_timeline']['unknown'] = [3]
    with pytest.raises(DocumentPatchError, match='contradictory authoring aliases'):
        emit_document_commands(before, after)


@pytest.mark.parametrize('scope', ['parent', 'shot', 'internal'])
def test_presence_clear_reserved_spellings_and_id_shaped_opaque_arrays(scope):
    before = source()
    local = next(iter(before['shots']))
    root = {'parent': before['parent'], 'shot': before['shots'][local]['payload'],
            'internal': before['shots'][local]['internal_timeline']}[scope]
    root['meta'] = {'org.example': {'literal:key.name': {'content_digest': 'literal', 'shot_id': 'opaque',
                    'revision_id': None, 'occurrences': [{'occurrence_id': 'x', 'id': 'a'}],
                    'clear': None, 'rows': [{'id': 'a', 'v': 1}, {'id': 'a', 'v': None}]}}}
    before = bundle_from_document(document_from_bundle(before))
    local = next(iter(before['shots']))
    after = copy.deepcopy(before)
    target = {'parent': after['parent'], 'shot': after['shots'][local]['payload'],
              'internal': after['shots'][local]['internal_timeline']}[scope]['meta']['org.example']['literal:key.name']
    target['content_digest'] = 'authored:exact'
    target['shot_id'] = False
    target['revision_id'] = 0
    target['added'] = None
    del target['clear']
    target['rows'] = [{'id': 'b', 'value': []}, None, {}]
    commands, _ = roundtrip(before, after)
    rows = commands[0]['args']['changes']
    assert all(not row['op'].startswith('list.') for row in rows)
    assert all(row['path'][:3] == ['meta', 'org.example', 'literal:key.name'] for row in rows)
    assert next(row for row in rows if row['path'][-1] == 'added')['before'] == {'state': 'absent'}
    clear = next(row for row in rows if row['path'][-1] == 'clear')
    assert clear['op'] == 'remove' and clear['before'] == {'state': 'present', 'value': None}
    assert next(row for row in rows if row['path'][-1] == 'rows')['before']['value'] == root['meta']['org.example']['literal:key.name']['rows']
    assert 'created' not in commands[0]


@pytest.mark.parametrize('damage', [None, {}, [{}], [{'id': 'x'}, {'id': 'x'}]])
@pytest.mark.parametrize('side', ['before', 'after'])
def test_malformed_known_collections_refuse_without_whole_array_bypass(damage, side):
    before = source()
    after = copy.deepcopy(before)
    {'before': before, 'after': after}[side]['parent']['config']['tracks'] = damage
    with pytest.raises(DocumentPatchError, match='collection|identity'):
        emit_document_commands(before, after)


@pytest.mark.parametrize('mirror', ['clips', 'registry'])
@pytest.mark.parametrize('side', ['before', 'after'])
def test_contradictory_parent_mirrors_refuse_before_emission(mirror, side):
    before = source()
    before['parent']['config']['registry'] = copy.deepcopy(before['parent']['registry'])
    after = copy.deepcopy(before)
    root = {'before': before, 'after': after}[side]['parent']
    root[mirror] = [] if mirror == 'clips' else {'assets': {}}
    with pytest.raises(DocumentPatchError, match='contradictory parent mirrors'):
        emit_document_commands(before, after)


def test_valid_mirrors_emit_one_logical_change_and_roundtrip_exactly():
    before = source()
    before['parent']['config']['registry'] = copy.deepcopy(before['parent']['registry'])
    after = copy.deepcopy(before)
    for clips in (after['parent']['clips'], after['parent']['config']['clips']):
        clips[0]['hold'] = 3
    for registry in (after['parent']['registry'], after['parent']['config']['registry']):
        registry['assets']['picture']['meta'] = {'org.example': {'content_digest': None}}
    commands, _ = roundtrip(before, after)
    assert len(commands) == 2
    assert sum(len(call['args']['changes']) for call in commands) == 2


@pytest.mark.parametrize('field', ['shot_id', 'revision_id', 'internal_timeline_revision_id'])
def test_shot_managed_edges_are_protected_at_root_only(field):
    before = source()
    local = next(iter(before['shots']))
    after = copy.deepcopy(before)
    after['shots'][local]['payload'][field] = 'forged'
    with pytest.raises(DocumentPatchError, match='managed closure edges|source-bound'):
        emit_document_commands(before, after)


def test_occurrence_shot_edge_is_protected_and_new_roots_require_semantics():
    before = source()
    after = copy.deepcopy(before)
    after['placements'][0]['shot_id'] = 'forged'
    with pytest.raises(DocumentPatchError, match='semantic command'):
        emit_document_commands(before, after)
    after = copy.deepcopy(before)
    after['shots']['new-root'] = copy.deepcopy(next(iter(after['shots'].values())))
    with pytest.raises(DocumentPatchError, match='semantic command'):
        emit_document_commands(before, after)


def test_insert_remove_move_claims_and_guards_are_sequential():
    before = source()
    before['parent']['config']['tracks'] = [{'id': key, 'kind': 'visual'} for key in ('a', 'b', 'c')]
    after = copy.deepcopy(before)
    after['parent']['config']['tracks'] = [{'id': key, 'kind': 'visual'} for key in ('c', 'new', 'a')]
    commands, plan = roundtrip(before, after)
    call = commands[0]
    assert call['created'] == ['parent/track:new']
    structural = call['args']['changes']
    assert [row['op'] for row in structural] == ['list.remove', 'list.insert', 'list.move', 'list.move']
    assert [row['order_before'] for row in structural] == [
        ['a', 'b', 'c'], ['a', 'c'], ['a', 'c', 'new'], ['c', 'a', 'new']]
    ordered = [dep for dep in plan.steps[0]['dependencies'] if dep['kind'] == 'collection']
    assert ordered and all(dep['order'] is not None for dep in ordered)
    bad = copy.deepcopy(commands)
    bad[0]['args']['changes'][0]['order_before'].reverse()
    with pytest.raises(CommandError, match='order changed'):
        execute(before, bad)
    after = copy.deepcopy(before)
    after['parent']['config']['tracks'][0]['label'] = 'Only a field'
    _, plan = roundtrip(before, after)
    assert not any(dep['kind'] == 'collection' for dep in plan.steps[0]['dependencies'])


@pytest.mark.parametrize('scope', ['parent', 'internal'])
def test_created_clip_track_and_asset_claims_follow_patch_order(scope):
    before = source()
    after = copy.deepcopy(before)
    root = after['parent'] if scope == 'parent' else next(iter(after['shots'].values()))['internal_timeline']
    tracks = root['config']['tracks'] if scope == 'parent' else root['tracks']
    tracks.append({'id': 'new-track', 'kind': 'visual'})
    clip = dict(copy.deepcopy(root['clips'][0]), id='new-clip', track='new-track')
    root['clips'].append(clip)
    if scope == 'parent':
        root['config']['clips'].append(copy.deepcopy(clip))
    root['registry']['assets']['new-asset'] = copy.deepcopy(root['registry']['assets']['picture'])
    commands, plan = roundtrip(before, after)
    assert commands[0]['created'] == [row['ref'] for row in plan.records[0]['created']]
    assert len(commands[0]['created']) == 3


@pytest.mark.parametrize('value', [1.0, True, None, [], {}])
def test_opaque_scalar_representation_is_preserved(value):
    before = source()
    before['parent']['unknown']['scalar'] = 1
    after = copy.deepcopy(before)
    after['parent']['unknown']['scalar'] = value
    commands, _ = roundtrip(before, after)
    assert commands[0]['args']['changes'][0]['before'] == {'state': 'present', 'value': 1}
    assert _json(commands[0]['args']['changes'][0]['value']) == _json(value)


def test_membership_reads_allow_reordering_while_ordered_reads_guard_it():
    before = source()
    before['parent']['config']['tracks'].append({'id': 'sound', 'kind': 'audio'})
    after = copy.deepcopy(before)
    after['parent']['name'] = 'New name'
    commands = emit_document_commands(before, after)
    read = {'kind': 'collection', 'ref': 'parent', 'path': ['config', 'tracks'],
            'members': ['picture', 'sound'], 'order': None}
    head = copy.deepcopy(before)
    head['parent']['config']['tracks'].reverse()
    plan = execute(head, commands, commit_request={'reads': [read]})
    assert plan.merge_mode == 'fields'
    assert plan.bundle['parent']['name'] == 'New name'
    read['order'] = ['picture', 'sound']
    with pytest.raises(CommandError, match='collection read does not match'):
        execute(head, commands, commit_request={'reads': [read]})
    read['order'] = None
    head['parent']['config']['tracks'].pop()
    with pytest.raises(CommandError, match='collection read does not match'):
        execute(head, commands, commit_request={'reads': [read]})


def test_unique_id_shaped_opaque_rows_are_one_guarded_value():
    before = source()
    before['parent']['unknown']['rows'] = [{'id': 'a', 'v': None}, {'id': 'b', 'v': 1}]
    after = copy.deepcopy(before)
    after['parent']['unknown']['rows'] = [{'id': 'b', 'v': []}, {'id': 'a', 'v': {}}]
    commands, _ = roundtrip(before, after)
    assert commands[0]['args']['changes'] == [{
        'op': 'set', 'ref': 'parent', 'path': ['unknown', 'rows'],
        'before': {'state': 'present', 'value': before['parent']['unknown']['rows']},
        'value': after['parent']['unknown']['rows'],
    }]


@pytest.mark.parametrize('spelling', ['clips', 'config.clips'])
def test_single_parent_clip_alias_is_lossless(spelling):
    before = source()
    if spelling == 'clips':
        before['parent']['config'].pop('clips')
    else:
        before['parent'].pop('clips')
    after = copy.deepcopy(before)
    clips = after['parent']['clips'] if spelling == 'clips' else after['parent']['config']['clips']
    clips[0]['hold'] = 3
    roundtrip(before, after)
