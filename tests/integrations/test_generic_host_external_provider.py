"""B9.3/B9.4 coverage for external provider packs at the host boundary."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sysconfig
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import (
    GenericPackHost,
    HostError,
    RuntimeProtocolClient,
)
from astrid.core.execution.network_broker import ObservableNetworkBroker
from astrid.pack_api import VERSION as PACK_API_VERSION
from astrid_runtime_client import WorkspaceClient

pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)


def _write_v3_pack(
    pack_root: Path, pack_id: str, name: str, actions: dict, *,
    version: str = "1.0.0", **header,
) -> None:
    """Write a schema v3 fixture pack whose provider actions are declared in pack.yaml.

    Each action is a v3 command action; ``kind: command`` projects to an
    ``external`` executor, so ``metadata.adapter_family``/``network_policy``
    and ``secrets`` keep the provider contract each test asserts.
    """
    pack_root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 3, "id": pack_id, "name": name, "version": version,
        "pack_api": "%d.%d" % PACK_API_VERSION,
        "status": "experimental", "domain": "integration", "stability": "experimental",
        "support": "community", "visibility": "visible",
        "description": f"{name} test fixture pack.",
        "permissions": [
            {"id": "network", "reason": "Provider fixture actions reach a local or declared endpoint."},
            {"id": "subprocess", "reason": "Provider fixture actions run as command subprocesses."},
        ],
        **header,
        "actions": {
            local_id: {"description": f"{action.get('name', local_id)} fixture action.", "inputs": [], **action}
            for local_id, action in actions.items()
        },
    }
    # JSON is a YAML subset: the loader parses this pack.yaml unchanged.
    (pack_root / "pack.yaml").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


_UPLOAD_BINDING = dict(run_id="run-1", task_id="task-1", attempt_id="attempt-1", lease_id="lease-1", fence=1)


def _claimed_client(ingest_object=None) -> RuntimeProtocolClient:
    """A Runtime client with only the state upload needs: its executor and the claimed attempt's epoch."""
    client = object.__new__(RuntimeProtocolClient)
    client.generated = SimpleNamespace(ingest_object=ingest_object, health=lambda: SimpleNamespace(runtime_epoch=7))
    client.executor_id = "executor-1"
    client._attempt_runtime_epochs = {"attempt-1": 7}
    return client


