"""Commit-list requests keep their merge mode and declared reads explicit."""
from __future__ import annotations

import json

import pytest

from astrid.sdk import timeline
from tests.sdk.test_timeline_sync import connected, open_session  # noqa: F401


def _command():
    return {
        "schema_version": 1,
        "op": "clip.move",
        "version": "1",
        "select": "parent/clip:a",
        "args": {"on": 3},
    }


@pytest.mark.parametrize("surface", ["timeline", "session"])
def test_list_commit_defaults_to_fields_and_empty_reads(connected, surface):
    client, generated, path = connected

    if surface == "timeline":
        timeline.open("project-slug", "timeline-slug", client=client,
                      outbox_path=path).commit([_command()])
    else:
        open_session(connected).commit_many([_command()])

    body = json.loads(generated.calls[-1][0])
    assert body["merge"] == "fields"
    assert body["reads"] == []


@pytest.mark.parametrize("surface", ["timeline", "session"])
def test_list_commit_preserves_explicit_reads_and_atomic_merge(connected, surface):
    client, generated, path = connected
    reads = [{"kind": "scope", "ref": "timeline", "path": []}]

    if surface == "timeline":
        timeline.open("project-slug", "timeline-slug", client=client,
                      outbox_path=path).commit([_command()], merge="atomic", reads=reads)
    else:
        open_session(connected).commit_many([_command()], merge="atomic", reads=reads)

    body = json.loads(generated.calls[-1][0])
    assert body["merge"] == "atomic"
    assert body["reads"] == reads


def test_function_commit_keeps_atomic_timeline_scope_read(connected):
    client, generated, path = connected
    opened = timeline.open("project-slug", "timeline-slug", client=client,
                           outbox_path=path)

    opened.commit(lambda working: working.move("parent/clip:a", 3))

    body = json.loads(generated.calls[-1][0])
    assert body["merge"] == "atomic"
    assert body["steps"][0]["reads"] == [
        {"kind": "scope", "ref": "timeline", "path": []},
    ]
