"""CPU-only physical ownership and private delivery contract tests."""

from __future__ import annotations

import asyncio
import dataclasses
import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import Mock

import pytest

from astrid.packs.runpod import worker_process as subject

MODULE = Path(subject.__file__).resolve()


@pytest.fixture(autouse=True)
def pidfd_probe(monkeypatch: Any) -> dict[str, Any]:
    """Exact stop reads Runtime's startup probe; tests here fake the pidfd calls themselves."""
    report: dict[str, Any] = {"pidfd": "available", "ok": True}
    monkeypatch.setattr(subject, "process_control_report", lambda: report)
    return report
EVIDENCE = "sha256:" + "e" * 64


def binding(**changes: Any) -> subject.WorkerBinding:
    return subject.WorkerBinding(
        **{
            "account_ref": "account-test",
            "pod_id": "pod-test",
            "target": "selected-target",
            "runtime_instance_id": "runtime-test",
            "runtime_epoch": 2,
            "runtime_session_id": "session-test",
            "executor_id": "astrid-pack-host",
            **changes,
        }
    )


class LocalTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, Any]]] = []
        self.uploads: list[tuple[Path, str, int]] = []
        self.failure: str | None = None

    async def call(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append((operation, request))
        value = subject.dispatch(operation, request)
        if self.failure == "ack" and operation == "verify_secret":
            return {**value, "inode": value["inode"] + 1}
        return value

    async def upload_private(self, local_path: Path, remote_path: str) -> None:
        self.uploads.append((local_path, remote_path, stat.S_IMODE(local_path.stat().st_mode)))
        if self.failure == "upload":
            raise subject.WorkerProcessError("private upload failure")
        # Match the production pre-reserved, non-truncating SFTP file mode.
        with local_path.open("rb") as local, Path(remote_path).open("r+b") as remote:
            shutil.copyfileobj(local, remote)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_physical_key_ignores_target_runtime_executor_and_epoch() -> None:
    original = binding()
    other = binding(
        target="other-target",
        runtime_instance_id="runtime-other",
        executor_id="other",
        runtime_epoch=3,
        runtime_session_id="session-other",
    )
    assert original.physical_key == other.physical_key
    assert original.physical_key != binding(pod_id="other-pod").physical_key
    assert original.physical_key != binding(account_ref="other-account").physical_key


def test_worker_binding_preserves_structured_runpod_target_and_rejects_nested_secrets() -> None:
    target = {
        "kind": "runpod",
        "pod_id": "pod-test",
        "provider_account_ref": "account-test",
        "storage": {"network_volume_id": "volume-test", "mount_path": "/workspace"},
    }
    assert binding(target=target).target == target
    with pytest.raises(subject.WorkerProcessError, match="secret fields"):
        binding(target={**target, "storage": {"credential_ref": "secret"}})


def test_actual_exec_child_keeps_lock_after_coordinator_exits(tmp_path: Path, owned_process_groups) -> None:
    """A separate coordinator exits; its exec child retains the actual flock."""
    root = tmp_path / "owners"
    pid_path = tmp_path / "host.pid"
    fixture = (
        "import json,os,subprocess,sys; "
        f"sys.path.insert(0,{str(MODULE.parents[3])!r}); "
        "from astrid.packs.runpod.worker_process import WorkerBinding,TargetProcessLock; "
        f"owner=TargetProcessLock(WorkerBinding(**json.loads(sys.argv[1])),{str(root)!r}); "
        "host=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "pass_fds=(owner.fd,),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,start_new_session=True); "
        f"open({str(pid_path)!r},'w').write(str(host.pid)); owner.close()"
    )
    coordinator = subprocess.run(
        [sys.executable, "-c", fixture, json.dumps(dataclasses.asdict(binding()))],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert coordinator.returncode == 0, coordinator.stderr
    pid = int(pid_path.read_text())
    try:
        # No local journal is shared, and an entirely different Runtime tries.
        with pytest.raises(subject.WorkerOwnerConflict):
            subject.TargetProcessLock(binding(runtime_instance_id="different-runtime"), str(root))
    finally:
        # The nested coordinator launched this host in a new session, so its
        # PID is also the group's stable, directly observed PGID.
        owned_process_groups.add_group(pid)
        owned_process_groups.terminate_all()


def test_lock_rejects_symlink_root_and_symlink_file(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(subject.WorkerProcessError):
        subject.TargetProcessLock(binding(), str(alias))
    lock_path = real / (binding().physical_key + ".lock")
    lock_path.symlink_to(tmp_path / "unrelated")
    with pytest.raises(OSError):
        subject.TargetProcessLock(binding(), str(real))


def handle(tmp_path: Path) -> subject.WorkerProcessHandle:
    root = tmp_path / "owners"
    root.mkdir(mode=0o700)
    return subject.WorkerProcessHandle(
        binding(),
        "incarnation-test",
        str(root / (binding().physical_key + ".json")),
        str(root / (binding().physical_key + ".lock")),
        1234,
        "boot:567",
        1234,
        1234,
        7,
    )


def test_independent_process_observation_rejects_pid_birth_drift(
    tmp_path: Path, monkeypatch: Any
) -> None:
    owned = handle(tmp_path)
    Path(owned.marker_path).write_text(json.dumps(owned.as_dict()))
    monkeypatch.setattr(
        subject,
        "process_snapshot",
        lambda pid: {"pid": pid, "birth_id": "boot:999", "pgid": 1234, "sid": 1234},
    )
    monkeypatch.setattr(subject, "_lock_owned_by", lambda value: True)
    with pytest.raises(subject.WorkerProcessError, match="identity or kernel lock"):
        subject.observe_owned_process(owned)


def test_stop_pins_process_and_never_signals_after_identity_drift(
    tmp_path: Path, monkeypatch: Any
) -> None:
    owned = dataclasses.replace(
        handle(tmp_path),
        pid=os.getpid(),
        birth_id=subject.process_birth_identity(os.getpid()) or "birth-current",
        pgid=os.getpgrp(),
        sid=os.getsid(0),
    )
    Path(owned.marker_path).write_text(json.dumps(owned.as_dict()))
    events: list[Any] = []
    monkeypatch.setattr(
        subject.platform, "pidfd_open", lambda pid: events.append(("pin", pid)) or 77, raising=False
    )
    monkeypatch.setattr(
        subject.platform, "pidfd_send_signal", lambda *args: events.append(("signal", args)), raising=False
    )
    monkeypatch.setattr(os, "close", lambda fd: events.append(("close", fd)))
    states = iter(["same", "different"])
    monkeypatch.setattr(subject, "_exact_incarnation_state", lambda _handle: next(states))
    result = subject.stop_owned_process(owned)
    assert result["stopped"] is True and result["already_exited"] is True
    assert events == [("pin", os.getpid()), ("close", 77)]


@pytest.mark.parametrize("pidfd", ["enosys", "unsupported", "error:EPERM"])
@pytest.mark.parametrize("process_control", ["strict", "sandbox"])
def test_stop_has_no_pid_or_group_fallback(
    tmp_path: Path, monkeypatch: Any, pidfd_probe: dict[str, Any], pidfd: str, process_control: str
) -> None:
    # Pods run a full kernel: even sandbox mode's checked-PID fallback is not used here.
    pidfd_probe.update(pidfd=pidfd, process_control=process_control)
    monkeypatch.setattr(subject.platform, "pidfd_open", lambda *_: pytest.fail("pidfd opened"))
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("numeric PID fallback"))
    with pytest.raises(subject.WorkerProcessError, match="no PID fallback"):
        subject.stop_owned_process(handle(tmp_path))


@pytest.mark.parametrize("operation", ["open", "send"])
@pytest.mark.parametrize("error", [errno.ENOSYS, errno.EPERM, errno.ESRCH])
def test_stop_native_pidfd_failure_keeps_exact_handle_and_closes_it(
    tmp_path: Path, monkeypatch: Any, operation: str, error: int
) -> None:
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("numeric PID fallback"))
    monkeypatch.setattr(os, "killpg", lambda *_: pytest.fail("numeric process-group fallback"), raising=False)
    opened = Mock(side_effect=OSError(error, "native pidfd failure")) if operation == "open" else Mock(return_value=77)
    sent = Mock(side_effect=OSError(error, "native pidfd failure"))
    monkeypatch.setattr(subject.platform, "pidfd_open", opened)
    monkeypatch.setattr(subject.platform, "pidfd_send_signal", sent)
    monkeypatch.setattr(subject, "_exact_incarnation_state", lambda _: "same")
    monkeypatch.setattr(subject, "observe_owned_process", lambda _: {"observed": True})
    closed = Mock()
    monkeypatch.setattr(subject.os, "close", closed)
    if operation == "open":
        with pytest.raises(subject.WorkerProcessError, match="could not be pinned"):
            subject.stop_owned_process(handle(tmp_path))
        closed.assert_not_called()
        sent.assert_not_called()
    else:
        if error == errno.ESRCH:
            # An ESRCH signal still requires exit proof on the original pidfd.
            waited = Mock(return_value=([], [], []))
            monkeypatch.setattr(subject.select, "select", waited)
            with pytest.raises(subject.WorkerProcessError, match="exit remains unresolved"):
                subject.stop_owned_process(handle(tmp_path))
            waited.assert_called_once_with([77], [], [], 10)
        else:
            with pytest.raises(OSError) as failure:
                subject.stop_owned_process(handle(tmp_path))
            assert failure.value.errno == error
        closed.assert_called_once_with(77)
        sent.assert_called_once_with(77, subject.platform.TERM)
    opened.assert_called_once_with(1234)


