"""The private retained-host producer path has no admission authority."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host as host
from astrid.core.generation import preflight
from astrid.core.generation.backends.comfy_target import ComfyTargetClient
from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
from astrid.core.generation.model_root import canonical_model_inventory_digest
from astrid.sdk import generation
from tests.core.generation.backends.test_comfy_model_resolution import target_info


VERSION = "runtime.local-image-recipe/v1"
CLOSURE = "3" * 64


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


@pytest.fixture
def retained_producer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Real model validation, fake already-prepared graph, no processes or RPCs."""
    root = tmp_path / "model-base" / "models"
    inventory = []
    for subdir, name in (("diffusion_models", "z_image_turbo_bf16.safetensors"),
                         ("text_encoders", "qwen_3_4b.safetensors"), ("vae", "ae.safetensors")):
        model = root / subdir / name
        model.parent.mkdir(parents=True, exist_ok=True)
        model.write_bytes(("fixture:" + name).encode())
        inventory.append({"name": name, "subdir": subdir, "size": model.stat().st_size,
                          "sha256": _digest(model.read_bytes())})
    model_root = {"schema_version": 1, "path": str(root), "inventory": inventory,
                  "inventory_digest": canonical_model_inventory_digest(inventory),
                  "qualification_identity": None}
    runtime_root = tmp_path / "runtime"
    session_root = runtime_root / "out" / "sessions" / "fixture"
    session_root.mkdir(parents=True)
    config = {"models_root": str(root), "models_root_normalized": str(root),
              "base_directory": str(root.parent), "launch_flags": ["--disable-all-custom-nodes"],
              "port": 8188, "locality": "managed_local_server", "warm_policy": "auto",
              "cwd": str(runtime_root), "runtime_root": str(runtime_root),
              "server_log_path": str(session_root / "comfy.log"), "ready_timeout_sec": 10}
    config_bytes = json.dumps(config, indent=2, sort_keys=True).encode("utf-8")
    (session_root / "config.json").write_bytes(config_bytes)
    argv = ["/installed/python", "serve", "--base-directory", str(root.parent),
            "--disable-all-custom-nodes"]
    (session_root / "server_argv.json").write_text(json.dumps(argv), encoding="utf-8")
    session = {"session_dir": str(session_root), "server_url": "http://127.0.0.1:8188",
               "pid": 7101, "comfy_pid": 7102, "process_birth_id": "engine-birth",
               "comfy_process_birth_id": "listener-birth", "launch_token": "fixture-launch",
               "source_revision": "fixture-revision", "source_content_digest": _digest(b"vibe-source"),
               "config_digest": _digest(config_bytes)}
    readiness = {"launch": {"model_root": model_root}, "vibecomfy_session": copy.deepcopy(session)}
    owner = {"pid": 7001, "uid": os.getuid(), "birth_id": "runtime-birth",
             "runtime_instance_id": "epoch", "coordinator_epoch": "epoch"}
    selected = {"profile_id": "fixture", "workspace_uuid": "realm", "realm_root": str(tmp_path / "realm"),
                "support_root": str(tmp_path / "support"), "machine_id": "machine",
                "worker_executable": "/installed/python", "host_executable": "/installed/python",
                "engine_executable": "/installed/python", "engine_listener_executable": "/installed/python",
                "engine_endpoint": session["server_url"], "session_config_digest": session["config_digest"],
                "profile_revision": "profile-revision", "profile_digest": _digest(b"profile"),
                "release_digest": _digest(b"release"),
                "host_protocol": {"version": host.CURRENT_ROLES_VERSION,
                                  "module": "astrid.core.execution.generic_host",
                                  "source_closure_digest": CLOSURE},
                "engine_launch": {"module": "vibecomfy.commands.session", "session_root": str(session_root),
                                  "config": config, "source_revision": session["source_revision"],
                                  "source_content_digest": session["source_content_digest"],
                                  "listener_argv": list(argv),
                                  "adapter_pins": {name: _digest(name.encode()) for name in (
                                      "session_source_sha256", "spawn_sha256", "cleanup_sha256",
                                      "stop_sha256", "adapter_source_sha256")}}}
    for role in ("worker", "host", "engine", "engine_listener"):
        selected[role + "_artifact_digest"] = _digest(role.encode())
    processes = {"host": {"pid": os.getpid(), "birth_id": "host-birth"},
                 "engine": {"pid": session["pid"], "birth_id": session["process_birth_id"]},
                 "engine_listener": {"pid": session["comfy_pid"], "birth_id": session["comfy_process_birth_id"]}}
    measured = {"processes": processes, "session_config_digest": session["config_digest"]}
    preparation = host.LocalExecutionPreparation(operation_id="operation", channel_id="channel",
        graph_factory=lambda **_: pytest.fail("recipe preparation must not allocate a graph"))
    preparation._request = {"owner_epoch": "epoch", "runtime_owner": owner, "profile": selected,
                            "custody_scope": str(tmp_path / "scope")}
    preparation.result = {"version": host._LOCAL_PREPARATION_VERSION, "status": "prepared",
                          "operation_id": "operation", "channel_id": "channel", "owner_epoch": "epoch",
                          **copy.deepcopy(measured)}
    preparation.graph = SimpleNamespace(readiness_profile={"vibecomfy_session": copy.deepcopy(session)},
                                        report=lambda: copy.deepcopy(measured))
    preparation._retained_relay_actor = SimpleNamespace(verify=lambda: copy.deepcopy(owner))
    preparation._activation_grant = {"fixture": "already activated"}
    preparation._claim_host = SimpleNamespace()
    monkeypatch.setattr(host, "process_birth_identity", lambda: "host-birth")
    preparation._retain_producer_profile(readiness, source_closure_digest=CLOSURE)
    prompt = "A precise blue bird\nwith UTF-8 plumage: 🐦"
    prompt_bytes = prompt.encode("utf-8")
    request = {"version": VERSION, "command": "prepare_image_recipe", "operation_id": "operation",
               "channel_id": "channel", "owner_epoch": "epoch", "request_id": "request-1",
               "inputs": [{"name": "prompt", "object_id": _digest(prompt_bytes), "digest": _digest(prompt_bytes),
                           "filename": "prompt.txt", "bytes_base64": base64.b64encode(prompt_bytes).decode("ascii")}],
               "controls": {"seed": 42, "size": "512x384", "steps": 2, "guidance_scale": 1.0,
                            "negative_prompt": "blur"}}
    recipe = {"version": "astrid.local-image-recipe/v1", "fixture": "complete helper result"}
    recipe_bytes = _canonical(recipe)
    calls = []
    def prepare(*args, **kwargs):
        calls.append((args, kwargs))
        return recipe_bytes
    real_prepare = generation._prepare_verified_image_recipe
    monkeypatch.setattr(generation, "_prepare_verified_image_recipe", prepare)
    def forbidden(*args, **kwargs):
        pytest.fail("private preparation crossed the admission/provider boundary")
    monkeypatch.setattr(generation.GenerationFacade, "workflow", forbidden)
    monkeypatch.setattr(generation.GenerationFacade, "_invoke", forbidden)
    monkeypatch.setattr(host.GenericPackHost, "run_task", forbidden)
    monkeypatch.setattr(VibeComfyBackend, "_run_workflow", forbidden)
    return SimpleNamespace(preparation=preparation, request=request, calls=calls, recipe_bytes=recipe_bytes,
                           readiness=readiness, selected=selected, session=session, measured=measured,
                           model=model, root=root, session_root=session_root, config=config,
                           owner=owner, prompt=prompt, real_prepare=real_prepare)