@pytest.mark.parametrize("project_id", [None, "existing-project"])
def test_output_publication_uploads_through_the_attempt_bound_canonical_cas(
    tmp_path: Path, project_id: str | None,
) -> None:
    calls = []
    def ingest_object(data, **kwargs):
        calls.append((data, kwargs))
        return SimpleNamespace(digest="sha256:" + hashlib.sha256(data).hexdigest(), size=len(data))

    client = _claimed_client(ingest_object)
    client.INLINE_SETTLEMENT_OUTPUTS = False
    host = GenericPackHost(pack_roots=[], client=client)
    path = tmp_path / "results.json"
    path.write_text('{"results": []}')
    outputs = host._upload_outputs(
        [{"name": "results", "path": str(path), "artifact_type": "application/json"}],
        project_id=project_id, **_UPLOAD_BINDING,
    )
    assert len(calls) == 1
    assert calls[0][0] == path.read_bytes()
    kwargs = calls[0][1]
    assert kwargs["media_type"] == "application/json" and kwargs["filename"] == "results.json"
    # The project travels inside the provenance binding, never as a separate ingest argument.
    assert "project_id" not in kwargs
    assert kwargs["upload_binding"] == {
        "project_id": project_id, "run_id": "run-1", "task_id": "task-1", "attempt_id": "attempt-1",
        "executor_id": "executor-1", "lease_id": "lease-1", "fence": 1, "runtime_epoch": 7,
        "output_key": "results", "output_port": "results", "filename": "results.json",
        "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size, "media_type": "application/json",
    }
    assert outputs == [{
        "name": "results", "kind": "object", "filename": "results.json", "media_type": "application/json",
        "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }]


@pytest.mark.parametrize("project_id", [None, "existing-project"])
def test_output_publication_inlines_small_outputs_when_the_attempt_has_no_upload_binding(
    tmp_path: Path, project_id: str | None,
) -> None:
    import base64
    client = _claimed_client(lambda *_args, **_kwargs: pytest.fail("an unbound attempt cannot upload"))
    client.INLINE_SETTLEMENT_OUTPUTS = True
    host = GenericPackHost(pack_roots=[], client=client)
    path = tmp_path / "results.json"
    path.write_text('{"results": []}')
    outputs = host._upload_outputs(
        [{"name": "results", "path": str(path), "artifact_type": "application/json"}],
        project_id=project_id,
    )
    assert base64.b64decode(outputs[0].pop("data_base64")) == path.read_bytes()
    assert outputs == [{
        "name": "results", "kind": "object", "filename": "results.json", "media_type": "application/json",
        "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }]


@pytest.mark.parametrize("project_id", ["", "  ", 7])
def test_output_publication_rejects_malformed_project(tmp_path: Path, project_id) -> None:
    client = _claimed_client()
    with pytest.raises(HostError, match="non-empty string or None"):
        client.upload_object(
            tmp_path / "unused", project_id=project_id, media_type="text/plain", filename="unused",
            output_key="results", output_port="results", **_UPLOAD_BINDING,
        )


class _TaskRuntime:
    """Canonical in-memory runtime surface for isolated provider-host tests."""

    def heartbeat(self, *_args, **_kwargs):
        return {"ok": True}

    def task(self, task_id):
        return {"task": {"id": task_id, "status": "running"}}

    def settle(self, *_args, **_kwargs):
        return {"state": "succeeded"}

    def fail(self, *_args, **_kwargs):
        return {"ok": True}

    def upload_object(self, path, **_kwargs):
        payload = Path(path).read_bytes()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        return SimpleNamespace(object_id=digest, digest=digest, size=len(payload))


def _run_provider(host: GenericPackHost, task: dict, *, lease_token: str = "fixture"):
    """Exercise the explicit provider grant request/consume seam."""
    if host.client is None:
        host.client = _TaskRuntime()
    task_data = task.setdefault("task", task)
    task_data.setdefault("attempt_id", f"attempt-{task_data['id']}")
    task_data.setdefault("fence", 1)
    task_data.setdefault("project_id", "fixture-project")
    raw_spec = task_data.get("spec", {})
    if isinstance(raw_spec, dict) and "spec" not in raw_spec:
        task_data["spec"] = {
            "input_object_ids": [],
            "schema_version": "1",
            "spec": raw_spec,
        }
    grant = host.request_provider_route_grant(task)
    return host.run_task(task, lease_token=lease_token, provider_route_grant=grant)


def test_external_pack_command_imports_from_its_admitted_pack_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_runtime
) -> None:
    """A command-pack provider must execute through the generic host.

    This is deliberately a local provider fixture: it proves the same
    subprocess/import boundary used by Hivemind without contacting a provider
    or carrying credentials.  The package lives beside its pack root, so the
    test fails if the host forgets to propagate the external pack import root.
    """
    pack_root = tmp_path / "fixture_provider"
    pack_root.mkdir(parents=True)
    (pack_root / "__init__.py").write_text("\n", encoding="utf-8")
    (pack_root / "emit.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "import sys\n"
        "from urllib.request import urlopen\n"
        "url = os.environ.get('FIXTURE_PROVIDER_URL')\n"
        "body = urlopen(url, timeout=2).read().decode('utf-8') if url else 'provider-fixture-response'\n"
        "Path(sys.argv[1]).write_text(body, encoding='utf-8')\n",
        encoding="utf-8",
    )
    _write_v3_pack(pack_root, "fixture_provider", "Fixture Provider", {
        "echo": {
            "name": "Fixture Provider Echo",
            "version": "1.0",
            "invocation": {"kind": "command", "command": {"argv": [
                "{python_exec}",
                "-m",
                # v3 selected pack commands import the admitted pack root
                # under its canonical astrid.packs.<pack_id> namespace.
                "astrid.packs.fixture_provider.emit",
                "{out}/response.txt",
            ]}},
            "outputs": [
                {
                    "name": "response",
                    "type": "file",
                    "path_template": "{out}/response.txt",
                }
            ],
            "isolation": {
                "mode": "subprocess",
                "network": True,
                "env_passthrough": ["FIXTURE_PROVIDER_URL"],
            },
            "metadata": {
                "adapter_family": "provider",
                "resource_keys": ["provider"],
                "network_policy": {
                    "allowed_protocols": ["dns", "tcp"],
                    "allowed_destinations": ["127.0.0.1"],
                    "broker": {"host_managed": True},
                },
            },
        },
    }, capabilities=["echo"])

    class ProviderHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            body = b"provider-http-response"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "FIXTURE_PROVIDER_URL",
        f"http://127.0.0.1:{server.server_port}/response",
    )
    daemon = real_runtime()
    try:
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        generated.handshake(
            "provider-fixture", "0.1.0", ["projects:read", "worker:execute"]
        )
        host = GenericPackHost(
            pack_roots=[pack_root],
            attempt_root=tmp_path / "attempt",
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            executor_id="astrid-pack-host",
        )
        record = host.discover()[0]
        assert record.definition.metadata["source_pack"] == "fixture_provider"
        assert Path(record.definition.metadata["pack_root"]) == pack_root
        host.preflight()
        assert record.adapter.family == "provider"
        assert host.capabilities[record.id].ready

        host.register()
        project = generated.create_project(
            "Provider fixture", slug="provider-http", idempotency_key="provider-http-project"
        )
        task = generated.admit_task(
            capability_id=record.id,
            capability_digest=record.capability_digest,
            input_object_ids=[],
            project_id=project.project_id,
            idempotency_key="provider-task",
        )
        settled = host.run(once=True)
        assert len(settled) == 1 and settled[0].state == "succeeded"
        assert generated.get_task(task.task_id).state == "succeeded"
    finally:
        daemon.stop()
        server.shutdown()
        server.server_close()
    assert (tmp_path / "attempt" / "outputs" / "response.txt").read_text(encoding="utf-8") == "provider-http-response"


def test_hivemind_without_clean_pinned_source_is_optional_unavailable(tmp_path: Path) -> None:
    """B9.3 must expose an unavailable Hivemind row, never silently advertise it."""
    pack_root = tmp_path / "hivemind"
    (pack_root / "docs").mkdir(parents=True)
    (pack_root / "docs" / "SKILL.md").write_text(
        "---\nname: hivemind\ndescription: Hivemind fixture.\n---\n# Hivemind fixture\n", encoding="utf-8")
    _write_v3_pack(pack_root, "hivemind", "Hivemind", {
        "search": {
            "name": "Hivemind Search",
            "version": "2.0",
            "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "pass"]}},
            "outputs": [],
            "secrets": ["HIVEMIND_ANON_KEY"],
            "isolation": {"mode": "subprocess", "network": True},
            "metadata": {"adapter_family": "provider", "required_env": ["HIVEMIND_API_URL"]},
        },
    }, version="2.0.0", capabilities=["search_corpus"],
        documentation={"kind": "skill", "path": "docs/SKILL.md"})
    host = GenericPackHost(pack_roots=[pack_root])
    record = host.discover()[0]
    assert record.definition.metadata["source_pack"] == "hivemind"
    host.preflight()
    assert host.capabilities[record.id].ready is False
    assert host.capabilities[record.id].preflight["pack_source"]["ok"] is False
    assert "not a Git checkout" in host.capabilities[record.id].preflight["pack_source"]["reason"]


def test_external_provider_without_key_is_admitted_by_runtime(tmp_path: Path, real_runtime) -> None:
    """A declared key never blocks admission or an unauthenticated provider call."""
    class ProviderHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            body = b"provider-ready"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    pack_root = tmp_path / "provider"
    _write_v3_pack(pack_root, "provider", "Provider", {
        "fetch": {
            "name": "Provider Fetch",
            "version": "1.0",
            "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", f"from urllib.request import urlopen; urlopen('http://127.0.0.1:{server.server_port}/ready', timeout=2).read()"]}},
            "outputs": [],
            "secrets": ["PROVIDER_KEY"],
            "isolation": {"mode": "subprocess", "network": True},
            "metadata": {
                "adapter_family": "provider",
                "required_env": ["PROVIDER_KEY"],
                "network_policy": {
                    "allowed_protocols": ["dns", "tcp"],
                    "allowed_destinations": ["127.0.0.1"],
                    "broker": {"host_managed": True},
                },
            },
        },
    })

    daemon = real_runtime()
    try:
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        generated.handshake("provider-fixture", "0.1.0", ["projects:read", "worker:execute"])
        host = GenericPackHost(
            pack_roots=[pack_root],
            attempt_root=tmp_path / "attempt",
            credential_source={},
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
            executor_id="astrid-pack-host",
        )
        record = host.discover()[0]
        registration = host.register()
        assert host.capabilities[record.id].ready is True
        assert registration["capabilities"][0]["ready"] is True
        assert host.capabilities[record.id].preflight["credentials"] == {
            "ok": True, "missing": ["PROVIDER_KEY"],
        }
        project = generated.create_project("Provider fixture", slug="provider-fixture", idempotency_key="provider-project")
        capability_page, capability_cursor = generated.list_capabilities()
        assert capability_cursor is None
        capability = next(item for item in capability_page if item.capability_id == record.id)
        assert capability.status == "ready"
        task = generated.admit_task(
            capability_id=record.id,
            capability_digest=record.capability_digest,
            input_object_ids=[],
            project_id=project.project_id,
            idempotency_key="provider-ready-task",
        )
        settled = host.run(once=True)
        assert len(settled) == 1 and settled[0].state == "succeeded"
        assert generated.get_task(task.task_id).state == "succeeded"
    finally:
        daemon.stop()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("provider_env", ["FAL_KEY", "GIPHY_API_KEY", "OPENAI_API_KEY"])
