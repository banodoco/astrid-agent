"""Hydration of Runtime-backed render snapshots for ranged visual review."""

import hashlib
import json

import pytest

from astrid.sdk.exceptions import CapabilityValidationError
from astrid.sdk.timeline_filmstrip import _hydrate_render_snapshot


def _snapshot_bytes():
    return json.dumps(
        {"config": {"clips": [{"id": "clip-1"}]}},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _envelope(raw=None, *, authority_timeline="timeline-1"):
    raw = _snapshot_bytes() if raw is None else raw
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return {
        "inputs": {
            "timeline_snapshot_ref": {
                "digest": digest,
                "project_id": "project-1",
                "timeline_id": "timeline-1",
                "revision_id": "revision-1",
                "source_kind": "revision",
            },
            "timeline_authority": {
                "project_id": "project-1",
                "timeline_id": authority_timeline,
                "parent_revision_id": "revision-1",
            },
        }
    }


class ObjectClient:
    def __init__(self, data):
        self.data = data
        self.requested = []

    def get_object(self, object_id):
        self.requested.append(object_id)
        return {"data": self.data}


def test_hydrates_sha256_snapshot_ref_and_checks_authority_identity():
    raw = _snapshot_bytes()
    envelope = _envelope(raw)
    client = ObjectClient(raw)

    hydrated = _hydrate_render_snapshot(envelope, client)

    assert hydrated["inputs"]["timeline_snapshot"] == json.loads(raw)
    assert "timeline_snapshot_ref" not in hydrated["inputs"]
    assert client.requested == [envelope["inputs"]["timeline_snapshot_ref"]["digest"]]


def test_rejects_snapshot_bytes_that_do_not_match_pinned_sha256():
    envelope = _envelope()
    client = ObjectClient(b'{"config":{"clips":[]}}')

    with pytest.raises(CapabilityValidationError, match="snapshot.*digest|digest.*snapshot"):
        _hydrate_render_snapshot(envelope, client)


def test_rejects_snapshot_ref_with_mismatched_timeline_authority():
    envelope = _envelope(authority_timeline="other-timeline")
    client = ObjectClient(_snapshot_bytes())

    with pytest.raises(CapabilityValidationError, match="snapshot.*authority|authority.*snapshot"):
        _hydrate_render_snapshot(envelope, client)


def test_legacy_inline_snapshot_is_returned_unchanged_without_object_read():
    snapshot = {"config": {"clips": [{"id": "legacy-clip"}]}}
    envelope = {"inputs": {"timeline_snapshot": snapshot}}
    client = ObjectClient(b"must not be read")

    result = _hydrate_render_snapshot(envelope, client)

    assert result == envelope
    assert client.requested == []