def test_retained_host_prepares_frozen_recipe_without_admission(retained_producer):
    fixture = retained_producer
    result = fixture.preparation.dispatch(fixture.request)
    assert len(fixture.calls) == 1
    assert result["version"] == VERSION
    assert result["status"] == "prepared"
    assert base64.b64decode(result["recipe_base64"], validate=True) == fixture.recipe_bytes
    assert result["recipe_digest"] == _digest(fixture.recipe_bytes)
    assert result["request_id"] == fixture.request["request_id"]
    assert result["operation_id"] == "operation" and result["channel_id"] == "channel"
    assert result["owner_epoch"] == "epoch"
    assert result["request_digest"] == _digest(_canonical(fixture.request))
    assert result["inputs"] == fixture.request["inputs"]
    binding = result["binding"]
    assert binding["source_closure_digest"] == CLOSURE
    assert binding["model_root"] == fixture.readiness["launch"]["model_root"]
    assert binding["session"] == fixture.session
    assert binding["processes"] == fixture.measured["processes"]
    assert binding["profile_digest"] == fixture.selected["profile_digest"]
    assert binding["profile_binding_digest"] == _digest(_canonical(fixture.selected))
    assert base64.b64decode(binding["session_config_base64"]) == (fixture.session_root / "config.json").read_bytes()
    assert base64.b64decode(binding["server_argv_base64"]) == (fixture.session_root / "server_argv.json").read_bytes()
    args, kwargs = fixture.calls[0]
    assert args == ()
    assert kwargs["inputs"] == {"prompt": fixture.prompt, **fixture.request["controls"]}
    assert kwargs["model_root"].path == fixture.root
    assert kwargs["target_client"].target.url == fixture.session["server_url"]
    assert fixture.preparation.result["version"] == host._LOCAL_PREPARATION_VERSION
    assert not fixture.preparation._aborted


