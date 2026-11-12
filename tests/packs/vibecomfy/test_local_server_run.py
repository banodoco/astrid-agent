from __future__ import annotations

import hashlib
import importlib
import json
import logging
import threading
from urllib.parse import parse_qs, urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrid.packs.vibecomfy import local_server_engine, production_engine
from astrid.packs.vibecomfy.local import pack_images

pytestmark = pytest.mark.integration  # pinned VibeComfy checkout


def _workflow():
    return {
        "last_node_id": 2, "last_link_id": 1, "version": 0.4,
        "nodes": [
            {"id": 1, "type": "SeedImage", "pos": [0, 0], "size": [200, 100],
             "flags": {}, "order": 0, "mode": 0, "inputs": [],
             "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1], "slot_index": 0}],
             "properties": {"Node name for S&R": "SeedImage", "models": [{
                 "name": "installed.safetensors", "subdir": "checkpoints",
                 "url": "https://example.invalid/installed.safetensors", "sha256": "0" * 64,
                 "size_bytes": 1,
             }]}, "widgets_values": [42, "installed.safetensors"]},
            {"id": 2, "type": "SaveImage", "pos": [300, 0], "size": [200, 100],
             "flags": {}, "order": 1, "mode": 0,
             "inputs": [{"name": "images", "type": "IMAGE", "link": 1}],
             "outputs": [], "properties": {"Node name for S&R": "SaveImage"},
             "widgets_values": ["astrid"]},
        ],
        "links": [[1, 1, 0, 2, 0, "IMAGE"]],
    }


