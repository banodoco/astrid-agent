"""Native add/delete batches through Runtime's validated service envelope."""
from __future__ import annotations

import copy

import pytest

from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('net_zero', [True, False], ids=['net-zero', 'replace-existing'])
def test_native_add_delete_one_publication_inverse_redo_restart_reservations(tmp_path, monkeypatch, net_zero):
    use_runtime()
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import route_path

    from astrid.sdk.timeline import commands, commit
    from astrid.sdk.timeline._clip_commands import capture_clip_add
    from astrid.sdk.timeline._command_state import ABSENT, present
    from astrid.sdk.timeline.capture import capture_clip_delete, capture_track_create
    from tests.editing_conformance.test_recipe_m2_clip_add import literal_row

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(commit.create_runtime_commit_service())
    project = service.create_project({'slug': 'add-delete', 'name': 'Add/delete'})['id']
    service.create_timeline(project, 'film', idempotency_key='create-film')

    def history():
        return service.list_timeline_commits(project, 'film', authorize=lambda: None)

    def submit(body, key):
        return service.commit_timeline(
            project, 'film', body, idempotency_key=key, publisher='author',
            invocation_path=route_path(project, 'film'), authorize=lambda: None)

    def request(calls):
        head = history()['head']
        return {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                'base_revision_id': head['revision_id'], 'base_publication_id': head['publication_id'],
                'commands': calls}

    def inverse(publication, key):
        return submit(request([{'schema_version': 1, 'op': 'timeline.inverse', 'version': '1',
                                'select': None, 'args': {'publications': [publication]}}]), key)

    def reload(result):
        document = result['data']['canonical_document']
        stored = service.get_project_parent_composition_revision(
            project, 'film', result['data']['accepted_revision_id'])
        for key, value in document['parent_revision'].items():
            assert stored[key] == value
        assert stored['dependency_manifest'] == document['dependency_manifest']
        return document

    def forbidden(*args, **kwargs):
        raise AssertionError('batch must use native commands without renderer or legacy publication')

    import runtime_protocol.service as service_module
    import runtime_protocol.visual_seam as visual_seam

    monkeypatch.setattr(service_module, 'evaluate_closure', forbidden)
    monkeypatch.setattr(visual_seam, 'evaluate_closure', forbidden)
    monkeypatch.setattr(RuntimeService, 'publish_parent_composition', forbidden)
    try:
        bootstrap = service.get_project_parent_composition_revision(
            project, 'film', history()['head']['revision_id'])
        document = {'schema_version': 1, 'project_id': project, 'timeline_id': 'film',
                    'parent_revision': {key: bootstrap[key] for key in ('revision_id', 'content_digest', 'payload')},
                    'shot_revisions': [], 'internal_timeline_revisions': [],
                    'dependency_manifest': bootstrap['dependency_manifest']}
        track = capture_track_create(commit.bundle_from_document(document), 'parent', {
            'id': 'first-track', 'kind': 'visual', 'role': 'output', 'opaque': [None, False]})
        tracked = submit(request([track]), 'create-track')
        before = reload(tracked)
        if not net_zero:
            for index in range(3):
                row = literal_row(id='existing-' + str(index), at=index * 2)
                call = capture_clip_add(commit.bundle_from_document(before), 'parent', row)
                before = reload(submit(request([call]), 'existing-' + str(index)))
        bundle = commit.bundle_from_document(before)
        row = literal_row()
        add = capture_clip_add(bundle, 'parent', row)
        ref = 'parent/clip:' + ('first-clip' if net_zero else 'existing-1')
        if net_zero:
            after_add = commands.execute(bundle, [add]).bundle
            calls = [add, capture_clip_delete(after_add, ref)]
        else:
            delete = capture_clip_delete(bundle, ref)
            middle = commands.execute(bundle, [delete]).bundle
            calls = [delete, capture_clip_add(middle, 'parent', row)]
        prior = copy.deepcopy(history())
        body = request(calls)
        accepted = submit(body, 'add-delete-batch')
        after = reload(accepted)
        entries = history()['items']
        assert len(entries) == len(prior['items']) + 1
        assert entries[:-1] == prior['items']
        assert accepted['data']['commands'] == calls  # Original claims were verified.
        records = copy.deepcopy(entries[-1]['records']['commands'])
        assert [record['command']['op'] for record in records] == [call['op'] for call in calls]
        created = {'ref': 'parent/clip:first-clip', 'kind': 'clip', 'owner': 'parent'}
        assert [item for record in records for item in record['created']] == [created]
        if net_zero:
            assert after == before
            assert records[0]['writes'] == [{'ref': ref, 'path': [], 'before': ABSENT, 'after': present(row)}]
            assert records[1]['writes'] == [{'ref': ref, 'path': [], 'before': present(row), 'after': ABSENT}]
        else:
            expected = [item for item in before['parent_revision']['payload']['clips'] if item['id'] != 'existing-1'] + [row]
            assert after['parent_revision']['payload']['clips'] == expected
            assert after['parent_revision']['payload']['config']['clips'] == expected
        assert submit(body, 'add-delete-batch') == accepted
        assert history()['items'] == entries
        if net_zero:
            with pytest.raises(RuntimeErrorBase) as error:
                submit(request(calls), 'reallocate-net-zero-batch')
            assert error.value.details['reason'] == 'identity_collision'
            assert history()['items'] == entries

        def refuse_reuse(identity, key):
            previous = copy.deepcopy(history())
            raw = {'schema_version': 1, 'op': 'clip.add', 'version': '1', 'select': 'parent',
                   'args': {'row': literal_row(id=identity)}}
            with pytest.raises(RuntimeErrorBase) as error:
                submit(request([raw]), key)
            assert error.value.status == 409 and error.value.details['reason'] == 'identity_collision'
            assert history() == previous

        for index in range(3):
            refuse_reuse('first-clip', 'reserved-new-' + str(index))
            if not net_zero:
                refuse_reuse('existing-1', 'reserved-old-' + str(index))
            if index == 0:
                result = inverse(accepted['receipt']['receipt_id'], 'undo-batch')
                assert reload(result) == before
            elif index == 1:
                result = inverse(result['receipt']['receipt_id'], 'redo-batch')
                assert reload(result) == after
            else:
                break
            if net_zero:
                inverse_record = history()['items'][-1]['records']['commands'][0]
                assert inverse_record['writes'] == [
                    {'ref': ref, 'path': [], 'before': ABSENT, 'after': present(row)},
                    {'ref': ref, 'path': [], 'before': present(row), 'after': ABSENT},
                ]
                assert inverse_record['created'] == [created]
        assert next(entry for entry in history()['items'] if entry['publication_id'] == accepted['receipt']['receipt_id'])['records']['commands'] == records
        if net_zero:
            # The authored state never moved, but the accepted absent-to-absent
            # inverse edges must still block reversal across intervening writes.
            previous = copy.deepcopy(history())
            with pytest.raises(RuntimeErrorBase) as error:
                inverse(accepted['receipt']['receipt_id'], 'inverse-across-unchanged-writes')
            assert error.value.details['reason'] == 'intervening_write'
            assert history() == previous
        durable = copy.deepcopy(history())
        service.close()
        service = RuntimeService(root)
        service.register_timeline_commit_provider(commit.create_runtime_commit_service())
        assert history() == durable
        refuse_reuse('first-clip', 'reserved-after-restart')
        with monkeypatch.context() as retry:
            retry.setattr(commands, 'execute', forbidden)
            retry.setattr(commit, '_check_reservations', forbidden)
            assert submit(body, 'add-delete-batch') == accepted
        assert history() == durable
    finally:
        service.close()
