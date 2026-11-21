"""Author shot fixtures without replacing Runtime's accepted closure edges."""
from __future__ import annotations

import copy


def authored_shot_patch(accepted, authored):
    """Replace authored fields while retaining each managed field's exact state."""
    managed = ('internal_timeline_revision_id', 'shot_id', 'revision_id')
    payload = {key: copy.deepcopy(value) for key, value in authored.items()
               if key not in managed}
    payload.update({key: copy.deepcopy(accepted[key]) for key in managed if key in accepted})
    return payload


def assert_reopened_shared_shot(service, project, timeline, document):
    """Follow the stored parent and shot links before any test retry or inverse."""
    shot, = document['shot_revisions']
    internal, = document['internal_timeline_revisions']
    parent = service.get_project_parent_composition_revision(
        project, timeline, document['parent_revision']['revision_id'])
    assert parent['payload'] == document['parent_revision']['payload']
    assert parent['dependency_manifest'] == document['dependency_manifest']
    assert {row['shot_revision_id'] for row in parent['payload']['occurrences']} == {
        shot['revision_id']}
    reopened = service.get_project_shot_revision(project, shot['shot_id'], shot['revision_id'])
    assert reopened['payload'] == shot['payload']
    assert reopened['content_digest'] == shot['content_digest']
    assert reopened['internal_timeline_revision_id'] == internal['revision_id']
    assert reopened['payload']['internal_timeline_revision_id'] == internal['revision_id']
    source = service.get_project_timeline_revision(
        project, internal['timeline_id'], reopened['internal_timeline_revision_id'])
    assert source['payload'] == internal['payload']
    assert source['content_digest'] == internal['content_digest']
