"""Custody against the pinned disposable Runtime, with exact HTTP mutations."""
import copy
import base64
import hashlib
from pathlib import Path
import json
import threading

import pytest

from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.execution._child_bridge import ChildBridgeError, task_resource
from astrid.sdk._child_bridge import _BridgeRejected
from tests.integrations.test_h3_delegated_child_claim_135 import (
    claim_world, child_request, unrelated, CHILD,
)

pytestmark = pytest.mark.integration


def test_lost_completion_reply_reconciles_without_opposite_terminal(claim_world, monkeypatch):
    world = claim_world
    calls = []
    settle = world.client.generated.settle_attempt
    def lost(attempt_id, settlement, **kwargs):
        result = settle(attempt_id, settlement, **kwargs)
        calls.append((attempt_id, copy.deepcopy(settlement), kwargs, result))
        raise OSError("committed completion reply lost")
    monkeypatch.setattr(world.client.generated, "settle_attempt", lost)
    failures = []
    monkeypatch.setattr(world.client, "fail", lambda *a, **kw: failures.append((a, kw)))
    terminal = {}
    def execute(*args, _terminal, **kwargs):
        terminal.update(value=_terminal)
        return _terminal["completion_call"](world.client.settle, world.context["task_id"],
            world.context["lease_id"], result={"cpu_fixture": True}, outputs=[], effect=None,
            attempt_id=world.context["attempt_id"], fence=world.context["fence"])
    monkeypatch.setattr(world.host, "_run_task_impl", execute)
    with pytest.raises(OSError, match="reply lost"):
        world.host.run_task({**world.parent, "id": world.context["task_id"]}, lease_token=world.context["lease_id"],
                            attempt_id=world.context["attempt_id"], fence=world.context["fence"], keep_attempt=True)
    assert len(calls) == 1 and failures == []
    current = task_resource(world.client.task(world.context["task_id"]))
    assert current["state"] == "succeeded"
    # Current task state is real, but it cannot prove this completion's request identity.
    assert terminal["value"]["completion_state"] == "unknown"
    root = terminal["value"]["root"]
    receipt = json.loads((root / "result.json").read_text())
    assert not receipt["ok"]
    assert receipt["publication"]["status"] == "unconfirmed"
    assert receipt["completion_diagnostic"] == {"status": "unconfirmed"}
    with pytest.raises(HostError, match="gate is closed or unresolved"):
        world.host._handoff_begin_direct_attempt()


def test_lease_loss_cannot_complete_replacement_attempt(claim_world, monkeypatch):
    from astrid.core.execution import generic_host as gh
    world = claim_world
    old = copy.deepcopy(world.context)
    world.client.fail(old["task_id"], old["lease_id"], "retry fixture", retryable=True,
                      attempt_id=old["attempt_id"], fence=old["fence"])
    world.owner.retry_task(old["task_id"], idempotency_key="custody-retry")
    replacement = world.client.claim_next(executor_id=world.host.executor_id,
        capability_ids=["h3_av.transform"], idempotency_key="custody-replacement")
    assert replacement.task_id == old["task_id"]
    assert replacement.attempt_id != old["attempt_id"]
    world.calls.clear()
    monkeypatch.setattr(gh, "_LEASE_POLL_SECONDS", 0.0)
    terminal = {}
    def execute(*args, _terminal, **kwargs):
        terminal.update(value=_terminal)
        assert _terminal["cancel_signal"].wait(5), "observer missed replacement attempt"
        return _terminal["completion_call"](world.client.settle, old["task_id"], old["lease_id"],
            result={}, outputs=[], effect=None, attempt_id=old["attempt_id"], fence=old["fence"])
    monkeypatch.setattr(world.host, "_run_task_impl", execute)
    with pytest.raises(HostError):
        claimed = {key: value for key, value in world.parent.items() if key != "lease_expires_at"}
        world.host.run_task({**claimed, "id": old["task_id"]}, lease_token=old["lease_id"],
                            attempt_id=old["attempt_id"], fence=old["fence"], keep_attempt=True)
    current = task_resource(world.client.task(old["task_id"]))
    assert current["state"] == "running"
    assert current["attempt_id"] == replacement.attempt_id
    assert terminal["value"]["forced"] == "lease_lost"
    assert not terminal["value"]["pump_thread"].is_alive()
    assert world.calls == []


