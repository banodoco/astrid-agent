"""LS-5 review: a stale pre-POST connection through the actual pack runner."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import select
import socket
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import pytest

from astrid.core.generation.backends.comfy_target import open_comfy_http
from astrid.core.generation.backends.vibecomfy_local_server import TargetRuntimeClient
from tests.helpers.ls5_comfy_peer import ComfyPeer
from tests.packs.vibecomfy.test_local_server_run import (
    comfy,  # noqa: F401 - unchanged integration fixture
)

pytestmark = pytest.mark.integration


def test_stale_post_reprepares_actual_pack_and_preserves_one_acceptance(comfy, tmp_path, monkeypatch):  # noqa: F811
    from astrid.packs.vibecomfy import local_server_engine

    assert Path(local_server_engine.__file__).resolve().parents[3] == Path(__file__).resolve().parents[3]
    # Keep the existing fixture's schema, prompt validation, PNG production and
    # output bytes. The packet peer adds only the real held socket boundary.
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
    preparations = []
    original_post = TargetRuntimeClient._post_prompt

    async def post(self, prompt):
        preparations.append((self.client.generation.identity,
                             peer.counts()["GET /object_info"]))
        if len(preparations) == 1:
            def idle_until_peer_closes():
                sock = self.session.pool.connection.sock
                assert select.select([sock], [], [], 5)[0]
                assert sock.recv(1, socket.MSG_PEEK) == b""
            await asyncio.to_thread(idle_until_peer_closes)
        return await original_post(self, prompt)

    monkeypatch.setattr(TargetRuntimeClient, "_post_prompt", post)
    with ComfyPeer(adapter, keepalive_timeout=1) as peer:
        action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
        output = tmp_path / "attempt" / "outputs"
        manifest = action._run_and_settle(
            comfy.workflow, output, task_identity="task-1", attempt_identity="attempt-1",
            comfy_url=peer.url,
        )
        assert len(preparations) == 2
        assert preparations[0][0] != preparations[1][0]
        assert preparations[1][1] > preparations[0][1] > 0
        assert peer.counts()["POST /prompt"] == len(comfy.prompts) == 1
        assert peer.counts()["GET /ws"] == 2
        result = next(row for row in manifest["outputs"] if row["role"] == "result")
        actual = (output / result["path"]).read_bytes()
        assert actual and hashlib.sha256(actual).hexdigest() == comfy.output_hashes["astrid_1.png"]
        journals = [json.loads(path.read_text()) for path in
                    (output / ".vibecomfy-runtime").glob("out/runs/*/attempt.json")]
        assert len(journals) == 2
        statuses = sorted(row["queue_acceptance"]["status"] for row in journals)
        assert statuses == ["accepted", "rejected"]
        accepted = next(row for row in journals if row["queue_acceptance"]["status"] == "accepted")
        assert accepted["queue_acceptance"]["prompt_id"] == "ours-1"
        assert accepted["artifact_retrieval"]["transport"] == "comfy-http-view"
        assert accepted["artifact_retrieval"]["connection"]["mode"] == "held"
        refused = next(row for row in journals if row["queue_acceptance"]["status"] == "rejected")
        assert refused["queue_acceptance"].get("prompt_id") is None
        assert "acceptance unknown" not in json.dumps(refused)
