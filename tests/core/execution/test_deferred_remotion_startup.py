"""Readiness is task acceptance; Remotion proof is required before dispatch."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import sys
from threading import Event

import pytest

from astrid.core.execution import generic_host
from astrid.core.rendering import remotion_runtime as runtime
from astrid.sdk import host_bootstrap
from tests.core.rendering.test_remotion_runtime import installed_runtime  # noqa: F401
from tests.test_generic_host import _IdleReadinessRuntime, _write_manifest


@pytest.mark.parametrize("outcome", ["success", "optional_failure", "custody_failure"])
def test_cli_ready_does_not_wait_for_validation_but_render_does(
    installed_runtime, tmp_path, monkeypatch, outcome,
):
    world = installed_runtime
    monkeypatch.setattr(os, "environ", dict(world.environment))
    monkeypatch.setattr(runtime, "_deferred_validation", None)
    pack_root = tmp_path / "packs"
    manifest = _write_manifest(pack_root / "render", capability_id="test.render")
    definition = json.loads(manifest.read_text())
    definition["metadata"].update(adapter_family="cpu", requires_remotion=True)
    manifest.write_text(json.dumps(definition))
    _write_manifest(pack_root / "plain", capability_id="test.plain")
    support = tmp_path / "support"
    support.mkdir()
    ready = support / "generic-host.ready.json"
    from astrid.core.gateway.dispatch import compose_profile_handoff
    boot = support / "astrid-host" / "boot-manifest.json"
    compose_profile_handoff(boot, support_root=support)
    entered, release, dispatched = Event(), Event(), Event()
    calls = []

    def prepare(**kwargs):
        assert remotion_lock.remotion_render_lock_held()
        calls.append(kwargs)
        entered.set()
        assert release.wait(10)
        if outcome != "success":
            failure = runtime.RemotionValidationError("partial npm installation")
            if outcome == "custody_failure":
                failure.cleanup_error = RuntimeError("cleanup unproved")
            raise failure
        return world.reference

    from astrid.packs.rendering.rendering.renderers.remotion import lock as remotion_lock
    monkeypatch.setattr(runtime, "prepare_remotion_validation", prepare)
    monkeypatch.setattr(host_bootstrap, "_capability_matrix_path", lambda source: tmp_path / "no-matrix")
    monkeypatch.setattr(generic_host, "_launch_worker_probe_environments", lambda *a, **k: [dict(os.environ)])
    monkeypatch.setattr(generic_host, "_startup_identity_attestation", lambda **kwargs: {"source": {}})
    monkeypatch.setattr(generic_host, "source_checkout_digest", lambda root: "fixture-digest")
    client = _IdleReadinessRuntime()
    client.tasks["task-render"] = {"task": {"id": "task-render", "capability": "test.render", "spec": {}}}
    client.on_claim = lambda payload: {"task_id": "task-render", "lease_id": "lease-render",
        "attempt_id": "attempt-render", "fence": 9, "runtime_epoch": 7}
    class ClientFactory:
        def __new__(cls, *args, **kwargs):
            return client

    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", ClientFactory)

    class ReachedDispatch(Exception):
        pass

    def materialize(*args, **kwargs):
        dispatched.set()
        raise ReachedDispatch("validated task reached materialization")

    monkeypatch.setattr(generic_host.GenericPackHost, "_materialize_inputs", materialize)

    def run(host, **kwargs):
        assert json.loads(ready.read_text())["status"] == "ready"
        assert not entered.is_set(), "startup must not start or wait for validation"
        assert host.capabilities["test.render"].preflight["remotion"]["pending"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            rendering = pool.submit(host.claim_once)
            try:
                if not entered.wait(10):
                    rendering.result(timeout=1)
                    pytest.fail("render did not enter deferred validation")
                assert json.loads(ready.read_text())["status"] == "ready"
                assert not rendering.done() and not dispatched.is_set()
                assert host.preflight("test.plain")[0].ready
            finally:
                release.set()
            if outcome == "success":
                with pytest.raises(ReachedDispatch):
                    rendering.result(timeout=10)
                assert dispatched.is_set()
                # Deferral must not re-advertise a known bad runtime after a
                # later full check invalidates the successful observation.
                world.node.write_bytes(b"changed Node")
                assert not host.preflight("test.render")[0].ready
                assert not host._preflight("test.render", full_remotion=False)[0].ready
            else:
                message = ("partial npm installation" if outcome == "custody_failure" else
                           "missing or invalid launch validation; explicit owned validation required")
                with pytest.raises(generic_host.HostError, match=message):
                    rendering.result(timeout=10)
                assert not dispatched.is_set()
                assert client.failures[0][0:2] == ("task-render", "lease-render")
        assert len(calls) == 1
        if outcome == "custody_failure":
            assert not ready.exists() and host._shutdown.is_set()
        else:
            assert json.loads(ready.read_text())["status"] == "ready"
            assert not host._shutdown.is_set()
        return {}

    monkeypatch.setattr(generic_host.GenericPackHost, "run", run)
    monkeypatch.setattr(sys, "argv", ["astrid-generic-host", "run", "--pack-root", str(pack_root),
        "--runtime-endpoint", "http://127.0.0.1:8765", "--register", "--ready-file", str(ready),
        "--support-root", str(support), "--source-checkout", str(tmp_path),
        "--boot-manifest-path", str(boot), "--defer-remotion-validation"])
    assert generic_host._cli() == 0


def test_bootstrap_defers_without_importing_another_launch_proof(monkeypatch):
    env = {"ASTRID_REMOTION_PROJECT_DIR": "/owned/remotion", "LD_LIBRARY_PATH": "/foreign",
           **{name: "foreign" for name in runtime._REF_ENV_KEYS}}
    checked = []
    monkeypatch.setattr(host_bootstrap, "_verify_remotion_probe_custody", lambda env: checked.append(dict(env)))
    monkeypatch.setattr(runtime, "prepare_remotion_validation", lambda **kw: pytest.fail("eager validation"))
    assert host_bootstrap._defer_render_validation(env)
    assert checked == [{"ASTRID_REMOTION_PROJECT_DIR": "/owned/remotion"}]
    assert env == checked[0]


def test_deferral_does_not_hide_already_unresolved_validation_custody(monkeypatch):
    retained = RuntimeError("original child cleanup unproved")
    monkeypatch.setattr(runtime, "_UNRESOLVED_VALIDATION_PROBES", [retained])
    with pytest.raises(runtime.RemotionValidationError, match="prior runtime validation child custody") as caught:
        host_bootstrap._defer_render_validation({})
    assert caught.value.__cause__ is retained
