from __future__ import annotations

import importlib
import json
import socket

import httpx

import pytest

from astrid.core.generation.backends.vibecomfy import VibeComfyEngine
from astrid.core.generation.backends.vibecomfy_local_server import cleanup_prompts
from tests.packs.vibecomfy.test_local_server_run import comfy as comfy  # noqa: F401


_INSTANCE = "6f0555d9-f8c0-47e7-bcc7-8b51d79ab3cc"
_DIGEST = "a" * 64


def _prepare(engine, fingerprint="same", **kwargs):
    return engine.prepare_session(
        fingerprint, "resident", runtime_instance_id=_INSTANCE,
        model_bytes_digest=_DIGEST, **kwargs,
    )


def _free_calls(comfy):
    return [call for call in comfy.calls if call[1].split("?", 1)[0] in {"/api/free", "/free"}]


def test_models_stay_loaded_between_settled_jobs_on_warm_session(comfy, tmp_path, monkeypatch):
    """Compatible warm jobs settle and release their prompts without freeing models."""
    original_connect = socket.socket.connect

    def connect_loopback(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
            raise OSError("external connections are forbidden")
        return original_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect_loopback)
    original_send = httpx.Client.send

    def send_loopback(client, request, *args, **kwargs):
        if request.url.host not in {"127.0.0.1", "::1"}:
            raise RuntimeError("external requests are forbidden")
        return original_send(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "send", send_loopback)
    del comfy.schema["SeedImage"]["input"]["required"]["ckpt_name"]
    workflow = json.loads(comfy.workflow.read_text())
    workflow["nodes"][0]["widgets_values"].pop()
    comfy.workflow.write_text(json.dumps(workflow))
    index_path = tmp_path / "node_index.json"
    index = json.loads(index_path.read_text())
    del index[1]["inputs"]["ckpt_name"]
    index_path.write_text(json.dumps(index))
    runtime = importlib.import_module("vibecomfy.runtime.run")
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    original_run = runtime.run_sync
    marker = object()
    attempt = [0]

    def run_task(workflow, *args, **kwargs):
        if workflow is not marker:
            return original_run(workflow, *args, **kwargs)
        number = attempt[0]
        attempt[0] += 1
        destination = tmp_path / f"attempt-{number}" / "outputs"
        result = action._run_and_settle(
            comfy.workflow, destination, task_identity=f"task-{number}",
            attempt_identity=f"attempt-{number}", comfy_url=comfy.url,
            comfy_output_dir=str(comfy.output),
        )
        cleanup_prompts(comfy.url, destination, confirm=True)
        return result

    monkeypatch.setattr(runtime, "run_sync", run_task)
    engine = VibeComfyEngine(comfy.url)
    for number in range(2):
        decision = _prepare(engine)
        assert decision["warm_reused"] is (number == 1)
        result = engine.run(marker, runtime_instance_id=_INSTANCE)
        assert result["managed_generation_result"]["task_id"] == f"task-{number}"
        assert engine.warm
        assert not _free_calls(comfy)
    assert len(comfy.prompts) == 2
    assert {call[1] for call in comfy.calls} >= {"/history/ours-1", "/history/ours-2"}


@pytest.mark.parametrize("condition", ["poisoned", "fence_pending", "cancel", "release", "incompatible"])
def test_free_occurs_for_each_containment_condition(comfy, condition):
    engine = VibeComfyEngine(comfy.url)
    if condition == "poisoned":
        engine._poisoned = True
        _prepare(engine, cold=True)
    elif condition == "fence_pending":
        engine._fence_pending = True
        _prepare(engine, cold=True)
    elif condition == "cancel":
        assert engine.cancel()["ok"]
    elif condition == "release":
        assert engine.release()["ok"]
    else:
        engine.adopt_warmth(
            fingerprint="same", warmth_identity="resident", model_bytes_digest=_DIGEST,
            runtime_instance_id=_INSTANCE,
        )
        _prepare(engine, fingerprint="different")
    assert [call[1] for call in _free_calls(comfy)] == ["/api/free"]