def test_provider_credentials_are_manifest_scoped_and_redacted(tmp_path: Path, provider_env: str) -> None:
    """FAL/GIPHY/OpenAI-shaped provider children see only declared secrets."""
    pack_root = tmp_path / "credential_provider"
    _write_v3_pack(pack_root, "credential_provider", "Credential Provider", {
        "echo": {
            "name": "Credential Echo", "version": "1.0",
            "invocation": {"kind": "command", "command": {"argv": [
                "{python_exec}", "-c",
                "import os,sys; print(os.getenv('%s'), os.getenv('FAL_KEY'), os.getenv('OPENAI_API_KEY'), file=sys.stderr); raise SystemExit(2)" % provider_env,
            ]}},
            "outputs": [],
            "secrets": [provider_env],
            "isolation": {"mode": "subprocess", "network": True},
            "metadata": {"adapter_family": "provider", "network_policy": {
                "allowed_protocols": ["dns", "tcp"], "allowed_destinations": [],
                "allow_redirects": False, "proxy": None,
                "broker": {"host_managed": True},
            }},
        },
    })
    credential_source = {"FAL_KEY": "fal-ambient-secret", "GIPHY_API_KEY": "giphy-ambient-secret", "OPENAI_API_KEY": "openai-ambient-secret"}
    credential_source[provider_env] = "declared-fixture-secret"
    host = GenericPackHost(
        pack_roots=[pack_root], attempt_root=tmp_path / "attempt",
        credential_source=credential_source,
    )
    record = host.discover()[0]
    host.preflight()
    assert host.capabilities[record.id].ready
    with pytest.raises(HostError) as caught:
        _run_provider(host, {"task": {"id": "credential-task", "capability": record.id, "spec": {"inputs": {}}}})
    assert "declared-fixture-secret" not in str(caught.value)
    assert "ambient-secret" not in str(caught.value)
    assert "<redacted>" in str(caught.value)


