"""Scalar acceptance/reload/inverse through the real native provider and recorder."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import ABSENT, State, present
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params, capture_track_set
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from tests.editing_conformance import test_recipe_scalar_commands as scalar_recipes
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedRealm,
    inverse,
    restore,
    track_create,
)
from tests.sdk.test_timeline_commit_provider import (
    runtime_abi as runtime_abi,
)

admitted_param_revisions = scalar_recipes.admitted_param_revisions
configured_project_pack = scalar_recipes.configured_project_pack


def current(realm):
    return bundle_from_document(realm.documents[realm.head['revision_id']])


def test_provider_refuses_old_param_basis_after_admitted_element_update(
        admitted_param_revisions, configured_project_pack):
    from astrid.sdk.timeline._clip_commands import capture_clip_add
    from tests.editing_conformance.test_recipe_m2_clip_add import empty_document

    realm = DetachedRealm(empty_document())
    realm.call([track_create('picture')])
    old, new = admitted_param_revisions
    options = scalar_recipes.schema_options(configured_project_pack, old)
    source, realm.snapshot_objects = options['schema_inputs'][0]
    realm.live_sources[_digest(source['pin'])] = source
    row = {'id': 'effect', 'clipType': old['id'], 'elementRef': old,
           'at': 0, 'hold': 2, 'track': 'picture'}
    realm.call([capture_clip_add(current(realm), 'parent', row)])
    original = current(realm)
    command = capture_clip_set_params(original, 'parent/clip:effect', values={'seed': 16}, **options)
    # Seed the supported replacement revision as an immutable accepted head.
    # No new element-update command or renderer is needed for this provider probe.
    replacement = copy.deepcopy(original)
    State(replacement).entity('parent/clip:effect')['elementRef'] = new
    replacement['parent']['config']['clips'] = copy.deepcopy(replacement['parent']['clips'])
    replacement_options = scalar_recipes.schema_options(configured_project_pack, new)
    document = document_from_bundle(
        replacement, document_pins=[replacement_options['schema_source']])
    revision = document['parent_revision']['revision_id']
    realm.documents[revision] = document
    source, realm.snapshot_objects = replacement_options['schema_inputs'][0]
    realm.retained_sources[_digest(source['pin'])] = source
    realm.call([restore(document)])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected) as error:
        realm.call([command])
    assert error.value.failure['details']['reason'] == 'dependency_changed'
    assert (realm.head, realm.entries, realm.documents) == before


def clear_realm(container):
    from tests.sdk.test_timeline_commit_provider_document import document_fixture

    document = document_fixture()
    for rows in (document['parent_revision']['payload']['clips'],
                 document['parent_revision']['payload']['config']['clips']):
        if container != ABSENT:
            rows[0]['params'] = copy.deepcopy(container['value'])
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    return DetachedRealm(document)


@pytest.mark.parametrize('container', [ABSENT, present(None)])
def test_provider_clear_only_preserves_absent_or_null_container(container):
    realm = clear_realm(container)
    ref = 'parent/clip:clip-a'
    before = copy.deepcopy(State(current(realm)).entity(ref))
    revision = realm.head['revision_id']
    accepted = realm.call([capture_clip_set_params(current(realm), ref, clear=['missing'])])
    assert State(current(realm)).entity(ref) == before
    assert realm.head['revision_id'] == revision
    assert accepted['data']['commands'][0]['expect'] == {ref + '::params.missing': ABSENT}
    record = realm.entries[-1]['records']['commands'][0]
    assert record['writes'] == [
        {'ref': ref, 'path': ['params', 'missing'], 'before': ABSENT, 'after': ABSENT}]
    assert {'ref': ref, 'path': ['params'], 'state': container} in record['reads']
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert State(current(realm)).entity(ref) == before
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']


@pytest.mark.parametrize('container', [ABSENT, present(None)])
@pytest.mark.parametrize('aba', [False, True])
def test_provider_absent_leaf_clear_retains_publication_and_aba_intent(container, aba):
    realm = clear_realm(container)
    ref = 'parent/clip:clip-a'
    first = realm.call([capture_clip_set_params(current(realm), ref, clear=['missing'])])
    revision = realm.head['revision_id']
    if aba:
        away = realm.call([capture_clip_set_params(current(realm), ref, values={'missing': None})])
        realm.call([inverse([away['receipt']['receipt_id']])])
    last = realm.call([capture_clip_set_params(current(realm), ref, clear=['missing'])])
    assert realm.head['revision_id'] == revision
    assert last['receipt']['receipt_id'] != first['receipt']['receipt_id']
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([first['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert (realm.head, realm.entries, realm.documents) == before


@pytest.mark.parametrize('registry_location', ['direct', 'config', 'both'])
def test_parent_param_binding_uses_main_effective_registry(registry_location):
    from tests.sdk.test_timeline_commit_provider_document import document_fixture
    document = document_fixture()
    payload = document['parent_revision']['payload']
    for rows in (payload['clips'], payload['config']['clips']):
        rows[0]['clipType'] = 'am-sprite'
    if registry_location in ('config', 'both'):
        payload['config']['registry'] = copy.deepcopy(payload['registry'])
    if registry_location == 'config':
        del payload['registry']
    document['parent_revision']['content_digest'] = _digest(payload)
    realm = DetachedRealm(document)
    original = current(realm)
    frames = {'frameWidth': 16, 'frameHeight': 16, 'count': 4, 'fps': 8}
    command = capture_clip_set_params(original, 'parent/clip:clip-a', values={'frames': frames})
    replacement = copy.deepcopy(original)
    payload = replacement['parent']
    for registry in (payload.get('registry'), payload['config'].get('registry')):
        if registry is not None:
            registry['assets']['picture']['media_id'] = 'sha256:' + 'f' * 64
    document = document_from_bundle(replacement)
    realm.documents[document['parent_revision']['revision_id']] = document
    realm.call([restore(document)])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected) as error:
        realm.call([command])
    assert error.value.failure['details']['reason'] == 'dependency_changed'
    assert (realm.head, realm.entries, realm.documents) == before


def test_committed_move_then_parent_label_and_occurrence_params_reload_undo_redo():
    realm = DetachedRealm()
    moved = realm.call([realm.move()])
    moved_document = moved['data']['canonical_document']
    bundle = current(realm)
    label = capture_track_set(bundle, 'parent/track:picture', values={'label': 'Picture'})
    local = execute(bundle, [label]).bundle
    params = capture_clip_set_params(local, 'occurrence:occ-a/clip:clip-a',
                                     values={'null': None, 'opaque': {'keep': [False, {}, '']}})
    result = realm.call([label, params])
    assert result['data']['commands'] == [label, params]
    document = result['data']['canonical_document']
    reloaded = bundle_from_document(json.loads(json.dumps(document)))
    assert State(reloaded).entity('parent/track:picture')['label'] == 'Picture'
    assert State(reloaded).entity('parent/clip:clip-a')['at'] == 3.003
    assert State(reloaded).entity('occurrence:occ-a/clip:clip-a')['params'] == {
        'null': None, 'opaque': {'keep': [False, {}, '']}}
    assert 'params' not in State(reloaded).entity('occurrence:occ-b/clip:clip-a')
    assert 'params' not in State(reloaded).entity('parent/clip:clip-a')
    assert moved_document['shot_revisions'][0] in document['shot_revisions']
    assert moved_document['internal_timeline_revisions'][0] in document['internal_timeline_revisions']
    undo = realm.call([inverse([result['receipt']['receipt_id']])])
    assert undo['data']['canonical_document'] == moved_document
    redo = realm.call([inverse([undo['receipt']['receipt_id']])])
    assert redo['data']['canonical_document'] == document


def test_move_then_parent_label_reload_and_exact_inverse_redo():
    realm = DetachedRealm()
    moved = realm.call([realm.move()])
    command = capture_track_set(current(realm), 'parent/track:picture', values={'label': 'Picture'})
    accepted = realm.call([command])
    assert State(current(realm)).entity('parent/clip:clip-a')['at'] == 3.003
    assert State(current(realm)).entity('parent/track:picture')['label'] == 'Picture'
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == moved['data']['canonical_document']
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']


def test_occurrence_scalar_inverse_restores_fields_and_redo_after_accepted_reload():
    realm = DetachedRealm()
    ref = 'occurrence:occ-a/clip:clip-a'
    original = copy.deepcopy(State(current(realm)).entity(ref))
    accepted = realm.call([capture_clip_set_params(current(realm), ref, values={'null': None})])
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert State(current(realm)).entity(ref) == original
    assert 'params' not in State(current(realm)).entity('occurrence:occ-b/clip:clip-a')
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']


def test_disjoint_param_leaf_rebase_survives_materialization_and_keeps_original_claims():
    realm = DetachedRealm()
    ref = 'occurrence:occ-a/clip:clip-a'
    realm.call([capture_clip_set_params(current(realm), ref, values={'x': 0, 'y': None})])
    base, original = copy.deepcopy(realm.head), current(realm)
    a = capture_clip_set_params(original, ref, values={'x': 1})
    b = capture_clip_set_params(original, ref, values={'y': False})
    first = realm.call([a])
    result = realm.call([b], base=base)
    assert result['data']['status'] == 'rebased'
    assert result['data']['commands'] == [b]
    assert State(current(realm)).entity(ref)['params'] == {'x': 1, 'y': False}
    undo = realm.call([inverse([result['receipt']['receipt_id']])])
    assert undo['data']['canonical_document'] == first['data']['canonical_document']


def test_dependent_original_capture_handoff_after_move_ack_and_same_child_revision_change():
    realm = DetachedRealm()
    original = current(realm)
    move = realm.move()
    local = execute(original, [move]).bundle
    b = capture_track_set(local, 'parent/track:picture', values={'label': 'After move'})
    sealed = json.dumps(b, sort_keys=True)
    realm.call([move])
    accepted = realm.call([b])
    assert json.dumps(accepted['data']['commands'][0], sort_keys=True) == sealed
    assert State(current(realm)).entity('parent/clip:clip-a')['at'] == 3.003
    ref = 'occurrence:occ-a/clip:clip-a'
    # Author B over local A before A has materialized a shared source occurrence.
    a = capture_clip_set_params(current(realm), ref, values={'x': 1, 'y': None})
    local = execute(current(realm), [a]).bundle
    b = capture_clip_set_params(local, ref, values={'y': 2})
    sealed = copy.deepcopy(b)
    realm.call([a])
    realm.call([capture_clip_set_params(current(realm), ref, values={'x': 3})])
    accepted = realm.call([b])
    assert accepted['data']['commands'] == [sealed]
    assert State(current(realm)).entity(ref)['params'] == {'x': 3, 'y': 2}


@pytest.mark.parametrize('op', ['track.set', 'clip.set-params'])
@pytest.mark.parametrize('aba', [False, True])
def test_scalar_unchanged_publications_and_aba_refuse_guarded_undo(op, aba):
    realm = DetachedRealm()

    def capture(value):
        if op == 'track.set':
            return capture_track_set(current(realm), 'parent/track:picture', values={'label': value})
        return capture_clip_set_params(current(realm), 'parent/clip:clip-a', values={'literal': value})

    original = realm.call([capture('same')])
    revision = realm.head['revision_id']
    if aba:
        realm.call([capture('away')])
    last = realm.call([capture('same')])
    assert realm.head['revision_id'] == revision
    assert last['receipt']['receipt_id'] != original['receipt']['receipt_id']
    record = realm.entries[-1]['records']['commands'][0]
    if not aba:
        assert any(write['before'] == write['after'] for write in record['writes'])
    before = copy.deepcopy(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([original['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert realm.head == before


def test_null_assignment_then_explicit_clear_accepted_reload_and_exact_inverse():
    realm = DetachedRealm()
    ref = 'parent/clip:clip-a'
    first = realm.call([capture_clip_set_params(current(realm), ref,
                                              values={'null': None, 'unknown': {'literal': [None, '']}})])
    cleared = realm.call([capture_clip_set_params(current(realm), ref, clear=['null'])])
    assert State(current(realm)).entity(ref)['params'] == {'unknown': {'literal': [None, '']}}
    restored = realm.call([inverse([cleared['receipt']['receipt_id']])])
    assert restored['data']['canonical_document'] == first['data']['canonical_document']
    assert State(current(realm)).entity(ref)['params']['null'] is None


def test_original_param_claim_conflict_and_malformed_batch_do_not_accept_partially():
    realm = DetachedRealm()
    ref = 'parent/clip:clip-a'
    original = capture_clip_set_params(current(realm), ref, values={'x': 2})
    realm.call([capture_clip_set_params(current(realm), ref, values={'x': 3})])
    before, count = copy.deepcopy(realm.head), len(realm.entries)
    with pytest.raises(CommitRejected) as error:
        realm.call([original])
    assert error.value.failure['details']['reason'] == 'dependency_changed'
    label = capture_track_set(current(realm), 'parent/track:picture', values={'label': 'New'})
    malformed = dict(original, args={'values': {'x': 4}, 'clear': [], 'config': {}})
    with pytest.raises(CommitRejected):
        realm.call([label, malformed])
    assert realm.head == before and len(realm.entries) == count


def test_occurrence_inverse_reuses_original_images_and_preserves_later_sibling_pins():
    realm = DetachedRealm()
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    a, b = 'occurrence:occ-a/clip:clip-a', 'occurrence:occ-b/clip:clip-a'
    accepted = realm.call([capture_clip_set_params(current(realm), a, values={'opaque': [None, False]})])
    sibling = realm.call([capture_clip_set_params(current(realm), b, values={'other': {'literal': 0}})])
    sibling_document = sibling['data']['canonical_document']
    sibling_occurrence = sibling_document['parent_revision']['payload']['occurrences'][1]
    sibling_shot = next(row for row in sibling_document['shot_revisions']
                        if row['revision_id'] == sibling_occurrence['shot_revision_id'])
    sibling_internal = next(row for row in sibling_document['internal_timeline_revisions']
                            if row['revision_id'] == sibling_shot['internal_timeline_revision_id'])
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    document = undone['data']['canonical_document']
    assert document['parent_revision']['payload']['occurrences'][0] == original[
        'parent_revision']['payload']['occurrences'][0]
    assert document['parent_revision']['payload']['occurrences'][1] == sibling_occurrence
    assert original['shot_revisions'][0] in document['shot_revisions']
    assert original['internal_timeline_revisions'][0] in document['internal_timeline_revisions']
    assert sibling_shot in document['shot_revisions']
    assert sibling_internal in document['internal_timeline_revisions']
    assert State(current(realm)).entity(a) == State(bundle_from_document(original)).entity(a)
    assert State(current(realm)).entity(b)['params'] == {'other': {'literal': 0}}
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == sibling_document
