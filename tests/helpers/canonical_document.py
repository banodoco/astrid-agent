"""Read a current accepted closure through the SDK's authenticated projection."""
from __future__ import annotations

from functools import partial
from types import SimpleNamespace


def current_canonical_document(service, project, timeline, *, authorize):
    from astrid.sdk.timeline.authoring_commit import _read_base
    from astrid.sdk.timeline.commit import document_from_bundle

    head = service.get_project_timeline(project, timeline)
    parent = service.get_project_parent_composition_revision(
        project, timeline, head['head_revision_id'])
    writer = SimpleNamespace(
        list_timeline_commits=partial(service.list_timeline_commits, authorize=authorize),
        get_project_parent_composition_revision=service.get_project_parent_composition_revision,
        get_project_shot_revision=service.get_project_shot_revision,
        get_project_timeline_revision=service.get_project_timeline_revision,
    )
    # _read_base checks every exact owner/pin, keeps complete payloads/digests,
    # projects canonical fields, and compares the closure with its receipt.
    bundle = _read_base(writer, {
        'project_id': project, 'timeline_id': timeline,
        'base_parent': {'revision_id': head['head_revision_id'],
                        'publication_id': head['head_publication_id'],
                        'content_digest': parent['content_digest']},
    })
    return document_from_bundle(bundle)