def test_provider_network_policy_records_hermetic_dns_tcp_and_denies_redirect(
    tmp_path: Path,
) -> None:
    """A local provider fixture proves bounded network enforcement/observability."""
    class ProviderHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            self.send_response(302 if self.path == "/" else 200)
            if self.path == "/":
                self.send_header("Location", "http://127.0.0.1:%d/redirected" % server.server_port)
            self.end_headers()
            self.wfile.write(b"fixture")

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        pack_root = tmp_path / "network_provider"
        url = "http://127.0.0.1:%d/" % server.server_port
        _write_v3_pack(pack_root, "network_provider", "Network Provider", {
            "fetch": {
                "name": "Network Fetch", "version": "1.0",
                "invocation": {"kind": "command", "command": {"argv": [
                    "{python_exec}", "-c",
                    "from pathlib import Path; from urllib.request import urlopen; Path('{out}/body').write_bytes(urlopen('%s').read())" % url,
                ]}},
                "outputs": [{"name": "body", "type": "file", "path_template": "{out}/body"}],
                "isolation": {"mode": "subprocess", "network": True},
                "metadata": {"adapter_family": "provider", "network_policy": {
                    "allowed_protocols": ["dns", "tcp"],
                    "allowed_destinations": ["127.0.0.1:%d" % server.server_port],
                    "allow_redirects": False,
                    "broker": {"host_managed": True},
                }},
            },
        })
        host = GenericPackHost(pack_roots=[pack_root], attempt_root=tmp_path / "attempt")
        record = host.discover()[0]
        host.preflight()
        with pytest.raises(HostError, match="network policy denied redirect"):
            _run_provider(host, {"task": {"id": "network-task", "capability": record.id, "spec": {"inputs": {}}}})
        evidence = json.loads((tmp_path / "attempt" / "network-evidence.json").read_text(encoding="utf-8"))
        assert {event["kind"] for event in evidence["events"]} >= {"dns", "tcp", "redirect"}
        assert any(event["kind"] == "redirect" and not event["allowed"] for event in evidence["events"])
        assert evidence["limitations"]
    finally:
        server.shutdown()
        server.server_close()


