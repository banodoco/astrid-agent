from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import (
    AuthoringBundleError,
    approve_authoring_candidate,
    publish_authoring_candidate,
)
from tests.timeline.test_authoring_bundle import _real_authoring_candidate


@pytest.mark.parametrize(
    ("damage", "expected_field"),
    [
        ("candidate", "candidate_digest"),
        ("candidate_digest", "candidate_digest"),
        ("publication_digest", "publication_digest"),
        ("scope", "project_id"),
        ("base", "base_parent"),
    ],
)
def test_d72_approval_binding_rejects_independent_tampering_before_commit(
    tmp_path, damage, expected_field
):
    service, candidate, writer = _real_authoring_candidate(tmp_path)
    try:
        approval = approve_authoring_candidate(
            candidate, approver="editor-1", approval_id="approval-attack-control"
        )
        submitted_candidate = copy.deepcopy(candidate)
        submitted_approval = copy.deepcopy(approval)
        if damage == "candidate":
            submitted_candidate["parent"]["config"]["approval-attack"] = True
        elif damage == "candidate_digest":
            submitted_approval["candidate_digest"] = "sha256:" + "0" * 64
        elif damage == "publication_digest":
            submitted_approval["publication_digest"] = "sha256:" + "0" * 64
        elif damage == "scope":
            submitted_approval["project_id"] = "foreign-project"
        else:
            submitted_approval["base_parent"] = {
                "revision_id": approval["base_parent"]["revision_id"],
                "content_digest": "sha256:" + "0" * 64,
            }

        with pytest.raises(
            AuthoringBundleError,
            match=rf"approval is stale or does not match candidate: {expected_field}",
        ):
            publish_authoring_candidate(
                submitted_candidate,
                writer,
                idempotency_key="approval-attack-control",
                approval=submitted_approval,
            )
        assert writer.calls == []
    finally:
        service.close()
