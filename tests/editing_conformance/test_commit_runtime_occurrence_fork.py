"""Registered fork through normal Runtime bootstrap, schema validation and acceptance.

Shared physical-source cases live in test_provider_occurrence_fork; this compact
service case proves the unchanged wire and production allocation/authentication path.
"""
from __future__ import annotations

import pytest

from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


def test_registered_fork_normal_wire_accept_edit_and_guarded_undo(tmp_path):
    use_runtime()
    from runtime_protocol.auth import CredentialStore
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from runtime_protocol.timeline_commit_validation import validate_commit_value

    from astrid.sdk.timeline.capture import capture_track_create
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from astrid.sdk.timeline.native_occurrence_duplicate import capture_occurrence_duplicate
    from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    credentials = CredentialStore(tmp_path / 'credentials')
    token, _ = credentials.provision('fork-editor', ['projects:read', 'projects:write'])

    def authorize():
        return credentials.require(token, 'projects:write')

    try:
        project = service.create_project({'slug': 'fork', 'name': 'Fork'}, idempotency_key='project')['id']
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        service.create_timeline(project, 'film', idempotency_key='bootstrap')

        def document():
            head = service.get_project_timeline(project, 'film')
            return service._timeline_commits()[0].revision_closure(project, 'film', head['head_revision_id'])

        def commit(call, key, *, base=None):
            head = base or service.get_project_timeline(project, 'film')
            body = {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                    'base_revision_id': head['head_revision_id'],
                    'base_publication_id': head['head_publication_id'], 'commands': [call],
                    'label': key, 'steps': [{'id': key, 'label': key, 'intent': '', 'tags': [],
                                          'command_ordinals': [0], 'patch': [], 'reads': []}],
                    'merge': 'fields', 'reads': [], 'dry_run': False, 'source': {'via': 'app'},
                    'rationale': 'fork selected child', 'group_id': 'gesture-1'}
            validate_commit_value(body, 'CommitRequest')
            result = service.commit_timeline(project, 'film', body, idempotency_key=key,
                publisher='fork-editor', invocation_path=f'/v1/projects/{project}/timelines/film/commit',
                authorize=authorize)
            validate_commit_value(result['data'], 'CommitAccepted')
            return result

        def bundle():
            return bundle_from_document(document())

        commit(capture_track_create(bundle(), 'parent', {'id': 'picture', 'kind': 'visual'}), 'track')
        commit(capture_occurrence_create(bundle(), 'selected', 'source-shot', name='Source',
                                         track='picture', on=0, duration=10), 'birth')
        commit(capture_occurrence_duplicate(bundle(), 'occurrence:selected', 'sibling', 'sibling-shot'), 'sibling')
        captured = capture_occurrence_fork(bundle(), 'occurrence:selected', 'fork-shot')
        # The App can send this exact native envelope; reads are reconstructed by the provider.
        fork_call = {key: captured[key] for key in (
            'schema_version', 'op', 'version', 'select', 'args', 'intent', 'created')}
        fork_base = service.get_project_timeline(project, 'film')
        commit({'schema_version': 1, 'op': 'track.set', 'version': '1', 'select': 'parent/track:picture',
                'args': {'values': {'label': 'Other edit'}, 'clear': []}}, 'disjoint')
        before = document()
        fork = commit(fork_call, 'fork', base=fork_base)
        assert fork['data']['status'] == 'merged'
        after = document()
        rows = after['parent_revision']['payload']['occurrences']
        assert len(rows) == 2 and rows[0]['occurrence_id'] == 'selected'
        assert rows[1] == before['parent_revision']['payload']['occurrences'][1]
        assert rows[0]['shot_id'] == 'fork-shot'
        assert fork['data']['id_map'] == {}
        undone = commit({'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
                         'args': {'publications': [fork['receipt']['receipt_id']]}}, 'undo-fork')
        assert document() == before
        redone = commit({'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
                         'args': {'publications': [undone['receipt']['receipt_id']]},
                         'created': ['shot:fork-shot']}, 'redo-fork')
        assert document() == after
        edit = commit({'schema_version': 1, 'op': 'shot.set', 'version': '1',
                       'select': 'occurrence:selected/shot',
                       'args': {'values': {'name': 'Edited'}, 'clear': []}}, 'edit')
        edited = document()
        assert edited['parent_revision']['payload']['occurrences'][1] == rows[1]
        assert next(row for row in edited['shot_revisions'] if row['shot_id'] == 'sibling-shot') == next(
            row for row in before['shot_revisions'] if row['shot_id'] == 'sibling-shot')
        assert edit['data']['status'] == 'accepted'
        with pytest.raises(RuntimeErrorBase):
            commit({'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
                    'args': {'publications': [redone['receipt']['receipt_id']]}}, 'dependent-fork-undo')
        assert document() == edited
    finally:
        service.close()
