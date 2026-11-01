"""Attach one compiled H3 task to an already claimed RunPod worker.

This is a thin caller adapter: Runtime still admits and owns the task receipt,
the Runtime deployment binding supplies task identity, and the RunPod P1/P2
owners supply machine custody and CPU preparation.  It never creates a second
task/result ledger and never probes or qualifies a GPU.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from astrid.core.path_utils import is_native_absolute_path
from astrid.sdk.execution_request import normalize_execution_request


class PreparedWorkerError(RuntimeError):
    """The selected claim, task, or worker handoff cannot be proven exact."""


_SELECTION_SCHEMA = "astrid.runpod.prepared-worker.v1"
_DEPLOYMENT_PROFILE_SCHEMA = "astrid.runpod.deployment-profile.v1"
_SELECTION_FIELDS = {
    "schema", "claim_handle_path", "deployment_profile_path",
    "staging_inputs_path", "journal_dir",
}


def _read_mapping(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PreparedWorkerError(f"{label} must be a regular non-symlink file")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise PreparedWorkerError(f"{label} must be a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PreparedWorkerError(f"{label} is unreadable JSON") from exc
    if not isinstance(value, Mapping):
        raise PreparedWorkerError(f"{label} must contain a JSON object")
    return dict(value)


def _claim_target(claim: Mapping[str, Any]) -> dict[str, Any]:
    required = ("pod_id", "provider_account_ref", "network_volume_id")
    if any(not isinstance(claim.get(key), str) or not claim[key].strip() for key in required):
        raise PreparedWorkerError("P1 claim does not identify its exact RunPod account, pod, and network volume")
    if claim.get("state") not in {"claimed", "release_pending", "cleanup_pending"}:
        raise PreparedWorkerError("P1 claim is not an active exact pod claim")
    storage = {"network_volume_id": claim["network_volume_id"]}
    if isinstance(claim.get("network_volume_name"), str) and claim["network_volume_name"]:
        storage["name"] = claim["network_volume_name"]
    return {
        "kind": "runpod",
        "pod_id": claim["pod_id"],
        "provider_account_ref": claim["provider_account_ref"],
        "storage": storage,
    }


@dataclass(frozen=True)
class PreparedWorkerSelection:
    claim_handle_path: Path
    deployment_profile_path: Path
    staging_inputs_path: Path
    journal_dir: Path
    target: Mapping[str, Any]
    deployment_profile: Mapping[str, Any]
    staging_inputs: Mapping[str, Any]
    composition_factory: Callable[..., PreparedTaskRuntime] | None = None

    def execution_request(
        self, supplied: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Bind H3's ordinary request to this exact claim and defer claims until activation."""

        claim = _read_mapping(self.claim_handle_path, label="P1 claim")
        if claim.get("state") != "claimed":
            raise PreparedWorkerError("prepared H3 tasks require an actively claimed RunPod target")
        request = dict(normalize_execution_request(supplied) or {}) if supplied else {}
        target = dict(self.target)
        if request.get("target") is not None:
            existing = dict(normalize_execution_request(request)["target"])
            expected = dict(self.target)
            if (existing.get("kind") != "runpod"
                    or existing.get("pod_id") != expected["pod_id"]
                    or existing.get("provider_account_ref") != expected["provider_account_ref"]):
                raise PreparedWorkerError("supplied H3 target differs from the exact P1 claim")
            storage = existing.get("storage")
            if (not isinstance(storage, Mapping)
                    or storage.get("network_volume_id", storage.get("volume_id", storage.get("id")))
                    != expected["storage"]["network_volume_id"]):
                raise PreparedWorkerError("supplied H3 target does not pin the exact P1 network volume")
            expected_volume_name = expected["storage"].get("name")
            supplied_volume_name = storage.get("name")
            if (supplied_volume_name is not None and expected_volume_name is not None
                    and supplied_volume_name != expected_volume_name):
                raise PreparedWorkerError("supplied H3 target volume name differs from the exact P1 claim")
            # Keep the P1 claim's canonical target (including its optional
            # volume label) so admission, selection, and Runtime binding use
            # one byte-for-byte representation after identity is validated.
            target = expected
        lifecycle = request.get("lifecycle")
        if lifecycle and lifecycle.get("mode") not in (None, "leave_running"):
            raise PreparedWorkerError("prepared RunPod tasks retain explicit leave-running machine custody")
        request["target"] = target
        request["lifecycle"] = {**dict(lifecycle or {}), "mode": "leave_running"}
        request["remote_activation_required"] = True
        normalized = normalize_execution_request(request)
        if not isinstance(normalized, Mapping):
            raise PreparedWorkerError("prepared execution request could not be normalized")
        return dict(normalized)

    def bind_client(
        self,
        client: Any,
        *,
        workflow_path: Path,
        workflow_bundle_path: Path,
        workflow_inputs: Mapping[str, Any],
    ) -> PreparedRunPodTaskClient:
        factory = self.composition_factory or _production_composition_factory
        runtime = factory(
            client=client,
            selection=self,
            workflow_path=workflow_path,
            workflow_bundle_path=workflow_bundle_path,
            workflow_inputs=dict(workflow_inputs),
        )
        return PreparedRunPodTaskClient(client, runtime)


