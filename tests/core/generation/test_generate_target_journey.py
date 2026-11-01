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


@contextmanager
def _managed_h3_comfy(root, runtime_client, monkeypatch):
    """Use the existing owner-session fixture, without its cloning harness."""
    import vibecomfy
    from vibecomfy.runtime.session import current_source_content_digest, current_source_revision
    from tests.helpers.h3_cpu_runtime import (
        ManagedSchemaSession,
        _synthetic_model_root_binding, _vibecomfy_host_readiness_environment,
    )
    from astrid.core.generation.backends.vibecomfy import _canonical_sha256

    checkout = Path(vibecomfy.__file__).resolve().parents[1]
    identity = {
        "revision": current_source_revision(), "content_digest": current_source_content_digest(),
    }
    assert identity["revision"] == H3_VIBECOMFY_REVISION
    clean = subprocess.run([
        "git", "-C", str(checkout), "status", "--porcelain", "--untracked-files=all",
    ], check=True, capture_output=True, text=True, timeout=10)
    assert not clean.stdout, "the managed fixture requires the reviewed clean VibeComfy checkout"
    health = runtime_client.health()
    instance_id = health["runtime_instance_id"] if isinstance(health, dict) else health.runtime_instance_id
    models = root / "synthetic-models"
    model_paths = (
        "vae/minimax_h3_audio_vae_fp32.safetensors",
        "vae/minimax_h3_video_vae_int8_convrot.safetensors",
        "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
        "loras/h3_character_swap_pro4500_1000.safetensors",
        "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
        "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
    )
    for relative in model_paths:
        path = models / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"synthetic-comfy-fixture:{relative}\n".encode())
    model_root = _synthetic_model_root_binding(models, model_paths)
    session = ManagedSchemaSession(
        root / "managed-comfy", identity["revision"], identity["content_digest"],
        vibecomfy_checkout=checkout,
        model_paths=model_paths,
    )
    try:
        # These are the established CPU fixture's synthetic readiness facts,
        # not machine qualification or real model/inference evidence.
        facts = {
            "exact": {
                "interpreter": str(sys.executable),
                "runtime_lock": "sha256:" + "1" * 64,
                "engine_lock": "sha256:" + "2" * 64,
                "model_digest": model_root["inventory_digest"],
                "custom_node_digest": "sha256:" + "4" * 64,
                "driver": "cpu-fixture", "root": "sha256:" + "5" * 64,
                "port": int(session.server_url.rsplit(":", 1)[1]),
            },
            "minimum": {"vram_bytes": 1, "scratch_bytes": 1},
        }
        profile = {
            "schema_version": "hc03-worker-readiness.v1", "status": "ready",
            "verified_facts": facts, "verified_facts_digest": _canonical_sha256(facts),
            "runtime": {"runtime_instance_id": instance_id, "comfyui_version": "==0.36.0"},
            "launch": {"output_root": str(session.output_root), "model_root": model_root},
            "vibecomfy_candidate": {
                "kind": "local_snapshot", "checkout": str(checkout),
                "revision": identity["revision"], "source_content_digest": identity["content_digest"],
                "source_worktree_dirty": False, "installation_mode": "explicit_checkout",
            },
            "vibecomfy_session": session.session_binding,
        }
        profile_path = root / "h3-readiness.json"
        profile_path.write_text(json.dumps(profile, sort_keys=True), encoding="utf-8")
        profile_hash = "sha256:" + hashlib.sha256(profile_path.read_bytes()).hexdigest()
        (root / "h3-session-evidence.json").write_text(json.dumps({
            "synthetic_inference": True, "runtime_instance_id": instance_id,
            "source_revision": identity["revision"], "source_content_digest": identity["content_digest"],
            "profile_hash": profile_hash, "config_digest": session.session_binding["config_digest"],
            "session_dir": str(session.session_dir), "server_url": session.server_url,
            "model_inventory_digest": model_root["inventory_digest"],
        }, indent=2), encoding="utf-8")
        with monkeypatch.context() as environment:
            for name in ("PATH", "ASTRID_C11_SCHEMA_PATH", "ASTRID_C11_EXPECTED_PATH",
                         "ASTRID_C11_EVIDENCE_PATH", "ASTRID_C11_SESSION_DIR"):
                environment.setenv(name, session.child_environment[name])
            environment.setenv("ASTRID_VIBECOMFY_CANDIDATE_KIND", "local_snapshot")
            environment.setenv("ASTRID_VIBECOMFY_CANDIDATE_REVISION", identity["revision"])
            environment.setenv("ASTRID_VIBECOMFY_CANDIDATE_CONTENT_DIGEST", identity["content_digest"])
            for name, value in _vibecomfy_host_readiness_environment(
                checkout, profile_path, profile_hash, identity["revision"], identity["content_digest"],
            ).items():
                environment.setenv(name, value)
            yield session
    finally:
        session.close()