def test_stop_refuses_owner_identity_failure_after_pinning(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    opened, sent, closed = Mock(return_value=77), Mock(), Mock()
    monkeypatch.setattr(subject.platform, "pidfd_open", opened)
    monkeypatch.setattr(subject.platform, "pidfd_send_signal", sent)
    monkeypatch.setattr(subject.os, "close", closed)
    monkeypatch.setattr(subject, "_exact_incarnation_state", lambda _: "same")
    monkeypatch.setattr(subject, "observe_owned_process", Mock(side_effect=subject.WorkerProcessError("identity differs")))
    with pytest.raises(subject.WorkerProcessError, match="identity differs"):
        subject.stop_owned_process(handle(tmp_path))
    opened.assert_called_once_with(1234)
    sent.assert_not_called()
    closed.assert_called_once_with(77)


def test_observation_proves_process_but_does_not_claim_runtime_ready(
    tmp_path: Path, monkeypatch: Any
) -> None:
    owned = handle(tmp_path)
    Path(owned.marker_path).write_text(json.dumps(owned.as_dict()))
    monkeypatch.setattr(
        subject,
        "process_snapshot",
        lambda pid: {"pid": pid, "birth_id": "boot:567", "pgid": 1234, "sid": 1234},
    )
    monkeypatch.setattr(subject, "_lock_owned_by", lambda value: True)
    observed = subject.observe_owned_process(owned)
    assert observed["provider_identity"] == {"account_ref": "account-test", "pod_id": "pod-test"}
    assert observed["process"]["birth_id"] == "boot:567"
    assert "ready" not in observed and "source_closure_digest" not in observed


def test_owner_rejects_handle_from_other_runtime_without_transport(tmp_path: Path) -> None:
    transport = LocalTransport()
    owner = subject.WorkerProcessOwner(
        transport, binding(runtime_instance_id="different"), ownership_root=str(tmp_path / "owners")
    )
    with pytest.raises(subject.WorkerProcessError, match="different runtime"):
        run(owner.stop(handle(tmp_path)))
    assert transport.calls == []


def test_release_fence_is_exact_idempotent_and_blocks_future_launches(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = tmp_path / "owners"
    root.mkdir(mode=0o700)
    fixture_root = tmp_path / "fixture"
    fixture_root.mkdir()
    owned = dataclasses.replace(
        handle(fixture_root),
        marker_path=str(root / (binding().physical_key + ".json")),
        lock_path=str(root / (binding().physical_key + ".lock")),
    )
    Path(owned.marker_path).write_text(json.dumps(owned.as_dict()))
    observed: list[str] = []
    monkeypatch.setattr(
        subject,
        "observe_owned_process",
        lambda value: observed.append(value.incarnation) or {"incarnation": value.incarnation},
    )
    receipt = subject.dispatch("retire_for_release", {
        "handle": owned.as_dict(),
        "operation_id": "release-operation-test",
        "ownership_root": str(root),
    })
    assert receipt == {
        "retired": True,
        "operation_id": "release-operation-test",
        "provider": "runpod",
        "account_ref": "account-test",
        "pod_id": "pod-test",
        "incarnation": "incarnation-test",
        "release_fence_digest": receipt["release_fence_digest"],
    }
    assert len(receipt["release_fence_digest"]) == len("sha256:") + 64
    # The fixture PID is already absent on this host, so exact retirement
    # proves absence under the shared target lock without a live observation.
    assert observed == []

    # Resume after the host is already gone: the durable fence is sufficient;
    # it must not inspect or contact the absent host again.
    Path(owned.marker_path).unlink()
    assert subject.dispatch("retire_for_release", {
        "handle": owned.as_dict(),
        "operation_id": "release-operation-test",
        "ownership_root": str(root),
    }) == receipt
    assert observed == []

    request = {
        "binding": dataclasses.asdict(binding()),
        "incarnation": "replacement-incarnation",
        "ownership_root": str(root),
        "python_executable": sys.executable,
        "argv": [sys.executable, "-c", "pass"],
        "cwd": str(tmp_path),
        "env_items": {},
    }
    monkeypatch.setattr(
        subject.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("launch must be refused before spawning"),
    )
    with pytest.raises(subject.WorkerOwnerConflict, match="retired for release"):
        subject.launch_owned_process(request)

    with pytest.raises(subject.WorkerOwnerConflict, match="different release operation"):
        subject.dispatch("retire_for_release", {
            "handle": owned.as_dict(),
            "operation_id": "other-release-operation",
            "ownership_root": str(root),
        })


@pytest.mark.parametrize("failure", [None, "upload", "ack"])
def test_private_delivery_bound_ack_and_local_cleanup(tmp_path: Path, failure: str | None) -> None:
    token = b"fixture-secret-that-must-never-appear-in-argv-env-or-markers"
    transport = LocalTransport()
    transport.failure = failure
    owner = subject.WorkerProcessOwner(
        transport, binding(), ownership_root=str(tmp_path / "owners")
    )
    operation_root = str(tmp_path / "projected-reference-operation")
    action = owner.deliver_credential(
        token,
        activation_id="activation-test",
        incarnation="incarnation-test",
        evidence_digest=EVIDENCE,
        credential_ref="file:" + str(Path(operation_root) / "executor.token"),
    )
    if failure:
        with pytest.raises(subject.WorkerProcessError):
            run(action)
        assert not (Path(operation_root) / "executor.token").exists()
    else:
        ack = run(action)
        assert ack["activation_id"] == "activation-test"
        assert ack["incarnation"] == "incarnation-test"
        assert ack["evidence_digest"] == EVIDENCE
        assert ack["file_identity"]["sha256"] == "sha256:" + hashlib.sha256(token).hexdigest()
        assert ack["file_identity"]["mode"] == 0o600
        assert (Path(operation_root) / "executor.token").read_bytes() == token
        assert stat.S_IMODE(Path(operation_root).stat().st_mode) == 0o700
    for local, _, mode in transport.uploads:
        assert mode == 0o600
        assert not local.exists() and not local.parent.exists()
    assert token.decode() not in json.dumps(transport.calls)
    assert all(op not in {"activate", "enable_credentials"} for op, _ in transport.calls)


def test_private_delivery_journals_reservation_before_upload_and_removal_replays(tmp_path: Path) -> None:
    events: list[str] = []

    class OrderedTransport(LocalTransport):
        async def upload_private(self, local_path: Path, remote_path: str) -> None:
            events.append("upload")
            await super().upload_private(local_path, remote_path)

    transport = OrderedTransport()
    owner = subject.WorkerProcessOwner(
        transport, binding(), ownership_root=str(tmp_path / "owners")
    )
    destination = tmp_path / "tasks" / "task-one" / "executor.token"
    ack = run(owner.deliver_credential(
        b"private-token", activation_id="activation-one", incarnation="incarnation-one",
        evidence_digest=EVIDENCE, credential_ref=str(destination),
        reservation_callback=lambda fact: events.append(
            "journal" if fact["size"] == 0 else "unexpected"
        ),
    ))
    assert events == ["journal", "upload"]
    assert subject.dispatch("remove_secret", {"file_identity": ack["file_identity"]}) == {"removed": True}
    assert subject.dispatch("remove_secret", {"file_identity": ack["file_identity"]}) == {"removed": True}
    assert not destination.parent.exists()


def test_empty_unjournaled_reservation_can_be_removed_but_secret_cannot(tmp_path: Path) -> None:
    destination = tmp_path / "tasks" / "task-one" / "executor.token"
    empty = subject.dispatch("reserve_secret", {"operation_root": str(destination.parent)})
    assert subject.dispatch("remove_unissued_secret", {
        "credential_path": str(destination),
    }) == {"removed": True}
    assert not destination.parent.exists()

    reserved = subject.dispatch("reserve_secret", {"operation_root": str(destination.parent)})
    destination.write_bytes(b"not-empty")
    with pytest.raises(subject.WorkerProcessError, match="not empty"):
        subject.dispatch("remove_unissued_secret", {"credential_path": str(destination)})
    assert destination.read_bytes() == b"not-empty"
    assert empty["mode"] == reserved["mode"] == 0o600


def test_private_delivery_rejects_wrong_leaf_before_transport(tmp_path: Path) -> None:
    transport = LocalTransport()
    owner = subject.WorkerProcessOwner(
        transport, binding(), ownership_root=str(tmp_path / "owners")
    )
    with pytest.raises(subject.WorkerProcessError, match="executor.token leaf"):
        run(
            owner.deliver_credential(
                b"secret",
                activation_id="activation-test",
                incarnation="host-test",
                evidence_digest=EVIDENCE,
                credential_ref=str(tmp_path / "operation" / "wrong.token"),
            )
        )
    assert transport.calls == []


def test_secret_verifier_rejects_replacement_or_mode_drift(tmp_path: Path) -> None:
    root = tmp_path / "operation"
    reserved = subject.dispatch("reserve_secret", {"operation_root": str(root)})
    path = Path(reserved["path"])
    path.write_bytes(b"secret")
    # Rename keeps the original inode allocated so this proves replacement.
    path.rename(root / "replaced-original")
    path.write_bytes(b"secret")
    path.chmod(0o600)
    request = {
        "file_identity": reserved,
        "sha256": "sha256:" + hashlib.sha256(b"secret").hexdigest(),
        "size": 6,
    }
    with pytest.raises(subject.WorkerProcessError, match="identity differs"):
        subject.dispatch("verify_secret", request)
    with pytest.raises(subject.WorkerProcessError):
        subject.dispatch("remove_secret", {"file_identity": reserved})
    assert path.read_bytes() == b"secret"
    path.chmod(0o644)
    with pytest.raises(subject.WorkerProcessError, match="0600"):
        subject._secret_fact(path)


@pytest.mark.linux_native
def test_linux_owned_host_survives_launcher_and_exact_stop(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    transport = LocalTransport()
    owner = subject.WorkerProcessOwner(
        transport, binding(), ownership_root=str(tmp_path / "owners")
    )
    owned = run(
        owner.launch(
            python_executable=sys.executable,
            argv=[sys.executable, "-c", "import time; time.sleep(30)"],
        )
    )
    try:
        assert run(owner.reconcile()) == owned
        assert run(owner.observe(owned))["process"]["pid"] == owned.pid
        other = subject.WorkerProcessOwner(
            transport, binding(runtime_instance_id="other"), ownership_root=str(tmp_path / "owners")
        )
        with pytest.raises(subject.WorkerOwnerConflict):
            run(other.reconcile())
        with pytest.raises(subject.WorkerOwnerConflict):
            run(
                other.launch(
                    python_executable=sys.executable,
                    argv=[sys.executable, "-c", "import time; time.sleep(30)"],
                )
            )
    finally:
        run(owner.stop(owned))


@pytest.mark.parametrize("epoch", [0, -1, True, "2"])
def test_epoch_requires_positive_integer(epoch: Any) -> None:
    with pytest.raises(subject.WorkerProcessError, match="positive integer"):
        binding(runtime_epoch=epoch)


def test_exec_environment_has_no_ambient_secrets(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("AMBIENT_SECRET_TEST", "must-not-cross")
    snapshots: list[dict[str, str]] = []
    monkeypatch.setattr(
        subject,
        "process_snapshot",
        lambda pid: {"pid": pid, "birth_id": "boot:123", "pgid": pid, "sid": pid},
    )

    def capture(executable: str, argv: list[str], env: dict[str, str]) -> None:
        snapshots.append(env)
        raise RuntimeError("exec replaced by local fixture")

    monkeypatch.setattr(os, "execvpe", capture)
    request = {
        "binding": dataclasses.asdict(binding()),
        "ownership_root": str(tmp_path / "owners"),
        "incarnation": "fixture-host",
        "argv": [sys.executable],
        "env_items": {"ASTRID_RUNTIME_INSTANCE_ID": "runtime-test"},
    }
    try:
        with pytest.raises(RuntimeError, match="local fixture"):
            subject._exec_owned_host(request)
        assert snapshots == [
            {"PATH": "/usr/local/bin:/usr/bin:/bin", "ASTRID_RUNTIME_INSTANCE_ID": "runtime-test"}
        ]
        marker_path = tmp_path / "owners" / (binding().physical_key + ".json")
        marker = json.loads(marker_path.read_text())
        assert "must-not-cross" not in json.dumps(marker)
    finally:
        if snapshots:
            os.close(marker["lock_fd"])


def test_runtime_projection_checks_binding_and_accepts_nonsecret_configuration(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    class CapturingTransport(LocalTransport):
        async def call(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
            self.calls.append((operation, request))
            owned = handle(tmp_path)
            return dataclasses.replace(
                owned, binding=binding(target={"kind": "runpod", "id": "pod-test"})
            ).as_dict()

    transport = CapturingTransport()
    selected = binding(target={"kind": "runpod", "id": "pod-test"})
    owner = subject.WorkerProcessOwner(transport, selected, ownership_root=str(tmp_path / "owners"))
    reference = SimpleNamespace(
        effective_target=selected.target,
        runtime_instance_id="runtime-test",
        runtime_epoch=2,
        executor_id="astrid-pack-host",
        executable=SimpleNamespace(path=Path(sys.executable)),
        digest=lambda: EVIDENCE,
    )
    launch = SimpleNamespace(
        deployment_digest=EVIDENCE,
        argv=(sys.executable, "-m", "some.host"),
        cwd=tmp_path,
        env_items=(
            ("ASTRID_RUNTIME_INSTANCE_ID", "runtime-test"),
            ("ASTRID_CREDENTIAL_REF", "file:/selected/executor.token"),
            ("ASTRID_RUNTIME_DATA_ROOT", "/selected/data"),
        ),
    )
    preparation = SimpleNamespace(reference=reference, launch=launch)
    assert run(owner.launch_projected(preparation)).binding == selected
    assert (
        transport.calls[0][1]["env_items"]["ASTRID_CREDENTIAL_REF"]
        == "file:/selected/executor.token"
    )
    reference.runtime_epoch = 3
    with pytest.raises(subject.WorkerProcessError, match="exact selected binding"):
        run(owner.launch_projected(preparation))
    assert len(transport.calls) == 1


def test_pod_adapter_uses_command_metadata_and_private_preexisting_sftp_file(
    tmp_path: Path,
) -> None:
    import base64
    import shlex

    class Sftp:
        def __init__(self) -> None:
            self.closed = False
            self.modes: list[str] = []

        def open(self, path: str, mode: str) -> Any:
            self.modes.append(mode)
            return Path(path).open(mode)

        def close(self) -> None:
            self.closed = True

    class Client:
        def __init__(self) -> None:
            self.sftp = Sftp()
            self.closed = False

        def open_sftp(self) -> Sftp:
            return self.sftp

        def close(self) -> None:
            self.closed = True

    class Pod:
        id = "pod-test"

        def __init__(self) -> None:
            self.commands: list[str] = []
            self.clients: list[Client] = []

        async def exec_ssh(self, command: str, timeout: int) -> tuple[int, str, str]:
            self.commands.append(command)
            args = shlex.split(command)
            compile(args[2], "<remote-wrapper>", "exec")
            request = json.loads(base64.b64decode(args[-1]))
            value = subject.remote_dispatch(args[-2], request)
            return 0, json.dumps(value), ""

        def open_ssh_client(self) -> Client:
            client = Client()
            self.clients.append(client)
            return client

    pod = Pod()
    transport = subject.RunPodWorkerProcessTransport(
        pod,
        account_ref="account-test",
        source_root=str(MODULE.parents[3]),
        python_executable=sys.executable,
    )
    owner = subject.WorkerProcessOwner(
        transport, binding(), ownership_root=str(tmp_path / "owners")
    )
    operation_root = str(tmp_path / "projected-private-operation")
    token = b"specific-private-token-bytes"
    ack = run(
        owner.deliver_credential(
            token,
            activation_id="activate-test",
            incarnation="host-test",
            evidence_digest=EVIDENCE,
            credential_ref="file:" + str(Path(operation_root) / "executor.token"),
        )
    )
    assert ack["file_identity"]["mode"] == 0o600
    assert all(token.decode() not in command for command in pod.commands)
    assert all(client.closed and client.sftp.closed for client in pod.clients)
    assert pod.clients[0].sftp.modes == ["r+b"]
    with pytest.raises(subject.WorkerProcessError, match="provider/account/pod"):
        run(
            transport.call(
                "reconcile",
                {
                    "binding": dataclasses.asdict(binding(pod_id="different")),
                    "ownership_root": str(tmp_path / "owners"),
                },
            )
        )


@pytest.mark.parametrize("exited", [True, False])
def test_stop_requires_pinned_exit_proof_before_stopped_receipt(
    tmp_path: Path, monkeypatch: Any, exited: bool
) -> None:
    owned = handle(tmp_path)
    events: list[Any] = []
    monkeypatch.setattr(subject.platform, "pidfd_open", lambda pid: 77, raising=False)
    monkeypatch.setattr(
        subject.platform, "pidfd_send_signal", lambda fd, sig: events.append(("signal", fd)), raising=False
    )
    monkeypatch.setattr(os, "close", lambda fd: events.append(("close", fd)))
    monkeypatch.setattr(subject, "observe_owned_process", lambda value: {"observed": True})
    monkeypatch.setattr(
        subject.select,
        "select",
        lambda read, write, errors, timeout: ([77] if exited else [], [], []),
    )
    states = iter(["same", "same", "different"] if exited else ["same", "same"])
    monkeypatch.setattr(subject, "_exact_incarnation_state", lambda _handle: next(states))
    if exited:
        result = subject.stop_owned_process(owned, timeout_seconds=0.01)
        assert result["stopped"] is True and "signal_sent" not in result
        assert result["birth_id"] == owned.birth_id
    else:
        with pytest.raises(subject.WorkerProcessError, match="exit remains unresolved"):
            subject.stop_owned_process(owned, timeout_seconds=0.01)
    assert events == [("signal", 77), ("close", 77)]


@pytest.mark.parametrize(
    "credential_ref",
    [
        "relative/executor.token",
        "/selected/../other/executor.token",
        "file:///selected/executor.token",
        "/executor.token",
    ],
)
def test_credential_reference_must_be_canonical_and_operation_scoped(
    tmp_path: Path, credential_ref: str
) -> None:
    transport = LocalTransport()
    owner = subject.WorkerProcessOwner(
        transport, binding(), ownership_root=str(tmp_path / "owners")
    )
    with pytest.raises(subject.WorkerProcessError):
        run(
            owner.deliver_credential(
                b"secret",
                activation_id="activation-test",
                incarnation="incarnation-test",
                evidence_digest=EVIDENCE,
                credential_ref=credential_ref,
            )
        )
    assert transport.calls == [] and transport.uploads == []


def test_exact_reference_destination_rejects_reuse_without_overwriting(tmp_path: Path) -> None:
    transport = LocalTransport()
    owner = subject.WorkerProcessOwner(
        transport, binding(), ownership_root=str(tmp_path / "owners")
    )
    # The reference selects this path before any host incarnation or activation.
    destination = tmp_path / "reference-selected-operation" / "executor.token"
    ack = run(
        owner.deliver_credential(
            b"original-secret",
            activation_id="first-activation",
            incarnation="first-incarnation",
            evidence_digest=EVIDENCE,
            credential_ref=str(destination),
        )
    )
    assert ack["file_identity"]["path"] == str(destination)
    assert ack["provider_identity"] == {"account_ref": "account-test", "pod_id": "pod-test"}
    for incarnation in ("first-incarnation", "different-incarnation"):
        with pytest.raises(subject.WorkerProcessError, match="refusing ambiguous reuse"):
            run(
                owner.deliver_credential(
                    b"replacement-secret",
                    activation_id="second-activation",
                    incarnation=incarnation,
                    evidence_digest=EVIDENCE,
                    credential_ref=str(destination),
                )
            )
    assert destination.read_bytes() == b"original-secret"
    assert len(transport.uploads) == 1


def test_empty_preexisting_credential_directory_is_not_adopted(tmp_path: Path) -> None:
    root = tmp_path / "preexisting-operation"
    root.mkdir(mode=0o700)
    transport = LocalTransport()
    owner = subject.WorkerProcessOwner(
        transport, binding(), ownership_root=str(tmp_path / "owners")
    )
    with pytest.raises(subject.WorkerProcessError, match="refusing ambiguous reuse"):
        run(
            owner.deliver_credential(
                b"secret",
                activation_id="activation-test",
                incarnation="incarnation-test",
                evidence_digest=EVIDENCE,
                credential_ref=str(root / "executor.token"),
            )
        )
    assert list(root.iterdir()) == [] and transport.uploads == []


def test_ambiguous_nonempty_reservation_is_not_written(tmp_path: Path) -> None:
    class AmbiguousTransport(LocalTransport):
        async def call(self, operation: str, request: Mapping[str, Any]) -> Mapping[str, Any]:
            value = await super().call(operation, request)
            if operation == "reserve_secret":
                Path(value["path"]).write_bytes(b"unknown-existing-secret")
                return subject._secret_fact(Path(value["path"]))
            return value

    transport = AmbiguousTransport()
    owner = subject.WorkerProcessOwner(
        transport, binding(), ownership_root=str(tmp_path / "owners")
    )
    destination = tmp_path / "operation" / "executor.token"
    with pytest.raises(subject.WorkerProcessError, match="ambiguous file"):
        run(
            owner.deliver_credential(
                b"new-secret",
                activation_id="activation-test",
                incarnation="incarnation-test",
                evidence_digest=EVIDENCE,
                credential_ref=str(destination),
            )
        )
    assert destination.read_bytes() == b"unknown-existing-secret" and transport.uploads == []
