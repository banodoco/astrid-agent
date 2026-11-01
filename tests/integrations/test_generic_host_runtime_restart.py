"""A running pack host follows a same-port or changed-port Runtime restart."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.sdk.host_bootstrap import PACK_HOST_ACTOR
from astrid_runtime_client import WorkspaceClient

pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)


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
