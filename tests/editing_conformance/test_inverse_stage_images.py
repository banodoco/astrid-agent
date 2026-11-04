"""Inverse effect inputs retain the exact preceding physical closure image."""
from __future__ import annotations

import copy

from astrid.sdk.timeline._commit_patch import CommitPatch
from astrid.sdk.timeline._recording import Recording
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_pristine_initialize import empty_document


def test_inverse_stage_input_keeps_original_canonical_revision():
    document = empty_document()
    bundle = bundle_from_document(document)
    historical = copy.deepcopy(document)
    historical['parent_revision']['revision_id'] = 'earlier-equal-image'
    call = {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1',
            'select': None, 'args': {'publications': ['accepted-undo']}}
    request = {'api_version': 1, 'project_id': document['project_id'], 'timeline_id': 'film',
               'base_revision_id': document['parent_revision']['revision_id'],
               'base_publication_id': 'accepted-undo', 'commands': [call], 'label': 'Redo'}
    metadata = CommitPatch(request, [call], bundle)
    ctx = Recording(bundle, call, 0)
    metadata.before(ctx)
    ctx.inverse_sources = [bundle_from_document(historical)]
    ctx.inverse_stages = [(copy.deepcopy(bundle), copy.deepcopy(bundle)),
                          (copy.deepcopy(bundle), copy.deepcopy(bundle))]
    metadata.after(ctx)
    first, second = metadata.inverse_stages[0]
    assert first[0].images['parent'] == document['parent_revision']
    assert first[1].images['parent'] == historical['parent_revision']
    assert second[0] is first[1]
    assert second[0].images == first[1].images
