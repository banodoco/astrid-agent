"""Explicit transport adapter around the generated workspace client.

HTTP protocol encoding, authentication, and response decoding are delegated
to the generated ``banodoco_workspace_client`` package from the runtime
contract. Endpoint and credential values are supplied by the caller.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import os
import stat
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from banodoco_workspace_client import WorkspaceClient as GeneratedWorkspaceClient
from banodoco_workspace_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST
from banodoco_workspace_client.generated import CommitRequest, CommitResult

from .pagination import page_pair, paged_rows

__all__ = [
    "GeneratedWorkspaceClient",
    "PROTOCOL",
    "SCHEMA_DIGEST",
    "WorkspaceClient",
    "WorkspaceClientError",
    "paged_rows",
    "resolve_runtime_connection",
]

_DOCUMENT_CONTENT_UNSET = object()

RECONFIGURE_ACTION = "reconfigure the workspace runtime with `banodoco-local up --profile astrid`"


def _reconfigure(field: str, message: str) -> "WorkspaceClientError":
    return WorkspaceClientError(
        0,
        "reconfigure_required",
        message,
        {"field": field, "next_action": RECONFIGURE_ACTION},
    )


def _safe_local_path(value: str | Path, *, field: str) -> Path:
    """Return a regular local path, rejecting symlinked discovery inputs.

    Launcher result paths are security-sensitive: resolving a symlink before
    checking it would let a changed manifest or credential path silently point
    at an unrelated file.  Check the lexical path and every existing parent
    first, then let the caller check the file type/content.
    """
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = Path(path.absolute())
    current = path
    while True:
        try:
            if current.is_symlink():
                raise _reconfigure(field, f"{field} path must not be a symlink; {RECONFIGURE_ACTION}")
        except OSError as exc:
            raise _reconfigure(field, f"cannot inspect {field} path; {RECONFIGURE_ACTION}") from exc
        if current == current.parent:
            break
        current = current.parent
    return path


def validate_runtime_endpoint(endpoint: str) -> str:
    """Validate an explicit runtime URL without permitting network pivots."""
    value = str(endpoint).strip()
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing port validates malformed values such as ``:not-a-port``.
        _ = parsed.port
    except ValueError as exc:
        raise _reconfigure("endpoint", f"runtime endpoint is malformed; {RECONFIGURE_ACTION}") from exc
    host = hostname.rstrip(".").lower() if hostname else ""
    is_loopback = host == "localhost"
    if host and not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not is_loopback
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or value.rstrip("/") in {"http:", "https:"}
    ):
        raise _reconfigure(
            "endpoint",
            "runtime endpoint must be an explicit loopback http(s) URL; " + RECONFIGURE_ACTION,
        )
    return value.rstrip("/")


class WorkspaceClientError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
        *,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.details = dict(details or {})
        self.request_id = request_id


def _read_credential(path: Path) -> str:
    # A target-bound worker may be allowed to open only this exact token file;
    # its parent directory is intentionally denied. Inspecting every parent
    # with lstat makes that narrow grant unusable. Compare the opened file's
    # kernel-resolved path with the lexical path instead: this also rejects a
    # symlink in any parent, without probing or opening those directories.
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if ".." in path.parts:
        raise _reconfigure("credential", f"credential path must not traverse a parent; {RECONFIGURE_ACTION}")
    try:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            # Retain the older all-parent inspection policy on platforms that
            # cannot obtain a no-follow descriptor for the leaf.
            path = _safe_local_path(path, field="credential")
        descriptor = os.open(path, os.O_RDONLY | nofollow)
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise _reconfigure("credential", f"runtime credential is not a regular file; {RECONFIGURE_ACTION}")
            if sys.platform == "darwin":
                import fcntl

                raw_path = fcntl.fcntl(handle.fileno(), fcntl.F_GETPATH, bytes(1024))
                opened_path = os.fsdecode(raw_path.split(b"\0", 1)[0])
            elif sys.platform.startswith("linux"):
                opened_path = os.readlink(f"/proc/self/fd/{handle.fileno()}")
            else:
                # The fallback checks each parent where descriptor-path
                # inspection is unavailable; it never silently accepts one.
                path = _safe_local_path(path, field="credential")
                opened_path = str(path)
            if not opened_path or opened_path != str(path):
                raise _reconfigure("credential", f"credential path must not contain a symlink; {RECONFIGURE_ACTION}")
            raw = handle.read().strip()
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _reconfigure("credential", f"credential path must not contain a symlink; {RECONFIGURE_ACTION}") from exc
        raise _reconfigure("credential", f"runtime credential is unavailable; {RECONFIGURE_ACTION}") from exc
    if not raw:
        raise _reconfigure("credential", f"runtime credential is empty; {RECONFIGURE_ACTION}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    token = value.get("token") if isinstance(value, dict) else None
    if not isinstance(token, str) or not token:
        raise _reconfigure("credential", f"runtime credential is invalid; {RECONFIGURE_ACTION}")
    return token


def resolve_runtime_connection(
    endpoint: str | None = None,
    credential: str | Path | None = None,
) -> tuple[str, str]:
    """Validate a runtime connection supplied by the caller or host boundary.

    Pack executors run in a host-created child process.  The host may provide
    the same explicit connection through ``BANODOCO_RUNTIME_ENDPOINT`` and
    ``BANODOCO_RUNTIME_CREDENTIAL``; this is still an explicit boundary, not
    runtime discovery.  A credential environment value is treated as an
    owner-only path when it names an existing file, otherwise as a bearer
    token already held by the host.
    """
    if endpoint is None:
        endpoint = os.environ.get("BANODOCO_RUNTIME_ENDPOINT", "")
    if credential is None:
        raw_credential = os.environ.get("BANODOCO_RUNTIME_CREDENTIAL", "").strip()
        credential = (
            Path(raw_credential)
            if raw_credential and Path(raw_credential).expanduser().is_file()
            else raw_credential
        )
    endpoint = validate_runtime_endpoint(endpoint)
    if not isinstance(credential, (str, Path)):
        raise _reconfigure("credential", f"runtime credential must be explicit and non-empty; {RECONFIGURE_ACTION}")
    if isinstance(credential, Path):
        token = _read_credential(Path(credential))
    else:
        token = str(credential).removeprefix("Bearer ").strip()
    if not token:
        raise _reconfigure("credential", f"runtime credential must be explicit and non-empty; {RECONFIGURE_ACTION}")
    return endpoint, token


class WorkspaceClient:
    """Small typed facade over the runtime's generated workspace client.

    This class deliberately contains no HTTP protocol code or local discovery.
    It translates only generated exceptions into Astrid's stable error type.
    """

    def __init__(self, endpoint: str, token: str):
        self.endpoint, self.token = resolve_runtime_connection(endpoint, token)
        self._generated = GeneratedWorkspaceClient(self.endpoint, self.token)
        # Populated after the explicit Astrid handshake.  On-demand local
        # Runtime work must use the authenticated actor as its executor.
        self.actor_id: str | None = None
        self._last_handshake: Any | None = None

    def _call_generated(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke one generated operation and normalize its typed value."""
        try:
            # Resolve exactly one method on demand.  The generated contract is
            # versioned independently of Astrid: a partial client used for a
            # narrow operation (for example ``settle_attempt``) must not fail
            # while constructing a map that eagerly looks up unrelated newer
            # operations such as ``health``.
            operations = {
                "health", "handshake", "doctor", "create_backup", "restore_backup",
                "export_realm", "tombstone_realm", "recover_realm", "purge_realm",
                "create_project", "get_project", "update_project", "list_projects",
                "select_project", "current_project", "create_timeline",
                "create_timeline_document", "update_timeline_document", "list_timelines",
                "get_timeline", "get_project_timeline", "commit_timeline", "list_timeline_history", "replace_timeline_clip", "diff_timeline", "archive_timeline",
                "plan_timeline_purge", "execute_timeline_purge",
                "recover_timeline", "list_project_shots", "create_project_shot",
                "inspect_timeline", "create_timeline_view",
                "get_project_shot", "update_project_shot", "archive_project_shot",
                "recover_project_shot", "add_shot_item", "remove_shot_item",
                "get_project_shot_revision", "get_project_timeline_revision",
                "get_project_parent_composition_revision",
                "list_project_parent_composition_revisions",
                "restore_project_parent_composition_revision",
                "publish_parent_composition",
                "replace_parent_composition_media",
                "promote_project_shot_candidate", "reorder_shot_items",
                "list_project_references", "create_project_reference",
                "list_project_shot_text_bindings", "set_project_shot_text_binding",
                "get_project_shot_text_binding", "set_project_shot_text_binding_by_id",
                "rebind_project_shot_text_binding",
                "get_project_reference", "update_project_reference", "archive_project_reference",
                "recover_project_reference", "associate_reference", "set_primary_reference",
                "link_references", "create_document", "list_documents", "get_document",
                "update_document", "ingest_object", "ingest_project_object",
                "get_preferences", "update_preferences",
                "import_project_media", "get_project_media_import",
                "list_project_objects", "get_project_object_location", "create_media_relation", "list_media_relations",
                "get_object", "head_object", "admit_task", "get_task", "list_project_tasks",
                "cancel_task", "retry_task", "cancel_run", "retry_run", "get_run",
                "list_project_runs", "list_events", "list_run_events", "list_managed_outputs",
                "get_managed_output", "list_generations",
                "get_generation", "list_variants", "attach_variant_thumbnail",
                "get_source_frame_thumbnail", "ensure_source_frame_thumbnail",
                "mark_variant_viewed", "mark_generation_variants_viewed",
                "create_generation", "create_variant",
                "list_capabilities", "register_capability", "claim_task", "register_executor",
                "settle_attempt", "fail_attempt", "publish_timeline_render",
                "record_remote_activation", "control_remote_credential", "get_executor",
                "get_local_worker_generation", "relinquish_local_worker", "start_local_worker",
            }
            if operation not in operations:
                raise ValueError(f"unknown generated workspace operation: {operation!r}")
            generated = getattr(self._generated, operation)
            if not callable(generated):
                raise AttributeError(f"generated workspace operation is not callable: {operation!r}")
            value = generated(*args, **kwargs)
        except Exception as exc:  # generated ApiError has stable fields
            raise WorkspaceClientError(
                int(getattr(exc, "status", 0)),
                str(getattr(exc, "code", "transport_error")),
                str(getattr(exc, "message", exc)),
                getattr(exc, "details", {}),
                request_id=str(getattr(exc, "request_id", "")),
            ) from exc
        def plain(item: Any) -> Any:
            if is_dataclass(item):
                return {key: plain(child) for key, child in asdict(item).items()}
            if isinstance(item, tuple):
                # Transport values are part of the JSON product surface;
                # never leave tuples in a DomainResult payload where the
                # canonical encoder would reject them.
                return [plain(child) for child in item]
            if isinstance(item, list):
                return [plain(child) for child in item]
            if isinstance(item, dict):
                return {key: plain(child) for key, child in item.items()}
            return item
        # The generated client carries committed mutation receipts as an
        # out-of-band ``MutationResult.receipt`` attribute on its resource
        # mapping. Preserve that attribute across the plain JSON conversion;
        # RemoteAstridClient._typed consumes the stable {data, receipt}
        # envelope and turns it into DomainResult.receipt.
        if hasattr(value, "receipt") and isinstance(value, dict):
            data = dict(value)
            # Some generated clients retain the wire receipt in the mapping
            # as well as on ``.receipt``.  The product envelope owns that
            # field; do not leak a duplicate receipt into DomainResult.data.
            data.pop("receipt", None)
            return {"data": plain(data), "receipt": plain(value.receipt)}
        return plain(value)

    # The following methods are intentionally explicit.  They form the
    # product adapter's typed vocabulary and keep generated operation names in
    # one explicit mapping.
    def health(self) -> Any:
        return self._call_generated("health")

    def handshake(self, client_name: str, client_version: str, requested_scopes: list[str]) -> Any:
        value = self._call_generated("handshake", client_name, client_version, requested_scopes)
        self._last_handshake = value
        return value

    def doctor(self) -> Any:
        return self._call_generated("doctor")

    def create_backup(self, destination: str) -> Any:
        return self._call_generated("create_backup", destination)

    def restore_backup(self, backup: str, destination: str) -> Any:
        return self._call_generated("restore_backup", backup, destination)

    def export_realm(self) -> Any:
        return self._call_generated("export_realm")

    def tombstone_realm(self, *, reason: str | None = None, expected_version: int | None = None) -> Any:
        return self._call_generated("tombstone_realm", reason=reason, expected_version=expected_version)

    def recover_realm(
        self,
        *,
        expected_realm_id: str,
        expected_version: int,
        confirmation: str | None = None,
        noninteractive: bool = False,
    ) -> Any:
        return self._call_generated(
            "recover_realm",
            expected_realm_id=expected_realm_id,
            expected_version=expected_version,
            confirmation=confirmation,
            noninteractive=noninteractive,
        )

    def purge_realm(self, confirmation: str) -> Any:
        return self._call_generated("purge_realm", confirmation)

    def create_project(
        self,
        name: str,
        *,
        idempotency_key: str,
        slug: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._call_generated(
            "create_project", name, idempotency_key=idempotency_key, slug=slug, metadata=metadata
        )

    def update_project(
        self,
        project_id: str,
        *,
        idempotency_key: str,
        expected_version: int | None = None,
        name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> Any:
        return self._call_generated(
            "update_project",
            project_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            name=name,
            metadata=metadata,
        )

    def list_projects(self, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_projects", cursor=cursor, limit=limit)

    def select_project(self, project: str, *, scope: str = "workspace", idempotency_key: str | None = None) -> Any:
        return self._call_generated("select_project", project, scope=scope, idempotency_key=idempotency_key)

    def current_project(self) -> Any:
        return self._call_generated("current_project")

    def get_project(self, project_id: str) -> Any:
        return self._call_generated("get_project", project_id)

    def get_project_shot_revision(self, project_id: str, shot_id: str, revision: str) -> Any:
        """Read one immutable shot revision for canonical render expansion."""
        return self._call_generated("get_project_shot_revision", project_id, shot_id, revision)

    def get_project_timeline_revision(self, project_id: str, timeline_id: str, revision: str) -> Any:
        """Read one immutable internal timeline revision for canonical render expansion."""
        return self._call_generated("get_project_timeline_revision", project_id, timeline_id, revision)

    def get_project_parent_composition_revision(self, project_id: str, timeline_id: str, revision: str) -> Any:
        """Read one immutable parent composition revision for canonical render expansion."""
        return self._call_generated(
            "get_project_parent_composition_revision", project_id, timeline_id, revision
        )

    def list_project_parent_composition_revisions(
        self,
        project_id: str,
        timeline_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> Any:
        """List immutable parent-composition revisions for one canonical timeline."""
        return self._call_generated(
            "list_project_parent_composition_revisions",
            project_id,
            timeline_id,
            cursor=cursor,
            limit=limit,
        )

    def commit_timeline(self, body: CommitRequest) -> CommitResult:
        """Submit a producer-owned command request through Runtime's commit route.

        The producer supplies the complete request, including its stable request
        and session identity. Preserve it for exact retry; Runtime authenticates
        the caller and the Astrid provider applies/rebases the commands.
        """
        return self._call_generated("commit_timeline", body)

    def publish_parent_composition(
        self,
        project_id: str,
        timeline_id: str,
        publication: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Any:
        """Publish one validated parent-composition closure through Runtime."""
        from astrid.core.timeline.compatibility import TimelineCompatibilityError, require_writable_candidate

        expected_head = publication.get("expected_head", publication.get("expected_head_revision_id"))
        base = self._require_writable_composition_revision(project_id, timeline_id, expected_head) if expected_head is not None else None
        try:
            require_writable_candidate({"base_parent_payload": base, "parent": publication.get("parent_composition")})
        except TimelineCompatibilityError as exc:
            raise WorkspaceClientError(409, "timeline_compatibility", str(exc)) from exc
        return self._call_generated(
            "publish_parent_composition",
            project_id,
            timeline_id,
            publication,
            idempotency_key=idempotency_key,
        )

    def _require_writable_composition_revision(self, project_id: str, timeline_id: str, revision: str) -> Mapping[str, Any]:
        from astrid.core.timeline.compatibility import TimelineCompatibilityError, require_writable_parent

        row = self.get_project_parent_composition_revision(project_id, timeline_id, revision)
        payload = row.get("payload") if isinstance(row, Mapping) else None
        if not isinstance(payload, Mapping) or row.get("revision_id") != revision:
            raise WorkspaceClientError(0, "protocol_error", "composition revision returned no matching identity/payload")
        try:
            require_writable_parent(payload, source=f"head/revision {revision}")
        except TimelineCompatibilityError as exc:
            raise WorkspaceClientError(409, "timeline_compatibility", str(exc)) from exc

        return payload

    def restore_project_parent_composition_revision(
        self, project_id: str, timeline_id: str, revision: str, *,
        expected_head: str | None, idempotency_key: str,
    ) -> Any:
        """Restore an immutable revision after checking both sides of the CAS.

        The expected head is read by exact revision ID, so a concurrent head
        change is still refused by Runtime's atomic compare-and-swap.
        """
        from astrid.core.timeline.compatibility import TimelineCompatibilityError, require_writable_candidate

        base = self._require_writable_composition_revision(project_id, timeline_id, expected_head) if expected_head is not None else None
        candidate = self._require_writable_composition_revision(project_id, timeline_id, revision)
        try:
            require_writable_candidate({"base_parent_payload": base, "parent": candidate})
        except TimelineCompatibilityError as exc:
            raise WorkspaceClientError(409, "timeline_compatibility", str(exc)) from exc
        return self._call_generated(
            "restore_project_parent_composition_revision", project_id, timeline_id, revision,
            expected_head=expected_head, idempotency_key=idempotency_key,
        )

    def replace_parent_composition_media(
        self,
        project_id: str,
        timeline_id: str,
        *,
        occurrence_id: str,
        clip_id: str,
        source_object_id: str,
        expected_head: str,
        idempotency_key: str,
        timing: str = "preserve-duration",
    ) -> Any:
        self._require_writable_composition_revision(project_id, timeline_id, expected_head)
        return self._call_generated(
            "replace_parent_composition_media",
            project_id,
            timeline_id,
            occurrence_id=occurrence_id,
            clip_id=clip_id,
            source_object_id=source_object_id,
            expected_head=expected_head,
            idempotency_key=idempotency_key,
            timing=timing,
        )

    def create_timeline(self, project_id: str, timeline_id: str, *, idempotency_key: str, duplicate_source: Mapping[str, Any] | None = None) -> Any:
        options = {"duplicate_source": duplicate_source} if duplicate_source is not None else {}
        return self._call_generated("create_timeline", project_id, timeline_id, idempotency_key=idempotency_key, **options)

    def create_timeline_document(self, project_id: str, timeline_id: str, *, config: Mapping[str, Any], registry: Mapping[str, Any], slug: str | None = None, name: str | None = None, idempotency_key: str) -> Any:
        return self._call_generated("create_timeline_document", project_id, timeline_id, config=config, registry=registry, slug=slug, name=name, idempotency_key=idempotency_key)

    def update_timeline_document(self, project_id: str, timeline_id: str, *, expected_version: int, config: Mapping[str, Any], registry: Mapping[str, Any], slug: str | None = None, name: str | None = None, idempotency_key: str) -> Any:
        return self._call_generated("update_timeline_document", project_id, timeline_id, expected_version=expected_version, config=config, registry=registry, slug=slug, name=name, idempotency_key=idempotency_key)

    def list_timelines(self, project_id: str, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_timelines", project_id, cursor=cursor, limit=limit)

    def get_timeline(self, timeline_id: str, *, project_id: str | None = None) -> Any:
        if hasattr(self._generated, "get_timeline"):
            return self._call_generated("get_timeline", timeline_id)
        if project_id is None:
            projects = paged_rows(self.list_projects, limit=50) or []
            for project in projects:
                candidate = str(project.get("project_id") or project.get("id") or "")
                timelines = paged_rows(self.list_timelines, candidate, limit=50) or []
                if any(str(row.get("timeline_id")) == timeline_id for row in timelines):
                    project_id = candidate
                    break
        if project_id is None:
            raise WorkspaceClientError(404, "not_found", "timeline not found", {"timeline_id": timeline_id})
        return self._call_generated("get_project_timeline", project_id, timeline_id)

    def inspect_timeline(
        self,
        project_id: str,
        timeline_id: str,
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        """Read one bounded, immutable timeline closure from Runtime.

        Inspection is a native Runtime operation.  It must not discover or
        start an executor/pack host, and the options mapping is passed through
        the generated contract unchanged.
        """
        return self._call_generated(
            "inspect_timeline", project_id, timeline_id, options=options or {}
        )

    def create_timeline_view(
        self,
        project_id: str,
        timeline_id: str,
        options: Mapping[str, Any] | None = None,
    ) -> Any:
        """Create a Runtime-owned declared-input timeline view."""
        return self._call_generated(
            "create_timeline_view", project_id, timeline_id, options=options or {}
        )

    def list_timeline_history(self, timeline_id: str, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_timeline_history", timeline_id, cursor=cursor, limit=limit)

    def replace_timeline_clip(
        self,
        timeline_id: str,
        *,
        clip_id: str,
        source_object_id: str,
        expected_version: int,
        idempotency_key: str,
        timing: str = "preserve-duration",
    ) -> Any:
        return self._call_generated(
            "replace_timeline_clip",
            timeline_id,
            clip_id=clip_id,
            source_object_id=source_object_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            timing=timing,
        )

    def diff_timeline(self, timeline_id: str, *, from_version: int, to_version: int) -> Any:
        return self._call_generated(
            "diff_timeline", timeline_id, from_version=from_version, to_version=to_version
        )

    def archive_timeline(self, timeline_id: str, *, expected_version: int, idempotency_key: str, withdraw_publication: Mapping[str, Any] | None = None) -> Any:
        options = {"withdraw_publication": withdraw_publication} if withdraw_publication is not None else {}
        return self._call_generated(
            "archive_timeline", timeline_id, expected_version=expected_version, idempotency_key=idempotency_key, **options
        )

    def plan_timeline_purge(self, project_id: str, targets: list[Mapping[str, Any]]) -> Any:
        return self._call_generated("plan_timeline_purge", project_id, targets)

    def execute_timeline_purge(self, project_id: str, targets: list[Mapping[str, Any]], *, plan_digest: str, idempotency_key: str) -> Any:
        return self._call_generated("execute_timeline_purge", project_id, targets, plan_digest=plan_digest, idempotency_key=idempotency_key)

    def recover_timeline(
        self,
        timeline_id: str,
        *,
        expected_version: int,
        version: int,
        idempotency_key: str,
    ) -> Any:
        return self._call_generated(
            "recover_timeline",
            timeline_id,
            expected_version=expected_version,
            version=version,
            idempotency_key=idempotency_key,
        )

    def list_project_shots(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        include_archived: bool = False,
    ) -> Any:
        return self._call_generated("list_project_shots", project_id, cursor=cursor, limit=limit, include_archived=include_archived)

    def create_project_shot(self, project_id: str, shot: Mapping[str, Any], *, idempotency_key: str) -> Any:
        return self._call_generated("create_project_shot", project_id, shot, idempotency_key=idempotency_key)

    def get_project_shot(self, project_id: str, shot_id: str) -> Any:
        return self._call_generated("get_project_shot", project_id, shot_id)

    def update_project_shot(self, project_id: str, shot_id: str, *, expected_version: int, name=None, metadata=None, idempotency_key: str | None = None) -> Any:
        return self._call_generated("update_project_shot", project_id, shot_id, expected_version=expected_version, name=name, metadata=metadata, idempotency_key=idempotency_key or "")

    def archive_project_shot(self, project_id: str, shot_id: str, *, expected_version: int, idempotency_key: str) -> Any:
        return self._call_generated("archive_project_shot", project_id, shot_id, expected_version=expected_version, idempotency_key=idempotency_key)

    def recover_project_shot(self, project_id: str, shot_id: str, *, expected_version: int, idempotency_key: str) -> Any:
        return self._call_generated("recover_project_shot", project_id, shot_id, expected_version=expected_version, idempotency_key=idempotency_key)

    def add_shot_item(self, project_id: str, shot_id: str, item: Mapping[str, Any], *, idempotency_key: str) -> Any:
        return self._call_generated("add_shot_item", project_id, shot_id, item, idempotency_key=idempotency_key)

    def promote_project_shot_candidate(
        self,
        project_id: str,
        shot_id: str,
        candidate_item_id: str,
        *,
        expected_head_seq: int,
        timeline_assets: list[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] = (),
        idempotency_key: str,
    ) -> Any:
        return self._call_generated(
            "promote_project_shot_candidate",
            project_id,
            shot_id,
            candidate_item_id,
            expected_head_seq=expected_head_seq,
            timeline_assets=timeline_assets,
            idempotency_key=idempotency_key,
        )

    def remove_shot_item(self, project_id: str, shot_id: str, item_id: str, *, expected_version: int, idempotency_key: str) -> Any:
        return self._call_generated("remove_shot_item", project_id, shot_id, item_id, expected_version=expected_version, idempotency_key=idempotency_key)

    def reorder_shot_items(self, project_id: str, shot_id: str, item_ids: list[str], *, expected_version: int, idempotency_key: str) -> Any:
        return self._call_generated("reorder_shot_items", project_id, shot_id, item_ids, expected_version=expected_version, idempotency_key=idempotency_key)

    def list_project_shot_text_bindings(self, project_id: str, *, shot_id: str | None = None, kind: str | None = None, slot: str | None = None) -> Any:
        return self._call_generated("list_project_shot_text_bindings", project_id, shot_id=shot_id, kind=kind, slot=slot)

    def set_project_shot_text_binding(self, project_id: str, body: Mapping[str, Any], *, idempotency_key: str) -> Any:
        return self._call_generated("set_project_shot_text_binding", project_id, body, idempotency_key=idempotency_key)

    def get_project_shot_text_binding(self, project_id: str, binding_id: str) -> Any:
        return self._call_generated("get_project_shot_text_binding", project_id, binding_id)

    def set_project_shot_text_binding_by_id(self, project_id: str, binding_id: str, body: Mapping[str, Any], *, idempotency_key: str) -> Any:
        return self._call_generated("set_project_shot_text_binding_by_id", project_id, binding_id, body, idempotency_key=idempotency_key)

    def rebind_project_shot_text_binding(self, project_id: str, binding_id: str, *, media_id: str, expected_head: int, idempotency_key: str) -> Any:
        return self._call_generated("rebind_project_shot_text_binding", project_id, binding_id, media_id=media_id, expected_head=expected_head, idempotency_key=idempotency_key)

    def list_project_references(
        self,
        project_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        include_archived: bool = False,
    ) -> Any:
        return self._call_generated("list_project_references", project_id, cursor=cursor, limit=limit, include_archived=include_archived)

    def create_project_reference(self, project_id: str, reference: Mapping[str, Any], *, idempotency_key: str) -> Any:
        return self._call_generated("create_project_reference", project_id, reference, idempotency_key=idempotency_key)

    def get_project_reference(self, project_id: str, reference_id: str) -> Any:
        return self._call_generated("get_project_reference", project_id, reference_id)

    def update_project_reference(self, project_id: str, reference_id: str, *, expected_version: int, name=None, description=None, metadata=None, idempotency_key: str | None = None) -> Any:
        return self._call_generated("update_project_reference", project_id, reference_id, expected_version=expected_version, name=name, description=description, metadata=metadata, idempotency_key=idempotency_key or "")

    def archive_project_reference(self, project_id: str, reference_id: str, *, expected_version: int, idempotency_key: str) -> Any:
        return self._call_generated("archive_project_reference", project_id, reference_id, expected_version=expected_version, idempotency_key=idempotency_key)

    def recover_project_reference(self, project_id: str, reference_id: str, *, expected_version: int, idempotency_key: str) -> Any:
        return self._call_generated("recover_project_reference", project_id, reference_id, expected_version=expected_version, idempotency_key=idempotency_key)

    def associate_reference(self, project_id: str, reference_id: str, association: Mapping[str, Any], *, idempotency_key: str) -> Any:
        return self._call_generated("associate_reference", project_id, reference_id, association, idempotency_key=idempotency_key)

    def set_primary_reference(self, project_id: str, reference_id: str, association_id: str, *, expected_version: int, idempotency_key: str) -> Any:
        return self._call_generated("set_primary_reference", project_id, reference_id, association_id, expected_version=expected_version, idempotency_key=idempotency_key)

    def link_references(self, project_id: str, link: Mapping[str, Any], *, idempotency_key: str) -> Any:
        return self._call_generated("link_references", project_id, link, idempotency_key=idempotency_key)

    def get_preferences(self, scope: str, *, project_id: str | None = None) -> Any:
        return self._call_generated("get_preferences", scope, project_id=project_id)

    def update_preferences(
        self,
        scope: str,
        content: str,
        *,
        expected_version: int,
        idempotency_key: str,
        project_id: str | None = None,
    ) -> Any:
        return self._call_generated(
            "update_preferences",
            scope,
            content,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            project_id=project_id,
        )

    def create_document(self, project_id: str, document_id: str, kind: str, content: Any, *, idempotency_key: str) -> Any:
        return self._call_generated("create_document", project_id, document_id, kind, content, idempotency_key=idempotency_key)

    def list_documents(self, project_id: str, *, cursor: str | None = None, limit: int = 50, kind: str | None = None) -> Any:
        return self._call_generated("list_documents", project_id, cursor=cursor, limit=limit, **({"kind": kind} if kind is not None else {}))

    def get_document(self, project_id: str, document_id: str) -> Any:
        return self._call_generated("get_document", project_id, document_id)

    def update_document(
        self,
        project_id: str,
        document_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        content: Any = _DOCUMENT_CONTENT_UNSET,
        kind: str | None = None,
    ) -> Any:
        return self._call_generated(
            "update_document",
            project_id,
            document_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            kind=kind,
            **({"content": content} if content is not _DOCUMENT_CONTENT_UNSET else {}),
        )

    def ingest_object(self, data: bytes, *, media_type: str, idempotency_key: str, filename: str | None = None, upload_binding: Mapping[str, Any] | None = None) -> Any:
        return self._call_generated("ingest_object", data, media_type=media_type, idempotency_key=idempotency_key, filename=filename, upload_binding=upload_binding)

    def ingest_project_object(
        self,
        project_id: str,
        data: bytes,
        *,
        media_type: str,
        idempotency_key: str,
        filename: str | None = None,
    ) -> Any:
        return self._call_generated(
            "ingest_project_object",
            project_id,
            data,
            media_type=media_type,
            idempotency_key=idempotency_key,
            filename=filename,
        )

    def import_project_media(
        self, project_id: str, data: bytes, *, media_type: str,
        idempotency_key: str, filename: str | None = None,
        expected_digest: str | None = None, width: int | None = None,
        height: int | None = None, duration_seconds: float | None = None,
    ) -> Any:
        """Import managed bytes and their truthful project catalog entry together."""
        return self._call_generated(
            "import_project_media", project_id, data, media_type=media_type,
            idempotency_key=idempotency_key, filename=filename,
            expected_digest=expected_digest, width=width, height=height,
            duration_seconds=duration_seconds,
        )

    def get_project_media_import(self, project_id: str, import_operation_id: str) -> Any:
        return self._call_generated("get_project_media_import", project_id, import_operation_id)

    def list_project_objects(self, project_id: str, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_project_objects", project_id, cursor=cursor, limit=limit)

    def get_project_object_location(self, project_id: str, object_id: str) -> Any:
        """Resolve a verified canonical local object path from the runtime."""
        return self._call_generated("get_project_object_location", project_id, object_id)

    def create_media_relation(
        self,
        project_id: str,
        from_object_id: str,
        to_object_id: str,
        kind: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        idempotency_key: str,
    ) -> Any:
        return self._call_generated(
            "create_media_relation",
            project_id,
            from_object_id,
            to_object_id,
            kind,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )

    def list_media_relations(self, project_id: str, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_media_relations", project_id, cursor=cursor, limit=limit)

    def get_object(self, object_id: str) -> Any:
        return self._call_generated("get_object", object_id)

    def get_source_frame_thumbnail(
        self,
        project_id: str,
        source_object_id: str,
        source_time_seconds: float,
        *,
        recipe_version: int = 1,
    ) -> Any:
        return self._call_generated(
            "get_source_frame_thumbnail",
            project_id,
            source_object_id,
            source_time_seconds,
            recipe_version=recipe_version,
        )

    def ensure_source_frame_thumbnail(
        self,
        project_id: str,
        thumbnail: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Any:
        return self._call_generated(
            "ensure_source_frame_thumbnail",
            project_id,
            thumbnail,
            idempotency_key=idempotency_key,
        )

    def head_object(self, object_id: str) -> Any:
        return self._call_generated("head_object", object_id)

    def prepare_task_admission(
        self,
        *,
        capability_id: str,
        capability_digest: str,
        input_object_ids: list[str],
        idempotency_key: str,
        schema_version: str = "1",
        settlement_effect: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        spec: Mapping[str, Any] | None = None,
        generation_intent: Mapping[str, Any] | None = None,
        storage_estimate: Mapping[str, int] | None = None,
        required_facts: Mapping[str, Any] | None = None,
        execution_request: Mapping[str, Any] | None = None,
        child_delegation: Mapping[str, Any] | None = None,
    ) -> Any:
        """Complete generated arguments with current fresh-admission preflight.

        Dispatch of these arguments is separate so exact replay does not
        rebuild placement, managed descriptors, or preflight evidence.
        """
        wire_spec = dict(spec or {})
        if execution_request is not None:
            from .execution_request import (
                merge_execution_input_manifest,
                merge_execution_request_inputs,
                normalize_execution_request,
                reject_caller_execution_binding,
                require_targeted_execution_binding_support,
            )

            reject_caller_execution_binding(execution_request, wire_spec)
            normalized = normalize_execution_request(execution_request)
            if normalized is None:
                raise ValueError("execution_request must not normalize to null")
            require_targeted_execution_binding_support(self)
            wire_spec = merge_execution_request_inputs(normalized, wire_spec)
            input_object_ids = merge_execution_input_manifest(
                normalized,
                input_object_ids,
            )
        else:
            from .execution_request import reject_caller_execution_binding

            reject_caller_execution_binding(None, wire_spec)
        if capability_id == "vibecomfy.run":
            from .remote import _vibecomfy_invocation_preflight

            strict = bool(
                isinstance(wire_spec.get("execution_request"), Mapping)
                and "workflow" in wire_spec["execution_request"]
            )
            receipt = _vibecomfy_invocation_preflight(
                self,
                wire_spec,
                strict=strict,
            )
            if receipt is not None:
                wire_spec["invocation_preflight"] = receipt
        admission = {
            "capability_id": capability_id,
            "capability_digest": capability_digest,
            "input_object_ids": input_object_ids,
            "idempotency_key": idempotency_key,
            "schema_version": schema_version,
            "settlement_effect": settlement_effect,
            "project_id": project_id,
            "spec": wire_spec if execution_request is not None else spec,
            "generation_intent": generation_intent,
            "storage_estimate": storage_estimate,
            "required_facts": required_facts,
            "child_delegation": child_delegation,
        }
        if execution_request is not None:
            admission["execution_request"] = normalized
        return admission

    def dispatch_task_admission(self, admission: Mapping[str, Any]) -> Any:
        """Send already completed generated-client arguments without rebuilding them."""
        return self._call_generated("admit_task", **dict(admission))

    def admit_task(
        self,
        *,
        capability_id: str,
        capability_digest: str,
        input_object_ids: list[str],
        idempotency_key: str,
        schema_version: str = "1",
        settlement_effect: Mapping[str, Any] | None = None,
        project_id: str | None = None,
        spec: Mapping[str, Any] | None = None,
        generation_intent: Mapping[str, Any] | None = None,
        storage_estimate: Mapping[str, int] | None = None,
        required_facts: Mapping[str, Any] | None = None,
        execution_request: Mapping[str, Any] | None = None,
        child_delegation: Mapping[str, Any] | None = None,
    ) -> Any:
        """Prepare and send one ordinary Runtime admission."""
        return self.dispatch_task_admission(self.prepare_task_admission(
            capability_id=capability_id, capability_digest=capability_digest,
            input_object_ids=input_object_ids, idempotency_key=idempotency_key,
            schema_version=schema_version, settlement_effect=settlement_effect,
            project_id=project_id, spec=spec, generation_intent=generation_intent,
            storage_estimate=storage_estimate, required_facts=required_facts,
            execution_request=execution_request, child_delegation=child_delegation,
        ))

    def get_task(self, task_id: str) -> Any:
        return self._call_generated("get_task", task_id)

    def get_executor(self, executor_id: str) -> Any:
        """Read the exact Runtime executor registration and readiness witness."""
        return self._call_generated("get_executor", executor_id)

    def get_local_worker_generation(self) -> Any:
        """Read the exact current local-worker generation, if one is active."""
        return self._call_generated("get_local_worker_generation")

    def relinquish_local_worker(
        self, executor_incarnation: str, evidence_digest: str
    ) -> Any:
        """Relinquish only the local generation identified by both witnesses."""
        return self._call_generated(
            "relinquish_local_worker", executor_incarnation, evidence_digest
        )

    def start_local_worker(self, profile_id: str, expected_workspace_uuid: str) -> Any:
        """Restart the selected Runtime-owned local worker after remote drain."""
        return self._call_generated(
            "start_local_worker", profile_id, expected_workspace_uuid
        )

    def record_remote_activation(
        self, task_id: str, qualification: Mapping[str, Any]
    ) -> Any:
        """Commit one exact task-bound remote activation through Runtime."""
        return self._call_generated("record_remote_activation", task_id, qualification)

    def control_remote_credential(
        self, task_id: str, control: Mapping[str, Any]
    ) -> Any:
        """Apply one exact Runtime credential-generation control operation."""
        return self._call_generated("control_remote_credential", task_id, control)

    def list_project_tasks(self, project_id: str, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_project_tasks", project_id, cursor=cursor, limit=limit)

    def list_managed_outputs(self, task_id: str) -> Any:
        """Read Runtime-owned managed output associations for one task."""
        return self._call_generated("list_managed_outputs", task_id)

    def get_managed_output(self, association_id: str) -> Any:
        """Read one Runtime-owned managed output association by id."""
        return self._call_generated("get_managed_output", association_id)

    def cancel_task(self, task_id: str, *, idempotency_key: str) -> Any:
        return self._call_generated("cancel_task", task_id, idempotency_key=idempotency_key)

    def retry_task(
        self,
        task_id: str,
        *,
        idempotency_key: str,
        expected_version: int | None = None,
    ) -> Any:
        return self._call_generated(
            "retry_task",
            task_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )

    def cancel_run(self, run_id: str, *, idempotency_key: str) -> Any:
        return self._call_generated("cancel_run", run_id, idempotency_key=idempotency_key)

    def retry_run(self, run_id: str, *, idempotency_key: str, selected_task_ids: list[str] | None = None) -> Any:
        return self._call_generated("retry_run", run_id, idempotency_key=idempotency_key, selected_task_ids=selected_task_ids)

    def get_run(self, run_id: str) -> Any:
        return self._call_generated("get_run", run_id)

    def list_project_runs(self, project_id: str, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_project_runs", project_id, cursor=cursor, limit=limit)

    def list_events(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        aggregate_id: str | None = None,
    ) -> Any:
        return self._call_generated(
            "list_events", cursor=cursor, limit=limit, aggregate_id=aggregate_id
        )

    def list_run_events(self, run_id: str, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_run_events", run_id, cursor=cursor, limit=limit)

    def list_generations(self, project_id: str, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_generations", project_id, cursor=cursor, limit=limit)

    def get_generation(self, generation_id: str) -> Any:
        return self._call_generated("get_generation", generation_id)

    def list_variants(self, generation_id: str, *, cursor: str | None = None, limit: int = 50) -> Any:
        return self._call_generated("list_variants", generation_id, cursor=cursor, limit=limit)

    def attach_variant_thumbnail(
        self,
        variant_id: str,
        *,
        thumbnail_object_id: str,
        source_object_id: str,
        recipe_version: int = 1,
        idempotency_key: str,
    ) -> Any:
        return self._call_generated(
            "attach_variant_thumbnail", variant_id,
            thumbnail_object_id=thumbnail_object_id,
            source_object_id=source_object_id,
            recipe_version=recipe_version,
            idempotency_key=idempotency_key,
        )

    def mark_variant_viewed(self, variant_id: str, *, idempotency_key: str) -> Any:
        return self._call_generated("mark_variant_viewed", variant_id, idempotency_key=idempotency_key)

    def mark_generation_variants_viewed(self, generation_id: str, *, idempotency_key: str) -> Any:
        return self._call_generated("mark_generation_variants_viewed", generation_id, idempotency_key=idempotency_key)

    def create_generation(self, project_id: str, generation_id: str, *, metadata: Mapping[str, Any] | None = None, type: str = "generation", source_task_id: str | None = None, idempotency_key: str) -> Any:
        return self._call_generated("create_generation", project_id, generation_id, metadata=metadata, type=type, source_task_id=source_task_id, idempotency_key=idempotency_key)

    def create_variant(self, generation_id: str, variant_id: str, *, object_id: str | None = None, variant_type: str = "original", metadata: Mapping[str, Any] | None = None, idempotency_key: str) -> Any:
        return self._call_generated("create_variant", generation_id, variant_id, object_id=object_id, variant_type=variant_type, metadata=metadata, idempotency_key=idempotency_key)

    def list_capabilities(self, *, cursor: str | None = None, limit: int = 50) -> Any:
        """Return one strict ``[items, next_cursor]`` capability page.

        The sibling runtime's generated client is cursor-bearing.  Preserve
        that boundary in the Astrid transport wrapper so callers can either
        request a page explicitly or use the shared ``paged_rows`` helper to
        exhaust the collection.  A bare list is never a valid response.
        """
        value = self._call_generated("list_capabilities", cursor=cursor, limit=limit)
        page = page_pair(value)
        if page is None:
            raise WorkspaceClientError(
                0,
                "protocol_error",
                "runtime capability listing returned an invalid page",
            )
        items, next_cursor = page
        return [[asdict(item) if is_dataclass(item) else item for item in items], next_cursor]

    def register_capability(self, capability_id: str, definition_digest: str, *, required_resource_keys: list[str] | None = None, status: str = "ready", estimated_scratch_bytes: int = 0, estimated_output_bytes: int = 0, unavailable_reason: str | None = None, idempotency_key: str | None = None) -> Any:
        return self._call_generated("register_capability", capability_id, definition_digest, required_resource_keys=required_resource_keys, status=status, estimated_scratch_bytes=estimated_scratch_bytes, estimated_output_bytes=estimated_output_bytes, unavailable_reason=unavailable_reason, idempotency_key=idempotency_key)

    def claim_task(
        self,
        *,
        executor_id: str,
        capability_ids: list[str],
        idempotency_key: str,
        runtime_epoch: int | None = None,
        target: Mapping[str, Any] | None = None,
    ) -> Any:
        if runtime_epoch is None:
            health = self.health()
            runtime_epoch = int(health.get("runtime_epoch", 0)) if isinstance(health, Mapping) else 0
        return self._call_generated(
            "claim_task",
            executor_id=executor_id,
            capability_ids=capability_ids,
            idempotency_key=idempotency_key,
            runtime_epoch=runtime_epoch,
            target=target,
        )

    def register_executor(self, executor: Mapping[str, Any], *, idempotency_key: str) -> Any:
        return self._call_generated("register_executor", executor, idempotency_key=idempotency_key)

    def settle_attempt(self, attempt_id: str, settlement: Mapping[str, Any], *, idempotency_key: str) -> Any:
        return self._call_generated("settle_attempt", attempt_id, settlement, idempotency_key=idempotency_key)

    def fail_attempt(
        self,
        attempt_id: str,
        *,
        lease_id: str,
        fence: int,
        error: Mapping[str, Any],
        runtime_epoch: int,
        idempotency_key: str,
    ) -> Any:
        """Explicitly fail a leased attempt through the generated Runtime API."""
        return self._call_generated(
            "fail_attempt",
            attempt_id,
            lease_id=lease_id,
            fence=fence,
            error=error,
            runtime_epoch=runtime_epoch,
            idempotency_key=idempotency_key,
        )

    def publish_timeline_render(
        self,
        attempt_id: str,
        publication: Mapping[str, Any] | None = None,
        *,
        lease_id: str | None = None,
        fence: int | None = None,
        runtime_epoch: int | None = None,
        timeline_id: str | None = None,
        expected_version: int | None = None,
        config: Mapping[str, Any] | None = None,
        registry: Mapping[str, Any] | None = None,
        render: Mapping[str, Any] | None = None,
        slug: str | None = None,
        name: str | None = None,
        idempotency_key: str,
    ) -> Any:
        """Publish a timeline revision and its renderer task atomically.

        The generated client intentionally exposes the wire fields as keyword
        arguments. Keep this adapter as the one place that translates the
        proposal mapping into that typed call; passing the mapping positionally
        would fail before reaching the runtime.
        """
        if publication is not None:
            if not isinstance(publication, Mapping):
                raise ValueError("timeline publication must be an object")
            # Accept the product-facing mapping form as well as the explicit
            # keyword form used by RemoteTasks._typed.
            values = {key: publication.get(key) for key in (
                "lease_id", "fence", "runtime_epoch", "timeline_id",
                "expected_version", "config", "registry", "render", "slug", "name",
            ) if key in publication}
            lease_id = values.get("lease_id", lease_id)
            fence = values.get("fence", fence)
            runtime_epoch = values.get("runtime_epoch", runtime_epoch)
            timeline_id = values.get("timeline_id", timeline_id)
            expected_version = values.get("expected_version", expected_version)
            config = values.get("config", config)
            registry = values.get("registry", registry)
            render = values.get("render", render)
            slug = values.get("slug", slug)
            name = values.get("name", name)
        required = (
            "lease_id", "fence", "runtime_epoch", "timeline_id",
            "expected_version", "config", "registry", "render",
        )
        supplied = {
            "lease_id": lease_id, "fence": fence, "runtime_epoch": runtime_epoch,
            "timeline_id": timeline_id, "expected_version": expected_version,
            "config": config, "registry": registry, "render": render,
        }
        missing = [field for field in required if supplied[field] is None]
        if missing:
            raise ValueError(f"timeline publication is missing required fields: {', '.join(missing)}")
        return self._call_generated(
            "publish_timeline_render", attempt_id,
            lease_id=lease_id, fence=fence, runtime_epoch=runtime_epoch,
            timeline_id=timeline_id, expected_version=expected_version,
            config=config, registry=registry, render=render,
            idempotency_key=idempotency_key,
            **{key: value for key, value in (("slug", slug), ("name", name)) if value is not None},
        )
