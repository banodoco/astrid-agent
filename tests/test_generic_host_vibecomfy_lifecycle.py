from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.execution.managed_tool_session import SessionCapacityError
from astrid.core.generation.backends.vibecomfy import CheckoutServerAdapter
from astrid.core.generation.model_root import canonical_model_inventory_digest
from tests.test_generic_host import FakeRuntime


class _FakeCheckout(CheckoutServerAdapter):
    """A real CheckoutServerAdapter with its external parts stubbed.

    It skips the live listener, registry and owned-process verification that
    ``from_host_session`` performs, but carries the same bound identity fields
    so the host's residency binding admits it exactly as it admits the real
    adapter.  Native control (observe/cancel/release) is recorded, not sent.
    """

    def __init__(
        self,
        events: list[str],
        *,
        spawn_child: bool,
        hc03_profile: dict[str, object],
        binding,
        endpoint_transport=None,
    ) -> None:
        # Deliberately no super().__init__(): it would validate and probe a server.
        self._host_session = dict(hc03_profile["vibecomfy_session"])  # type: ignore[arg-type]
        self._host_profile = hc03_profile
        self.binding = binding
        self.observed_binding = binding
        self.contexts = []
        self.prompt_id = None
        self.uncertain = False
        self._runtime_instance_id = hc03_profile["runtime"]["runtime_instance_id"]  # type: ignore[index]
        self._bound_fingerprint = hc03_profile["verified_facts_digest"]  # type: ignore[assignment]
        self.events = events
        self.child = (
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            if spawn_child
            else None
        )
        self.events.append("construct")

    def observe(self, *, binding) -> dict[str, object]:
        self.events.append("observe")
        return {"ok": binding == self.binding == self.observed_binding}

    def invoke(self, *, token, model_id, template_id):
        assert token.binding_identity == self.binding.identity_key
        self.contexts.append((token, model_id, template_id))
        self.prompt_id = token.token_id
        return {"ok": True, "child_context": {"token_id": token.token_id,
                "invocation_id": token.invocation_id, "generation": token.generation,
                "binding_identity": list(token.binding_identity)}}

    def fence(self, *, reason):
        self.events.append("fence:" + reason)
        return {"ok": True, "fenced": True}

    def cancel(self, *, reason) -> dict[str, object]:
        self.events.append("cancel:" + str(self.prompt_id))
        return {"ok": not self.uncertain, "cancelled": not self.uncertain}

    def release(self, *, reason: str = "requested") -> dict[str, object]:
        self.events.append(f"release:{reason}")
        return {"ok": not self.uncertain, "released": not self.uncertain,
                "release_requested": True, "memory_observed_returned": False}


def _task(task_id: str, digest: str) -> dict[str, object]:
    return {
        "task": {
            "id": task_id,
            "capability": "vibecomfy.run",
            "project_id": "fixture-project",
            "attempt_id": f"{task_id}-attempt",
            "fence": 1,
            "runtime_epoch": 1,
            "input_object_ids": [digest],
            "spec": {"spec": {"inputs": {}, "input_digests": [{"name": "workflow", "digest": digest}]}},
        }
    }


