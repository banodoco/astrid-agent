"""A running pack host follows a same-port or changed-port Runtime restart."""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.sdk.host_bootstrap import PACK_HOST_ACTOR
from astrid_runtime_client import WorkspaceClient

pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)

_PINNED_RUNTIME = "df3d4ddac7bf73cd858106cc51a802521a4d3552"


@pytest.fixture
def short_lease_runtime_source(monkeypatch):
    """Load the pinned Runtime before the daemon fixture imports its service modules."""
    from tests.helpers.runtime import use_runtime

    root = use_runtime(_PINNED_RUNTIME).resolve()
    # The shared venv can preload a Runtime from W/src before the integration
    # fixture gets a chance to select its requested commit. Remove only those
    # stale service modules so the fixture and this test use one exact Runtime.
    for name, module in tuple(sys.modules.items()):
        if name == "runtime_protocol" or name.startswith("runtime_protocol."):
            source = getattr(module, "__file__", None)
            if source and not Path(source).resolve().is_relative_to(root):
                del sys.modules[name]
    import runtime_protocol.service as runtime_service
    import runtime_protocol.store as runtime_store

    assert Path(runtime_service.__file__).resolve().is_relative_to(root)
    monkeypatch.setattr(runtime_store, "LEASE_SECONDS", 2)
    monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 0.1)
    return root


@pytest.fixture
def short_real_runtime(short_lease_runtime_source, real_runtime):
    # This dependency order ensures the daemon starts only after the exact pinned
    # Runtime modules and the shortened store lease have been selected.
    return real_runtime


def _executor(pack: Path) -> None:
    pack.mkdir()
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "render.basic", "name": "Echo", "kind": "external", "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c",
                             "from pathlib import Path; Path('{out}/answer.txt').write_text('hello')"]},
        "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt",
                     "artifact_type": "text/plain"}],
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"]},
    }), encoding="utf-8")


def _lease_executor(pack: Path, script: str) -> None:
    pack.mkdir()
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "render.lease", "name": "Lease probe", "kind": "external", "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c", script]},
        "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt",
                     "artifact_type": "text/plain"}],
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"]},
    }), encoding="utf-8")


