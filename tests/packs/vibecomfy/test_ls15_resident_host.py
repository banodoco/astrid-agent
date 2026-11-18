"""LS-15: actual admitted host jobs, held loopback peer and durable outputs."""
from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.generation.backends.comfy_target import open_comfy_http
from tests.helpers.ls5_comfy_peer import ComfyPeer
from tests.packs.vibecomfy import test_local_server_run
from tests.test_generic_host import FakeRuntime

comfy = test_local_server_run.comfy
pytestmark = pytest.mark.integration


class IsolatedPeer(ComfyPeer):
    """A real queued peer: deletion and interrupt must name one owned prompt."""
    def __init__(self, *args, **kwargs):
        self.deleted = []
        self.interrupted = []
        super().__init__(*args, **kwargs)

    async def _handle(self, request):
        if request.path == "/queue":
            from aiohttp import web
            self.calls.append((request.method, request.path))
            if request.method == "POST":
                body = await request.json()
                assert set(body) == {"delete"} and len(body["delete"]) == 1
                self.deleted.extend(body["delete"])
            return web.json_response({"queue_running": [], "queue_pending": [
                [index, prompt] for index, prompt in enumerate(self.pending)
                if prompt not in self.history and prompt not in self.deleted]})
        if request.path == "/interrupt":
            from aiohttp import web
            body = await request.json()
            assert set(body) == {"prompt_id"}
            self.interrupted.append(body["prompt_id"])
            return web.json_response({})
        return await super()._handle(request)