def test_provider_network_policy_retires_unbrokered_quic_over_udp_fixture(tmp_path: Path) -> None:
    """UDP providers are unavailable until the host broker supports datagrams."""
    received: list[bytes] = []
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(("127.0.0.1", 0))
    udp.settimeout(2)

    def receive() -> None:
        try:
            data, _ = udp.recvfrom(1024)
            received.append(data)
        except socket.timeout:
            pass

    thread = threading.Thread(target=receive, daemon=True)
    thread.start()
    try:
        pack_root = tmp_path / "quic_provider"
        port = udp.getsockname()[1]
        _write_v3_pack(pack_root, "quic_provider", "QUIC Provider", {
            "fetch": {
                "name": "QUIC UDP Fixture", "version": "1.0",
                "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.sendto(b'quic-fixture', ('127.0.0.1', %d))" % port]}},
                "outputs": [], "isolation": {"mode": "subprocess", "network": True},
                "metadata": {"adapter_family": "provider", "network_policy": {
                    "allowed_protocols": ["dns", "udp"], "allowed_destinations": ["127.0.0.1:%d" % port],
                }},
            },
        })
        host = GenericPackHost(pack_roots=[pack_root], attempt_root=tmp_path / "attempt")
        record = host.discover()[0]
        host.preflight()
        assert host.capabilities[record.id].ready is False
        assert "host-managed broker" in host.capabilities[record.id].preflight["network"]["reason"]
    finally:
        udp.close()


def test_provider_network_policy_retires_unbrokered_aioquic_handshake(tmp_path: Path) -> None:
    """A QUIC provider is unavailable until a host-owned datagram broker exists."""
    package_root = tmp_path / "quic_provider"
    package_root.mkdir()
    (package_root / "__init__.py").write_text("\n", encoding="utf-8")
    (package_root / "run.py").write_text(textwrap.dedent("""
        import asyncio, datetime, ipaddress, sys
        from pathlib import Path
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from aioquic.asyncio import connect, serve
        from aioquic.quic.configuration import QuicConfiguration

        def certificate():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                    .public_key(key.public_key()).serial_number(x509.random_serial_number())
                    .not_valid_before(now - datetime.timedelta(days=1))
                    .not_valid_after(now + datetime.timedelta(days=1))
                    .add_extension(x509.SubjectAlternativeName([
                        x509.DNSName('localhost'),
                        x509.IPAddress(ipaddress.ip_address('127.0.0.1')),
                    ]), critical=False).sign(key, hashes.SHA256()))
            cert_path = Path(sys.argv[2] + '.crt')
            key_path = Path(sys.argv[2] + '.key')
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
            return cert_path, key_path

        async def echo(reader, writer):
            writer.write(await reader.read(1024))
            await writer.drain()
            writer.close()

        def stream_handler(reader, writer):
            asyncio.create_task(echo(reader, writer))

        async def main():
            cert_path, key_path = certificate()
            server_config = QuicConfiguration(is_client=False, alpn_protocols=['hq-29'])
            server_config.load_cert_chain(str(cert_path), str(key_path))
            server = await serve('127.0.0.1', 0, configuration=server_config,
                                 stream_handler=stream_handler)
            port = server._transport.get_extra_info('sockname')[1]
            client_config = QuicConfiguration(is_client=True, alpn_protocols=['hq-29'])
            client_config.verify_mode = 0
            message = b'astrid-real-quic-handshake'
            async with connect('127.0.0.1', port, configuration=client_config) as protocol:
                reader, writer = await protocol.create_stream()
                writer.write(message)
                await writer.drain()
                response = await asyncio.wait_for(reader.read(len(message)), 5)
                writer.close()
            server.close()
            if response != message:
                raise RuntimeError('QUIC stream echo did not complete')
            Path(sys.argv[1]).write_text('quic handshake complete', encoding='utf-8')

        asyncio.run(main())
    """), encoding="utf-8")
    pack_root = tmp_path / "quic_pack"
    _write_v3_pack(pack_root, "quic_pack", "QUIC Pack", {
        "handshake": {
            "name": "QUIC Handshake", "version": "1.0",
            "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-m", "quic_provider.run", "{out}/proof.txt", str(tmp_path / "cert")], "env": {"PYTHONPATH": os.pathsep.join((sysconfig.get_paths()["purelib"], str(Path(__file__).resolve().parents[2])))}}},
            "outputs": [{"name": "proof", "type": "file", "path_template": "{out}/proof.txt"}],
            "isolation": {"mode": "subprocess", "network": True},
            "metadata": {"adapter_family": "provider", "network_policy": {
                "allowed_protocols": ["dns", "udp"], "allowed_destinations": ["127.0.0.1"],
                "allow_redirects": False, "proxy": None,
            }},
        },
    })
    host = GenericPackHost(pack_roots=[pack_root], attempt_root=tmp_path / "attempt")
    record = host.discover()[0]
    host.preflight()
    assert host.capabilities[record.id].ready is False
    assert "host-managed broker" in host.capabilities[record.id].preflight["network"]["reason"]