class PreparedTaskRuntime(Protocol):
    def ensure_for_task(self, task: Mapping[str, Any]) -> None: ...
    def retire_after_task(self, task: Mapping[str, Any]) -> None: ...


def load_prepared_worker_selection(
    path: str | Path,
    *,
    composition_factory: Callable[..., PreparedTaskRuntime] | None = None,
) -> PreparedWorkerSelection:
    """Load a reference to existing P1/P2 custody; do not create or attach a pod."""

    selection_path = Path(path).expanduser()
    document = _read_mapping(selection_path, label="prepared worker selection")
    if set(document) != _SELECTION_FIELDS or document.get("schema") != _SELECTION_SCHEMA:
        raise PreparedWorkerError("prepared worker selection schema or fields are invalid")

    def selected_path(name: str, *, directory: bool = False) -> Path:
        value = document.get(name)
        if not isinstance(value, str) or not is_native_absolute_path(value) or ".." in Path(value).parts:
            raise PreparedWorkerError(f"prepared worker {name} must be a canonical absolute path")
        result = Path(value)
        if result.is_symlink() or (directory and result.exists() and not result.is_dir()):
            raise PreparedWorkerError(f"prepared worker {name} has an unsafe filesystem type")
        return result

    claim_path = selected_path("claim_handle_path")
    profile_path = selected_path("deployment_profile_path")
    staging_path = selected_path("staging_inputs_path")
    journal_dir = selected_path("journal_dir", directory=True)
    claim = _read_mapping(claim_path, label="P1 claim")
    target = _claim_target(claim)
    profile = _read_mapping(profile_path, label="deployment profile")
    if profile.get("schema") != _DEPLOYMENT_PROFILE_SCHEMA:
        raise PreparedWorkerError("deployment profile schema is invalid")
    staging = _read_mapping(staging_path, label="P2 staging inputs")
    # JSON cannot preserve pathlib.Path values. Normalize the three local
    # filesystem inputs here, before they cross into P2's typed preparation
    # boundary. Remote paths inside the readiness/release profile remain
    # strings because they name paths on the selected RunPod volume.
    for key, want_directory in (
        ("release_manifest_path", False),
        ("astrid_source", True),
        ("vibecomfy_source", True),
    ):
        value = staging.get(key)
        if (not isinstance(value, str) or not is_native_absolute_path(value)
                or ".." in Path(value).parts):
            raise PreparedWorkerError(f"P2 staging input {key} must be a canonical absolute local path")
        local_path = Path(value)
        if str(local_path) != value:
            raise PreparedWorkerError(f"P2 staging input {key} must be a canonical absolute local path")
        if (local_path.is_symlink() or not local_path.exists()
                or local_path.resolve(strict=True) != local_path):
            raise PreparedWorkerError(f"P2 staging input {key} must be an existing non-symlink path")
        if (want_directory and not local_path.is_dir()) or (not want_directory and not local_path.is_file()):
            expected = "directory" if want_directory else "regular file"
            raise PreparedWorkerError(f"P2 staging input {key} must be a {expected}")
        staging[key] = local_path
    return PreparedWorkerSelection(
        claim_path, profile_path, staging_path, journal_dir,
        target, profile, staging, composition_factory,
    )