def test_public_h3_local_inputs_reach_tracked_runtime_parent(
    tmp_path, monkeypatch, capsys, real_runtime,
):
    """Public producer files reach one authenticated parent and named publications."""
    from astrid import sdk
    from astrid.sdk.pagination import paged_rows
    from tests.helpers.h3_verified_parent import VerifiedH3Host

    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=1024x576:r=24:d=1",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-crf", "51", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "8k",
        "-shortest", str(source),
    ], check=True, capture_output=True, timeout=60)
    source_digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request = {
        "version": 1, "operation": "continue",
        "source": {"asset": "source", "range": [0, 1]}, "output": {"duration": 2},
        "content": {"prompt": "continue"},
        "changes": {"video": [{"during": [1, 2], "area": {"full_frame": True}, "action": "generate"}],
                    "audio": [{"during": [1, 2], "action": "generate"}]},
        "references": [], "overrides": {},
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    asset_map = tmp_path / "assets.json"
    asset_map.write_text(json.dumps({"source": str(source)}), encoding="utf-8")
    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"request": str(request_path), "asset_map": str(asset_map)}), encoding="utf-8")

    daemon = real_runtime(support_root=tmp_path / "data" / "runtime")
    client = AstridClient.open(
        endpoint=daemon.endpoint, credential=daemon.credential_path,
        realm_id=daemon.service.realm["id"], actor_id="owner",
        client_name="h3-public-cli-journey", client_version="test",
        protocol_version="workspace.v1",
    )
    project = client.projects.create(slug="h3-public", name="H3 public", idempotency_key="h3-public-project")
    assert project.ok
    project_id = project.data["project_id"]
    worker_client = RuntimeProtocolClient(daemon.endpoint, daemon.worker_token)
    host = GenericPackHost(
        pack_roots=[H3_PACK, PACK], attempt_base=tmp_path / "attempts", max_concurrency=2,
        client=worker_client,
    )
    verified_host = VerifiedH3Host(tmp_path, daemon, [H3_PACK, PACK])
    claim_child = verified_host._claim

    def observe_compiled_source(*args, **kwargs):
        result = claim_child(*args, **kwargs)
        spec = result["spec"]
        if spec.get("delegated_stage") == "run":
            (binding,) = [row for row in spec["delegated_inputs"] if row["name"] == "managed_assets"]
            # This observer runs inside the Runtime claim handler; avoid a
            # reentrant HTTP call while that handler owns its request scope.
            archive_bytes = daemon.service.cas.path_for(binding["object_id"].removeprefix("sha256:")).read_bytes()
            assert "sha256:" + hashlib.sha256(archive_bytes).hexdigest() == binding["object_id"]
            with zipfile.ZipFile(io.BytesIO(archive_bytes)) as bundle:
                (asset,) = json.loads(bundle.read("manifest.json"))["assets"]
                assert asset["binding"] == "source_video"
                data = bundle.read(asset["member"])
            assert "sha256:" + asset["sha256"] == source_digest
            assert hashlib.sha256(data).hexdigest() == asset["sha256"]
            assert len(data) == asset["size"] == source.stat().st_size
            expected = json.loads(comfy.expected_path.read_text())
            expected["basename"] = Path(asset["member"]).name
            outputs = daemon.service.managed_output_page(binding["producer_task_id"])["items"]
            (compiled,) = [row for row in outputs if row["output_port"] == "compilation"
                           and row["attempt_id"] == binding["producer_attempt_id"]]
            compilation_bytes = daemon.service.cas.path_for(compiled["object_id"].removeprefix("sha256:")).read_bytes()
            assert "sha256:" + hashlib.sha256(compilation_bytes).hexdigest() == compiled["object_id"]
            compilation = json.loads(compilation_bytes)
            assert "sha256:" + compilation["managed_assets"]["sha256"] == binding["object_id"]
            timing = compilation["continuation_timing"]
            expected["generated_seconds"] = timing["expected_graph_output_frames"] / timing["fps"]
            comfy.expected_path.write_text(json.dumps(expected), encoding="utf-8")
            (tmp_path / "h3-compiled-source-binding.json").write_text(json.dumps({
                "task_id": result["task_id"], "managed_assets": binding, "source": asset,
                "generated_seconds": expected["generated_seconds"],
                "compilation": compiled["object_id"], "continuation_timing": timing,
            }, indent=2), encoding="utf-8")
        return result

    monkeypatch.setattr(verified_host, "_claim", observe_compiled_source)
    monkeypatch.setattr(sdk.AstridClient, "open_from_launcher", lambda **_kwargs: nullcontext(client))
    invoke_result = client.invoke_result

    def bounded_invoke(*args, **kwargs):
        # Leave time for all seven real child processes on a CPU host while
        # keeping this observer below the suite's 600-second hang guard.
        kwargs.update(timeout_seconds=300.0, poll_seconds=0.02)
        return invoke_result(*args, **kwargs)

    monkeypatch.setattr(client, "invoke_result", bounded_invoke)
    imports = {}
    import_bytes = client.media.import_bytes

    def observe_import(**kwargs):
        data = kwargs["data"]
        result = import_bytes(**kwargs)
        assert result.ok
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        assert (result.data.get("digest") or result.data.get("object_id")) == digest
        assert client.media.read_bytes(digest) == data
        imports[kwargs["filename"]] = {"digest": digest, "size": len(data)}
        (tmp_path / "h3-imports.json").write_text(json.dumps(imports, indent=2), encoding="utf-8")
        return result

    monkeypatch.setattr(client.media, "import_bytes", observe_import)
    create = client.tasks.create
    admitted = {}

    def observe_admission(**kwargs):
        if kwargs["capability"] == "h3_av.transform":
            kwargs["execution_request"] = {
                **kwargs.get("execution_request", {"schema_version": 1}),
                "target": verified_host.target,
            }
            assert not admitted, "the public command must create exactly one parent"
            assert {"request.json", "h3-inputs.zip"} <= set(imports)
            expected_request = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            assert client.media.read_bytes(imports["request.json"]["digest"]) == expected_request
            assert source_digest in {row["digest"] for row in imports.values()}
            assert client.media.read_bytes(source_digest) == source.read_bytes()
            with zipfile.ZipFile(io.BytesIO(client.media.read_bytes(imports["h3-inputs.zip"]["digest"]))) as bundle:
                manifest = json.loads(bundle.read("manifest.json"))
                (asset,) = manifest["assets"]
                assert asset["binding"] == "source"
                assert asset["sha256"] == source_digest.removeprefix("sha256:")
                assert bundle.read(asset["member"]) == source.read_bytes()
                comfy.expected_path.write_text(json.dumps({
                    "basename": Path(asset["member"]).name, "sha256": asset["sha256"],
                    "size": asset["size"], "generated_seconds": 1,
                }), encoding="utf-8")
                assert not (comfy.input_root / Path(asset["member"]).name).exists()
            spec = kwargs["spec"]
            assert str(request_path) not in json.dumps(spec)
            assert str(asset_map) not in json.dumps(spec)
            assert str(source) not in json.dumps(spec)
            for row in imports.values():
                assert row["digest"] in json.dumps(spec)
        result = create(**kwargs)
        if kwargs["capability"] == "h3_av.transform":
            admitted.update({"request": kwargs, "result": result.data, "ok": result.ok})
            (tmp_path / "h3-admission.json").write_text(json.dumps(admitted, indent=2, default=str), encoding="utf-8")
            assert result.ok
            verified_host.activate(result.data["task_id"])
        return result

    monkeypatch.setattr(client.tasks, "create", observe_admission)
    captured = None
    try:
        with _managed_h3_comfy(tmp_path, worker_client, monkeypatch) as comfy:
            host.discover()
            host.register()
            code = console.main([
                "generate", "video", "--entry", "h3", "--inputs-json", str(inputs),
                "--project", project_id, "--json",
            ])
            results = verified_host.receipts()
            captured = capsys.readouterr()
            cli_diagnostic = (
                f"public CLI return code: {code}\n"
                f"stdout:\n{captured.out}\n"
                f"stderr:\n{captured.err}"
            )
            assert code == 0, cli_diagnostic
            assert admitted and admitted["ok"], (
                f"{cli_diagnostic}\nadmission record: {admitted!r}"
            )
            assert results and all(result["status"] == "succeeded" for result in results)
            payload = json.loads(captured.out)
            assert payload["ok"] is True
            task_id = payload["kernel_task_id"]
            tasks = paged_rows(client.tasks.list, project_id)
            assert tasks is not None
            parents = [row for row in tasks if row["capability_id"] == "h3_av.transform"]
            assert len(parents) == 1 and parents[0]["task_id"] == task_id
            assert parents[0]["run_id"] == payload["kernel_run_id"]
            assert parents[0]["spec"].get("delegated_parent") is None
            parent_binding = parents[0]["execution_binding"]
            assert parent_binding["actual_target"] == verified_host.target
            assert parent_binding["verification"] == {
                "method": "credential_claim", "verified": True,
                "evidence_digest": verified_host.qualification["evidence_digest"],
            }
            assert parent_binding["executor_incarnation"] == verified_host.qualification["executor_incarnation"]
            children = [row for row in tasks if row["spec"].get("delegated_parent", {}).get("parent_task_id") == task_id]
            stages = {row["spec"]["delegated_stage"]: row for row in children}
            assert len(children) == len(stages) == 7
            assert set(stages) == {"prepare", "compile", "validate", "run", "compose", "verify", "finalizer"}
            assert all(row["state"] == "succeeded" for row in [*parents, *children])
            assert all(row["spec"]["delegated_parent"]["parent_attempt_id"] == parents[0]["attempt_id"] for row in children)
            assert all(row["spec"]["delegated_parent"]["parent_placement"] == {
                "actual": verified_host.target, "verification": parent_binding["verification"],
                "executor_incarnation": parent_binding["executor_incarnation"],
            } for row in children)
            assert all(all(row["execution_binding"][key] == parent_binding[key] for key in (
                "actual_target", "verification", "executor_incarnation", "executor_id",
            )) for row in children)
            managed = {}
            for row in [*parents, *children]:
                listing = client.tasks.list_managed_outputs(row["task_id"])
                assert listing.ok
                rows, cursor = listing.data
                assert cursor is None and rows
                managed[row["task_id"]] = rows
                for output in rows:
                    assert output["task_id"] == row["task_id"]
                    data = client.media.read_bytes(output["object_id"])
                    assert "sha256:" + hashlib.sha256(data).hexdigest() == output["object_id"]
            (tmp_path / "h3-managed-outputs.json").write_text(json.dumps(managed, indent=2), encoding="utf-8")
            task = client.tasks.show(task_id)
            assert task.ok and task.data["state"] == "succeeded"
            assert task.data["result"]["status"] == "candidate_verified"
            parent_outputs = {row["output_port"]: row for row in managed[task_id]}
            candidate = parent_outputs["verified_candidate"]
            receipt = json.loads(client.media.read_bytes(parent_outputs["final_receipt"]["object_id"]))
            assert receipt["overall_status"] == "candidate_verified"
            for state in ("raw_managed_publication", "candidate_verified", "final_composition_publication"):
                assert receipt["states"][state]["status"] == "passed"
            for stage, port in (("run", "vibecomfy_run"), ("finalizer", "verified_candidate")):
                child = stages[stage]
                settled_task = client.tasks.show(child["task_id"])
                assert settled_task.ok
                publication = settled_task.data["result"]["generation_publish_v1"]
                assert publication["effect_type"] == "generation.publish_v1"
                (group,) = publication["publications"]
                assert group["missing_selectors"] == []
                (variant,) = group["variants"]
                output = next(row for row in managed[child["task_id"]] if row["output_port"] == port)
                assert variant["object_id"] == output["object_id"]
                binding = output["provenance"]["delegated_publication"]
                assert binding["parent_task_id"] == task_id
                if stage == "finalizer":
                    assert candidate["object_id"] == output["object_id"]
                    parent_binding = candidate["provenance"]["delegated_publication"]
                    assert parent_binding["parent_task_id"] == task_id
                    assert parent_binding["source_task_id"] == child["task_id"]
                    assert parent_binding["source_association_id"] == output["association_id"]
            clip = tmp_path / "published.mp4"
            clip.write_bytes(client.media.read_bytes(candidate["object_id"]))
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
                "-of", "json", str(clip),
            ], check=True, capture_output=True, text=True, timeout=30)
            media = json.loads(probe.stdout)
            assert sorted(row["codec_type"] for row in media["streams"]) == ["audio", "video"]
            assert float(media["format"]["duration"]) == pytest.approx(2, abs=0.1)
            evidence = json.loads(comfy.queue_evidence_path.read_text())
            assert evidence["sha256"] == source_digest.removeprefix("sha256:")
            assert evidence["size"] == source.stat().st_size
    finally:
        verified_host.close()
        host.shutdown()
        final_capture = capsys.readouterr()
        if captured is None:
            captured = final_capture
        else:
            captured = SimpleNamespace(out=captured.out + final_capture.out, err=captured.err + final_capture.err)
        (tmp_path / "h3-cli.stdout.txt").write_text(captured.out, encoding="utf-8")
        (tmp_path / "h3-cli.stderr.txt").write_text(captured.err, encoding="utf-8")
        tasks = client.tasks.list(project_id)
        if tasks.ok:
            (tmp_path / "h3-task-state.json").write_text(json.dumps(tasks.data, indent=2, default=str), encoding="utf-8")