def test_native_network_command_is_unready_without_enforceable_observable_gateway(tmp_path: Path) -> None:
    pack_root = tmp_path / "native_provider"
    _write_v3_pack(pack_root, "native_provider", "Native Provider", {
        "fetch": {
            "name": "Native Fetch", "version": "1.0",
            "invocation": {"kind": "command", "command": {"argv": ["curl", "https://example.invalid/"]}},
            "outputs": [],
            "isolation": {"mode": "subprocess", "network": True},
            "metadata": {"adapter_family": "provider", "native": True,
                         "network_policy": {"allowed_protocols": ["tcp"], "allowed_destinations": ["example.invalid:443"]}},
        },
    })
    host = GenericPackHost(pack_roots=[pack_root], attempt_root=tmp_path / "attempt")
    record = host.discover()[0]
    host.preflight()
    assert host.capabilities[record.id].ready is False
    assert "host-managed broker" in host.capabilities[record.id].preflight["network"]["reason"]
    host.client = _TaskRuntime()
    with pytest.raises(HostError, match="capability .* unavailable"):
        host.run_task(
            {
                "task": {
                    "id": "native-task",
                    "capability": record.id,
                    "attempt_id": "attempt-native-task",
                    "fence": 1,
                    "spec": {"input_object_ids": [], "spec": {"inputs": {}}},
                }
            },
            lease_token="fixture",
        )


