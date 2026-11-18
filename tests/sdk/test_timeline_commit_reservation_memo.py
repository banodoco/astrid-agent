"""Reservation scans read each revision once per commit and only new history.

Counts, never clocks: the loads a creating commit makes must not grow with
history, and the memoised reserved set must equal a full scan.
"""
from __future__ import annotations

import pytest

from astrid.sdk.timeline import commit
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedRealm,
    detached,
    inverse,
    runtime_abi,  # noqa: F401  (autouse: installs the test commit ABI)
    track_create,
)


def grow(realm, count):
    for number in range(count):
        realm.call([track_create(f"track-{number}")])


def closure_reads(realm):
    return sum(read[0] == "revision" for read in realm.reads)


def creating_commit_loads(entries):
    commit._RESERVED_HISTORY.clear()
    realm = DetachedRealm()
    grow(realm, entries - 1)
    realm.reads.clear()
    realm.call([track_create("measured")])
    return closure_reads(realm), [read for read in realm.reads if read[0] == "history"]


def test_loads_per_creating_commit_do_not_grow_with_history():
    small, small_history = creating_commit_loads(3)
    large, large_history = creating_commit_loads(30)
    assert small == large
    assert large <= 4  # the base, the newest entry's two boundaries, the head
    # Only the entries newer than the memoised publication are paged.
    assert all(read[2] is not None for read in small_history + large_history)


def test_memoised_reserved_set_equals_a_full_scan_at_every_commit(monkeypatch):
    seen = {}

    def run(label, warm):
        commit._RESERVED_HISTORY.clear()
        if not warm:
            monkeypatch.setattr(commit, "_warm_reserved", lambda scope: (None, None))
        remember = commit._remember_reserved
        rows = seen[label] = []

        def spy(scope, current, reserved, recorded):
            rows.append((current, frozenset(reserved), recorded))
            remember(scope, current, reserved, recorded)

        monkeypatch.setattr(commit, "_remember_reserved", spy)
        realm = DetachedRealm()
        grow(realm, 4)
        realm.call([track_create("later")])
        realm.call([inverse([realm.head["publication_id"]])])
        grow_more = [track_create(f"after-{number}") for number in range(4)]
        for call in grow_more:
            realm.call([call])
        monkeypatch.undo()

    run("warm", True)
    run("cold", False)
    assert seen["warm"] and seen["warm"] == seen["cold"]
    assert any("parent/track:later" in row[1] for row in seen["warm"])


def test_retired_identity_is_still_refused_from_a_warm_memo():
    realm = DetachedRealm()
    realm.call([track_create("warmup")])
    realm.call([track_create()])
    realm.call([inverse([realm.head["publication_id"]])])
    realm.call([track_create("other")])
    head = detached(realm.head)
    with pytest.raises(CommitRejected) as error:
        realm.call([track_create()])
    assert error.value.failure["details"]["reason"] == "identity_collision"
    assert realm.head == head


@pytest.mark.parametrize("damage", ["missing_entry", "ancestry", "unknown_intent", "records",
                                   "missing_created", "null_created", "created_item",
                                   "created_claim", "recorder_abi", "receipt_base", "previous"])
def test_damage_newer_than_the_memo_still_fails_closed(damage):
    realm = DetachedRealm()
    realm.page_size = 1
    realm.call([track_create("first")])
    realm.call([track_create("second")])
    realm.call([track_create("third")])
    assert commit._RESERVED_HISTORY  # warm: the memo sits before the entries below
    key = next(iter(commit._RESERVED_HISTORY))
    newer = [entry for entry in realm.entries if entry["publication_id"] != key[2]
             and realm.entries.index(entry) > next(
                 i for i, row in enumerate(realm.entries) if row["publication_id"] == key[2])]
    entry = newer[0]
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
        realm.call([track_create("different-new-id")])
    assert error.value.failure["details"]["reason"] == "history_unavailable"
    assert realm.head == head
