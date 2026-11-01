"""Real parked receiver against the locally selected Runtime owner contract."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import (
    GenericPackHost,
    RuntimeProtocolClient,
    _await_enabled_runtime_credential,
    source_checkout_closure_digest,
    source_checkout_digest,
)
from astrid.core.gateway.dispatch import compose_profile_handoff
from astrid.core.generation.model_root import canonical_model_inventory_digest
from astrid.sdk import host_bootstrap
from astrid_runtime_client import ApiError

pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)

OWNER = {"actor": "owner", "scopes": ["admin"]}


@pytest.fixture
def resident_host(tmp_path, real_runtime, monkeypatch):
    """CPU fixture: actual host/OS/artifacts and one resident authority store."""
    source = Path(__file__).resolve().parents[2]
    data = tmp_path / "data"
    support = data / "runtime"
    support.mkdir(parents=True)
    models = tmp_path / "models"
    models.mkdir()
    imports = tmp_path / "python-libs"
    imports.mkdir()
    # The canonical, non-symlink interpreter consumes these measured packages.
    # Avoid changing/installing anything in the shared venv or Runtime checkout.
    # This host runs a CPU pack and never renders. A source checkout with a Remotion install would make the
    # launch validate Remotion with the canonical interpreter, which has none of the venv's packages; leave
    # the host without a provisioned Remotion (its documented "unavailable capability" launch).
    monkeypatch.setattr(host_bootstrap, "_provision_render_runtime_env", lambda *_args: None)
    owners = importlib.metadata.packages_distributions()
    for name in ("PIL", "yaml", "dotenv", "jsonschema", "attrs", "attr", "referencing", "rpds",
                 "jsonschema_specifications", "typing_extensions"):
        package = importlib.import_module(name)
        if hasattr(package, "__path__"):
            shutil.copytree(Path(package.__file__).parent, imports / name,
                            ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(package.__file__, imports / Path(package.__file__).name)
        # Linux wheels keep the shared libraries their extension modules load (Pillow's libjpeg, ...) in a
        # sibling "<dist>.libs" directory; a package copied without it cannot be imported by the host.
        for owner in owners.get(name, ()):
            distribution = importlib.metadata.distribution(owner)
            for libs in {Path(item.parts[0]) for item in distribution.files or () if item.parts[0].endswith(".libs")}:
                if not (imports / libs).exists():
                    shutil.copytree(Path(distribution.locate_file(libs)), imports / libs)
    pack = tmp_path / "cpu-pack"
    pack.mkdir()
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "cpu.activation", "name": "CPU activation",
        "kind": "external", "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c",
                              "from pathlib import Path; "
                              f"assert Path('{{out}}').is_relative_to({str(tmp_path / 'outputs')!r}); "
                              "Path('{out}/answer.txt').write_text('accepted')"]},
        "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt",
                     "artifact_type": "text/plain"}],
        "metadata": {"adapter_family": "cpu", "resource_keys": ["cpu"]},
    }))
    profile = support / "profile.json"
    profile.write_text(json.dumps({
        "launch": {"model_root": {"schema_version": 1, "path": str(models), "inventory": [],
                                  "inventory_digest": canonical_model_inventory_digest([])}},
        "verified_facts": {"exact": {}, "minimum": {}},
    }))
    session = support / "session.json"
    session.write_text(json.dumps({"session_ref": "cpu-local-session", "capacity": 1}))
    boot = support / "boot-manifest.json"
    handoff = compose_profile_handoff(boot, support_root=support)
    record = GenericPackHost(pack_roots=[pack]).discover()[0]
    daemon = real_runtime(tmp_path / "realm", support_root=support)
    # The Runtime is importable only once real_runtime has located it.
    from runtime_protocol.remote_worker_activation import QualifiedRemoteWorkerLauncher
    from runtime_protocol.remote_worker_deployment import (
        ArtifactReference,
        DeploymentReference,
        deployment_binding_from_task,
        project_launch,
    )

    # Runtime proves a "runpod" placement with a two-lane attached child this host does not have; the
    # local CPU fixture is a "machine" placement, observed from this machine's own OS.
    target = {"kind": "machine", "id": socket.gethostname()}
    def verify_local_target(actual):
        assert actual == target
    preparer = host_bootstrap._ParkedPackHostPreparer(
        project_launch, timeout_seconds=10, verify_target=verify_local_target,
    )
    original_abort = preparer.abort
    try:
        service = daemon.service
        service.register_capability({"capability_id": record.id, "definition_digest": record.capability_digest})
        project = service.create_project({"slug": "activation", "name": "Activation"},
                                         idempotency_key="activation-project")
        task_id = service.create_task({
            "project": project["id"],
            "capability_id": record.id, "capability_digest": record.capability_digest,
            "input_object_ids": [], "spec": {"inputs": {}}, "idempotency_key": "cpu-parked-receiver",
            "execution_request": {"schema_version": 1, "target": target},
        }, enforce_readiness=True)["task"]["id"]
        task = service._task_resource(service.store.get_task(task_id))
        binding = deployment_binding_from_task(task)
        executable = Path(sys.executable).resolve()
        artifacts = tuple(sorted((ArtifactReference(name, path, host_bootstrap._host_artifact_digest(path))
                          for name, path in (("python", executable), ("imports", imports), ("cpu-pack", pack))),
                          key=lambda item: item.name))
        health_client = RuntimeProtocolClient(daemon.endpoint, daemon.token)
        health = health_client.health()
        reference = DeploymentReference(
            deployment_id="cpu-receiver", revision="cpu-receiver-1", task_id=task_id,
            run_id=binding.admission_identity.run_id,
            target_ref="machine:" + target["id"], effective_target_ref="machine:" + target["id"],
            executable=next(item for item in artifacts if item.name == "python"), dependency_closure=artifacts,
            source_closure_digest="sha256:" + source_checkout_closure_digest(source),
            data_root=data, support_root=support, runtime_endpoint=daemon.endpoint,
            runtime_instance_id=daemon.instance_id, runtime_epoch=health.runtime_epoch,
            runtime_schema_digest=health.schema_digest, model_root=models, capacity=1,
            session_ref="cpu-local-session", session_config_digest="sha256:" + hashlib.sha256(session.read_bytes()).hexdigest(),
            output_root=tmp_path / "outputs", credential_ref=str(daemon.worker_credential_path),
            executor_id=host_bootstrap.PACK_HOST_ACTOR, boot_manifest_path=boot,
            boot_manifest_hash="sha256:" + handoff["sha256"], readiness_profile_path=profile,
            readiness_profile_hash="sha256:" + hashlib.sha256(profile.read_bytes()).hexdigest(),
            source_checkout=source, source_checkout_digest=source_checkout_digest(source),
            pack_roots=(pack,), ready_file=support / "receiver.ready.json",
            admission_identity=binding.admission_identity, capability_identity=binding.capability_identity,
            input_bindings=binding.input_bindings, original_target=target, effective_target=target,
            execution_target=target,
        )
        reference.output_root.mkdir()
        def runtime_health():
            value = vars(health_client.health())
            return {**value, "runtime_session_id": service.runtime_session_id}
        def executor_observer(executor_id):
            with service.store._mutex:
                row = service.store.conn.execute("SELECT * FROM executors WHERE id=?", (executor_id,)).fetchone()
                return service.store._executor_result(row) if row is not None else None
        inspector = host_bootstrap._ParkedPackHostInspector(
            runtime_health, session_config_path=session,
            executor_observer=executor_observer,
        )
        launcher = QualifiedRemoteWorkerLauncher(
            runtime=service, credentials=None, preparer=preparer, inspector=inspector,
            credential_control=lambda task_id, body: daemon.remote_credential_control(task_id, body, identity=OWNER),
            scopes=host_bootstrap.PACK_HOST_SCOPES,
        )
        yield SimpleNamespace(daemon=daemon, service=service, task_id=task_id, task=task,
                              reference=reference, preparer=preparer, inspector=inspector, launcher=launcher)
    finally:
        if preparer._handle is not None:
            original_abort(preparer._handle)
        daemon.stop()


def test_real_parked_process_inspection_preserves_identity_guards(resident_host, monkeypatch):
    witness = resident_host
    handle = witness.preparer.prepare(witness.reference)
    observed = witness.inspector.observe(handle)
    assert observed["process"]["pid"] == handle.process.pid
    assert observed["process"]["birth_id"] == handle.state["process_birth_id"]
    assert observed["process"]["uid"] == os.getuid()

    original_command = host_bootstrap._host_command
    command = original_command(handle.process.pid)
    import shlex
    argv = shlex.split(command)
    monkeypatch.setattr(host_bootstrap, "_host_command", lambda pid: shlex.join([
        "/unrelated/Python", *argv[1:],
    ]))
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="executable, uid or birth"):
        witness.inspector.observe(handle)
    monkeypatch.setattr(host_bootstrap, "_host_command", original_command)

    original_run = host_bootstrap.subprocess.run
    def foreign_uid(argv, **kwargs):
        result = original_run(argv, **kwargs)
        if argv[-1] == "uid=":
            result.stdout = str(os.getuid() + 1)
        return result
    monkeypatch.setattr(host_bootstrap, "subprocess", SimpleNamespace(
        **{**vars(subprocess), "run": foreign_uid},
    ))
    with pytest.raises(host_bootstrap.PackHostBootstrapError, match="executable, uid or birth"):
        witness.inspector.observe(handle)
    monkeypatch.setattr(host_bootstrap, "subprocess", subprocess)

    birth = handle.state["process_birth_id"]
    try:
        handle.state["process_birth_id"] = "a different process incarnation"
        with pytest.raises(host_bootstrap.PackHostBootstrapError, match="OS identity changed"):
            witness.inspector.observe(handle)
    finally:
        handle.state["process_birth_id"] = birth


@pytest.mark.parametrize("lost_reply", [False, True])
def test_bootstrap_register_observe_commit_and_same_incarnation_recovery(resident_host, monkeypatch, lost_reply):
    """Resident acceptance is committed over the private channel while the credential is still disabled."""
    witness = resident_host
    parked = witness.launcher.park(witness.reference, target=witness.reference.effective_target)
    events = []
    acknowledge = witness.preparer.acknowledge
    observe_ready = witness.inspector.observe_ready
    record = witness.service.record_remote_activation

    def credential_is_disabled(grant):
        token = Path(grant["credential_file"]).read_text().strip()
        with pytest.raises(ApiError) as denied:
            RuntimeProtocolClient(witness.daemon.endpoint, token)._authenticate_worker(witness.reference.executor_id)
        assert denied.value.status == 401

    def private_ack(handle, grant, *, accept=None):
        assert accept is not None  # a machine placement requires Runtime's acceptance receipt
        credential_is_disabled(grant)
        assert witness.service._latest_remote_activation(witness.task_id) is None
        events.append("ack_started")

        def checked_accept(received, process):
            credential_is_disabled(grant)
            assert witness.service._latest_remote_activation(witness.task_id) is None
            accept(received, process)
            assert witness.service._latest_remote_activation(witness.task_id) is not None
            events.append("accepted")

        result = acknowledge(handle, grant, accept=checked_accept)
        events.append("acknowledged")
        return result

    def ready(handle):
        observation = observe_ready(handle)
        assert events[-1] == "acknowledged"
        # Only after the private ACK does Runtime enable the credential the host registers with.
        RuntimeProtocolClient(witness.daemon.endpoint, witness.daemon.worker_credential_path.read_text().strip()
                              )._authenticate_worker(witness.reference.executor_id)
        events.append("ready")
        return observation

    def commit(task_id, qualification, **kwargs):
        first = "commit" not in events
        if first:
            assert events == ["ack_started"]
        result = record(task_id, qualification, **kwargs)
        events.append("commit" if first else "replayed_commit")
        if lost_reply and first:
            raise EOFError("committed response lost")  # Runtime replays the identical request
        return result

    monkeypatch.setattr(witness.preparer, "acknowledge", private_ack)
    monkeypatch.setattr(witness.inspector, "observe_ready", ready)
    monkeypatch.setattr(witness.service, "record_remote_activation", commit)
    qualification = witness.launcher.activate(witness.task, witness.reference, parked)
    assert witness.launcher.activation_state == "active"
    assert events == ["ack_started", "commit", *(["replayed_commit"] if lost_reply else []),
                      "accepted", "acknowledged", "ready"]
    assert witness.service._latest_remote_activation(witness.task_id) == qualification
    assert host_bootstrap._parked_host_identity_matches(parked.handle.state)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = witness.service.task(witness.task_id)["task"]
        if task["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)
    assert task["status"] == "completed", task
    with witness.service.store._mutex:
        assert witness.service.store.conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=?", (witness.task_id,)).fetchone()[0] == 1
    outputs = witness.service.managed_outputs(witness.task_id)
    assert len(outputs) == 1
    assert witness.service.object(outputs[0]["object_id"])[1] == b"accepted"
    assert witness.service.store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE task_id=? AND kind='task.remote_activation_qualified'",
        (witness.task_id,),
    ).fetchone()[0] == 1


@pytest.mark.parametrize("failure", ["ack", "cleanup"])
def test_precommit_failure_revokes_before_stopping_exact_host(resident_host, monkeypatch, failure):
    """A private ACK that fails before Runtime accepts leaves nothing committed: revoke, then stop the host."""
    witness = resident_host
    parked = witness.launcher.park(witness.reference, target=witness.reference.effective_target)
    abort = witness.preparer.abort
    stopped = []
    def fail(*args, **kwargs):
        raise OSError("deliberate precommit failure")
    monkeypatch.setattr(witness.preparer, "acknowledge", fail)
    def stop(handle):
        assert witness.service._latest_remote_activation(witness.task_id) is None
        assert witness.daemon.credentials.actor_metadata(witness.reference.executor_id) is None
        stopped.append(handle)
        if failure == "cleanup":
            raise OSError("cannot confirm exact cleanup")
        abort(handle)
    monkeypatch.setattr(witness.preparer, "abort", stop)
    with pytest.raises(OSError):
        witness.launcher.activate(witness.task, witness.reference, parked)
    assert stopped == [parked.handle]
    cleanup_uncertain = failure == "cleanup" or parked.handle.cleanup_error is not None
    assert witness.launcher.activation_state == ("unknown" if cleanup_uncertain else "inactive")
    assert witness.service.task(witness.task_id)["task"]["status"] == "queued"


def _owned_parked_handle(tmp_path):
    """A real child in its own group, wrapped as the parked preparer's retained handle."""
    process = host_bootstrap.popen_owned_group(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    control = SimpleNamespace(close=lambda: None)
    preparer = host_bootstrap._ParkedPackHostPreparer(lambda reference: None)
    handle = host_bootstrap._ParkedPackHostHandle(
        process, control, None,
        {"pid": process.pid, "process_birth_id": host_bootstrap._host_birth_identity(process.pid),
         "ready_file": str(tmp_path / "not-ready.json")},
        "test-operation", "test-channel",
    )
    preparer._handle = handle
    return process, preparer, handle


def _forbid_group_and_census(monkeypatch):
    """Abort acts on the retained child tree: no numeric-group signal, no identity census."""
    def forbidden(*args, **kwargs):
        pytest.fail("abort of a retained child must not signal a numeric group or re-observe its identity")
    monkeypatch.setattr(os, "killpg", forbidden)
    monkeypatch.setattr(host_bootstrap, "_parked_host_identity_matches", forbidden)


def test_abort_stops_the_retained_child_without_a_census_or_group_signal(tmp_path, monkeypatch):
    """Since retained process custody (32b044360) the owned child is stopped by its own handle."""
    process, preparer, handle = _owned_parked_handle(tmp_path)
    try:
        _forbid_group_and_census(monkeypatch)
        preparer.abort(handle)
        assert handle.aborted and handle.cleanup_error is None
        assert process.poll() is not None
        preparer.abort(handle)  # a second abort of the same handle is a no-op
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        process.wait(timeout=3)


@pytest.mark.parametrize("has_primary", [True, False], ids=["primary-error", "no-primary-error"])
def test_abort_that_cannot_verify_cleanup_keeps_the_primary_failure_and_the_uncertainty(
    tmp_path, monkeypatch, has_primary,
):
    """A cleanup that cannot be verified is retained; the original failure stays primary."""
    process, preparer, handle = _owned_parked_handle(tmp_path)
    primary = OSError("original private ACK failed")
    uncertain = host_bootstrap.PackHostBootstrapError("host process tree cannot be verified safely")

    def unverifiable(_process):
        raise uncertain
    try:
        _forbid_group_and_census(monkeypatch)
        monkeypatch.setattr(host_bootstrap, "terminate_group", unverifiable)
        if has_primary:
            with pytest.raises(OSError) as caught:
                try:
                    raise primary
                except OSError:
                    preparer.abort(handle)
                    raise
            assert caught.value is primary and primary.__cause__ is uncertain
        else:
            with pytest.raises(host_bootstrap.PackHostBootstrapError) as caught:
                preparer.abort(handle)
            assert caught.value is uncertain
        assert handle.cleanup_error is uncertain and not handle.aborted
        assert process.poll() is None  # nothing was stopped on uncertain evidence
    finally:
        monkeypatch.undo()
        if not process.stdin.closed:
            process.stdin.close()
        process.wait(timeout=3)


@pytest.mark.parametrize("changed", ["ready_marker", "registration", "machine"])
def test_independent_readiness_rejects_changed_evidence(resident_host, monkeypatch, changed):
    """Evidence that changes after acceptance never reports active; Runtime keeps the exact generation."""
    from runtime_protocol.errors import ConflictError

    witness = resident_host
    parked = witness.launcher.park(witness.reference, target=witness.reference.effective_target)
    await_ready = witness.preparer.await_ready
    def changed_after_start(handle):
        await_ready(handle)
        if changed == "ready_marker":
            marker = json.loads(witness.reference.ready_file.read_text())
            marker["output_root"] = "/foreign/output"
            witness.reference.ready_file.write_text(json.dumps(marker))
        elif changed == "registration":
            monkeypatch.setattr(witness.inspector, "executor_observer", lambda executor_id: None)
        else:
            monkeypatch.setattr(host_bootstrap.socket, "gethostname", lambda: "a-different-machine")
    monkeypatch.setattr(witness.preparer, "await_ready", changed_after_start)
    with pytest.raises((host_bootstrap.PackHostBootstrapError, ConflictError)):
        witness.launcher.activate(witness.task, witness.reference, parked)
    # Acceptance was already committed over the private channel: from there no exception authorizes
    # revoking the credential or stopping the host, and the launcher stays unresolved, never active.
    assert witness.launcher.activation_state == "unknown"
    assert witness.launcher.activation_host == parked
    assert witness.service._latest_remote_activation(witness.task_id) is not None
    assert witness.daemon.credentials.actor_metadata(witness.reference.executor_id) is not None
    assert not parked.handle.aborted and parked.handle.cleanup_error is None


def test_uncertain_commit_preserves_exact_host_and_credential(resident_host, monkeypatch):
    """Acceptance whose reply is lost twice may have committed: keep the generation, never revoke or stop."""
    from runtime_protocol.errors import ConflictError

    witness = resident_host
    parked = witness.launcher.park(witness.reference, target=witness.reference.effective_target)
    record = witness.service.record_remote_activation
    def commits_but_reply_is_lost(*args, **kwargs):
        record(*args, **kwargs)
        raise OSError("commit transport unresolved")
    monkeypatch.setattr(witness.service, "record_remote_activation", commits_but_reply_is_lost)
    monkeypatch.setattr(witness.preparer, "abort", lambda handle: pytest.fail("uncertain commit must not stop host"))
    # Runtime cannot prove the commit failed: its exact cleanup refuses, leaving everything in place.
    with pytest.raises(ConflictError, match="reconcile before cleanup"):
        witness.launcher.activate(witness.task, witness.reference, parked)
    assert witness.launcher.activation_state == "unknown"
    assert witness.launcher.activation_host == parked
    assert witness.service._latest_remote_activation(witness.task_id) is not None
    assert witness.daemon.credentials.actor_metadata(witness.reference.executor_id) is not None
    assert not parked.handle.aborted and parked.handle.cleanup_error is None
    assert host_bootstrap._parked_host_identity_matches(parked.handle.state)


def test_real_disabled_bearer_fails_handshake_and_enabled_bearer_becomes_ready(real_runtime):
    daemon = real_runtime()
    try:
        token, _path = daemon.credentials.provision(
            "activation-host", ["handshake", "worker:execute"], enabled=False,
        )
        client = RuntimeProtocolClient(daemon.endpoint, token)
        assert client.health().status == "ok"
        with pytest.raises(ApiError) as rejected:
            client._authenticate_worker("activation-host")
        assert rejected.value.status == 401
        daemon.credentials.enable_actor("activation-host")
        _await_enabled_runtime_credential(client, executor_id="activation-host", timeout_seconds=1)
        daemon.credentials.disable_actor("activation-host")
        with pytest.raises(ApiError) as rejected:
            client._authenticate_worker("activation-host")
        assert rejected.value.status == 401
    finally:
        daemon.stop()


@pytest.mark.darwin_native  # the control channel pins its peer by Darwin audit token (custody_broker._peer_token)
def test_local_host_control_is_separate_and_survives_multiple_bounded_requests(monkeypatch):
    import socket
    from astrid.core.execution.generic_host import LocalExecutionPreparation, _read_activation_frame, _send_activation_frame
    control_parent, control_child = socket.socketpair()
    activation_parent, activation_child = socket.socketpair()
    service = LocalExecutionPreparation(operation_id="op", channel_id="channel")
    calls = []
    def dispatch(frame):
        calls.append(frame)
        return {"version": "runtime.local-execution-host/v1", "status": "prepared", "ordinal": len(calls)}
    monkeypatch.setattr(service, "dispatch", dispatch)
    thread = service.serve_control(control_child.detach())
    try:
        control_parent.settimeout(1)
        for ordinal in (1, 2):
            _send_activation_frame(control_parent, {"command": "report_local_execution", "operation_id": "op"})
            assert _read_activation_frame(control_parent)["ordinal"] == ordinal
        activation_parent.sendall(b"activation-remains-separate")
        assert activation_child.recv(100) == b"activation-remains-separate"
        assert len(calls) == 2
    finally:
        control_parent.close(); activation_parent.close(); activation_child.close()
        thread.join(timeout=1)
    assert service.control_error is not None  # EOF is uncertainty, never exit proof.