@pytest.fixture
def comfy(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    monkeypatch.setenv("VIBECOMFY_HEADLESS", "1")
    monkeypatch.chdir(tmp_path)
    schema = {
        "SeedImage": {"input": {"required": {"seed": ["INT", {"default": 42}],
                                               "asset": [["installed.safetensors"]]}},
                      "output": ["IMAGE"], "output_name": ["IMAGE"], "output_node": False},
        "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "astrid"}]}},
                      "output": [], "output_node": True},
    }
    (tmp_path / "node_index.json").write_text(json.dumps([
        {"class_type": "LoadImage", "pack": "core", "inputs": {"image": {"type": "STRING", "required": True}},
         "outputs": [{"type": "IMAGE", "name": "IMAGE"}, {"type": "MASK", "name": "MASK"}]},
        {"class_type": "SeedImage", "pack": "test", "inputs": {"seed": {"type": "INT", "required": True},
         "asset": {"type": "STRING", "required": True, "choices": ["installed.safetensors"]}},
         "outputs": [{"type": "IMAGE", "name": "IMAGE"}]},
        {"class_type": "SaveImage", "pack": "core", "inputs": {
            "images": {"type": "IMAGE", "required": True},
            "filename_prefix": {"type": "STRING", "required": True}}, "outputs": []},
    ]))
    user_output = tmp_path / "user-output"
    user_output.mkdir()
    state = SimpleNamespace(schema=schema, calls=[], prompts=[], pending=False,
                            output=user_output, histories={}, output_hashes={}, history_images=None)

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
            state.calls.append(("GET", self.path, None))
            if self.path == "/object_info":
                self.send(state.schema)
            elif self.path == "/system_stats":
                self.send({"system": {"comfyui_version": "0.26.0"}, "devices": []})
            elif self.path.startswith("/history/"):
                prompt_id = self.path.rsplit("/", 1)[1]
                self.send({} if state.pending else {prompt_id: state.histories[prompt_id]})
            elif self.path == "/queue":
                self.send({"queue_running": [], "queue_pending": []})
            elif self.path.startswith("/view?"):
                name = parse_qs(urlsplit(self.path).query)["filename"][0]
                data = (user_output / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send({})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/upload/image":
                state.calls.append(("POST", self.path, raw))
                self.send({"name": "uploaded.png", "subfolder": "astrid/task-1", "type": "input"})
                return
            body = json.loads(raw or b"{}")
            state.calls.append(("POST", self.path, body))
            if self.path == "/prompt":
                state.prompts.append(body["prompt"])
                prompt_id = f"ours-{len(state.prompts)}"
                filename = f"astrid_{len(state.prompts)}.png"
                Image.new("RGB", (16, 16), (len(state.prompts), 10, 20)).save(user_output / filename)
                state.output_hashes[filename] = hashlib.sha256((user_output / filename).read_bytes()).hexdigest()
                images = state.history_images or [{"filename": filename, "subfolder": "", "type": "output"}]
                state.histories[prompt_id] = {
                    "outputs": {str(index + 2): {"images": [image]} for index, image in enumerate(images)},
                    "status": {"status_str": "success", "completed": True, "messages": []},
                }
                self.send({"prompt_id": prompt_id, "number": len(state.prompts), "node_errors": {}})
            else:
                self.send({})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    state.workflow = tmp_path / "workflow.json"
    state.workflow.write_text(json.dumps(_workflow()))
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_installed_custom_node_model_without_placement_converts(comfy):
    from vibecomfy.porting.import_service import import_workflow_bytes

    workflow = _workflow()
    workflow["nodes"][0]["properties"].pop("models")
    # The installed model is exposed by the live schema on the original loader field.
    comfy.schema["SeedImage"]["input"]["required"]["ckpt_name"] = (
        comfy.schema["SeedImage"]["input"]["required"].pop("asset")
    )
    index_path = Path("node_index.json")
    index = json.loads(index_path.read_text())
    inputs = next(node for node in index if node["class_type"] == "SeedImage")["inputs"]
    inputs["ckpt_name"] = inputs.pop("asset")
    index_path.write_text(json.dumps(index))

    artifacts = import_workflow_bytes(json.dumps(workflow).encode(), workflow_id="installed-custom")
    assert artifacts.report["validation"]["structural_and_parity"]["ok"] is True
    warning = next(issue for issue in artifacts.report["diagnostics"]
                   if issue["code"] == "filename_only_asset_candidate")
    assert warning["detail"]["name"] == "installed.safetensors"
    assert warning["detail"]["subdir"] is None
    assert warning["detail"]["placement_reason"] == "no known directory for loader class 'SeedImage'"


def test_real_server_url_writes_managed_result_and_records_fresh_seed(comfy, tmp_path):
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    seeds = []
    output_names = []
    original_workflow = comfy.workflow.read_bytes()
    for number in range(2):
        out = tmp_path / f"attempt-{number}" / "outputs"
        manifest = action._run_and_settle(
            comfy.workflow, out, task_identity=f"task-{number}", attempt_identity=f"attempt-{number}",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output),
        )
        managed = manifest["managed_generation_result"]
        assert managed["task_id"] == f"task-{number}"
        expected_schema_digest = "sha256:" + hashlib.sha256(
            json.dumps(comfy.schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        assert (
            managed["evidence"]["producer"]["runtime"]["schema_provenance"]
            ["workflow_conversion_object_info_sha256"]
            == expected_schema_digest
        )
        seeds.append(managed["inputs"]["seed"])
        assert (out / manifest["managed_generation_result_path"]).is_file()
        result = next(row for row in manifest["outputs"] if row["role"] == "result")
        original = comfy.output / f"astrid_{number + 1}.png"
        output_names.append(original.name)
        assert hashlib.sha256((out / result["path"]).read_bytes()).digest() == hashlib.sha256(original.read_bytes()).digest()
        assert hashlib.sha256(original.read_bytes()).hexdigest() == comfy.output_hashes[original.name]
        # The attempt's source.json sits beside its derived workflow, not in the cwd.
        assert (out / "local-server-workflow" / "source.json").is_file()
        assert not (tmp_path / "source.json").exists()
    assert seeds[0] != seeds[1]
    assert len(set(output_names)) == 2
    assert [prompt["1"]["inputs"]["seed"] for prompt in comfy.prompts] == seeds
    assert not [call for call in comfy.calls if call[0] == "POST" and call[1] != "/prompt"]
    assert not [
        call for call in comfy.calls
        if call[1] in {"/api/free", "/queue/clear"}
        or (call[1] == "/queue" and isinstance(call[2], dict) and call[2].get("clear"))
    ]
    assert not [call for call in comfy.calls if call[1] == "/interrupt"]
    assert comfy.workflow.read_bytes() == original_workflow


def test_missing_live_node_fails_before_queueing(comfy, tmp_path):
    del comfy.schema["SeedImage"]
    with pytest.raises(production_engine.ProductionEngineError, match="missing nodes: SeedImage.*Missing nodes and models"):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )
    assert not comfy.prompts


def test_attached_workflow_uses_view_custody_without_shared_output_directory(comfy, tmp_path, monkeypatch):
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient

    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    operations = []
    for name in ("submit_prompt", "history", "download_output"):
        original = getattr(ComfyTargetClient, name)

        def record(self, *args, _name=name, _original=original, **kwargs):
            operations.append((_name, self.target.kind, self.target.url))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(ComfyTargetClient, name, record)
    manifest = action._run_and_settle(
        comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
        attempt_identity="attempt-1", comfy_url=comfy.url,
    )
    result = next(row for row in manifest["outputs"] if row["role"] == "result")
    assert (tmp_path / "attempt" / "outputs" / result["path"]).read_bytes() == (comfy.output / "astrid_1.png").read_bytes()
    assert {name for name, _, _ in operations} == {"submit_prompt", "history", "download_output"}
    assert all(kind == "attached" and url == comfy.url for _, kind, url in operations)
    assert any(path.startswith("/view?") for _, path, _ in comfy.calls)
    assert [(method, path) for method, path, _ in comfy.calls if method == "POST"] == [("POST", "/prompt")]
    journal = next((tmp_path / "attempt" / "outputs" / ".vibecomfy-runtime").glob("out/runs/*/attempt.json"))
    evidence = json.loads(journal.read_text())
    assert evidence["adapter"]["kind"] == "external"
    assert evidence["artifact_retrieval"]["transport"] == "comfy-http-view"
    assert evidence["artifacts"][0]["download_source"] == "external_http"
    assert "lifecycle_retrieval" not in evidence


def test_unknown_connected_node_fails_live_preflight_before_staging(comfy, tmp_path):
    workflow = _workflow()
    workflow["nodes"][0].update(
        type="AstridDefinitelyMissingNode",
        properties={"Node name for S&R": "AstridDefinitelyMissingNode"},
        widgets_values=[],
    )
    comfy.workflow.write_text(json.dumps(workflow))
    original_workflow = comfy.workflow.read_bytes()
    assert "AstridDefinitelyMissingNode" not in comfy.schema

    with pytest.raises(
        production_engine.ProductionEngineError,
        match=r"missing nodes: AstridDefinitelyMissingNode.*Missing nodes and models",
    ):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )

    assert not comfy.prompts
    assert not [call for call in comfy.calls if call[0] == "POST"]
    assert comfy.workflow.read_bytes() == original_workflow


