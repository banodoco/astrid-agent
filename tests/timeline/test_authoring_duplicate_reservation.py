"""A native duplicate inherits only its verified source's legacy baseline."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import (
    UnsupportedAuthoringEditError,
    publish_authoring_candidate,
)
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_occurrence_duplicate import capture_occurrence_duplicate
from astrid.sdk.timeline_editing import duplicate_authoring_shot
from tests.timeline.test_authoring_publication_migrations import checkout


@pytest.mark.parametrize('shared,selected', [(False, 0), (True, 0), (True, 1)])
@pytest.mark.parametrize('damage', [None, 'reservation', 'owner', 'source', 'baseline',
                                   'created', 'parent', 'offset', 'speed', 'placement-baseline'])
def test_source_bound_duplicate_reservation_and_residual_are_atomic(shared, selected, damage):
    candidate, writer = checkout(shared=shared)
    source = copy.deepcopy(writer.document)
    original = bundle_from_document(source)
    owner = original['placements'][selected]
    call = capture_occurrence_duplicate(original, 'occurrence:' + owner['occurrence_id'],
                                        'new-occ', 'new-shot')
    plan = execute(original, [call])
    intermediate = document_from_bundle(plan.bundle)
    inherited = intermediate['parent_revision']['payload']['occurrences'][-1]
    assert inherited['source_offset'] == owner['source_offset']
    assert {key: value for key, value in inherited.items()
            if key not in {'occurrence_id', 'shot_id', 'shot_revision_id'}} == {
        key: value for key, value in owner.items() if key not in {'occurrence_id', 'shot_id'}}
    assert intermediate['parent_revision']['payload']['occurrences'][:-1] == (
        source['parent_revision']['payload']['occurrences'])
    changed = copy.deepcopy(plan.bundle)
    changed['placements'][-1]['source_offset'] = {'start': 1, 'end': 2}
    with pytest.raises(UnsupportedAuthoringEditError, match='source_offset'):
        document_from_bundle(changed)
    assert original == bundle_from_document(source)

    duplicate = duplicate_authoring_shot(candidate, owner['shot_id'],
                                         new_shot_id='new-shot', occurrence_id='new-occ', start_ms=12)
    for row in (candidate['placements'][-1], candidate['source_mapping']['placements']['new-occ']):
        row['source_offset'] = {'start': 0, 'end': 0}
        row['transform'] = {}
    duplicate['payload']['unknown']['keep'] = 'edited copy'
    duplicate['payload']['items'][0]['metadata']['opaque_literal'] = 'item-1'
    if damage == 'reservation':
        candidate.pop('_authoring_duplicates')
    elif damage == 'owner':
        candidate['_authoring_duplicates'][0]['command']['select'] = (
            'occurrence:' + original['placements'][1 - selected]['occurrence_id'] if shared
            else 'occurrence:missing')
    elif damage == 'source':
        candidate['source_mapping']['shots']['new-shot']['revision_id'] = 'forged'
    elif damage == 'baseline':
        duplicate['base_payload']['unknown']['keep'] = 'forged'
    elif damage == 'created':
        duplicate['_created'] = True
    elif damage == 'parent':
        candidate['base_parent']['publication_id'] = 'forged'
    elif damage == 'offset':
        candidate['placements'][-1]['source_offset'] = {'start': 1, 'end': 2}
    elif damage == 'speed':
        candidate['placements'][-1]['speed'] = 2
    elif damage == 'placement-baseline':
        candidate['base_placements']['new-occ'] = copy.deepcopy(candidate['placements'][-1])
    frozen = copy.deepcopy(candidate)
    history = copy.deepcopy(writer.realm.entries)
    if damage:
        with pytest.raises(ValueError):
            publish_authoring_candidate(candidate, writer, idempotency_key='duplicate-refusal')
        assert writer.calls == []
        assert writer.realm.entries == history
    else:
        saved = publish_authoring_candidate(candidate, writer, idempotency_key='duplicate-residual')
        assert saved['publication']['status'] == 'accepted'
        request = writer.calls[0]
        assert request['commands'][0]['op'] == 'occurrence.duplicate'
        assert request['commands'][0]['select'] == 'occurrence:' + owner['occurrence_id']
        assert all(command['op'] == 'doc.patch' for command in request['commands'][1:])
        assert request['steps'][0]['command_ordinals'] == [0]
        document = saved['publication']['canonical_document']
        rows = document['parent_revision']['payload']['occurrences']
        assert rows[:-1] == source['parent_revision']['payload']['occurrences']
        assert rows[-1]['placement']['start_ms'] == 12
        assert rows[-1]['source_offset'] == {'start': 0, 'end': 0}
        shot = next(row for row in document['shot_revisions'] if row['shot_id'] == 'new-shot')
        assert shot['payload']['unknown']['keep'] == 'edited copy'
        assert shot['payload']['items'][0]['metadata']['opaque_literal'] == 'item-1'
        assert source['shot_revisions'][0] in document['shot_revisions']
        assert source['internal_timeline_revisions'][0] in document['internal_timeline_revisions']
        recorded = writer.realm.entries[-1]['records']
        assert recorded['commands'][0]['bindings']
        assert recorded['steps'][0]['patch']
        assert saved['publication']['receipt']['receipt_id']
    assert candidate == frozen
    assert writer.document == source