class PreparedRunPodTaskClient:
    """Two-phase wrapper around the SDK's one ordinary admission receipt."""

    def __init__(self, client: Any, runtime: PreparedTaskRuntime) -> None:
        self._client = client
        self._runtime = runtime

    def invoke_result(self, capability_id: str, *, kind: str, **kwargs: Any) -> Any:
        request = kwargs.get("execution_request")
        prepared = isinstance(request, Mapping) and request.get("remote_activation_required") is True
        if capability_id != "vibecomfy.run" or not prepared:
            return self._client.invoke_result(capability_id, kind=kind, **kwargs)
        if kwargs.get("wait") is not True:
            return self._client.invoke_result(capability_id, kind=kind, **kwargs)

        recovery_path = kwargs.get("recovery_path")
        if recovery_path is None:
            raise PreparedWorkerError("prepared H3 execution requires the ordinary SDK recovery receipt path")
        first_args = dict(kwargs)
        first_args["wait"] = False
        first = self._client.invoke_result(capability_id, kind=kind, **first_args)
        if not bool(getattr(first, "ok", False)):
            return first
        task_id = getattr(first, "kernel_task_id", None)
        run_id = getattr(first, "kernel_run_id", None)
        if not isinstance(task_id, str) or not task_id or not isinstance(run_id, str) or not run_id:
            raise PreparedWorkerError("SDK admission receipt omitted its exact Runtime task/run identity")
        task = self._get_exact_task(task_id, run_id, capability_id)
        state = str(task.get("state") or task.get("status") or "").casefold()
        activated = False
        if state not in {"succeeded", "completed", "failed", "cancelled", "canceled"}:
            self._runtime.ensure_for_task(task)
            activated = True

        finish_args = dict(kwargs)
        finish_args["wait"] = True
        finish_args["resume"] = True
        try:
            result = self._client.invoke_result(capability_id, kind=kind, **finish_args)
        except BaseException:
            settled = self._settled_task_or_none(task_id, run_id, capability_id)
            if settled is not None:
                self._runtime.retire_after_task(settled)
            raise
        settled = self._settled_task_or_none(task_id, run_id, capability_id)
        if settled is not None and (activated or state in {
            "succeeded", "completed", "failed", "cancelled", "canceled",
        }):
            self._runtime.retire_after_task(settled)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def _settled_task_or_none(
        self, task_id: str, run_id: str, capability_id: str
    ) -> dict[str, Any] | None:
        try:
            task = self._get_exact_task(task_id, run_id, capability_id)
        except Exception:  # noqa: BLE001 - retain the invocation error and leave uncertain ownership active
            return None
        state = str(task.get("state") or task.get("status") or "").casefold()
        return task if state in {"succeeded", "completed", "failed", "cancelled", "canceled"} else None

    def _get_exact_task(
        self, task_id: str, run_id: str, capability_id: str
    ) -> dict[str, Any]:
        task = _read_exact_task(self._client, task_id, capability_id=capability_id)
        if task.get("run_id") != run_id:
            raise PreparedWorkerError("Runtime returned a foreign task after admission")
        return task


def _read_exact_task(client: Any, task_id: str, *, capability_id: str) -> dict[str, Any]:
    tasks = getattr(client, "tasks", None)
    show = getattr(tasks, "show", None)
    response = show(task_id) if callable(show) else None
    if response is None:
        remote = getattr(client, "_remote", None)
        transport = getattr(remote, "_transport", None)
        get_task = getattr(transport, "get_task", None)
        response = get_task(task_id) if callable(get_task) else None
    data = getattr(response, "data", response if isinstance(response, Mapping) else None)
    if not isinstance(data, Mapping):
        raise PreparedWorkerError("Runtime exact task read is unavailable")
    task = dict(data)
    if (task.get("task_id", task.get("id")) != task_id
            or task.get("capability_id", task.get("capability")) != capability_id
            or not isinstance(task.get("execution_binding"), Mapping)):
        raise PreparedWorkerError("Runtime returned a foreign task or one without its exact binding")
    return task