def _install_short_real_heartbeat_lease(monkeypatch, api, task_id, heartbeats):
    """Set the supported lease_seconds wire option, retaining the real SDK transport."""
    original = WorkspaceClient.heartbeat_attempt

    def heartbeat_attempt(self, attempt_id, *, lease_id, fence, idempotency_key,
                          runtime_epoch, progress=None):
        payload = {"lease_id": lease_id, "fence": fence, "runtime_epoch": runtime_epoch,
                   "lease_seconds": 2}
        if progress is not None:
            payload["progress"] = dict(progress)
        _, _, body = self._request(
            "POST", f"/v1/attempts/{attempt_id}/heartbeat",
            body=json.dumps(payload, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": idempotency_key},
        )
        result = self._mutation_json(body)
        observed = api.get_task(task_id)
        heartbeats.append({
            "attempt_id": attempt_id, "fence": fence,
            "progress": dict(progress or {}), "lease_expires_at": observed.lease_expires_at,
        })
        return result

    # Keep an explicit reference to the pinned SDK implementation as evidence that
    # the wrapper only supplies its server-supported optional lease setting.
    assert callable(original)
    monkeypatch.setattr(WorkspaceClient, "heartbeat_attempt", heartbeat_attempt)


def _run_host_one(host, served, *, once=False, errors=None):
    def run():
        try:
            served.extend(host.run(once=once, poll_seconds=0.02, max_tasks=1))
        except BaseException as exc:
            if errors is None:
                raise
            errors.append(exc)

    thread = threading.Thread(
        target=run, daemon=True,
    )
    thread.start()
    return thread


def test_real_host_renews_short_lease_during_cpu_execution(
    tmp_path: Path, monkeypatch, short_real_runtime,
) -> None:
    script = (
        "from pathlib import Path; import json,time; "
        "p=Path('{out}').parent/'.astrid-progress.json'; "
        "[(p.write_text(json.dumps(dict(phase='validation',current=i,total=35))),time.sleep(.12)) "
        "for i in range(35)]; "
        "Path('{out}/answer.txt').write_text('validated')"
    )
    pack = tmp_path / "pack"
    _lease_executor(pack, script)
    record = GenericPackHost(pack_roots=[pack]).discover()[0]
    daemon = short_real_runtime(tmp_path / "realm", support_root=tmp_path / "support",
                                commit=_PINNED_RUNTIME)
    api = WorkspaceClient(daemon.endpoint, daemon.token)
    project = api.create_project("Lease", slug="lease", idempotency_key="lease-project")
    admitted = api.admit_task(capability_id=record.id, capability_digest=record.capability_digest,
                              input_object_ids=[], project_id=project.project_id, idempotency_key="lease-task")
    heartbeats: list[dict] = []
    _install_short_real_heartbeat_lease(monkeypatch, api, admitted.task_id, heartbeats)
    runtime_client = RuntimeProtocolClient(
        daemon.endpoint, daemon.worker_credential_path.read_text().strip(),
        credential_file=daemon.worker_credential_path,
    )
    host = GenericPackHost(pack_roots=[pack], executor_id=PACK_HOST_ACTOR,
                           client=runtime_client)
    host.register()
    served: list = []
    thread = _run_host_one(host, served)
    try:
        deadline = time.monotonic() + 20
        claimed = None
        while time.monotonic() < deadline:
            current = api.get_task(admitted.task_id)
            if current.attempt_id:
                claimed = current
                break
            time.sleep(0.02)
        assert claimed is not None, "host did not claim the admitted task"
        initial_expiry = claimed.lease_expires_at
        thread.join(timeout=20)
        assert not thread.is_alive(), "host did not finish the bounded CPU validation"
        final = api.get_task(admitted.task_id)
        assert final.state == "succeeded"
        assert final.attempt_id == claimed.attempt_id
        assert final.lease_fence == claimed.lease_fence
        assert len(heartbeats) >= 2, heartbeats
        expiries = [item["lease_expires_at"] for item in heartbeats]
        assert initial_expiry is not None and len(set(expiries)) >= 2
        assert all(item["attempt_id"] == claimed.attempt_id for item in heartbeats)
        assert all(item["fence"] == claimed.lease_fence for item in heartbeats)
        assert any(item["progress"].get("current", 0) > 0 for item in heartbeats)
    finally:
        host.shutdown()
        thread.join(timeout=5)
        daemon.stop()


def test_expired_real_lease_rejects_renewal_and_host_fails_closed(
    tmp_path: Path, monkeypatch, short_real_runtime,
) -> None:
    started_marker = repr(str(tmp_path / "child-started.txt"))
    completed_marker = repr(str(tmp_path / "child-completed.txt"))
    script = (
        "from pathlib import Path; import time; "
        f"Path({started_marker}).write_text('started'); time.sleep(8); "
        f"Path({completed_marker}).write_text('completed'); "
        "Path('{out}/answer.txt').write_text('must-not-settle')"
    )
    pack = tmp_path / "pack"
    _lease_executor(pack, script)
    record = GenericPackHost(pack_roots=[pack]).discover()[0]
    daemon = short_real_runtime(tmp_path / "realm", support_root=tmp_path / "support",
                                commit=_PINNED_RUNTIME)
    api = WorkspaceClient(daemon.endpoint, daemon.token)
    project = api.create_project("Expired lease", slug="expired-lease", idempotency_key="expired-project")
    admitted = api.admit_task(capability_id=record.id, capability_digest=record.capability_digest,
                              input_object_ids=[], project_id=project.project_id, idempotency_key="expired-task")
    heartbeats: list[dict] = []
    _install_short_real_heartbeat_lease(monkeypatch, api, admitted.task_id, heartbeats)
    runtime_client = RuntimeProtocolClient(
        daemon.endpoint, daemon.worker_credential_path.read_text().strip(),
        credential_file=daemon.worker_credential_path,
    )
    host = GenericPackHost(pack_roots=[pack], executor_id=PACK_HOST_ACTOR,
                           client=runtime_client)
    host.register()
    served: list = []
    host_errors: list[BaseException] = []
    thread = _run_host_one(host, served, once=True, errors=host_errors)
    try:
        deadline = time.monotonic() + 20
        claimed = None
        while time.monotonic() < deadline:
            current = api.get_task(admitted.task_id)
            if current.attempt_id:
                claimed = current
                break
            time.sleep(0.02)
        assert claimed is not None, "host did not claim the admitted task"
        marker = tmp_path / "child-started.txt"
        marker_deadline = time.monotonic() + 10
        while not marker.is_file() and time.monotonic() < marker_deadline:
            time.sleep(0.02)
        assert marker.is_file(), "real GenericPackHost child did not start"
        child_completion_deadline = time.monotonic() + 8.5
        store = daemon.service.store
        with store._transaction():
            store.conn.execute(
                "UPDATE attempts SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (claimed.attempt_id,),
            )
            store.conn.execute(
                "UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?",
                (admitted.task_id,),
            )
        lease_row = store.conn.execute(
            "SELECT lease_id FROM attempts WHERE id=?", (claimed.attempt_id,),
        ).fetchone()
        assert lease_row is not None
        runtime_client._attempt_runtime_epochs[claimed.attempt_id] = claimed.runtime_epoch
        with pytest.raises(Exception, match="(?i)(lease|attempt|settled|stale)"):
            runtime_client.heartbeat(admitted.task_id, lease_row["lease_id"], attempt_id=claimed.attempt_id,
                                     fence=claimed.lease_fence)
        thread.join(timeout=15)
        assert not thread.is_alive(), "one-shot host did not return after observing the expired lease"
        assert len(host_errors) == 1, host_errors
        assert "execution lease lost" in str(host_errors[0]).lower(), host_errors
        while time.monotonic() < child_completion_deadline:
            time.sleep(0.02)
        final = api.get_task(admitted.task_id)
        assert final.state == "failed"
        assert final.attempt_id == claimed.attempt_id
        assert final.lease_fence == claimed.lease_fence
        assert not (tmp_path / "child-completed.txt").exists()
        assert not (final.result or {}).get("outputs")
    finally:
        host.shutdown()
        thread.join(timeout=5)
        daemon.stop()


@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.parametrize("changed_port", [False, True])
def test_idle_host_claims_work_admitted_after_the_runtime_restarts(
    tmp_path: Path, monkeypatch, concurrency, changed_port: bool, real_runtime,
) -> None:
    # Renew at once, so the host's own loop (not a client) is what must notice the restart.
    monkeypatch.setattr(generic_host, "_EXECUTOR_REFRESH_SECONDS", 0.0)
    pack = tmp_path / "pack"
    _executor(pack)
    record = GenericPackHost(pack_roots=[pack]).discover()[0]

    realm, support = tmp_path / "realm", tmp_path / "support"

    def start(port: int = 0):
        return real_runtime(realm, support_root=support, port=port)

    daemon = start()
    port = daemon.httpd.server_port
    first_epoch = WorkspaceClient(daemon.endpoint, daemon.token).health().runtime_epoch
    runtime_client = RuntimeProtocolClient(
        daemon.endpoint, daemon.worker_credential_path.read_text().strip(),
        credential_file=daemon.worker_credential_path,
    )
    if changed_port:
        runtime_client.bind_local_runtime(support / "discovery.json")
    host = GenericPackHost(
        pack_roots=[pack], executor_id=PACK_HOST_ACTOR, max_concurrency=concurrency,
        client=runtime_client,
    )
    host.register()
    served: list = []
    thread = threading.Thread(target=lambda: served.extend(host.run(poll_seconds=0.05, max_tasks=1)), daemon=True)
    thread.start()
    try:
        # Restart under the idle host. When the port changes, reserve the old one so
        # the daemon must bind a different port instead of reusing it opportunistically.
        daemon.stop()
        if changed_port:
            with socket.socket() as reserved_port:
                reserved_port.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                reserved_port.bind(("127.0.0.1", port))
                daemon = start()
            assert daemon.httpd.server_port != port
        else:
            daemon = start(port)
        api = WorkspaceClient(daemon.endpoint, daemon.token)
        assert api.health().runtime_epoch != first_epoch
        project = api.create_project("Restart", slug="restart", idempotency_key="restart-project")
        task = api.admit_task(capability_id=record.id, capability_digest=record.capability_digest,
                              input_object_ids=[], project_id=project.project_id, idempotency_key="restart-task")
        # No Astrid client opens: the host alone must register under the new epoch and claim it.
        thread.join(timeout=60)
        assert not thread.is_alive(), f"host never claimed the task: {host._runtime_registration_fence!r}"
        assert api.get_task(task.task_id).state == "succeeded"
        digest = "sha256:" + hashlib.sha256(b"hello").hexdigest()
        assert api.get_object(digest).data == b"hello"
    finally:
        host.shutdown()
        thread.join(timeout=10)
        daemon.stop()