def test_private_dispatch_freezes_real_installed_producer_recipe(retained_producer, monkeypatch):
    """Exercise socket dispatcher through actual catalog binding and compiler."""
    fixture = retained_producer
    monkeypatch.setattr(generation, "_prepare_verified_image_recipe", fixture.real_prepare)
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(preflight, "_comfyui_runtime_available", lambda: pytest.fail("installation probed"))
    observed = []
    def object_info(client):
        observed.append(client.target.url)
        return target_info(vaes=("ae.safetensors",))
    def forbidden(*args, **kwargs):
        pytest.fail("private producer submitted a provider request")
    monkeypatch.setattr(ComfyTargetClient, "object_info", object_info)
    monkeypatch.setattr(ComfyTargetClient, "request_json", forbidden)
    monkeypatch.setattr(ComfyTargetClient, "submit_prompt", forbidden)
    result = fixture.preparation.dispatch(fixture.request)
    assert result["status"] == "prepared"
    raw = base64.b64decode(result["recipe_base64"], validate=True)
    assert result["recipe_digest"] == _digest(raw)
    recipe = json.loads(raw)
    assert raw == _canonical(recipe)
    assert recipe["entry"] == "z-image"
    assert recipe["effective_inputs"]["prompt"] == fixture.prompt
    assert recipe["resolved_randomness"] == {"seed": 42}
    assert recipe["effective_inputs"]["seed"] == 42
    assert recipe["workflow_inputs"]["width"]["value"] == 512
    assert recipe["workflow_inputs"]["height"]["value"] == 384
    assert recipe["model_root"] == result["binding"]["model_root"]
    assert len(recipe["model_references"]) == 3
    assert all(item["inventory"] in recipe["model_root"]["inventory"] for item in recipe["model_references"])
    assert set(recipe["members"]) == {"workflow.py", "workflow.vibe.json", "source.json"}
    assert recipe["approval"]["api_projection"] and recipe["approval"]["ui_projection"]
    assert observed and set(observed) == {fixture.session["server_url"]}
    # Runtime can verify/retain every returned member after scratch teardown.
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    for member in recipe["members"].values():
        data = base64.b64decode(member["bytes_base64"], validate=True)
        assert _digest(data) == member["sha256"] and len(data) == member["size"]
    assert result["inputs"] == fixture.request["inputs"]


