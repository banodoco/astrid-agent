"""Managed mask pickers are dynamic while other live enums stay strict."""

from __future__ import annotations

import dataclasses
import importlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrid.packs.vibecomfy import production_engine
from tests.packs.vibecomfy.test_local_server_media import _archive, _continuation_workflow

pytestmark = pytest.mark.integration  # pinned VibeComfy checkout


@pytest.fixture
def comfy_mask(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    monkeypatch.setenv("VIBECOMFY_HEADLESS", "1")
    monkeypatch.chdir(tmp_path)
    schema = {
        "VHS_LoadVideoFFmpeg": {"input": {"required": {"video": [["original.mp4"], {"video_upload": True}]}},
                                "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False},
        "ContinueFrames": {"input": {"required": {"frames": ["IMAGE"]}},
                           "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False},
        "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "astrid"}]}},
                      "output": [], "output_node": True},
    }
    (tmp_path / "node_index.json").write_text(json.dumps([
        {"class_type": "VHS_LoadVideoFFmpeg", "pack": "vhs", "inputs": {"video": {"type": "STRING", "required": True}},
         "outputs": [{"type": "IMAGE", "name": "IMAGE"}]},
        {"class_type": "ContinueFrames", "pack": "test", "inputs": {"frames": {"type": "IMAGE", "required": True}},
         "outputs": [{"type": "IMAGE", "name": "IMAGE"}]},
        {"class_type": "SaveImage", "pack": "core", "inputs": {
            "images": {"type": "IMAGE", "required": True},
            "filename_prefix": {"type": "STRING", "required": True}}, "outputs": []},
    ]))
    user_output = tmp_path / "user-output"
    user_output.mkdir()
    state = SimpleNamespace(schema=schema, calls=[], prompts=[], uploads=[], output=user_output, histories={})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            state.calls.append(("GET", self.path))
            if self.path == "/object_info":
                self.send(state.schema)
            elif self.path == "/system_stats":
                self.send({"system": {"comfyui_version": "0.26.0"}, "devices": []})
            elif self.path.startswith("/history/"):
                self.send({self.path.rsplit("/", 1)[1]: state.histories[self.path.rsplit("/", 1)[1]]})
            elif self.path == "/queue":
                self.send({"queue_running": [], "queue_pending": []})
            else:
                self.send({})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.calls.append(("POST", self.path))
            if self.path == "/upload/image":
                state.uploads.append(raw)
                name = raw.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
                self.send({"name": "server-renamed.png", "subfolder": "astrid/task-1", "type": "input"})
                return
            body = json.loads(raw or b"{}")
            if self.path == "/prompt":
                state.prompts.append(body["prompt"])
                prompt_id = f"ours-{len(state.prompts)}"
                filename = f"astrid_{len(state.prompts)}.png"
                Image.new("RGB", (16, 16), (len(state.prompts), 10, 20)).save(user_output / filename)
                state.histories[prompt_id] = {
                    "outputs": {"3": {"images": [{"filename": filename, "subfolder": "", "type": "output"}]}},
                    "status": {"status_str": "success", "completed": True, "messages": []},
                }
                self.send({"prompt_id": prompt_id, "number": len(state.prompts), "node_errors": {}})
            else:
                self.send({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    state.workflow = tmp_path / "continuation.json"
    state.workflow.write_text(json.dumps(_continuation_workflow()))
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()



def _mask_workflow(channel: str) -> dict:
    return {
        "last_node_id": 3, "last_link_id": 2, "version": 0.4,
        "nodes": [
            {"id": 99, "type": "LoadImageMask", "pos": [0, 0], "size": [200, 100],
             "flags": {}, "order": 0, "mode": 0,
             "inputs": [{"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": None},
                        {"name": "channel", "type": "COMBO", "widget": {"name": "channel"}, "link": None}],
             "outputs": [{"name": "MASK", "type": "MASK", "links": [1], "slot_index": 0}],
             "properties": {"Node name for S&R": "LoadImageMask"}, "widgets_values": ["old.png", channel]},
            {"id": 2, "type": "MaskToImage", "pos": [250, 0], "size": [200, 100],
             "flags": {}, "order": 1, "mode": 0,
             "inputs": [{"name": "mask", "type": "MASK", "link": 1}],
             "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [2], "slot_index": 0}],
             "properties": {"Node name for S&R": "MaskToImage"}, "widgets_values": []},
            {"id": 3, "type": "SaveImage", "pos": [500, 0], "size": [200, 100],
             "flags": {}, "order": 2, "mode": 0,
             "inputs": [{"name": "images", "type": "IMAGE", "link": 2}],
             "outputs": [], "properties": {"Node name for S&R": "SaveImage"}, "widgets_values": ["mask"]},
        ],
        "links": [[1, 99, 0, 2, 0, "MASK"], [2, 2, 0, 3, 0, "IMAGE"]],
    }


def _configure_mask(comfy, tmp_path, monkeypatch, channel: str) -> Path:
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import emit_bundle_with_candidate, load_bundle

    comfy.schema["LoadImageMask"] = {
        "input": {"required": {"image": [["old.png"], {"image_upload": True}],
                               "channel": [["alpha", "red", "green", "blue"]]}},
        "output": ["MASK"], "output_name": ["MASK"], "output_node": False,
    }
    comfy.schema["MaskToImage"] = {"input": {"required": {"mask": ["MASK"]}},
                                   "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False}
    index_path = tmp_path / "node_index.json"
    index = json.loads(index_path.read_text())
    index.extend([
        {"class_type": "LoadImageMask", "pack": "core", "inputs": {
            "image": {"type": "STRING", "required": True}, "channel": {"type": "STRING", "required": True}},
         "outputs": [{"type": "MASK", "name": "MASK"}]},
        {"class_type": "MaskToImage", "pack": "core", "inputs": {"mask": {"type": "MASK", "required": True}},
         "outputs": [{"type": "IMAGE", "name": "IMAGE"}]},
    ])
    index_path.write_text(json.dumps(index))
    comfy.workflow.write_text(json.dumps(_mask_workflow(channel)))
    real_load = production_engine.load_workflow_path

    def load(*args, **kwargs):
        loaded = real_load(*args, **kwargs)
        bundle = production_engine._canonical_bundle_value(loaded.resolved)
        workflow = bundle.workflow.clone()
        node = next(n for n in workflow.nodes.values() if n.class_type == "LoadImageMask")
        workflow.register_input("source_mask", node.id, "image", "", type="COMBO", media_semantics="mask")
        root = Path(args[1]) / "declared-mask-workflow"
        root.mkdir(parents=True, exist_ok=True)
        derived = emit_bundle_with_candidate(
            workflow, root / "workflow.py", Provenance.USER_CONFIRMED, bundle.ui_sidecar,
            parent_revision=bundle.revision_id, operation="authored",
            parent_evidence={"revision_id": bundle.revision_id, "workflow_identity": bundle.workflow_identity},
            source_format="ready_template",
        )
        return dataclasses.replace(loaded, resolved=load_bundle(derived.python_path, trust=Provenance.USER_CONFIRMED))

    monkeypatch.setattr(production_engine, "load_workflow_path", load)
    mask = tmp_path / "mask.png"
    Image.new("L", (4, 4), 255).save(mask)
    return _archive(tmp_path / "assets.zip", {"source_mask": (mask, mask.read_bytes())})


def test_managed_mask_passes_stale_picker_and_queues_uploaded_name(comfy_mask, tmp_path, monkeypatch):
    archive = _configure_mask(comfy_mask, tmp_path, monkeypatch, "red")
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    result = action._run_and_settle(
        comfy_mask.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
        comfy_url=comfy_mask.url, comfy_output_dir=str(comfy_mask.output), managed_assets=str(archive),
    )
    assert result["managed_generation_result_path"]
    assert len(comfy_mask.uploads) == 1
    body = comfy_mask.uploads[0]
    staged_name = body.split(b'filename="', 1)[1].split(b'"', 1)[0].decode()
    assert staged_name.endswith("-mask.png") and staged_name != "old.png"
    assert comfy_mask.prompts[0]["99"]["inputs"] == {
        "image": "astrid/task-1/server-renamed.png", "channel": "red",
    }
    posts = [path for method, path in comfy_mask.calls if method == "POST"]
    assert posts.index("/upload/image") < posts.index("/prompt")


def test_invalid_mask_channel_fails_before_upload(comfy_mask, tmp_path, monkeypatch):
    archive = _configure_mask(comfy_mask, tmp_path, monkeypatch, "invalid")
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    with pytest.raises(production_engine.ProductionEngineError, match="channel"):
        action._run_and_settle(
            comfy_mask.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy_mask.url, comfy_output_dir=str(comfy_mask.output), managed_assets=str(archive),
        )
    assert not comfy_mask.uploads and not comfy_mask.prompts
