"""Real local HTTP/TLS transport; fake provider and readiness observers."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import select
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from aiohttp import web

from astrid.core.generation.engine_contracts import (
    AttestationError, DeadlineExceeded, EndpointAuth, EndpointHealthError,
    EndpointOwnershipError, EngineEndpoint, HealthEvidence, LifecycleOwner,
    RunPodAttestation, SecretRef, TransportError,
)
from astrid.core.generation.engine_endpoint import read_private_endpoint_file
from astrid.core.generation.engine_recipe import load_model_lock, load_recipe
from astrid.core.generation.engine_runner import plan_install
from astrid.core.generation.engine_transport import EngineEndpointTransport
from runpod_lifecycle.pod_agent import AgentServer, Handler

TOKEN = "endpoint-test-only-" + "x" * 40
PLATFORM = "linux-x86_64-cu130"
CLAIMED_AUTHORITY = "pod123-8000.proxy.runpod.net:443"
PROXY_ENV = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
             "NO_PROXY", "no_proxy", "SSL_CERT_DIR")


class EgressProxy(socketserver.ThreadingTCPServer):
    """Local stand-in for the controller's configured HTTPS egress proxy.

    It accepts only CONNECT to the claimed agent authority and tunnels those
    bytes to the local TLS agent. Anything else (a plain proxied request or
    another CONNECT target) is recorded and refused.
    """
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, agent_port):
        self.agent_port = agent_port
        self.connects, self.denied, self.tunneled = [], [], bytearray()
        super().__init__(("127.0.0.1", 0), _EgressHandler)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class _EgressHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client, head = self.request, b""
        client.settimeout(10)
        while b"\r\n\r\n" not in head and len(head) < 65536:
            block = client.recv(4096)
            if not block:
                return
            head += block
        lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        method, target = lines[0].split(" ")[:2]
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        if method != "CONNECT" or target != CLAIMED_AUTHORITY:
            self.server.denied.append((method, target, headers))
            client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        self.server.connects.append((target, headers))
        upstream = socket.create_connection(("127.0.0.1", self.server.agent_port), timeout=10)
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        try:
            while True:
                readable, _, _ = select.select([client, upstream], [], [], 30)
                if not readable:
                    return
                for sock in readable:
                    data = sock.recv(65536)
                    if not data:
                        return
                    if sock is client:
                        self.server.tunneled.extend(data)
                        upstream.sendall(data)
                    else:
                        client.sendall(data)
        except OSError:
            return
        finally:
            upstream.close()


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def canonical_session(tmp_path):
    """Exact installed-session registry config for the pinned recipe launch.

    Mirrors InstalledVibeComfySessionTransport.read_installed's exact dict:
    there is NO input_directory field. ComfyUI's input directory is the pinned
    default beneath the recipe's verified comfy root/base_directory.
    """
    install_root = tmp_path / "engine"
    cwd = install_root / "runtime" / "ComfyUI"
    registry = install_root / "runtime" / "out" / "sessions" / "vibecomfy-h3"
    config = {
        "runtime_root": str(install_root / "runtime"), "cwd": str(cwd), "port": 8188,
        "warm_policy": "never", "base_directory": str(cwd),
        "models_root": str(install_root / "models"), "models_root_normalized": str(install_root / "models"),
        "output_directory": str(cwd / "output"), "locality": "managed_local_server",
        "server_log_path": str(registry / "comfy.log"),
    }
    return SimpleNamespace(config=config, input=cwd / "input", output=cwd / "output")


def private(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(0o600)
    return path


@pytest.fixture
def engine_configuration():
    return {}


@pytest.fixture
def observation_configuration():
    return None


@pytest.fixture
def network(tmp_path, engine_configuration, observation_configuration):
    recipe = load_recipe("vibecomfy-h3", PLATFORM)
    facts = {"objects": {n: {} for n in recipe.health.required_nodes}, "upstream": [], "agent": [],
             "redirect": None, "recipient": [], "handler": None, "timeouts": [], "bounded_timeouts": [],
             "exchanges": [], "corrupt_digest": False, "responses": [], "image": b"image-data\x00\xff" * 200000,
             "provider": None, "attempts": []}
    loop = asyncio.new_event_loop()

    async def handle(request):
        facts["upstream"].append((request.method, request.path_qs, dict(request.headers)))
        if request.path == "/recipient":
            facts["recipient"].append(dict(request.headers))
            return web.Response(text="recipient")
        if facts["redirect"]:
            return web.Response(status=307, headers={"Location": facts["redirect"]})
        if facts["handler"] is not None:
            result = await facts["handler"](request)
            if result is not None:
                return result
        if request.path == "/view":
            return web.Response(body=facts["image"], content_type="image/png")
        if request.path == "/object_info":
            return web.json_response(facts["objects"])
        if request.path == "/system_stats":
            return web.json_response({"system": {"comfyui_version": "0.26.0"}})
        if request.path.startswith("/upload/"):
            facts["upload"] = await request.read()
            return web.json_response({"name": "input.png"})
        if request.path == "/prompt":
            facts["prompt"] = await request.json()
            return web.json_response({"prompt_id": "prompt-1"})
        if request.path.startswith("/history"):
            return web.json_response({"prompt-1": {"outputs": {}}})
        return web.json_response({"ok": True})

    async def start():
        app = web.Application(client_max_size=16 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, site._server.sockets[0].getsockname()[1]

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    runner, port = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
    facts["port"] = port
    key, cert = tmp_path / "tls.key", tmp_path / "tls.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=pod123-8000.proxy.runpod.net",
                    "-addext", "subjectAltName=DNS:pod123-8000.proxy.runpod.net",
                    "-keyout", str(key), "-out", str(cert)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    server = AgentServer(("127.0.0.1", 0), TOKEN, tmp_path / "state",
                         observation_policy=observation_configuration,
                         engine_proxy={"endpoint_id": "single", "port": port, **engine_configuration})

    class RecordingHandler(Handler):
        def send_response(self, code, message=None):
            facts["responses"].append(code)
            return super().send_response(code, message)

        def reply(self, status, data=None, **kwargs):
            if self.path == "/download-chunk" and facts["corrupt_digest"] and isinstance(data, dict) and "sha256" in data:
                data = {**data, "sha256": "0" * 64}
            return super().reply(status, data, **kwargs)

        def dispatch(self):
            facts["agent"].append((self.path, self.headers.get("Authorization")))
            facts["exchanges"].append((self.command, self.path, dict(self.headers)))
            action = facts["provider"](self.command, self.path) if facts["provider"] else None
            if action is None:
                facts["attempts"].append((self.command, self.path, self.headers.get("Authorization"), None, "delivered"))
                return super().dispatch()
            # Provider-side replies at the actual agent-bound boundary. The
            # agent's own handler never runs for these, so ComfyUI sees nothing.
            body = self.body()
            facts["attempts"].append((self.command, self.path, self.headers.get("Authorization"), body, action))
            self.close_connection = True
            if action == "no-route":  # RunPod proxy: empty-bodied 404
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif action == "json404":  # the agent's own final JSON 404
                self.reply(404, {"error": "not found"})
            elif action == "502":
                self.send_response(502)
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b"bad gateway")
            # "drop": the request was received; the reply is lost.

    server.RequestHandlerClass = RecordingHandler
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    agent_thread = threading.Thread(target=server.serve_forever, daemon=True)
    agent_thread.start()
    facts["server"] = server
    facts["cert"] = cert
    try:
        yield facts
    finally:
        server.shutdown()
        server.server_close()
        agent_thread.join(5)
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()


@pytest.fixture
def endpoint(tmp_path, network):
    digest = hashlib.sha256(encoded(network["objects"])).hexdigest()
    health = HealthEvidence("healthy", "2026-10-27T22:00:00Z", digest, True)
    token = private(tmp_path / "token", TOKEN.encode())
    attestation = RunPodAttestation("runpod-engine.v1", "account", "pod123", str(tmp_path / "snapshot"),
                                   "a" * 64, "/engine/.astrid-engine/install.json", "b" * 64, health, digest)
    return EngineEndpoint("astrid.engine-endpoint.v1", "single", "vibecomfy-h3", PLATFORM, "runpod",
                          "https://pod123-8000.proxy.runpod.net/engines/single",
                          EndpointAuth("bearer-file", SecretRef("file", str(token))), "ready", health,
                          LifecycleOwner("astrid-runpod", "original", str(tmp_path / "claim"), None, "pod123", False),
                          attestation)


@pytest.fixture
def local_tls(monkeypatch, network):
    # The production client is unpatched: the claimed-agent leg uses the
    # normal configured egress (HTTPS_PROXY, as observed on the controller)
    # and environment CA. Only this proxy maps the claimed authority to the
    # loopback agent; TLS still verifies the claimed hostname via SNI.
    proxy = EgressProxy(network["server"].server_port)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", proxy.url)
    monkeypatch.setenv("SSL_CERT_FILE", str(network["cert"]))
    original = EngineEndpointTransport._client

    def recording_client(transport, timeout):
        # Record each actual agent-bound request bound; behavior unchanged.
        network["timeouts"].append(timeout)
        if transport._deadline is not None:
            network["bounded_timeouts"].append(timeout)
        return original(transport, timeout)
    monkeypatch.setattr(EngineEndpointTransport, "_client", recording_client)
    network["proxy"] = proxy
    try:
        yield proxy
    finally:
        proxy.shutdown()
        proxy.server_close()
        thread.join(5)


def assert_only_claimed_egress(network):
    proxy = network["proxy"]
    assert proxy.denied == []
    assert proxy.connects and all(target == CLAIMED_AUTHORITY for target, _ in proxy.connects)
    for _, headers in proxy.connects:
        assert not {"authorization", "proxy-authorization", "cookie"} & {k.lower() for k in headers}
    assert TOKEN.encode() not in proxy.tunneled


def test_actual_vibecomfy_clients_http_only(endpoint, network, local_tls, tmp_path):
    from vibecomfy.runtime.client import ComfyClient
    from vibecomfy.schema.provider import get_target_schema_provider
    from astrid.core.generation.backends.vibecomfy import VibeComfyEngine
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        url = transport.server_url
        assert transport.server_url == url
        provider = get_target_schema_provider(url, cache_dir=tmp_path / "schema")
        assert provider.object_info() == network["objects"]
        async def exercise_client():
            client = ComfyClient(url)
            assert await client.ready()
            assert await client.object_info() == network["objects"]
            assert (await client._post_prompt({"1": {"class_type": "Example"}}))["prompt_id"] == "prompt-1"
            assert "prompt-1" in await client.history("prompt-1")
            assert await client.free() == {"ok": True}
        asyncio.run(exercise_client())
        engine = VibeComfyEngine("http://127.0.0.1:8188", endpoint_transport=transport)
        assert engine._origin == "http://127.0.0.1:8188"
        assert engine._post("/interrupt", {}) == {"ok": True}
        assert engine._post("/queue", {"delete": ["prompt-1"]}) == {"ok": True}
        with httpx.Client(trust_env=False) as client:
            for path in ("/ws", "/view?filename=output.png&type=output", "/upload/image"):
                assert client.get(url + path).status_code == 403
            assert client.get(url + "/object_info", headers={"Upgrade": "websocket"}).status_code == 403
    assert all(path.startswith("/engines/single/") and auth == "Bearer " + TOKEN
               for path, auth in network["agent"])
    assert all("Authorization" not in headers for _, _, headers in network["upstream"])
    assert network["timeouts"] and max(network["timeouts"]) <= 15
    assert_only_claimed_egress(network)


@pytest.mark.parametrize("change", ["pod", "port", "path", "query", "userinfo", "http"])
def test_wrong_origin_is_rejected_before_bearer_read(endpoint, monkeypatch, change):
    def forbidden(*args):
        pytest.fail("bearer opened before origin rejection")
    monkeypatch.setattr(EngineEndpointTransport, "_bearer", forbidden)
    url = endpoint.url
    url = {"pod": url.replace("pod123", "foreign"), "port": url.replace("8000", "9000"),
           "path": url.replace("/single", "/foreign"), "query": url + "?target=foreign",
           "userinfo": url.replace("https://", "https://user@"), "http": url.replace("https:", "http:")}[change]
    with pytest.raises(AttestationError):
        EngineEndpointTransport(replace(endpoint, url=url), pod_id="pod123", agent_port=8000)


def test_http_redirect_recipient_receives_nothing(endpoint, network, local_tls):
    network["redirect"] = f"http://127.0.0.1:{network['port']}/recipient"
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        with httpx.Client(trust_env=False, follow_redirects=True) as client:
            assert client.get(transport.server_url + "/object_info").status_code == 502
    assert network["recipient"] == []
    assert all("Authorization" not in headers for _, _, headers in network["upstream"])


def test_wrong_bearer_denies_http(endpoint, network, local_tls):
    Path(endpoint.auth.token.path).write_text("wrong-token-" + "z" * 40)
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        with httpx.Client(trust_env=False) as client:
            assert client.get(transport.server_url + "/object_info").status_code == 401
    assert network["upstream"] == []


def test_close_detaches_without_stopping_engine(endpoint, network, local_tls):
    transport = EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000)
    url = transport.server_url
    transport.close()
    with pytest.raises(httpx.ConnectError):
        httpx.get(url + "/object_info", trust_env=False, timeout=1)
    assert network["server"].socket.fileno() >= 0


@pytest.fixture
def prepared(tmp_path, endpoint, network):
    from astrid.packs.runpod.worker_owner import PreparedRunPodHost, RunPodRemoteWorkerInspector, RunPodRemoteWorkerPreparer
    from astrid.packs.runpod.worker_preparation import write_target_record

    recipe = load_recipe(endpoint.engine, endpoint.platform)
    root = "/engine"
    plan = plan_install(recipe, root=root, placement="local")
    models = load_model_lock(recipe.models_lock)
    receipt = {
        "schema": "astrid.engine-install.v1", "receipt_id": "installed", "engine": endpoint.engine,
        "platform": endpoint.platform, "placement": "local", "root": root, "status": "installed", "qualified": False,
        "recipe_sha256": plan.recipe_sha256, "models_lock_sha256": plan.models_lock_sha256,
        "requirements_locks": {e.name: "c" * 64 for e in recipe.environments},
        "source_commits": {s.name: s.pin.revision for s in recipe.sources},
        "models": [{"model": m.name, "path": root + "/models/" + m.destination, "action": "skipped", "bytes": m.bytes,
                    "sha256": m.sha256, "verified": True, "resumed_from_bytes": 0} for m in models.models],
        "steps": [{"id": s.id, "kind": s.kind, "action": "skipped", "validation": "passed", "facts": {}} for s in plan.steps],
        "health": asdict(endpoint.health), "started_at": "2026-10-27T21:00:00+00:00", "completed_at": "2026-10-27T22:00:00+00:00",
        "error_code": None,
    }
    claim = {"state": "claimed", "operation_id": "original", "provider_account_ref": "account", "pod_id": "pod123",
             "network_volume_id": "volume", "network_volume_name": "name", "network_volume_size_gb": 200,
             "network_volume_datacenter_id": "dc", "volume_mount_path": "/workspace", "acquisition_mode": "allocate",
             "transport": "https", "https_token_file": endpoint.auth.token.path}
    snapshot = private(tmp_path / "snapshot", encoded(claim))
    receipt_bytes = encoded(receipt)
    attestation = replace(endpoint.attestation, claim_handle_path=str(snapshot),
                          claim_handle_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                          install_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest())
    profile = {"schema_version": "hc03-worker-readiness.v1", "status": "ready",
               "vibecomfy_session": {"server_url": "http://127.0.0.1:8188"},
               "endpoint_evidence": {"schema": "astrid.runpod.endpoint-evidence.v1", "engine": endpoint.engine,
                                     "platform": PLATFORM, "claim_operation_id": "original", "claim_attestation": asdict(attestation)}}
    target = {"kind": "runpod", "pod_id": "pod123", "provider_account_ref": "account", "storage": {"network_volume_id": "volume"}}
    reference = SimpleNamespace(effective_target=target, readiness_profile_path=Path("/final-readiness.json"),
                                readiness_profile_hash="sha256:" + hashlib.sha256(encoded(profile)).hexdigest(),
                                session_config_digest="sha256:" + "d" * 64, session_ref="session", digest=lambda: "deployment")
    process = SimpleNamespace(incarnation="incarnation", as_dict=lambda: {"pid": 12, "birth_id": "original"})
    host = PreparedRunPodHost(reference, object(), process, "activation-operation", "channel", "/tmp/private/s", {}, "/ready",
                             activation_id="activation", session_receipt=SimpleNamespace(
                                 url="http://127.0.0.1:8188", config_digest=reference.session_config_digest,
                                 session_ref="session", pod_id="pod123", account_ref="account"))
    claim["managed_worker"] = {"claim_operation_id": "original", "deployment_digest": reference.digest(),
                               "process_handle": process.as_dict(), "activation_operation_id": host.operation_id, "activation_channel_id": host.channel_id,
                               "activation_qualification": {"activation_id": "activation"}, "state": "committed"}
    host.persisted = claim["managed_worker"]
    host.session_selection = SimpleNamespace(runtime_root="/engine/runtime")
    claim_path = tmp_path / "claim"
    write_target_record(claim_path, claim)
    preparer = RunPodRemoteWorkerPreparer(SimpleNamespace(id="pod123"), provider_account_ref="account",
                                         runtime_session_id="runtime", claim_handle_path=claim_path,
                                         journal_dir=tmp_path / "journal", staging_inputs={}, process_transport=object())
    preparer._handle = host
    inspector = RunPodRemoteWorkerInspector(preparer, runtime_observer=lambda: {}, session_config_observer=lambda h: {}, executor_observer=lambda e: {})
    observed = []
    # Only the external existing readiness witness is stubbed. The new ensure
    # parser, receipt/claim/slot checks and actual TLS health request run intact.
    inspector.observe_ready = lambda h: observed.append(h)
    files = {str(reference.readiness_profile_path): encoded(profile), attestation.install_receipt_path: receipt_bytes}
    calls = []

    def read_file(path, *, deadline):
        assert deadline > time.monotonic()
        calls.append(path)
        return files[path]

    kwargs = dict(engine=endpoint.engine, platform=PLATFORM, endpoint_id="single", agent_port=8000,
                  token_ref=endpoint.auth.token, read_file=read_file, deadline=time.monotonic() + 30)
    return SimpleNamespace(inspector=inspector, host=host, claim=claim, claim_path=claim_path, profile=profile,
                           files=files, kwargs=kwargs, observed=observed, receipt=receipt)


def test_ensure_reuses_one_original_claim_slot(prepared, network, local_tls):
    p = prepared
    first = p.inspector.ensure_endpoint(p.host, **p.kwargs)
    second = p.inspector.ensure_endpoint(p.host, **p.kwargs)
    assert first == second
    assert [path for _, path, _ in network["upstream"]] == ["/object_info"]
    saved = json.loads(p.claim_path.read_bytes())
    assert saved["operation_id"] == "original"
    assert saved["managed_worker"]["engine_endpoint"]["endpoint_id"] == "single"
    assert first.lifecycle_owner.handle_path == str(p.claim_path)
    assert first.lifecycle_owner.release_on_finish is False
    with pytest.raises(EndpointOwnershipError):
        p.inspector.ensure_endpoint(p.host, **{**p.kwargs, "endpoint_id": "replacement"})
    assert len(network["upstream"]) == 1


@pytest.mark.parametrize("field", ["operation_id", "provider_account_ref", "pod_id", "network_volume_id",
                                    "network_volume_name", "network_volume_size_gb", "network_volume_datacenter_id", "volume_mount_path"])
def test_claim_drift_refuses_before_bearer_send(prepared, network, local_tls, field):
    claim = json.loads(prepared.claim_path.read_bytes())
    claim[field] = "foreign"
    prepared.claim_path.write_bytes(encoded(claim))
    with pytest.raises(AttestationError):
        prepared.inspector.ensure_endpoint(prepared.host, **prepared.kwargs)
    assert network["agent"] == []


def test_readiness_mutation_and_deadline_refuse_before_send(prepared, network, local_tls):
    prepared.files["/final-readiness.json"] += b" "
    with pytest.raises(AttestationError):
        prepared.inspector.ensure_endpoint(prepared.host, **prepared.kwargs)
    with pytest.raises(DeadlineExceeded):
        prepared.inspector.ensure_endpoint(prepared.host, **{**prepared.kwargs, "deadline": time.monotonic() - 1})
    assert network["agent"] == []


def test_live_health_must_match_bound_object_info(prepared, network, local_tls):
    network["objects"] = {}
    with pytest.raises(EndpointHealthError):
        prepared.inspector.ensure_endpoint(prepared.host, **prepared.kwargs)
    assert "engine_endpoint" not in json.loads(prepared.claim_path.read_bytes())["managed_worker"]


def test_attached_endpoint_cannot_transfer_release_ownership(prepared, network, local_tls):
    p = prepared
    claim = json.loads(p.claim_path.read_bytes())
    claim["acquisition_mode"] = "attach"
    p.claim_path.write_bytes(encoded(claim))
    snapshot = dict(p.claim)
    snapshot.pop("managed_worker", None)
    snapshot["acquisition_mode"] = "attach"
    attestation = p.profile["endpoint_evidence"]["claim_attestation"]
    Path(attestation["claim_handle_path"]).write_bytes(encoded(snapshot))
    attestation["claim_handle_sha256"] = hashlib.sha256(encoded(snapshot)).hexdigest()
    p.files["/final-readiness.json"] = encoded(p.profile)
    p.host.reference.readiness_profile_hash = "sha256:" + hashlib.sha256(encoded(p.profile)).hexdigest()
    result = p.inspector.ensure_endpoint(p.host, **p.kwargs)
    assert result.lifecycle_owner.kind == "attached"
    assert result.lifecycle_owner.release_on_finish is False
    assert all(method == "GET" for method, _, _ in network["upstream"])


@pytest.mark.parametrize("unsafe", ["symlink", "parent-symlink", "mode", "hardlink"])
def test_private_snapshot_actual_open_guards(tmp_path, unsafe):
    target = private(tmp_path / "directory" / "snapshot", b"{}")
    if unsafe == "symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(target)
        target = alias
    elif unsafe == "parent-symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(target.parent, target_is_directory=True)
        target = alias / target.name
    elif unsafe == "mode":
        target.chmod(0o644)
    else:
        os.link(target, tmp_path / "hardlink")
    with pytest.raises(AttestationError):
        read_private_endpoint_file(str(target), deadline=time.monotonic() + 5)


@pytest.mark.parametrize("mutation", ["missing-evidence", "foreign-session", "receipt-bytes", "snapshot-bytes", "activation", "incarnation"])
def test_ensure_rejects_unbound_evidence_and_activation_before_send(prepared, network, local_tls, mutation):
    p = prepared
    if mutation == "missing-evidence":
        p.profile.pop("endpoint_evidence")
        p.files["/final-readiness.json"] = encoded(p.profile)
        p.host.reference.readiness_profile_hash = "sha256:" + hashlib.sha256(encoded(p.profile)).hexdigest()
    elif mutation == "foreign-session":
        p.host.session_receipt.url = "http://127.0.0.1:9999"
    elif mutation == "receipt-bytes":
        p.files["/engine/.astrid-engine/install.json"] += b" "
    elif mutation == "snapshot-bytes":
        path = Path(p.profile["endpoint_evidence"]["claim_attestation"]["claim_handle_path"])
        path.write_bytes(path.read_bytes() + b" ")
    else:
        claim = json.loads(p.claim_path.read_bytes())
        if mutation == "activation":
            claim["managed_worker"]["activation_qualification"]["activation_id"] = "foreign"
        else:
            claim["managed_worker"]["process_handle"]["birth_id"] = "foreign"
        p.claim_path.write_bytes(encoded(claim))
    with pytest.raises((AttestationError, EndpointOwnershipError)):
        p.inspector.ensure_endpoint(p.host, **p.kwargs)
    assert network["agent"] == []


def test_original_readiness_witness_is_required(prepared, network, local_tls):
    def refuse(_handle):
        raise EndpointOwnershipError("original activation refused")
    prepared.inspector.observe_ready = refuse
    with pytest.raises(EndpointOwnershipError):
        prepared.inspector.ensure_endpoint(prepared.host, **prepared.kwargs)
    assert network["agent"] == []


def test_support_admission_precedes_original_owner_or_secret_access(prepared, network, local_tls):
    from astrid.core.generation.engine_contracts import UnknownEngine, UnsupportedPlatform
    with pytest.raises(UnknownEngine):
        prepared.inspector.ensure_endpoint(prepared.host, **{**prepared.kwargs, "engine": "unknown-engine"})
    with pytest.raises(UnsupportedPlatform):
        prepared.inspector.ensure_endpoint(prepared.host, **{**prepared.kwargs, "platform": "windows-x86_64-cu130"})
    assert prepared.observed == [] and network["agent"] == []


def test_benign_claim_checkpoint_does_not_replace_frozen_snapshot(prepared, network, local_tls):
    p = prepared
    first = p.inspector.ensure_endpoint(p.host, **p.kwargs)
    claim = json.loads(p.claim_path.read_bytes())
    claim["last_observation"] = "ordinary bookkeeping"
    p.claim_path.write_bytes(encoded(claim))
    assert p.inspector.ensure_endpoint(p.host, **p.kwargs) == first
    assert len(network["upstream"]) == 1


def test_denied_gateway_operation_does_not_open_bearer(endpoint, network, local_tls, monkeypatch):
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        monkeypatch.setattr(transport, "_bearer", lambda: pytest.fail("denied operation read bearer"))
        with httpx.Client(trust_env=False) as client:
            assert client.post(transport.server_url + "/jobs", json={}).status_code == 403
    assert network["agent"] == []


@pytest.mark.parametrize("evidence_present", [True, False])
def test_checkout_host_session_preserves_binding_and_uses_transport(endpoint, network, local_tls,
                                                                   tmp_path, monkeypatch, evidence_present):
    import astrid.core.generation.backends.vibecomfy as backend
    from tests.core.generation.backends.test_checkout_server import _hc03_profile

    root = tmp_path / "host"
    root.mkdir()
    profile = _hc03_profile(root)
    session = profile["vibecomfy_session"]
    session["server_url"] = "http://127.0.0.1:8188"
    directory = Path(session["session_dir"])
    canonical = canonical_session(tmp_path)
    canonical.output.mkdir(parents=True)
    (directory / "config.json").write_bytes(encoded(canonical.config))
    session["config_digest"] = "sha256:" + hashlib.sha256(encoded(canonical.config)).hexdigest()
    (directory / "url").write_text(session["server_url"])
    marker = json.loads((directory / "launch.json").read_text())
    marker["url"] = session["server_url"]
    (directory / "launch.json").write_bytes(encoded(marker))
    profile["verified_facts"]["exact"]["port"] = 8188
    profile["verified_facts_digest"] = backend._canonical_sha256(profile["verified_facts"])
    if evidence_present:
        profile["endpoint_evidence"] = {"schema": "astrid.runpod.endpoint-evidence.v1", "engine": endpoint.engine,
                                        "platform": PLATFORM, "claim_operation_id": "original",
                                        "claim_attestation": asdict(endpoint.attestation)}
    monkeypatch.setattr(backend, "_verify_owned_vibe_session", lambda *_: None)
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        kwargs = dict(hc03_profile=profile, model_id="model", template_id="workflow", invocation_identity="task",
                      endpoint_transport=transport)
        if not evidence_present:
            with pytest.raises(AttestationError):
                backend.CheckoutServerAdapter.from_host_session(**kwargs)
            assert network["agent"] == []
            return
        adapter = backend.CheckoutServerAdapter.from_host_session(**kwargs)
        assert adapter._origin == session["server_url"]
        assert adapter._engine._origin == session["server_url"]
        assert adapter._http_origin == transport.server_url
        assert adapter._host_session["input_directory"] == str(canonical.input)
        assert adapter._engine._post("/api/free", {}) == {"ok": True}
        with pytest.raises(RuntimeError, match="original-owner reconciliation"):
            adapter._restart_owned_session()


# ---- Configured normal egress and the narrow no-route retry ------------------

def test_configured_egress_carries_only_claimed_origin_and_gateway_stays_local(endpoint, network, local_tls,
                                                                              monkeypatch):
    from astrid.core.generation.backends.vibecomfy import VibeComfyEngine
    # Even with every proxy variable pointing at the egress proxy, loopback
    # gateway clients owned by this route never go through it.
    for name in ("HTTP_PROXY", "ALL_PROXY", "http_proxy", "all_proxy"):
        monkeypatch.setenv(name, local_tls.url)
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        assert transport.object_info(deadline=time.monotonic() + 10) == network["objects"]
        engine = VibeComfyEngine("http://127.0.0.1:8188", endpoint_transport=transport)
        assert engine._post("/interrupt", {}) == {"ok": True}
        from runpod_lifecycle.https import HttpsPodTransport, PodTransportError
        local = HttpsPodTransport("pod123", TOKEN, base_url=transport.server_url, poll_interval=.01)
        try:
            assert local.http.trust_env is False
            with pytest.raises(PodTransportError, match="HTTP 403"):
                local.connect()  # the gateway itself denies /health, locally
        finally:
            local.disconnect()
        # Absolute-form/foreign targets cannot select another origin.
        with socket.create_connection(("127.0.0.1", int(transport.server_url.rsplit(":", 1)[1]))) as raw:
            raw.sendall(b"GET http://foreign.example/object_info HTTP/1.1\r\nHost: foreign.example\r\n\r\n")
            raw.settimeout(5)
            assert raw.recv(64).split(b"\r\n", 1)[0].split(b" ")[1] == b"403"
    assert_only_claimed_egress(network)
    assert [path for path, _ in network["agent"]] == ["/engines/single/object_info", "/engines/single/interrupt"]


def _script(network, method, suffix, actions):
    queue = list(actions)
    network["provider"] = lambda command, path: (
        queue.pop(0) if command == method and path.endswith(suffix) and queue else None)


@pytest.mark.parametrize("outcomes,status,delivered", [
    (["no-route", "no-route"], 200, 1),
    (["json404"], 404, 0),
    (["502"], 502, 0),
    (["drop"], 502, 0),
])
def test_gateway_retries_only_provider_empty_404_unchanged(endpoint, network, local_tls, outcomes, status, delivered):
    _script(network, "POST", "/prompt", outcomes)
    payload = b'{"prompt":{"1":{"class_type":"Example","inputs":{}}},"client_id":"fixed"}'
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000,
                                 deadline=time.monotonic() + 20) as transport:
        with httpx.Client(trust_env=False) as client:
            reply = client.post(transport.server_url + "/prompt", content=payload,
                                headers={"Content-Type": "application/json"})
            assert reply.status_code == status
            if status == 404:
                assert reply.json() == {"error": "not found"}
            # One accepted prompt per attachment, whatever happened.
            assert client.post(transport.server_url + "/prompt", content=payload).status_code == 409
    prompt_attempts = [a for a in network["attempts"] if a[1].endswith("/prompt")]
    expected = outcomes + (["delivered"] if delivered else [])
    assert [a[4] for a in prompt_attempts] == expected
    assert {(a[0], a[1], a[2]) for a in prompt_attempts} == {("POST", "/engines/single/prompt", "Bearer " + TOKEN)}
    assert all(a[3] == payload for a in prompt_attempts if a[3] is not None)
    assert len([u for u in network["upstream"] if u[1] == "/prompt"]) == delivered
    if delivered:
        assert network["prompt"] == json.loads(payload)
    assert_only_claimed_egress(network)


def test_object_info_rides_out_no_route_within_its_deadline(endpoint, network, local_tls):
    _script(network, "GET", "/object_info", ["no-route", "no-route"])
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000) as transport:
        assert transport.object_info(deadline=time.monotonic() + 10) == network["objects"]
    assert [a[4] for a in network["attempts"]] == ["no-route", "no-route", "delivered"]


def test_no_route_retry_stops_when_local_caller_abandons(endpoint, network, local_tls):
    network["provider"] = lambda command, path: "no-route" if path.endswith("/queue") else None
    with EngineEndpointTransport(endpoint, pod_id="pod123", agent_port=8000,
                                 deadline=time.monotonic() + 30) as transport:
        with httpx.Client(trust_env=False, timeout=1.2) as client:
            with pytest.raises(httpx.TimeoutException):
                client.post(transport.server_url + "/queue", json={"delete": ["prompt-1"]})
        time.sleep(1.5)
        settled = len(network["attempts"])
        time.sleep(1.0)
        assert len(network["attempts"]) == settled
    assert 1 <= settled <= 5
    assert not [u for u in network["upstream"] if u[1] == "/queue"]