def test_delegated_child_exact_replay_and_parent_output_custody(claim_world, monkeypatch):
    world = claim_world
    decoy = unrelated(world)
    entered, release = threading.Event(), threading.Event()
    executed = []
    contents = b"verified child output"
    output = {"name": "answer", "filename": "answer.txt", "kind": "object", "media_type": "text/plain",
              "digest": "sha256:" + hashlib.sha256(contents).hexdigest(), "size": len(contents),
              "data_base64": base64.b64encode(contents).decode(), "ordinal": 0, "role": "result"}
    def execute(slot, task, **kwargs):
        executed.append(task["task"]["id"])
        entered.set()
        assert release.wait(5)
        return slot.client.settle(task["task"]["id"], kwargs["lease_token"],
            result={"cpu_fixture": True}, outputs=[output], effect=None,
            attempt_id=kwargs["attempt_id"], fence=kwargs["fence"])
    monkeypatch.setattr(GenericPackHost, "run_task", execute)
    request = child_request(world)
    try:
        first = world.sdk.submit(request)
        assert entered.wait(5)
        assert world.sdk.submit(copy.deepcopy(request)) == first
        assert executed == [first["task_id"]]
        changed = copy.deepcopy(request)
        changed["inputs"] = {"foreign": True}
        with pytest.raises(_BridgeRejected):
            world.sdk.submit(changed)
        assert world.sdk.status("selected", first["task_id"])["state"] == "running"
        # A parent cannot consume an unrelated child's output page.
        with pytest.raises(_BridgeRejected):
            world.sdk.status("selected", decoy["task_id"])
        assert all(call["path"] != "/v1/tasks/claim" for call in world.calls)
        assert task_resource(world.client.task(decoy["task_id"]))["state"] == "queued"
    finally:
        release.set()
        bound = world.source._children.get("selected", {})
        if "claim_thread" in bound:
            bound["claim_thread"].join(5)
    assert world.sdk.status("selected", first["task_id"])["state"] == "succeeded"
    rows = world.sdk.outputs("selected", first["task_id"])
    assert len(rows) == 1
    assert rows[0]["digest"] == output["digest"]
    assert rows[0]["task_id"] == first["task_id"]
    materialized = world.sdk.materialize_output(child_key="selected", task_id=first["task_id"], output=rows[0])
    assert (world.source.output_root / materialized.filename).read_bytes() == contents
    assert world.sdk.materialize_output(child_key="selected", task_id=first["task_id"], output=rows[0]) == materialized
    assert len(world.calls) == 1 and len(executed) == 1


