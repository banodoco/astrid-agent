"""A running service host serves a pack that `astrid packs add` commits after it registered (real Runtime)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.pack.transaction import install_group, refresh_pending
from astrid.pack_api import VERSION as PACK_API_VERSION
from astrid.sdk.host_bootstrap import PACK_HOST_ACTOR
from astrid_runtime_client import WorkspaceClient

pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)


def _add_pack(tmp_path: Path, personal: Path, pack_id: str) -> None:
    """`astrid packs add <folder>`: the real group transaction publishes the pack and commits its outbox entry."""
    source = tmp_path / "sources" / pack_id
    (source / pack_id).mkdir(parents=True)
    (source / pack_id / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": pack_id, "name": pack_id.title(), "version": "1.0.0",
        "pack_api": "%d.%d" % PACK_API_VERSION,
        "status": "experimental", "domain": "integration", "stability": "experimental",
        "support": "community", "visibility": "visible", "description": f"{pack_id} fixture pack.",
        "permissions": [{"id": "subprocess", "reason": "Fixture actions run as command subprocesses."}],
        "capabilities": ["echo"],
        "actions": {"echo": {
            "description": "echo fixture action.", "inputs": [], "name": "Echo", "version": "1.0",
            "invocation": {"kind": "command", "command": {"argv": [
                "{python_exec}", "-c", "from pathlib import Path; Path('{out}/answer').write_text('ok')"]}},
            "outputs": [],
        }},
    }), encoding="utf-8")
    install_group(source=source, revision=None, packs={pack_id: pack_id}, packs_root=personal, python_packages={})


@pytest.mark.parametrize("concurrency", [1, 2])
def test_registered_host_serves_a_pack_committed_after_it_registered(
    tmp_path: Path, monkeypatch, concurrency, real_runtime,
) -> None:
    home = tmp_path / "home"
    personal = home / "Astrid" / "packs"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    _add_pack(tmp_path, personal, "personal_echo")

    daemon = real_runtime(tmp_path / "realm", support_root=tmp_path / "support")
    api = WorkspaceClient(daemon.endpoint, daemon.token)
    runtime_client = RuntimeProtocolClient(
        daemon.endpoint, daemon.worker_credential_path.read_text().strip(),
        credential_file=daemon.worker_credential_path,
    )
    # The installed service's host: `run --register` serves the personal collection.
    host = GenericPackHost(pack_roots=[], executor_id=PACK_HOST_ACTOR, max_concurrency=concurrency,
                           client=runtime_client, personal_collection=True)
    host.register()
    assert host.acknowledge_pack_refresh() is True  # the startup entry, under an unchanged epoch
    epoch_before = host.source_epoch
    served: list = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            served.extend(host.run(poll_seconds=0.05, max_tasks=1))
        except BaseException as exc:  # noqa: BLE001 - reported by the assertions below
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        time.sleep(0.5)  # the loop is idle and polling before the pack lands
        _add_pack(tmp_path, personal, "chiptune")
        assert refresh_pending(packs_root=personal)

        deadline = time.monotonic() + 60
        while refresh_pending(packs_root=personal) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not refresh_pending(packs_root=personal), (
            f"host never acknowledged the committed pack: {host._pack_refresh_reason!r}; "
            f"admission={host._registration_admission_reason()!r}")
        assert host.source_epoch != epoch_before
        record = host.capabilities["chiptune.echo"]

        project = api.create_project("Pack refresh", slug="pack-refresh", idempotency_key="pack-refresh-project")
        task = api.admit_task(capability_id=record.id, capability_digest=record.capability_digest,
                              input_object_ids=[], project_id=project.project_id,
                              idempotency_key="pack-refresh-task")
        thread.join(timeout=60)
        assert not thread.is_alive(), (
            f"host never claimed the chiptune task: {host._registration_admission_reason()!r}")
        assert errors == []
        final = api.get_task(task.task_id)
        assert final.state == "succeeded", final
        assert host._registration_admission_reason() is None
    finally:
        host.shutdown()
        thread.join(timeout=10)
        daemon.stop()
