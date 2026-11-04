"""Authoring semantic births must support residual edits in the same commit."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import publish_authoring_candidate
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork
from astrid.sdk.timeline_editing import duplicate_authoring_shot
from tests.timeline.test_authoring_publication_migrations import checkout


@pytest.mark.parametrize('selected', [0, 1], ids=['first', 'second'])
def test_authoring_item_edit_matches_registered_fork_then_reopened_patch(selected):
    candidate, writer = checkout(shared=True)
    source = copy.deepcopy(writer.document)
    placement = candidate['placements'][selected]
    owner = 'occurrence:' + placement['occurrence_id']
    local = placement['shot_id']
    candidate['shots'][local]['payload']['items'][0]['metadata']['opaque_literal'] = 'authored'
    frozen = copy.deepcopy(candidate)

    # The existing registered fork and a patch against its reopened output
    # already support this edit. The production caller must do it atomically.
    before = bundle_from_document(source)
    call = capture_occurrence_fork(before, owner, 'reference-fork')
    forked = bundle_from_document(document_from_bundle(execute(before, [call]).bundle))
    after = copy.deepcopy(forked)
    after['shots']['reference-fork']['payload']['items'][0]['metadata']['opaque_literal'] = 'authored'
    patches = emit_document_commands(forked, after)
    reference = document_from_bundle(execute(forked, patches, commit_request={}).bundle)
    sibling = source['parent_revision']['payload']['occurrences'][1 - selected]
    assert reference['parent_revision']['payload']['occurrences'][1 - selected] == sibling
    assert source['shot_revisions'][0] in reference['shot_revisions']
    assert source['internal_timeline_revisions'][0] in reference['internal_timeline_revisions']

    saved = publish_authoring_candidate(candidate, writer, idempotency_key='fork-item')
    document = saved['publication']['canonical_document']
    assert saved['publication']['status'] == 'accepted'
    assert writer.calls[0]['commands'][0]['op'] == 'occurrence.fork'
    selected_row = document['parent_revision']['payload']['occurrences'][selected]
    shot = next(row for row in document['shot_revisions']
                if row['revision_id'] == selected_row['shot_revision_id'])
    assert shot['payload']['items'][0]['metadata']['opaque_literal'] == 'authored'
    assert document['parent_revision']['payload']['occurrences'][1 - selected] == sibling
    assert source['shot_revisions'][0] in document['shot_revisions']
    assert source['internal_timeline_revisions'][0] in document['internal_timeline_revisions']
    assert candidate == frozen


def test_authoring_fork_nonitem_edit_keeps_exact_sibling():
    candidate, writer = checkout(shared=True)
    source = copy.deepcopy(writer.document)
    local = candidate['placements'][0]['shot_id']
    candidate['shots'][local]['payload']['unknown']['keep'] = 'authored'

    saved = publish_authoring_candidate(candidate, writer, idempotency_key='fork-metadata')

    document = saved['publication']['canonical_document']
    assert writer.calls[0]['commands'][0]['op'] == 'occurrence.fork'
    assert saved['publication']['status'] == 'accepted'
    selected = document['parent_revision']['payload']['occurrences'][0]
    shot = next(row for row in document['shot_revisions']
                if row['revision_id'] == selected['shot_revision_id'])
    assert shot['payload']['unknown']['keep'] == 'authored'
    assert document['parent_revision']['payload']['occurrences'][1] == source[
        'parent_revision']['payload']['occurrences'][1]
    assert source['shot_revisions'][0] in document['shot_revisions']
    assert source['internal_timeline_revisions'][0] in document['internal_timeline_revisions']


def test_production_authoring_duplicate_records_semantic_birth_and_residual():
    candidate, writer = checkout()
    source = copy.deepcopy(writer.document)
    duplicate = duplicate_authoring_shot(candidate, candidate['placements'][0]['shot_id'],
                                         new_shot_id='new-shot', occurrence_id='new-occ')
    # A new occurrence uses admitted defaults, as in the original residual test.
    for row in (candidate['placements'][-1], candidate['source_mapping']['placements']['new-occ']):
        row['source_offset'] = {'start': 0, 'end': 0}
        row['transform'] = {}
    duplicate['payload']['unknown']['keep'] = 'edited copy'
    frozen = copy.deepcopy(candidate)

    saved = publish_authoring_candidate(candidate, writer, idempotency_key='duplicate-residual')

    document = saved['publication']['canonical_document']
    assert saved['publication']['status'] == 'accepted'
    assert writer.calls[0]['commands'][0]['op'] == 'occurrence.duplicate'
    created = next(row for row in document['parent_revision']['payload']['occurrences']
                   if row['occurrence_id'] == 'new-occ')
    shot = next(row for row in document['shot_revisions']
                if row['revision_id'] == created['shot_revision_id'])
    assert shot['payload']['unknown']['keep'] == 'edited copy'
    assert source['shot_revisions'][0] in document['shot_revisions']
    assert source['internal_timeline_revisions'][0] in document['internal_timeline_revisions']
    assert saved['summary']['occurrence_changes']['added'] == 1
    assert saved['summary']['other_properties_changed'] is True
    assert candidate == frozen
