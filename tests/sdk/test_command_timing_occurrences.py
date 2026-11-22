from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace

import pytest

from astrid.sdk import command_timing
from astrid.sdk.command_timing import TimingCollector, stage, timing_scope


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(command_timing, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def collector_without_monitor():
    collector = TimingCollector()
    # The monitor's scheduling is unrelated to timing arithmetic; avoid a real
    # thread observing a clock that advances only when the test moves it.
    collector.monitor = SimpleNamespace(join=lambda: None)
    return collector


def by_name(rows, name):
    return [row for row in rows if row["stage"] == name]


def test_active_snapshots_are_inclusive_and_do_not_double_charge(clock):
    collector = collector_without_monitor()
    with timing_scope(collector):
        clock.now = 1.0
        with stage("parent", detail="first"):
            clock.now = 2.0
            with stage("child"):
                clock.now = 3.0
                first = collector.snapshot()
                second = collector.snapshot()
                assert first == second
                parent, child = by_name(first, "parent")[0], by_name(first, "child")[0]
                assert parent["seconds"] == 1.0
                assert parent["total_seconds"] == 2.0
                assert parent["detail"] == "first"
                assert child["seconds"] == child["total_seconds"] == 1.0
                assert child["parent"] == "parent"
                assert child["parent_id"] == parent["occurrence_id"]
                clock.now = 4.0
            clock.now = 5.0
        clock.now = 6.0
        rows = collector.snapshot()

    parent, child = by_name(rows, "parent")[0], by_name(rows, "child")[0]
    assert parent["seconds"] == 2.0
    assert parent["total_seconds"] == 4.0
    assert child["seconds"] == child["total_seconds"] == 2.0
    assert parent["parent_id"] is None
    assert by_name(rows, "other command work") == [{"stage": "other command work", "seconds": 2.0}]
    assert sum(row["seconds"] for row in rows) == 6.0


def test_repeated_names_keep_their_own_parents_and_exceptions(clock):
    collector = collector_without_monitor()
    with timing_scope(collector):
        with stage("registry"):
            clock.now = 1.0
            with stage("pack discovery"):
                clock.now = 2.0
        with stage("registry"):
            clock.now = 3.0
            with pytest.raises(ValueError, match="failure"):
                with stage("pack discovery"):
                    clock.now = 4.0
                    raise ValueError("failure")
        rows = collector.snapshot()

    parents = by_name(rows, "registry")
    children = by_name(rows, "pack discovery")
    assert len(parents) == len(children) == 2
    assert len({row["occurrence_id"] for row in parents + children}) == 4
    assert {row["parent_id"] for row in children} == {row["occurrence_id"] for row in parents}
    assert all(row["parent"] == "registry" and row["total_seconds"] == 1.0 for row in children)
    assert sum(row["seconds"] for row in rows) == 4.0


def test_interleaved_tasks_keep_parent_identity_and_exclusive_partition(clock):
    collector = collector_without_monitor()

    async def scenario():
        first_entered, second_entered, first_exit = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def first():
            with stage("first"):
                first_entered.set()
                await second_entered.wait()
                clock.now = 3.0
            first_exit.set()

        async def second():
            await first_entered.wait()
            clock.now = 1.0
            with stage("second"):
                second_entered.set()
                await first_exit.wait()
                clock.now = 4.0

        with stage("parent"):
            await asyncio.gather(first(), second())
        return collector.snapshot()

    with timing_scope(collector):
        rows = asyncio.run(scenario())

    parent = by_name(rows, "parent")[0]
    first, second = by_name(rows, "first")[0], by_name(rows, "second")[0]
    assert first["parent_id"] == second["parent_id"] == parent["occurrence_id"]
    assert first["total_seconds"] == 3.0
    assert second["total_seconds"] == 3.0
    assert sum(row["seconds"] for row in rows) == 4.0
    assert all(row["seconds"] >= 0 for row in rows)


def test_nested_and_sequential_commands_do_not_inherit_parent(clock):
    outer = collector_without_monitor()
    inner = collector_without_monitor()
    with timing_scope(outer):
        with stage("outer"):
            with timing_scope(inner):
                with stage("inner"):
                    clock.now = 1.0
            with stage("outer child"):
                clock.now = 2.0
        outer_rows = outer.snapshot()
    inner_rows = inner.snapshot()
    inner_row = by_name(inner_rows, "inner")[0]
    assert inner_row["parent_id"] is None
    outer_row = by_name(outer_rows, "outer")[0]
    assert by_name(outer_rows, "outer child")[0]["parent_id"] == outer_row["occurrence_id"]

    next_collector = collector_without_monitor()
    with timing_scope(next_collector):
        with stage("next"):
            clock.now = 3.0
        next_rows = next_collector.snapshot()
    assert by_name(next_rows, "next")[0]["parent_id"] is None
    assert not by_name(next_rows, "outer")


def test_text_display_groups_full_ancestry_and_handles_active_rows(clock):
    collector = collector_without_monitor()
    with timing_scope(collector):
        clock.now = 1.0
        for _ in range(2):
            with stage("registry"):
                with stage("scan"):
                    with stage("scan"):
                        clock.now += 1.0
        with stage("other parent"):
            with stage("scan"):
                clock.now += 1.0
                rows = collector.snapshot()

    output = StringIO()
    command_timing.print_timings(rows, file=output)
    lines = output.getvalue().splitlines()
    assert lines[0].startswith("registry ×2:")
    assert lines[1].startswith("  scan ×2:")
    assert lines[2].startswith("    scan ×2:")
    assert lines[3].startswith("other parent:")
    assert lines[4].startswith("  scan:")
    assert lines[5].startswith("other command work: 1.000s (25.0%)")