@pytest.mark.parametrize("choices", [[], ["other.safetensors"]])
def test_missing_model_fails_before_queueing(comfy, tmp_path, choices):
    comfy.schema["SeedImage"]["input"]["required"]["ckpt_name"] = [choices]
    with pytest.raises(production_engine.ProductionEngineError, match="missing models: installed.safetensors.*Missing nodes and models"):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )
    assert not comfy.prompts


@pytest.mark.parametrize('default_image', ['original.png', ''])
def test_managed_image_is_uploaded_and_bound_to_load_image(comfy, tmp_path, default_image):
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    workflow = _workflow()
    workflow["nodes"][0].update(type="LoadImage", widgets_values=[default_image],
                                 inputs=[{"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": None}],
                                 properties={"Node name for S&R": "LoadImage"})
    comfy.workflow.write_text(json.dumps(workflow))
    comfy.schema.pop("SeedImage")
    comfy.schema["LoadImage"] = {"input": {"required": {"image": [["original.png"], {"image_upload": True}]}},
                                "output": ["IMAGE", "MASK"], "output_node": False}
    source = tmp_path / "portrait.png"
    Image.new("RGB", (16, 16)).save(source)
    archive = tmp_path / "images.zip"
    pack_images(str(archive), [str(source)])
    result = action._run_and_settle(
        comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
        comfy_url=comfy.url, comfy_output_dir=str(comfy.output), managed_assets=str(archive),
    )
    assert result["managed_generation_result_path"]
    assert comfy.prompts[0]["1"]["inputs"]["image"] == "astrid/task-1/uploaded.png"
    upload = next(body for method, path, body in comfy.calls if path == "/upload/image")
    assert b"astrid/task-1" in upload


def test_connected_blank_image_without_binding_is_refused(comfy, tmp_path):
    workflow = _workflow()
    workflow["nodes"][0].update(type="LoadImage", widgets_values=[""],
        inputs=[{"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": None}],
        properties={"Node name for S&R": "LoadImage"})
    comfy.workflow.write_text(json.dumps(workflow))
    comfy.schema.pop("SeedImage")
    comfy.schema["LoadImage"] = {"input": {"required": {"image": [["available.png"], {"image_upload": True}]}},
                                 "output": ["IMAGE", "MASK"], "output_node": False}
    with pytest.raises(production_engine.ProductionEngineError):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server")
    assert not comfy.prompts


