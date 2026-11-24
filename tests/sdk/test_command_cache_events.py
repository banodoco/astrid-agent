from __future__ import annotations

import io
import json
import threading

import pytest

from astrid.sdk.command_timing import (
    TimingCollector, TimingOutput, cache_event, current_cache_events,
    print_cache_events, print_timings, stage, timing_scope,
)


def test_ordering_and_fields() -> None:
    collector = TimingCollector()
    with timing_scope(collector):
        cache_event("pack", "miss")
        cache_event("pack", "recompute", "schema changed")
        cache_event("graph", "hit", None)
        assert current_cache_events() == collector.cache_event_snapshot()
    assert collector.cache_event_snapshot() == [
        {"cache": "pack", "outcome": "miss"},
        {"cache": "pack", "outcome": "recompute", "reason": "schema changed"},
        {"cache": "graph", "outcome": "hit"},
    ]


@pytest.mark.parametrize("outcome", ["", "HIT", "hits", "evict", None])
def test_invalid_outcome_raises(outcome) -> None:
    with timing_scope(TimingCollector()):
        with pytest.raises(ValueError):
            cache_event("pack", outcome)
    with pytest.raises(ValueError):
        cache_event("pack", outcome)


@pytest.mark.parametrize("cache", ["", None, 3])
def test_invalid_cache_raises(cache) -> None:
    with pytest.raises(ValueError):
        cache_event(cache, "hit")


def test_no_collector_is_silent_noop(capsys) -> None:
    assert cache_event("pack", "hit", "why") is None
    assert current_cache_events() == []
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_events_do_not_alter_snapshot_rows() -> None:
    collector = TimingCollector()
    with timing_scope(collector):
        with stage("work"):
            pass
        before = collector.snapshot()
        cache_event("pack", "hit")
        cache_event("pack", "miss", "cold")
        after = collector.snapshot()
    assert [sorted(row) for row in before] == [sorted(row) for row in after]
    assert [row["stage"] for row in before] == [row["stage"] for row in after] == ["work", "other command work"]
    assert all("cache" not in row["stage"] for row in after)
    assert collector.snapshot_with_span()[0][-1]["stage"] == "other command work"


def _emit(collector: TimingCollector, payload: dict) -> dict:
    out = io.StringIO()
    TimingOutput(out, collector).write(json.dumps(payload))
    return json.loads(out.getvalue())


def test_json_output_has_cache_events_only_when_present() -> None:
    plain = TimingCollector()
    result = _emit(plain, {"ok": True})
    assert "cache_events" not in result
    assert set(result) == {"ok", "timings"}

    collector = TimingCollector()
    with timing_scope(collector):
        cache_event("pack", "recompute", "stale")
    result = _emit(collector, {"ok": True})
    assert result["cache_events"] == [{"cache": "pack", "outcome": "recompute", "reason": "stale"}]
    assert [row["stage"] for row in result["timings"]] == ["other command work"]


def test_thread_safety_smoke() -> None:
    collector = TimingCollector()
    threads, per_thread = 8, 200

    def work(index: int) -> None:
        with timing_scope(collector):
            for n in range(per_thread):
                cache_event(f"c{index}", "hit", str(n))

    workers = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    events = collector.cache_event_snapshot()
    assert len(events) == threads * per_thread
    for index in range(threads):
        mine = [int(e["reason"]) for e in events if e["cache"] == f"c{index}"]
        assert mine == list(range(per_thread))


def test_human_timings_lines() -> None:
    collector = TimingCollector()
    with timing_scope(collector):
        with stage("work"):
            pass
        cache_event("pack", "hit")
        cache_event("graph", "recompute", "inputs changed")
    table = io.StringIO()
    print_timings(collector.snapshot(), file=table)
    assert "cache" not in table.getvalue()

    lines = io.StringIO()
    print_cache_events(collector.cache_event_snapshot(), file=lines)
    assert lines.getvalue().splitlines() == [
        "cache pack: hit",
        "cache graph: recompute (inputs changed)",
    ]
    empty = io.StringIO()
    print_cache_events([], file=empty)
    assert empty.getvalue() == ""
