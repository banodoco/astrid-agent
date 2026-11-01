from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError, publish_authoring_candidate
from astrid.sdk.timeline.authoring_commit import authoring_commit_request
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, create_runtime_commit_service
from astrid.sdk.timeline_editing import (
    duplicate_authoring_shot,
    move_occurrence_group,
    remove_authoring_shot,
)
from tests.sdk.test_timeline_commit_provider import DetachedPort, DetachedRealm
from tests.sdk.test_timeline_commit_provider_document import document_fixture


class Writer:
    def __init__(self, document):
        self.realm = DetachedRealm(document)
        self.document = document
        self.calls = []

    def list_timeline_commits(self, project, timeline, **kwargs):
        return {'items': copy.deepcopy(self.realm.entries), 'next_cursor': None}

    def get_project_parent_composition_revision(self, project, timeline, revision):
        return {**copy.deepcopy(self.document['parent_revision']), 'project_id': project,
                'timeline_id': timeline, 'dependency_manifest': self.document['dependency_manifest']}

    def get_project_shot_revision(self, project, shot, revision):
        return {**copy.deepcopy(next(row for row in self.document['shot_revisions']
                                    if row['revision_id'] == revision)), 'project_id': project}

    def get_project_timeline_revision(self, project, timeline, revision):
        return {**copy.deepcopy(next(row for row in self.document['internal_timeline_revisions']
                                    if row['revision_id'] == revision)),
                'project_id': project, 'timeline_id': timeline}

    def commit_timeline(self, project, timeline, body, *, idempotency_key):
        self.calls.append(copy.deepcopy(body))
        context = self.realm.context(body['commands'])
        context['request'] = copy.deepcopy(body)
        context['invocation']['body'] = copy.deepcopy(body)
        context['request_hash'] = _digest(context['invocation'])
        context['idempotency_key'] = idempotency_key
        return create_runtime_commit_service().commit(context, DetachedPort(self.realm, context))


def checkout(*, shared=False):
    document = document_fixture(shared=shared)
    candidate = bundle_from_document(document)
    return candidate, Writer(document)


def test_unchanged_publication_is_one_guarded_commit_and_one_compilation(monkeypatch):
    import astrid.core.timeline.authoring_bundle as authoring

    candidate, writer = checkout()
    original = copy.deepcopy(candidate)
    compile_candidate = authoring.compile_authoring_candidate
    calls = []

    def compile_once(value):
        if value is candidate:
            calls.append(value)
        return compile_candidate(value)

    monkeypatch.setattr(authoring, 'compile_authoring_candidate', compile_once)
    monkeypatch.setattr('astrid.sdk.timeline.authoring_commit.compile_authoring_candidate', compile_once)
    result = publish_authoring_candidate(candidate, writer, idempotency_key='unchanged')
    assert len(calls) == len(writer.calls) == 1
    assert writer.calls[0]['commands']
    assert writer.calls[0]['merge'] == 'atomic'
    assert result['publication']['canonical_document'] == writer.document
    assert result['publication']['receipt']['command_kind'] == 'timeline.commit'
    assert result['publication']['data']['canonical_document'] == writer.document
    assert result['publication']['data']['new_head'] == result['publication']['new_head']
    assert result['summary']['authored_change_count'] == 0
    assert writer.realm.entries[-1]['records']['commands'][0]['reads']
    assert candidate == original


@pytest.mark.parametrize('shared', [False, True])
def test_removed_occurrence_uses_registered_delete_and_preserves_shared_sibling(shared):
    candidate, writer = checkout(shared=shared)
    removed = candidate['placements'][0]
    remove_authoring_shot(candidate, removed['shot_id'])
    result = publish_authoring_candidate(candidate, writer, idempotency_key='remove')
    assert writer.calls[0]['commands'][0]['op'] == 'occurrence.delete'
    document = result['publication']['canonical_document']
    assert [row['occurrence_id'] for row in document['parent_revision']['payload']['occurrences']] == (
        ['occ-b'] if shared else [])
    assert document['shot_revisions'] == (writer.document['shot_revisions'] if shared else [])
    assert document['internal_timeline_revisions'] == (
        writer.document['internal_timeline_revisions'] if shared else [])
    record = writer.realm.entries[-1]['records']['commands'][0]
    assert record['reads'] and record['bindings'] and record['writes']