def release_prepared_worker(
    client: Any, selection_path: str | Path, task_id: str, *, resume: bool = False,
) -> Mapping[str, Any]:
    """Explicitly release the exact prepared claim after its Runtime task settles."""
    claim_path = Path(selection_path).expanduser()
    claim = None
    try:
        selection_document = _read_mapping(claim_path, label="prepared worker selection")
        if (set(selection_document) != _SELECTION_FIELDS
                or selection_document.get("schema") != _SELECTION_SCHEMA):
            raise PreparedWorkerError("prepared worker selection schema or fields are invalid")
        claim_file = selection_document.get("claim_handle_path")
        if isinstance(claim_file, str) and claim_file.startswith("/"):
            claim = _read_mapping(Path(claim_file), label="P1 claim")
    except PreparedWorkerError:
        raise
    if not isinstance(claim, Mapping):
        raise PreparedWorkerError("prepared worker selection does not name a readable exact P1 claim")
    managed = claim.get("managed_worker")
    qualification = managed.get("activation_qualification") if isinstance(managed, Mapping) else None
    if claim.get("state") == "cleanup_complete":
        release_receipt = claim.get("release_quiescence")
        if (not resume or not isinstance(qualification, Mapping)
                or not isinstance(release_receipt, Mapping)
                or qualification.get("task_id") != task_id
                or qualification.get("activation_id") != release_receipt.get("activation_id")):
            raise PreparedWorkerError("completed RunPod release is not the selected task's exact receipt")
        try:
            from .worker_preparation import _validate_release_quiescence

            _validate_release_quiescence(release_receipt, claim)
        except Exception as exc:
            raise PreparedWorkerError("completed RunPod release lacks exact worker-quiescence evidence") from exc
        cleanup = claim.get("cleanup")
        if (not isinstance(cleanup, Mapping)
                or cleanup.get("pod_id") != claim.get("pod_id")
                or cleanup.get("backup_volume_id") != claim.get("network_volume_id")
                or cleanup.get("backup_volume_preserved") is not True
                or cleanup.get("status") not in {"already_gone", "terminated"}
                or claim.get("cleanup_status") != "complete"
                or claim.get("reconciliation_required") is not False):
            raise PreparedWorkerError("completed RunPod release lacks exact provider cleanup evidence")
        return dict(claim)

    selection = load_prepared_worker_selection(claim_path)
    task = _read_exact_task(client, task_id, capability_id="vibecomfy.run")
    state = str(task.get("state") or task.get("status") or "").casefold()
    if state not in {"succeeded", "completed", "failed", "cancelled", "canceled"}:
        raise PreparedWorkerError("explicit RunPod release requires the selected Runtime task to be settled")
    if (not isinstance(qualification, Mapping)
            or qualification.get("task_id") != task_id
            or qualification.get("run_id") != task.get("run_id")):
        raise PreparedWorkerError("P1 custody does not contain this exact settled Runtime activation")
    from .worker_owner import _run_sync

    runtime = _ProductionPreparedTaskRuntime(
        client=client, selection=selection,
        workflow_path=Path("/"), workflow_bundle_path=Path("/"), workflow_inputs={},
    )
    reference, preparer, runtime_owner, _launcher, _pod = runtime._components(task)
    try:
        return _run_sync(preparer.release(
            reference, runtime_owner=runtime_owner, resume=resume,
        ))
    except Exception as exc:
        if isinstance(exc, PreparedWorkerError):
            raise
        raise PreparedWorkerError("exact prepared RunPod release did not complete") from exc


def _target_reference(target: Mapping[str, Any]) -> str:
    provider = target.get("provider_account_ref")
    pod_id = target.get("pod_id")
    if not isinstance(provider, str) or not provider or not isinstance(pod_id, str) or not pod_id:
        raise PreparedWorkerError("Runtime binding has no exact RunPod target reference")
    return f"runpod:{provider}:{pod_id}"


def _task_credential_reference(value: str, task_id: str) -> str:
    """Give each Runtime task its own private remote credential directory."""
    prefix = "file:" if value.startswith("file:") else ""
    raw = value[5:] if prefix else value
    path = Path(raw)
    if (not path.is_absolute() or path.name != "executor.token"
            or ".." in path.parts or str(path) != raw):
        raise PreparedWorkerError("deployment credential reference must name an absolute executor.token path")
    task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return prefix + str(path.parent / "tasks" / task_key / "executor.token")


