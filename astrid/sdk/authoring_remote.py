"""Target-bound remote authoring seam.

This module deliberately composes the existing authoring-bundle compiler and
the Runtime's immutable-revision/publication ports.  It is not a second
timeline format or a generic ``save`` operation.  The target descriptor is a
trusted launch input: callers cannot choose a different endpoint, project,
timeline, or parent head after the session is opened.

The generic pack worker currently has only task/CAS scopes and does not receive
this pinned closure, so it cannot safely use ``publish`` today.  A worker with
an explicitly provisioned project-scoped credential may use this seam; the
normal pack-host credential must remain read/settle-only until Runtime adds a
task-bound authoring capability.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from astrid.core.timeline.authoring_bundle import (
    AuthoringBundleError,
    compile_authoring_candidate,
    diff_authoring_candidate,
    open_authoring_bundle,
    preview_authoring_candidate,
    publish_authoring_candidate,
    validate_authoring_candidate,
)

from .workspace_client import WorkspaceClientError, validate_runtime_endpoint
from .pagination import paged_rows

__all__ = ["AuthoringRemoteError", "TargetBoundAuthoringBundle"]


class AuthoringRemoteError(ValueError):
    """A target, closure, ownership, or Runtime publication violation."""


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(child) for child in value]
    return value


def _data(value: Any, operation: str) -> Mapping[str, Any]:
    value = _plain(value)
    if isinstance(value, Mapping) and isinstance(value.get("data"), Mapping):
        value = value["data"]
    if not isinstance(value, Mapping):
        raise AuthoringRemoteError(f"Runtime {operation} response is not an object")
    return value


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuthoringRemoteError(f"authoring target requires a non-empty {field}")
    return value.strip()


def _transport_idempotency_key(value: str) -> str:
    """Normalize a stable caller key to Runtime's transport spelling."""
    key = value.strip()
    if not key:
        raise AuthoringRemoteError("idempotency_key must be a non-empty string")
    if key.startswith("sha256:"):
        key = "candidate-" + key.removeprefix("sha256:")
    key = re.sub(r"[^A-Za-z0-9._~-]", "-", key)
    if not key[0].isalnum():
        key = f"candidate-{key}"
    try:
        key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AuthoringRemoteError("idempotency_key must contain ASCII characters") from exc
    return key[:256]


@dataclass(frozen=True)
class _Target:
    endpoint: str
    project_id: str
    timeline_id: str
    base_head: str


