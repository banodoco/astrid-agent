from __future__ import annotations

import time

import pytest

from astrid.sdk.command_timing import TimingCollector, timing_scope
from astrid.sdk import host_bootstrap
from tests.sdk.test_host_recovery import FAKE_PID, Harness, _supervise


@pytest.fixture
def isolated_harness(tmp_path, monkeypatch):
    # The test harness builds its own source tree; do not bind it to the
    # wrapper's separately selected VibeComfy checkout.
    monkeypatch.delenv("ASTRID_VIBECOMFY_CHECKOUT", raising=False)
    h = Harness(tmp_path, monkeypatch)
    h.ensure()
    h.launches.clear()
    yield h
    h.cleanup()


def test_supervised_starting_host_wait_is_recorded_by_production_readiness_stage(
    isolated_harness, monkeypatch
):
    h = isolated_harness
    _supervise(h)
    ready_record = (h.support / "generic-host.ready.json").read_bytes()
    (h.support / "generic-host.ready.json").unlink()
    polls = []

    monkeypatch.setattr(host_bootstrap, "_registered_host_status", lambda _state: "ours")
    real_sleep = time.sleep

    def publish_ready(seconds):
        polls.append(seconds)
        real_sleep(0.02)
        (h.support / "generic-host.ready.json").write_bytes(ready_record)

    monkeypatch.setattr(host_bootstrap.time, "sleep", publish_ready)
    from astrid.core.gateway import dispatch

    monkeypatch.setattr(
        dispatch,
        "compose_profile_handoff",
        lambda *_args, **_kwargs: pytest.fail("attach must preserve the resident boot manifest"),
    )
    collector = TimingCollector()
    with timing_scope(collector):
        result = host_bootstrap.ensure_pack_host(
            h.value, reconfigure_action="reconfigure", attach_healthy=True
        )

    readiness = [row for row in collector.snapshot() if row["stage"] == "pack host readiness"]
    assert result["host_status"] == "ready"
    assert result["host_pid"] == FAKE_PID
    assert len(polls) == 1
    assert len(readiness) == 1
    assert readiness[0]["seconds"] >= 0.015
    assert h.launches == []
