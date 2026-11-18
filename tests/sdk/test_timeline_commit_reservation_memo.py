"""Reservation scans read each revision once per commit and only new history.

Counts, never clocks: the loads a creating commit makes must not grow with
history, and the memoised reserved set must equal a full scan. The memo lives
on the provider instance (one realm run), so these tests keep one service
across commits, as the Runtime does.
"""
from __future__ import annotations

import pytest

from astrid.sdk.timeline.commit import create_runtime_commit_service
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    detached,
    inverse,
    runtime_abi,  # noqa: F401  (autouse: installs the test commit ABI)
    track_create,
)

SCOPE = ("project-1", "film")


def call(realm, service, command):
    context = realm.context([command])
    return service.commit(context, DetachedPort(realm, context))


def grow(realm, service, count):
    for number in range(count):
        call(realm, service, track_create(f"track-{number}"))


def closure_reads(realm):
    return sum(read[0] == "revision" for read in realm.reads)


def creating_commit_loads(entries):
    realm, service = DetachedRealm(), create_runtime_commit_service()
    grow(realm, service, entries - 1)
    realm.reads.clear()
    call(realm, service, track_create("measured"))
    return closure_reads(realm), [read for read in realm.reads if read[0] == "history"]


def test_loads_per_creating_commit_do_not_grow_with_history():
    small, small_history = creating_commit_loads(3)
    large, large_history = creating_commit_loads(30)
    assert small == large
    assert large <= 4  # the base, the newest entry's two boundaries, the head
    # Only the entries newer than the memoised publication are paged.
    assert all(read[2] is not None for read in small_history + large_history)


def test_memoised_reserved_set_equals_a_full_scan_at_every_commit():
    def run(warm):
        realm, service = DetachedRealm(), create_runtime_commit_service()
        rows = []
        commands = [track_create(f"track-{number}") for number in range(4)]
        commands.append(track_create("later"))
        for command in commands:
            call(realm, service, command)
            rows.append(service._reserved[SCOPE])
            service = service if warm else create_runtime_commit_service()
        call(realm, service, inverse([realm.head["publication_id"]]))
        service = service if warm else create_runtime_commit_service()
        for number in range(4):
            call(realm, service, track_create(f"after-{number}"))
            rows.append(service._reserved[SCOPE])
            service = service if warm else create_runtime_commit_service()
        return rows

    warm, cold = run(True), run(False)
    assert warm == cold
    assert "parent/track:later" in warm[-1][2]


def test_retired_identity_is_still_refused_from_a_warm_memo():
    realm, service = DetachedRealm(), create_runtime_commit_service()
    call(realm, service, track_create("warmup"))
    call(realm, service, track_create())
    call(realm, service, inverse([realm.head["publication_id"]]))
    call(realm, service, track_create("other"))
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        call(realm, service, track_create())
    assert error.value.failure["details"]["reason"] == "identity_collision"
    assert realm.head == head


@pytest.mark.parametrize("foreign", [("publication-99", "parent-99"),
                                     ("bootstrap", "another-revision")])
def test_a_memo_row_from_outside_this_history_falls_back_to_a_full_scan(foreign):
    realm, service = DetachedRealm(), create_runtime_commit_service()
    call(realm, service, track_create())
    call(realm, service, inverse([realm.head["publication_id"]]))
    head = detached(realm.head)
    service._reserved[SCOPE] = (*foreign, frozenset(), False)
    with pytest.raises(CommitRejected) as error:
        call(realm, service, track_create())
    assert error.value.failure["details"]["reason"] == "identity_collision"
    assert realm.head == head
    assert service._reserved[SCOPE][:2] == (head["publication_id"], head["revision_id"])


@pytest.mark.parametrize("damage", ["missing_entry", "ancestry", "unknown_intent", "records",
                                   "missing_created", "null_created", "created_item",
                                   "created_claim", "recorder_abi", "receipt_base", "previous"])
def test_damage_newer_than_the_memo_still_fails_closed(damage):
    realm, service = DetachedRealm(), create_runtime_commit_service()
    realm.page_size = 1
    call(realm, service, track_create("first"))
    call(realm, service, track_create("second"))
    call(realm, service, track_create("third"))
    memo = service._reserved[SCOPE]  # the memo sits before the entries below
    position = next(i for i, row in enumerate(realm.entries) if row["publication_id"] == memo[0])
    entry = realm.entries[position + 1]
    if damage == "missing_entry":
        realm.entries.remove(entry)
    elif damage == "ancestry":
        entry["previous"]["publication_id"] = "missing"
    elif damage == "unknown_intent":
        entry["intent"] = "unknown"
    elif damage == "records":
        entry["records"] = None
    elif damage == "missing_created":
        del entry["records"]["commands"][0]["created"]
    elif damage == "null_created":
        entry["records"]["commands"][0]["created"] = None
    elif damage == "created_item":
        entry["records"]["commands"][0]["created"] = [{"ref": "parent/track:first"}]
    elif damage == "created_claim":
        entry["records"]["commands"][0]["created"] = []
    elif damage == "recorder_abi":
        entry["recorder_abi"] = 9
    elif damage == "receipt_base":
        del entry["receipt"]["result"]
    elif damage == "previous":
        entry["previous"] = None
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        call(realm, service, track_create("different-new-id"))
    assert error.value.failure["details"]["reason"] == "history_unavailable"
    assert realm.head == head
    assert service._reserved[SCOPE] == memo  # only a clean scan is stored
