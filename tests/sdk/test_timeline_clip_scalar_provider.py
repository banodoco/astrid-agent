"""Native provider acceptance of the preserved App's three bounded raw edits."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._scalar_commands import capture_track_set
from astrid.sdk.timeline.commit import _manifest, bundle_from_document
from tests.editing_conformance.test_recipe_clip_scalar_fields import (
    CHILD,
    CLEAR,
    PARENT,
    app_calls,
    app_closure,
    clip,
)
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedRealm,
    inverse,
    runtime_abi,  # noqa: F401 (shared autouse Runtime ABI fixture)
)
from tests.timeline.test_authoring_bundle import AUDIO, OLD


def app_document():
    parent, shots, internals = app_closure()
    for row in (parent, *shots, *internals):
        row.pop('project_id')
    parent.pop('timeline_id')
    return {'schema_version': 1, 'project_id': 'project-1', 'timeline_id': 'film',
            'parent_revision': parent, 'shot_revisions': shots,
            'internal_timeline_revisions': internals,
            'dependency_manifest': _manifest(shots, internals, [
                {'media_id': value, 'content_digest': value} for value in (OLD, AUDIO)], [])}


def current(realm):
    return bundle_from_document(realm.documents[realm.head['revision_id']])


def test_original_app_fields_provider_reload_exact_inverse_redo_and_disjoint_rebase():
    original = app_document()
    realm = DetachedRealm(original)
    base = copy.deepcopy(realm.head)
    calls = app_calls(current(realm))
    realm.call([capture_track_set(current(realm), 'parent/track:V1', values={'label': 'App A'})])
    before = copy.deepcopy(realm.documents[realm.head['revision_id']])
    accepted = realm.call(calls, base=base)
    assert accepted['data']['commands'] == calls
    assert clip(current(realm), CHILD)['hold'] == 4
    assert 'asset' not in clip(current(realm), CLEAR)
    assert 'transition' not in clip(current(realm), PARENT)
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == before
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']


@pytest.mark.parametrize('ref,field', [(CHILD, 'hold'), (CLEAR, 'asset'), (PARENT, 'transition')])
@pytest.mark.parametrize('aba', [False, True])
def test_provider_unchanged_publication_and_aba_refuse_old_inverse(ref, field, aba):
    realm = DetachedRealm(app_document())
    calls = {'values': {'hold': 3}} if field == 'hold' else {'clear': [field]}
    first = realm.call([capture_clip_set(current(realm), ref, **calls)])
    if aba:
        away = ({'values': {'hold': 4}} if field == 'hold' else {'clear': ['hold']})
        # Clear on a windowless child cannot be evaluated. A guarded reversing
        # publication provides the real A→B→A edge for the cleared fields too.
        if field == 'hold':
            changed = realm.call([capture_clip_set(current(realm), ref, **away)])
        else:
            changed = realm.call([inverse([first['receipt']['receipt_id']])])
        realm.call([inverse([changed['receipt']['receipt_id']])])
    last = realm.call([capture_clip_set(current(realm), ref, **calls)])
    assert first['receipt']['receipt_id'] != last['receipt']['receipt_id']
    snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([first['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert snapshot == (realm.head, realm.entries, realm.documents)


@pytest.mark.parametrize('ref,on', [(PARENT, 16), (CHILD, 4)])
@pytest.mark.parametrize('separate', [False, True])
def test_move_then_hold_accepts_original_capture_and_generic_inverse_stages(ref, on, separate):
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
    from astrid.sdk.timeline.capture import capture_move
    from astrid.sdk.timeline.commands import execute

    realm = DetachedRealm(app_document())
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    move = capture_move(current(realm), ref, on)
    local = execute(current(realm), [move]).bundle
    edit = capture_clip_set(local, ref, values={'hold': 4})
    sealed = copy.deepcopy(edit)
    sibling = copy.deepcopy(clip(current(realm), 'occurrence:shared-sibling/clip:f2-agent-clip'))
    if separate:
        moved = realm.call([move])
        baseline = copy.deepcopy(moved['data']['canonical_document'])
        base = copy.deepcopy(realm.head)
        realm.call([capture_clip_set_params(current(realm), ref, values={'opaqueSibling': [None, False]})])
        before = copy.deepcopy(realm.documents[realm.head['revision_id']])
        accepted = realm.call([edit], base=base)
        assert accepted['data']['status'] == 'rebased'
        assert accepted['data']['commands'] == [sealed]
        snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents))
        with pytest.raises(CommitRejected):
            realm.call([inverse([moved['receipt']['receipt_id']])])
        assert snapshot == (realm.head, realm.entries, realm.documents)
    else:
        before = original
        accepted = realm.call([move, edit])
        assert accepted['data']['commands'] == [move, sealed]
    assert clip(current(realm), ref)['hold'] == 4
    assert clip(current(realm), ref)['at'] == on
    assert clip(current(realm), ref)['app']['astrid'] == {'fixed_start': True, 'for': 4}
    assert clip(current(realm), 'occurrence:shared-sibling/clip:f2-agent-clip') == sibling
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert undone['data']['canonical_document'] == before
    redone = realm.call([inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']
    if separate:
        assert before != baseline  # The disjoint parameter survives guarded undo/redo.


@pytest.mark.parametrize('aba', [False, True])
def test_hold_after_move_records_unchanged_for_and_refuses_publication_aba(aba):
    from astrid.sdk.timeline.capture import capture_move

    realm = DetachedRealm(app_document())
    realm.call([capture_move(current(realm), CHILD, 4)])
    first = realm.call([capture_clip_set(current(realm), CHILD, values={'hold': 3})])
    assert any(w['path'] == ['app', 'astrid', 'for'] and w['before'] == w['after']
               for w in realm.entries[-1]['records']['commands'][0]['writes'])
    if aba:
        realm.call([capture_clip_set(current(realm), CHILD, values={'hold': 4})])
    realm.call([capture_clip_set(current(realm), CHILD, values={'hold': 3})])
    snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected) as error:
        realm.call([inverse([first['receipt']['receipt_id']])])
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert snapshot == (realm.head, realm.entries, realm.documents)


@pytest.mark.parametrize('ref,on', [(PARENT, 16), (CHILD, 4)])
def test_hold_captured_after_move_refuses_changed_basis_without_recapture(ref, on):
    from astrid.sdk.timeline.capture import capture_move

    realm = DetachedRealm(app_document())
    realm.call([capture_move(current(realm), ref, on)])
    sealed = capture_clip_set(current(realm), ref, values={'hold': 4})
    realm.call([capture_clip_set(current(realm), ref, values={'hold': 5})])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected):
        realm.call([sealed])
    assert before == (realm.head, realm.entries, realm.documents)


def test_appearance_normalizes_owner_in_same_provider_commit():
    document = app_document()
    payload = document['parent_revision']['payload']
    payload['config']['output']['resolution'] = '1280x720'
    payload['clips'][0]['x'] = 10
    payload['config']['clips'][0]['x'] = 10
    from astrid.sdk.timeline.commit import _digest
    document['parent_revision']['content_digest'] = _digest(payload)
    realm = DetachedRealm(document)
    captured = capture_clip_set(current(realm), PARENT, values={'fit': 'contain'})
    accepted = realm.call([captured])
    updated = current(realm)
    assert clip(updated, PARENT)['fit'] == 'contain'
    assert clip(updated, 'parent/clip:f2-app-clip')['fit'] == 'cover'
    assert updated['parent']['config']['tracks'][0]['app']['clipPlacementVersion'] == 1
    undone = realm.call([inverse([accepted['receipt']['receipt_id']])])
    restored = bundle_from_document(undone['data']['canonical_document'])
    assert 'fit' not in clip(restored, PARENT)
    assert 'fit' not in clip(restored, 'parent/clip:f2-app-clip')


def test_appearance_stale_owner_dependency_refuses_without_publication():
    document = app_document()
    payload = document['parent_revision']['payload']
    payload['config']['output']['resolution'] = '1280x720'
    from astrid.sdk.timeline.commit import _digest
    document['parent_revision']['content_digest'] = _digest(payload)
    realm = DetachedRealm(document)
    base = copy.deepcopy(realm.head)
    captured = capture_clip_set(current(realm), PARENT, values={'fit': 'cover'})
    realm.call([capture_track_set(current(realm), 'parent/track:V1', values={'label': 'Changed'})])
    prior = copy.deepcopy(realm.head)
    with pytest.raises(CommitRejected):
        realm.call([captured], base=base)
    assert realm.head == prior


def test_child_appearance_materializes_authored_parent_canvas_atomically():
    from astrid.sdk.timeline.commit import _digest
    document = app_document()
    parent = document['parent_revision']['payload']
    parent['config']['theme_overrides'] = {'visual': {'canvas': {'width': 1280, 'height': 720, 'fps': 24}}}
    parent['config'].pop('output', None)
    document['parent_revision']['content_digest'] = _digest(parent)
    child = document['internal_timeline_revisions'][0]
    child['payload']['output'] = {'resolution': '1920x1080', 'fps': 24, 'file': 'child.mp4'}
    child['content_digest'] = _digest(child['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'], document['internal_timeline_revisions'], [
        {'media_id': value, 'content_digest': value} for value in (OLD, AUDIO)], [])
    realm = DetachedRealm(document)
    command = capture_clip_set(current(realm), CHILD, values={'fit': 'cover'})
    accepted = realm.call([command])
    assert current(realm)['parent']['config']['output'] == {'resolution': '1280x720', 'fps': 24, 'file': 'output.mp4'}
    restored = realm.call([inverse([accepted['receipt']['receipt_id']])])
    assert 'output' not in bundle_from_document(restored['data']['canonical_document'])['parent']['config']
    redone = realm.call([inverse([restored['receipt']['receipt_id']])])
    assert bundle_from_document(redone['data']['canonical_document'])['parent']['config']['output']['resolution'] == '1280x720'