@pytest.mark.parametrize("peer_matches", [True, False])
def test_private_wire_uses_retained_peer_and_preserves_ordinary_report(retained_producer, monkeypatch, peer_matches):
    from astrid.core.execution import custody_broker

    fixture = retained_producer
    peer = copy.deepcopy(fixture.owner)
    if not peer_matches:
        peer["birth_id"] = "different private peer"
    actor = SimpleNamespace(verify=lambda: copy.deepcopy(peer))
    authenticated = []
    def private_peer(channel):
        authenticated.append(channel.fileno())
        return actor
    monkeypatch.setattr(custody_broker.AuthenticatedCleanupActor, "private_peer", staticmethod(private_peer))
    client, server = socket.socketpair()
    client.settimeout(5)
    thread = fixture.preparation.serve_control(server.detach())
    try:
        host._send_activation_frame(client, fixture.request)
        result = host._read_activation_frame(client)
        assert len(authenticated) == 1
        if peer_matches:
            assert result["status"] == "prepared" and result["recipe_digest"] == _digest(fixture.recipe_bytes)
            assert len(fixture.calls) == 1
            host._send_activation_frame(client, {**fixture.request, "model": "caller-selected"})
            rejected = host._read_activation_frame(client)
            assert rejected["status"] == "unresolved"
            assert "recipe_base64" not in rejected and len(fixture.calls) == 1
        else:
            assert result["status"] == "unresolved"
            assert "recipe_base64" not in result and fixture.calls == []
        host._send_activation_frame(client, {"version": host._LOCAL_PREPARATION_VERSION,
            "command": "report_local_execution", "operation_id": "operation", "channel_id": "channel",
            "owner_epoch": "epoch"})
        assert host._read_activation_frame(client) == fixture.preparation.result
    finally:
        client.close()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize("field,value", [
    ("version", "runtime.local-image-recipe/v99"), ("command", "prepare_recipe"),
    ("operation_id", "other"), ("channel_id", "other"), ("owner_epoch", "other"),
    ("request_id", ""), ("request_id", True),
])
def test_private_request_binding_fails_closed(retained_producer, field, value):
    fixture = retained_producer
    request = {**fixture.request, field: value}
    with pytest.raises(host.HostError):
        fixture.preparation.dispatch(request)
    assert fixture.calls == []


@pytest.mark.parametrize("field", ["model", "model_root", "target", "profile", "session",
                                   "code", "import_root", "code_hash", "trusted", "ready"])
def test_request_cannot_select_producer_or_assert_authority(retained_producer, field):
    fixture = retained_producer
    with pytest.raises(host.HostError):
        fixture.preparation.dispatch({**fixture.request, field: "caller-selected"})
    assert fixture.calls == []


@pytest.mark.parametrize("controls", [
    {"model": "caller-model"}, {"models_root": "/caller-root"}, {"target": "http://example.invalid"},
    {"session": "caller-session"}, {"seed": {"value": 42}}, {"steps": [2]},
    {"guidance_scale": float("nan")}, {"guidance_scale": float("inf")},
    {"size": {"width": 512}},
])
def test_controls_cannot_add_authority_or_unbounded_values(retained_producer, controls):
    fixture = retained_producer
    with pytest.raises(host.HostError):
        fixture.preparation.dispatch({**fixture.request, "controls": controls})
    assert fixture.calls == []


@pytest.mark.parametrize("case", ["wrong_bytes", "wrong_object", "bad_base64", "bad_utf8", "duplicate",
                                  "extra_input", "locator", "wrong_name", "wrong_filename"])
