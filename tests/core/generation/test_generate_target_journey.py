"""A public generate command crosses real admission, HTTP custody and settlement.

Only ComfyUI is fake: the image fixture publishes a tiny PNG over HTTP;
the H3 fixture uses the existing managed owner-session and synthetic AV server.
The catalog, compiler, Runtime, host, receipt and publication are production code.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

import pytest
from PIL import Image

from astrid import console
from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation import cli
from astrid.sdk.client import AstridClient

pytestmark = pytest.mark.integration  # real Runtime and the pinned VibeComfy checkout

PACK = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "vibecomfy"
H3_PACK = PACK.parent / "h3_av"
# The reviewed H3 fixture pairing, distinct from the generic image engine pin.
H3_VIBECOMFY_REVISION = "4acc1525236294e3542890a4a3000a399841ba55"






def _target_schema(*entry_ids):
    """Expose the shipped default image graphs with exact installed role names.

    The default image entry owns its failure (no fallback to another model), so
    the target must serve the preferred default's assets as well as z-image.
    """
    from astrid.core.model_catalog.registry import ModelRegistry
    from vibecomfy.porting.object_info import get_class
    from vibecomfy.registry.ready import workflow_from_ready

    schema = {}
    for entry_id in entry_ids or ("z-image", "qwen-image-2.1-turbo"):
        spec = ModelRegistry.load_default().get(entry_id).modes["t2i"].backends["local"]
        workflow = workflow_from_ready(spec.template)
        for node in workflow.runtime_nodes().values():
            definition = get_class(node.class_type)
            assert definition is not None
            schema.setdefault(node.class_type, {
                "input": copy.deepcopy(definition["inputs"]),
                "input_order": copy.deepcopy(definition["input_order"]),
                "output": [output["type"] for output in definition["outputs"]],
                "output_name": [output["name"] for output in definition["outputs"]],
                "output_is_list": [output["is_list"] for output in definition["outputs"]],
                "output_node": node.class_type == "SaveImage",
            })
        for role in spec.model_roles.values():
            choices = schema[role.node_class]["input"]["required"].get(role.input)
            names = list(choices[0]) if choices and isinstance(choices[0], list) else []
            schema[role.node_class]["input"]["required"][role.input] = [list(dict.fromkeys([*names, role.names[0]]))]
    return schema


@pytest.fixture
def loopback_comfy(tmp_path):
    state = SimpleNamespace(schema=_target_schema(), calls=[], prompts=[], histories={})
    image = io.BytesIO()
    Image.new("RGB", (16, 16), (24, 48, 96)).save(image, format="PNG")
    state.image = image.getvalue()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, data, media_type="application/json"):
            if media_type == "application/json":
                data = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", media_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            state.calls.append(("GET", self.path, None))
            if self.path == "/object_info":
                self.send(state.schema)
            elif self.path == "/system_stats":
                self.send({"system": {"comfyui_version": "0.36.0"}, "devices": []})
            elif self.path.startswith("/history/"):
                prompt_id = self.path.rsplit("/", 1)[1]
                self.send({prompt_id: state.histories[prompt_id]})
            elif self.path == "/queue":
                self.send({"queue_running": [], "queue_pending": []})
            elif self.path.startswith("/view?"):
                descriptor = parse_qs(urlsplit(self.path).query)
                assert descriptor["filename"] == [state.filename]
                assert descriptor["type"] == ["output"]
                self.send(state.image, "image/png")
            else:
                self.send_error(404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state.calls.append(("POST", self.path, body))
            # Repeated deletion of this fixture's accepted prompt is task
            # containment, not ownership of the attached server lifecycle.
            if self.path == "/queue" and any(
                body == {"delete": [prompt_id]} for prompt_id in state.histories
            ):
                self.send({})
                return
            if self.path != "/prompt":
                self.send_error(400, "attached fixture rejects lifecycle or unowned controls")
                return
            state.prompts.append(body["prompt"])
            prompt_id = f"g24-{len(state.prompts)}"
            save_id, save = next((key, node) for key, node in body["prompt"].items()
                                 if node["class_type"] == "SaveImage")
            state.filename = save["inputs"]["filename_prefix"].rsplit("/", 1)[-1] + "_00001_.png"
            state.histories[prompt_id] = {
                "outputs": {save_id: {"images": [{"filename": state.filename, "subfolder": "", "type": "output"}]}},
                "status": {"completed": True, "status_str": "success", "messages": []},
            }
            self.send({"prompt_id": prompt_id, "number": len(state.prompts), "node_errors": {}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        (tmp_path / "fake-comfy-http.json").write_text(json.dumps({
            "url": state.url, "calls": state.calls, "prompts": state.prompts,
            "object_info": state.schema,
            "output_sha256": hashlib.sha256(state.image).hexdigest(),
            "output_bytes": len(state.image),
        }, indent=2))


@pytest.mark.parametrize('entry_kind', ['catalog', 'saved'])
def test_public_image_command_registers_view_bytes_and_canonical_receipt(
    loopback_comfy, tmp_path, monkeypatch, capsys, real_runtime, entry_kind,
):
    comfy = loopback_comfy
    entry_id = 'image/z_image_turbo'
    if entry_kind == 'saved':
        from vibecomfy.entries import import_as_entry
        from vibecomfy.registry.ready import workflow_from_ready
        from vibecomfy.workflow_bundle import load_bundle
        source = tmp_path / 'ready-image.json'
        source.write_text(json.dumps(load_bundle(workflow_from_ready(entry_id)).materialize_ui()))
        monkeypatch.setenv('VIBECOMFY_ENTRIES_DIR', str(tmp_path / 'entries'))
        saved = import_as_entry(source, 'ready-image')
        assert saved['kind'] == 'image' and 'prompt' in saved['binds']
        entry_id = saved['id']
    daemon = real_runtime()
    client = AstridClient.open(
        endpoint=daemon.endpoint, credential=daemon.credential_path,
        realm_id=daemon.service.realm["id"], actor_id="owner",
        client_name="g24-public-cli-journey", client_version="test",
        protocol_version="workspace.v1",
    )
    project = client.projects.create(slug="g24", name="G24", idempotency_key="g24-project")
    assert project.ok
    import vibecomfy
    from vibecomfy.runtime.session import current_source_content_digest, current_source_revision
    from astrid.core.generation import targets

    # Use the existing explicit candidate environment. The production owner
    # verifies its revision and clean content; no dependency or custody
    # function is replaced, and this does not promote the production pin.
    dependency_root = str(Path(vibecomfy.__file__).resolve().parents[1])
    monkeypatch.setenv('ASTRID_VIBECOMFY_CHECKOUT', dependency_root)
    monkeypatch.setenv('ASTRID_VIBECOMFY_CANDIDATE_KIND', 'local_snapshot')
    monkeypatch.setenv('ASTRID_VIBECOMFY_CANDIDATE_REVISION', current_source_revision())
    monkeypatch.setenv('ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST', current_source_content_digest())
    targets.save_target(targets.TargetRecord(endpoint=comfy.url))
    attempt = tmp_path / "generate-attempt"
    host = GenericPackHost(
        pack_roots=[PACK], attempt_root=attempt,
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
    )
    host.discover()
    host.register()
    # Use the real connection and limit only the supported observer controls;
    # a broken claim must fail this CPU journey within a minute.
    monkeypatch.setattr(cli, "_open_client", lambda: nullcontext(client))
    invoke_result = client.invoke_result

    def bounded_invoke(*args, **kwargs):
        kwargs["timeout_seconds"] = 45.0
        kwargs["poll_seconds"] = 0.02
        return invoke_result(*args, **kwargs)

    monkeypatch.setattr(client, "invoke_result", bounded_invoke)
    try:
        with ThreadPoolExecutor(max_workers=1) as worker:
            settled = worker.submit(host.run, poll_seconds=0.02, max_tasks=1)
            try:
                code = console.main([
                    "generate", "image", "A tiny blue lighthouse.",
                    "--entry", entry_id, "--placement", "auto",
                    "--project", project.data["project_id"], "--out", str(tmp_path / 'delivered'), "--json",
                ])
            finally:
                host.shutdown()
            results = settled.result(timeout=15)
        captured = capsys.readouterr()
        (tmp_path / "generate-cli.stdout.txt").write_text(captured.out)
        (tmp_path / "generate-cli.stderr.txt").write_text(captured.err)
        tasks = client.tasks.list(project.data["project_id"])
        assert tasks.ok
        (tmp_path / "generate-task-state.json").write_text(json.dumps(tasks.data, indent=2))
        assert code == 0, captured.out + captured.err
        assert len(results) == 1 and results[0].state == "succeeded"
        delivered = json.loads(captured.out)
        assert delivered['image_paths']
        assert all(Path(path).is_relative_to(tmp_path / 'delivered') for path in delivered['image_paths'])
        assert any(Path(path).read_bytes() == comfy.image for path in delivered['image_paths'])

        # This is the frozen GenericPackHost receipt, not a CLI-created
        # completion file. CAS publication and its hash are independently read.
        receipt = json.loads((attempt / "result.json").read_text())
        assert receipt["status"] == "succeeded" and receipt["ok"] is True
        assert receipt["capability_id"] == "vibecomfy.run"
        assert receipt["publication"]["status"] == "settled"
        task = client.tasks.show(receipt["task_id"])
        assert task.ok and task.data["state"] == "succeeded"
        digest = "sha256:" + hashlib.sha256(comfy.image).hexdigest()
        output = next(row for row in task.data["result"]["outputs"] if row["digest"] == digest)
        assert output["name"] == "vibecomfy_run"
        assert client.media.read_bytes(digest) == comfy.image
        assert any(row["digest"] == digest for row in receipt["outputs"]["artifacts"])
        assert len(comfy.prompts) == 1
        by_class = {node["class_type"]: node["inputs"] for node in comfy.prompts[0].values()}
        assert by_class["UNETLoader"]["unet_name"] == "z_image_turbo_bf16.safetensors"
        assert by_class["CLIPLoader"]["clip_name"] == "qwen_3_4b.safetensors"
        assert by_class["VAELoader"]["vae_name"] in {"ae.safetensors", "FLUX1\\ae.safetensors"}
        assert any(
            node["class_type"] == "CLIPTextEncode"
            and node["inputs"].get("text") == "A tiny blue lighthouse."
            for node in comfy.prompts[0].values()
        )
        assert any(path.startswith("/view?") for _, path, _ in comfy.calls)
        posts = [(path, body) for method, path, body in comfy.calls if method == "POST"]
        assert [path for path, _ in posts if path != "/queue"] == ["/prompt"]
        cleanup = [body for path, body in posts if path == "/queue"]
        assert cleanup and all(body == {"delete": ["g24-1"]} for body in cleanup)
    finally:
        host.shutdown()


@pytest.mark.parametrize("path, payload", [
    ("/queue", {"clear": True}),
    ("/queue", {"delete": ["unrelated"]}),
    ("/queue", {"delete": ["g24-1", "unrelated"]}),
    ("/interrupt", {}),
    ("/api/free", {}),
])
def test_attached_fixture_repeats_owned_cleanup_and_rejects_unowned_controls(
    loopback_comfy, tmp_path, path, payload,
):
    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
    from astrid.core.generation.backends.vibecomfy_local_server import cleanup_prompts

    comfy = loopback_comfy
    client = ComfyTargetClient(ComfyTarget(comfy.url, "attached"))
    acceptance = client.submit_prompt({"prompt": {
        "1": {"class_type": "SaveImage", "inputs": {"filename_prefix": "cleanup"}},
    }})
    prompt_id = acceptance["prompt_id"]
    assert comfy.histories[prompt_id]["status"]["completed"] is True
    journal = tmp_path / "owned-attempt" / ".vibecomfy-runtime" / "out" / "runs" / "run" / "attempt.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({"queue_acceptance": {"status": "accepted", "prompt_id": prompt_id}}))
    destination = tmp_path / "owned-attempt"
    cleanup_prompts(comfy.url, destination)
    cleanup_prompts(comfy.url, destination)
    assert [(method, route, body) for method, route, body in comfy.calls if route == "/queue"] == [
        ("POST", "/queue", {"delete": [prompt_id]}), ("GET", "/queue", None),
        ("POST", "/queue", {"delete": [prompt_id]}), ("GET", "/queue", None),
    ]
    # Probe the fixture directly: the production client also rejects these
    # controls before HTTP. Neither layer grants attached-server ownership.
    request = Request(comfy.url + path, data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(HTTPError) as error:
        urlopen(request, timeout=5)
    assert error.value.code == 400
    error.value.close()
    assert len(comfy.prompts) == 1


@pytest.mark.parametrize("source", ["global", "repo", "env", "request"])
def test_f02_sdk_bound_target_failure_is_read_only(source, tmp_path, monkeypatch):
    """The public facade keeps the selected human target, including dry-run."""
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation import preflight, targets
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    from astrid.sdk.generation import GenerationFacade

    targets.save_target(targets.TargetRecord(endpoint="http://localhost:8199",
        root=str(tmp_path), python=sys.executable, facts={"launch_argv": ["python", "main.py"]}))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vibecomfy.toml").write_text('[comfyui]\nurl = "http://localhost:8200"\n')
    if source == "env":
        monkeypatch.setenv("VIBECOMFY_COMFYUI_URL", "http://localhost:8201")
    observed = []

    def offline(client, path, **kwargs):
        observed.append((client.target.url, path))
        raise OSError("selected server is offline")

    def forbidden(*args, **kwargs):
        pytest.fail("bound human target must fail before managed readiness or admission")

    monkeypatch.setattr(ComfyTargetClient, "request_json", offline)
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", forbidden)
    monkeypatch.setattr(GenerationFacade, "workflow", forbidden)
    config = targets.config_path()
    original, modified = config.read_bytes(), config.stat().st_mtime_ns
    output = tmp_path / "absent-output"
    with pytest.raises(AstridError) as error:
        GenerationFacade().image(placement="http://localhost:8202" if source == "request" else "auto",
            project_root=repo if source == "repo" else None, prompt="one boat", out=output, dry_run=True)
    assert error.value.code == "target_unreachable"
    port = {"global": 8199, "repo": 8200, "env": 8201, "request": 8202}[source]
    assert observed == [(f"http://127.0.0.1:{port}", "/system_stats")]
    assert config.read_bytes() == original and config.stat().st_mtime_ns == modified
    assert not output.exists()


def test_f02_cli_dry_run_preserves_unreachable_saved_human_target(tmp_path, monkeypatch, capsys):
    from astrid.core.generation import preflight, targets
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient

    targets.save_target(targets.TargetRecord(endpoint="http://localhost:8199"))
    config = targets.config_path()
    original, modified = config.read_bytes(), config.stat().st_mtime_ns

    def offline(*args, **kwargs):
        raise OSError("selected server is offline")

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must fail before Runtime connection or managed fallback")

    monkeypatch.setattr(ComfyTargetClient, "request_json", offline)
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", forbidden)
    monkeypatch.setattr(cli, "_open_client", forbidden)
    output = tmp_path / "absent-output"
    assert console.main(["generate", "image", "one boat", "--dry-run", "--out", str(output)]) == 2
    captured = capsys.readouterr()
    assert "target_unreachable" in captured.err and ":8199" in captured.err
    assert not captured.out and not output.exists()
    assert config.read_bytes() == original and config.stat().st_mtime_ns == modified


@pytest.mark.parametrize("source", ["global", "env", "request"])
def test_f02_successful_dry_run_keeps_saved_identity_and_does_not_admit(
    source, loopback_comfy, tmp_path, monkeypatch,
):
    from astrid.core.generation import preflight, targets
    from astrid.sdk.generation import GenerationFacade

    targets.save_target(targets.TargetRecord(
        endpoint=loopback_comfy.url if source == "global" else "http://localhost:8199",
        root=str(tmp_path), python=sys.executable, facts={"launch_argv": ["python", "main.py"]}))
    config = targets.config_path()
    original, modified = config.read_bytes(), config.stat().st_mtime_ns
    targets.config_lock_path().unlink()
    if source == "env":
        monkeypatch.setenv("VIBECOMFY_COMFYUI_URL", loopback_comfy.url)

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run on a human target must not admit a task or use managed readiness")

    monkeypatch.setattr(preflight, "_comfyui_runtime_available", forbidden)
    monkeypatch.setattr(GenerationFacade, "workflow", forbidden)
    output = tmp_path / "absent-output"
    plan = GenerationFacade().image(
        prompt="one boat", placement=loopback_comfy.url if source == "request" else "auto",
        out=output, dry_run=True)
    assert plan["target"]["endpoint"] == loopback_comfy.url
    assert config.read_bytes() == original and config.stat().st_mtime_ns == modified
    assert not targets.config_lock_path().exists() and not output.exists()
    assert not loopback_comfy.prompts