def test_explicit_seed_is_preserved(comfy, tmp_path):
    result = production_engine.run_workflow_result_path(
        comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
        comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        workflow_input_bindings={"seed": 7},
    )
    assert result.managed_generation_result_path.is_file()
    assert result.managed_generation_result["inputs"]["seed"] == 7


def test_profile_identity_requires_exact_local_semantics():
    profile = production_engine._PROFILE_CONFIGS["local_server"]
    assert production_engine._profile_id(profile) == "local_server"
    with pytest.raises(production_engine.ProductionEngineError, match="identity"):
        production_engine._profile_id({**profile, "warm_policy": "auto"})


def test_prompt_timeout_deletes_only_accepted_prompt(comfy, tmp_path, monkeypatch):
    comfy.pending = True
    monkeypatch.setattr(local_server_engine, "_LOCAL_PROMPT_TIMEOUT_SEC", 0.05)
    with pytest.raises(Exception, match="ours-1.*did not complete"):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )
    posts = [(path, body) for method, path, body in comfy.calls if method == "POST"]
    assert ("/queue", {"delete": ["ours-1"]}) in posts
    assert not any(path in {"/interrupt", "/api/free"} for path, body in posts)


def test_server_mode_fails_closed_without_managed_result(comfy, tmp_path, monkeypatch):
    runtime = importlib.import_module("vibecomfy.runtime.run")
    monkeypatch.setattr(runtime, "run_sync", lambda *_args, **_kwargs: SimpleNamespace(outputs=[], managed_generation_result_path=None))
    with pytest.raises(production_engine.ProductionEngineError, match="requires VibeComfy managed_generation_result_path"):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, profile_id="local_server",
        )
    assert not comfy.prompts


def test_prompt_cleanup_failure_is_debug_only_and_preserves_attempt_error(
    comfy, tmp_path, monkeypatch, caplog, capsys,
):
    runtime = importlib.import_module("vibecomfy.runtime.run")
    original_error = RuntimeError("attempt failed first")
    cleanup_error = RuntimeError("prompt cleanup failed second")

    def fail_attempt(*_args, **_kwargs):
        raise original_error

    def fail_cleanup(*_args, **_kwargs):
        raise cleanup_error

    monkeypatch.setattr(runtime, "run_sync", fail_attempt)
    from astrid.core.generation.backends import vibecomfy_local_server
    monkeypatch.setattr(vibecomfy_local_server, "cleanup_prompts", fail_cleanup)
    caplog.set_level(logging.DEBUG, logger=local_server_engine.__name__)

    with pytest.raises(RuntimeError, match="attempt failed first") as raised:
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )

    assert raised.value is original_error
    assert any("local_server prompt cleanup failed: prompt cleanup failed second" in note
               for note in getattr(original_error, "__notes__", []))
    records = [record for record in caplog.records if record.name == local_server_engine.__name__]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert records[0].getMessage().endswith(str(tmp_path / "attempt" / "outputs"))
    assert "RuntimeError: prompt cleanup failed second" in records[0].exc_text
    assert "Traceback (most recent call last)" in records[0].exc_text
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("comfy_url", ["http://192.168.1.9:8188", "http://example.com:8188"])
def test_local_server_rejects_non_loopback_before_execution(comfy, tmp_path, comfy_url):
    with pytest.raises((ValueError, production_engine.ProductionEngineError)):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy_url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )
    assert not comfy.prompts


def test_multiple_outputs_without_filename_prefix_fail_closed(comfy, tmp_path):
    workflow = _workflow()
    workflow["last_node_id"] = 3
    source = workflow["nodes"][0]
    workflow["nodes"].append({
        "id": 3, "type": "SaveImage", "pos": [300, 150], "size": [200, 100],
        "flags": {}, "order": 2, "mode": 0,
        "inputs": [{"name": "images", "type": "IMAGE", "link": 2}],
        "outputs": [], "properties": {"Node name for S&R": "SaveImage"}, "widgets_values": [""],
    })
    workflow["links"].append([2, source["id"], 0, 3, 0, "IMAGE"])
    comfy.workflow.write_text(json.dumps(workflow))
    Image.new("RGB", (16, 16), (50, 20, 10)).save(comfy.output / "second.png")
    comfy.history_images = [
        {"filename": "astrid_1.png", "subfolder": "", "type": "output"},
        {"filename": "second.png", "subfolder": "", "type": "output"},
    ]

    with pytest.raises(production_engine.ProductionEngineError, match="managed_generation_result_path"):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )


