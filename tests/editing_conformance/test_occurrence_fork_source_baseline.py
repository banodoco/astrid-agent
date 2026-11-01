"""Retained App media metadata survives a registered occurrence-local copy."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError, _copy_shot_for_placement
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.emitter import DocumentPatchError, emit_document_commands
from astrid.sdk.timeline.native_occurrence_fork import capture_occurrence_fork
from tests.editing_conformance.test_recipe_clip_scalar_fields import app_closure
from tests.editing_conformance.test_recipe_m1_commands import inverse
from tests.editing_conformance.test_recipe_occurrence_duplicate import accepted
from tests.helpers.trusted_seed import _closure
from tests.timeline.test_authoring_bundle import AUDIO, NEW, OLD, PHANTOM

OWNER = 'occurrence:f2-occurrence'
TARGET = OWNER + '/clip:f2-agent-clip'


def retained_document():
    parent, shots, internals = app_closure()
    # Exact seed_emitter_parent preparation at the frozen G5 continuation.
    for rows in (parent['payload']['clips'], parent['payload']['config']['clips']):
        next(row for row in rows if row['id'] == 'f2-app-clip')['hold'] = 3
    child = next(row for row in internals[0]['payload']['clips'] if row['id'] == 'f2-agent-clip')
    child.update(clipType='am-type', **{'from': 0, 'to': 3, 'params': {'text': 'hello'}})
    child.pop('asset')
    return _closure({'project_id': parent['project_id'], 'timeline_id': parent['timeline_id'],
                     'parent_revision_id': parent['revision_id'], 'parent_composition': parent['payload'],
                     'shot_revisions': shots, 'internal_timeline_revisions': internals},
                    {'shot_revisions': [], 'internal_timeline_revisions': []}, _digest)


def forked():
    original = bundle_from_document(retained_document())
    call = capture_occurrence_fork(original, OWNER, 'selected-shot')
    return original, call, execute(original, [call]).bundle


@pytest.mark.parametrize('metadata', [False, True])
def test_retained_app_fork_projects_and_emits_selected_edit_without_changing_sibling(metadata):
    document = retained_document()
    original, call, candidate = forked()
    untouched = copy.deepcopy(original)
    assert {item['media_id'] for item in candidate['shots']['selected-shot']['payload']['items']} == {OLD, AUDIO}
    if metadata:
        candidate = execute(original, [call], commit_request={}).bundle
    projected = document_from_bundle(candidate)
    assert document_from_bundle(bundle_from_document(projected)) == projected
    old_rows = document['parent_revision']['payload']['occurrences']
    rows = projected['parent_revision']['payload']['occurrences']
    assert rows[1:] == old_rows[1:]
    assert rows[0] == {**old_rows[0], 'shot_id': 'selected-shot',
                       'shot_revision_id': rows[0]['shot_revision_id']}
    assert document['shot_revisions'][0] in projected['shot_revisions']
    assert document['internal_timeline_revisions'][0] in projected['internal_timeline_revisions']
    copied = next(row for row in projected['shot_revisions'] if row['shot_id'] == 'selected-shot')
    child = next(row for row in projected['internal_timeline_revisions']
                 if row['revision_id'] == copied['internal_timeline_revision_id'])
    assert child['payload'] == document['internal_timeline_revisions'][0]['payload']
    expected_shot, _ = _copy_shot_for_placement(
        document['shot_revisions'][0]['payload'], project_id=document['project_id'],
        timeline_id=document['timeline_id'], occurrence_id='f2-occurrence',
        authoring_shot_id='selected-shot', source_shot_id=document['shot_revisions'][0]['shot_id'])
    expected_shot['internal_timeline_revision_id'] = child['revision_id']
    assert copied['payload'] == expected_shot
    before = bundle_from_document(projected)
    after = copy.deepcopy(before)
    State(after).entity(TARGET)['params'] = {'text': '', 'opaque': [None, False, 7]}
    commands = emit_document_commands(before, after)
    edited = document_from_bundle(execute(before, commands, commit_request={}).bundle)
    assert edited['parent_revision']['payload']['occurrences'][1:] == old_rows[1:]
    assert document['shot_revisions'][0] in edited['shot_revisions']
    assert document['internal_timeline_revisions'][0] in edited['internal_timeline_revisions']
    assert State(bundle_from_document(edited)).entity(TARGET)['params'] == State(after).entity(TARGET)['params']
    edited_shot = next(row for row in edited['shot_revisions'] if row['shot_id'] == 'selected-shot')
    edited_internal = next(row for row in edited['internal_timeline_revisions']
                           if row['revision_id'] == edited_shot['internal_timeline_revision_id'])
    assert edited_internal['payload'] == after['shots']['selected-shot']['internal_timeline']
    expected_shot['internal_timeline_revision_id'] = edited_internal['revision_id']
    assert edited_shot['payload'] == expected_shot
    assert edited['dependency_manifest']['media'] == document['dependency_manifest']['media']
    assert original == untouched


def test_original_contradictory_shared_alias_edit_still_refuses():
    before = bundle_from_document(retained_document())
    after = copy.deepcopy(before)
    State(after).entity(TARGET)['params']['text'] = 'Selected only'
    with pytest.raises(DocumentPatchError, match='contradictory'):
        emit_document_commands(before, after)


def test_retained_app_fork_reload_undo_restores_exact_closure():
    original, call, _ = forked()
    plan = execute(original, [call])
    landed = bundle_from_document(document_from_bundle(plan.bundle))
    undone = inverse(landed, accepted(plan.records, original, landed))
    assert document_from_bundle(undone.bundle, source_bundles=(original, landed)) == retained_document()


def test_new_competing_selection_after_fork_refuses_without_mutating_input():
    _, _, candidate = forked()
    shot = candidate['shots']['selected-shot']
    shot['payload']['items'][0]['media_id'] = NEW
    shot['internal_timeline']['registry']['assets']['f2-plate']['media_id'] = PHANTOM
    before = copy.deepcopy(candidate)
    with pytest.raises(AuthoringBundleError, match='competing selected media'):
        document_from_bundle(candidate)
    assert candidate == before


@pytest.mark.parametrize('mutation', ['json-claim', 'source-pin', 'outside-source', 'foreign-closure',
                                     'stale-closure', 'mapped-creation', 'old-payload'])
def test_forged_copied_baseline_cannot_authorize_new_selection(mutation):
    _, _, candidate = forked()
    shot = candidate['shots']['selected-shot']
    if mutation == 'json-claim':
        candidate['shots']['selected-shot'] = json.loads(json.dumps(shot))
    elif mutation == 'source-pin':
        shot.source['source_revision_id'] = 'stale-shot'
    elif mutation == 'outside-source':
        shot.source['source_occurrence_id'] = 'outside'
    elif mutation == 'foreign-closure':
        candidate['source_mapping']['_runtime_document']['project_id'] = 'foreign'
    elif mutation == 'stale-closure':
        candidate['base_parent']['revision_id'] = 'stale-parent'
    elif mutation == 'mapped-creation':
        candidate['source_mapping']['shots']['selected-shot'] = copy.deepcopy(
            next(iter(candidate['source_mapping']['shots'].values())))
    else:
        shot['payload']['items'][0]['media_id'] = NEW
        shot['internal_timeline']['registry']['assets']['f2-plate']['media_id'] = PHANTOM
        shot['base_payload'] = copy.deepcopy(shot['payload'])
        shot['base_internal_timeline'] = copy.deepcopy(shot['internal_timeline'])
    before = copy.deepcopy(candidate)
    with pytest.raises(AuthoringBundleError):
        document_from_bundle(candidate)
    assert candidate == before


@pytest.mark.parametrize('mutation', ['source', 'outside', 'foreign', 'occupied'])
def test_retained_app_stale_or_invalid_fork_refuses_without_writes(mutation):
    original, call, _ = forked()
    if mutation == 'source':
        State(original).entity(TARGET)['params']['text'] = 'Changed'
    elif mutation == 'outside':
        call['select'] = 'occurrence:outside'
    elif mutation == 'foreign':
        call['select'] = 'other-project/occurrence:f2-occurrence'
    else:
        original['shots']['selected-shot'] = copy.deepcopy(next(iter(original['shots'].values())))
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        execute(original, [call])
    assert original == before


@pytest.mark.integration
def test_current_runtime_retained_fork_then_emitter(tmp_path):
    from types import SimpleNamespace

    from tests.helpers.runtime import new_runtime_daemon, use_runtime, vendored_runtime_commit

    use_runtime(vendored_runtime_commit())
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_validation import validate_commit_value

    from astrid.sdk.client import AstridClient
    from tests.editing_conformance.test_native_sheet_current_runtime import (
        seed_document_diff_parent,
    )

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    daemon = new_runtime_daemon(
        root, commit=vendored_runtime_commit(), support_root=tmp_path / 'support',
        authoring_profile='astrid',
    ).start()
    service = daemon.service
    client = AstridClient.open(
        endpoint=daemon.endpoint, credential=daemon.token, realm_id=service.realm['id'],
        actor_id='owner', client_name='fork-editor', client_version='1',
        protocol_version='workspace.v1',
    )

    def document():
        head = service.get_project_timeline(project, 'film')
        return service._timeline_commits()[0].revision_closure(project, 'film', head['head_revision_id'])

    def commit(calls, key, *, dry_run=False):
        head = service.get_project_timeline(project, 'film')
        body = {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                'base_revision_id': head['head_revision_id'],
                'base_publication_id': head['head_publication_id'], 'commands': calls,
                'label': key, 'steps': [{'id': f'{key}-{ordinal}', 'label': key,
                    'intent': 'Selected occurrence', 'tags': [], 'command_ordinals': [ordinal],
                    'patch': call['args']['changes'] if call['op'] == 'doc.patch' else [], 'reads': []}
                    for ordinal, call in enumerate(calls)],
                'merge': 'fields', 'reads': [], 'dry_run': dry_run, 'source': {'via': 'app'},
                'rationale': 'Preserve the copied source baseline', 'group_id': 'fork-gesture'}
        validate_commit_value(body, 'CommitRequest')
        return service.commit_timeline(project, 'film', body, idempotency_key=key,
            publisher='fork-editor', invocation_path=f'/v1/projects/{project}/timelines/film/commit',
            authorize=lambda: daemon.credentials.require(daemon.token, 'projects:write'))

    try:
        project = service.create_project({'slug': 'fork', 'name': 'Fork'}, idempotency_key='project')['id']
        service.create_timeline(project, 'film', idempotency_key='bootstrap')
        seed_document_diff_parent(SimpleNamespace(project=project, service=lambda: service, client=lambda: client))
        original = document()
        before = bundle_from_document(original)
        call = capture_occurrence_fork(before, OWNER, 'selected-shot')
        projected = bundle_from_document(document_from_bundle(execute(before, [call]).bundle))
        after = copy.deepcopy(projected)
        State(after).entity(TARGET)['params'] = {}
        patches = emit_document_commands(projected, after)
        calls = [call, *patches]
        preview = commit(calls, 'preview', dry_run=True)
        assert preview['receipt'] is None
        assert preview['data']['status'] == 'preview'
        assert preview['data']['would_status'] == 'accepted'
        assert document() == original
        result = commit(calls, 'fork-edit')
        validate_commit_value(result['data'], 'CommitAccepted')
        assert result['data']['status'] == 'accepted'
        landed = document()
        assert landed == result['data']['canonical_document'] == preview['data']['canonical_document']
        assert landed['parent_revision']['payload']['occurrences'][1:] == original[
            'parent_revision']['payload']['occurrences'][1:]
        assert original['shot_revisions'][0] in landed['shot_revisions']
        assert original['internal_timeline_revisions'][0] in landed['internal_timeline_revisions']
        assert State(bundle_from_document(landed)).entity(TARGET)['params'] == {}
        assert result['data']['steps'][0]['patch']
    finally:
        daemon.stop()
