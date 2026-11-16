"""Capture admission and SDK inspect share the exact normalized closure."""
from __future__ import annotations

import copy

from astrid.packs.rendering.actions.render.managed_timeline import _project_exact_parent_head
from astrid.sdk.timeline.commit import _digest, _manifest, create_runtime_commit_service
from tests.sdk.test_timeline_clip_scalar_provider import app_document
from tests.timeline.test_authoring_bundle import AUDIO, OLD


def test_saved_capture_authority_matches_sdk_normalized_projection():
    document = app_document()
    parent = document['parent_revision']
    config = parent['payload']['config']
    config.pop('output', None)
    config['theme_overrides'] = {'visual': {'canvas': {'width': 1280, 'height': 720, 'fps': 30}}}
    config['tracks'][0]['scale'] = 2
    parent['payload']['clips'][0]['rect'] = {'x': 10, 'y': 10, 'width': 100, 'height': 100}
    config['clips'][0]['rect'] = copy.deepcopy(parent['payload']['clips'][0]['rect'])
    parent['content_digest'] = _digest(parent['payload'])
    child = document['internal_timeline_revisions'][0]
    child['payload'].pop('output', None)
    child['payload'].pop('theme_overrides', None)
    for track in child['payload']['tracks']:
        track.setdefault('app', {})['scaleAppliesToPositionedClips'] = True
    child['content_digest'] = _digest(child['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'], document['internal_timeline_revisions'], [
        {'media_id': value, 'content_digest': value} for value in (OLD, AUDIO)], [])
    original = copy.deepcopy(document)

    class Reader:
        def get_project_parent_composition_revision(self, project, timeline, revision):
            return {**copy.deepcopy(parent), 'project_id': project, 'timeline_id': timeline}
        def get_project_shot_revision(self, project, shot, revision):
            return {**copy.deepcopy(next(row for row in document['shot_revisions'] if row['revision_id'] == revision)),
                    'project_id': project}
        def get_project_timeline_revision(self, project, timeline, revision):
            return {**copy.deepcopy(next(row for row in document['internal_timeline_revisions'] if row['revision_id'] == revision)),
                    'project_id': project}

    parent_row, projected, expansion = _project_exact_parent_head(
        client=Reader(), project_id=document['project_id'], timeline_id=document['timeline_id'],
        parent_revision_id=parent['revision_id'])
    authority = create_runtime_commit_service().project_capture(document)
    assert authority['config'] == projected.config
    assert authority['registry'] == projected.registry
    assert authority['expansion'] == {**expansion, 'parent_content_digest': parent_row['content_digest']}
    assert any(f.get('kind') == 'rect-alias' for f in authority['expansion']['placement_migrations'])
    assert any(f.get('kind') == 'canvas-not-required' for f in authority['expansion']['placement_migrations'])
    assert document == original


def test_candidate_capture_authority_matches_sdk_projection():
    from astrid.core.timeline.authoring_bundle import preview_authoring_candidate
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.authoring_render_preview import candidate_projection
    document = app_document()
    parent = document['parent_revision']
    parent['payload']['config']['output'].update(resolution='1280x720', file='out.mp4')
    parent['payload']['config']['tracks'][0]['scale'] = 2
    parent['payload']['clips'][0]['rect'] = {'x': 10, 'y': 10, 'width': 100, 'height': 100}
    parent['payload']['config']['clips'][0]['rect'] = copy.deepcopy(parent['payload']['clips'][0]['rect'])
    parent['content_digest'] = _digest(parent['payload'])
    child = document['internal_timeline_revisions'][0]
    for track in child['payload']['tracks']:
        track.setdefault('app', {})['scaleAppliesToPositionedClips'] = True
    child['content_digest'] = _digest(child['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'], document['internal_timeline_revisions'], [
        {'media_id': value, 'content_digest': value} for value in (OLD, AUDIO)], [])
    original = copy.deepcopy(document)
    preview = preview_authoring_candidate(bundle_from_document(document))

    class Reader:
        def get_project_parent_composition_revision(self, project, timeline, revision):
            return {**copy.deepcopy(parent), 'project_id': project, 'timeline_id': timeline}
        def get_project_shot_revision(self, project, shot, revision):
            return {**copy.deepcopy(next(row for row in document['shot_revisions'] if row['revision_id'] == revision)), 'project_id': project}
        def get_project_timeline_revision(self, project, timeline, revision):
            return {**copy.deepcopy(next(row for row in document['internal_timeline_revisions'] if row['revision_id'] == revision)), 'project_id': project}

    built = candidate_projection(preview, client=Reader(), project_id=document['project_id'])
    authority = create_runtime_commit_service().project_capture(document, authoring_preview=preview)
    assert authority['config'] == built['projected'].config
    assert authority['registry'] == built['projected'].registry
    assert authority['expansion']['working_copy'] == built['marker']
    from astrid.sdk.timeline_filmstrip import _open_candidate_input_closure
    config, registry, expansion = _open_candidate_input_closure(
        Reader(), project_id=document['project_id'], timeline_id=document['timeline_id'], preview=preview)
    expansion.pop('_host_transition_contract')
    assert authority == {'config': config, 'registry': registry, 'expansion': expansion}
    assert document == original


def test_standalone_legacy_canvas_is_read_only_and_shared_with_capture_authority():
    document = app_document()
    parent = document['parent_revision']
    parent['payload']['config'].pop('output', None)
    parent['payload']['config'].pop('theme_overrides', None)
    parent['content_digest'] = _digest(parent['payload'])
    child = document['internal_timeline_revisions'][0]
    for track in child['payload']['tracks']:
        track.setdefault('app', {})['scaleAppliesToPositionedClips'] = True
    child['content_digest'] = _digest(child['payload'])
    document['dependency_manifest'] = _manifest(document['shot_revisions'], document['internal_timeline_revisions'], [
        {'media_id': value, 'content_digest': value} for value in (OLD, AUDIO)], [])
    original = copy.deepcopy(document)

    class Reader:
        def get_project_parent_composition_revision(self, project, timeline, revision):
            return {**copy.deepcopy(parent), 'project_id': project, 'timeline_id': timeline}
        def get_project_shot_revision(self, project, shot, revision):
            return {**copy.deepcopy(next(row for row in document['shot_revisions'] if row['revision_id'] == revision)), 'project_id': project}
        def get_project_timeline_revision(self, project, timeline, revision):
            return {**copy.deepcopy(next(row for row in document['internal_timeline_revisions'] if row['revision_id'] == revision)), 'project_id': project}

    parent_row, projected, expansion = _project_exact_parent_head(
        client=Reader(), project_id=document['project_id'], timeline_id=document['timeline_id'],
        parent_revision_id=parent['revision_id'])
    authority = create_runtime_commit_service().project_capture(document)
    assert authority == {'config': projected.config, 'registry': projected.registry,
                         'expansion': {**expansion, 'parent_content_digest': parent_row['content_digest']}}
    finding = next(row for row in expansion['placement_migrations'] if row.get('source') == 'legacy-render-resolved')
    assert finding['after']['resolution'] == '1920x1080' and finding['after']['fps'] == 30
    assert 'output' not in document['parent_revision']['payload']['config']
    assert document == original