@pytest.fixture
def resident_comfy(comfy, tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "source-state.json"))
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    workflow = json.loads(comfy.workflow.read_text())
    workflow["nodes"][0].update(type="EmptyImage", widgets_values=[16, 16, 1, 0],
        properties={"Node name for S&R": "EmptyImage"})
    comfy.workflow.write_text(json.dumps(workflow))
    comfy.schema["EmptyImage"] = {
        "input": {"required": {"width": ["INT", {"default": 16}], "height": ["INT", {"default": 16}],
                               "batch_size": ["INT", {"default": 1}], "color": ["INT", {"default": 0}]}},
        "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False, "python_module": "nodes",
    }
    peer_adapter = SimpleNamespace(history=comfy.histories, outputs={}, views=[], input_files=set(),
                                   object_info=lambda: comfy.schema)
    def submit(body):
        request = Request(comfy.url + "/prompt", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with open_comfy_http(request, timeout=5) as response:
            result = json.load(response)
        for path in comfy.output.iterdir():
            peer_adapter.outputs[path.name] = (path.read_bytes(), "image/png")
        return 200, result
    peer_adapter.submit = submit

    class Runtime(FakeRuntime):
        def get_object(self, digest):
            return self.sources[digest]

        def cancel(self, task_id, **kwargs):
            self.cancellations.append((task_id, kwargs))
            self.tasks[task_id]["task"]["status"] = "cancelled"
            return self.tasks[task_id]

        def heartbeat(self, task_id, lease_token, *, attempt_id, fence, progress=None):
            super().heartbeat(task_id, lease_token, attempt_id=attempt_id, fence=fence, progress=progress)
            return {"task_id": task_id, "lease_id": lease_token, "attempt_id": attempt_id,
                    "fence": fence, "runtime_epoch": 1,
                    "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()}

    runtime = Runtime()
    runtime.sources, runtime.cancellations = {}, []
    pack = Path(__file__).resolve().parents[3] / "astrid/packs/vibecomfy"
    host = GenericPackHost(pack_roots=[pack], client=runtime, attempt_base=tmp_path / "attempts")
    host.discover()
    assert "vibecomfy.run" in host.capabilities
    host.register()
    with IsolatedPeer(peer_adapter) as peer:
        try:
            yield host, runtime, peer, comfy
        finally:
            host.shutdown()


def run_job(world, number, *, values=None, expected_status="completed", align=None, managed=False):
    host, runtime, peer, comfy = world
    sources = getattr(comfy, "members", {"workflow": comfy.workflow})
    descriptors = {}
    for name, path in sources.items():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        runtime.sources[digest] = path.read_bytes()
        descriptors[name] = {"digest": digest, "filename": path.name}
    task_id, attempt_id = f"ls15-task-{number}", f"ls15-attempt-{number}"
    task = {"task": {"id": task_id, "attempt_id": attempt_id, "fence": number,
        "capability": "vibecomfy.run", "project_id": "ls15-project", "runtime_epoch": 1,
        "status": "running", "input_object_ids": ["sha256:" + row["digest"] for row in descriptors.values()],
        "spec": {"spec": {"inputs": {**descriptors,
                "workflow_inputs": json.dumps(values or {}), "comfy_url": "" if managed else peer.url}}}}}
    if align is not None:
        task["task"]["spec"]["spec"]["metadata"] = {"align": align}
    runtime.tasks[task_id] = task
    outcome = host.run_task(task, lease_token=f"ls15-lease-{number}", keep_attempt=True)
    assert outcome.get("task", outcome)["status"] == expected_status
    if expected_status != "completed":
        return outcome
    result = next(row[2]["result"] for row in runtime.settlements if row[0] == task_id)
    receipt = next((host.attempt_base).glob(f"**/{task_id}-{attempt_id}/outputs/manifest.json"))
    manifest = json.loads(receipt.read_bytes())
    assert manifest["managed_generation_result"]["task_id"] == task_id
    produced = [row for row in manifest["outputs"] if row["role"] == "result"]
    assert len(produced) == 1
    output = receipt.parent / produced[0]["path"]
    assert "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest() == produced[0]["content_hash"]
    journal = next(receipt.parent.glob(".vibecomfy-runtime/out/runs/*/attempt.json"))
    record = json.loads(journal.read_bytes())
    native_result = json.loads(Path(record["managed_generation_result"]["path"]).read_bytes())
    assert native_result["task_id"] == task_id and native_result["attempt_id"] == attempt_id
    assert record["queue_acceptance"]["status"] == "accepted"
    assert record["artifact_retrieval"]["connection"]["mode"] == "held"
    return result, manifest, record


def test_second_admitted_job_reuses_worker_template_and_target(resident_comfy, monkeypatch, tmp_path):
    from tests.helpers.ls15_counters import Counters
    counters = Counters(monkeypatch, tmp_path)
    host, runtime, peer, comfy = resident_comfy
    with counters.observe():
        first = run_job(resident_comfy, 1)
        process = host.managed_tool_session.current_adapter.process
        before = peer.counts()
        cold = counters.snapshot()
        second = run_job(resident_comfy, 2)
        warm = counters.difference(cold, counters.snapshot())
    (tmp_path / "warm-counters.json").write_text(json.dumps({"cold": cold, "warm": warm}, indent=2))
    for scope in ("parent", "child"):
        assert not {key: warm[scope + "_counts"].get(key) for key in counters.zero_categories
                    if warm[scope + "_counts"].get(key)}, warm
        assert warm[scope + "_counts"]["source_validation"] > 0
    assert warm["child_counts"]["per_job_authorization"] > 0
    assert warm["captures"] and all(row["files"] > 0 and row["bytes"] > 0 for row in warm["captures"])
    assert host.managed_tool_session.current_adapter.process is process
    assert first[0]["process_id"] == second[0]["process_id"] == process.pid
    assert peer.counts()["GET /object_info"] == before["GET /object_info"]
    assert peer.counts()["GET /ws"] == before["GET /ws"] == 1
    assert len(peer.posts) == 2
    assert first[2]["queue_acceptance"]["prompt_id"] != second[2]["queue_acceptance"]["prompt_id"]
    prefixes = [body["prompt"]["2"]["inputs"]["filename_prefix"] for body in peer.posts]
    assert len(set(prefixes)) == 2
    assert all(f"ls15-attempt-{index}" in prefix for index, prefix in enumerate(prefixes, 1))


def canonical_inputs(world, root):
    from vibecomfy.nodes.core import EmptyImage, SaveImage
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.templates import InputSpec, OutputSpec, ReadyMetadata, new_workflow
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate
    root.mkdir()
    with new_workflow(ReadyMetadata.build(template_id="ls15-isolation", capability="image")) as workflow:
        image = EmptyImage(_id="1", width=16, height=16, batch_size=1, color=0)
        saved = SaveImage(_id="2", images=image.out("IMAGE"), filename_prefix="ls15")
        workflow.finalize({name: InputSpec(node=image, field=name, default=value, type="INT")
                           for name, value in (("width", 16), ("height", 16), ("color", 0))},
                          outputs=[OutputSpec(node=saved)])
    emit_bundle_with_candidate(workflow, root / "workflow.py", Provenance.USER_CONFIRMED, None)
    world[3].members = {"python": root / "workflow.py", "companion": root / "workflow.vibe.json",
                        "source": world[3].workflow}


def wait_posts(peer, count, futures):
    deadline = time.monotonic() + 30
    while (len(peer.posts) < count or len(peer.pending) < count) and time.monotonic() < deadline:
        for future in futures:
            if future.done():
                future.result()
        time.sleep(0.01)
    assert len(peer.posts) == count, "both admitted jobs must submit before either can complete"
    assert len(peer.pending) == count, "both POSTs must receive their own prompt identities"


@pytest.mark.parametrize("cancel_first", [False, True], ids=["both-complete", "cancel-one"])
def test_concurrent_vibe_jobs_keep_inputs_outputs_and_cancellation_separate(resident_comfy, tmp_path, cancel_first):
    host, runtime, peer, comfy = resident_comfy
    canonical_inputs(resident_comfy, tmp_path / "authored")
    run_job(resident_comfy, 1, values={"width": 16, "height": 16, "color": 0})
    process = host.managed_tool_session.current_adapter.process
    peer.auto_complete = False
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(run_job, resident_comfy, 2, values={"width": 24, "height": 16, "color": 11},
                           expected_status="cancelled" if cancel_first else "completed")
        right = pool.submit(run_job, resident_comfy, 3, values={"width": 40, "height": 24, "color": 22})
        try:
            wait_posts(peer, 3, (left, right))
            assert not left.done() and not right.done()
            assert host.managed_tool_session.current_adapter.process is process
            prompts = list(peer.pending)[1:]
            by_attempt = {next(body["prompt"][node]["inputs"]["filename_prefix"] for node in body["prompt"]
                               if body["prompt"][node]["class_type"] == "SaveImage"): prompt
                          for body, prompt in zip(peer.posts[1:], prompts, strict=True)}
            first_prompt = next(prompt for prefix, prompt in by_attempt.items() if "ls15-attempt-2" in prefix)
            second_prompt = next(prompt for prefix, prompt in by_attempt.items() if "ls15-attempt-3" in prefix)
            if cancel_first:
                runtime.tasks["ls15-task-2"]["task"]["status"] = "cancel_requested"
                left.result(timeout=20)
                assert peer.deleted and set(peer.deleted) == {first_prompt}
                assert not right.done() and second_prompt not in peer.deleted
                assert not peer.interrupted
            else:
                peer.complete(first_prompt)
            peer.complete(second_prompt)
            results = left.result(timeout=25), right.result(timeout=25)
        finally:
            peer.auto_complete = True
            for prompt in tuple(peer.pending):
                if prompt not in peer.history and prompt not in peer.deleted:
                    peer.complete(prompt)
    observed = {next(node["inputs"]["filename_prefix"] for node in body["prompt"].values()
                     if node["class_type"] == "SaveImage"):
                next(node["inputs"] for node in body["prompt"].values() if node["class_type"] == "EmptyImage")
                for body in peer.posts[1:]}
    assert len(observed) == 2
    for prefix, values in observed.items():
        assert (values["width"], values["height"], values["color"]) == (
            (24, 16, 11) if "ls15-attempt-2" in prefix else (40, 24, 22))
    assert len(peer.posts) == 3 and peer.counts()["GET /ws"] == 1
    assert host.managed_tool_session.active
    assert host.managed_tool_session.current_adapter.process is process
    assert results[1][1]["managed_generation_result"]["task_id"] == "ls15-task-3"
    if cancel_first:
        assert [row[0] for row in runtime.cancellations] == ["ls15-task-2"]
        assert not any(row[0] == "ls15-task-2" for row in runtime.settlements)