def test_managed_input_bytes_and_descriptor_are_verified_before_producer(retained_producer, case):
    fixture = retained_producer
    request = copy.deepcopy(fixture.request)
    item = request["inputs"][0]
    if case == "wrong_bytes": item["bytes_base64"] = base64.b64encode(b"different prompt").decode()
    elif case == "wrong_object": item["object_id"] = _digest(b"different object")
    elif case == "bad_base64": item["bytes_base64"] = "***"
    elif case == "bad_utf8":
        item.update(bytes_base64=base64.b64encode(b"\xff").decode(), digest=_digest(b"\xff"), object_id=_digest(b"\xff"))
    elif case in {"duplicate", "extra_input"}:
        request["inputs"].append({**item, **({"name": "image"} if case == "extra_input" else {})})
    elif case == "locator": item["path"] = str(fixture.model)
    elif case == "wrong_name": item["name"] = "other"
    elif case == "wrong_filename": item["filename"] = "prompt.py"
    with pytest.raises(host.HostError):
        fixture.preparation.dispatch(request)
    assert fixture.calls == []


@pytest.mark.parametrize("case", ["missing_peer", "wrong_peer", "unresolved", "aborted", "error", "no_graph",
                                  "no_activation", "no_claim_host", "wrong_host", "changed_graph",
                                  "wrong_closure", "wrong_module", "wrong_protocol", "session_mismatch",
                                  "config_mismatch", "model_bytes"])
def test_live_retained_authority_is_rechecked_before_preparation(retained_producer, case):
    fixture = retained_producer
    preparation = fixture.preparation
    if case == "missing_peer": preparation._retained_relay_actor = None
    elif case == "wrong_peer": preparation._retained_relay_actor = SimpleNamespace(verify=lambda: {**fixture.owner, "birth_id": "foreign"})
    elif case == "unresolved": preparation.result["status"] = "unresolved"
    elif case == "aborted": preparation._aborted = True
    elif case == "error": preparation.error = RuntimeError("failed graph preparation")
    elif case == "no_graph": preparation.graph = None
    elif case == "no_activation": preparation._activation_grant = None
    elif case == "no_claim_host": preparation._claim_host = None
    elif case == "wrong_host": fixture.measured["processes"]["host"]["birth_id"] = "replaced host"
    elif case == "changed_graph": fixture.measured["processes"]["engine"]["pid"] += 1
    elif case == "wrong_closure": fixture.selected["host_protocol"]["source_closure_digest"] = "4" * 64
    elif case == "wrong_module": fixture.selected["host_protocol"]["module"] = "caller.host"
    elif case == "wrong_protocol": fixture.selected["host_protocol"]["version"] = "future"
    elif case == "session_mismatch": preparation.graph.readiness_profile["vibecomfy_session"]["comfy_pid"] += 1
    elif case == "config_mismatch": (fixture.session_root / "config.json").write_text("{}")
    elif case == "model_bytes": fixture.model.write_bytes(b"tampered model bytes")
    with pytest.raises((host.HostError, ValueError)):
        preparation.dispatch(fixture.request)
    assert fixture.calls == []


@pytest.mark.parametrize("case", ["models_root", "normalized_root", "base_directory", "extra_root",
                                  "extra_config", "custom_nodes", "argv_root", "argv_extras", "base_yaml", "cwd_yaml"])
