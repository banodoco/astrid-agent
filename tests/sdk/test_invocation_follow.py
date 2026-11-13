from __future__ import annotations

from types import SimpleNamespace

from astrid.sdk import invocation


def _result(value, *, ok=True):
    return SimpleNamespace(ok=ok, data=value, error=None)


def test_queued_task_reports_what_it_waits_for(monkeypatch, capsys, tmp_path):
    now = [0.0]
    task = {
        "task_id": "queued-1",
        "project_id": "project-1",
        "capability_id": "generation.generate_image",
        "state": "queued",
        "waiting_reason": "waiting_for_worker",
    }
    shows = iter([_result(task), _result(task), _result(task), _result({**task, "state": "succeeded"})])
    client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _: next(shows)))
    from astrid.sdk import storage_root
    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: tmp_path)
    from astrid.sdk import host_bootstrap
    support_root = tmp_path / "runtime"
    support_root.mkdir()
    (support_root / "generic-host.json").write_text(
        '{"support_root": "' + str(support_root) + '", "pid": 12345, "executor_id": "astrid-pack-host"}',
        encoding="utf-8",
    )
    (support_root / "generic-host.busy.json").write_text(
        '{"pid": 12345, "active": 1, "updated_at": "2026-11-13T00:00:00+00:00"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    monkeypatch.setattr(invocation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(invocation.time, "sleep", lambda delay: now.__setitem__(0, now[0] + 5))

    result, ok, _ = invocation._wait_for_kernel_task_loop(
        client, task_id="queued-1", run_id="run-1", timeout_seconds=30, poll_seconds=1
    )

    captured = capsys.readouterr()
    assert ok and result["state"] == "completed"
    assert captured.err.count("current executor occupancy:") == 1
    assert "registered_host=astrid-pack-host active_tasks=1" in captured.err


def test_queued_follow_uses_task_waiting_details_without_listing(monkeypatch, capsys):
    now = [0.0]
    task = {
        "task_id": "queued-1",
        "state": "queued",
        "waiting_details": {"holder_task_id": "runtime-holder"},
    }
    shows = iter([_result(task), _result(task), _result(task), _result({**task, "state": "succeeded"})])
    client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _: next(shows)))
    monkeypatch.setattr(invocation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(invocation.time, "sleep", lambda delay: now.__setitem__(0, now[0] + 5))

    invocation._wait_for_kernel_task_loop(
        client, task_id="queued-1", run_id="run-1", timeout_seconds=30, poll_seconds=1
    )

    assert "current executor occupancy: waiting_details.holder_task_id=runtime-holder" in capsys.readouterr().err


def test_queued_follow_reports_why_runtime_occupancy_is_unavailable(monkeypatch, tmp_path):
    from astrid.sdk import storage_root
    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: tmp_path)
    holders, unavailable = invocation._queued_task_holders({"state": "queued"})

    assert holders == []
    assert unavailable and "unavailable" in unavailable


def test_waiting_holder_occurs_once_per_line(monkeypatch, capsys):
    now = [0.0]
    task = {"task_id": "queued-1", "state": "queued", "waiting_reason": "waiting_for_worker",
            "waiting_details": {"holder_task_id": "runtime-holder"}}
    shows = iter([_result(task), _result(task), _result(task), _result({**task, "state": "succeeded"})])
    client = SimpleNamespace(tasks=SimpleNamespace(show=lambda _: next(shows)))
    monkeypatch.setattr(invocation.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(invocation.time, "sleep", lambda _: now.__setitem__(0, now[0] + 5))
    invocation._wait_for_kernel_task_loop(client, task_id="queued-1", run_id="run-1",
                                          timeout_seconds=30, poll_seconds=1)
    lines = [line for line in capsys.readouterr().err.splitlines() if "runtime-holder" in line]
    assert lines and all(line.count("holder_task_id=runtime-holder") == 1 for line in lines)
    assert all("inspect running tasks" not in line for line in lines)


def test_idle_and_follow_share_the_occupancy_reader(tmp_path, monkeypatch):
    import json
    from astrid.sdk import host_bootstrap
    for active in (0, 1):
        (tmp_path / "generic-host.busy.json").write_text(json.dumps({"pid": 123, "active": active}))
        assert host_bootstrap.host_occupancy(123, tmp_path) == active
        assert host_bootstrap._host_idle(123, tmp_path) is (active == 0)
    for active in (True, "1", -1, None):
        (tmp_path / "generic-host.busy.json").write_text(json.dumps({"pid": 123, "active": active}))
        assert host_bootstrap.host_occupancy(123, tmp_path) is None
    assert host_bootstrap.host_occupancy(456, tmp_path) is None