@pytest.mark.parametrize("escape_kind", ["parent", "symlink"])
def test_output_path_escape_fails_closed(comfy, tmp_path, escape_kind):
    outside = tmp_path / "outside.png"
    Image.new("RGB", (16, 16), (70, 20, 10)).save(outside)
    if escape_kind == "parent":
        image = {"filename": "../outside.png", "subfolder": "", "type": "output"}
    else:
        (comfy.output / "escape.png").symlink_to(outside)
        image = {"filename": "escape.png", "subfolder": "", "type": "output"}
    comfy.history_images = [image]

    with pytest.raises(Exception, match="(?i)escape|outside|symlink|path|unsafe|managed"):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1",
            comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
        )
    assert not (tmp_path / "attempt" / "outputs").exists() or not list((tmp_path / "attempt" / "outputs").glob("**/outside.png"))


def test_local_session_pins_environment_and_host_owned_configuration(comfy, tmp_path, monkeypatch):
    runtime = importlib.import_module("vibecomfy.runtime.run")
    monkeypatch.setenv("VIBECOMFY_WARM", "always")
    monkeypatch.setenv("VIBECOMFY_COMFY_CONFIGURATION", '{"server_url":"https://foreign"}')
    monkeypatch.setenv("COMFYUI_PATH", "/unused-comfyui")
    original = runtime.run_sync

    def run(record, bundle, **kwargs):
        import os
        assert os.environ["VIBECOMFY_WARM"] == "never"
        assert os.environ["VIBECOMFY_COMFY_CONFIGURATION"] == "{}"
        assert os.environ["COMFYUI_PATH"] == "/unused-comfyui"
        assert "ensure_models" not in kwargs
        cfg = kwargs["config"]
        assert cfg.extra["task_id"] == "task-1"
        assert cfg.extra["attempt_id"] == "attempt-1"
        assert cfg.extra["prompt_timeout_sec"] < 3600
        assert "base_directory" not in cfg.extra
        return original(record, bundle, **kwargs)

    monkeypatch.setattr(runtime, "run_sync", run)
    production_engine.run_workflow_result_path(
        comfy.workflow, tmp_path / "attempt" / "outputs", task_identity="task-1", attempt_identity="attempt-1",
        comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id="local_server",
    )
    import os
    assert os.environ["VIBECOMFY_WARM"] == "always"
    assert os.environ["VIBECOMFY_COMFY_CONFIGURATION"] == '{"server_url":"https://foreign"}'


def test_local_server_retains_host_owned_configuration_guard(tmp_path):
    bundle = SimpleNamespace(workflow=SimpleNamespace(metadata={"comfy_configuration": {"server_url": "https://foreign"}}))
    with pytest.raises(production_engine.ProductionEngineError, match="host-owned keys: server_url"):
        production_engine._workflow_comfy_configuration(bundle)