def test_selected_model_root_cannot_be_redirected_by_session(retained_producer, case):
    fixture = retained_producer
    config = fixture.selected["engine_launch"]["config"]
    if case == "models_root": config["models_root"] = str(fixture.root.parent / "other")
    elif case == "normalized_root": config["models_root_normalized"] = str(fixture.root.parent / "other")
    elif case == "base_directory": config["base_directory"] = str(fixture.root.parent / "other")
    elif case == "extra_root": config["extra"] = {"model_paths": [str(fixture.root.parent / "other")]}
    elif case == "extra_config": config["extra_model_paths_config"] = ["/other/models.yaml"]
    elif case == "custom_nodes": config["launch_flags"] = []
    elif case in {"argv_root", "argv_extras"}:
        argv_path = fixture.session_root / "server_argv.json"
        argv = json.loads(argv_path.read_text())
        if case == "argv_root": argv[argv.index("--base-directory") + 1] = str(fixture.root.parent / "other")
        else: argv += ["--extra-model-paths-config", "/other/models.yaml"]
        argv_path.write_text(json.dumps(argv))
    elif case == "base_yaml": (fixture.root.parent / "extra_model_paths.yaml").write_text("{}")
    elif case == "cwd_yaml": (Path(config["cwd"]) / "extra_model_paths.yaml").write_text("{}")
    if case in {"models_root", "normalized_root", "base_directory", "extra_root", "extra_config", "custom_nodes"}:
        raw = json.dumps(config, indent=2, sort_keys=True).encode()
        (fixture.session_root / "config.json").write_bytes(raw)
        digest = _digest(raw)
        fixture.selected["session_config_digest"] = digest
        fixture.measured["session_config_digest"] = digest
        fixture.preparation.result["session_config_digest"] = digest
        fixture.readiness["vibecomfy_session"]["config_digest"] = digest
        fixture.preparation.graph.readiness_profile["vibecomfy_session"]["config_digest"] = digest
        # Model this configuration as the initial immutable startup snapshot.
        fixture.preparation._producer_profile_json = _canonical(fixture.readiness)
    with pytest.raises((host.HostError, ValueError)):
        fixture.preparation.dispatch(fixture.request)
    assert fixture.calls == []


def test_producer_failure_propagates_without_admission_or_fallback(retained_producer, monkeypatch):
    fixture = retained_producer
    calls = []
    def failed(*args, **kwargs):
        calls.append("prepare")
        raise ValueError("producer compilation failed")
    monkeypatch.setattr(generation, "_prepare_verified_image_recipe", failed)
    with pytest.raises((host.HostError, ValueError), match="producer|compilation"):
        fixture.preparation.dispatch(fixture.request)
    assert calls == ["prepare"]


@pytest.mark.parametrize("case", ["model", "session", "peer"])
def test_authority_drift_during_compilation_cannot_escape_as_prepared(retained_producer, monkeypatch, case):
    fixture = retained_producer
    calls = []
    def changed(*args, **kwargs):
        calls.append("prepare")
        if case == "model": fixture.model.write_bytes(b"model changed while compiling")
        elif case == "session": fixture.preparation.graph.readiness_profile["vibecomfy_session"]["launch_token"] = "new-launch"
        else: fixture.preparation._retained_relay_actor = SimpleNamespace(verify=lambda: {**fixture.owner, "pid": 9999})
        return fixture.recipe_bytes
    monkeypatch.setattr(generation, "_prepare_verified_image_recipe", changed)
    with pytest.raises((host.HostError, ValueError)):
        fixture.preparation.dispatch(fixture.request)
    assert calls == ["prepare"]


@pytest.mark.parametrize("recipe", [b"{}\n", b"not json", "caller string", {"mutable": "result"}])
def test_noncanonical_or_mutable_producer_result_is_rejected(retained_producer, monkeypatch, recipe):
    fixture = retained_producer
    monkeypatch.setattr(generation, "_prepare_verified_image_recipe", lambda **_: recipe)
    with pytest.raises((host.HostError, ValueError)):
        fixture.preparation.dispatch(fixture.request)


def test_startup_profile_is_detached_and_cannot_be_replaced(retained_producer):
    fixture = retained_producer
    before = fixture.preparation._producer_profile_json
    with pytest.raises(host.HostError, match="changed"):
        fixture.preparation._retain_producer_profile(fixture.readiness, source_closure_digest="4" * 64)
    assert fixture.preparation._producer_source_closure == CLOSURE
    assert fixture.preparation._producer_profile_json == before
    fixture.readiness["launch"]["model_root"]["path"] = "/caller/models"
    assert fixture.preparation._producer_profile_json == before
    with pytest.raises(host.HostError, match="changed"):
        fixture.preparation._retain_producer_profile(fixture.readiness, source_closure_digest=CLOSURE)
    assert fixture.preparation._producer_profile_json == before
