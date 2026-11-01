"""Legacy null mirrors survive the real emitter, commit and inverse boundaries."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._doc_patch import validate_payload
from astrid.sdk.timeline.commit import (
    _digest,
    _verify_document,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.emitter import DocumentPatchError, emit_document_commands
from astrid.sdk.timeline.validation import FinalCandidateError, validate_final_candidate
from tests.editing_conformance.test_doc_patch_v2 import submit
from tests.editing_conformance.test_recipe_m1_commands import removal_commit
from tests.sdk.test_timeline_commit_provider import DetachedRealm, inverse
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture


def source(null_side):
    document = document_fixture()
    image = document['parent_revision']
    scope = image['payload'] if null_side == 'payload' else image['payload']['config']
    scope['clips'] = None
    image['content_digest'] = _digest(image['payload'])
    _verify_document(document)
    return document


@pytest.mark.parametrize('null_side', ['payload', 'config'])
@pytest.mark.parametrize('edit', ['field', 'remove'])
def test_emitted_live_list_edit_commits_and_exactly_undoes_redoes(null_side, edit):
    document = source(null_side)
    before = bundle_from_document(document)
    after = copy.deepcopy(before)
    rows = after['parent']['config']['clips'] if null_side == 'payload' else after['parent']['clips']
    if edit == 'field':
        rows[0]['label'] = 'Retained null mirror'
    else:
        rows.pop(0)
    untouched = copy.deepcopy((before, after))
    commands = emit_document_commands(before, after)
    assert commands
    assert (before, after) == untouched
    realm = DetachedRealm(document)
    accepted = removal_commit(realm, commands, 'Edit live list')
    assert accepted['data']['status'] == 'accepted'
    assert accepted['data']['canonical_document'] == document_from_bundle(after)
    publication = accepted['receipt']['receipt_id']
    undone = submit(realm, inverse([publication]))
    assert undone['data']['canonical_document'] == document
    redone = submit(realm, inverse([undone['receipt']['receipt_id']]))
    assert redone['data']['canonical_document'] == accepted['data']['canonical_document']


@pytest.mark.parametrize('null_side', ['payload', 'config'])
def test_changed_live_list_is_validated_without_mutating_raw_null(null_side):
    baseline = bundle_from_document(source(null_side))
    candidate = copy.deepcopy(baseline)
    snapshot = copy.deepcopy(candidate)
    validate_final_candidate(candidate, baseline=baseline)
    assert candidate == snapshot
    rows = candidate['parent']['config']['clips'] if null_side == 'payload' else candidate['parent']['clips']
    rows[0]['track'] = 'missing-track'
    with pytest.raises(FinalCandidateError, match='existing track'):
        validate_final_candidate(candidate, baseline=baseline)


@pytest.mark.parametrize('side', ['payload', 'config'])
def test_new_null_mirror_is_rejected_by_validator_and_emitter(side):
    before = bundle_from_document(document_fixture())
    after = copy.deepcopy(before)
    scope = after['parent'] if side == 'payload' else after['parent']['config']
    scope['clips'] = None
    with pytest.raises(FinalCandidateError, match='must be a list'):
        validate_final_candidate(after, baseline=before)
    with pytest.raises(DocumentPatchError, match='null parent clip mirror'):
        emit_document_commands(before, after)


@pytest.mark.parametrize('damage', ['scalar', 'empty', 'duplicate', 'registry', 'null-only'])
def test_malformed_mirrors_still_refuse(damage):
    before = bundle_from_document(document_fixture())
    after = copy.deepcopy(before)
    parent = after['parent']
    if damage == 'scalar':
        parent['config']['clips'] = False
    elif damage == 'empty':
        parent['config']['clips'] = []
    elif damage == 'duplicate':
        parent['clips'].append(copy.deepcopy(parent['clips'][0]))
        parent['config']['clips'] = copy.deepcopy(parent['clips'])
    elif damage == 'registry':
        parent['config']['registry'] = None
    else:
        parent['clips'] = None
        parent['config'].pop('clips')
    with pytest.raises(CommandError):
        validate_payload('parent', parent)
    with pytest.raises(FinalCandidateError):
        validate_final_candidate(after, baseline=before)
    with pytest.raises(DocumentPatchError):
        emit_document_commands(before, after)


@pytest.mark.parametrize('side', ['payload', 'config'])
def test_absent_mirror_remains_valid(side):
    candidate = bundle_from_document(document_fixture())
    scope = candidate['parent'] if side == 'payload' else candidate['parent']['config']
    scope.pop('clips')
    snapshot = copy.deepcopy(candidate)
    validate_final_candidate(candidate, baseline=snapshot)
    assert emit_document_commands(candidate, snapshot) == []
    assert candidate == snapshot