@pytest.mark.parametrize("loss", ["before_dispatch", "revoke_inflight", "parent_failure"])
def test_parent_authority_loss_revokes_children_without_fallback(claim_world, monkeypatch, loss):
    world = claim_world
    if loss == "before_dispatch":
        selected = task_resource(world.client.admit_child(child={"child_id": "selected", "capability_id": CHILD,
            "capability_digest": world.host.capabilities[CHILD].capability_digest}, inputs={}, ordered_inputs=[],
            derived_inputs=[], stage="prepare", input_refs=[], **world.context))
        world.source.revoke()
        monkeypatch.setattr(world.host, "_new_claim_slot", lambda: pytest.fail("revoked parent spawned child"))
        with pytest.raises(ChildBridgeError, match="parent authority ended"):
            world.source._dispatch_local_stage({}, selected)
        assert world.calls == []
        assert task_resource(world.client.task(selected["task_id"]))["state"] == "queued"
        return
    entered = threading.Event()
    slots = []
    def execute(slot, task, **kwargs):
        slots.append(slot)
        entered.set()
        assert slot._shutdown.wait(5), "revocation did not contain delegated execution"
        if loss == "revoke_inflight":
            return slot.client.fail(task["task"]["id"], kwargs["lease_token"], "revoked CPU fixture",
                retryable=False, attempt_id=kwargs["attempt_id"], fence=kwargs["fence"])
        return {"cancelled": True}
    monkeypatch.setattr(GenericPackHost, "run_task", execute)
    selected = world.sdk.submit(child_request(world))
    assert entered.wait(5)
    try:
        if loss == "parent_failure":
            c = world.context
            world.client.fail(c["task_id"], c["lease_id"], "parent authority ended", retryable=False,
                              attempt_id=c["attempt_id"], fence=c["fence"])
            with pytest.raises(ChildBridgeError):
                world.source._live()
        else:
            world.source.revoke()
    finally:
        world.source.revoke()
        world.source._children["selected"]["claim_thread"].join(5)
    assert slots[0]._shutdown.is_set()
    assert not world.source._children["selected"]["claim_thread"].is_alive()
    assert world.host._handoff_active_attempts == 0
    with pytest.raises(ChildBridgeError, match="parent authority ended"):
        world.source._dispatch_local_stage({}, task_resource(world.client.task(selected["task_id"])))
    assert len(world.calls) == 1 and world.calls[0]["path"].endswith("/claim-delegated-child")
    child = task_resource(world.client.task(selected["task_id"]))
    assert child["state"] == ("cancel_requested" if loss == "parent_failure" else "failed")


def test_external_cancellation_acknowledgement_is_not_terminal_proof(claim_world, monkeypatch):
    world = claim_world
    calls, terminals = [], []
    def execute(host, task, *args, _terminal, **kwargs):
        terminals.append((host, _terminal))
        def cancel(task_id, **options):
            calls.append(task_id)
            # Existing owner RPC supplies an acknowledged external request;
            # product code gains no owner credential or fallback.
            current = world.owner.get_task(task_id)
            return world.owner.cancel_task(task_id, expected_version=current.version,
                                           idempotency_key="custody-external-cancel")
        monkeypatch.setattr(host.client, "cancel", cancel)
        monkeypatch.setattr(host.client, "fail", lambda *a, **kw: pytest.fail("opposite failure mutation"))
        _terminal["force_abort"]("cancelled")
        return {"cancelled": True}
    monkeypatch.setattr(GenericPackHost, "_run_task_impl", execute)
    selected = world.sdk.submit(child_request(world))
    world.source._children["selected"]["claim_thread"].join(5)
    assert calls == [selected["task_id"]]
    assert task_resource(world.client.task(selected["task_id"]))["state"] == "cancel_requested"
    host, terminal = terminals[0]
    assert terminal["completion_state"] == "unknown"
    receipt = json.loads((terminal["root"] / "result.json").read_text())
    assert receipt["publication"]["status"] == "unconfirmed"
    assert receipt["completion_diagnostic"] == {"status": "unconfirmed"}
    assert not host._handoff_observation_known