def _reference_for_task(
    selection: PreparedWorkerSelection, client: Any, task: Mapping[str, Any],
) -> Any:
    try:
        from runtime_protocol.remote_worker_deployment import (
            ArtifactReference,
            DeploymentReference,
            deployment_binding_from_task,
        )

        binding = deployment_binding_from_task(task)
        health = client.health()
        raw_profile = selection.deployment_profile.get("reference")
        if not isinstance(raw_profile, Mapping):
            raise PreparedWorkerError("deployment profile has no static reference")
        profile = dict(raw_profile)
        required_static_fields = {
            "deployment_id", "revision", "source_closure_digest", "data_root",
            "support_root", "runtime_schema_digest", "model_root", "capacity",
            "session_ref", "session_config_digest", "output_root", "credential_ref",
            "executor_id", "boot_manifest_path", "boot_manifest_hash",
            "readiness_profile_path", "readiness_profile_hash", "source_checkout_digest",
        }
        optional_static_fields = {
            "source_inventory_identity", "capability_matrix", "ready_file",
        }
        if set(profile) - {*required_static_fields, *optional_static_fields,
                           "dependency_closure", "executable", "pack_roots"}:
            raise PreparedWorkerError("deployment profile static reference has unknown fields")
        if required_static_fields - set(profile):
            raise PreparedWorkerError("deployment profile static reference is incomplete")
        profile["credential_ref"] = _task_credential_reference(
            str(profile["credential_ref"]), binding.admission_identity.task_id,
        )
        if profile.get("runtime_schema_digest") != health.get("schema_digest"):
            raise PreparedWorkerError("deployment profile Runtime schema digest differs from the connected Runtime")
        target = dict(binding.placement.effective_target)
        if target != dict(selection.target):
            raise PreparedWorkerError("admitted Runtime placement differs from the exact P1 claim")
        dependency_values = profile.pop("dependency_closure")
        executable_value = profile.pop("executable")
        pack_root_values = profile.pop("pack_roots", ("packs",))
        if not isinstance(dependency_values, list) or not isinstance(executable_value, Mapping):
            raise PreparedWorkerError("deployment profile artifact closure is invalid")
        closure = tuple(ArtifactReference(
            name=row["name"], path=Path(row["path"]), digest=row["digest"],
        ) for row in dependency_values if isinstance(row, Mapping))
        if len(closure) != len(dependency_values):
            raise PreparedWorkerError("deployment profile contains a malformed artifact")
        executable = ArtifactReference(
            name=executable_value["name"], path=Path(executable_value["path"]),
            digest=executable_value["digest"],
        )
        claim = _read_mapping(selection.claim_handle_path, label="P1 claim")
        from .worker_staging import _claim_identity, _remote_owned_root

        identity = _claim_identity(selection.claim_handle_path, str(claim["provider_account_ref"]))
        source_checkout = Path(_remote_owned_root(identity)) / "source" / "astrid"
        pack_roots = tuple(
            source_checkout / item if not Path(item).is_absolute() else Path(item)
            for item in pack_root_values
        )
        expected_python = selection.staging_inputs.get("python_executable")
        if not isinstance(expected_python, str) or str(executable.path) != expected_python:
            raise PreparedWorkerError("deployment executable differs from P2's selected Python")
        original_target = dict(binding.placement.original_target)
        target_ref = _target_reference(original_target)
        effective_target_ref = _target_reference(target)
        reference = DeploymentReference(
            **profile,
            task_id=binding.admission_identity.task_id,
            run_id=binding.admission_identity.run_id,
            target_ref=target_ref,
            effective_target_ref=effective_target_ref,
            executable=executable,
            dependency_closure=closure,
            runtime_endpoint=str(client.endpoint),
            runtime_instance_id=str(health["runtime_instance_id"]),
            runtime_epoch=int(health["runtime_epoch"]),
            source_checkout=source_checkout,
            pack_roots=pack_roots,
            admission_identity=binding.admission_identity,
            capability_identity=binding.capability_identity,
            input_bindings=binding.input_bindings,
            original_target=original_target,
            effective_target=target,
            placement_version=binding.placement.placement_version,
            recovery_decision_digest=binding.placement.recovery_decision_digest,
        )
        if reference.capability_identity.capability_id != "vibecomfy.run":
            raise PreparedWorkerError("prepared RunPod path accepts only ordinary vibecomfy.run tasks")
        return reference
    except PreparedWorkerError:
        raise
    except Exception as exc:
        raise PreparedWorkerError("Runtime task or deployment profile cannot form an exact deployment reference") from exc


