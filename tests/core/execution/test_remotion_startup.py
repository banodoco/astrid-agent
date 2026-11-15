"""The optional content proof has a zero-wait host-readiness budget."""
from __future__ import annotations

import ast
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.core.rendering import remotion_runtime
from astrid.sdk import host_bootstrap
from tests.test_generic_host import FakeRuntime, _write_manifest


def test_bootstrap_remotion_budget_is_zero_content_proofs_before_spawn(tmp_path, monkeypatch):
    environment = {"ASTRID_REMOTION_PROJECT_DIR": str(tmp_path),
                   "ASTRID_REMOTION_VALIDATION_PATH": "stale"}
    calls = []
    monkeypatch.setattr(remotion_runtime, "prepare_remotion_validation", lambda **kw: calls.append(kw))
    assert host_bootstrap._prepare_render_validation(
        tmp_path, environment, tmp_path, sys.executable, defer=True,
    ) is None
    assert calls == []
    assert "ASTRID_REMOTION_VALIDATION_PATH" not in environment


@pytest.mark.parametrize("failure", [None, "node_modules content changed"])
def test_ready_budget_and_remotion_admission_during_slow_proof(tmp_path, monkeypatch, failure):
    from astrid.core._shared.boot_manifest import build_manifest, load_boot_manifest_hash

    packs = tmp_path / "packs"
    _write_manifest(packs / "plain", capability_id="test.plain")
    manifest = _write_manifest(packs / "render", capability_id="test.render")
    data = json.loads(manifest.read_text())
    data["metadata"].update(adapter_family="cpu", requires_remotion="selected")
    manifest.write_text(json.dumps(data))
    client = FakeRuntime()
    host = generic_host.GenericPackHost(pack_roots=[packs], client=client)
    ready = tmp_path / "ready.json"
    boot = tmp_path / "boot.json"
    boot.write_text(json.dumps(build_manifest(registry={}, fixtures=(), profile_order=())))
    digest = "sha256:" + load_boot_manifest_hash(boot, support_root=tmp_path)
    entered, finish = threading.Event(), threading.Event()
    calls = []

    def slow_proof(*args, **kwargs):
        calls.append("proof")
        assert ready.exists(), "content validation must start after ready publication"
        entered.set()
        assert finish.wait(10), "test must release its owned validator"
        if failure:
            raise remotion_runtime.RemotionValidationError(failure)
        return "a" * 32

    def status(**kwargs):
        calls.append("admission")
        return remotion_runtime.RemotionRuntimeStatus(True, tmp_path)

    monkeypatch.setattr(host_bootstrap, "_prepare_render_validation", slow_proof)
    monkeypatch.setattr(remotion_runtime, "remotion_runtime_status", status)
    monkeypatch.setattr(host, "_remotion_idle_context", lambda: "fixed-proof-context")
    monkeypatch.setattr(generic_host, "GenericPackHost", lambda **kw: host)
    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", lambda *a, **kw: client)
    monkeypatch.setattr(generic_host, "_compose_cli_boot_manifest", lambda *a: (boot, digest))
    monkeypatch.setattr(generic_host, "_startup_identity_attestation", lambda **kw: {"source": {"closure_digest": "3" * 64}})
    monkeypatch.setattr(generic_host.platform, "install_stop_handlers", lambda *a: None)
    monkeypatch.setattr(generic_host.faulthandler, "register", lambda *a, **kw: None)
    monkeypatch.setattr(host, "_child_environment", lambda *a, **kw: ({}, {}))
    # Keep the real task admission, lease settlement and producer path; the
    # unrelated child-environment proof has dedicated runtime tests.
    monkeypatch.setattr(host, "_run_command_definition", lambda record, **kw: {"answer": "ok"})
    credential = tmp_path / "unit-worker"
    credential.write_text("unit-worker")
    credential.chmod(0o600)
    monkeypatch.setattr(sys, "argv", ["host", "run", "--register", "--pack-root", str(packs),
        "--runtime-endpoint", "http://127.0.0.1:1", "--credential-file", str(credential),
        "--ready-file", str(ready), "--support-root", str(tmp_path),
        "--source-checkout", str(tmp_path), "--defer-remotion-validation"])
    for name in ("ASTRID_EXECUTION_TARGET_JSON", "ASTRID_OUTPUT_ROOT", "ASTRID_HOST_READINESS_PROFILE_PATH",
                 "ASTRID_PACK_HOST_SERVICE_MODE", "ASTRID_REMOTION_VALIDATION_LAUNCH"):
        monkeypatch.delenv(name, raising=False)

    def task(name, selector=None):
        row = {"id": name, "capability": "test.render", "spec": {"inputs": {"selector": selector}},
               "attempt_id": "attempt-" + name, "fence": 1}
        client.tasks[name] = {"task": row}
        return row

    def loop(**kwargs):
        assert ready.exists() and entered.wait(5)
        assert calls == ["proof"]  # Zero synchronous native preflights before ready.
        with pytest.raises(generic_host.HostError, match="validation pending"):
            host.run_task(task("pending", "rendering.remotion"), lease_token="lease")
        assert client.failures[-1][3]["structured_error"]["code"] == "remotion_validation_pending"
        # A task explicitly choosing ffmpeg never waits for the Remotion proof.
        host.preflight("test.plain")
        assert host.capabilities["test.plain"].ready and not finish.is_set()
        assert generic_host._renderer_selection(host.capabilities["test.render"], task("ffmpeg", "rendering.ffmpeg"))["selector"] == "rendering.ffmpeg"
        finish.set()
        host._remotion_validation_thread.join(5)
        assert not host._remotion_validation_thread.is_alive()
        if failure:
            with pytest.raises(generic_host.HostError, match=failure):
                host.run_task(task("failed", "rendering.remotion"), lease_token="lease")
            assert client.failures[-1][3]["structured_error"]["code"] == "remotion_validation_failed"
            host.preflight()
            assert failure in generic_host._gated_remotion_faults(host.capabilities.values())["test.render"]
            assert calls == ["proof"]
        else:
            host.preflight("test.render")
            assert host.capabilities["test.render"].preflight["remotion"]["ok"]
            assert generic_host._renderer_selection(host.capabilities["test.render"], task("proved", "rendering.remotion"))["remotion_ok"]
            assert calls == ["proof", "admission"]  # The per-job full check is retained.
        return []

    monkeypatch.setattr(host, "run", loop)
    try:
        assert generic_host._cli() == 0
    finally:
        finish.set()
        thread = getattr(host, "_remotion_validation_thread", None)
        if thread is not None:
            thread.join(5)


@pytest.mark.parametrize("module,argv,enabled", [
    ("__main__", ["host", "--service"], True),
    ("__main__", ["host", "run", "--ready-file", "unit-ready"], True),
    ("__main__", ["host", "--help"], False),
    ("astrid.core.execution.generic_host", ["host", "--service"], False),
])
def test_startup_observation_enables_service_and_preserves_other_modes(module, argv, enabled):
    # Evaluate the actual import-time switch without executing a service.
    tree = ast.parse(Path(generic_host.__file__).read_text())
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == "_STARTUP_OBSERVATION_ENABLED" for t in node.targets))
    actual = eval(compile(ast.Expression(assignment.value), "startup-switch", "eval"),
                  {"__name__": module, "sys": SimpleNamespace(argv=argv)})
    assert actual is enabled