def test_native_render_uses_exact_verified_inputs_and_proves_cleanup(tmp_path, monkeypatch, real_runtime):
    """Run one supported Remotion task from its exact admitted Runtime objects.

    This is deliberately an integration proof, not a direct renderer call: it
    mutates the caller-side files after admission and requires the host to use
    the immutable Runtime media/timeline objects, settle that exact lease once,
    and leave neither a renderer task lease nor a retained native process.
    """
    from tests.integrations import test_generic_host_remotion_render as native

    problems = native.remotion_install_problems()
    assert not problems, "a supported native Remotion backend is required: " + "; ".join(problems)
    node = native.shutil.which("node")
    assert node is not None
    node_version = native.subprocess.run([node, "--version"], check=True, text=True,
                                         capture_output=True).stdout.strip()
    assert node_version == "v20.19.4"

    with native._execution_env():
        for name in list(native.os.environ):
            if name.startswith(("DYLD_", "LD_")) or name in native._REMOTION_UNSUPPORTED_OVERRIDES:
                monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ASTRID_NODE_EXECUTABLE", str(native.Path(node).resolve()))
        monkeypatch.setenv("ASTRID_TIMELINE_SCHEMA_PYTHONPATH", str(native.SCHEMA_ROOT))
        monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(native.REMOTION.resolve()))
        environment = dict(native.os.environ)
        launch = native._prepare_render_validation(
            native.ROOT,
            environment,
            tmp_path / "support",
            native.sys.executable,
            (native.ROOT / "astrid" / "packs" / "rendering",),
        )
        assert launch is not None
        for name in (
            native.ASTRID_REMOTION_VALIDATION_PATH,
            native.ASTRID_REMOTION_VALIDATION_SHA256,
            native.ASTRID_REMOTION_VALIDATION_LAUNCH,
            native.ASTRID_REMOTION_VALIDATION_ROLE,
        ):
            monkeypatch.setenv(name, environment[name])

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        timeline_path, assets_path = native._make_media(workspace)
        original_timeline = timeline_path.read_bytes()
        original_registry = json.loads(assets_path.read_text(encoding="utf-8"))
        original_media = {
            filename: (workspace / "media" / filename).read_bytes()
            for filename in ("black.mp4", "silence.m4a")
        }
        daemon = real_runtime()
        host = None
        try:
            generated = native.WorkspaceClient(daemon.endpoint, daemon.token)
            generated.handshake(
                "native-custody-exact-inputs",
                "0.1.0",
                [
                    "projects:read", "projects:write", "objects:read", "objects:write",
                    "tasks:read", "tasks:write",
                ],
            )
            project = generated.create_project(
                "Native custody exact inputs",
                slug="native-custody-exact-inputs",
                idempotency_key="native-custody-project",
            )
            project_id = str(project["project_id"])

            registry = copy.deepcopy(original_registry)
            input_object_ids = []
            admitted_media = {}
            for key, filename in (("black", "black.mp4"), ("silence", "silence.m4a")):
                imported = generated.ingest_project_object(
                    project_id,
                    original_media[filename],
                    media_type=registry["assets"][key]["type"],
                    idempotency_key=f"native-custody-{key}",
                    filename=filename,
                )
                object_id = imported.get("object_id") if isinstance(imported, dict) else imported.object_id
                digest = imported.get("digest") if isinstance(imported, dict) else imported.digest
                assert isinstance(object_id, str) and isinstance(digest, str)
                input_object_ids.append(object_id)
                admitted_media[key] = {"object_id": object_id, "digest": digest, "filename": filename}
                registry["assets"][key]["media_id"] = object_id
                registry["assets"][key]["content_sha256"] = digest.removeprefix("sha256:")
                assert generated.get_object(digest).data == original_media[filename]

            assets_path.write_text(json.dumps(registry), encoding="utf-8")
            managed = {}
            for name, path in (("timeline", timeline_path), ("assets_registry", assets_path)):
                imported = generated.ingest_project_object(
                    project_id,
                    path.read_bytes(),
                    media_type="application/json",
                    idempotency_key=f"native-custody-{name}",
                    filename=path.name,
                )
                object_id = imported.get("object_id") if isinstance(imported, dict) else imported.object_id
                digest = imported.get("digest") if isinstance(imported, dict) else imported.digest
                assert isinstance(object_id, str) and isinstance(digest, str)
                input_object_ids.append(object_id)
                managed[name] = {"object_id": object_id, "digest": digest}

            host_client = native.RuntimeProtocolClient(daemon.endpoint, daemon.worker_token)
            host = native.GenericPackHost(
                pack_roots=[native.ROOT / "astrid" / "packs" / "rendering"],
                client=host_client,
                executor_id="astrid-pack-host",
                attempt_root=tmp_path / "attempt",
            )
            record = next(item for item in host.discover() if item.id == "rendering.render")
            host.preflight(record.id)
            assert host.capabilities[record.id].ready is True
            assert host.register()["registration"].executor_id == "astrid-pack-host"

            spec = {
                "inputs": {
                    "timeline": managed["timeline"],
                    "assets_registry": managed["assets_registry"],
                    "selector": "rendering.remotion",
                    "backend_config": {
                        "rendering.remotion": {
                            "project_dir": str(native.REMOTION),
                            "composition_id": "TimelineComposition",
                        }
                    },
                    "output_name": "native-custody.mp4",
                }
            }
            task = generated.admit_task(
                capability_id=record.id,
                capability_digest=record.capability_digest,
                input_object_ids=input_object_ids,
                idempotency_key="native-custody-task",
                project_id=project_id,
                spec=spec,
            )
            admitted = host_client.task(task.task_id)
            assert list(admitted.input_object_ids) == input_object_ids
            assert admitted.spec["inputs"] == spec["inputs"]
            assert admitted.spec["inputs"]["timeline"] == managed["timeline"]
            assert admitted.spec["inputs"]["assets_registry"] == managed["assets_registry"]
            assert set(input_object_ids) == {
                admitted_media["black"]["object_id"], admitted_media["silence"]["object_id"],
                managed["timeline"]["object_id"], managed["assets_registry"]["object_id"],
            }

            claim = host_client.claim_next(
                executor_id="astrid-pack-host",
                capability_ids=[record.id],
                idempotency_key="native-custody-claim",
            )
            assert claim is not None and claim["task_id"] == task.task_id
            # Mutating every local source after admission must not change the
            # render: native execution is authorized by the pinned Runtime
            # objects above, not these caller-side paths.
            timeline_path.write_text('{"tampered": true}', encoding="utf-8")
            assets_path.write_text('{"assets": {}}', encoding="utf-8")
            for filename in original_media:
                (workspace / "media" / filename).write_bytes(b"tampered-local-media")

            settlement_calls = []
            settle_attempt = host_client.generated.settle_attempt

            def observe_settlement(attempt_id, settlement, **kwargs):
                settlement_calls.append((attempt_id, copy.deepcopy(settlement), dict(kwargs)))
                return settle_attempt(attempt_id, settlement, **kwargs)

            monkeypatch.setattr(host_client.generated, "settle_attempt", observe_settlement)
            claimed_task = host_client.task(task.task_id)
            settled = host.run_task(
                {
                    "task": {
                        "run_id": claimed_task.run_id,
                        "runtime_epoch": getattr(claimed_task, "runtime_epoch", None) or claim["runtime_epoch"],
                        "id": task.task_id,
                        "capability": record.id,
                        "spec": claim.spec,
                        "input_object_ids": claim.input_object_ids,
                        "project_id": claim.project_id,
                        "attempt_id": claim["attempt_id"],
                        "fence": claim["fence"],
                    }
                },
                lease_token=claim["lease_id"],
                attempt_id=claim["attempt_id"],
                fence=int(claim["fence"]),
            )
            assert settled.state == "succeeded"
            assert generated.get_task(task.task_id).state == "succeeded"
            assert len(settlement_calls) == 1
            settled_attempt, settlement, options = settlement_calls[0]
            assert settled_attempt == claim["attempt_id"]
            assert settlement["attempt_id"] == claim["attempt_id"]
            assert settlement["fence"] == int(claim["fence"])
            assert options["idempotency_key"] == f"settle-{claim['attempt_id']}-{claim['fence']}"

            output = tmp_path / "attempt" / "outputs" / "native-custody.mp4"
            provenance = output.with_suffix(".mp4.provenance.json")
            assert output.is_file() and output.stat().st_size > 0
            assert provenance.is_file() and provenance.stat().st_size > 0
            receipt = json.loads((tmp_path / "attempt" / "result.json").read_text(encoding="utf-8"))
            assert receipt["status"] == "succeeded"
            assert receipt["publication"]["status"] == "settled"
            assert host._handoff_active_attempts == host._handoff_pending_settlements == 0
            assert not host._active_processes
            assert host._remotion_host_session is not None
            assert not host._remotion_host_session._leases
            host.shutdown()
            assert not host._active_processes and not host._host_lifetime_processes
            assert not host._unresolved_task_scopes
        finally:
            if host is not None and not host._shutdown.is_set():
                host.shutdown()
            daemon.stop()