class _ProductionPreparedTaskRuntime:
    """Lazy composition of the existing P1/P2/Runtime/host owners for one task."""

    def __init__(self, *, client: Any, selection: PreparedWorkerSelection,
                 workflow_path: Path, workflow_bundle_path: Path,
                 workflow_inputs: Mapping[str, Any]) -> None:
        self.client = client
        self.selection = selection
        self.workflow_path = workflow_path
        self.workflow_bundle_path = workflow_bundle_path
        self.workflow_inputs = dict(workflow_inputs)
        self.workspace = getattr(getattr(client, "_remote", None), "_transport", None)
        if self.workspace is None:
            raise PreparedWorkerError("prepared RunPod execution requires the explicit Runtime owner client")
        self._task: dict[str, Any] | None = None
        self._reference: Any | None = None
        self._preparer: Any | None = None
        self._runtime_owner: Any | None = None
        self._launcher: Any | None = None
        self._parked: Any | None = None
        self._qualification: dict[str, Any] | None = None
        self._local_generation: dict[str, Any] | None = None

    def _components(self, task: Mapping[str, Any]) -> tuple[Any, Any, Any, Any, Any]:
        from runpod_lifecycle import RunPodConfig, get_pod
        from runtime_protocol.remote_worker_activation import QualifiedRemoteWorkerLauncher

        from astrid.core.execution.remote_activation_owner import RuntimeRemoteActivationOwner

        from .worker_owner import RunPodRemoteWorkerInspector, RunPodRemoteWorkerPreparer, _run_sync
        from .worker_session import (
            RunPodManagedVibeComfySessionOwner,
            RunPodVibeComfySessionTransport,
        )
        from .worker_staging import RunPodPreparationTransport

        reference = _reference_for_task(self.selection, self.client, task)
        claim = _read_mapping(self.selection.claim_handle_path, label="P1 claim")
        account_env = os.environ.get("ASTRID_RUNPOD_ACCOUNT_REF", "").strip()
        if account_env != claim["provider_account_ref"]:
            raise PreparedWorkerError("ASTRID_RUNPOD_ACCOUNT_REF must match the exact P1 claim account")
        from astrid.core.util.credentials_scope import CredentialsScope

        api_key_ref = str(claim.get("api_key_ref") or "RUNPOD_API_KEY")
        credential = CredentialsScope.resolve_local("runpod", env_var=api_key_ref)
        config = RunPodConfig.from_env(api_key=credential.value)
        if (claim.get("state") in {"release_pending", "cleanup_pending"}
                and isinstance(claim.get("release_quiescence"), Mapping)):
            # The durable quiescence receipt means release resume needs only
            # the exact provider config and journal; the pod may be gone.
            from types import SimpleNamespace

            pod = SimpleNamespace(id=str(claim["pod_id"]), config=config)
        else:
            pod = _run_sync(get_pod(
                str(claim["pod_id"]), config, name=str(claim.get("name") or claim["pod_id"]),
            ))
        preparation_transport = RunPodPreparationTransport(pod)
        session_owner = RunPodManagedVibeComfySessionOwner(
            RunPodVibeComfySessionTransport(pod, preparation_transport=preparation_transport),
        )
        staging = dict(self.selection.staging_inputs)
        staging.update({
            "workflow_path": self.workflow_path,
            "workflow_bundle_path": self.workflow_bundle_path,
            "workflow_inputs": self.workflow_inputs,
            "capability_id": "vibecomfy.run",
        })
        preparer = RunPodRemoteWorkerPreparer(
            pod,
            provider_account_ref=str(claim["provider_account_ref"]),
            runtime_session_id=str(self.client.health()["runtime_session_id"]),
            claim_handle_path=self.selection.claim_handle_path,
            journal_dir=self.selection.journal_dir / hashlib.sha256(
                reference.task_id.encode("utf-8")
            ).hexdigest()[:20],
            staging_inputs=staging,
            preparation_transport=preparation_transport,
            session_owner=session_owner,
            session_port=int(self.selection.deployment_profile.get("session_port", 8188)),
        )
        runtime_owner = RuntimeRemoteActivationOwner(
            self.workspace,
            runtime_instance_id=reference.runtime_instance_id,
            runtime_session_id=str(self.client.health()["runtime_session_id"]),
            runtime_epoch=reference.runtime_epoch,
        )
        inspector = RunPodRemoteWorkerInspector(
            preparer,
            runtime_observer=lambda: {
                "runtime_instance_id": runtime_owner.runtime_instance_id,
                "runtime_session_id": runtime_owner.runtime_session_id,
                "runtime_epoch": runtime_owner.runtime_epoch,
            },
            session_config_observer=lambda handle: session_owner.observe_config(
                handle.session_selection, handle.session_receipt,
            ),
            executor_observer=self.workspace.get_executor,
        )
        launcher = QualifiedRemoteWorkerLauncher(
            runtime=runtime_owner,
            credentials=None,
            credential_control=runtime_owner.control_remote_credential,
            preparer=preparer,
            inspector=inspector,
            ttl_seconds=int(self.selection.deployment_profile.get("activation_ttl_seconds", 300)),
        )
        self._reference = reference
        self._preparer = preparer
        self._runtime_owner = runtime_owner
        self._launcher = launcher
        return reference, preparer, runtime_owner, launcher, pod

    def _relinquish_local_worker(self, preparer: Any, reference: Any) -> None:
        from astrid.sdk.workspace_client import WorkspaceClientError

        try:
            observed = self.workspace.get_local_worker_generation()
        except WorkspaceClientError as exc:
            if exc.status == 404:
                self._local_generation = None
                return
            raise
        if not isinstance(observed, Mapping):
            raise PreparedWorkerError("Runtime local-worker generation observation is invalid")
        generation = dict(observed)
        if (not all(isinstance(generation.get(key), str) and generation[key]
                    for key in ("executor_incarnation", "evidence_digest", "profile_id", "workspace_uuid"))
                or generation.get("state") not in {
                    "active", "fencing", "fenced", "stop_unknown", "stopped", "relinquished",
                }):
            raise PreparedWorkerError("Runtime local-worker generation is unresolved")
        self._local_generation = generation
        preparer.record_local_generation(generation)
        if generation.get("state") != "relinquished":
            result = self.workspace.relinquish_local_worker(
                generation["executor_incarnation"], generation["evidence_digest"],
            )
            if (not isinstance(result, Mapping)
                    or result.get("state") != "relinquished"
                    or result.get("executor_incarnation") != generation["executor_incarnation"]):
                raise PreparedWorkerError("Runtime did not relinquish the exact local worker generation")

    def ensure_for_task(self, task: Mapping[str, Any]) -> None:
        self._task = dict(task)
        reference, preparer, runtime_owner, launcher, _pod = self._components(task)
        claim = _read_mapping(self.selection.claim_handle_path, label="P1 claim")
        if claim.get("state") != "claimed":
            raise PreparedWorkerError("prepared activation requires the same active P1 claim")
        managed = claim.get("managed_worker")
        if isinstance(managed, Mapping) and managed.get("state") not in {"stopped", "released"}:
            state = str(managed.get("state") or "")
            qualification = managed.get("activation_qualification")
            if not isinstance(qualification, Mapping):
                if state not in {"parked_process", "parked"}:
                    raise PreparedWorkerError("existing P1 worker activation is unresolved; refusing replacement")
                try:
                    preparer.abort_parked(reference)
                    self._restore_local_worker()
                except Exception as exc:
                    raise PreparedWorkerError("exact unqualified RunPod process could not be safely replaced") from exc
            elif state in {"ready", "ready_observed_by_preparer", "committed"}:
                try:
                    parked = launcher.restore_prepared_activation(task, reference, qualification)
                    qualification = launcher.reconcile_activation(task, reference, parked, qualification)
                except Exception as exc:
                    if state == "committed":
                        raise PreparedWorkerError("committed RunPod activation could not be reconciled") from exc
                    self._retire_uncommitted_generation(
                        task, reference, preparer, runtime_owner, qualification,
                    )
                    if str(task.get("state") or task.get("status") or "").casefold() != "queued":
                        raise PreparedWorkerError("uncommitted activation was retired; task is no longer queued") from exc
                else:
                    self._parked, self._qualification = parked, dict(qualification)
                    return
            elif state in {
                "qualification", "credential", "credential_delivered", "bootstrapped",
                "acknowledged", "enabled",
            }:
                self._retire_uncommitted_generation(
                    task, reference, preparer, runtime_owner, qualification,
                )
                if str(task.get("state") or task.get("status") or "").casefold() != "queued":
                    raise PreparedWorkerError("uncommitted activation was retired; task is no longer queued")
            elif state == "cleanup_pending":
                self._retire_uncommitted_generation(
                    task, reference, preparer, runtime_owner, qualification,
                )
                if str(task.get("state") or task.get("status") or "").casefold() != "queued":
                    raise PreparedWorkerError("uncommitted cleanup completed; task is no longer queued")
            else:
                raise PreparedWorkerError(f"unsupported persisted RunPod activation phase: {state or 'unknown'}")
        elif isinstance(managed, Mapping):
            self._restore_local_worker()
        try:
            parked = launcher.park(reference, target=reference.effective_target)
            self._relinquish_local_worker(preparer, reference)
            qualification = launcher.activate(task, reference, parked)
        except Exception:
            if launcher.activation_state == "inactive":
                self._restore_local_worker()
            raise
        self._parked, self._qualification = parked, dict(qualification)

    def _retire_uncommitted_generation(
        self, task: Mapping[str, Any], reference: Any, preparer: Any,
        runtime_owner: Any, qualification: Mapping[str, Any],
    ) -> None:
        activation_id = qualification.get("activation_id")
        if not isinstance(activation_id, str) or not activation_id:
            raise PreparedWorkerError("persisted uncommitted activation has no exact generation ID")
        placement = {
            "actual": dict(qualification.get("effective_target") or {}),
            "verification": {
                "method": "credential_claim", "verified": True,
                "evidence_digest": qualification.get("evidence_digest"),
            },
            "executor_incarnation": qualification.get("executor_incarnation"),
        }
        try:
            revoked = runtime_owner.control_remote_credential(str(task["task_id"]), {
                "action": "revoke-uncommitted",
                "qualification": dict(qualification),
                "placement": placement,
            })
            result = preparer.abort_uncommitted(
                reference, activation_id=activation_id, runtime_revocation=revoked,
            )
            if (result.get("stopped") is not True
                    or result.get("activation_id") != activation_id
                    or result.get("credential_removed") is not True):
                raise PreparedWorkerError("exact uncommitted RunPod cleanup receipt is incomplete")
            self._restore_local_worker()
        except Exception as exc:
            raise PreparedWorkerError(
                "uncommitted RunPod activation remains unresolved; refusing replacement"
            ) from exc

    def _restore_local_worker(self) -> None:
        generation = self._local_generation
        if generation is None:
            claim = _read_mapping(self.selection.claim_handle_path, label="P1 claim")
            managed = claim.get("managed_worker")
            generation = managed.get("local_worker_generation") if isinstance(managed, Mapping) else None
        if not isinstance(generation, Mapping):
            return
        profile_id, workspace_uuid = generation.get("profile_id"), generation.get("workspace_uuid")
        if not isinstance(profile_id, str) or not isinstance(workspace_uuid, str):
            raise PreparedWorkerError("saved local-worker restoration identity is incomplete")
        try:
            current = self.workspace.get_local_worker_generation()
        except Exception as exc:  # noqa: BLE001 - uncertainty must block a second local worker
            raise PreparedWorkerError(
                "Runtime local-worker generation could not be observed before restoration"
            ) from exc
        if isinstance(current, Mapping) and current.get("state") == "active":
            if current.get("profile_id") != profile_id or current.get("workspace_uuid") != workspace_uuid:
                raise PreparedWorkerError("a different local-worker generation became active")
            return
        self.workspace.start_local_worker(profile_id, workspace_uuid)
        current = self.workspace.get_local_worker_generation()
        if (not isinstance(current, Mapping) or current.get("state") != "active"
                or current.get("profile_id") != profile_id
                or current.get("workspace_uuid") != workspace_uuid):
            raise PreparedWorkerError("Runtime did not restore the selected local-worker profile")

    def retire_after_task(self, task: Mapping[str, Any]) -> None:
        managed = _read_mapping(self.selection.claim_handle_path, label="P1 claim").get("managed_worker")
        if not isinstance(managed, Mapping):
            return
        state = str(managed.get("state") or "")
        qualification = managed.get("activation_qualification")
        if state in {"stopped", "released"}:
            self._restore_local_worker()
            return
        if (isinstance(qualification, Mapping)
                and (qualification.get("task_id") != task.get("task_id")
                     or qualification.get("run_id") != task.get("run_id"))):
            # Recovering an older terminal receipt must not disturb a newer
            # task that currently owns this claim.
            return
        if self._reference is None or self._preparer is None or self._runtime_owner is None:
            reference, preparer, runtime_owner, launcher, _pod = self._components(task)
        else:
            reference, preparer, runtime_owner, launcher = (
                self._reference, self._preparer, self._runtime_owner, self._launcher,
            )
        from .worker_owner import _run_sync

        if not isinstance(qualification, Mapping):
            if state not in {"parked_process", "parked"}:
                raise PreparedWorkerError("terminal task found an unresolved RunPod preparation phase")
            preparer.abort_parked(reference)
            self._restore_local_worker()
            return
        if state in {
            "qualification", "credential", "credential_delivered", "bootstrapped",
            "acknowledged", "enabled", "cleanup_pending",
        }:
            self._retire_uncommitted_generation(
                task, reference, preparer, runtime_owner, qualification,
            )
            return
        if state in {"ready", "ready_observed_by_preparer"}:
            try:
                parked = launcher.restore_prepared_activation(task, reference, qualification)
                qualification = launcher.reconcile_activation(task, reference, parked, qualification)
            except Exception:  # noqa: BLE001 - reconcile the exact activation before any cleanup fallback
                try:
                    _run_sync(preparer.retire_task(
                        reference, runtime_owner=runtime_owner, qualification=qualification,
                    ))
                except Exception:  # noqa: BLE001 - uncommitted cleanup is the only safe fallback
                    self._retire_uncommitted_generation(
                        task, reference, preparer, runtime_owner, qualification,
                    )
                self._restore_local_worker()
                return

        _run_sync(preparer.retire_task(
            reference, runtime_owner=runtime_owner, qualification=qualification,
        ))
        self._restore_local_worker()


def _production_composition_factory(**kwargs: Any) -> PreparedTaskRuntime:
    try:
        return _ProductionPreparedTaskRuntime(**kwargs)
    except PreparedWorkerError:
        raise
    except Exception as exc:
        raise PreparedWorkerError("prepared RunPod production composition could not be constructed") from exc


__all__ = [
    "PreparedWorkerSelection", "PreparedRunPodTaskClient", "PreparedTaskRuntime",
    "PreparedWorkerError", "load_prepared_worker_selection", "release_prepared_worker",
]