def _target_schema():
    """Expose the shipped default image graph with exact installed role names."""
    from astrid.core.model_catalog.registry import ModelRegistry
    from vibecomfy.porting.object_info import get_class
    from vibecomfy.registry.ready import workflow_from_ready

    entry = ModelRegistry.load_default().get("z-image")
    spec = entry.modes["t2i"].backends["local"]
    workflow = workflow_from_ready(spec.template)
    schema = {}
    for node in workflow.runtime_nodes().values():
        definition = get_class(node.class_type)
        assert definition is not None
        schema[node.class_type] = {
            "input": copy.deepcopy(definition["inputs"]),
            "input_order": copy.deepcopy(definition["input_order"]),
            "output": [output["type"] for output in definition["outputs"]],
            "output_name": [output["name"] for output in definition["outputs"]],
            "output_is_list": [output["is_list"] for output in definition["outputs"]],
            "output_node": node.class_type == "SaveImage",
        }
    for role in spec.model_roles.values():
        schema[role.node_class]["input"]["required"][role.input] = [[role.names[0]]]
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


def test_public_image_command_registers_view_bytes_and_canonical_receipt(
    loopback_comfy, tmp_path, monkeypatch, capsys, real_runtime,
):
    comfy = loopback_comfy
    daemon = real_runtime()
    client = AstridClient.open(
        endpoint=daemon.endpoint, credential=daemon.credential_path,
        realm_id=daemon.service.realm["id"], actor_id="owner",
        client_name="g24-public-cli-journey", client_version="test",
        protocol_version="workspace.v1",
    )
    project = client.projects.create(slug="g24", name="G24", idempotency_key="g24-project")
    assert project.ok
    import astrid.core.execution.generic_host as host_module
    import vibecomfy

    # The host deliberately ignores ambient PYTHONPATH. Select the same
    # pinned dependency tree in its documented source-selection seam, so the
    # subprocess qualifies these sources rather than a separately installed
    # VibeComfy. No process-custody or execution guard is replaced.
    dependency_root = str(Path(vibecomfy.__file__).resolve().parents[1])
    from tests.helpers.runtime import runtime_checkout

    dependencies = tuple(dict.fromkeys((
        dependency_root, str(runtime_checkout()),
        *(root for root in sys.path if Path(root).name in {"site-packages", "dist-packages"}),
    )))
    monkeypatch.setattr(host_module, "_dependency_pythonpath", lambda: dependencies)
    assert host_module._dependency_pythonpath() == dependencies
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
                    "--target", comfy.url, "--project", project.data["project_id"], "--json",
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
        GenerationFacade().image(target="http://localhost:8202" if source == "request" else "auto",
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
        prompt="one boat", target=loopback_comfy.url if source == "request" else "auto",
        out=output, dry_run=True)
    assert plan["target"]["endpoint"] == loopback_comfy.url
    assert config.read_bytes() == original and config.stat().st_mtime_ns == modified
    assert not targets.config_lock_path().exists() and not output.exists()
    assert not loopback_comfy.prompts
