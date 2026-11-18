"""Unsent preparation and socket loss through the unchanged actual pack runner."""
from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import pytest

from astrid.core.generation.backends.comfy_target import ComfyTargetClient, open_comfy_http
from astrid.core.generation.backends.vibecomfy_local_server import TargetRuntimeClient
from tests.core.generation.backends.test_comfy_held_rework import peer_closed_http
from tests.core.generation.backends.test_comfy_preparation_rework import drop_before_post
from tests.helpers.ls5_comfy_peer import ComfyPeer
from tests.packs.vibecomfy.test_local_server_run import comfy  # noqa: F401 - unchanged fixture

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("boundary", ["preparation", "websocket"])
def test_unsent_preparation_retries_actual_pack_once(boundary, comfy, tmp_path, monkeypatch):  # noqa: F811
    from astrid.packs.vibecomfy import local_server_engine

    assert Path(local_server_engine.__file__).resolve().parents[3] == Path(__file__).resolve().parents[3]
    adapter = SimpleNamespace(history=comfy.histories, outputs={}, views=[], input_files=set(),
                              object_info=lambda: comfy.schema)

    def submit(body):
        request = Request(comfy.url + "/prompt", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with open_comfy_http(request, timeout=5) as response:
            result = json.load(response)
        for path in comfy.output.iterdir():
            adapter.outputs[path.name] = (path.read_bytes(), "image/png")
        return 200, result

    adapter.submit = submit
    preparations, injected = [], []
    original_info, original_post = ComfyTargetClient.object_info, TargetRuntimeClient._post_prompt

    def info(self):
        result = original_info(self)
        if self.held_session is not None and self not in preparations:
            preparations.append(self)
            assert not self._prompt_attempted and not self._witnessed_prompts
        if boundary == "preparation" and self.held_session is not None and not injected:
            injected.append(self.generation.identity)
            peer_closed_http(self.held_session)
            self.system_stats()  # A real preparation read; no prompt has been reserved.
        return result

    async def post(self, prompt):
        if boundary == "websocket" and not injected:
            injected.append(self.client.generation.identity)
            drop_before_post(self.session, peer)
        return await original_post(self, prompt)

    monkeypatch.setattr(ComfyTargetClient, "object_info", info)
    monkeypatch.setattr(TargetRuntimeClient, "_post_prompt", post)
    with ComfyPeer(adapter, keepalive_timeout=1) as peer:
        action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
        output = tmp_path / "attempt" / "outputs"
        manifest = action._run_and_settle(
            comfy.workflow, output, task_identity="task-1", attempt_identity="attempt-1", comfy_url=peer.url)
        assert len(injected) == 1 and len(preparations) == 2
        assert preparations[0].generation.retired
        assert preparations[0].generation.identity != preparations[1].generation.identity
        assert peer.counts()["GET /object_info"] >= 2
        assert peer.counts()["GET /ws"] == 2
        assert peer.counts()["POST /prompt"] == len(comfy.prompts) == 1
        assert not preparations[0]._witnessed_prompts
        result = next(row for row in manifest["outputs"] if row["role"] == "result")
        actual = (output / result["path"]).read_bytes()
        assert actual and hashlib.sha256(actual).hexdigest() == comfy.output_hashes["astrid_1.png"]
        journals = [json.loads(path.read_text()) for path in
                    (output / ".vibecomfy-runtime").glob("out/runs/*/attempt.json")]
        assert len(journals) == (1 if boundary == "preparation" else 2)
        accepted = [row for row in journals if row["queue_acceptance"]["status"] == "accepted"]
        assert len(accepted) == 1 and accepted[0]["queue_acceptance"]["prompt_id"] == "ours-1"
        assert accepted[0]["artifact_retrieval"]["transport"] == "comfy-http-view"
        assert accepted[0]["artifact_retrieval"]["connection"]["mode"] == "held"
        for row in journals:
            if row["queue_acceptance"]["status"] != "accepted":
                assert row["queue_acceptance"]["status"] == "rejected"
                assert row["queue_acceptance"].get("prompt_id") is None
                assert "acceptance unknown" not in json.dumps(row)