def test_unplaced_local_copy_is_filtered_but_reachable_edit_is_published():
    candidate, writer = checkout()
    local = candidate['placements'][0]['shot_id']
    duplicate_authoring_shot(candidate, local, new_shot_id='unplaced', occurrence_id='unused')
    candidate['placements'].pop()
    candidate['shots']['unplaced']['payload']['name'] = 'local only'
    candidate['shots'][local]['payload']['unknown']['keep'] = 'reachable'
    result = publish_authoring_candidate(candidate, writer, idempotency_key='filter')
    document = result['publication']['canonical_document']
    assert len(document['shot_revisions']) == 1
    assert document['shot_revisions'][0]['payload']['unknown']['keep'] == 'reachable'
    assert document['shot_revisions'][0]['payload']['name'] == 'Chapter One'
    assert candidate['shots']['unplaced']['payload']['name'] == 'local only'


def test_reorder_emits_source_bound_list_move_and_keyed_timing_changes():
    candidate, writer = checkout(shared=True)
    move_occurrence_group(candidate, 'occ-b', before_occurrence_id='occ-a')
    request = authoring_commit_request(candidate, writer)
    changes = [change for command in request['commands'] for change in command['args']['changes']]
    # feb0236f4: keyed timing edits are emitted first; the list.move follows in its own command.
    assert changes[-1]['op'] == 'list.move'
    assert changes[-1]['order_before'] == ['occ-a', 'occ-b']
    assert changes[-1]['before']['value']['shot_id'] == 'chapter-shot'
    assert [change['path'] for change in changes[:-1]] == [
        ['occurrences', 'occurrence_id:occ-b', 'placement', 'start_ms'],
        ['occurrences', 'occurrence_id:occ-a', 'placement', 'start_ms'],
    ]
    result = writer.commit_timeline('project-1', 'film', request, idempotency_key='reorder')
    rows = result['data']['canonical_document']['parent_revision']['payload']['occurrences']
    assert [row['occurrence_id'] for row in rows] == ['occ-b', 'occ-a']
    assert [row['placement']['start_ms'] for row in rows] == [0, 10000]


@pytest.mark.parametrize('damage', ['source', 'baseline', 'owner'])
def test_reachable_provenance_and_ownership_are_not_filtered(damage):
    candidate, writer = checkout()
    local = candidate['placements'][0]['shot_id']
    if damage == 'source':
        candidate['source_mapping']['shots'][local]['revision_id'] = 'foreign'
    elif damage == 'baseline':
        candidate['shots'][local]['base_payload']['unknown']['keep'] = 'forged'
    else:
        candidate['placements'][0]['shot_id'] = 'foreign'
    with pytest.raises((AuthoringBundleError, ValueError)):
        authoring_commit_request(candidate, writer)
    assert writer.calls == []


def test_generic_patch_still_cannot_remove_managed_occurrence():
    candidate, _ = checkout()
    with pytest.raises(ValueError, match='closure membership'):
        execute(candidate, [{'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
                             'args': {'changes': [{'op': 'list.remove', 'ref': 'parent',
                                 'path': ['occurrences'], 'id': 'occ-a',
                                 'before': {'state': 'present', 'value': candidate['placements'][0]},
                                 'order_before': ['occ-a']}]}}])


def test_unchanged_preview_has_null_receipt_and_stale_atomic_commit_is_refused():
    from runtime_protocol.timeline_commit import CommitRejected

    candidate, writer = checkout()
    request = authoring_commit_request(candidate, writer)
    preview = writer.commit_timeline('project-1', 'film', {**request, 'dry_run': True},
                                     idempotency_key='preview')
    assert preview['receipt'] is None
    assert preview['data']['status'] == 'preview'
    assert preview['data']['would_status'] == 'accepted'
    assert writer.realm.head['publication_id'] == 'bootstrap'
    writer.realm.call([writer.realm.move()])
    before = copy.deepcopy((writer.realm.head, writer.realm.entries, writer.realm.documents))
    with pytest.raises(CommitRejected) as refusal:
        writer.commit_timeline('project-1', 'film', request, idempotency_key='stale')
    assert refusal.value.failure['details']['code'] == 'atomic_scope_changed'
    assert (writer.realm.head, writer.realm.entries, writer.realm.documents) == before


def test_semantic_removal_undo_restores_exact_closure():
    from tests.sdk.test_timeline_commit_provider import inverse

    candidate, writer = checkout(shared=True)
    remove_authoring_shot(candidate, candidate['placements'][0]['shot_id'])
    saved = publish_authoring_candidate(candidate, writer, idempotency_key='delete-undo')
    restored = writer.realm.call([inverse([saved['publication']['receipt']['receipt_id']])])
    assert restored['data']['canonical_document'] == writer.document
