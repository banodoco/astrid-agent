"""Missing immutable snapshot objects should become actionable review errors."""

import hashlib

import pytest

from astrid.sdk.exceptions import CapabilityValidationError
from astrid.sdk.timeline_filmstrip import _hydrate_render_snapshot
from astrid.sdk.workspace_client import WorkspaceClientError


def test_missing_render_snapshot_object_is_reported_as_a_rerenderable_error():
    raw = b'{"config":{"clips":[]}}'
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    envelope = {
        "inputs": {
            "timeline_snapshot_ref": {
                "digest": digest,
                "project_id": "project-1",
                "timeline_id": "timeline-1",
                "revision_id": "revision-1",
            },
            "timeline_authority": {
                "project_id": "project-1",
                "timeline_id": "timeline-1",
                "parent_revision_id": "revision-1",
            },
        },
    }

    class MissingObjectClient:
        def get_object(self, object_id):
            assert object_id == digest
            raise WorkspaceClientError(404, "not_found", "object not found", {})

    with pytest.raises(CapabilityValidationError, match=r"snapshot.*(unavailable|missing)|rerender.*timeline"):
        _hydrate_render_snapshot(envelope, MissingObjectClient())
