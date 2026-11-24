"""Host preparation keeps fresh schemas and current per-attempt bindings."""
from __future__ import annotations

import json

import pytest

from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.packs.vibecomfy import local_server_engine, production_engine
from astrid.sdk.command_timing import TimingCollector, timing_scope
from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401

pytestmark = pytest.mark.integration


def test_host_capture_adds_no_fetch_and_live_state_keeps_fresh_attempt_prefix(comfy, tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    loaded = production_engine.load_workflow_path(comfy.workflow, tmp_path / "author", comfy_url=comfy.url)
    bundle = production_engine._canonical_bundle_value(loaded.resolved)
    original = comfy.workflow.read_bytes()
    prefixes = []
    for number in range(2):
        destination = tmp_path / f"attempt-{number}"
        client = ComfyTargetClient(ComfyTarget(comfy.url, "attached"))
        local_server_engine._live_preflight(bundle, None, comfy.url, destination, client=client)
        capture = client.node_list_capture
        assert capture is not None and json.loads(capture.raw) == capture.observed
        fetched = len([call for call in comfy.calls if call[1] == "/object_info"])
        collector = TimingCollector()
        with timing_scope(collector):
            derived = local_server_engine.local_server_bundle(bundle, destination,
                output_identity=f"attempt-{number}", node_capture=capture)
        assert len([call for call in comfy.calls if call[1] == "/object_info"]) == fetched
        prefixes.append(derived.workflow.compile("api")["2"]["inputs"]["filename_prefix"])
        events = [(event["outcome"], event.get("reason")) for event in collector.cache_event_snapshot()
                  if event["cache"] == "prepared_template"]
        # Live NodeSchema is outside the submitted closed recipe contract.
        # Keep it for the real compiler and report a full preparation explicitly.
        assert events == [("miss", "entry_invalid"), ("recompute", "entry_invalid")]
        assert bundle.workflow.nodes["1"].metadata["schema"] is not None
    assert prefixes == ["astrid_astrid_attempt-0", "astrid_astrid_attempt-1"]
    assert comfy.workflow.read_bytes() == original
    assert not [call for call in comfy.calls if call[0] == "POST"]