@pytest.mark.parametrize("termination", ["terminate", "kill"])
def test_child_termination_and_host_release_contain_only_our_prompt(comfy, tmp_path, termination):
    import os
    import subprocess
    import sys
    import time

    from astrid.core.execution.generic_host import _ManagedLocalServerTaskAdapter
    import vibecomfy

    comfy.pending = True
    attempt = tmp_path / "attempt"
    out = attempt / "outputs"
    out.mkdir(parents=True)
    (attempt / "node_index.json").write_bytes((tmp_path / "node_index.json").read_bytes())
    process = subprocess.Popen(
        [sys.executable, "-m", "astrid.packs.vibecomfy.actions.run.run", "run", str(comfy.workflow),
         "--out", str(out), "--task-identity", "task-1", "--attempt-identity", "attempt-1",
         "--comfy-url", comfy.url, "--comfy-output-dir", str(comfy.output)],
        cwd=attempt, env={**os.environ, "PYTHONPATH": os.pathsep.join((
            str(Path(__file__).resolve().parents[3]),
            str(Path(vibecomfy.__file__).resolve().parents[1]),
        ))},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    signal = threading.Event()
    adapter = _ManagedLocalServerTaskAdapter(
        signal, url=comfy.url, destination=out, process_census=lambda: process.poll() is None,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            journals = list((out / ".vibecomfy-runtime").glob("out/runs/*/attempt.json"))
            if any(json.loads(path.read_text()).get("queue_acceptance", {}).get("prompt_id") == "ours-1" for path in journals):
                break
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(f"child exited before acceptance: {stdout}\n{stderr}")
            time.sleep(0.02)
        else:
            pytest.fail("child did not persist its prompt witness")
        if termination == "terminate":
            adapter.cancel(reason="user_cancel")
            assert signal.is_set()
        getattr(process, termination)()
        process.communicate(timeout=15)
        release_reason = "host_deadline" if termination == "kill" else "child_stopped"
        assert adapter.release(reason=release_reason)["released"] is True
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=15)
    deletes = [body for method, path, body in comfy.calls if method == "POST" and path == "/queue"]
    assert deletes and all(body == {"delete": ["ours-1"]} for body in deletes)
    assert not any(path in {"/api/free", "/interrupt"} for _, path, _ in comfy.calls)


def test_host_command_runs_local_server_on_dynamic_port(comfy, tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import GenericPackHost
    import vibecomfy

    # The shared test environment can install a different dependency pin.
    # Exercise this checkout in the real child, as in the parent test process.
    monkeypatch.setattr(
        "astrid.core.execution.generic_host._dependency_pythonpath",
        lambda: (str(Path(vibecomfy.__file__).resolve().parents[1]),),
    )

    pack_root = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "vibecomfy"
    host = GenericPackHost(pack_roots=[pack_root])
    host.discover()
    attempt = tmp_path / "host-attempt"
    out = attempt / "outputs"
    out.mkdir(parents=True)
    (attempt / "node_index.json").write_bytes((tmp_path / "node_index.json").read_bytes())
    result = host._run_command_definition(
        host.capabilities["vibecomfy.run"],
        {"workflow": str(comfy.workflow), "comfy_url": comfy.url,
         "comfy_output_dir": str(comfy.output)},
        out, attempt,
    )
    assert result.returncode == 0
    assert (out / "managed-generation" / "managed-generation-result.json").is_file()
    assert comfy.prompts
    assert not any(path == "/api/free" for _, path, _ in comfy.calls)
    evidence = json.loads((attempt / "network-evidence.json").read_text())
    assert evidence["policy"]["allowed_destinations"] == [comfy.url.removeprefix("http://")]


def test_seedless_rerun_uses_attempt_specific_save_prefix(comfy, tmp_path):
    workflow = _workflow()
    workflow["nodes"][0]["widgets_values"] = ["installed.safetensors"]
    comfy.workflow.write_text(json.dumps(workflow))
    del comfy.schema["SeedImage"]["input"]["required"]["seed"]
    index_path = tmp_path / "node_index.json"
    index = json.loads(index_path.read_text())
    del next(node for node in index if node["class_type"] == "SeedImage")["inputs"]["seed"]
    index_path.write_text(json.dumps(index))
    original = comfy.workflow.read_bytes()
    prefixes = []
    for number in range(2):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / f"attempt-{number}" / "outputs", task_identity="same-task",
            attempt_identity=f"attempt-{number}", comfy_url=comfy.url,
            comfy_output_dir=str(comfy.output), profile_id="local_server",
        )
        assert "seed" not in comfy.prompts[-1]["1"]["inputs"]
        prefixes.append(comfy.prompts[-1]["2"]["inputs"]["filename_prefix"])
    assert prefixes[0] != prefixes[1]
    assert all(prefix.startswith("astrid") for prefix in prefixes)
    assert comfy.workflow.read_bytes() == original


@pytest.mark.parametrize('prefix', ['custom/name', ''])
def test_explicit_save_prefix_keeps_attempt_namespace_and_user_evidence(comfy, tmp_path, prefix):
    from vibecomfy.workflow_bundle import emit_bundle

    loaded = production_engine.load_workflow_path(comfy.workflow, tmp_path / 'author', comfy_url=comfy.url)
    bundle = production_engine._canonical_bundle_value(loaded.resolved)
    bundle.workflow.register_input('save_name', '2', 'filename_prefix', 'astrid', type='STRING')
    emitted = emit_bundle(bundle.workflow, tmp_path / 'canonical/workflow.py', {'operation': 'authored'})
    result = production_engine.run_workflow_result_path(
        emitted.python_path, tmp_path / 'attempt/outputs', task_identity='task', attempt_identity='attempt-1',
        comfy_url=comfy.url, comfy_output_dir=str(comfy.output), profile_id='local_server',
        workflow_input_bindings={'save_name': prefix})
    assert comfy.prompts[-1]['2']['inputs']['filename_prefix'] == prefix + '_astrid_attempt-1'
    assert result.managed_generation_result['inputs']['save_name'] == prefix


def test_explicit_trust_is_unverified_in_native_attempt_and_published_receipt(comfy, tmp_path, monkeypatch):
    monkeypatch.setenv('ASTRID_ALIGN', 'trust')
    result = production_engine.run_workflow_result_path(
        comfy.workflow, tmp_path / 'attempt' / 'outputs', task_identity='task-trust',
        attempt_identity='attempt-trust', comfy_url=comfy.url, profile_id='local_server',
    )
    receipt_path = Path(result.managed_generation_result_path)
    receipt = json.loads(receipt_path.read_text())
    attempt = json.loads(receipt_path.with_name('attempt.json').read_text())
    expected = {'mode': 'trust', 'status': 'unverified', 'verified': False}
    assert receipt['evidence']['producer']['alignment'] == expected
    assert attempt['alignment'] == expected


def test_trust_refuses_a_native_attempt_symlink(comfy, tmp_path, monkeypatch):
    run = importlib.import_module('vibecomfy.runtime.run')
    monkeypatch.setenv('ASTRID_ALIGN', 'trust')
    original = run.run_sync
    outside = tmp_path / 'outside-attempt.json'
    outside.write_text('{"sentinel": true}')
    def substitute_attempt(*args, **kwargs):
        result = original(*args, **kwargs)
        native = Path(result.managed_generation_result_path).with_name('attempt.json')
        native.unlink()
        native.symlink_to(outside)
        return result
    monkeypatch.setattr(run, 'run_sync', substitute_attempt)
    with pytest.raises(production_engine.ProductionEngineError, match='native attempt escaped attempt custody'):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / 'attempt' / 'outputs', task_identity='task-trust',
            attempt_identity='attempt-trust', comfy_url=comfy.url, profile_id='local_server',
        )
    assert outside.read_text() == '{"sentinel": true}'


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel'])
def test_managed_preparation_retains_native_http_identity_on_all_outcomes(comfy, tmp_path, monkeypatch, outcome):
    prepared = {'placement': 'managed', 'alignment': {'mode': 'align', 'status': 'ready', 'verified': True},
                'manifest': {'models': []}, 'execution_identity': 'execution'}
    monkeypatch.setattr('astrid.core.generation.managed_preparation.read_preparation', lambda **kwargs: prepared)
    run = importlib.import_module('vibecomfy.runtime.run')
    original = run.run_sync
    observed = {}
    def invoke(*args, **kwargs):
        context = kwargs.get('run_context')
        assert context is not None, 'managed run needs an exact native context on failure too'
        observed['path'] = context.receipt_path
        if outcome == 'failure':
            raise ValueError('forced native failure')
        if outcome == 'cancel':
            raise SystemExit(143)
        return original(*args, **kwargs)
    monkeypatch.setattr(run, 'run_sync', invoke)
    def execute():
        return production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / 'attempt' / 'outputs', task_identity='task-managed',
            attempt_identity='attempt-managed', comfy_url=comfy.url, profile_id='local_server')
    if outcome == 'success':
        observed['result'] = execute()
    else:
        with pytest.raises(ValueError if outcome == 'failure' else SystemExit): execute()
    attempt = json.loads(observed['path'].read_text())
    assert attempt['placement'] == 'managed'
    assert attempt['alignment'] == prepared['alignment']
    assert attempt['managed_engine_preparation'] == prepared
    if outcome == 'success':
        assert attempt['adapter']['kind'] == 'external'
        payload = json.loads(Path(observed['result'].managed_generation_result_path).read_text())
        embedded = payload['evidence']['producer']['attempt']['record']
        assert embedded['managed_engine_preparation'] == prepared
        assert embedded['adapter']['kind'] == 'external'