def test_clean_pinned_hivemind_pack_publishes_through_real_runtime(tmp_path: Path, real_runtime) -> None:
    """A committed local Hivemind-style pack is admitted by digest and settles via CAS."""
    checkout = tmp_path / "hivemind-checkout"
    pack_root = checkout / "packs" / "hivemind"
    (pack_root / "docs").mkdir(parents=True)
    (pack_root / "docs" / "SKILL.md").write_text(
        "---\nname: hivemind\ndescription: Hivemind fixture.\n---\n# Hivemind fixture\n", encoding="utf-8")
    _write_v3_pack(pack_root, "hivemind", "Hivemind", {
        "search": {
            "name": "Hivemind Search", "version": "2.0",
            "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "import json; from pathlib import Path; Path('{out}/result.json').write_text(json.dumps({'hits': []}), encoding='utf-8')"]}},
            "outputs": [{"name": "result", "type": "file", "path_template": "{out}/result.json", "artifact_type": "application/json"}],
            "secrets": ["HIVEMIND_ANON_KEY"],
            "isolation": {"mode": "subprocess", "network": True},
            "metadata": {"adapter_family": "provider", "network_policy": {"allowed_protocols": ["dns", "tcp"], "allowed_destinations": [], "broker": {"host_managed": True}}},
        },
    }, version="2.0.0", capabilities=["search_corpus"],
        documentation={"kind": "skill", "path": "docs/SKILL.md"})
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(checkout), "config", "user.name", "Fixture"], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "packs"], check=True)
    subprocess.run(["git", "-C", str(checkout), "commit", "-qm", "pin hivemind fixture"], check=True)
    host = GenericPackHost(pack_roots=[pack_root], attempt_root=tmp_path / "attempt", credential_source={"HIVEMIND_ANON_KEY": "fixture-key"})
    record = host.discover()[0]
    host.preflight()
    assert host.capabilities[record.id].preflight["pack_source"]["ok"]
    revision = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    assert host.capabilities[record.id].preflight["pack_source"]["revision"] == revision

    daemon = real_runtime()
    try:
        generated = WorkspaceClient(daemon.endpoint, daemon.token)
        generated.handshake("hivemind-fixture", "0.1.0", ["projects:read", "worker:execute"])
        runtime_host = GenericPackHost(
            pack_roots=[pack_root], attempt_root=tmp_path / "attempt-runtime",
            credential_source={"HIVEMIND_ANON_KEY": "fixture-key"},
            client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token), executor_id="astrid-pack-host",
        )
        registration = runtime_host.register()
        capability = next(
            item
            for item in registration["registration"].capabilities
            if item.capability_id == record.id
        )
        assert capability.definition_digest == record.capability_digest
        project = generated.create_project(
            "Hivemind fixture", slug="hivemind-fixture", idempotency_key="hivemind-fixture-project"
        )
        task = generated.admit_task(capability_id=record.id, capability_digest=record.capability_digest, input_object_ids=[], project_id=project.project_id, idempotency_key="hivemind-fixture-task")
        settled = runtime_host.run(once=True)
        assert len(settled) == 1 and settled[0].state == "succeeded"
        payload = b'{"hits": []}'
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        assert generated.get_object(digest).data == payload
        assert generated.get_task(task.task_id).state == "succeeded"
    finally:
        daemon.stop()


def test_python_provider_cannot_spawn_unobserved_native_network_descendant(tmp_path: Path) -> None:
    """A curl/yt-dlp-shaped escape hatch fails closed inside the child."""
    pack_root = tmp_path / "native_escape_provider"
    _write_v3_pack(pack_root, "native_escape_provider", "Native Escape Provider", {
        "fetch": {
            "name": "Native Escape", "version": "1.0",
            "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", "-c", "import subprocess; subprocess.run(['curl', 'https://example.invalid'], check=False)"]}},
            "outputs": [], "isolation": {"mode": "subprocess", "network": True},
            "metadata": {"adapter_family": "provider", "network_policy": {
                "allowed_protocols": ["dns", "tcp"], "allowed_destinations": ["127.0.0.1:9"],
                "allow_redirects": False, "proxy": None,
                "broker": {"host_managed": True},
            }},
        },
    })
    host = GenericPackHost(pack_roots=[pack_root], attempt_root=tmp_path / "attempt")
    record = host.discover()[0]
    host.preflight()
    assert host.capabilities[record.id].ready
    with pytest.raises(HostError, match="exited"):
        _run_provider(host, {"task": {"id": "native-escape", "capability": record.id, "spec": {"inputs": {}}}})
    evidence = json.loads((tmp_path / "attempt" / "network-evidence.json").read_text(encoding="utf-8"))
    assert any(event["kind"] == "native_descendant" and not event["allowed"] for event in evidence["events"])


