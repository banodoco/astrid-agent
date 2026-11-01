"""Concrete RunPod owner joining existing staging, process and Runtime seams.

The P1 claim record remains the durable machine-custody record. This adapter
adds only the current prepared GenericPackHost handle and activation generation
to that record; it does not create another task/result ledger or supervisor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator, Mapping

from runtime_protocol import platform
from runtime_protocol.dirfd import filesystem as os

from astrid.core.execution.platform_helpers import owned_by_current_user

from .worker_preparation import (
    _checkpoint_release_progress,
    _release_exact_target_after_quiescence,
    replace_target_record,
)
from .worker_process import (
    RunPodWorkerProcessTransport,
    WorkerBinding,
    WorkerProcessError,
    WorkerProcessHandle,
    WorkerProcessOwner,
    WorkerProcessTransport,
)
from .worker_staging import (
    RunPodPreparationTransport,
    WorkerPreparationTransport,
    _canonical_digest,
    prepare_worker,
)


class RunPodWorkerOwnerError(RuntimeError):
    """Exact RunPod preparation or host custody could not be established."""


def _run_sync(coroutine: Any) -> Any:
    """Bridge existing async RunPod APIs to Runtime's synchronous preparer API."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # Runtime's public preparer contract is synchronous. If an embedding
    # caller already owns an event loop, run the I/O coroutine on a short-lived
    # worker thread rather than nesting or patching that caller's loop.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="astrid-runpod-io") as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _absolute(value: str, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise RunPodWorkerOwnerError(f"{label} must be a canonical absolute path")
    return path


def _credential_destination(value: str) -> str:
    if not isinstance(value, str):
        raise RunPodWorkerOwnerError("deployment credential reference is invalid")
    path_value = value[5:] if value.startswith("file:") else value
    path = _absolute(path_value, label="deployment credential reference")
    if path.name != "executor.token" or path.parent == Path("/"):
        raise RunPodWorkerOwnerError(
            "RunPod credential reference must name executor.token in a fresh operation directory"
        )
    return str(path)


