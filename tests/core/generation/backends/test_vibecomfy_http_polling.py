"""Canonical pinned runtime over real local HTTPS agent/HTTP engine sockets.

Only the physical claimed-host dial and native process ownership witness are
fixtures. Compilation, approval, queue/history, chunks and producer are real.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web
from PIL import Image

from astrid.core.generation.backends import vibecomfy as backend
from astrid.core.generation.engine_transport import EngineEndpointTransport
from astrid.packs.vibecomfy import production_engine
from runpod_lifecycle.pod_agent import OBSERVATION_SCHEMA
from tests.core.generation.backends.test_checkout_server import _hc03_profile
from tests.core.generation.test_engine_endpoint_transport import (
    PLATFORM, TOKEN, canonical_session, encoded, endpoint, local_tls, network,
)


@pytest.fixture(autouse=True)
def no_websockets(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP route attempted WebSocket construction/connect")
    for module_name, names in (
        ("websockets", ("connect",)),
        ("websockets.asyncio.client", ("connect", "ClientConnection")),
        ("websockets.sync.client", ("connect", "ClientConnection")),
        ("websockets.legacy.client", ("connect", "WebSocketClientProtocol")),
        ("aiohttp", ("ClientWebSocketResponse", "WebSocketResponse")),
        ("aiohttp.web", ("WebSocketResponse",)),
    ):
        module = importlib.import_module(module_name)
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr("aiohttp.ClientSession.ws_connect", forbidden)
    monkeypatch.setattr("vibecomfy.runtime.watchdog.Watchdog.__init__", forbidden)
    monkeypatch.setenv("VIBECOMFY_WATCHDOG", "1")


@pytest.fixture
def engine_configuration(tmp_path, request):
    paths = canonical_session(tmp_path)
    paths.input.mkdir(parents=True)
    paths.output.mkdir(parents=True)
    return {"input_files": [str(paths.input / "reference.png")] if getattr(request, "param", True) else [],
            "output_roots": [str(paths.output)]}


@pytest.fixture
def observation_configuration(tmp_path):
    return {"schema": OBSERVATION_SCHEMA, "roots": [], "files": [str(tmp_path / "ready.json")]}


@pytest.fixture
def route(tmp_path, monkeypatch, endpoint, network, local_tls):
    from vibecomfy.workflow import VibeWorkflow, WorkflowSource, VibeOutput
    from vibecomfy.workflow_bundle import emit_bundle

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VIBECOMFY_HEADLESS", "1")
    monkeypatch.setenv("VIBECOMFY_HISTORY_POLL_INTERVAL_SEC", "0.01")
    # Only native physical process ownership is synthetic; registry/source,
    # config/model/endpoint facts still pass their ordinary validation.
    monkeypatch.setattr(backend, "_verify_owned_vibe_session", lambda *_: None)
    root = tmp_path / "host"
    root.mkdir()
    profile = _hc03_profile(root)
    session = profile["vibecomfy_session"]
    session["server_url"] = "http://127.0.0.1:8188"
    directory = Path(session["session_dir"])
    (directory / "url").write_text(session["server_url"])
    marker = json.loads((directory / "launch.json").read_text())
    marker["url"] = session["server_url"]
    (directory / "launch.json").write_bytes(encoded(marker))
    canonical = canonical_session(tmp_path)
    configuration = canonical.config
    assert "input_directory" not in configuration
    (directory / "config.json").write_bytes(encoded(configuration))
    session["config_digest"] = "sha256:" + hashlib.sha256(encoded(configuration)).hexdigest()
    profile["verified_facts"]["exact"]["port"] = 8188
    profile["verified_facts_digest"] = backend._canonical_sha256(profile["verified_facts"])
    profile["endpoint_evidence"] = {"schema": "astrid.runpod.endpoint-evidence.v1", "engine": endpoint.engine,
                                    "platform": PLATFORM, "claim_operation_id": "original",
                                    "claim_attestation": asdict(endpoint.attestation)}
    source_image = canonical.input / "reference.png"
    Image.new("RGB", (16, 16), (20, 30, 40)).save(source_image)
    # Large enough for actual multiple JSON download chunks, with real PNG
    # bytes followed by legal uninterpreted trailing data for this fixture.
    output_bytes = source_image.read_bytes() + b"fixture-output" * 180000
    output_root = canonical.output
    facts = SimpleNamespace(profile=profile, prompts=[], prompt_bodies=[], polls=0, mode="success", delay=0,
                            output=output_bytes, source=source_image, factory=0, closed=0)
    objects = {
        "LoadImage": {"input": {"required": {"image": [["reference.png"]]}},
                      "output": ["IMAGE", "MASK"], "output_name": ["IMAGE", "MASK"], "output_node": False},
        "SaveImage": {"input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING"]}},
                      "output": [], "output_node": True},
    }
    network["objects"] = objects
    (tmp_path / "node_index.json").write_text(json.dumps([
        {"class_type": "LoadImage", "pack": "core", "inputs": {"image": {"type": "STRING", "required": True}},
         "outputs": [{"type": "IMAGE", "name": "IMAGE"}, {"type": "MASK", "name": "MASK"}]},
        {"class_type": "SaveImage", "pack": "core", "inputs": {"images": {"type": "IMAGE", "required": True},
         "filename_prefix": {"type": "STRING", "required": True}}, "outputs": []},
    ]))
    workflow = VibeWorkflow("http-only", WorkflowSource("http-only"))
    workflow.add_node("LoadImage", _id="1", uid="reference", image="")
    workflow.add_node("SaveImage", _id="2", uid="save", filename_prefix="http-only")
    workflow.nodes["1"].native_input_names = ["image"]
    workflow.nodes["1"].native_input_types = ["STRING"]
    workflow.nodes["1"].native_output_names = ["IMAGE", "MASK"]
    workflow.nodes["1"].native_output_types = ["IMAGE", "MASK"]
    workflow.nodes["2"].native_input_names = ["images", "filename_prefix"]
    workflow.nodes["2"].native_input_types = ["IMAGE", "STRING"]
    workflow.nodes["2"].native_output_names = []
    workflow.nodes["2"].native_output_types = []
    workflow.connect("1.0", "2.images")
    workflow.register_input("reference", "1", "image", "", type="STRING", required=True, media_semantics="image")
    workflow.outputs = [VibeOutput("2", "SaveImage", name="image", artifact_kind="image", mime_type="image/png")]
    workflow.metadata["comfy_configuration"] = {"runtime_root": str(tmp_path / "runtime"), "prompt_timeout_sec": 10,
                                                "execution_id": "execution-http-only"}
    workflow.metadata["execution_id"] = "execution-http-only"
    source = tmp_path / "workflow.py"
    (tmp_path / "source.json").write_text(json.dumps({"nodes": [
        {"id": 1, "type": "LoadImage", "inputs": [], "widgets_values": [""]},
        {"id": 2, "type": "SaveImage", "inputs": [{"name": "images", "link": 1}], "widgets_values": ["http-only"]}],
        "links": [[1, 1, 0, 2, 0, "IMAGE"]]}))
    emit_bundle(workflow, source, {"operation": "authored"})
    facts.workflow = source

    async def handler(request):
        if request.path == "/prompt":
            facts.prompt_bodies.append(await request.read())
            facts.prompts.append(await request.json())
            assert source_image.read_bytes() == facts.source.read_bytes()
            if facts.mode == "uncertain":
                request.transport.close()  # accepted, reply lost
                return web.Response()
            return web.json_response({"prompt_id": "prompt-http-only", "number": 1, "node_errors": {}})
        if request.path.startswith("/history/"):
            facts.polls += 1
            if facts.delay:
                await asyncio.sleep(facts.delay)
            if facts.mode == "pending" or facts.polls < 3:
                return web.json_response({})
            if facts.mode == "error":
                return web.json_response({"prompt-http-only": {"status": {"status_str": "error", "completed": False,
                    "messages": [["execution_error", {"exception_message": "fixture terminal error"}]]}, "outputs": {}}})
            (output_root / "renders").mkdir(exist_ok=True)
            (output_root / "renders" / "result.png").write_bytes(facts.output)
            descriptor = {"filename": "result.png", "subfolder": "renders", "type": "output"}
            if facts.mode == "unsafe":
                descriptor["subfolder"] = "../input"
            return web.json_response({"prompt-http-only": {"status": {"status_str": "success", "completed": True,
                "messages": []}, "outputs": {"2": {"images": [descriptor]}}}})
        return None
    network["handler"] = handler
    facts.destination = root / "outputs" / "invocation"
    return facts


def execute_route(route, endpoint, *, deadline_seconds=None, settle=False, **transport_kwargs):
    @contextmanager
    def checkout(**kwargs):
        route.factory += 1
        if deadline_seconds is not None:
            transport_kwargs["deadline"] = time.monotonic() + deadline_seconds
        with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000, **transport_kwargs) as transport:
            route.transport = transport
            adapter = backend.CheckoutServerAdapter.from_host_session(**kwargs, endpoint_transport=transport)
            route.adapter = adapter
            try:
                yield adapter
            finally:
                route.closed += 1
    if settle:
        from astrid.core.pack.entrypoint import canonical_runtime_entrypoint
        profile_path = route.destination.parent / "readiness.json"
        profile_path.write_bytes(encoded(route.profile))
        loaded = production_engine.load_workflow_path(route.workflow, route.destination.parent / "preflight")
        identity = production_engine.loaded_workflow_execution_identity(loaded, route.profile)
        with canonical_runtime_entrypoint("vibecomfy.run"):
            action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
        return action._run_and_settle(
            route.workflow, route.destination, task_identity="task-http-only", attempt_identity="attempt-http-only",
            execution_identity=identity, readiness_profile_path=str(profile_path),
            readiness_profile_hash="sha256:" + hashlib.sha256(profile_path.read_bytes()).hexdigest(),
            workflow_inputs='{"reference":"reference.png"}', placement="runpod",
            execution_request={"target": {"kind": "runpod", "pod_id": "pod123", "provider_account_ref": "account",
                                          "storage": {"network_volume_id": "original-volume"}}},
            endpoint_checkout=checkout)
    return production_engine.run_workflow_result_path(
        route.workflow, route.destination, task_identity="task-http-only", attempt_identity="attempt-http-only",
        profile_id="checkout_server", hc03_profile=route.profile,
        workflow_input_bindings={"reference": "reference.png"}, endpoint_checkout=checkout)


def test_one_prompt_multiple_polls_chunks_and_managed_identity(route, endpoint, network):
    produced = execute_route(route, endpoint, deadline=time.monotonic() + 20)
    assert route.factory == route.closed == 1
    assert len(route.prompts) == 1 and route.polls == 3
    assert route.prompts[0]["prompt"]["1"]["inputs"]["image"] == "reference.png"
    assert produced.outputs[0].read_bytes() == route.output
    managed = produced.managed_generation_result
    assert managed["task_id"] == "task-http-only"
    assert managed["attempt_id"] == "attempt-http-only"
    assert managed["evidence"]["producer"]["metadata"]["record"]["execution_id"] == "execution-http-only"
    assert managed["producer_run_id"] == route.adapter._last_run_result.run_id
    row = managed["outputs"][0]
    assert row["sha256"] == hashlib.sha256(route.output).hexdigest()
    assert row["bytes"] == len(route.output)
    result_path = Path(produced.managed_generation_result_path)
    assert (result_path.parent / row["path"]).read_bytes() == route.output
    metadata = json.loads(Path(route.adapter._last_run_result.metadata_path).read_text())
    artifact = metadata["artifacts"][0]
    assert artifact["descriptor"] == {"filename": "result.png", "subfolder": "renders", "type": "output"}
    assert artifact["download_source"] == "runpod_lifecycle"
    assert "renders/result.png" in artifact["reported_path"]
    assert sum(path == "/uploads" for _, path, _ in network["exchanges"]) == 1
    assert sum(path.startswith("/uploads/") for _, path, _ in network["exchanges"]) >= 2
    assert sum(path == "/download-chunk" for _, path, _ in network["exchanges"]) >= 3
    assert network["timeouts"] and 0 < max(network["timeouts"]) <= 15
    assert 101 not in network["responses"]
    assert all("/ws" not in path and "/view" not in path and "Range" not in headers and "Upgrade" not in headers
               for _, path, headers in network["exchanges"])
    assert not list(result_path.parent.glob("generation-manifest*"))


def test_registry_action_settles_real_json_custody_to_original_manifest_v2(route, endpoint, network):
    manifest = execute_route(route, endpoint, settle=True)
    assert manifest["schema_version"] == 2
    assert json.loads((route.destination / "manifest.json").read_bytes()) == manifest
    assert route.factory == route.closed == len(route.prompts) == 1
    assert route.polls == 3
    media, envelope = manifest["outputs"]
    assert media["output_port"] == "vibecomfy_run" and media["ordinal"] == 0 and media["role"] == "result"
    assert media["content_hash"] == "sha256:" + hashlib.sha256(route.output).hexdigest()
    assert (route.destination / media["path"]).read_bytes() == route.output
    assert envelope["output_port"] == "result_manifest" and envelope["role"] == "auxiliary"
    assert hashlib.sha256((route.destination / envelope["path"]).read_bytes()).hexdigest() == envelope["content_hash"].removeprefix("sha256:")
    assert manifest["managed_generation_result"]["task_id"] == "task-http-only"
    assert manifest["managed_generation_result"]["attempt_id"] == "attempt-http-only"
    assert manifest["managed_generation_result"]["producer_run_id"] == route.adapter._last_run_result.run_id
    assert not list(route.destination.rglob("generation-manifest*"))
    assert all("/ws" not in path and "/view" not in path and "Range" not in headers and "Upgrade" not in headers
               for _, path, headers in network["exchanges"])


@pytest.mark.parametrize("mode", ["error", "uncertain", "unsafe"])
def test_terminal_failure_or_uncertain_submit_never_replays(route, endpoint, network, mode):
    route.mode = mode
    with pytest.raises(Exception) as failure:
        execute_route(route, endpoint)
    assert len(route.prompts) == 1
    assert route.polls == (0 if mode == "uncertain" else 3)
    assert not [path for _, path, _ in network["exchanges"] if path == "/download-chunk"]
    assert route.factory == route.closed == 1
    if mode == "uncertain":
        attempts = list(route.destination.parents[2].glob("runtime/out/runs/*/attempt.json"))
        assert len(attempts) == 1
        assert "acceptance unknown" in str(failure.value)
        assert failure.value.acceptance_known is False and failure.value.prompt_id is None
        assert Path(failure.value.receipt_path) == attempts[0]
        # Concrete upstream limitation, left unchanged without a pin decision:
        assert json.loads(attempts[0].read_text())["queue_acceptance"]["status"] == "rejected"


def test_attachment_refuses_a_second_prompt_even_after_lost_reply(route, endpoint):
    route.mode = "uncertain"
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        with httpx.Client(trust_env=False) as client:
            first = client.post(transport.server_url + "/prompt", json={"prompt": {}})
            assert first.status_code == 502
            assert client.post(transport.server_url + "/prompt", json={"prompt": {}}).status_code == 409
    assert len(route.prompts) == 1


def test_shared_caller_deadline_bounds_actual_remote_history_leg(route, endpoint, network):
    route.mode, route.delay = "pending", 10
    started = time.monotonic()
    with pytest.raises(Exception):
        execute_route(route, endpoint, deadline_seconds=8)
    assert len(route.prompts) == 1 and route.polls >= 1
    assert time.monotonic() - started < 12
    assert network["bounded_timeouts"] and all(0 < value <= 8 for value in network["bounded_timeouts"])


@pytest.mark.parametrize("engine_configuration", [False], indirect=True)
def test_missing_exact_input_write_permission_fails_before_prompt(route, endpoint, network):
    with pytest.raises(Exception, match="403"):
        execute_route(route, endpoint)
    assert route.prompts == [] and route.polls == 0
    assert not [path for _, path, _ in network["exchanges"] if "/upload/image" in path]


def test_bad_json_chunk_digest_never_produces_managed_result(route, endpoint, network, tmp_path):
    network["corrupt_digest"] = True
    with pytest.raises(Exception, match="retrieval failed"):
        execute_route(route, endpoint)
    assert len(route.prompts) == 1 and route.polls == 3
    assert not list((tmp_path / "runtime").rglob("managed-generation-result.json"))


@pytest.mark.parametrize("entry", ["_run_profile_result", "_run_profile", "run_workflow_path", "run_workflow_result_path"])
def test_checkout_transport_conflict_refused_before_effects(tmp_path, entry):
    common = dict(task_identity="task", endpoint_checkout=object(), endpoint_transport=object())
    with pytest.raises(production_engine.ProductionEngineError, match="mutually exclusive"):
        if entry.startswith("_run_profile"):
            getattr(production_engine, entry)(None, "checkout_server", None, model_id="m", template_id="t",
                                             destination=tmp_path / "out", **common)
        else:
            getattr(production_engine, entry)(tmp_path / "missing", tmp_path / "out", **common)
    assert not (tmp_path / "out").exists()


# ---- Provider no-route retry at the actual claimed-agent boundary ----------

def provider_script(method, suffix, actions):
    """Provider replies, in order, for matching agent-bound requests.

    actions is a list (None = deliver to the agent) or a callable for every
    matching request. Non-matching requests are always delivered.
    """
    queue = list(actions) if not callable(actions) else None

    def provider(command, path):
        if command != method or not path.split("?", 1)[0].endswith(suffix):
            return None
        if queue is None:
            return actions()
        return queue.pop(0) if queue else None
    return provider


def attempts(network, method, suffix):
    return [a for a in network["attempts"] if a[0] == method and a[1].split("?", 1)[0].endswith(suffix)]


def test_prompt_empty_404_then_accepted_exactly_once(route, endpoint, network):
    network["provider"] = provider_script("POST", "/prompt", ["no-route"])
    produced = execute_route(route, endpoint, deadline=time.monotonic() + 20)
    assert produced.outputs[0].read_bytes() == route.output
    prompt_attempts = attempts(network, "POST", "/prompt")
    assert [a[4] for a in prompt_attempts] == ["no-route", "delivered"]
    # Unchanged method/path/origin/bearer/payload bytes on the retry.
    assert len({(a[0], a[1], a[2]) for a in prompt_attempts}) == 1
    assert prompt_attempts[0][2] == "Bearer " + TOKEN
    assert route.prompt_bodies == [prompt_attempts[0][3]]
    assert len(route.prompts) == 1 and route.polls == 3
    assert all(target == "pod123-8000.proxy.runpod.net:443" for target, _ in network["proxy"].connects)
    assert network["proxy"].denied == []
    assert 0 < max(network["timeouts"]) <= 15


def test_history_empty_404_then_pending_then_terminal(route, endpoint, network):
    network["provider"] = provider_script("GET", "/history/prompt-http-only", ["no-route", None, "no-route"])
    produced = execute_route(route, endpoint, deadline=time.monotonic() + 20)
    assert produced.outputs[0].read_bytes() == route.output
    outcomes = [a[4] for a in attempts(network, "GET", "/history/prompt-http-only")]
    # no-route, pending, no-route, pending, terminal success: polls see 3.
    assert outcomes == ["no-route", "delivered", "no-route", "delivered", "delivered"]
    assert len(route.prompts) == 1 and route.polls == 3


def test_history_empty_404_terminal_error_is_final(route, endpoint, network):
    route.mode = "error"
    network["provider"] = provider_script("GET", "/history/prompt-http-only", ["no-route"])
    with pytest.raises(Exception, match="fixture terminal error"):
        execute_route(route, endpoint, deadline=time.monotonic() + 20)
    assert len(route.prompts) == 1 and route.polls == 3
    assert not [a for a in network["attempts"] if a[1] == "/download-chunk"]


def test_repeated_empty_404_ends_within_shared_caller_deadline(route, endpoint, network):
    network["provider"] = provider_script("GET", "/history/prompt-http-only", lambda: "no-route")
    started = time.monotonic()
    with pytest.raises(Exception) as failure:
        execute_route(route, endpoint, deadline_seconds=6)
    elapsed = time.monotonic() - started
    assert elapsed < 10
    history = attempts(network, "GET", "/history/prompt-http-only")
    assert len(history) >= 3 and {a[4] for a in history} == {"no-route"}
    assert len(route.prompts) == 1 and route.polls == 0
    assert "acceptance unknown" not in str(failure.value)
    assert network["bounded_timeouts"] and all(0 < value <= 6 for value in network["bounded_timeouts"])
    assert not [a for a in network["attempts"] if a[1] == "/download-chunk"]


def test_agent_json_404_on_prompt_is_final(route, endpoint, network):
    network["provider"] = provider_script("POST", "/prompt", ["json404"])
    with pytest.raises(Exception) as failure:
        execute_route(route, endpoint, deadline=time.monotonic() + 20)
    assert [a[4] for a in attempts(network, "POST", "/prompt")] == ["json404"]
    assert route.prompts == [] and route.polls == 0
    assert "acceptance unknown" not in str(failure.value)


@pytest.mark.parametrize("outcome", ["502", "drop"])
def test_lost_reply_or_502_is_unknown_and_never_resubmitted(route, endpoint, network, outcome):
    network["provider"] = provider_script("POST", "/prompt", [outcome])
    with pytest.raises(Exception) as failure:
        execute_route(route, endpoint, deadline=time.monotonic() + 20)
    assert [a[4] for a in attempts(network, "POST", "/prompt")] == [outcome]
    assert route.prompts == [] and route.polls == 0
    assert "acceptance unknown" in str(failure.value)
    assert failure.value.acceptance_known is False and failure.value.prompt_id is None
    # The attachment refuses any later prompt, even after a lost reply.
    try:
        with httpx.Client(trust_env=False) as client:
            assert client.post(route.transport.server_url + "/prompt", json={"prompt": {}}).status_code == 409
    finally:
        route.transport.close()
    assert [a[4] for a in attempts(network, "POST", "/prompt")] == [outcome]


# ---- Canonical media input directory (Contracts v2.1 23:25) ------------------

def test_canonical_registry_without_input_directory_uploads_from_pinned_default(route, endpoint, network):
    produced = execute_route(route, endpoint, deadline=time.monotonic() + 20)
    assert produced.outputs[0].read_bytes() == route.output
    expected = canonical_session(route.source.parents[4]).input
    assert route.adapter._host_session["input_directory"] == str(expected)
    uploads = [a for a in network["attempts"] if a[1] == "/uploads"]
    assert len(uploads) == 1
    config = json.loads(Path(route.profile["vibecomfy_session"]["session_dir"], "config.json").read_bytes())
    assert "input_directory" not in config
    assert route.profile["vibecomfy_session"]["config_digest"] == "sha256:" + hashlib.sha256(encoded(config)).hexdigest()


@pytest.mark.parametrize("change", ["cwd", "base_directory", "runtime_root", "missing_runtime_root"])
def test_input_directory_derivation_requires_actual_launch_selection(tmp_path, change):
    from astrid.core.generation.engine_recipe import load_recipe
    recipe = load_recipe("vibecomfy-h3", PLATFORM)
    config = dict(canonical_session(tmp_path).config)
    assert backend._endpoint_session_input_directory(config, recipe) == config["cwd"] + "/input"
    if change == "missing_runtime_root":
        config.pop("runtime_root")
    else:
        config[change] = str(tmp_path / "foreign" / ("runtime" if change == "runtime_root" else "ComfyUI"))
    with pytest.raises(ValueError):
        backend._endpoint_session_input_directory(config, recipe)


def test_explicit_input_directory_is_kept_and_override_flags_refused(tmp_path, monkeypatch):
    from dataclasses import replace
    from astrid.core.generation.engine_recipe import load_recipe
    recipe = load_recipe("vibecomfy-h3", PLATFORM)
    config = dict(canonical_session(tmp_path).config)
    explicit = str(tmp_path / "explicit-input")
    assert backend._endpoint_session_input_directory({**config, "input_directory": explicit}, recipe) == explicit
    with pytest.raises(ValueError):
        backend._endpoint_session_input_directory({**config, "input_directory": "relative"}, recipe)
    for flag in ("--input-directory", "--base-directory"):
        overridden = replace(recipe, start=replace(recipe.start, argv=(*recipe.start.argv, flag, "/elsewhere")))
        with pytest.raises(ValueError):
            backend._endpoint_session_input_directory(config, overridden)