class TargetBoundAuthoringBundle:
    """Open and publish one exact parent-composition closure.

    ``target`` must be the coordinator-issued, read-only target descriptor
    supplied to a worker.  Candidate dictionaries remain ordinary JSON-shaped
    authoring objects and can be edited with the existing SDK helpers.
    """

    def __init__(self, client: Any, target: Mapping[str, Any]):
        # Keep the seam usable by the generated WorkspaceClient and by narrow
        # remote doubles.  The latter must expose the same typed methods; no
        # arbitrary ``save``/HTTP object is accepted or used here.
        connected_endpoint = getattr(client, "endpoint", None)
        connected_endpoint = getattr(connected_endpoint, "url", connected_endpoint)
        if not isinstance(connected_endpoint, str):
            raise AuthoringRemoteError("target-bound authoring requires a connected Runtime client")
        if not isinstance(target, Mapping):
            raise AuthoringRemoteError("target-bound authoring requires a target descriptor")
        endpoint = _required_text(target.get("endpoint"), "endpoint")
        try:
            endpoint = validate_runtime_endpoint(endpoint)
        except WorkspaceClientError as exc:
            raise AuthoringRemoteError(f"target endpoint is invalid: {exc}") from exc
        connected = validate_runtime_endpoint(connected_endpoint)
        if endpoint != connected:
            raise AuthoringRemoteError("authoring target endpoint is not the connected Runtime")
        if target.get("read_only") is True:
            raise AuthoringRemoteError("authoring target is read-only")
        capabilities = target.get("capabilities")
        edit = capabilities.get("edit") if isinstance(capabilities, Mapping) else None
        if not isinstance(edit, Mapping) or edit.get("status") != "available":
            raise AuthoringRemoteError("authoring target does not advertise an available edit capability")
        if edit.get("route") != "authoring-bundle validate/commit":
            raise AuthoringRemoteError("authoring target advertises an unsupported edit route")
        self._client = client
        self._target = target
        self._identity = _Target(
            endpoint=endpoint,
            project_id=_required_text(target.get("project_id"), "project_id"),
            timeline_id=_required_text(target.get("timeline_id"), "timeline_id"),
            base_head=_required_text(target.get("head_revision_id"), "head_revision_id"),
        )

    @property
    def target(self) -> Mapping[str, Any]:
        return dict(self._target)

    @property
    def identity(self) -> Mapping[str, str]:
        return {
            "endpoint": self._identity.endpoint,
            "project_id": self._identity.project_id,
            "timeline_id": self._identity.timeline_id,
            "base_head": self._identity.base_head,
        }

    def open(self) -> dict[str, Any]:
        """Read the exact pinned parent/shot/internal closure once."""
        identity = self._identity
        parent = _data(
            self._client.get_project_parent_composition_revision(
                identity.project_id, identity.timeline_id, identity.base_head
            ),
            "get_project_parent_composition_revision",
        )
        if (
            str(parent.get("project_id") or identity.project_id) != identity.project_id
            or str(parent.get("timeline_id") or identity.timeline_id) != identity.timeline_id
            or str(parent.get("revision_id") or parent.get("parent_revision_id") or "") != identity.base_head
        ):
            raise AuthoringRemoteError("Runtime returned a parent revision for the wrong target or head")
        payload = parent.get("payload")
        occurrences = payload.get("occurrences") if isinstance(payload, Mapping) else None
        if not isinstance(occurrences, list):
            raise AuthoringRemoteError("pinned parent closure has no occurrence list")
        shots: dict[str, Mapping[str, Any]] = {}
        internals: dict[str, Mapping[str, Any]] = {}
        for occurrence in occurrences:
            if not isinstance(occurrence, Mapping):
                raise AuthoringRemoteError("pinned parent occurrence is not an object")
            shot_id = _required_text(occurrence.get("shot_id"), "occurrence.shot_id")
            shot_revision_id = _required_text(
                occurrence.get("shot_revision_id") or occurrence.get("revision_id"),
                "occurrence.shot_revision_id",
            )
            if shot_revision_id not in shots:
                shot = _data(
                    self._client.get_project_shot_revision(
                        identity.project_id, shot_id, shot_revision_id
                    ),
                    "get_project_shot_revision",
                )
                if str(shot.get("project_id") or identity.project_id) != identity.project_id:
                    raise AuthoringRemoteError("pinned shot revision belongs to another project")
                if str(shot.get("shot_id") or shot_id) != shot_id or str(shot.get("revision_id") or "") != shot_revision_id:
                    raise AuthoringRemoteError("Runtime returned a shot revision for the wrong pin")
                shots[shot_revision_id] = shot
            shot = shots[shot_revision_id]
            shot_payload = shot.get("payload")
            internal_id = shot.get("internal_timeline_revision_id")
            if not isinstance(internal_id, str) and isinstance(shot_payload, Mapping):
                internal_id = shot_payload.get("internal_timeline_revision_id")
            internal_id = _required_text(internal_id, "shot.internal_timeline_revision_id")
            if internal_id not in internals:
                internal = _data(
                    self._client.get_project_timeline_revision(
                        identity.project_id, identity.timeline_id, internal_id
                    ),
                    "get_project_timeline_revision",
                )
                if str(internal.get("project_id") or identity.project_id) != identity.project_id:
                    raise AuthoringRemoteError("pinned internal revision belongs to another project")
                if str(internal.get("timeline_id") or identity.timeline_id) != identity.timeline_id or str(internal.get("revision_id") or "") != internal_id:
                    raise AuthoringRemoteError("Runtime returned an internal revision for the wrong pin")
                internals[internal_id] = internal
        try:
            return open_authoring_bundle(
                parent,
                shot_revisions=list(shots.values()),
                internal_timeline_revisions=list(internals.values()),
            )
        except AuthoringBundleError as exc:
            raise AuthoringRemoteError(f"pinned authoring closure is invalid: {exc}") from exc

    @staticmethod
    def compile(candidate: Mapping[str, Any]):
        return compile_authoring_candidate(candidate)

    @staticmethod
    def validate(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return validate_authoring_candidate(candidate)

    @staticmethod
    def diff(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return diff_authoring_candidate(candidate)

    @staticmethod
    def preview(candidate: Mapping[str, Any]) -> dict[str, Any]:
        return preview_authoring_candidate(candidate)

    def publish(self, candidate: Mapping[str, Any], *, idempotency_key: str) -> dict[str, Any]:
        """Validate target/head/media ownership, then perform one CAS publish."""
        identity = self._identity
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise AuthoringRemoteError("idempotency_key must be a non-empty string")
        # Runtime's transport contract requires an ASCII key whose first
        # character is alphanumeric. Candidate digests conventionally use
        # the ``sha256:`` prefix, so normalize that common caller spelling.
        transport_idempotency_key = _transport_idempotency_key(idempotency_key)
        if not isinstance(candidate, Mapping):
            raise AuthoringRemoteError("authoring candidate must be an object")
        if candidate.get("project_id") != identity.project_id or candidate.get("timeline_id") != identity.timeline_id:
            raise AuthoringRemoteError("authoring candidate targets a different project or timeline")
        base_parent = candidate.get("base_parent")
        if not isinstance(base_parent, Mapping) or base_parent.get("revision_id") != identity.base_head:
            raise AuthoringRemoteError("authoring candidate base head is not the trusted target head")
        rows = paged_rows(self._client.list_timelines, identity.project_id, limit=50) or []
        current = next(
            (
                row for row in rows
                if isinstance(row, Mapping)
                and str(row.get("timeline_id") or row.get("id")) == identity.timeline_id
            ),
            None,
        )
        if not isinstance(current, Mapping):
            raise AuthoringRemoteError("authoring target timeline is not in the canonical project listing")
        inspection = _data(
            self._client.inspect_timeline(
                identity.project_id,
                identity.timeline_id,
                options={"limit": 1, "detail": False},
            ),
            "inspect_timeline",
        )
        if inspection.get("is_current_head") is not True:
            raise AuthoringRemoteError("authoring target inspection is not the canonical current head")
        # Let Runtime evaluate the immutable expected_head after its durable
        # accepted-request replay check.  Comparing this observation with the
        # pinned base head here would reject a valid identical retry; the
        # compiled publication still carries the original expected_head.
        try:
            compilation = compile_authoring_candidate(candidate)
        except AuthoringBundleError as exc:
            raise AuthoringRemoteError(f"authoring candidate validation failed: {exc}") from exc
        manifest = compilation.publication.get("dependency_manifest")
        media = manifest.get("media") if isinstance(manifest, Mapping) else None
        if not isinstance(media, list):
            raise AuthoringRemoteError("compiled candidate dependency manifest has no media list")
        for index, dependency in enumerate(media):
            if not isinstance(dependency, Mapping):
                raise AuthoringRemoteError(f"compiled media dependency {index} is not an object")
            media_id = dependency.get("media_id") or dependency.get("content_digest")
            if not isinstance(media_id, str) or not media_id:
                raise AuthoringRemoteError(f"compiled media dependency {index} has no media identity")
            location = _data(
                self._client.get_project_object_location(
                    identity.project_id, media_id.removeprefix("sha256:")
                ),
                "get_project_object_location",
            )
            if location.get("verified") is not True:
                raise AuthoringRemoteError(f"candidate media is not destination-owned: {media_id}")
            observed = location.get("digest") or location.get("object_id")
            if isinstance(observed, str) and observed.removeprefix("sha256:") != media_id.removeprefix("sha256:"):
                raise AuthoringRemoteError(f"destination-owned media digest differs for {media_id}")

        class _Writer:
            def __init__(self, owner: WorkspaceClient):
                self.owner = owner
                self.result: Any = None

            def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
                self.result = self.owner.publish_parent_composition(
                    project_id, timeline_id, publication, idempotency_key=idempotency_key
                )
                return _data(self.result, "publish_parent_composition")

        writer = _Writer(self._client)
        try:
            result = publish_authoring_candidate(
                candidate, writer, idempotency_key=transport_idempotency_key
            )
        except (AuthoringBundleError, WorkspaceClientError) as exc:
            raise AuthoringRemoteError(f"authoring candidate publication failed: {exc}") from exc
        return {
            "route": "authoring-bundle validate/commit",
            "project_id": identity.project_id,
            "timeline_id": identity.timeline_id,
            "expected_head": identity.base_head,
            "candidate_digest": result.get("candidate_digest"),
            "identity_mapping": result.get("identity_mapping"),
            "publication": result.get("publication"),
            "update": result.get("update"),
            "summary": result.get("summary"),
            "next_actions": result.get("next_actions"),
        }
