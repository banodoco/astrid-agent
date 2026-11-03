"""First canonical shot via normal bootstrap, generated schema and accepted history.

Privately generate the full S2 superset and run the journey in an isolated
process so other integration tests keep their own Runtime imports. No SQL, legacy baseline, HTTP alias or shared dependency writes.
"""
from __future__ import annotations

import copy
import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_occurrence_media_commands import (
    capture_occurrence_add_media,
)
from tests.helpers.runtime import runtime_checkout

pytestmark = pytest.mark.integration
OWNER = 'occurrence:first-occurrence'


@pytest.fixture(scope='module')
def generated_runtime(tmp_path_factory):
    source = runtime_checkout()
    private = tmp_path_factory.mktemp('birth-runtime')
    archive = subprocess.run(['git', '-C', str(source), 'archive', 'HEAD'], check=True, capture_output=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tree:
        tree.extractall(private, filter='data')
    schema = Path(__file__).parents[2] / 'astrid/core/timeline/schema/edit-command.schema.json'
    (private / 'contract/schemas/edit-command.json').write_bytes(schema.read_bytes())
    environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(private)}
    # The normal generator checks all immutable published floors, including S2.
    for script in ('generators/generate.py', 'scripts/artifact_manifest.py'):
        for flags in ([], ['--check']):
            result = subprocess.run([sys.executable, script, *flags], cwd=private,
                                    env=environment, capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
    return private


@pytest.mark.parametrize('grouped', [False, True])
def test_bootstrap_birth_child_media_exact_history_inverse_redo_retry(
    generated_runtime, tmp_path, grouped,
):
    script = '''
import sys
from pathlib import Path
import pytest
from tests.helpers.runtime import use_runtime
use_runtime()
from tests.editing_conformance.test_commit_runtime_occurrence_create import _prove_birth
with pytest.MonkeyPatch.context() as patch:
    _prove_birth(Path(sys.argv[1]), patch, sys.argv[2] == 'True')
'''
    environment = {**os.environ, 'ASTRID_RUNTIME_CHECKOUT': str(generated_runtime),
                   'PYTHONDONTWRITEBYTECODE': '1',
                   'PYTHONPATH': os.pathsep.join([str(Path(__file__).parents[2]),
                                                 str(generated_runtime / 'tests')])}
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), str(grouped)],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def _prove_birth(tmp_path, monkeypatch, grouped):
    from runtime_protocol.auth import CredentialStore
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from test_generation_publish_v1_settlement import (
        _effect,
        _fixture,
        _output,
        _settle,
    )

    def effect(project):
        return _effect(project, groups=[{'group_key': 'main', 'selectors': [
            {'selector': 'image', 'ordinal': 0, 'variant_key': 'image', 'output_port': 'image'}]}])

    service, project, task, attempt, admitted = _fixture(tmp_path, effect_factory=effect)
    project = project['id']
    credentials = CredentialStore(tmp_path / 'credentials')
    token, _ = credentials.provision('birth-editor', ['projects:read', 'projects:write'])

    def authorize():
        return credentials.require(token, 'projects:write')

    def no_legacy(*args, **kwargs):
        raise AssertionError('birth must not call a legacy writer or renderer')

    import runtime_protocol.service as service_module
    import runtime_protocol.visual_seam as visual_seam

    monkeypatch.setattr(service_module, 'evaluate_closure', no_legacy)
    monkeypatch.setattr(visual_seam, 'evaluate_closure', no_legacy)
    monkeypatch.setattr(service, 'publish_parent_composition', no_legacy)
    try:
        _settle(service, attempt, [_output(b'image-bytes', output_port='image', group_key='main',
            variant_key='image', ordinal=0, filename='image.png', media_type='image/png')], effect=admitted)
        association = service.managed_outputs(task['task']['id'])[0]
        pin = service.get_project_media_selection(project, {
            'association_id': association['association_id'], 'object_id': association['object_id'],
            'version': association['lifecycle']['version']}, authorize=authorize)
        verified = service.get_project_verified_media(project, pin, authorize=authorize)
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        service.create_timeline(project, 'film', idempotency_key='normal-bootstrap')

        def history():
            return service.list_timeline_commits(project, 'film', limit=100, authorize=authorize)

        def body(calls, *, timeline='film'):
            head = service.get_project_timeline(project, timeline)
            return {'api_version': 1, 'project_id': project, 'timeline_id': timeline,
                    'base_revision_id': head['head_revision_id'],
                    'base_publication_id': head['head_publication_id'], 'commands': calls}

        def commit(request, key):
            timeline = request['timeline_id']
            return service.commit_timeline(project, timeline, request, idempotency_key=key,
                publisher='birth-editor', invocation_path=f'/v1/projects/{project}/timelines/{timeline}/commit',
                authorize=authorize)

        def inverse(result, key):
            return commit(body([{'schema_version': 1, 'op': 'timeline.inverse', 'version': '1',
                'select': None, 'args': {'publications': [result['receipt']['receipt_id']]}}]), key)

        def document(result):
            value = result['data']['canonical_document']
            stored = service.get_project_parent_composition_revision(
                project, 'film', result['data']['accepted_revision_id'])
            assert all(stored[key] == child for key, child in value['parent_revision'].items())
            assert stored['dependency_manifest'] == value['dependency_manifest']
            # The normal read API follows every immutable child revision link.
            for shot in value['shot_revisions']:
                assert service.get_project_shot_revision(project, shot['shot_id'], shot['revision_id'])['payload'] == shot['payload']
            return value

        initial = history()
        assert len(initial['items']) == 1 and initial['items'][0]['intent'] == 'bootstrap'
        bootstrap = service.get_project_parent_composition_revision(
            project, 'film', initial['head']['revision_id'])
        assert bootstrap['payload']['occurrences'] == []
        assert bootstrap['dependency_manifest'] == {
            'shots': [], 'internal_timelines': [], 'media': [], 'documents': []}
        empty = {'schema_version': 1, 'project_id': project, 'timeline_id': 'film',
                 'parent_revision': {key: bootstrap[key] for key in
                     ('revision_id', 'content_digest', 'payload')},
                 'shot_revisions': [], 'internal_timeline_revisions': [],
                 'dependency_manifest': bootstrap['dependency_manifest']}
        bundle = bundle_from_document(empty)
        parent_track = capture_track_create(bundle, 'parent', {'id': 'picture', 'kind': 'visual'})
        tracked = commit(body([parent_track]), 'parent-track')
        before = document(tracked)
        bundle = bundle_from_document(before)
        birth = capture_occurrence_create(bundle, 'first-occurrence', 'first-shot', name='Chapter one',
                                          track='picture', on=0, duration=10)
        invalid_birth = {key: copy.deepcopy(birth[key])
                         for key in ('schema_version', 'op', 'version', 'select', 'args')}
        invalid_birth['args']['on'] = 1e308
        history_before_invalid = copy.deepcopy(history())
        head_before_invalid = service.get_project_timeline(project, 'film')
        with pytest.raises(RuntimeErrorBase) as error:
            commit(body([invalid_birth]), 'invalid-birth-quantizer')
        assert error.value.status == 422
        assert error.value.details['status'] == 'invalid'
        assert error.value.details['code'] == 'unsupported_timing'
        assert error.value.details['reason'] == 'unsupported_timing'
        assert 'retry the exact request' not in str(error.value)
        assert service.get_project_timeline(project, 'film') == head_before_invalid
        assert history() == history_before_invalid
        born = execute(bundle, [birth]).bundle
        child_track = capture_track_create(born, OWNER, {'id': 'picture', 'kind': 'visual'})
        child_bundle = execute(born, [child_track]).bundle
        row = {'id': 'first-image', 'clipType': 'image', 'asset': pin['asset'], 'track': 'picture',
               'at': 0, 'hold': 2, 'opaque': [None, False]}
        add = capture_occurrence_add_media(child_bundle, OWNER, row, pin, verified_media=[verified])
        birth_request = body([birth, child_track, add] if grouped else [birth])
        created = commit(birth_request, 'birth')
        created_document = document(created)
        birth_entry = history()['items'][-1]
        assert birth_entry['records']['commands'][0]['created'] == [
            {'ref': 'shot:first-shot', 'kind': 'shot', 'owner': None},
            {'ref': OWNER, 'kind': 'occurrence', 'owner': 'parent'}]
        if grouped:
            assert created_document['internal_timeline_revisions'][0]['payload']['clips'] == [row]
        else:
            assert created_document['shot_revisions'][0]['payload']['items'] == []
            assert created_document['internal_timeline_revisions'][0]['payload']['clips'] == []
        # A different timeline cannot claim this shot, including after undo.
        service.create_timeline(project, 'other-film', idempotency_key='other-bootstrap')
        other_head = service.get_project_timeline(project, 'other-film')
        other_parent = service.get_project_parent_composition_revision(
            project, 'other-film', other_head['head_revision_id'])
        other_empty = {**copy.deepcopy(empty), 'timeline_id': 'other-film',
                       'parent_revision': {key: other_parent[key] for key in
                           ('revision_id', 'content_digest', 'payload')},
                       'dependency_manifest': other_parent['dependency_manifest']}
        other_bundle = bundle_from_document(other_empty)
        other_track = capture_track_create(other_bundle, 'parent', {'id': 'picture', 'kind': 'visual'})
        commit(body([other_track], timeline='other-film'), 'other-track')
        other_birth = {key: copy.deepcopy(birth[key]) for key in
                       ('schema_version', 'op', 'version', 'select', 'args')}
        other_birth['args']['occurrence_id'] = 'other-occurrence'

        def refuse_other(key):
            previous = service.list_timeline_commits(project, 'other-film', authorize=authorize)
            with pytest.raises(RuntimeErrorBase) as collision:
                commit(body([other_birth], timeline='other-film'), key)
            assert collision.value.details['reason'] == 'destination_occupied'
            assert service.list_timeline_commits(project, 'other-film', authorize=authorize) == previous

        refuse_other('occupied-project-shot')
        undone = inverse(created, 'undo-birth')
        assert document(undone) == before
        refuse_other('retired-project-shot')
        raw = {key: birth[key] for key in ('schema_version', 'op', 'version', 'select', 'args')}
        previous = copy.deepcopy(history())
        with pytest.raises(RuntimeErrorBase) as collision:
            commit(body([raw]), 'retired-birth')
        assert collision.value.details['reason'] == 'identity_collision'
        assert history() == previous
        redone = inverse(undone, 'redo-birth')
        assert document(redone) == created_document
        assert commit(birth_request, 'birth') == created
        expected_head = created_document
        if not grouped:
            media_bundle = bundle_from_document(document(redone))
            child_track = capture_track_create(media_bundle, OWNER, {'id': 'picture', 'kind': 'visual'})
            track_result = commit(body([child_track]), 'child-track')
            media_bundle = bundle_from_document(document(track_result))
            add = capture_occurrence_add_media(media_bundle, OWNER, row, pin, verified_media=[verified])
            media_result = commit(body([add]), 'child-media')
            expected_head = document(media_result)
            assert expected_head['internal_timeline_revisions'][0]['payload']['clips'] == [row]
            # Even a later inverse cannot erase intervening authored history.
            with pytest.raises(RuntimeErrorBase):
                inverse(redone, 'dependent-birth-undo')
            media_undo = inverse(media_result, 'undo-media')
            assert document(media_undo) == document(track_result)
            assert document(inverse(media_undo, 'redo-media')) == expected_head
        # Re-open storage/provider and retry the exact original request.
        root = service.store.root
        service.close()
        try:
            service = RuntimeService(root)
        except RuntimeErrorBase as error:
            pytest.fail(str(error) + ': ' + repr(error.details))
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        assert commit(birth_request, 'birth') == created
        latest = service.get_project_timeline(project, 'film')
        assert latest['head_revision_id'] == expected_head['parent_revision']['revision_id']
    finally:
        service.close()