def test_live_observable_proxy_handshake_route_and_bypass_rejection(tmp_path: Path) -> None:
    """A provider must use the live broker and cannot bypass its route."""
    class ProviderHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib handler API
            body = b"brokered-response"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        port = upstream.server_port
        pack_root = tmp_path / "broker_provider"
        # v3 argv entries are single-line strings, so the multi-line child is a
        # script inside the pack root; the host substitutes {out} as argv[1].
        pack_root.mkdir(parents=True)
        (pack_root / "fetch.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "from urllib.request import urlopen, build_opener, ProxyHandler\n"
            f"body = urlopen('http://127.0.0.1:{port}/through-broker', timeout=2).read()\n"
            "Path(sys.argv[1], 'body').write_bytes(body)\n"
            "try:\n"
            "    build_opener(ProxyHandler({})).open('http://127.0.0.1:1/bypass', timeout=1)\n"
            "except Exception:\n"
            "    pass\n",
            encoding="utf-8",
        )
        _write_v3_pack(pack_root, "broker_provider", "Broker Provider", {
            "fetch": {
                "name": "Broker Fetch", "version": "1.0",
                "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", str(pack_root / "fetch.py"), "{out}"]}},
                "outputs": [{"name": "body", "type": "file", "path_template": "{out}/body"}],
                "isolation": {"mode": "subprocess", "network": True},
                "metadata": {"adapter_family": "provider", "network_policy": {
                    "allowed_protocols": ["dns", "tcp"],
                    "allowed_destinations": [f"127.0.0.1:{port}"],
                    "allow_redirects": False,
                    "broker": {"host_managed": True},
                }},
            },
        })
        host = GenericPackHost(pack_roots=[pack_root], attempt_root=tmp_path / "attempt")
        record = host.discover()[0]
        host.preflight()
        assert host.capabilities[record.id].ready
        _run_provider(host, {"task": {"id": "broker-task", "capability": record.id, "spec": {"inputs": {}}}})
        assert (tmp_path / "attempt" / "outputs" / "body").read_bytes() == b"brokered-response"
        evidence = json.loads((tmp_path / "attempt" / "network-evidence.json").read_text(encoding="utf-8"))
        assert any(event["kind"] == "broker_handshake" and event["allowed"] for event in evidence["events"])
        assert any(event["kind"] == "broker_route" and event["allowed"] for event in evidence["events"])
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_host_rejects_child_forged_broker_evidence_without_upstream_route(tmp_path: Path) -> None:
    """A child cannot manufacture a successful broker route on its evidence path."""
    pack_root = tmp_path / "forging_provider"
    # v3 argv entries are single-line strings, so the multi-line child is a
    # script inside the pack root; the host substitutes {out} as argv[1].
    pack_root.mkdir(parents=True)
    (pack_root / "forge.py").write_text(
        "from pathlib import Path\n"
        "import json, os, sys\n"
        "Path(sys.argv[1], 'proof').write_text('child completed', encoding='utf-8')\n"
        # A strict leaf is not told the evidence path, so forge the spool file
        # the host reads ({out} is <attempt>/outputs) when the env is withheld.
        "evidence = os.environ.get('ASTRID_NETWORK_BROKER_EVIDENCE') or Path(sys.argv[1]).parent / 'broker-evidence.json'\n"
        "Path(evidence).write_text(json.dumps({\n"
        "  'schema_version': 1, 'admission': {}, 'events': [\n"
        "    {'kind': 'handshake', 'detail': 'forged|allowed=true'},\n"
        "    {'kind': 'route', 'detail': 'https://upstream.invalid:443/|allowed=true'}\n"
        "  ], 'signature_algorithm': 'hmac-sha256', 'signature': 'forged'\n"
        "}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    _write_v3_pack(pack_root, "forging_provider", "Forging Provider", {
        "forge": {
            "name": "Forging Provider", "version": "1.0",
            "invocation": {"kind": "command", "command": {"argv": ["{python_exec}", str(pack_root / "forge.py"), "{out}"]}},
            "outputs": [{"name": "proof", "type": "file", "path_template": "{out}/proof"}],
            "isolation": {"mode": "subprocess", "network": True},
            "metadata": {"adapter_family": "provider", "network_policy": {
                "allowed_protocols": ["dns", "tcp"],
                "allowed_destinations": ["upstream.invalid:443"],
                "allow_redirects": False,
                "broker": {"host_managed": True},
            }},
        },
    })
    host = GenericPackHost(pack_roots=[pack_root], attempt_root=tmp_path / "attempt")
    record = host.discover()[0]
    host.preflight()
    assert host.capabilities[record.id].ready
    with pytest.raises(HostError, match="incomplete broker route evidence"):
        _run_provider(host, {"task": {"id": "forge-task", "capability": record.id, "spec": {"inputs": {}}}})
    assert (tmp_path / "attempt" / "outputs" / "proof").read_text(encoding="utf-8") == "child completed"