def _read_credential(path_value: str) -> bytes:
    path = Path(path_value).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise RunPodWorkerOwnerError("Runtime credential source path is unsafe")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise RunPodWorkerOwnerError("Runtime credential source is unavailable") from exc
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or not owned_by_current_user(fd, before, mode=0o600)):
            raise RunPodWorkerOwnerError("Runtime credential source must be an owned regular 0600 file")
        with os.fdopen(os.dup(fd), "rb") as stream:
            value = stream.read(1024 * 1024 + 1)
        after = os.fstat(fd)
        current = os.stat(path, follow_symlinks=False)
        if (len(value) > 1024 * 1024 or not value or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or
                (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
            raise RunPodWorkerOwnerError("Runtime credential source changed during private read")
        return value.strip()
    finally:
        os.close(fd)


def _claim_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RunPodWorkerOwnerError("exact P1 claim handle is unavailable")
    return path


@dataclass
class PreparedRunPodHost:
    reference: Any
    owner: WorkerProcessOwner
    process: WorkerProcessHandle
    operation_id: str
    channel_id: str
    socket_path: str
    staging: Mapping[str, Any]
    ready_file: str
    session_selection: Any | None = None
    session_receipt: Any | None = None
    persisted: dict[str, Any] = field(default_factory=dict)
    activation_id: str | None = None
    evidence_digest: str | None = None
    credential_source_file: str | None = None
    credential_ack: Mapping[str, Any] | None = None

    @property
    def incarnation(self) -> str:
        return self.process.incarnation


class RunPodRemoteWorkerPreparer:
    """Runtime ``RemotePreparer`` for one already-claimed exact RunPod pod.

    P1 owns allocation/attachment, P2 owns selected staging evidence, and the
    injected ProcessOwner owns the host incarnation. This class sequences
    those existing owners and checkpoints the exact handle in the P1 record.
    """

    def __init__(
        self,
        pod: Any,
        *,
        provider_account_ref: str,
        runtime_session_id: str,
        claim_handle_path: str | Path,
        journal_dir: str | Path,
        staging_inputs: Mapping[str, Any],
        process_transport: WorkerProcessTransport | None = None,
        preparation_transport: WorkerPreparationTransport | None = None,
        session_owner: Any | None = None,
        session_port: int = 8188,
        process_timeout_seconds: int = 30,
        activation_timeout_seconds: int = 120,
        ready_timeout_seconds: int = 300,
    ) -> None:
        if not 0 < activation_timeout_seconds <= 900 or not 0 < ready_timeout_seconds <= 3600:
            raise ValueError("RunPod host startup timeouts must be bounded")
        self.pod = pod
        self.provider_account_ref = provider_account_ref
        self.runtime_session_id = runtime_session_id
        self.claim_handle_path = _claim_path(claim_handle_path)
        self.journal_dir = Path(journal_dir).expanduser()
        self.staging_inputs = dict(staging_inputs)
        self.preparation_transport = preparation_transport or RunPodPreparationTransport(pod)
        if type(session_port) is not int or not 1 <= session_port <= 65535:
            raise ValueError("managed VibeComfy session port must be in range")
        self.session_owner = session_owner
        self.session_port = session_port
        self.process_transport = process_transport
        self.process_timeout_seconds = process_timeout_seconds
        self.activation_timeout_seconds = activation_timeout_seconds
        self.ready_timeout_seconds = ready_timeout_seconds
        self._handle: PreparedRunPodHost | None = None

    def _binding(self, reference: Any) -> WorkerBinding:
        target = reference.effective_target
        if (not isinstance(target, Mapping) or target.get("kind") != "runpod"
                or target.get("provider_account_ref") != self.provider_account_ref
                or str(target.get("pod_id")) != str(getattr(self.pod, "id", ""))):
            raise RunPodWorkerOwnerError("Runtime placement differs from the exact acquired RunPod pod")
        return WorkerBinding(
            account_ref=self.provider_account_ref,
            pod_id=str(target["pod_id"]),
            target=dict(target),
            runtime_instance_id=reference.runtime_instance_id,
            runtime_epoch=reference.runtime_epoch,
            runtime_session_id=self.runtime_session_id,
            executor_id=reference.executor_id,
        )

    def _owner(self, reference: Any) -> WorkerProcessOwner:
        binding = self._binding(reference)
        transport = self.process_transport
        if transport is None:
            source_root = reference.source_checkout or reference.executable.path.parent
            transport = RunPodWorkerProcessTransport(
                self.pod,
                account_ref=self.provider_account_ref,
                source_root=str(source_root),
                python_executable=str(reference.executable.path),
                timeout_seconds=self.process_timeout_seconds,
            )
        return WorkerProcessOwner(transport, binding)

    def _stage(self, reference: Any) -> dict[str, Any]:
        inputs = dict(self.staging_inputs)
        fixed = {
            "claim_handle_path": self.claim_handle_path,
            "journal_dir": self.journal_dir,
            "provider_account_ref": self.provider_account_ref,
            "transport": self.preparation_transport,
        }
        if set(inputs) & set(fixed):
            raise RunPodWorkerOwnerError("staging inputs cannot replace P1 custody or transport")
        readiness = inputs.get("readiness_profile")
        if not isinstance(readiness, Mapping) or not isinstance(readiness.get("launch"), Mapping):
            raise RunPodWorkerOwnerError("selected P2 readiness profile is required")
        launch = readiness["launch"]
        if (launch.get("model_root", {}).get("path") != str(reference.model_root)
                or launch.get("output_root") != str(reference.output_root)
                or inputs.get("python_executable") != str(reference.executable.path)
                or inputs.get("capability_id") != reference.capability_identity.capability_id):
            raise RunPodWorkerOwnerError("P2 staging profile differs from the Runtime deployment reference")
        result = _run_sync(prepare_worker(**fixed, **inputs))
        if (result.get("status") != "cpu_ready" or result.get("gpu_qualified") is not False
                or result.get("worker_qualified") is not False):
            raise RunPodWorkerOwnerError("P2 did not return exact CPU-only preparation evidence")
        paths = result.get("cpu_readiness", {}).get("effective_paths", {})
        source_root = paths.get("astrid_root") if isinstance(paths, Mapping) else None
        if reference.source_checkout is None or str(reference.source_checkout) != source_root:
            raise RunPodWorkerOwnerError("Runtime source checkout is not the exact P2 staged Astrid tree")
        tree_facts = result.get("cpu_readiness", {}).get("tree_facts")
        astrid_fact = tree_facts.get("astrid_root") if isinstance(tree_facts, Mapping) else None
        if (not isinstance(astrid_fact, Mapping)
                or astrid_fact.get("tree_digest") != "sha256:" + str(reference.source_checkout_digest)):
            raise RunPodWorkerOwnerError("Runtime source checkout digest differs from exact P2 tree evidence")
        if not reference.pack_roots or any(
            not Path(root).is_relative_to(Path(source_root)) for root in reference.pack_roots
        ):
            raise RunPodWorkerOwnerError("Runtime pack roots must be inside the exact staged Astrid tree")
        expected_ready = reference.ready_file or reference.support_root / "generic-host.ready.json"
        if (not expected_ready.is_relative_to(reference.support_root)
                or expected_ready == reference.support_root):
            raise RunPodWorkerOwnerError("GenericPackHost ready file must stay inside the selected support root")
        return result

    def observe_provider_identity(self, handle: PreparedRunPodHost) -> dict[str, str]:
        """Re-query this account's full pod and volume inventory by exact IDs."""
        self._assert_handle(handle)
        with self._claim_owner():
            return self._observe_provider_identity_locked(handle)

    def _observe_provider_identity_locked(self, handle: PreparedRunPodHost) -> dict[str, str]:
        from .worker_preparation import RunPodTargetContract, observe_exact_target

        claim = self._read_claim()
        required = (
            "pod_id", "gpu_type", "worker_image", "network_volume_id",
            "network_volume_size_gb", "provider_account_ref",
        )
        if any(claim.get(key) is None for key in required):
            raise RunPodWorkerOwnerError("P1 custody lacks the exact provider observation contract")
        if claim["provider_account_ref"] != self.provider_account_ref:
            raise RunPodWorkerOwnerError("P1 provider account differs from the prepared worker")
        config = getattr(self.pod, "config", None)
        api_key = getattr(config, "api_key", None)
        if not isinstance(api_key, str) or not api_key:
            raise RunPodWorkerOwnerError("authenticated RunPod account context is unavailable")
        contract = RunPodTargetContract(
            pod_id=str(claim["pod_id"]),
            expected_pod_name=claim.get("name") or claim.get("request_name"),
            gpu_type=str(claim["gpu_type"]),
            worker_image=str(claim["worker_image"]),
            network_volume_id=str(claim["network_volume_id"]),
            network_volume_name=claim.get("network_volume_name"),
            network_volume_size_gb=int(claim["network_volume_size_gb"]),
            datacenter_id=claim.get("network_volume_datacenter_id"),
        )
        observed = _run_sync(observe_exact_target(api_key, contract))
        if observed.pod_id != str(self.pod.id):
            raise RunPodWorkerOwnerError("authenticated provider observation selected another pod")
        return {"account_ref": self.provider_account_ref, "pod_id": observed.pod_id}

    def observe_staging(self, handle: PreparedRunPodHost) -> dict[str, Any]:
        """Re-run P2's exact remote tree, release, model and workflow checks."""
        self._assert_handle(handle)
        with self._claim_owner():
            current = self._stage(handle.reference)
        if current.get("plan_digest") != handle.staging.get("plan_digest"):
            raise RunPodWorkerOwnerError("P2 selected source or release changed after preparation")
        return current

    def _read_claim(self, *, allowed_states: tuple[str, ...] = ("claimed",)) -> dict[str, Any]:
        try:
            value = json.loads(self.claim_handle_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RunPodWorkerOwnerError("P1 claim record cannot be read safely") from exc
        if not isinstance(value, dict) or value.get("state") not in allowed_states:
            raise RunPodWorkerOwnerError("P1 claim record no longer names the exact claimed pod")
        return value

    @contextmanager
    def _claim_owner(self) -> Iterator[tuple[dict[str, Any], int]]:
        lock_path = self.claim_handle_path.with_name(f".{self.claim_handle_path.name}.owner.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                platform.lock_file(fd, blocking=False)
            except BlockingIOError as exc:
                raise RunPodWorkerOwnerError("another coordinator owns this P1 custody record") from exc
            current = self._read_claim()
            yield current, fd
        finally:
            try:
                platform.unlock_file(fd)
            finally:
                os.close(fd)

    def _write_worker_state(
        self, value: Mapping[str, Any], *, owner_lock_fd: int | None = None,
        replace_previous: bool = False,
    ) -> None:
        def replace(current: Mapping[str, Any]) -> None:
            operation_id = current.get("operation_id")
            if value.get("claim_operation_id") != operation_id:
                raise RunPodWorkerOwnerError("prepared worker changed the P1 claim operation")
            prior = current.get("managed_worker")
            if replace_previous:
                if (isinstance(prior, Mapping)
                        and prior.get("state") not in {"stopped", "released"}):
                    raise RunPodWorkerOwnerError("a different P1 worker generation is still live or unresolved")
            else:
                if (not isinstance(prior, Mapping)
                        or prior.get("process_handle") != value.get("process_handle")
                        or prior.get("activation_operation_id") != value.get("activation_operation_id")
                        or prior.get("deployment_digest") != value.get("deployment_digest")):
                    raise RunPodWorkerOwnerError("P1 worker generation changed before its checkpoint")
            updated = {**current, "managed_worker": dict(value)}
            replace_target_record(
                self.claim_handle_path, updated, operation_id=str(operation_id)
            )

        if owner_lock_fd is not None:
            replace(self._read_claim())
            return

        with self._claim_owner() as (current, _fd):
            replace(current)

    def _write_managed_session(
        self, claim: Mapping[str, Any], selection: Any, receipt: Any,
        *, owner_lock_fd: int,
    ) -> dict[str, Any]:
        if self.session_owner is None:
            raise RunPodWorkerOwnerError("managed VibeComfy session owner is unavailable")
        record = {
            "schema": "astrid.runpod.managed-session.v1",
            "claim_operation_id": claim.get("operation_id"),
            "account_ref": selection.account_ref,
            "pod_id": selection.pod_id,
            "session_ref": selection.session_ref,
            "config_digest": selection.config_digest,
            "output_root": selection.output_root,
            "selection": asdict(selection),
            "receipt": receipt.as_dict(),
            "gpu_qualified": False,
        }
        updated = {**self._read_claim(), "managed_session": record}
        replace_target_record(
            self.claim_handle_path, updated, operation_id=str(claim.get("operation_id"))
        )
        return record

    def record_local_generation(self, generation: Mapping[str, Any]) -> None:
        """Persist the exact caller worker identity before Runtime relinquishes it."""
        required = ("executor_incarnation", "evidence_digest", "profile_id", "workspace_uuid")
        if (not isinstance(generation, Mapping)
                or any(not isinstance(generation.get(key), str) or not generation[key]
                       for key in required)):
            raise RunPodWorkerOwnerError("Runtime local-worker generation is incomplete")
        saved = {key: generation[key] for key in required}
        with self._claim_owner() as (claim, _fd):
            managed = claim.get("managed_worker")
            if (not isinstance(managed, Mapping)
                    or managed.get("schema") != "astrid.runpod.managed-worker.v1"
                    or managed.get("claim_operation_id") != claim.get("operation_id")
                    or managed.get("state") not in {"parked", "parked_process"}):
                raise RunPodWorkerOwnerError("local worker can only be relinquished for this parked generation")
            prior = managed.get("local_worker_generation")
            if prior is not None and prior != saved:
                raise RunPodWorkerOwnerError("P1 custody already records a different local worker generation")
            state = {**dict(managed), "local_worker_generation": saved}
            self._write_worker_state(state)

    def persist_credential_reservation(
        self, handle: object, reservation: Mapping[str, Any],
    ) -> None:
        """Journal the exact empty remote secret before any token bytes are uploaded."""
        handle = self._assert_handle(handle)
        required = {"path", "device", "inode", "mode", "size", "sha256"}
        expected_path = _credential_destination(handle.reference.credential_ref)
        empty_digest = "sha256:" + hashlib.sha256(b"").hexdigest()
        if (not isinstance(reservation, Mapping) or set(reservation) != required
                or reservation.get("path") != expected_path
                or type(reservation.get("device")) is not int
                or type(reservation.get("inode")) is not int
                or reservation.get("mode") != 0o600
                or reservation.get("size") != 0
                or reservation.get("sha256") != empty_digest):
            raise RunPodWorkerOwnerError("remote credential reservation is not the exact empty private file")
        claim = self._read_claim()
        persisted = dict(claim.get("managed_worker") or {})
        qualification = persisted.get("activation_qualification")
        if (persisted.get("deployment_digest") != handle.reference.digest()
                or persisted.get("process_handle") != handle.process.as_dict()
                or persisted.get("state") != "credential"
                or not isinstance(qualification, Mapping)
                or qualification.get("activation_id") != handle.activation_id):
            raise RunPodWorkerOwnerError("credential reservation belongs to another activation checkpoint")
        prior = persisted.get("credential_reservation")
        if prior is not None and dict(prior) != dict(reservation):
            raise RunPodWorkerOwnerError("P1 custody already records another remote credential file")
        persisted["credential_reservation"] = dict(reservation)
        self._write_worker_state(persisted)
        handle.persisted = persisted

    def prepare(self, preparation_launch: Any) -> PreparedRunPodHost:
        with self._claim_owner() as (claim, lock_fd):
            return self._prepare_locked(preparation_launch, claim, lock_fd)

    def _prepare_locked(
        self, preparation_launch: Any, claim: Mapping[str, Any], owner_lock_fd: int
    ) -> PreparedRunPodHost:
        reference = preparation_launch.reference
        launch = preparation_launch.launch
        if (launch.deployment_digest != reference.digest()
                or _credential_destination(reference.credential_ref) != str(
                    Path(reference.credential_ref.removeprefix("file:")).expanduser()
                )):
            raise RunPodWorkerOwnerError("Runtime launch or credential destination is not canonical")
        if (claim.get("pod_id") != str(self.pod.id)
                or claim.get("provider_account_ref") != self.provider_account_ref):
            raise RunPodWorkerOwnerError("P1 custody does not match the selected RunPod pod")
        previous = claim.get("managed_worker")
        if isinstance(previous, Mapping) and previous.get("state") not in {"stopped", "released"}:
            raise RunPodWorkerOwnerError("P1 custody already contains a live or unresolved managed worker")

        staging = self._stage(reference)
        session_selection = None
        session_receipt = None
        if self.session_owner is not None:
            session_selection = self.session_owner.select(
                reference, staging["cpu_readiness"], port=self.session_port,
            )
            session_receipt = self.session_owner.start(session_selection)
            self._write_managed_session(
                claim, session_selection, session_receipt, owner_lock_fd=owner_lock_fd,
            )
        operation_id, channel_id = secrets.token_hex(12), secrets.token_hex(12)
        socket_path = f"/tmp/.a{secrets.token_hex(8)}/s"
        if len(os.fsencode(socket_path)) >= 108:
            raise RunPodWorkerOwnerError("private activation socket path exceeds the Linux path limit")
        ready_file = str(reference.ready_file or (reference.support_root / "generic-host.ready.json"))
        argv = [
            *launch.argv,
            "--source-closure-digest", reference.source_closure_digest.removeprefix("sha256:"),
            "--activation-socket", socket_path,
            "--activation-operation-id", operation_id,
            "--activation-channel-id", channel_id,
            "--activation-timeout-seconds", str(self.activation_timeout_seconds),
        ]
        remote_vibe_root = staging["cpu_readiness"]["effective_paths"]["vibecomfy_root"]
        remote_import_roots = (str(reference.source_checkout), str(remote_vibe_root))
        env_items = dict(launch.env_items)
        selected_pythonpath = os.pathsep.join(remote_import_roots)
        if "PYTHONPATH" in env_items and env_items["PYTHONPATH"] != selected_pythonpath:
            raise RunPodWorkerOwnerError("projected PYTHONPATH differs from the exact staged source roots")
        env_items["PYTHONPATH"] = selected_pythonpath
        projected = replace(
            launch,
            argv=tuple(argv),
            cwd=reference.source_checkout,
            env_items=tuple(sorted(env_items.items())),
        )
        from runtime_protocol.remote_worker_activation import RemotePreparationLaunch

        owner = self._owner(reference)
        process = _run_sync(owner.launch_projected(RemotePreparationLaunch(reference, projected)))
        handle = PreparedRunPodHost(
            reference, owner, process, operation_id, channel_id, socket_path,
            staging, ready_file,
            session_selection=session_selection,
            session_receipt=session_receipt,
        )
        self._handle = handle
        p1_operation = str(claim["operation_id"])
        self._write_worker_state({
            "schema": "astrid.runpod.managed-worker.v1",
            "state": "parked_process",
            "claim_operation_id": p1_operation,
            "pod_id": str(self.pod.id),
            "provider_account_ref": self.provider_account_ref,
            "deployment_digest": reference.digest(),
            "process_handle": process.as_dict(),
            "activation_operation_id": operation_id,
            "activation_channel_id": channel_id,
            "activation_socket_path": socket_path,
            "ready_file": ready_file,
            "staging_plan_digest": staging["plan_digest"],
            "gpu_qualified": False,
        }, owner_lock_fd=owner_lock_fd, replace_previous=True)
        return handle

    def _assert_handle(self, handle: object) -> PreparedRunPodHost:
        if not isinstance(handle, PreparedRunPodHost) or handle is not self._handle:
            raise RunPodWorkerOwnerError("RunPod preparation handle is foreign to this owner")
        return handle

    def persist_parked(self, handle: object, parked: Any) -> None:
        handle = self._assert_handle(handle)
        claim = self._read_claim()
        state = dict(claim.get("managed_worker") or {})
        state.update({
            "state": "parked",
            "observation": dict(parked.observation),
            "evidence_digest": parked.evidence_digest,
            "executor_incarnation": parked.executor_incarnation,
        })
        self._write_worker_state(state)
        handle.persisted = state

    def persist_activation(
        self,
        handle: object,
        qualification: Mapping[str, Any],
        *,
        state: str,
        credential_file: str | None = None,
    ) -> None:
        handle = self._assert_handle(handle)
        claim = self._read_claim()
        persisted = dict(claim.get("managed_worker") or {})
        if persisted.get("deployment_digest") != handle.reference.digest():
            raise RunPodWorkerOwnerError("persisted RunPod deployment identity changed")
        prior = persisted.get("activation_qualification")
        if prior is not None and prior != dict(qualification):
            raise RunPodWorkerOwnerError("RunPod custody already names a different activation generation")
        persisted.update({
            "state": state,
            "activation_qualification": dict(qualification),
        })
        if credential_file is not None:
            persisted["credential_source_file"] = credential_file
            handle.credential_source_file = credential_file
        if handle.credential_ack is not None:
            persisted["credential_ack"] = dict(handle.credential_ack)
        handle.activation_id = str(qualification["activation_id"])
        handle.evidence_digest = str(qualification["evidence_digest"])
        handle.persisted = persisted
        self._write_worker_state(persisted)

    def checkpoint(self, parked: Any, stage: str, details: Mapping[str, Any]) -> None:
        """Persist Runtime's exact activation generation in the existing P1 row."""
        handle = self._assert_handle(getattr(parked, "handle", None))
        stages = {"parked", "qualification", "credential", "acknowledged", "enabled", "ready", "committed"}
        if stage not in stages or details.get("schema_version") != 1:
            raise RunPodWorkerOwnerError("Runtime activation checkpoint has an unsupported shape")
        claim = self._read_claim()
        persisted = dict(claim.get("managed_worker") or {})
        if (persisted.get("deployment_digest") != handle.reference.digest()
                or persisted.get("process_handle") != handle.process.as_dict()
                or persisted.get("activation_operation_id") != handle.operation_id
                or persisted.get("activation_channel_id") != handle.channel_id):
            raise RunPodWorkerOwnerError("P1 custody differs from the prepared RunPod incarnation")

        if stage == "parked":
            if (set(details) != {"schema_version"}
                    or parked.executor_incarnation != handle.incarnation
                    or not isinstance(parked.observation, Mapping)
                    or parked.evidence_digest != _canonical_digest(parked.observation)):
                raise RunPodWorkerOwnerError("parked Runtime checkpoint differs from exact process evidence")
            persisted.update({
                "observation": dict(parked.observation),
                "observation_digest": parked.evidence_digest,
                "executor_incarnation": parked.executor_incarnation,
            })
        else:
            qualification = details.get("qualification")
            if (not isinstance(qualification, Mapping)
                    or qualification.get("executor_incarnation") != handle.incarnation
                    or qualification.get("evidence_digest") != parked.evidence_digest
                    or qualification.get("observation_digest") != _canonical_digest(parked.observation)
                    or qualification.get("deployment_digest") != handle.reference.digest()):
                raise RunPodWorkerOwnerError("Runtime qualification differs from exact parked evidence")
            prior = persisted.get("activation_qualification")
            if prior is not None and prior != dict(qualification):
                raise RunPodWorkerOwnerError("P1 custody already names another activation generation")
            persisted["activation_qualification"] = dict(qualification)
            credential_file = details.get("credential_file")
            if credential_file is not None:
                if not isinstance(credential_file, str) or not Path(credential_file).is_absolute():
                    raise RunPodWorkerOwnerError("Runtime checkpoint credential locator is invalid")
                persisted["credential_source_file"] = credential_file
                handle.credential_source_file = credential_file
            acknowledgement = details.get("acknowledgement")
            if acknowledgement is not None:
                expected = {
                    "activation_id": qualification["activation_id"],
                    "executor_incarnation": handle.incarnation,
                    "evidence_digest": parked.evidence_digest,
                }
                if acknowledgement != expected:
                    raise RunPodWorkerOwnerError("Runtime checkpoint acknowledgement is foreign")
                persisted["runtime_acknowledgement"] = dict(acknowledgement)
            ready_observation = details.get("ready_observation")
            if ready_observation is not None:
                if not isinstance(ready_observation, Mapping) or ready_observation != parked.observation:
                    raise RunPodWorkerOwnerError("Runtime checkpoint readiness observation changed")
                persisted["ready_observation"] = dict(ready_observation)

        persisted["state"] = stage
        persisted["runtime_checkpoint_schema"] = 1
        self._write_worker_state(persisted)
        handle.persisted = persisted
        qualification = persisted.get("activation_qualification")
        if isinstance(qualification, Mapping):
            handle.activation_id = str(qualification["activation_id"])
            handle.evidence_digest = str(qualification["evidence_digest"])

    def acknowledge(self, handle: object, grant: Mapping[str, Any]) -> Mapping[str, Any]:
        handle = self._assert_handle(handle)
        expected_keys = {"activation_id", "credential_file", "executor_incarnation", "evidence_digest"}
        if set(grant) != expected_keys:
            raise RunPodWorkerOwnerError("Runtime activation grant has an invalid shape")
        if (grant["executor_incarnation"] != handle.incarnation
                or not isinstance(grant["activation_id"], str)
                or not isinstance(grant["evidence_digest"], str)):
            raise RunPodWorkerOwnerError("Runtime activation grant differs from the parked host")
        destination = _credential_destination(handle.reference.credential_ref)
        token = _read_credential(str(grant["credential_file"]))
        ack = _run_sync(handle.owner.deliver_credential(
            token,
            activation_id=str(grant["activation_id"]),
            incarnation=handle.incarnation,
            evidence_digest=str(grant["evidence_digest"]),
            credential_ref=destination,
            reservation_callback=lambda reservation: self.persist_credential_reservation(
                handle, reservation,
            ),
        ))
        handle.credential_source_file = str(grant["credential_file"])
        handle.credential_ack = ack
        handle.activation_id = str(grant["activation_id"])
        handle.evidence_digest = str(grant["evidence_digest"])
        self.persist_activation(
            handle,
            {**dict(handle.persisted.get("activation_qualification") or {}), **{
                "activation_id": handle.activation_id,
                "evidence_digest": handle.evidence_digest,
                "executor_incarnation": handle.incarnation,
            }},
            state="credential_delivered",
            credential_file=str(grant["credential_file"]),
        )
        result = _run_sync(handle.owner.bootstrap(
            handle.process,
            socket_path=handle.socket_path,
            operation_id=handle.operation_id,
            channel_id=handle.channel_id,
            grant={
                "activation_id": handle.activation_id,
                "credential_file": destination,
                "executor_incarnation": handle.incarnation,
                "evidence_digest": handle.evidence_digest,
            },
            file_identity=ack["file_identity"],
        ))
        expected = {
            "activation_id": handle.activation_id,
            "executor_incarnation": handle.incarnation,
            "evidence_digest": handle.evidence_digest,
        }
        if dict(result) != expected:
            raise RunPodWorkerOwnerError("remote GenericPackHost bootstrap acknowledgement differs")
        self.persist_activation(
            handle,
            dict(handle.persisted["activation_qualification"]),
            state="bootstrapped",
        )
        return expected

    def await_ready(self, handle: object) -> None:
        handle = self._assert_handle(handle)
        if not handle.activation_id or not handle.evidence_digest:
            raise RunPodWorkerOwnerError("host cannot become ready before exact activation delivery")
        deadline = time.monotonic() + self.ready_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                ready = _run_sync(handle.owner.read_ready(handle.process, handle.ready_file))
            except WorkerProcessError as exc:
                last_error = exc
                break
            if ready is not None:
                activation = ready.get("activation")
                if ready.get("status") == "failed":
                    raise RunPodWorkerOwnerError("GenericPackHost reported startup failure")
                if ready.get("status") == "ready":
                    expected_process = {"pid": handle.process.pid, "birth_id": handle.process.birth_id}
                    if (ready.get("pid") != handle.process.pid
                            or ready.get("process_birth_id") != handle.process.birth_id
                            or not isinstance(activation, Mapping)
                            or activation.get("operation_id") != handle.operation_id
                            or activation.get("channel_id") != handle.channel_id
                            or activation.get("activation_id") != handle.activation_id
                            or activation.get("executor_incarnation") != handle.incarnation
                            or activation.get("evidence_digest") != handle.evidence_digest
                            or activation.get("host") != expected_process):
                        raise RunPodWorkerOwnerError("GenericPackHost readiness names a different activation")
                    persisted = dict(handle.persisted)
                    persisted["state"] = "ready_observed_by_preparer"
                    self._write_worker_state(persisted)
                    handle.persisted = persisted
                    return
            time.sleep(0.05)
        raise RunPodWorkerOwnerError("GenericPackHost did not publish matching readiness") from last_error

    def abort(self, handle: object) -> None:
        handle = self._assert_handle(handle)
        stopped = _run_sync(handle.owner.stop(handle.process))
        if (stopped.get("stopped") is not True
                or stopped.get("pid") != handle.process.pid
                or stopped.get("birth_id") != handle.process.birth_id
                or stopped.get("incarnation") != handle.incarnation):
            raise RunPodWorkerOwnerError("exact GenericPackHost stop was not confirmed")
        if handle.credential_ack is not None:
            removed = _run_sync(handle.owner.remove_credential(
                handle.process, handle.credential_ack["file_identity"]
            ))
            if removed.get("removed") is not True:
                raise RunPodWorkerOwnerError("exact remote Runtime credential cleanup was not confirmed")
        claim = self._read_claim()
        persisted = dict(claim.get("managed_worker") or {})
        persisted["state"] = "stopped"
        persisted["stop_evidence"] = dict(stopped)
        persisted.pop("credential_ack", None)
        self._write_worker_state(persisted)
        handle.persisted = persisted

    async def release(self, reference: Any, *, runtime_owner: Any,
                      resume: bool = False) -> Mapping[str, Any]:
        """Drain and release P1's exact pod under its one custody lock.

        A restart needs the saved activation/process identities, not a live
        host or successful staging. P1 owns all journal writes and provider
        deletion; the callback only checkpoints this exact teardown's progress.
        """
        states = ("claimed", "release_pending", "cleanup_pending")
        attempt = self._read_claim(allowed_states=(*states, "cleanup_complete") if resume else states)
        managed = attempt.get("managed_worker")
        if (not isinstance(managed, Mapping) or managed.get("deployment_digest") != reference.digest()
                or WorkerProcessHandle.from_dict(managed["process_handle"]).binding != self._binding(reference)):
            raise RunPodWorkerOwnerError("release reference differs from persisted machine custody")
        if attempt.get("managed_session") is not None and self.session_owner is None:
            raise RunPodWorkerOwnerError("explicit RunPod release requires the managed VibeComfy session owner")

        async def quiesce(locked_record: Mapping[str, Any]) -> Mapping[str, Any]:
            return await self._quiesce_release_locked(reference, runtime_owner, locked_record)

        return await _release_exact_target_after_quiescence(
            self.pod.config, handle_path=self.claim_handle_path, attempt=attempt,
            quiesce_worker=quiesce, resume=resume,
        )

    async def retire_task(
        self, reference: Any, *, runtime_owner: Any,
        qualification: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Drain one settled task and stop its host while retaining the pod/session."""
        with self._claim_owner() as (claim, owner_lock_fd):
            managed = claim.get("managed_worker")
            if (not isinstance(managed, Mapping)
                    or managed.get("schema") != "astrid.runpod.managed-worker.v1"
                    or managed.get("claim_operation_id") != claim.get("operation_id")
                    or managed.get("deployment_digest") != reference.digest()):
                raise RunPodWorkerOwnerError("P1 custody has no matching prepared worker to retire")
            process = WorkerProcessHandle.from_dict(managed["process_handle"])
            binding = self._binding(reference)
            activation = managed.get("activation_qualification")
            expected = {
                "task_id": reference.task_id, "run_id": reference.run_id,
                "deployment_digest": reference.digest(),
                "binding_digest": reference.deployment_binding.digest(),
                "effective_target": reference.effective_target,
                "runtime_session_id": binding.runtime_session_id,
                "runtime_epoch": binding.runtime_epoch,
                "credential_actor": binding.executor_id,
                "executor_incarnation": process.incarnation,
            }
            if (process.binding != binding or not isinstance(activation, Mapping)
                    or dict(activation) != dict(qualification)
                    or any(qualification.get(key) != value for key, value in expected.items())
                    or not isinstance(qualification.get("activation_id"), str)
                    or not qualification.get("activation_id")
                    or qualification.get("evidence_digest") != managed.get("observation_digest")
                    or managed.get("executor_incarnation") != process.incarnation):
                raise RunPodWorkerOwnerError("task retirement differs from persisted Runtime activation custody")

            identity = {
                "schema": "astrid.runpod.task-retirement.v1",
                "task_id": reference.task_id,
                "run_id": reference.run_id,
                "activation_id": qualification["activation_id"],
                "incarnation": process.incarnation,
            }
            saved = managed.get("task_retirement")
            progress = dict(identity if saved is None else saved)
            allowed = {*identity, "drain_started", "runtime_drain", "process_stop", "credential_remove"}
            if (not isinstance(progress, Mapping) or set(progress) - allowed
                    or any(progress.get(key) != value for key, value in identity.items())
                    or ("drain_started" in progress and progress["drain_started"] is not True)
                    or ("runtime_drain" in progress and progress.get("drain_started") is not True)
                    or ("process_stop" in progress and "runtime_drain" not in progress)
                    or ("credential_remove" in progress and "process_stop" not in progress)):
                raise RunPodWorkerOwnerError("task retirement progress is foreign or incomplete")
            progress = dict(progress)

            def checkpoint(**changes: Any) -> None:
                nonlocal managed
                progress.update(changes)
                state = {**dict(managed), "task_retirement": dict(progress)}
                self._write_worker_state(state, owner_lock_fd=owner_lock_fd)
                managed = state

            task_id = str(qualification["task_id"])
            activation_id = str(qualification["activation_id"])
            if not progress.get("drain_started"):
                begun = await asyncio.to_thread(
                    runtime_owner.begin_remote_drain, task_id, dict(qualification),
                )
                if begun != {"state": "pending", "activation_id": activation_id}:
                    raise RunPodWorkerOwnerError("Runtime did not begin this exact task drain")
                checkpoint(drain_started=True)
            drained = progress.get("runtime_drain")
            if drained is None:
                drained = await asyncio.to_thread(
                    runtime_owner.finish_remote_drain, task_id, dict(qualification),
                )
                if drained != {"state": "drained", "activation_id": activation_id}:
                    raise RunPodWorkerOwnerError("Runtime task remains active or its drain is uncertain")
                checkpoint(runtime_drain=dict(drained))
            elif drained != {"state": "drained", "activation_id": activation_id}:
                raise RunPodWorkerOwnerError("persisted task drain response is foreign")

            stopped = progress.get("process_stop")
            if stopped is None:
                owner = self._owner(reference)
                owner._require_handle(process)
                stopped = await owner.stop(process)
                if (not isinstance(stopped, Mapping) or stopped.get("stopped") is not True
                        or any(stopped.get(key) != getattr(process, key)
                               for key in ("pid", "birth_id", "incarnation"))):
                    raise RunPodWorkerOwnerError("exact prepared host stop was not confirmed")
                checkpoint(process_stop=dict(stopped))
            elif (not isinstance(stopped, Mapping) or stopped.get("stopped") is not True
                    or any(stopped.get(key) != getattr(process, key)
                           for key in ("pid", "birth_id", "incarnation"))):
                raise RunPodWorkerOwnerError("persisted host stop evidence is foreign")

            credential_removed = progress.get("credential_remove")
            if credential_removed is None:
                reservation = managed.get("credential_ack", {}).get("file_identity") if isinstance(
                    managed.get("credential_ack"), Mapping
                ) else managed.get("credential_reservation")
                if not isinstance(reservation, Mapping):
                    raise RunPodWorkerOwnerError("settled activation has no exact remote credential file identity")
                removed = await owner.remove_credential(process, reservation)
                if not isinstance(removed, Mapping) or removed.get("removed") is not True:
                    raise RunPodWorkerOwnerError("exact retired Runtime credential cleanup is unresolved")
                credential_removed = {"removed": True}
                checkpoint(credential_remove=credential_removed)
            elif credential_removed != {"removed": True}:
                raise RunPodWorkerOwnerError("persisted remote credential removal evidence is foreign")

            state = {
                **dict(managed), "state": "stopped", "stop_evidence": dict(stopped),
                "task_retirement": {**dict(progress), "credential_remove": dict(credential_removed)},
            }
            state.pop("credential_ack", None)
            state.pop("credential_reservation", None)
            self._write_worker_state(state, owner_lock_fd=owner_lock_fd)
            self._handle = None
            return {
                "schema": "astrid.runpod.task-quiescence.v1",
                "operation_id": claim["operation_id"],
                "pod_id": binding.pod_id,
                "provider_account_ref": binding.account_ref,
                "task_id": task_id,
                "activation_id": activation_id,
                "runtime_instance_id": binding.runtime_instance_id,
                "runtime_session_id": binding.runtime_session_id,
                "runtime_epoch": binding.runtime_epoch,
                "runtime_drain": dict(drained),
                "process_stop": dict(stopped),
                "gpu_qualified": False,
            }

    async def _quiesce_release_locked(
        self, reference: Any, runtime_owner: Any, record: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Called only inside P1's release lock; never call public preparer APIs."""
        value = record.get("managed_worker")
        if (record.get("state") != "release_pending" or not isinstance(value, Mapping)
                or value.get("schema") != "astrid.runpod.managed-worker.v1"
                or value.get("claim_operation_id") != record.get("operation_id")
                or value.get("deployment_digest") != reference.digest()
                or value.get("pod_id") != record.get("pod_id")
                or value.get("provider_account_ref") != record.get("provider_account_ref")):
            raise RunPodWorkerOwnerError("release has no exact persisted managed-worker custody")
        process = WorkerProcessHandle.from_dict(value["process_handle"])
        qualification = value.get("activation_qualification")
        binding = self._binding(reference)
        expected = {
            "task_id": reference.task_id, "run_id": reference.run_id,
            "deployment_digest": reference.digest(),
            "binding_digest": reference.deployment_binding.digest(),
            "effective_target": reference.effective_target,
            "runtime_session_id": binding.runtime_session_id,
            "runtime_epoch": binding.runtime_epoch,
            "credential_actor": binding.executor_id,
            "executor_incarnation": process.incarnation,
        }
        if (process.binding != binding or record.get("pod_id") != binding.pod_id
                or record.get("provider_account_ref") != binding.account_ref
                or not isinstance(qualification, Mapping)
                or any(qualification.get(key) != item for key, item in expected.items())
                or not isinstance(qualification.get("activation_id"), str)
                or not qualification["activation_id"]
                or qualification.get("evidence_digest") != value.get("observation_digest")
                or value.get("executor_incarnation") != process.incarnation):
            raise RunPodWorkerOwnerError("release qualification differs from persisted task/process binding")
        owner = self._owner(reference)
        owner._require_handle(process)
        retire = getattr(owner, "retire_for_release", None)
        if not callable(retire):
            raise RunPodWorkerOwnerError("target-wide release fencing is unavailable")
        identity = {
            "schema": "astrid.runpod.release-progress.v1",
            "activation_id": qualification["activation_id"],
            "incarnation": process.incarnation,
        }
        saved_progress = record.get("release_progress")
        if saved_progress is not None and not isinstance(saved_progress, Mapping):
            raise RunPodWorkerOwnerError("release progress is invalid")
        progress = dict(identity if saved_progress is None else saved_progress)
        allowed = {*identity, "drain_started", "runtime_drain", "release_fence", "process_stop",
                   "credential_remove", "session_stop"}
        if (set(progress) - allowed or any(progress.get(key) != item for key, item in identity.items())
                or ("drain_started" in progress and progress["drain_started"] is not True)
                or (set(progress) & {"runtime_drain", "release_fence", "process_stop"}
                    and progress.get("drain_started") is not True)
                or ("release_fence" in progress and "runtime_drain" not in progress)
                or ("process_stop" in progress and "release_fence" not in progress)
                or ("credential_remove" in progress and "process_stop" not in progress)
                or ("session_stop" in progress and "process_stop" not in progress)):
            raise RunPodWorkerOwnerError("release progress belongs to another or incomplete generation")

        retirement = value.get("task_retirement")
        if saved_progress is None and value.get("state") == "stopped":
            if (not isinstance(retirement, Mapping)
                    or retirement.get("activation_id") != qualification.get("activation_id")
                    or retirement.get("incarnation") != process.incarnation
                    or retirement.get("runtime_drain") != {
                        "state": "drained", "activation_id": qualification["activation_id"],
                    }
                    or not isinstance(retirement.get("process_stop"), Mapping)
                    or retirement.get("credential_remove") != {"removed": True}):
                raise RunPodWorkerOwnerError("stopped worker has no exact task-retirement drain evidence")
            progress.update({
                "drain_started": True,
                "runtime_drain": dict(retirement["runtime_drain"]),
                "process_stop": dict(retirement["process_stop"]),
                "credential_remove": dict(retirement["credential_remove"]),
            })

        def checkpoint(**changes: Any) -> None:
            progress.update(changes)
            _checkpoint_release_progress(self.claim_handle_path, record, progress)

        task_id, activation_id = reference.task_id, qualification["activation_id"]
        if not progress.get("drain_started"):
            begun = await asyncio.to_thread(runtime_owner.begin_remote_drain, task_id, dict(qualification))
            if begun != {"state": "pending", "activation_id": activation_id}:
                raise RunPodWorkerOwnerError("Runtime did not begin this exact activation drain")
            # Durable before finish: a lost finish reply can leave the token
            # revoked, after which begin is no longer a valid replay.
            checkpoint(drain_started=True)
        drained = progress.get("runtime_drain")
        if drained is None:
            drained = await asyncio.to_thread(runtime_owner.finish_remote_drain, task_id, dict(qualification))
            if drained != {"state": "drained", "activation_id": activation_id}:
                raise RunPodWorkerOwnerError("Runtime activation remains active or its drain is uncertain")
            checkpoint(runtime_drain=dict(drained))
        elif drained != {"state": "drained", "activation_id": activation_id}:
            raise RunPodWorkerOwnerError("persisted Runtime drain is foreign")

        fence_identity = {
            "retired": True, "operation_id": record["operation_id"],
            "provider": "runpod", "account_ref": binding.account_ref,
            "pod_id": binding.pod_id, "incarnation": process.incarnation,
        }
        fence = progress.get("release_fence")
        if fence is None:
            fence = await retire(process, operation_id=record["operation_id"])
        if (not isinstance(fence, Mapping) or set(fence) != {*fence_identity, "release_fence_digest"}
                or fence.get("retired") is not True
                or any(fence.get(key) != item for key, item in fence_identity.items())
                or not isinstance(fence.get("release_fence_digest"), str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", fence["release_fence_digest"])):
            raise RunPodWorkerOwnerError("physical target release fence is foreign or uncertain")
        if "release_fence" not in progress:
            checkpoint(release_fence=dict(fence))
        stopped = progress.get("process_stop")
        if stopped is None:
            stopped = await owner.stop(process)
        if (not isinstance(stopped, Mapping) or stopped.get("stopped") is not True
                or type(stopped.get("pid")) is not int
                or any(stopped.get(key) != getattr(process, key) for key in ("pid", "birth_id", "incarnation"))):
            raise RunPodWorkerOwnerError("exact release process stop was not confirmed")
        if "process_stop" not in progress:
            checkpoint(process_stop=dict(stopped))

        credential_removed = progress.get("credential_remove")
        if credential_removed is None:
            reservation = value.get("credential_ack", {}).get("file_identity") if isinstance(
                value.get("credential_ack"), Mapping
            ) else value.get("credential_reservation")
            if not isinstance(reservation, Mapping):
                raise RunPodWorkerOwnerError("release has no exact remote credential file identity")
            removed = await owner.remove_credential(process, reservation)
            if not isinstance(removed, Mapping) or removed.get("removed") is not True:
                raise RunPodWorkerOwnerError("exact release Runtime credential cleanup is unresolved")
            credential_removed = {"removed": True}
            checkpoint(credential_remove=credential_removed)
        elif credential_removed != {"removed": True}:
            raise RunPodWorkerOwnerError("persisted release credential removal evidence is foreign")

        session_stopped = progress.get("session_stop")
        session_record = record.get("managed_session")
        if session_record is not None:
            if (not isinstance(session_record, Mapping)
                    or session_record.get("schema") != "astrid.runpod.managed-session.v1"
                    or session_record.get("claim_operation_id") != record.get("operation_id")
                    or not isinstance(session_record.get("selection"), Mapping)
                    or not isinstance(session_record.get("receipt"), Mapping)
                    or self.session_owner is None):
                raise RunPodWorkerOwnerError("explicit release has no exact managed session custody")
            from .worker_session import ManagedSessionReceipt, ManagedSessionSelection

            selection = ManagedSessionSelection(**dict(session_record["selection"]))
            receipt = ManagedSessionReceipt.from_dict(session_record["receipt"])
            if (session_record.get("session_ref") != selection.session_ref
                    or session_record.get("config_digest") != selection.config_digest
                    or session_record.get("output_root") != selection.output_root):
                raise RunPodWorkerOwnerError("managed session custody changed before release")
            if session_stopped is None:
                session_stopped = self.session_owner.stop(selection, receipt)
                checkpoint(session_stop=dict(session_stopped))
            elif (not isinstance(session_stopped, Mapping)
                    or session_stopped.get("state") != "stopped"
                    or session_stopped.get("session_ref") != selection.session_ref
                    or session_stopped.get("config_digest") != selection.config_digest
                    or session_stopped.get("gpu_qualified") is not False):
                raise RunPodWorkerOwnerError("persisted managed session stop receipt is foreign")
        return {
            "schema": "astrid.runpod.worker-quiescence.v1",
            "operation_id": record["operation_id"], "pod_id": binding.pod_id,
            "provider_account_ref": binding.account_ref,
            "task_id": task_id, "activation_id": activation_id,
            "runtime_instance_id": binding.runtime_instance_id,
            "runtime_session_id": binding.runtime_session_id, "runtime_epoch": binding.runtime_epoch,
            "drain": {"state": "complete", "task_id": task_id, "activation_id": activation_id},
            "runtime_drain": dict(drained), "release_fence": dict(fence), "process_stop": dict(stopped),
            "credential_remove": dict(credential_removed),
            **({"session_stop": dict(session_stopped)} if session_stopped is not None else {}),
        }

    def restore(self, reference: Any) -> Any:
        """Rehydrate only the exact process and deployment bound in P1 custody."""
        with self._claim_owner() as (claim, _fd):
            return self._restore_locked(reference, claim)

    def abort_parked(self, reference: Any) -> Mapping[str, Any]:
        """Stop an exact unqualified host after a coordinator restart.

        This is permitted only before a Runtime activation generation was
        checkpointed. Once credential issuance may have started, callers must
        reconcile or revoke that exact generation first.
        """
        with self._claim_owner() as (claim, owner_lock_fd):
            value = claim.get("managed_worker")
            if (not isinstance(value, Mapping)
                    or value.get("schema") != "astrid.runpod.managed-worker.v1"
                    or value.get("deployment_digest") != reference.digest()
                    or value.get("claim_operation_id") != claim.get("operation_id")
                    or value.get("state") not in {"parked_process", "parked"}
                    or value.get("activation_qualification") is not None):
                raise RunPodWorkerOwnerError("only the exact unqualified parked process can be replaced")
            process = WorkerProcessHandle.from_dict(value["process_handle"])
            if process.binding != self._binding(reference):
                raise RunPodWorkerOwnerError("parked process belongs to another Runtime or RunPod target")
            owner = self._owner(reference)
            owner._require_handle(process)
            stopped = _run_sync(owner.stop(process))
            if (not isinstance(stopped, Mapping) or stopped.get("stopped") is not True
                    or any(stopped.get(key) != getattr(process, key)
                           for key in ("pid", "birth_id", "incarnation"))):
                raise RunPodWorkerOwnerError("exact unqualified process stop is not confirmed")
            state = {**dict(value), "state": "stopped", "stop_evidence": dict(stopped)}
            self._write_worker_state(state, owner_lock_fd=owner_lock_fd)
            self._handle = None
            return dict(stopped)

    def abort_uncommitted(
        self, reference: Any, *, activation_id: str,
        runtime_revocation: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Retire one pre-readiness generation after Runtime revoked it exactly."""
        with self._claim_owner() as (claim, owner_lock_fd):
            value = claim.get("managed_worker")
            if (not isinstance(value, Mapping)
                    or value.get("schema") != "astrid.runpod.managed-worker.v1"
                    or value.get("deployment_digest") != reference.digest()
                    or value.get("claim_operation_id") != claim.get("operation_id")):
                raise RunPodWorkerOwnerError("P1 custody has no matching uncommitted RunPod process")
            qualification = value.get("activation_qualification")
            stage = value.get("state")
            permitted = {
                "qualification", "credential", "credential_delivered", "bootstrapped",
                "acknowledged", "enabled", "ready", "ready_observed_by_preparer", "cleanup_pending",
            }
            if (stage not in permitted or not isinstance(qualification, Mapping)
                    or qualification.get("activation_id") != activation_id
                    or dict(runtime_revocation) != {"revoked": True}):
                raise RunPodWorkerOwnerError("uncommitted cleanup lacks exact Runtime revocation authority")
            process = WorkerProcessHandle.from_dict(value["process_handle"])
            if process.binding != self._binding(reference):
                raise RunPodWorkerOwnerError("uncommitted process belongs to another Runtime or RunPod target")
            prior = value.get("uncommitted_abort")
            identity = {
                "schema": "astrid.runpod.uncommitted-abort.v1",
                "activation_id": activation_id,
                "incarnation": process.incarnation,
                "runtime_revocation": {"revoked": True},
            }
            progress = dict(identity if prior is None else prior)
            allowed = {*identity, "process_stop", "credential_removed"}
            if (not isinstance(progress, Mapping) or set(progress) - allowed
                    or any(progress.get(key) != item for key, item in identity.items())):
                raise RunPodWorkerOwnerError("uncommitted cleanup progress is foreign")
            progress = dict(progress)

            def checkpoint(**changes: Any) -> None:
                nonlocal value
                progress.update(changes)
                value = {**dict(value), "state": "cleanup_pending", "uncommitted_abort": dict(progress)}
                self._write_worker_state(value, owner_lock_fd=owner_lock_fd)

            owner = self._owner(reference)
            stopped = progress.get("process_stop")
            if stopped is None:
                owner._require_handle(process)
                stopped = _run_sync(owner.stop(process))
                if (not isinstance(stopped, Mapping) or stopped.get("stopped") is not True
                        or any(stopped.get(key) != getattr(process, key)
                               for key in ("pid", "birth_id", "incarnation"))):
                    raise RunPodWorkerOwnerError("exact uncommitted GenericPackHost stop was not confirmed")
                checkpoint(process_stop=dict(stopped))
            elif (not isinstance(stopped, Mapping) or stopped.get("stopped") is not True
                    or any(stopped.get(key) != getattr(process, key)
                           for key in ("pid", "birth_id", "incarnation"))):
                raise RunPodWorkerOwnerError("persisted uncommitted process stop evidence is foreign")

            removed = progress.get("credential_removed")
            if removed is None:
                reservation = value.get("credential_ack", {}).get("file_identity") if isinstance(
                    value.get("credential_ack"), Mapping
                ) else value.get("credential_reservation")
                if isinstance(reservation, Mapping):
                    result = _run_sync(owner.remove_credential(process, reservation))
                else:
                    result = _run_sync(owner.remove_unissued_credential(reference.credential_ref))
                if not isinstance(result, Mapping) or result.get("removed") is not True:
                    raise RunPodWorkerOwnerError("exact uncommitted remote credential cleanup is unresolved")
                removed = {"removed": True}
                checkpoint(credential_removed=removed)
            elif removed != {"removed": True}:
                raise RunPodWorkerOwnerError("persisted remote credential cleanup evidence is foreign")

            final = {
                **dict(value),
                "state": "stopped",
                "stop_evidence": dict(stopped),
                "uncommitted_abort": dict(progress),
            }
            final.pop("credential_ack", None)
            final.pop("credential_reservation", None)
            self._write_worker_state(final, owner_lock_fd=owner_lock_fd)
            self._handle = None
            return {"stopped": True, "activation_id": activation_id,
                    "incarnation": process.incarnation, "credential_removed": True}

    def _restore_locked(self, reference: Any, claim: Mapping[str, Any]) -> Any:
        value = claim.get("managed_worker")
        if (not isinstance(value, Mapping)
                or value.get("schema") != "astrid.runpod.managed-worker.v1"
                or value.get("state") in {"stopped", "released"}
                or value.get("deployment_digest") != reference.digest()
                or value.get("pod_id") != str(self.pod.id)
                or value.get("provider_account_ref") != self.provider_account_ref
                or value.get("claim_operation_id") != claim.get("operation_id")):
            raise RunPodWorkerOwnerError("P1 record has no resumable matching prepared worker")
        owner = self._owner(reference)
        stored = WorkerProcessHandle.from_dict(value["process_handle"])
        owner._require_handle(stored)
        current = _run_sync(owner.reconcile())
        if current != stored:
            raise RunPodWorkerOwnerError("physical RunPod owner belongs to a different incarnation")
        observed = _run_sync(owner.observe(stored))
        if (observed.get("target") != self._binding(reference).target
                or observed.get("provider_identity", {}).get("account_ref") != self.provider_account_ref
                or observed.get("provider_identity", {}).get("pod_id") != str(self.pod.id)):
            raise RunPodWorkerOwnerError("rehydrated RunPod process observation is foreign")
        staging = self._stage(reference)
        if staging.get("plan_digest") != value.get("staging_plan_digest"):
            raise RunPodWorkerOwnerError("selected P2 plan differs from durable RunPod preparation custody")
        session_selection = None
        session_receipt = None
        if self.session_owner is not None:
            saved_session = claim.get("managed_session")
            if (not isinstance(saved_session, Mapping)
                    or saved_session.get("schema") != "astrid.runpod.managed-session.v1"
                    or saved_session.get("claim_operation_id") != claim.get("operation_id")
                    or saved_session.get("account_ref") != self.provider_account_ref
                    or saved_session.get("pod_id") != str(self.pod.id)
                    or saved_session.get("gpu_qualified") is not False):
                raise RunPodWorkerOwnerError("P1 record has no exact managed VibeComfy session receipt")
            from .worker_session import ManagedSessionReceipt

            session_selection = self.session_owner.select(
                reference, staging["cpu_readiness"], port=self.session_port,
            )
            session_receipt = ManagedSessionReceipt.from_dict(saved_session["receipt"])
            if (saved_session.get("session_ref") != session_selection.session_ref
                    or saved_session.get("config_digest") != session_selection.config_digest
                    or saved_session.get("output_root") != session_selection.output_root
                    or saved_session.get("selection") != asdict(session_selection)):
                raise RunPodWorkerOwnerError("persisted VibeComfy session selection changed")
            self.session_owner.resume(session_selection, session_receipt)
        handle = PreparedRunPodHost(
            reference=reference,
            owner=owner,
            process=stored,
            operation_id=str(value["activation_operation_id"]),
            channel_id=str(value["activation_channel_id"]),
            socket_path=str(value["activation_socket_path"]),
            staging=staging,
            ready_file=str(value["ready_file"]),
            session_selection=session_selection,
            session_receipt=session_receipt,
            persisted=dict(value),
            activation_id=(value.get("activation_qualification") or {}).get("activation_id"),
            evidence_digest=(value.get("activation_qualification") or {}).get("evidence_digest"),
            credential_source_file=value.get("credential_source_file"),
            credential_ack=value.get("credential_ack"),
        )
        self._handle = handle
        observation = value.get("observation")
        qualification = value.get("activation_qualification")
        if (not isinstance(observation, Mapping)
                or not isinstance(qualification, Mapping)
                or value.get("executor_incarnation") != stored.incarnation
                or value.get("observation_digest") != _canonical_digest(observation)):
            raise RunPodWorkerOwnerError("P1 record has no complete resumable activation checkpoint")
        from runtime_protocol.remote_worker_activation import RemotePreparationCheckpoint

        return RemotePreparationCheckpoint(
            handle=handle,
            target=dict(observation["target"]),
            observation=dict(observation),
            evidence_digest=str(value["observation_digest"]),
            executor_incarnation=str(value["executor_incarnation"]),
        )


class RunPodRemoteWorkerInspector:
    """Independent provider, process, staged-input and Runtime readiness witness.

    Callbacks read Runtime authority and the selected remote session config.
    Provider inventory and CPU staging are re-observed through the owners
    already used for preparation; a host ready file is only corroborating data.
    """

    def __init__(
        self,
        preparer: RunPodRemoteWorkerPreparer,
        *,
        runtime_observer: Any,
        session_config_observer: Any,
        executor_observer: Any,
    ) -> None:
        self.preparer = preparer
        self.runtime_observer = runtime_observer
        self.session_config_observer = session_config_observer
        self.executor_observer = executor_observer

    def observe(self, raw_handle: object) -> Mapping[str, Any]:
        handle = self.preparer._assert_handle(raw_handle)
        reference = handle.reference
        process_evidence = _run_sync(handle.owner.observe(handle.process))
        provider = self.preparer.observe_provider_identity(handle)
        if (process_evidence.get("provider_identity") != provider
                or process_evidence.get("incarnation") != handle.incarnation):
            raise RunPodWorkerOwnerError("physical process custody and live provider identity disagree")
        staging = self.preparer.observe_staging(handle)
        cpu = staging.get("cpu_readiness")
        if (not isinstance(cpu, Mapping) or cpu.get("status") != "cpu_ready"
                or cpu.get("gpu_qualified") is not False):
            raise RunPodWorkerOwnerError("current P2 evidence is not CPU-only readiness")
        paths = cpu.get("effective_paths")
        model = cpu.get("model_root")
        if not isinstance(paths, Mapping) or not isinstance(model, Mapping):
            raise RunPodWorkerOwnerError("P2 omitted current model or selected path evidence")
        runtime = self.runtime_observer()
        session = self.session_config_observer(handle)
        if not isinstance(runtime, Mapping) or not isinstance(session, Mapping):
            raise RunPodWorkerOwnerError("independent Runtime or session observation is missing")
        target = self.preparer._binding(reference).target
        expected_runtime = {
            "runtime_instance_id": reference.runtime_instance_id,
            "runtime_epoch": reference.runtime_epoch,
            "runtime_session_id": self.preparer.runtime_session_id,
        }
        if any(runtime.get(key) != value for key, value in expected_runtime.items()):
            raise RunPodWorkerOwnerError("live Runtime identity differs from the selected deployment")
        if (session.get("session_ref") != reference.session_ref
                or session.get("session_config_digest") != reference.session_config_digest):
            raise RunPodWorkerOwnerError("live session config differs from the selected deployment")
        model_root = str(paths.get("model_root", ""))
        output_root = str(paths.get("output_root", ""))
        selected_model = self.preparer.staging_inputs["readiness_profile"]["launch"]["model_root"]
        if (model_root != str(reference.model_root)
                or output_root != str(reference.output_root)
                or model.get("path") != model_root
                or model.get("inventory_digest") != selected_model.get("inventory_digest")):
            raise RunPodWorkerOwnerError("current P2 model or output root differs from Runtime selection")
        dependencies = [
            {"name": item.name, "path": str(item.path), "digest": item.digest}
            for item in reference.dependency_closure
        ]
        process = process_evidence.get("process")
        if not isinstance(process, Mapping):
            raise RunPodWorkerOwnerError("current physical process identity is missing")
        observation = {
            "target": target,
            "provider_identity": provider,
            "process": dict(process),
            **expected_runtime,
            "source_closure_digest": reference.source_closure_digest,
            "dependency_closure_digest": _canonical_digest(dependencies),
            "model_root": model_root,
            "session_ref": reference.session_ref,
            "data_root": str(reference.data_root),
            "support_root": str(reference.support_root),
            "output_root": output_root,
            "capacity": reference.capacity,
            "model_inventory_digest": str(model["inventory_digest"]),
            "session_config_digest": reference.session_config_digest,
        }
        return observation

    def observe_ready(self, raw_handle: object) -> Mapping[str, Any]:
        from astrid.core.execution.generic_host import RuntimeProtocolClient

        handle = self.preparer._assert_handle(raw_handle)
        observed = dict(self.observe(handle))
        ready = _run_sync(handle.owner.read_ready(handle.process, handle.ready_file))
        activation = ready.get("activation") if isinstance(ready, Mapping) else None
        expected_host = {
            "pid": handle.process.pid,
            "birth_id": handle.process.birth_id,
        }
        if (not isinstance(ready, Mapping) or ready.get("status") != "ready"
                or ready.get("pid") != handle.process.pid
                or ready.get("process_birth_id") != handle.process.birth_id
                or ready.get("executor_id") != handle.reference.executor_id
                or ready.get("output_root") != observed["output_root"]
                or ready.get("attempt_base") != str(Path(observed["output_root"]) / "attempts")
                or ready.get("session_ref") != observed["session_ref"]
                or ready.get("session_config_digest") != observed["session_config_digest"]
                or ready.get("data_root") != observed["data_root"]
                or ready.get("support_root") != observed["support_root"]
                or ready.get("runtime_instance_id") != observed["runtime_instance_id"]
                or ready.get("runtime_epoch") != observed["runtime_epoch"]
                or not isinstance(ready.get("identity_attestation"), Mapping)
                or ready["identity_attestation"].get("target") != observed["target"]
                or ready.get("source_closure_digest") != handle.reference.source_closure_digest.removeprefix("sha256:")
                or not isinstance(activation, Mapping)
                or activation.get("operation_id") != handle.operation_id
                or activation.get("channel_id") != handle.channel_id
                or activation.get("activation_id") != handle.activation_id
                or activation.get("executor_incarnation") != handle.incarnation
                or activation.get("evidence_digest") != handle.evidence_digest
                or activation.get("host") != expected_host):
            raise RunPodWorkerOwnerError("remote GenericPackHost readiness is foreign or stale")
        model_binding = ready.get("model_root_binding")
        if (not isinstance(model_binding, Mapping)
                or model_binding.get("path") != observed["model_root"]
                or model_binding.get("inventory_digest") != observed["model_inventory_digest"]):
            raise RunPodWorkerOwnerError("remote GenericPackHost model readiness differs from P2")
        if not handle.credential_source_file:
            raise RunPodWorkerOwnerError("enabled Runtime credential locator is missing")
        credential = _read_credential(handle.credential_source_file).decode("utf-8")
        client = RuntimeProtocolClient(handle.reference.runtime_endpoint, credential)
        client._authenticate_worker(handle.reference.executor_id)
        registration = self.executor_observer(handle.reference.executor_id)
        source_facts = _run_sync(handle.owner.inspect_source(
            handle.process,
            source_root=str(handle.reference.source_checkout),
            pack_roots=[str(item) for item in handle.reference.pack_roots],
        ))
        capability = handle.reference.capability_identity
        source_capabilities = source_facts.get("capabilities") if isinstance(source_facts, Mapping) else None
        registered_capabilities = registration.get("capabilities") if isinstance(registration, Mapping) else None
        if (not isinstance(registration, Mapping)
                or not isinstance(source_facts, Mapping)
                or not isinstance(source_capabilities, list)
                or not isinstance(registered_capabilities, list)
                or registration.get("id") != handle.reference.executor_id
                or registration.get("max_concurrency") != handle.reference.capacity
                or registration.get("runtime_epoch") != observed["runtime_epoch"]
                or registration.get("readiness") != "ready"
                or registration.get("source_digest") != source_facts.get("source_digest")
                or registration.get("dependency_digest") != source_facts.get("dependency_digest")
                or not any(item.get("capability_id") == capability.capability_id
                           and item.get("definition_digest") == capability.capability_digest
                           and item.get("status") == "ready"
                           for item in source_capabilities)
                or not any(item.get("capability_id") == capability.capability_id
                           and item.get("definition_digest") == capability.capability_digest
                           and item.get("status") == "ready"
                           for item in registered_capabilities)):
            raise RunPodWorkerOwnerError("Runtime does not independently report the exact executor as ready")
        return observed


__all__ = [
    "PreparedRunPodHost",
    "RunPodRemoteWorkerInspector",
    "RunPodRemoteWorkerPreparer",
    "RunPodWorkerOwnerError",
]