def _install_fixture(tmp_path: Path) -> tuple[str, bytes]:
    data = b"canonical workflow fixture\n"
    digest = hashlib.sha256(data).hexdigest()
    capability = tmp_path / "vibecomfy-fixture"
    capability.mkdir()
    command = (
        "from pathlib import Path; out=Path('{out}'); out.mkdir(parents=True,exist_ok=True); "
        "(out/'result.txt').write_bytes(Path('{workflow}').read_bytes())"
    )
    manifest = {
        "schema_version": 1,
        "id": "vibecomfy.run",
        "name": "VibeComfy lifecycle fixture",
        "kind": "external",
        "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c", command]},
        "inputs": [{"name": "workflow", "type": "file"}],
        "outputs": [{"name": "result", "type": "file", "path_template": "{out}/result.txt", "artifact_type": "text/plain"}],
        "metadata": {"adapter_family": "local_generation", "resource_keys": ["gpu"]},
    }
    (capability / "executor.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    return digest, data


def _profile() -> dict[str, object]:
    return {
        "schema_version": "hc03-worker-readiness.v1",
        "status": "ready",
        "verified_facts_digest": "facts-a",
        "verified_facts": {"exact": {
            "interpreter": "python", "runtime_lock": "runtime", "engine_lock": "engine",
            "model_digest": "sha256:" + "a" * 64, "custom_node_digest": "sha256:" + "b" * 64,
            "driver": "driver", "root": "/tmp/root", "port": 8188,
        }, "minimum": {"vram_bytes": 1, "scratch_bytes": 1}},
        "runtime": {"runtime_instance_id": "00000000-0000-4000-8000-000000000001"},
        "launch": {
            "model_root": {
                "schema_version": 1,
                "path": str(Path(__file__).resolve().parent),
                "inventory": [],
                "inventory_digest": canonical_model_inventory_digest([]),
            }
        },
        "vibecomfy_session": {
            "session_dir": "/tmp/session", "process_birth_id": "process-a", "comfy_process_birth_id": "comfy-a", "launch_generation": "launch-a",
            "server_url": "http://127.0.0.1:8188", "source_revision": "source-a",
            "source_content_digest": "sha256:" + "c" * 64, "config_digest": "sha256:" + "d" * 64,
        },
    }


def test_generic_host_run_task_keeps_same_resident_identity_warm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    digest, data = _install_fixture(tmp_path)
    runtime = FakeRuntime()
    runtime.get_object = lambda requested: data  # type: ignore[method-assign]
    profile_reads: list[int] = []

    def read_profile() -> dict[str, object]:
        profile = _profile()
        pid = 100
        profile_reads.append(pid)
        session = profile["vibecomfy_session"]
        assert isinstance(session, dict)
        session["pid"] = pid
        return profile

    monkeypatch.setattr(generic_host, "_read_readiness_profile_document", read_profile)
    identities = iter((("execution-a", "model-a"), ("execution-a", "model-a"), ("execution-b", "model-b"), ("execution-b", "model-b")))
    monkeypatch.setattr(generic_host, "_prepare_vibecomfy_execution_identity", lambda *args: (lambda value: (value[0], value[1], "template-a", {"model_id": value[1], "resident_metadata": {"model_assets": [value[1]]}}))(next(identities)))
    from astrid.core.generation.backends import vibecomfy
    events: list[str] = []
    monkeypatch.setattr(
        vibecomfy.CheckoutServerAdapter,
        "open_host_session",
        classmethod(lambda cls, **kwargs: (
            events.append(f"profile-pid:{kwargs['hc03_profile']['vibecomfy_session']['pid']}"),
            _FakeCheckout(
                events,
                spawn_child=False,
                hc03_profile=kwargs["hc03_profile"],
                binding=kwargs["binding"],
            ),
        )[1]), raising=False,
    )
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    for task_id in ("warm-1", "warm-2"):
        task = _task(task_id, digest)
        runtime.tasks[task_id] = task
        assert host.run_task(task, lease_token=f"lease-{task_id}")["task"]["status"] == "completed"
    observed_binding = runtime.settlements[0][2]["result"]["managed_tool_session"]["binding"]
    assert observed_binding["launch_generation"] == "launch-a"
    assert observed_binding["engine_birth_id"] == "comfy-a"
    assert [settled[0] for settled in runtime.settlements] == ["warm-1", "warm-2"]
    second_binding = runtime.settlements[1][2]["result"]["managed_tool_session"]["binding"]
    assert second_binding == observed_binding, "the same-identity task must reuse the warm engine"

    # A changed model never replaces an SDK-bound engine in place (71d43e00,
    # generic_host.py residency_bound): the host refuses, nothing is released
    # or settled, and the resident child is not torn down behind custody.
    task = _task("changed-model", digest)
    runtime.tasks["changed-model"] = task
    with pytest.raises(SessionCapacityError, match="managed session release was not verified"):
        host.run_task(task, lease_token="lease-changed-model")
    assert "release:capacity_replacement" not in events
    assert "old-child-exited" not in events
    assert [settled[0] for settled in runtime.settlements] == ["warm-1", "warm-2"]
    host.managed_tool_session.close()


def test_idle_expiry_frees_the_warm_vibecomfy_session_and_the_next_task_starts_cold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astrid.core.execution.warm_hold import WarmHolds
    from astrid.core.generation.backends import vibecomfy
    from tests.core.execution.test_warm_hold import Timers

    digest, data = _install_fixture(tmp_path)
    runtime = FakeRuntime()
    runtime.get_object = lambda requested: data  # type: ignore[method-assign]
    monkeypatch.delenv("ASTRID_WARM_IDLE_SECONDS", raising=False)
    monkeypatch.setattr(generic_host, "_read_readiness_profile_document", lambda: {
        **_profile(), "vibecomfy_session": {**_profile()["vibecomfy_session"], "pid": 100}})  # type: ignore[dict-item]
    monkeypatch.setattr(generic_host, "_prepare_vibecomfy_execution_identity", lambda *args: (
        "execution-a", "model-a", "template-a", {"model_id": "model-a", "resident_metadata": {"model_assets": ["model-a"]}}))
    events: list[str] = []
    monkeypatch.setattr(vibecomfy.CheckoutServerAdapter, "open_host_session", classmethod(
        lambda cls, **kwargs: _FakeCheckout(events, spawn_child=False, **kwargs)), raising=False)
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    timers = Timers()
    host.warm_holds = WarmHolds(timer=timers)
    try:
        for released, task_id in enumerate(("before-idle", "after-idle")):
            task = _task(task_id, digest)
            runtime.tasks[task_id] = task
            assert host.run_task(task, lease_token=f"lease-{task_id}")["task"]["status"] == "completed"
            (hold,) = host.warm_holds.status()
            assert (hold["holding"], hold["leases"], hold["idle_seconds"]) == ("vibecomfy.run", 0, 600.0)
            assert hold["on_idle"] == "models unloaded; engine process kept; memory release not measured"
            assert timers.live().seconds == 600 and events.count("release:idle") == released
            assert host._vibecomfy_warmth_hint is not None
            timers.fire()
            # Models are unloaded through the session's own release; the slot is empty.
            assert events[-1] == "release:idle"
            assert not host.managed_tool_session.occupied and host.warm_holds.status() == []
            assert host._vibecomfy_warmth_hint is None
        assert events.count("construct") == 2, "the task after idle release binds a fresh, cold session"
    finally:
        host.managed_tool_session.close()


def test_vibecomfy_child_receives_only_the_profile_model_root_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fixture(tmp_path)
    monkeypatch.setattr(generic_host, "_read_readiness_profile_document", _profile)
    monkeypatch.setenv("ASTRID_VIBECOMFY_MODELS_ROOT", "/ambient/override")
    host = GenericPackHost(pack_roots=[tmp_path])
    record = host.discover()[0]

    child_env, secrets = host._child_environment(record, tmp_path / "attempt")
    try:
        expected_root = str(Path(__file__).resolve().parent)
        assert child_env["ASTRID_VIBECOMFY_MODELS_ROOT"] == expected_root
        binding = json.loads(child_env["ASTRID_VIBECOMFY_MODEL_ROOT_BINDING"])
        assert binding["path"] == expected_root
    finally:
        child_env.clear()
        secrets.clear()


def test_generic_host_pip_embedded_route_does_not_require_hc03(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    digest, data = _install_fixture(tmp_path)
    runtime = FakeRuntime()
    runtime.get_object = lambda requested: data  # type: ignore[method-assign]
    monkeypatch.setattr(generic_host, "_read_readiness_profile_document", lambda: None)
    monkeypatch.setattr(generic_host, "_prepare_vibecomfy_execution_identity", lambda *args: ("execution-pip", "model-a", "template-a", {"model_id": "model-a", "resident_metadata": {}}))
    host = GenericPackHost(pack_roots=[tmp_path], client=runtime)
    host.discover()
    task = _task("pip-embedded", digest)
    runtime.tasks["pip-embedded"] = task
    assert host.run_task(task, lease_token="lease-pip")["task"]["status"] == "completed"
    assert runtime.settlements[0][2]["result"]["managed_tool_session"]["capability"]["residency_support"] == "unsupported"
