"""Shared provenance through real managed-media reads and Runtime acceptance.

The test-only recipe/provider adapter admits its declared selection using ABI 1.
This qualifies manifest subset acceptance, not any production popup operation.
The historical fixture keeps numeric 24 fps and its original window scope.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from tests.helpers.runtime import use_runtime
from tests.helpers.trusted_seed import seed_trusted_parent

pytestmark = pytest.mark.integration


@pytest.fixture
def manifest_media(tmp_path, monkeypatch):
    use_runtime()
    from runtime_protocol.service import RuntimeService

    from tests.editing_conformance.test_commit_runtime_parent_media_swap import (
        editing as native_parent_media_fixture,
    )
    from tests.editing_conformance.test_commit_runtime_parent_media_swap import source_bytes

    # This helper builds the film with historical public APIs in a separate
    # interpreter, settles decodable PNGs, then opens it with current Runtime.
    fixture = native_parent_media_fixture.__wrapped__(
        SimpleNamespace(param='image'), tmp_path, monkeypatch, 'swap', 'parent',
        source_bytes.__wrapped__(tmp_path),
    )
    case = next(fixture)
    try:
        service, project = case.service, case.project
        service.list_timeline_commits(project, 'film', authorize=case.authorize)
        case.legacy = service.get_project_parent_composition_revision(project, 'film', 'popup-baseline')
        commits, _ = service._timeline_commits()
        document = commits.revision_closure(project, 'film', 'popup-baseline')
        shot = copy.deepcopy(document['shot_revisions'][0])
        binding = service.get_project_shot_text_binding(
            project, shot['payload']['text_bindings'][0]['binding_id'])
        descriptor = {key: binding[key] for key in (
            'kind', 'slot', 'media_id', 'content_hash', 'byte_size', 'mime_type')}
        assert service.cas.read(descriptor['media_id'].removeprefix('sha256:')) == b'A chapter'
        assert descriptor['content_hash'] == descriptor['media_id']
        assert descriptor['byte_size'] == len(b'A chapter')
        case.binding = binding
        shot.update(revision_id='manifest-shot')
        shot['payload']['text_bindings'] = [descriptor]
        parent = copy.deepcopy(document['parent_revision']['payload'])
        for occurrence in parent['occurrences']:
            occurrence['shot_revision_id'] = shot['revision_id']
        # Approved TEST SETUP boundary: the current six-field descriptor refers
        # to real retained legacy text, never inline text authority. No Agent
        # command is credited with creating this authored baseline.
        seed_trusted_parent(service, {
            'project_id': project, 'timeline_id': 'film', 'expected_head': 'popup-baseline',
            'parent_revision_id': 'media-baseline', 'parent_composition': parent,
            'shot_revisions': [shot],
        }, idempotency_key='media-baseline')
        case.baseline = commits.revision_closure(project, 'film', 'media-baseline')
        service.close()
        case.service = RuntimeService(case.root)
        yield case
    finally:
        fixture.close()


@pytest.mark.parametrize('operation', ['add', 'swap'])
def test_real_media_manifest_inverse_retry_restart_and_history(monkeypatch, operation, manifest_media):
    use_runtime()
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import RequestCommitPort, route_path

    from astrid.sdk.timeline import commands
    from astrid.sdk.timeline.commit import RuntimeCommitService, create_runtime_commit_service

    service, project, authorize = manifest_media.service, manifest_media.project, manifest_media.authorize
    old, new = manifest_media.old, manifest_media.new

    def introduce(ctx, command):
        pin = command['args']['media']
        owner = command['select']
        ctx.read(owner + '/source')
        ctx.write(owner + '/source', ['registry', 'assets', 'new'], {
            'media_id': pin['object_id'], 'type': 'image', 'generationId': pin['generation_id'],
            'variantId': pin['variant_id'], 'opaque': [None, False, {}]})
        ctx.dependency('fixture.media', 'media', pin)
        if operation == 'add':
            ctx.create(owner, 'clip', {'id': 'reserved-child', 'track': 'picture', 'asset': 'new',
                                       'clipType': 'image', 'at': .125, 'hold': 1.25, 'params': None})
        else:
            ctx.read(owner + '/clip:clip-a')
            ctx.write(owner + '/clip:clip-a', ['asset'], 'new')

    installed = commands.operations()
    installed['test.media-provenance'] = commands.Operation({'type': 'object'}, introduce, {})
    monkeypatch.setattr(commands, 'operations', lambda: installed)

    class RecipeProvider(RuntimeCommitService):
        def commit(self, context, port):
            # Test-only recipe ABI adapter, not a production popup operation.
            for call in context['request']['commands']:
                if call['op'] == 'test.media-provenance':
                    port.selected_media(call['args']['media'])
            return super().commit(context, port)

    captured = []
    real_closure = RequestCommitPort.revision_closure

    def closure(port, revision):
        result = real_closure(port, revision)
        captured.append(copy.deepcopy(result))
        return result

    monkeypatch.setattr(RequestCommitPort, 'revision_closure', closure)

    def register():
        service.register_timeline_commit_provider(RecipeProvider(create_runtime_commit_service().contract_digest))

    def body(calls, *, dry_run=False):
        head = service.get_project_timeline(project, 'film')
        return {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                'base_revision_id': head['head_revision_id'], 'base_publication_id': head['head_publication_id'],
                'commands': calls, 'dry_run': dry_run, 'source': {'via': 'python'}}

    def submit(request, key):
        return service.commit_timeline(project, 'film', request, idempotency_key=key, publisher='media-editor',
                                       invocation_path=route_path(project, 'film'), authorize=authorize)

    def inverse(result):
        return {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
                'args': {'publications': [result['receipt']['receipt_id']]}}

    try:
        register()
        request = body([{'schema_version': 1, 'op': 'test.media-provenance', 'version': '1',
                         'select': 'occurrence:occ-a', 'args': {'media': new}}])
        created = submit(request, 'introduce')
        baseline = next(row for row in captured if row['parent_revision']['revision_id'] == 'media-baseline')
        assert baseline == manifest_media.baseline
        assert old['object_id'] in {pin['media_id'] for pin in baseline['dependency_manifest']['media']}
        assert new['object_id'] not in {pin['media_id'] for pin in baseline['dependency_manifest']['media']}
        assert new['object_id'] in {pin['media_id'] for pin in created['data']['canonical_document']['dependency_manifest']['media']}
        assert ({pin['media_id'] for pin in created['data']['canonical_document']['dependency_manifest']['media']}
                - {pin['media_id'] for pin in baseline['dependency_manifest']['media']}) == {new['object_id']}
        undo_request = body([inverse(created)])
        preview = submit(dict(undo_request, dry_run=True), 'undo-preview')
        assert preview['data']['canonical_document'] == baseline
        captured.clear()
        undone = submit(undo_request, 'undo')
        assert undone['data']['canonical_document'] == baseline
        # Acceptance captured and validated the introduced media in the target
        # closure, while its result manifest omits it. History remains immutable.
        assert created['data']['canonical_document'] in captured
        before_restart = service.list_timeline_commits(project, 'film', authorize=authorize)
        root = manifest_media.root
        service.close()
        service = RuntimeService(root)
        register()
        with monkeypatch.context() as retry:
            retry.setattr(commands, 'execute', lambda *a, **kw: pytest.fail('retry executed semantics'))
            assert submit(undo_request, 'undo') == undone
            assert submit(request, 'introduce') == created
        changed = copy.deepcopy(undo_request)
        changed['source'] = {'via': 'cli'}
        with pytest.raises(RuntimeErrorBase) as error:
            submit(changed, 'undo')
        assert error.value.status == 409
        assert service.list_timeline_commits(project, 'film', authorize=authorize) == before_restart
        assert service.get_project_media_selection(project, {key: new[key] for key in (
            'asset', 'generation_id', 'variant_id')}, authorize=authorize) == new
        redone = submit(body([inverse(undone)]), 'redo')
        assert redone['data']['canonical_document'] == created['data']['canonical_document']
        history = service.list_timeline_commits(project, 'film', authorize=authorize)['items']
        edge = next(row for row in history if row['publication_id'] == created['receipt']['receipt_id'])
        assert edge['receipt'] == created['receipt']
        assert edge['records']['commands'][0]['dependencies'][0]['pin'] == new
        stored = service.get_project_parent_composition_revision(project, 'film', created['data']['accepted_revision_id'])
        assert stored['dependency_manifest'] == created['data']['canonical_document']['dependency_manifest']
        assert service.get_project_shot_text_binding(project, manifest_media.binding['binding_id']) == manifest_media.binding
        assert service.get_project_parent_composition_revision(project, 'film', 'popup-baseline') == manifest_media.legacy
        assert service.store.integrity_report()['ok']
    finally:
        service.close()
