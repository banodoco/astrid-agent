"""Native fork provenance carries item edits through one command batch."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork
from tests.timeline.test_authoring_publication_migrations import checkout


@pytest.mark.parametrize('damage', [None, 'json', 'source-pin', 'owner', 'map', 'foreign',
                                   'stale', 'item-id'])
def test_native_fork_item_continuation_matches_reopened_boundary(damage):
    original, writer = checkout(shared=True)
    owner = 'occurrence:' + original['placements'][0]['occurrence_id']
    call = capture_occurrence_fork(original, owner, 'native-fork')
    forked = execute(original, [call]).bundle
    opened = bundle_from_document(document_from_bundle(forked))
    after = copy.deepcopy(opened)
    after['shots']['native-fork']['payload']['items'][0]['metadata']['opaque_literal'] = 'edited'
    patches = emit_document_commands(opened, after)
    expected = document_from_bundle(execute(opened, patches, commit_request={}).bundle)
    current = forked['shots']['native-fork']
    if damage == 'json':
        forked['shots']['native-fork'] = json.loads(json.dumps(current))
    elif damage == 'source-pin':
        current.source['source_revision_id'] = 'unverified'
    elif damage == 'owner':
        current.source['occurrence_id'] = original['placements'][1]['occurrence_id']
    elif damage == 'map':
        forked['source_mapping']['shots']['native-fork'] = copy.deepcopy(
            next(iter(original['source_mapping']['shots'].values())))
    elif damage == 'foreign':
        forked['source_mapping']['_runtime_document']['project_id'] = 'foreign'
    elif damage == 'stale':
        forked['base_parent']['revision_id'] = 'stale'
    elif damage == 'item-id':
        current['payload']['items'][0]['item_id'] = 'unverified'
    frozen = copy.deepcopy(forked)
    if damage is not None:
        with pytest.raises(ValueError):
            execute(forked, patches, commit_request={})
    else:
        request = {'label': 'Fork and edit', 'steps': [
            {'id': 'fork', 'label': 'Fork', 'intent': '', 'tags': [],
             'command_ordinals': [0], 'patch': []},
            {'id': 'edit', 'label': 'Edit items', 'intent': '', 'tags': [],
             'command_ordinals': list(range(1, len(patches) + 1)),
             'patch': [change for patch in patches for change in patch['args']['changes']]}]}
        assert document_from_bundle(execute(original, [call, *patches], commit_request=request).bundle) == expected
        assert document_from_bundle(execute(forked, patches, commit_request={}).bundle) == expected
        assert expected['parent_revision']['payload']['occurrences'][1] == writer.document[
            'parent_revision']['payload']['occurrences'][1]
        assert writer.document['shot_revisions'][0] in expected['shot_revisions']
        assert writer.document['internal_timeline_revisions'][0] in expected['internal_timeline_revisions']
    assert forked == frozen