def test_managed_preparation_refuses_native_attempt_symlink(comfy, tmp_path, monkeypatch):
    monkeypatch.setattr('astrid.core.generation.managed_preparation.read_preparation', lambda **kwargs:
        {'placement': 'managed', 'alignment': {'mode': 'align'}, 'manifest': {'models': []}})
    run = importlib.import_module('vibecomfy.runtime.run')
    original = run.run_sync
    outside = tmp_path / 'outside-managed-attempt.json'
    outside.write_text('{"sentinel": true}')
    def substitute(*args, **kwargs):
        result = original(*args, **kwargs)
        native = Path(result.managed_generation_result_path).with_name('attempt.json')
        native.unlink()
        native.symlink_to(outside)
        return result
    monkeypatch.setattr(run, 'run_sync', substitute)
    with pytest.raises(production_engine.ProductionEngineError, match='native attempt escaped attempt custody'):
        production_engine.run_workflow_result_path(
            comfy.workflow, tmp_path / 'attempt' / 'outputs', task_identity='task-managed',
            attempt_identity='attempt-managed', comfy_url=comfy.url, profile_id='local_server')
    assert outside.read_text() == '{"sentinel": true}'



@pytest.mark.parametrize('outcome', ['success', 'pre-post-failure', 'accepted-failure'])
def test_trust_absent_auxiliary_asset_is_unverified_in_every_astrid_record(comfy, tmp_path, monkeypatch, outcome):
    from vibecomfy.workflow_bundle import emit_bundle
    loaded = production_engine.load_workflow_path(comfy.workflow, tmp_path / 'author-trust', comfy_url=comfy.url)
    bundle = production_engine._canonical_bundle_value(loaded.resolved)
    bundle.workflow.metadata['model_assets'] = [{'name': 'absent-aux.bin', 'subdir': 'auxiliary',
        'url': 'https://example.invalid/absent-aux.bin', 'sha256': 'a' * 64, 'size_bytes': 123}]
    canonical = emit_bundle(bundle.workflow, tmp_path / 'trusted-aux/workflow.py', {'operation': 'authored'})
    monkeypatch.setenv('ASTRID_ALIGN', 'trust')
    destination = tmp_path / 'attempt-aux/outputs'
    observed = []
    def snapshot():
        documents = list(destination.rglob('*.json')) + list(destination.rglob('*.jsonl'))
        text = '\n'.join(path.read_text() for path in documents)
        assert 'verified_target_provisioning' not in text
        assert 'declared_unverified' in text and 'absent-aux.bin' in text
        observed.append(True)
    from astrid.core.generation.backends.vibecomfy_local_server import TargetRuntimeClient, AttachedArtifacts
    post = TargetRuntimeClient._post_prompt
    async def submit(self, *args, **kwargs):
        snapshot()
        if outcome == 'pre-post-failure': raise ValueError('forced failure before POST')
        return await post(self, *args, **kwargs)
    monkeypatch.setattr(TargetRuntimeClient, '_post_prompt', submit)
    if outcome == 'accepted-failure':
        async def retrieval(self, **kwargs):
            snapshot()
            raise ValueError('forced accepted retrieval failure')
        monkeypatch.setattr(AttachedArtifacts, 'download_artifacts', retrieval)
    def execute():
        return production_engine.run_workflow_result_path(
            canonical.python_path, destination, task_identity='task-aux',
            attempt_identity='attempt-aux', comfy_url=comfy.url, profile_id='local_server')
    if outcome == 'success': assert execute().outputs
    else:
        with pytest.raises(Exception): execute()
    assert observed
    snapshot()



@pytest.mark.parametrize('gap', ['missing-node', 'scalar-type'])
def test_explicit_trust_preserves_unrelated_target_schema_refusals(comfy, tmp_path, monkeypatch, gap):
    from vibecomfy.workflow_bundle import emit_bundle
    loaded = production_engine.load_workflow_path(comfy.workflow, tmp_path / 'author-schema', comfy_url=comfy.url)
    bundle = production_engine._canonical_bundle_value(loaded.resolved)
    canonical = emit_bundle(bundle.workflow, tmp_path / 'trust-schema/workflow.py', {'operation': 'authored'})
    if gap == 'missing-node': del comfy.schema['SaveImage']
    else: comfy.schema['SeedImage']['input']['required']['seed'] = ['STRING']
    monkeypatch.setenv('ASTRID_ALIGN', 'trust')
    with pytest.raises(production_engine.ProductionEngineError):
        production_engine.run_workflow_result_path(
            canonical.python_path, tmp_path / 'trust-schema-attempt/outputs', task_identity='schema-task',
            attempt_identity='schema-attempt', comfy_url=comfy.url, profile_id='local_server')
    assert not comfy.prompts
