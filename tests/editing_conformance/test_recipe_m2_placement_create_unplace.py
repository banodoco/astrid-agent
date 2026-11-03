"""Occurrence placement CREATE/UNPLACE: exact capture, scoped birth, inverse and refusals."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute as _execute
from astrid.sdk.timeline.commands import validate_command
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_placement_commands import (
    capture_placement_create,
    capture_placement_unplace,
)
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import inverse as _inverse
from tests.editing_conformance.test_recipe_m1_commands import later
from tests.editing_conformance.test_recipe_m2_auto_placement import GenuineTimeline
from tests.editing_conformance.test_recipe_occurrence_media import inputs
from tests.sdk.test_timeline_commit_provider import (
    inverse as inverse_call,
)
from tests.sdk.test_timeline_commit_provider_document import MEDIA

execute = _execute
inverse = _inverse

OWNER = 'occurrence:occ-a'
CHILD = OWNER + '/clip:placed'
NEW = 'sha256:' + '2' * 64
ON, HOLD = 9.0, 4
FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_placement_create_unplace.json'


@pytest.fixture
def genuine_runtime(tmp_path, monkeypatch):
    context = GenuineTimeline.create(tmp_path, monkeypatch, 'image')
    try:
        yield context
    finally:
        context.close()


def create_case(context=None):
    if context is None:
        document, _, new, verified = inputs('image')
    else:
        document, new, verified = context.document, context.new, context.verified
    bundle = bundle_from_document(document)
    command = capture_placement_create(bundle, OWNER, 'placed', new, ON, HOLD,
                                       ownership='occurrence', verified_media=verified)
    return document, bundle, command, verified


def unplace_case(context=None):
    _, bundle, command, verified = create_case(context)
    if context is not None:
        created = context.call([command], 'placement-create')
        document = created['data']['canonical_document']
        bundle = bundle_from_document(document)
        removal = capture_placement_unplace(bundle, OWNER, 'placed', ownership='occurrence')
        return document, bundle, removal
    created = execute(bundle, [command], verified_media=verified)
    document = document_from_bundle(created.bundle)
    bundle = bundle_from_document(document)
    removal = capture_placement_unplace(bundle, OWNER, 'placed', ownership='occurrence')
    return document, bundle, removal


def case(operation, context=None):
    if operation == 'create':
        return create_case(context)
    document, bundle, command = unplace_case(context)
    return document, bundle, command, []


def plain(command):
    return {key: command[key] for key in ('schema_version', 'op', 'version', 'select', 'args', 'intent')}


@pytest.mark.parametrize('operation', ['create', 'unplace'])
def test_portable_goldens(operation):
    golden = load_fixture(FIXTURE)[operation]
    document, bundle, command, verified = case(operation)
    plan = execute(bundle, [command], verified_media=verified)
    assert golden == {'input': document, 'verified_media': verified, 'commands': [command],
                      'records': plan.records, 'output': document_from_bundle(plan.bundle)}


@pytest.mark.parametrize('operation', ['create', 'unplace'])
@pytest.mark.integration
def test_exact_capture_replay_inverse_and_redo(operation, genuine_runtime):
    _, bundle, command, verified = case(operation, genuine_runtime)
    before = copy.deepcopy(bundle)
    validate_command(command)
    assert json.loads(json.dumps(command, allow_nan=False)) == command
    plan = execute(bundle, [command], verified_media=verified)
    assert bundle == before
    assert plan.commands == [command]
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    # Re-inserting a removed row needs accepted order, as in the removal recipe.
    redone = inverse(undone.bundle, genuine_runtime.accepted_boundaries(
        undone.records, plan.bundle, undone.bundle, plan.commands))
    assert redone.bundle == plan.bundle


def test_create_is_scoped_birth_and_leaves_parent_and_sibling_untouched():
    document, bundle, command, verified = create_case()
    plan = execute(bundle, [command], verified_media=verified)
    assert command['created'] == [OWNER + '/asset:reserved-new', CHILD]
    assert [write['before'] for write in plan.records[0]['writes'] if write['ref'] == CHILD] == [
        {'state': 'absent'}]
    assert State(plan.bundle).entity(CHILD)['at'] == pytest.approx(1.009)
    assert State(plan.bundle).entity(CHILD)['hold'] == HOLD
    assert State(plan.bundle).entity(CHILD)['track'] == 'picture'
    assert plan.bundle['parent'] == bundle['parent']
    assert State(plan.bundle).collection('occurrence:occ-b', 'clip') == State(bundle).collection(
        'occurrence:occ-b', 'clip')
    assert NEW not in {pin['media_id'] for pin in document['dependency_manifest']['media']}
    output = document_from_bundle(plan.bundle)
    assert NEW in {pin['media_id'] for pin in output['dependency_manifest']['media']}


def test_unplace_clears_the_child_and_keeps_its_registry_binding():
    _, bundle, command, verified = case('unplace')
    plan = execute(bundle, [command], verified_media=verified)
    assert command['created'] == []
    assert [write['after'] for write in plan.records[0]['writes'] if write['ref'] == CHILD] == [{'state': 'absent'}]
    assert [row['id'] for row in State(plan.bundle).collection(OWNER, 'clip')] == ['clip-a', 'clip-b']
    assert State(plan.bundle).entity(OWNER + '/registry') == State(bundle).entity(OWNER + '/registry')
    assert plan.bundle['parent'] == bundle['parent']


@pytest.mark.integration
def test_provider_accepts_create_with_exact_undo_and_redo(genuine_runtime):
    document, _, command, verified = create_case(genuine_runtime)
    realm = genuine_runtime
    created = realm.call([command], 'placement-create')
    undo = realm.call([inverse_call([created['receipt']['receipt_id']])], 'placement-create-undo')
    assert undo['data']['canonical_document'] == document
    redo = realm.call([inverse_call([undo['receipt']['receipt_id']])], 'placement-create-redo')
    assert redo['data']['canonical_document'] == created['data']['canonical_document']


@pytest.mark.integration
def test_provider_accepts_unplace_with_exact_undo_and_redo(genuine_runtime):
    document, _, command, verified = create_case(genuine_runtime)
    realm = genuine_runtime
    created = realm.call([command], 'placement-create')
    after = created['data']['canonical_document']
    removal = capture_placement_unplace(bundle_from_document(after), OWNER, 'placed', ownership='occurrence')
    removed = realm.call([removal], 'placement-unplace')
    assert removed['data']['canonical_document'] != after
    undo = realm.call([inverse_call([removed['receipt']['receipt_id']])], 'placement-unplace-undo')
    assert undo['data']['canonical_document'] == after
    redo = realm.call([inverse_call([undo['receipt']['receipt_id']])], 'placement-unplace-redo')
    assert redo['data']['canonical_document'] == removed['data']['canonical_document']


@pytest.mark.parametrize('damage', ['shared-intent', 'parent-select', 'shared-root', 'occupied', 'video',
    'missing-media', 'wrong-object', 'before-occurrence', 'after-end', 'bool-on', 'negative-on',
    'zero-hold', 'slash-id', 'hidden-lane', 'source-map', 'extra-arg'])
def test_create_refusals_preserve_input(damage):
    _, bundle, command, verified = create_case()
    if damage == 'shared-root':
        bundle['placements'][1]['shot_id'] = bundle['placements'][0]['shot_id']
    elif damage == 'hidden-lane':
        State(bundle).config(OWNER)['tracks'][0]['hidden'] = True
    elif damage == 'source-map':
        State(bundle).occurrence(OWNER)['source_offset'] = 1
    elif damage == 'video':
        verified[1].update(media_type='video/fixture', duration_seconds=6)
    elif damage == 'missing-media':
        verified.pop()
    elif damage == 'wrong-object':
        command['args']['media']['object_id'] = MEDIA
    elif damage == 'occupied':
        command['args']['id'] = 'clip-a'
    elif damage == 'before-occurrence':
        command['args']['on'] = 7.0
    elif damage == 'after-end':
        command['args']['on'] = 17.5
    elif damage == 'bool-on':
        command['args']['on'] = True
    elif damage == 'negative-on':
        command['args']['on'] = -1
    elif damage == 'zero-hold':
        command['args']['hold'] = 0
    elif damage == 'slash-id':
        command['args']['id'] = 'a/b'
    elif damage == 'shared-intent':
        command['intent'] = {'ownership': 'shared'}
    elif damage == 'parent-select':
        command['select'] = 'parent'
    else:
        command['extra'] = True
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        execute(bundle, [command], verified_media=verified)
    assert bundle == before


@pytest.mark.parametrize('damage', ['shared-root', 'not-media', 'nested', 'relation', 'missing-target'])
def test_unplace_refusals_preserve_input(damage):
    _, bundle, command, _ = case('unplace')
    if damage == 'shared-root':
        bundle['placements'][1]['shot_id'] = bundle['placements'][0]['shot_id']
    elif damage == 'not-media':
        State(bundle).entity(CHILD)['clipType'] = 'text'
    elif damage == 'nested':
        State(bundle).entity(CHILD)['children'] = []
    elif damage == 'relation':
        State(bundle).entity(OWNER + '/clip:clip-a')['app'] = {'astrid': {'on': 'word:old:0'}}
    else:
        command['select'] = OWNER + '/clip:absent'
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError):
        execute(bundle, [command])
    assert bundle == before


def test_registry_binding_aba_blocks_undo_and_replay_refuses_changed_binding():
    _, bundle, command, verified = create_case()
    plan = execute(bundle, [command], verified_media=verified)
    removal = capture_placement_unplace(plan.bundle, OWNER, 'placed', ownership='occurrence')
    removed = execute(plan.bundle, [removal])
    head = copy.deepcopy(removed.bundle)
    State(head).entity(OWNER + '/registry')['assets']['reserved-new']['generationId'] = 'generation-other'
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(removed, head)
    assert head == before
    path = ['registry', 'assets', 'reserved-new', 'generationId']
    same = [later(OWNER + '/source', path, 'generation-new')]
    assert replay(removed, plan.bundle, intervening_records=same).records == removed.records
    with pytest.raises(CommandError, match='intervening'):
        inverse(removed.bundle, removed.records, same)


def test_disjoint_edit_rebases_and_retains_original_capture():
    _, bundle, command, verified = create_case()
    plan = execute(bundle, [command], verified_media=verified)
    head = copy.deepcopy(bundle)
    State(head).entity('occurrence:occ-b/clip:clip-a')['at'] = 3
    head['parent']['name'] = 'Someone else renamed this'
    rebased = replay(plan, head, verified_media=verified)
    assert rebased.commands == plan.commands
    assert State(rebased.bundle).entity('occurrence:occ-b/clip:clip-a')['at'] == 3
    assert inverse(rebased.bundle, rebased.records).bundle == head


def test_later_consumer_of_created_binding_blocks_inverse():
    _, bundle, command, verified = create_case()
    plan = execute(bundle, [command], verified_media=verified)
    State(plan.bundle).entity(OWNER + '/clip:clip-b')['asset'] = 'reserved-new'
    with pytest.raises(CommandError, match='surviving dependents'):
        inverse(plan.bundle, plan.records)


def test_unplace_refuses_when_a_member_declares_a_relation():
    _, bundle, command, verified = create_case()
    staged = execute(bundle, [command], verified_media=verified).bundle
    State(staged).entity(OWNER + '/clip:clip-a')['app'] = {'astrid': {'until': CHILD}}
    before = copy.deepcopy(staged)
    with pytest.raises(CommandError, match='semantic dependencies'):
        capture_placement_unplace(staged, OWNER, 'placed', ownership='occurrence')
    assert staged == before


def test_capture_refuses_unsupported_owner_and_identity_before_execution():
    _, bundle, _, verified = create_case()
    for owner, ownership in [('parent', 'occurrence'), (OWNER, 'parent'), (OWNER, 'shared'),
                             ('occurrence:bad::id', 'occurrence')]:
        with pytest.raises(CommandError) as error:
            capture_placement_create(bundle, owner, 'placed', verified[1]['selection'], ON, HOLD,
                                     ownership=ownership, verified_media=verified)
        assert error.value.kind == 'unsupported_ownership'
    with pytest.raises(CommandError):
        capture_placement_unplace(bundle, OWNER, 'parent/clip:x', ownership='occurrence')


def test_pool_auto_and_relative_placement_are_not_supported():
    _, bundle, command, verified = create_case()
    for field, value in [('on', None), ('on', 'auto'), ('timeline_frame', 120), ('frame', 120)]:
        forged = plain(command)
        forged['args'] = {**forged['args'], field: value}
        with pytest.raises(CommandError):
            execute(bundle, [forged], verified_media=verified)


def test_undo_of_create_refuses_after_any_intervening_write_to_the_created_clip():
    # Undo is strict ABA, not a LIFO stack: even an equal-value rewrite refuses.
    _, bundle, command, verified = create_case()
    plan = execute(bundle, [command], verified_media=verified)
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later(CHILD, ['hold'], HOLD)])


def test_create_cannot_reuse_a_removed_clip_identity_while_it_is_occupied():
    # Re-placing unchanged media is placement.move; a replacement needs a NEW reserved id.
    _, bundle, command, verified = create_case()
    plan = execute(bundle, [command], verified_media=verified)
    again = capture_placement_create(bundle, OWNER, 'replacement', verified[1]['selection'], ON, HOLD,
                                     ownership='occurrence', verified_media=verified)
    assert again['created'][-1] == OWNER + '/clip:replacement'
    forged = plain(command)
    with pytest.raises(CommandError):
        execute(plan.bundle, [forged], verified_media=verified)
