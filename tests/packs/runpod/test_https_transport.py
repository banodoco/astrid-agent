"""Real HTTP coverage for the H3 and prepared-worker transport boundaries."""
from __future__ import annotations

import asyncio
import io
import zipfile
import json
import sys
import threading
from pathlib import Path

import pytest

from runpod_lifecycle.config import RunPodConfig
from runpod_lifecycle.https import HttpsPodTransport, read_token_file
from runpod_lifecycle.pod import Pod
from runpod_lifecycle.pod_agent import AgentServer
from astrid.packs.runpod import h3_release as builder
from astrid.packs.runpod.worker_staging import RunPodPreparationTransport
from astrid.packs.runpod.worker_process import RunPodWorkerProcessTransport
from scripts import claim_runpod_5090_backup as claim

TOKEN = "local-test-only-" + "x" * 40


@pytest.fixture
def http_pod(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    server = AgentServer(("127.0.0.1", 0), TOKEN, tmp_path / "agent")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    pod = Pod("localpod", "test", RunPodConfig(api_key="test", transport="https", https_token=TOKEN))
    def transport():
        return HttpsPodTransport("localpod", TOKEN, base_url=f"http://127.0.0.1:{server.server_port}", poll_interval=.01)
    monkeypatch.setattr(pod, "https_transport", transport)
    try:
        yield pod
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_staging_tree_probe_and_reserved_worker_credential_over_http(http_pod, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.py").write_text("print('hello')\n")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "skip").write_bytes(b"skip")
    remote = tmp_path / "remote"
    stage = RunPodPreparationTransport(http_pod)
    async def exercise():
        await stage.upload_path(source, str(remote))
        evidence = await stage.probe_tree(str(remote))
        assert evidence["exists"]
        assert (remote / "hello.py").read_bytes() == (source / "hello.py").read_bytes()
        assert not (remote / "__pycache__").exists()
        worker = RunPodWorkerProcessTransport(http_pod, account_ref="test-account",
                                              source_root=str(Path(__file__).resolve().parents[3]),
                                              python_executable=sys.executable)
        reserved = await worker.call("reserve_secret", {"operation_root": str(tmp_path / "private")})
        target = Path(reserved["path"])
        inode = target.stat().st_ino
        local = tmp_path / "credential"
        local.write_bytes(b"test-private-credential")
        await worker.upload_private(local, str(target))
        assert target.stat().st_ino == inode
        assert target.stat().st_mode & 0o777 == 0o600
        assert target.read_bytes() == local.read_bytes()
        verified = await worker.call("verify_secret", {"file_identity": reserved,
                                     "size":local.stat().st_size, "sha256": "sha256:" + __import__('hashlib').sha256(local.read_bytes()).hexdigest()})
        assert verified
    asyncio.run(exercise())


def test_builder_https_runs_existing_provisioner_receipt_wrapper(http_pod, tmp_path, monkeypatch):
    # Exercise the production command/receipt wrapper over HTTP; provisioning
    # itself is covered by the existing download/install/manifest tests.
    program = tmp_path / "fake_provisioner.py"
    program.write_text('''from pathlib import Path
import hashlib

def provision(request):
    return {'manifest_sha256':hashlib.sha256((Path(request['root'])/'release-manifest.json').read_bytes()).hexdigest()}
''')
    root = tmp_path / "release"
    root.mkdir()
    manifest = root / "release-manifest.json"
    manifest.write_text('{"test":true}')
    # The cold wrapper now binds an exact source archive, not remote.__file__.
    # Replace only the physical provisioner in this transport fixture while
    # retaining the real archive loader, identity check and receipt wrapper.
    source = builder.bootstrap.source_archive()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(output, "w") as archive:
        for entry in original.infolist():
            body = program.read_bytes() if entry.filename == "astrid/packs/runpod/h3_release_remote.py" else original.read(entry)
            archive.writestr(entry, body)
    payload = output.getvalue()
    monkeypatch.setattr(builder.bootstrap, "source_archive", lambda: payload)
    request = {"root": str(root), "recipe": {}, "lock": {}}
    identity = builder.bootstrap.release_digest(request["recipe"], request["lock"], payload)
    request.update(recipe_sha256=identity, release_id="h3-cu130-" + identity)
    transport = builder.LifecycleTransport.__new__(builder.LifecycleTransport)
    transport.ssh = http_pod.https_transport()
    try:
        result = transport.provision(request, timeout=10)
        assert __import__('base64').b64decode(result['manifest_base64']) == manifest.read_bytes()
    finally:
        transport.close()


def test_builder_preflight_before_allocation_journal(tmp_path, monkeypatch):
    provider = builder.RunPodProvider("test", transport="ssh")
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:1")
    path = tmp_path / "handle.json"
    try:
        with pytest.raises(builder.BuildError, match="SSH egress unavailable; use --transport https"):
            builder.build_release(provider, None, volume_id="v", handle_path=path)
        assert not path.exists()
    finally:
        provider.http.close()


def test_https_claim_persists_only_token_file_reference(tmp_path, monkeypatch):
    monkeypatch.setattr("runpod_lifecycle.preflight.preflight_transport", lambda *_a, **_kw: None)
    monkeypatch.setattr(claim.RunPodConfig, "from_env", staticmethod(lambda **kw: RunPodConfig(api_key="test", **kw)))
    async def volume(*_args):
        return {"id": "v", "name": "backup", "size": 200, "dataCenterId": "DC"}
    monkeypatch.setattr(claim, "_require_existing_volume", volume)
    async def acquire(config, *, attempt, **_kwargs):
        token_path = Path(attempt["https_token_file"])
        assert read_token_file(token_path) == config.https_token
        raw = json.dumps(attempt)
        assert config.https_token not in raw
        assert attempt["transport"] == "https"
        raise RuntimeError("stop before paid provider")
    monkeypatch.setattr(claim, "acquire_target", acquire)
    path = tmp_path / "claim.json"
    args = claim._parser().parse_args(["--transport", "https", "--handle-path", str(path),
                                       "--provider-account-ref", "test-account"])
    with pytest.raises(RuntimeError, match="stop before paid"):
        asyncio.run(claim._claim(args))
    saved = json.loads(path.read_text())
    assert saved["transport"] == "https"
    token_path = Path(saved["https_token_file"])
    assert token_path.stat().st_mode & 0o777 == 0o600


def test_builder_cli_ssh_preflight_before_identity_requirement(monkeypatch, capsys):
    monkeypatch.setenv("HTTPS_PROXY", "http://localhost:1")
    monkeypatch.delenv("RUNPOD_SSH_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_SSH_KEY_PATH", raising=False)
    assert builder.main(["--volume", "volume-id"]) == 1
    assert "SSH egress unavailable; use --transport https" in capsys.readouterr().err


def test_https_claim_resume_reuses_original_token_and_rejects_transport_change(tmp_path, monkeypatch):
    monkeypatch.setattr("runpod_lifecycle.preflight.preflight_transport", lambda *_a, **_kw: None)
    monkeypatch.setattr(claim.RunPodConfig, "from_env", staticmethod(lambda **kw: RunPodConfig(api_key="test", **kw)))
    async def volume(*_args):
        return {"id": "v", "name": "backup", "size": 200, "dataCenterId": "DC"}
    monkeypatch.setattr(claim, "_require_existing_volume", volume)
    tokens = []
    async def stop(config, *, attempt, **_kwargs):
        tokens.append(config.https_token)
        assert config.https_token == read_token_file(attempt["https_token_file"])
        raise RuntimeError("offline stop")
    monkeypatch.setattr(claim, "acquire_target", stop)
    monkeypatch.setattr(claim, "reconcile_target", stop)
    path = tmp_path / "claim.json"
    argv = ["--transport", "https", "--handle-path", str(path), "--provider-account-ref", "test-account"]
    for suffix in ([], ["--resume"]):
        with pytest.raises(RuntimeError, match="offline stop"):
            asyncio.run(claim._claim(claim._parser().parse_args(argv + suffix)))
    assert len(tokens) == 2 and tokens[0] == tokens[1]
    assert len(list(tmp_path.glob("*.token"))) == 1
    assert tokens[0] not in path.read_text()
    with pytest.raises(RuntimeError, match="resume transport differs"):
        asyncio.run(claim._claim(claim._parser().parse_args(argv + ["--resume", "--transport", "ssh"])))
    recorded = json.loads(path.read_text())
    recorded.pop("transport")
    recorded.pop("https_token_file")
    path.write_text(json.dumps(recorded))
    with pytest.raises(RuntimeError, match="resume transport differs"):
        asyncio.run(claim._claim(claim._parser().parse_args(argv + ["--resume"])))


def test_https_claim_preflight_failure_leaves_no_intent_or_token(tmp_path, monkeypatch):
    def denied(transport):
        assert transport == "https"
        raise RuntimeError("offline egress denied")
    monkeypatch.setattr("runpod_lifecycle.preflight.preflight_transport", denied)
    path = tmp_path / "claim.json"
    args = claim._parser().parse_args(["--transport", "https", "--handle-path", str(path),
                                       "--provider-account-ref", "test-account"])
    with pytest.raises(RuntimeError, match="offline egress denied"):
        asyncio.run(claim._claim(args))
    assert not path.exists() and not list(tmp_path.glob("*.token"))


@pytest.mark.integration
@pytest.mark.parametrize("state", ["claimed", "release_pending", "cleanup_pending"])
def test_https_production_composition_selects_claim_token_and_cleanup_needs_no_sidecar(tmp_path, monkeypatch, state):
    from types import SimpleNamespace
    import runpod_lifecycle
    from astrid.packs.runpod import prepared_task
    from astrid.core.util.credentials_scope import CredentialsScope
    from runpod_lifecycle.https import write_token_file
    from tests.helpers.runtime import use_runtime

    use_runtime()
    token_file = tmp_path / "claim.token"
    provider_only = state != "claimed"
    if not provider_only:
        write_token_file(token_file, TOKEN)
    claim_file = tmp_path / "claim.json"
    claim_file.write_text(json.dumps({
        "state": state, "transport": "https", "https_token_file": str(token_file),
        "pod_id": "pod1", "provider_account_ref": "test-account",
        **({"release_quiescence": {"schema": "fixture-quiescence"}} if provider_only else {}),
    }))
    monkeypatch.setenv("ASTRID_RUNPOD_ACCOUNT_REF", "test-account")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(tmp_path / "no-credentials.env"))
    monkeypatch.setenv("RUNPOD_HTTPS_TOKEN_FILE", str(tmp_path / "missing-ambient.token"))
    monkeypatch.setattr(CredentialsScope, "resolve_local", staticmethod(lambda *_a, **_kw: SimpleNamespace(value="test-key")))
    reference = SimpleNamespace(task_id="task1", runtime_instance_id="runtime1", runtime_epoch=1)
    monkeypatch.setattr(prepared_task, "_reference_for_task", lambda *_a: reference)
    lookups = []
    async def get_pod(pod_id, config, **_kwargs):
        assert not provider_only
        lookups.append(pod_id)
        return SimpleNamespace(id=pod_id, config=config)
    monkeypatch.setattr(runpod_lifecycle, "get_pod", get_pod)
    health = {"runtime_instance_id": "runtime1", "runtime_epoch": 1, "runtime_session_id": "session1"}
    runtime = prepared_task._ProductionPreparedTaskRuntime.__new__(prepared_task._ProductionPreparedTaskRuntime)
    runtime.workspace = SimpleNamespace(health=lambda: health, get_executor=lambda *_a: None)
    runtime.client = SimpleNamespace(health=lambda: health)
    runtime.selection = SimpleNamespace(claim_handle_path=claim_file, journal_dir=tmp_path / "journals",
                                       staging_inputs={}, deployment_profile={})
    runtime.workflow_path = runtime.workflow_bundle_path = tmp_path / "workflow"
    runtime.workflow_inputs = {}
    *_, pod = runtime._components({})
    assert pod.config.transport == "https"
    assert pod.config.api_key == "test-key"
    assert lookups == ([] if provider_only else ["pod1"])
    if not provider_only:
        assert pod.config.https_token == TOKEN
    assert TOKEN not in claim_file.read_text()
