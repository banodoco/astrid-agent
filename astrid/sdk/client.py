"""Public Astrid SDK client backed exclusively by the workspace runtime.

The client is deliberately only a transport composition root. It does not
open a database, acquire a store lock, load repositories, or construct a
local application. Runtime discovery and credentials are resolved by
``workspace_client`` and product families are exposed by ``remote``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

from banodoco_workspace_client.contract_metadata import (
    COMPONENT_MANIFEST_SHA256,
    PROTOCOL,
    SCHEMA_DIGEST,
)

__all__ = ["AstridClient"]


_REQUIRED_SCOPES = frozenset(
    {
        "projects:read",
        "projects:write",
        "objects:read",
        "objects:write",
        "tasks:read",
        "tasks:write",
    }
)
_HEALTH_KEYS = frozenset(
    {
        "status",
        "protocol",
        "runtime_epoch",
        "runtime_instance_id",
        "runtime_session_id",
        "schema_digest",
    }
)
_HANDSHAKE_KEYS = frozenset(
    {
        "protocol",
        "schema_digest",
        "session_id",
        "actor_id",
        "realm_id",
        "scopes",
        "capabilities",
    }
)
_TARGETED_EXECUTION_BINDING_CAPABILITY = "execution_binding.targeted.v1"
# Pairing is owned by the Runtime lifecycle.  This deliberately does not tell
# a caller to check out a historical tree or kill a process by PID.
RUNTIME_PAIRING_FIX = (
    "use the owner-managed Astrid Runtime lifecycle (`banodoco-local down --profile astrid`, "
    "then `banodoco-local up --profile astrid`) against the installed workspace contract; "
    "do not switch checkouts or kill a PID"
)


def _protocol_error(field: str, message: str, *, actual_digest: str | None = None) -> Any:
    from astrid.sdk.workspace_client import WorkspaceClientError

    details: dict[str, Any] = {
        "field": field,
        "expected_schema_digest": SCHEMA_DIGEST,
        "expected_component_manifest": COMPONENT_MANIFEST_SHA256,
        "next_action": "reconfigure the workspace runtime with `banodoco-local up --profile astrid`",
    }
    if actual_digest is not None:
        details["actual_schema_digest"] = actual_digest
    return WorkspaceClientError(
        0,
        "protocol_error",
        message,
        details,
    )


def _identity_error(field: str, message: str) -> Any:
    from astrid.sdk.workspace_client import WorkspaceClientError

    return WorkspaceClientError(
        0,
        "identity_mismatch",
        message,
        {
            "field": field,
            "next_action": "reconfigure the workspace runtime with `banodoco-local up --profile astrid`",
        },
    )


def _safe_failure_text(value: object) -> str:
    from astrid.sdk.exceptions import _redact_message

    return _redact_message(str(value))


def _digest_mismatch(field: str, actual: object, expected: str) -> Any:
    observed = actual if isinstance(actual, str) and actual else None
    return _protocol_error(
        f"{field}.schema_digest",
        f"runtime {field} schema digest {observed or 'is missing'} does not match this Astrid "
        f"client's expected {expected}",
        actual_digest=observed,
    )


def _require_mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _protocol_error(field, f"runtime {field} response must be an object")
    return value


def _validate_health(value: Any, *, expected_protocol: str, expected_digest: str) -> None:
    health = _require_mapping(value, field="health")
    if frozenset(health) != _HEALTH_KEYS:
        raise _protocol_error("health", "runtime health response has an unexpected schema")
    if health.get("status") != "ok":
        raise _protocol_error("health.status", "runtime health status is not ok")
    if health.get("protocol") != expected_protocol:
        raise _protocol_error("health.protocol", "runtime health protocol does not match workspace.v1")
    if health.get("schema_digest") != expected_digest:
        raise _digest_mismatch("health", health.get("schema_digest"), expected_digest)
    epoch = health.get("runtime_epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise _protocol_error("health.runtime_epoch", "runtime health runtime_epoch is invalid")
    for field in ("runtime_instance_id", "runtime_session_id"):
        identity = health.get(field)
        if not isinstance(identity, str) or not identity.strip():
            raise _protocol_error(f"health.{field}", f"runtime health {field} is invalid")


def _validate_handshake(
    value: Any,
    *,
    expected_protocol: str,
    expected_digest: str,
    expected_realm: str,
    expected_actor: str,
) -> None:
    handshake = _require_mapping(value, field="handshake")
    if frozenset(handshake) != _HANDSHAKE_KEYS:
        raise _protocol_error("handshake", "runtime handshake response has an unexpected schema")
    if handshake.get("protocol") != expected_protocol:
        raise _protocol_error("handshake.protocol", "runtime handshake protocol does not match workspace.v1")
    if handshake.get("schema_digest") != expected_digest:
        raise _digest_mismatch("handshake", handshake.get("schema_digest"), expected_digest)
    session_id = handshake.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise _protocol_error("handshake.session_id", "runtime handshake session_id is missing")
    if handshake.get("realm_id") != expected_realm:
        raise _identity_error("handshake.realm_id", "runtime handshake realm_id does not match the explicit client context")
    if handshake.get("actor_id") != expected_actor:
        raise _identity_error("handshake.actor_id", "runtime handshake actor_id does not match the explicit client context")
    scopes = handshake.get("scopes")
    if not isinstance(scopes, (list, tuple)) or any(
        not isinstance(scope, str) or not scope.strip() for scope in scopes
    ):
        raise _protocol_error("handshake.scopes", "runtime handshake scopes must be non-empty strings")
    if len(set(scopes)) != len(scopes) or frozenset(scopes) != _REQUIRED_SCOPES:
        raise _protocol_error("handshake.scopes", "runtime handshake did not grant exactly the required scopes")
    capabilities = handshake.get("capabilities")
    if not isinstance(capabilities, (list, tuple)) or any(
        not isinstance(capability, str) or not capability.strip()
        for capability in capabilities
    ):
        raise _protocol_error(
            "handshake.capabilities",
            "runtime handshake capabilities must be non-empty strings",
        )
    if _TARGETED_EXECUTION_BINDING_CAPABILITY not in capabilities:
        raise _protocol_error(
            "handshake.capabilities",
            "runtime handshake did not advertise targeted execution binding support",
        )


class AstridClient:
    """Context-managed client for the remote workspace product contract."""

    def __init__(self, *, remote: Any) -> None:
        self._remote = remote

    @classmethod
    def open(
        cls,
        *,
        endpoint: str | None = None,
        credential: str | Path | None = None,
        realm_id: str | None = None,
        actor_id: str | None = None,
        client_name: str | None = None,
        client_version: str | None = None,
        protocol_version: str | None = None,
    ) -> Self:
        """Connect using a fully explicit, validated runtime context.

        Cold launch belongs to the explicit Astrid launcher boundary. An
        ordinary SDK construction never retries by invoking a process.
        """
        from astrid.sdk.exceptions import ServiceUnavailableError
        from astrid.sdk.remote import RemoteAstridClient
        from astrid.sdk.workspace_client import (
            WorkspaceClient,
            resolve_runtime_connection,
        )

        if protocol_version != PROTOCOL:
            raise ServiceUnavailableError(
                "unsupported runtime protocol; run `banodoco-local up --profile astrid`",
                details={"next_action": "banodoco-local up --profile astrid"},
            )
        if not all(isinstance(value, str) and value.strip() for value in (endpoint, realm_id, actor_id, client_name, client_version)) or not isinstance(credential, (str, Path)) or (isinstance(credential, str) and not credential.strip()):
            raise ServiceUnavailableError(
                "runtime realm, actor, and client identity are required; run `banodoco-local up --profile astrid`",
                details={"next_action": "banodoco-local up --profile astrid"},
            )

        def unavailable(exc: Exception, *, operation: str | None = None) -> ServiceUnavailableError:
            from astrid.core.audit.util import redact

            raw_fields = getattr(exc, "details", {})
            fields = redact(dict(raw_fields)) if isinstance(raw_fields, Mapping) else {}
            cause = exc.__cause__ if exc.__cause__ is not None else exc
            reason = str(getattr(exc, "code", "") or "unavailable")
            cause_code = str(getattr(cause, "code", "") or "")
            cause_name = type(cause).__name__
            if (
                operation in {"health", "handshake"}
                and (reason == "invalid_response" or cause_code == "invalid_response" or cause_name in {"AttributeError", "IndexError", "KeyError", "TypeError", "ValueError"})
            ):
                reason = "protocol_error"
            original = _safe_failure_text(getattr(exc, "message", "") or exc)
            cause_message = _safe_failure_text(getattr(cause, "message", "") or cause)
            details: dict[str, Any] = {
                **(fields if isinstance(fields, Mapping) else {}),
                "reason": reason,
                "cause_class": cause_name,
                "cause_message": cause_message,
            }
            if operation is not None:
                details["operation"] = operation
            message = (
                f"runtime request failed ({reason}: {original}); reconfigure the Astrid runtime "
                "with `banodoco-local up --profile astrid`"
            )
            if reason == "protocol_error":
                details.setdefault("expected_schema_digest", SCHEMA_DIGEST)
                details.setdefault("expected_component_manifest", COMPONENT_MANIFEST_SHA256)
                observed = details.get("actual_schema_digest")
                if not isinstance(observed, str) or not observed:
                    observed = "not reported"
                details["pairing_hint"] = RUNTIME_PAIRING_FIX
                subject = operation or "runtime"
                message = (
                    f"runtime {subject} response does not match this Astrid client contract ({original}); "
                    f"expected schema digest {SCHEMA_DIGEST} and component manifest "
                    f"{COMPONENT_MANIFEST_SHA256}, runtime reports {observed}."
                )
            elif reason == "identity_mismatch":
                message = (
                    f"runtime rejected the explicit client context ({original}); reconfigure the Astrid "
                    "runtime with `banodoco-local up --profile astrid`"
                )
            details["next_action"] = "banodoco-local up --profile astrid"
            return ServiceUnavailableError(
                message,
                details=details,
            )

        def connect(endpoint_value: str, token: str) -> Any:
            workspace = WorkspaceClient(endpoint_value, token)
            try:
                _validate_health(workspace.health(), expected_protocol=PROTOCOL, expected_digest=SCHEMA_DIGEST)
            except Exception as exc:
                raise unavailable(exc, operation="health") from exc
            try:
                handshake = workspace.handshake(
                    client_name,
                    client_version,
                    [
                        "projects:read",
                        "projects:write",
                        "objects:read",
                        "objects:write",
                        "tasks:read",
                        "tasks:write",
                    ],
                )
                _validate_handshake(
                    handshake,
                    expected_protocol=PROTOCOL,
                    expected_digest=SCHEMA_DIGEST,
                    expected_realm=realm_id,
                    expected_actor=actor_id,
                )
            except Exception as exc:
                raise unavailable(exc, operation="handshake") from exc
            workspace.actor_id = actor_id
            return workspace

        try:
            endpoint_value, token = resolve_runtime_connection(endpoint, credential)
            workspace = connect(endpoint_value, token)
            return cls(remote=RemoteAstridClient(workspace))
        except ServiceUnavailableError:
            raise
        except Exception as exc:
            raise unavailable(exc) from exc

    @classmethod
    def open_from_launcher(
        cls,
        *,
        credential: str | Path | None = None,
        client_name: str = "astrid",
        client_version: str = "stage1",
        protocol_version: str = "workspace.v1",
        start_pack_host: bool = True,
    ) -> Self:
        """Launch the neutral runtime at Astrid's explicit CLI boundary.

        The credential is still explicit: the launcher may supply it as a
        path/token, or the caller may opt into the documented
        ``BANODOCO_RUNTIME_CREDENTIAL`` setting.  No SDK operation invokes
        this method implicitly.
        """
        import os

        from astrid.sdk.autobootstrap import AutoBootstrapError, ensure_runtime
        from astrid.sdk.exceptions import ServiceUnavailableError

        # The timeline evaluation loop supplies an explicit, case-local
        # Runtime connection to the worker.  Prefer that connection over the
        # normal launcher path so `python -m astrid timelines ...` exercises
        # the same public SDK surface against the disposable case realm.  It
        # is deliberately all-or-nothing: a partial override must never fall
        # through to an ambient canonical Runtime.
        eval_endpoint = os.environ.get("ASTRID_TIMELINE_EVAL_ENDPOINT", "").strip()
        eval_credential = os.environ.get("ASTRID_TIMELINE_EVAL_CREDENTIAL", "").strip()
        eval_realm = os.environ.get("ASTRID_TIMELINE_EVAL_REALM_ID", "").strip()
        eval_actor = os.environ.get("ASTRID_TIMELINE_EVAL_ACTOR_ID", "").strip()
        if any((eval_endpoint, eval_credential, eval_realm, eval_actor)):
            if not all((eval_endpoint, eval_credential, eval_realm, eval_actor)):
                raise ServiceUnavailableError(
                    "case-local Runtime connection is incomplete",
                    details={"next_action": "prepare a complete timeline evaluation Runtime context"},
                )
            return cls.open(
                endpoint=eval_endpoint,
                credential=Path(eval_credential),
                realm_id=eval_realm,
                actor_id=eval_actor,
                client_name=client_name,
                client_version=client_version,
                protocol_version=protocol_version,
            )

        try:
            result = (
                ensure_runtime()
                if start_pack_host
                else ensure_runtime(start_pack_host=False)
            )
        except AutoBootstrapError as exc:
            lifecycle_details = dict(getattr(exc, "details", {}) or {})
            lifecycle_details.setdefault("lifecycle_code", exc.code)
            lifecycle_details.setdefault("next_action", exc.next_action)
            raise ServiceUnavailableError(
                str(exc), details=lifecycle_details
            ) from exc
        if credential is not None:
            credential_value: str | Path = credential
        else:
            # The environment variable is a launcher-issued path, not an
            # ambient bearer token. Keep it typed as a Path so the explicit
            # credential boundary rejects symlinks before reading it.
            credential_env = os.environ.get("BANODOCO_RUNTIME_CREDENTIAL", "").strip()
            credential_value = Path(credential_env) if credential_env else ""
        if not credential_value:
            # `up --json` deliberately hands off a file path, not the secret.
            # Let the explicit client boundary read that owner-only file.
            credential_file = result.get("credential_file", "")
            credential_value = Path(str(credential_file)) if credential_file else ""
        if not credential_value:
            raise ServiceUnavailableError(
                "runtime credential is required; run `banodoco-local up --profile astrid`",
                details={"next_action": "banodoco-local up --profile astrid"},
            )
        return cls.open(
            endpoint=str(result.get("endpoint", "")),
            credential=credential_value,
            realm_id=str(result.get("realm_id", "")),
            actor_id=str(result.get("actor_id", "")),
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
        )

    @classmethod
    def open_existing_from_launcher(
        cls,
        *,
        client_name: str = "astrid",
        client_version: str = "stage1",
        protocol_version: str = "workspace.v1",
    ) -> Self:
        """Connect to the selected, already-running Runtime without starting it.

        This route is for the pure ``tasks list`` observer.  It only asks the
        installed Runtime CLI to inspect the selected workspace and observe
        status, then opens the normal explicit SDK boundary.  In particular,
        it does not call the acquisition/bootstrap path or create its lock.
        """
        import json
        import os
        import stat
        import sys

        from astrid.runtime_cli import RuntimeCLI, RuntimeCLIError, validate_selected_workspace
        from astrid.sdk.exceptions import ServiceUnavailableError
        from astrid.sdk.storage_root import resolve_runtime_data_root

        next_action = "banodoco-local up --profile astrid"

        def unavailable(message: str, *, reason: str = "runtime_unavailable") -> ServiceUnavailableError:
            return ServiceUnavailableError(
                message,
                details={"reason": reason, "next_action": next_action},
            )

        # Preserve the explicit, case-local connection used by the timeline
        # evaluation loop.  Partial overrides remain fail-closed and this path
        # never falls through to an ambient/default Runtime.
        eval_endpoint = os.environ.get("ASTRID_TIMELINE_EVAL_ENDPOINT", "").strip()
        eval_credential = os.environ.get("ASTRID_TIMELINE_EVAL_CREDENTIAL", "").strip()
        eval_realm = os.environ.get("ASTRID_TIMELINE_EVAL_REALM_ID", "").strip()
        eval_actor = os.environ.get("ASTRID_TIMELINE_EVAL_ACTOR_ID", "").strip()
        if any((eval_endpoint, eval_credential, eval_realm, eval_actor)):
            if not all((eval_endpoint, eval_credential, eval_realm, eval_actor)):
                raise unavailable(
                    "case-local Runtime connection is incomplete",
                    reason="runtime_context_incomplete",
                )
            return cls.open(
                endpoint=eval_endpoint,
                credential=Path(eval_credential),
                realm_id=eval_realm,
                actor_id=eval_actor,
                client_name=client_name,
                client_version=client_version,
                protocol_version=protocol_version,
            )

        try:
            support_root = resolve_runtime_data_root()
            runtime = RuntimeCLI()
            selected = runtime.inspect(support_root=support_root, timeout=5.0)
            if not selected.ok:
                raise unavailable(
                    "the selected workspace is not configured or is unavailable",
                    reason=str(selected.data.get("problem_code") or "workspace_missing"),
                )

            selected_data = selected.data
            realm_id = str(selected_data.get("realm_id") or "")
            realm_root = selected_data.get("realm_root")
            if not realm_id or not isinstance(realm_root, str) or not realm_root:
                raise unavailable(
                    "Runtime workspace inspection did not identify one selected workspace",
                    reason="workspace_ambiguous",
                )
            validate_selected_workspace(
                selected_data,
                expected_realm_id=realm_id,
                expected_realm_root=realm_root,
                expected_support_root=support_root,
            )

            observed = runtime.observe("status", support_root=support_root, timeout=5.0)
            if not observed.ok:
                raise unavailable(
                    "the selected Runtime is not healthy and available",
                    reason=str(observed.data.get("problem_code") or "runtime_unavailable"),
                )
            discovery = observed.data.get("discovery")
            support = observed.data.get("support")
            health = observed.data.get("health")
            if not isinstance(discovery, Mapping) or not isinstance(support, Mapping) or not isinstance(health, Mapping):
                raise unavailable("Runtime status did not return a complete live identity")
            if support.get("healthy") is not True:
                raise unavailable("the selected Runtime is not healthy")

            instance_id = str(discovery.get("runtime_instance_id") or "")
            endpoint = str(discovery.get("endpoint") or "")
            discovered_root = str(discovery.get("realm_root") or "")
            if (
                str(discovery.get("active_realm") or "") != realm_id
                or not discovered_root
                or Path(discovered_root).absolute() != Path(realm_root).absolute()
                or not instance_id
                or str(health.get("runtime_instance_id") or "") != instance_id
                or health.get("status") != "ok"
                or health.get("protocol") != PROTOCOL
                or health.get("schema_digest") != SCHEMA_DIGEST
            ):
                raise unavailable(
                    "Runtime status does not match the selected workspace and installed contract",
                    reason="workspace_identity_mismatch",
                )

            credential_file = support_root / "credentials" / "astrid.json"
            if str(discovery.get("credential_file") or "") != str(credential_file):
                raise unavailable(
                    "Runtime did not advertise the selected Astrid credential",
                    reason="credential_identity_mismatch",
                )
            credential_path = credential_file.absolute()
            if ".." in credential_path.parts:
                raise unavailable("Runtime credential path is invalid", reason="credential_identity_mismatch")

            # Read only the non-secret profile identity.  The SDK's normal
            # WorkspaceClient boundary separately opens the same file with
            # no-follow checks to obtain the token and performs health plus
            # handshake validation before returning a client.
            descriptor = os.open(credential_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise unavailable("Runtime Astrid credential is not a regular file", reason="credential_invalid")
                if sys.platform == "darwin":
                    import fcntl

                    raw_path = fcntl.fcntl(handle.fileno(), fcntl.F_GETPATH, bytes(1024))
                    opened_path = os.fsdecode(raw_path.split(b"\0", 1)[0])
                elif sys.platform.startswith("linux"):
                    opened_path = os.readlink(f"/proc/self/fd/{handle.fileno()}")
                else:
                    opened_path = str(credential_path.resolve(strict=True))
                if opened_path != str(credential_path):
                    raise unavailable("Runtime Astrid credential path is unsafe", reason="credential_invalid")
                metadata = json.load(handle)
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("version") != 1
                or metadata.get("scope") != "astrid"
                or not isinstance(metadata.get("actor_id"), str)
                or not metadata.get("actor_id", "").strip()
                or not isinstance(metadata.get("token"), str)
                or not metadata.get("token", "").strip()
            ):
                raise unavailable("Runtime Astrid credential metadata is invalid", reason="credential_invalid")
            actor_id = str(metadata["actor_id"])
        except ServiceUnavailableError:
            raise
        except (RuntimeCLIError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise unavailable(
                "the selected Runtime is unavailable; run `banodoco-local up --profile astrid`",
                reason=getattr(exc, "code", "runtime_unavailable"),
            ) from exc

        return cls.open(
            endpoint=endpoint,
            credential=credential_path,
            realm_id=realm_id,
            actor_id=actor_id,
            client_name=client_name,
            client_version=client_version,
            protocol_version=protocol_version,
        )

    def invoke_result(self, capability_id: str, *, kind: str, **kwargs: Any) -> Any:
        """Invoke through this explicit runtime-bound client as a result.

        The module-level helper owns the typed preflight/result conversion;
        this method supplies the already-authenticated client so admission
        cannot silently compose a second local authority.
        """
        from astrid.sdk.invocation import invoke_result

        kwargs.setdefault("client", self)
        return invoke_result(capability_id, kind=kind, **kwargs)

    def open_authoring_target(self, target: Any) -> Any:
        """Open the existing detached authoring seam on this bound connection."""
        from astrid.sdk.authoring_remote import TargetBoundAuthoringBundle

        return TargetBoundAuthoringBundle(self._remote._transport, target)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    @property
    def endpoint(self) -> str:
        """Connected Runtime URL for a same-connection authoring target.

        Credentials remain private to the connection; this exposes only the
        identity already required by the target-bound authoring seam.
        """
        endpoint = self._remote._transport.endpoint
        return str(getattr(endpoint, "url", endpoint))

    @property
    def documents(self) -> Any:
        return self._remote.documents

    @property
    def preferences(self) -> Any:
        return self._remote.preferences

    @property
    def projects(self) -> Any:
        return self._remote.projects

    @property
    def timelines(self) -> Any:
        return self._remote.timelines

    @property
    def media(self) -> Any:
        return self._remote.media

    @property
    def tasks(self) -> Any:
        return self._remote.tasks

    @property
    def runs(self) -> Any:
        return self._remote.runs

    @property
    def references(self) -> Any:
        return self._remote.references

    @property
    def shots(self) -> Any:
        return self._remote.shots

    @property
    def generations(self) -> Any:
        return self._remote.generations

    def handshake(self, client_name: str, client_version: str, requested_scopes: list[str]) -> Any:
        return self._remote.handshake(client_name, client_version, requested_scopes)

    def doctor(self) -> Any:
        return self._remote.doctor()

    def create_backup(self, destination: str) -> Any:
        return self._remote.create_backup(destination)

    def restore_backup(self, backup: str, destination: str) -> Any:
        return self._remote.restore_backup(backup, destination)

    def export_realm(self) -> Any:
        return self._remote.export_realm()

    def tombstone_realm(self, *, reason: str | None = None, expected_version: int | None = None) -> Any:
        return self._remote.tombstone_realm(reason=reason, expected_version=expected_version)

    def recover_realm(self, *, expected_realm_id: str, expected_version: int, confirmation: str | None = None, noninteractive: bool = False) -> Any:
        return self._remote.recover_realm(expected_realm_id=expected_realm_id, expected_version=expected_version, confirmation=confirmation, noninteractive=noninteractive)

    def purge_realm(self, confirmation: str) -> Any:
        return self._remote.purge_realm(confirmation)

    def health(self) -> Any:
        from astrid.sdk.exceptions import ServiceUnavailableError
        from astrid.sdk.workspace_client import WorkspaceClientError

        try:
            return self._remote.health()
        except WorkspaceClientError as exc:
            raise ServiceUnavailableError(
                "runtime unavailable; run `banodoco-local up --profile astrid`",
                details={
                    "next_action": "banodoco-local up --profile astrid",
                    "reason": exc.code,
                },
            ) from exc

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return self._remote.invoke(*args, **kwargs)
