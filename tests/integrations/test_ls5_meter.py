"""LS-5 comparison: the unchanged LS-2 journey with an asynchronous WS peer.

Keep the original meter, caller paths, marks, source custody and output/task
assertions. Only this case replaces its HTTP boundary; shared fixtures and old
reference baselines are not edited. Both sides use the same approved D10 pin.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

import pytest

from tests import _ls5_meter_observer
from tests.helpers.ls5_comfy_peer import ComfyPeer
from tests.integrations import test_local_speed_meter as meter
from tests.integrations import test_runpod_slot_path_cpu as journey
from tests.integrations.local_speed_sources import tuple_sources  # noqa: F401

pytestmark = list(meter.pytestmark)


@pytest.mark.parametrize("caller", ["resident_sdk", "fresh_python_cli"])
@pytest.mark.parametrize("sample_index", meter.sample_indices())
def test_ls5_attached_comparison(
    caller, sample_index, tmp_path, monkeypatch, real_runtime, tuple_sources, request  # noqa: F811
):
    mode = os.environ.get("ASTRID_LS5_MODE", "held")
    assert mode in {"before", "held"}
    peers, reports = [], []

    @contextlib.contextmanager
    def attached(comfy):
        with ComfyPeer(comfy, delay=0.25) as peer:
            peers.append(peer)
            yield peer.url

    monkeypatch.setattr(journey, "_attached_comfy", attached)
    original_report = meter.AttachedMeter.report

    def report(owner):
        result = original_report(owner)
        result["standin_timeline_scope"] = "LS-5 asynchronous stand-in; fixed 0.25s completion delay; no inference"
        result["ls5_comparison"] = {"mode": mode, "history_ready": "executing with node=null after history stored"}
        reports.append(result)
        return result

    monkeypatch.setattr(meter.AttachedMeter, "report", report)
    if os.environ.get("ASTRID_LS2_OBSERVE") == "1":
        original_sources = meter.sources
        extension = Path(_ls5_meter_observer.__file__).read_text()

        def sources():
            hashes, process = original_sources()
            return hashes, process + "\n" + extension + "\nProcessObserver = with_async_sleeps(ProcessObserver)\n"

        monkeypatch.setattr(meter, "sources", sources)
        monkeypatch.setattr(meter, "ProcessObserver", _ls5_meter_observer.with_async_sleeps(meter.ProcessObserver))

    meter.test_attached_caller_observation(
        caller, sample_index, tmp_path, monkeypatch, real_runtime, tuple_sources, request
    )
    assert len(peers) == len(reports) == 1
    peer, result = peers[0], reports[0]
    paths = result["attached_http_paths"]
    history_reads = sum(count for path, count in paths.items() if path.startswith("GET /history/"))
    assert paths["POST /prompt"] == 1
    assert len(peer.posts) == 2
    if mode == "held":
        assert paths["GET /ws"] == 1
        assert history_reads == 1
        for post in peer.posts:
            client_id = post["client_id"]
            assert peer.events.index(("socket", client_id)) < peer.events.index(("submit", client_id))
    else:
        assert paths.get("GET /ws", 0) == 0
        assert history_reads >= 2
        assert all("client_id" not in post for post in peer.posts)
    for prompt_id in peer.history:
        assert peer.events.index(("history", prompt_id)) < peer.events.index(("history_ready", prompt_id))
    if result["instrumented"]:
        workers = [row for row in result["process_observations"] if row["role"] == "pack_worker"]
        assert len(workers) == 1 and workers[0]["ls5_async_sleep_observed"] is True
        sleeps = workers[0]["counts"]["ls5_comfy_poll_sleep_attempt"]
        assert sleeps == 0 if mode == "held" else sleeps >= 1
        if mode == "held":
            assert workers[0]["counts"].get("sleep_attempt", 0) == 0
