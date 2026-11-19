"""Local feasibility fixture: genuine historical placement and guarded upgrade."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.media_manifest_upgrade import upgrade_manifest_copy
from tests.helpers.shot_patch import assert_reopened_shared_shot

SOURCE = Path(__file__).parents[1] / 'editing_conformance/fixtures/media_manifest_compatibility.tar.xz'
SOURCE_SHA256 = 'b74d609a3ec48f64de4d4424f5f61458609e7d97715c5306784f820a68b1ddfc'


def _inventory(root):
    return {str(path.relative_to(root)): {
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size,
    } for path in sorted(root.rglob('*')) if path.is_file()}


def historical_manifest_case(tmp_path):
    from runtime_protocol.auth import CredentialStore
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_shared import capture_occurrence_place_shared

    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == SOURCE_SHA256
    source = tmp_path / 'immutable-source'
    with tarfile.open(SOURCE) as archive:
        archive.extractall(source, filter='data')
    seed = json.loads((source / 'seed.json').read_text())
    provenance = json.loads((source / 'provenance.json').read_text())
    assert seed['source_agent'] == provenance['source_agent'] == 'ba21beffd177e7d80a5b839bde5a9e25b97235c2'
    assert seed['source_runtime'] == provenance['source_runtime'] == 'b2a1d9fd82121a5509769531a2e91aece97d78b9'
    original = _inventory(source / 'realm')
    assert original == provenance['realm_files']
    original_dirs = {path.relative_to(source / 'realm').as_posix()
                     for path in (source / 'realm').rglob('*') if path.is_dir()}
    assert original_dirs == set(provenance['realm_directories'])
    root = tmp_path / 'publish'
    shutil.copytree(source / 'realm', root)
    assert _inventory(root) == original
    assert {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_dir()} == original_dirs
    service = state = None
    try:
        # The operator owns real fingerprint, fences, recovery, migration and
        # backfill. Only its existing test process boundary is isolated.
        upgrade_manifest_copy(root, tmp_path)
        service = RuntimeService(root)
        project = seed['project']
        binding = service.get_project_shot_text_binding(project, seed['binding']['binding_id'])
        assert binding == seed['binding']
        assert binding['project_id'] == project and binding['shot_id'] == seed['shot']['shot_id']
        assert service.cas.read(binding['media_id'].removeprefix('sha256:')) == b'A chapter'
        head = service.get_project_timeline(project, 'film')
        parent = service.get_project_parent_composition_revision(project, 'film', seed['parent']['revision_id'])
        shot = service.get_project_shot_revision(project, seed['shot']['shot_id'], seed['shot']['revision_id'])
        internal = service.get_project_timeline_revision(project, 'film', seed['internal']['revision_id'])
        for actual, expected in ((parent, seed['parent']), (shot, seed['shot']), (internal, seed['internal'])):
            assert actual['revision_id'] == expected['revision_id']
            assert actual['content_digest'] == expected['content_digest']
            assert actual['payload'] == expected['payload']
        assert parent['dependency_manifest'] == seed['parent']['dependency_manifest']
        assert shot['internal_timeline_revision_id'] == internal['revision_id']
        assert shot['payload']['internal_timeline_revision_id'] == internal['revision_id']
        credentials = CredentialStore(tmp_path / 'current-credentials')
        token, _ = credentials.provision('media-editor', ['projects:read', 'projects:write'])

        def authorize():
            return credentials.require(token, 'projects:write')

        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        service.list_timeline_commits(project, 'film', authorize=authorize)
        commits, _ = service._timeline_commits()
        canonical = commits.revision_closure(project, 'film', head['head_revision_id'])
        shared = capture_occurrence_place_shared(
            bundle_from_document(canonical), 'occurrence:occ-a', 'occ-b', on=10)
        head = service.get_project_timeline(project, 'film')
        request = {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                   'base_revision_id': head['head_revision_id'],
                   'base_publication_id': head['head_publication_id'],
                   'commands': [shared], 'dry_run': False, 'source': {'via': 'python'}}
        accepted = service.commit_timeline(project, 'film', request, idempotency_key='compatibility-sibling',
            publisher='media-editor', invocation_path=f'/v1/projects/{project}/timelines/film/commit',
            authorize=authorize)
        document = accepted['data']['canonical_document']
        assert document['shot_revisions'] == canonical['shot_revisions']
        assert document['internal_timeline_revisions'] == canonical['internal_timeline_revisions']
        occurrences = document['parent_revision']['payload']['occurrences']
        assert occurrences[0] == canonical['parent_revision']['payload']['occurrences'][0]
        assert {row['occurrence_id']: row['shot_revision_id'] for row in occurrences} == {
            'occ-a': shot['revision_id'], 'occ-b': shot['revision_id']}
        assert {key: value for key, value in document['parent_revision']['payload'].items()
                if key != 'occurrences'} == {
                    key: value for key, value in canonical['parent_revision']['payload'].items()
                    if key != 'occurrences'}
        assert service.get_project_shot_text_binding(project, binding['binding_id']) == binding
        assert_reopened_shared_shot(service, project, 'film', document)
        service.close()
        service = RuntimeService(root)
        assert service.get_project_shot_text_binding(project, binding['binding_id']) == binding
        assert service.get_project_parent_composition_revision(project, 'film', seed['parent']['revision_id']) == parent
        assert_reopened_shared_shot(service, project, 'film', document)
        fields = ('project', 'old', 'new', 'associations', 'outputs', 'kind', 'operation')
        state = SimpleNamespace(**{key: copy.deepcopy(seed[key]) for key in fields},
                                root=root, service=service, authorize=authorize)
        yield state
    finally:
        if state is not None:
            state.service.close()
        elif service is not None:
            service.close()
        assert _inventory(source / 'realm') == original
        assert {path.relative_to(source / 'realm').as_posix()
                for path in (source / 'realm').rglob('*') if path.is_dir()} == original_dirs
