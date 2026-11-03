"""Authenticated restoration preserves literal identities and physical closure."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import present
from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document
from tests.editing_conformance.test_doc_patch_removal_guards import (
    document,
    remove_call,
    send,
)
from tests.editing_conformance.test_doc_patch_v2 import change, patch
from tests.editing_conformance.test_provider_step_undo_v2 import undo
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


@pytest.mark.parametrize('identity', ['clip/a', 'clip::a', 'clip:a'])
@pytest.mark.parametrize('scope', ['both', 'shared'])
def test_literal_member_removal_authenticated_undo_redo_is_exact(identity, scope):
    original = document()
    parent = original['parent_revision']['payload']
    if scope == 'shared':
        image = original['internal_timeline_revisions'][0]
        image['payload']['clips'][0]['id'] = identity
        image['content_digest'] = _digest(image['payload'])
        original['dependency_manifest'] = _manifest(
            original['shot_revisions'], original['internal_timeline_revisions'],
            original['dependency_manifest']['media'], original['dependency_manifest']['documents'])
        ref, path = 'internal-revision:' + image['revision_id'], ['clips']
    else:
        for payload in (parent, parent['config']):
            payload['clips'][0]['id'] = identity
        original['parent_revision']['content_digest'] = _digest(parent)
        ref, path = 'parent', ['clips']
    realm = DetachedRealm(original)
    landed = send(realm, remove_call(bundle_from_document(original), path, ref))
    accepted = copy.deepcopy(realm.entries[-1])
    undone = send(realm, undo(landed['receipt']['receipt_id']))
    assert undone['data']['canonical_document'] == original
    redone = send(realm, undo(undone['receipt']['receipt_id']))
    assert redone['data']['canonical_document'] == landed['data']['canonical_document']
    assert realm.entries[1] == accepted


@pytest.mark.parametrize('identity', ['clip/a', 'clip::a', 'clip:a'])
def test_literal_restoration_keeps_intervening_aba_guard(identity):
    original = document()
    parent = original['parent_revision']['payload']
    for payload in (parent, parent['config']):
        payload['clips'][0]['id'] = identity
    original['parent_revision']['content_digest'] = _digest(parent)
    realm = DetachedRealm(original)
    landed = send(realm, remove_call(bundle_from_document(original)))
    undone = send(realm, undo(landed['receipt']['receipt_id']))
    changed = copy.deepcopy(parent['clips'])
    changed[0]['app']['org.example']['literal'] = 'later author'
    send(realm, patch(change(['clips'], present(parent['clips']), changed)))
    send(realm, patch(change(['clips'], present(changed), parent['clips'])))
    snapshot = copy.deepcopy((realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared))
    with pytest.raises(CommitRejected) as error:
        send(realm, undo(undone['receipt']['receipt_id']))
    assert error.value.failure['details']['reason'] == 'intervening_write'
    assert (realm.head, realm.entries, realm.documents, realm.accepted, realm.prepared) == snapshot
