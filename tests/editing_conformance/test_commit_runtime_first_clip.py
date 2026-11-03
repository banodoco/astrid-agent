"""First nonempty film through the real client/provider, with no legacy seed.

Run centrally with the commit-integration suite and this file appended.
"""
from __future__ import annotations

import copy
import sys

import pytest

from astrid.core.timeline.authoring_bundle import open_authoring_bundle
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance.test_commit_runtime_m1 import open_client
from tests.editing_conformance.test_recipe_m2_clip_add import literal_row
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('nullable_speed', [False, True])
def test_empty_bootstrap_first_clip_client_commit_exact_reload_inverse_redo(
    tmp_path, monkeypatch, real_runtime, nullable_speed,
):
    use_runtime()
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client-data'))
    daemon = real_runtime(authoring_profile='astrid')
    project = daemon.service.create_project({'slug': 'first-clip', 'name': 'First clip'})['id']
    daemon.service.create_timeline(project, 'film', idempotency_key='empty-bootstrap')

    def forbidden(*args, **kwargs):
        raise AssertionError('first clip must not use a legacy publisher or renderer')

    class NoSceneImports:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(('composition', 'remotion', 'astrid.sdk.authoring_render_preview')):
                raise AssertionError('creation imported renderer: ' + fullname)

    import runtime_protocol.service as service_module
    import runtime_protocol.visual_seam as visual_seam

    monkeypatch.setattr(sys, 'meta_path', [NoSceneImports(), *sys.meta_path])
    monkeypatch.setattr(service_module, 'evaluate_closure', forbidden)
    monkeypatch.setattr(visual_seam, 'evaluate_closure', forbidden)
    monkeypatch.setattr(daemon.service, 'publish_parent_composition', forbidden)
    client = open_client(daemon)

    def history():
        result = client.timelines.history(project, 'film', limit=100)
        assert result.ok, result.as_dict()
        return result.data

    def commit(calls, key):
        head = history()['head']
        body = {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                'base_revision_id': head['revision_id'],
                'base_publication_id': head['publication_id'], 'commands': calls}
        result = client.timelines.commit(project, 'film', body, idempotency_key=key)
        assert result.ok, result.as_dict()
        return result

    def inverse(publication, key):
        return commit([{'schema_version': 1, 'op': 'timeline.inverse', 'version': '1',
                        'select': None, 'args': {'publications': [publication]}}], key)

    def reload_document(result):
        document = result.data['canonical_document']
        stored = client._remote._transport.get_project_parent_composition_revision(
            project, 'film', result.data['accepted_revision_id'])
        for key, value in document['parent_revision'].items():
            assert stored[key] == value
        assert stored['dependency_manifest'] == document['dependency_manifest']
        opened = client.timelines.open_bundle(project, 'film', revision_id=result.data['accepted_revision_id'])
        assert opened.ok, opened.as_dict()
        # Main moves occurrences out of parent into placements when opening.
        assert opened.data['bundle'] == open_authoring_bundle(
            {**document['parent_revision'], 'project_id': project, 'timeline_id': 'film'},
            shot_revisions=[], internal_timeline_revisions=[])
        assert opened.data['bundle']['shots'] == {}
        return document

    initial = history()
    assert len(initial['items']) == 1 and initial['items'][0]['records'] is None
    opened = client.timelines.open_bundle(project, 'film')
    assert opened.ok, opened.as_dict()
    assert opened.data['bundle']['parent']['clips'] == []
    track = capture_track_create(opened.data['bundle'], 'parent', {
        'id': 'first-track', 'kind': 'visual', 'role': 'output', 'opaque': [None, False]})
    tracked = commit([track], 'create-track')
    before = reload_document(tracked)
    row = literal_row(**({'speed': None} if nullable_speed else {}))
    add = capture_clip_add(bundle_from_document(before), 'parent', row)
    created = commit([add], 'create-first-clip')
    document = reload_document(created)
    assert document['parent_revision']['payload']['clips'] == [row]
    assert document['parent_revision']['payload']['config']['clips'] == [row]
    assert document['dependency_manifest'] == before['dependency_manifest']
    assert document['shot_revisions'] == document['internal_timeline_revisions'] == []
    accepted = history()
    assert [entry['records']['commands'][0]['command']['op'] for entry in accepted['items'][1:]] == ['track.create', 'clip.add']
    assert accepted['items'][-1]['records']['commands'][0]['created'] == [
        {'ref': 'parent/clip:first-clip', 'kind': 'clip', 'owner': 'parent'}]

    # Ordinary creation cannot reuse an occupied or retired ID. Refusal leaves
    # accepted history unchanged, while reversing an accepted inverse can reuse it.
    raw = {key: add[key] for key in ('schema_version', 'op', 'version', 'select', 'args')}
    def refuse(key):
        prior = copy.deepcopy(history())
        head = prior['head']
        result = client.timelines.commit(project, 'film', {
            'api_version': 1, 'project_id': project, 'timeline_id': 'film',
            'base_revision_id': head['revision_id'], 'base_publication_id': head['publication_id'],
            'commands': [raw]}, idempotency_key=key)
        assert not result.ok and result.error.details['reason'] == 'identity_collision', result.as_dict()
        assert history() == prior

    refuse('occupied-first-clip')
    undone = inverse(created.as_dict()['receipt']['receipt_id'], 'undo-first-clip')
    assert reload_document(undone) == before
    refuse('retired-first-clip')
    redone = inverse(undone.as_dict()['receipt']['receipt_id'], 'redo-first-clip')
    assert reload_document(redone) == document
    again = inverse(redone.as_dict()['receipt']['receipt_id'], 'undo-redone-first-clip')
    assert reload_document(again) == before
