"""Astrid's product-neutral adapter over the generated workspace client."""

from __future__ import annotations

import mimetypes
import hashlib
import json
import re
import tempfile
import uuid
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping

from astrid.core.receipts.contract import CommandReceipt

from .contracts import DomainResult, ErrorObject
from .capability_selection import select_capability_row
from .pagination import page_pair, paged_rows
from .workspace_client import WorkspaceClient, WorkspaceClientError

from .execution_request import (
    ExecutionRequest,
    ExecutionRequestError,
    merge_execution_input_manifest,
    merge_execution_request_inputs,
    normalize_execution_request,
    reject_caller_execution_binding,
    require_targeted_execution_binding_support,
)


def _vibecomfy_invocation_preflight(
    client: WorkspaceClient,
    spec: Mapping[str, Any],
    *,
    strict: bool = False,
) -> dict[str, Any] | None:
    """Validate a complete canonical VibeComfy task before admission.

    Older non-canonical VibeComfy tasks (for example import/edit tasks) do not
    enter this path. Once a task declares any canonical sibling member, the
    full bundle and its task-bound source asset are required and validated.
    """
    raw_inputs = spec.get("inputs")
    if not isinstance(raw_inputs, Mapping):
        if strict:
            raise ValueError("canonical VibeComfy task requires an input manifest")
        return None
    inputs = dict(raw_inputs)
    canonical_names = ("python", "companion", "source")
    if not any(name in inputs for name in canonical_names):
        if strict:
            raise ValueError("canonical VibeComfy task is missing bundle members")
        return None
    missing = [name for name in canonical_names if name not in inputs]
    if missing:
        raise ValueError(
            "canonical VibeComfy task is missing bundle members: "
            + ", ".join(missing)
        )

    def descriptor(name: str) -> tuple[str, str, str]:
        value = inputs[name]
        if not isinstance(value, Mapping):
            raise ValueError(f"VibeComfy input {name!r} must be a digest descriptor")
        # ``object_id`` is the canonical immutable CAS identity; ``digest``
        # is an optional repeated witness in the request stencil.  Admission
        # must accept the canonical object-id-only spelling and derive the
        # witness before reading or compiling the bundle.
        digest = str(value.get("digest") or value.get("object_id") or "")
        object_id = str(value.get("object_id") or digest)
        normalized = digest.removeprefix("sha256:")
        if (
            not digest.startswith("sha256:")
            or len(normalized) != 64
            or any(char not in "0123456789abcdef" for char in normalized)
            or object_id != digest
        ):
            raise ValueError(
                f"VibeComfy input {name!r} must have matching sha256 digest/object_id"
            )
        filename = value.get("filename")
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise ValueError(f"VibeComfy input {name!r} requires a safe filename")
        return digest, filename, normalized

    descriptors = {name: descriptor(name) for name in canonical_names}
    source_descriptor = None
    if "source_video" in inputs:
        source_descriptor = descriptor("source_video")
    managed_descriptor = None
    if "managed_assets" in inputs:
        managed_descriptor = descriptor("managed_assets")

    def read_object(digest: str, name: str) -> bytes:
        response = client.get_object(digest)
        if isinstance(response, (bytes, bytearray)):
            data = bytes(response)
        else:
            data = (
                response.get("data")
                if isinstance(response, Mapping)
                else getattr(response, "data", None)
            )
        if not isinstance(data, bytes):
            raise ValueError(f"VibeComfy preflight could not read managed object {name!r}")
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest.removeprefix("sha256:"):
            raise ValueError(f"VibeComfy preflight hash mismatch for {name!r}")
        return data

    with tempfile.TemporaryDirectory(prefix="astrid-vibecomfy-preflight-") as raw_root:
        root = Path(raw_root)
        paths = {
            "python": root / "workflow.py",
            "companion": root / "workflow.vibe.json",
            "source": root / "source.json",
        }
        for name, path in paths.items():
            path.write_bytes(read_object(descriptors[name][0], name))
        source_video_path = None
        run_inputs: dict[str, Any] = {}
        managed_manifest: dict[str, Any] | None = None
        managed_members: dict[str, bytes] = {}
        if managed_descriptor is not None:
            from astrid.packs.vibecomfy.asset_manifest import (
                AssetManifestError,
                read_archive_bytes,
                resolve_inputs,
            )

            try:
                resolved = read_archive_bytes(
                    read_object(managed_descriptor[0], "managed_assets")
                )
            except AssetManifestError as exc:
                raise ValueError(str(exc)) from exc
            managed_manifest = resolved.manifest
            managed_members = resolved.members
            supplied_workflow_inputs: dict[str, Any] = {}
            raw_workflow_inputs = inputs.get("workflow_inputs")
            if isinstance(raw_workflow_inputs, str):
                try:
                    decoded_workflow_inputs = json.loads(raw_workflow_inputs or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError("workflow_inputs must be valid JSON") from exc
                if not isinstance(decoded_workflow_inputs, Mapping):
                    raise ValueError("workflow_inputs must be a JSON object")
                supplied_workflow_inputs = dict(decoded_workflow_inputs)
            elif isinstance(raw_workflow_inputs, Mapping):
                supplied_workflow_inputs = dict(raw_workflow_inputs)
            try:
                run_inputs = resolve_inputs(managed_manifest, supplied_workflow_inputs)
            except AssetManifestError as exc:
                raise ValueError(str(exc)) from exc
        managed_source_digest = next(
            (
                "sha256:" + str(record["sha256"])
                for record in (managed_manifest or {}).get("assets", [])
                if isinstance(record, Mapping) and record.get("binding") == "source_video"
            ),
            None,
        )
        if source_descriptor is not None:
            source_video_path = root / source_descriptor[1]
            source_video_path.write_bytes(read_object(source_descriptor[0], "source_video"))
            if managed_source_digest is not None and source_descriptor[0] != managed_source_digest:
                raise ValueError("managed assets conflict with workflow inputs: source_video")
            if "source_video" not in run_inputs:
                run_inputs["source_video"] = source_descriptor[1]
        elif "source_video" in run_inputs:
            source_member = str(run_inputs["source_video"])
            managed_member = next(
                (name for name in managed_members if Path(name).name == source_member),
                None,
            )
            if managed_member is None:
                raise ValueError("managed assets source_video binding is missing its member")
            source_video_path = root / source_member
            source_video_path.write_bytes(managed_members[managed_member])
        from astrid.packs.vibecomfy.invocation_preflight import preflight_invocation

        receipt = preflight_invocation(
            paths["python"],
            run_inputs=run_inputs,
            expected_prompt=(
                str(inputs["prompt"])
                if isinstance(inputs.get("prompt"), str) and inputs["prompt"].strip()
                else (
                    str(run_inputs["prompt"])
                    if isinstance(run_inputs.get("prompt"), str) and run_inputs["prompt"].strip()
                    else None
                )
            ),
            source_video_path=source_video_path,
            expected_source_digest=(
                source_descriptor[0]
                if source_descriptor is not None
                else managed_source_digest
            ),
            phase="submission",
        )
        receipt["members"] = {
            name: {"digest": digest, "filename": filename}
            for name, (digest, filename, _normalized) in descriptors.items()
        }
        if source_descriptor is not None:
            receipt["members"]["source_video"] = {
                "digest": source_descriptor[0],
                "filename": source_descriptor[1],
            }
        if managed_descriptor is not None:
            receipt["members"]["managed_assets"] = {
                "digest": managed_descriptor[0],
                "filename": managed_descriptor[1],
            }
        receipt["input_manifest"] = sorted(
            descriptor[0] for descriptor in descriptors.values()
        ) + ([source_descriptor[0]] if source_descriptor is not None else []) + (
            [managed_descriptor[0]] if managed_descriptor is not None else []
        )
        receipt["resolved_workflow_inputs"] = run_inputs
        if managed_manifest is not None:
            receipt["managed_asset_manifest"] = managed_manifest
        return receipt


def _full_mapping(value: Any) -> dict[str, Any] | None:
    """Copy a generated resource without narrowing its explicit fields."""
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return None


def _source_task_id(value: Any) -> str | None:
    resource = _full_mapping(value)
    if resource is None:
        return None
    direct = resource.get("source_task_id")
    if isinstance(direct, str) and direct:
        return direct
    metadata = resource.get("metadata")
    if isinstance(metadata, Mapping):
        nested = metadata.get("source_task_id")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _prepare_execution_admission(
    client: WorkspaceClient,
    *,
    execution_request: ExecutionRequest | Mapping[str, Any] | None,
    spec: Mapping[str, Any],
    supplied_input_object_ids: list[str] | tuple[str, ...] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[str]]:
    """Run the common request/binding/manifest fence for every task route."""

    reject_caller_execution_binding(execution_request, spec)
    normalized_request = normalize_execution_request(execution_request)
    submitted_spec = dict(spec)
    if normalized_request is not None:
        require_targeted_execution_binding_support(client)
        # A targeted request may intentionally omit ``inputs`` when a higher
        # level orchestrator assembles managed file ports.  Those ports are
        # already immutable Runtime descriptors in ``spec.inputs``; promote
        # them into the frozen execution request before admission so the
        # request and input_object_ids cannot diverge at claim time.
        if not normalized_request.get("inputs"):
            raw_inputs = spec.get("inputs")
            candidates: list[dict[str, Any]] = []
            if isinstance(raw_inputs, Mapping):
                for name, value in raw_inputs.items():
                    if not isinstance(value, Mapping):
                        continue
                    object_id = value.get("object_id") or value.get("digest")
                    filename = value.get("filename")
                    if not isinstance(object_id, str) or not object_id.strip():
                        continue
                    if not isinstance(filename, str) or not filename.strip():
                        continue
                    descriptor: dict[str, Any] = {
                        "name": str(name),
                        "object_id": object_id,
                        "filename": filename,
                        "required": bool(value.get("required", True)),
                    }
                    digest = value.get("digest")
                    if digest is not None:
                        descriptor["digest"] = digest
                    candidates.append(descriptor)
            if candidates:
                # ``_kernel_invoke`` supplies the manifest in the canonical
                # file-port order. Preserve it when possible because the
                # Runtime treats the ordered manifest as part of admission
                # identity; otherwise retain the spec's deterministic order.
                if supplied_input_object_ids is not None:
                    by_object_id = {
                        item["object_id"].removeprefix("sha256:"): item
                        for item in candidates
                    }
                    ordered: list[dict[str, Any]] = []
                    for object_id in supplied_input_object_ids:
                        candidate = by_object_id.get(str(object_id).removeprefix("sha256:"))
                        if candidate is not None:
                            ordered.append(candidate)
                    if len(ordered) == len(candidates):
                        candidates = ordered
                normalized_request = normalize_execution_request(
                    {**normalized_request, "inputs": candidates}
                )
        submitted_spec = merge_execution_request_inputs(normalized_request, spec)
    admitted_manifest = merge_execution_input_manifest(
        normalized_request,
        supplied_input_object_ids,
    )
    return normalized_request, submitted_spec, admitted_manifest


class _RemoteFamily:
    def __init__(self, client: WorkspaceClient):
        self._client = client

    def _typed(self, operation: str, *args: Any, key: str | None = None, **kwargs: Any) -> DomainResult[Any]:
        reads = {"get_project_timeline", "get_project", "list_projects", "current_project", "list_timelines", "list_timeline_history", "list_project_parent_composition_revisions", "diff_timeline", "inspect_timeline", "create_timeline_view", "get_shot", "list_project_shots", "get_reference", "list_project_references", "get_object", "head_object", "list_project_objects", "list_media_relations", "get_task", "list_project_tasks", "list_managed_outputs", "get_managed_output", "get_run", "list_project_runs", "list_events", "list_run_events", "list_generations", "get_generation", "list_variants", "get_document", "list_documents", "get_preferences", "list_project_shot_text_bindings", "get_project_shot_text_binding", "get_project_shot_revision", "get_project_timeline_revision", "get_project_parent_composition_revision", "get_source_frame_thumbnail"}
        if key is None and operation not in reads:
            key = uuid.uuid4().hex
        try:
            # Keep the operation vocabulary auditable.  In particular, never
            # let a caller turn an arbitrary string into an attribute lookup
            # on the generated client.
            if operation == "get_preferences": value = self._client.get_preferences(*args, **kwargs)
            elif operation == "update_preferences": value = self._client.update_preferences(*args, **kwargs)
            elif operation == "create_document": value = self._client.create_document(*args, **kwargs)
            elif operation == "get_document": value = self._client.get_document(*args, **kwargs)
            elif operation == "update_document": value = self._client.update_document(*args, **kwargs)
            elif operation == "list_documents": value = self._client.list_documents(*args, **kwargs)
            elif operation == "add_shot_item": value = self._client.add_shot_item(*args, **kwargs)
            elif operation == "admit_task": value = self._client.admit_task(*args, **kwargs)
            elif operation == "dispatch_task_admission":
                dispatch = getattr(self._client, "dispatch_task_admission", None)
                value = dispatch(args[0]) if callable(dispatch) else self._client.admit_task(**args[0])
            elif operation == "attach_variant_thumbnail": value = self._client.attach_variant_thumbnail(*args, **kwargs)
            elif operation == "archive_project_reference": value = self._client.archive_project_reference(*args, **kwargs)
            elif operation == "archive_project_shot": value = self._client.archive_project_shot(*args, **kwargs)
            elif operation == "archive_timeline": value = self._client.archive_timeline(*args, **kwargs)
            elif operation == "associate_reference": value = self._client.associate_reference(*args, **kwargs)
            elif operation == "cancel_run": value = self._client.cancel_run(*args, **kwargs)
            elif operation == "cancel_task": value = self._client.cancel_task(*args, **kwargs)
            elif operation == "claim_task": value = self._client.claim_task(*args, **kwargs)
            elif operation == "create_generation": value = self._client.create_generation(*args, **kwargs)
            elif operation == "create_media_relation": value = self._client.create_media_relation(*args, **kwargs)
            elif operation == "create_project": value = self._client.create_project(*args, **kwargs)
            elif operation == "create_timeline": value = self._client.create_timeline(*args, **kwargs)
            elif operation == "create_project_reference": value = self._client.create_project_reference(*args, **kwargs)
            elif operation == "create_project_shot": value = self._client.create_project_shot(*args, **kwargs)
            elif operation == "create_variant": value = self._client.create_variant(*args, **kwargs)
            elif operation == "current_project": value = self._client.current_project(*args, **kwargs)
            elif operation == "diff_timeline": value = self._client.diff_timeline(*args, **kwargs)
            elif operation == "get_generation": value = self._client.get_generation(*args, **kwargs)
            elif operation == "get_object": value = self._client.get_object(*args, **kwargs)
            elif operation == "get_project": value = self._client.get_project(*args, **kwargs)
            elif operation == "get_project_timeline":
                value = self._client.get_timeline(args[1], project_id=args[0])
            elif operation == "get_project_reference": value = self._client.get_project_reference(*args, **kwargs)
            elif operation == "get_project_shot": value = self._client.get_project_shot(*args, **kwargs)
            elif operation == "get_project_shot_revision": value = self._client.get_project_shot_revision(*args, **kwargs)
            elif operation == "get_project_timeline_revision": value = self._client.get_project_timeline_revision(*args, **kwargs)
            elif operation == "get_project_parent_composition_revision": value = self._client.get_project_parent_composition_revision(*args, **kwargs)
            elif operation == "list_project_parent_composition_revisions": value = self._client.list_project_parent_composition_revisions(*args, **kwargs)
            elif operation == "restore_project_parent_composition_revision": value = self._client.restore_project_parent_composition_revision(*args, **kwargs)
            elif operation == "get_source_frame_thumbnail":
                value = self._client.get_source_frame_thumbnail(*args, **kwargs)
            elif operation == "get_project_shot_text_binding": value = self._client.get_project_shot_text_binding(*args, **kwargs)
            elif operation == "get_managed_output": value = self._client.get_managed_output(*args, **kwargs)
            elif operation == "get_run": value = self._client.get_run(*args, **kwargs)
            elif operation == "get_task": value = self._client.get_task(*args, **kwargs)
            elif operation == "inspect_timeline": value = self._client.inspect_timeline(*args, **kwargs)
            elif operation == "create_timeline_view": value = self._client.create_timeline_view(*args, **kwargs)
            elif operation == "fail_attempt": value = self._client.fail_attempt(*args, **kwargs)
            elif operation == "head_object": value = self._client.head_object(*args, **kwargs)
            elif operation == "ensure_source_frame_thumbnail":
                value = self._client.ensure_source_frame_thumbnail(*args, **kwargs)
            elif operation == "ingest_project_object": value = self._client.ingest_project_object(*args, **kwargs)
            elif operation == "link_references": value = self._client.link_references(*args, **kwargs)
            elif operation == "list_events": value = self._client.list_events(*args, **kwargs)
            elif operation == "list_generations": value = self._client.list_generations(*args, **kwargs)
            elif operation == "list_media_relations": value = self._client.list_media_relations(*args, **kwargs)
            elif operation == "list_project_objects": value = self._client.list_project_objects(*args, **kwargs)
            elif operation == "list_project_references": value = self._client.list_project_references(*args, **kwargs)
            elif operation == "list_project_runs": value = self._client.list_project_runs(*args, **kwargs)
            elif operation == "list_project_shots": value = self._client.list_project_shots(*args, **kwargs)
            elif operation == "list_project_shot_text_bindings": value = self._client.list_project_shot_text_bindings(*args, **kwargs)
            elif operation == "list_project_tasks": value = self._client.list_project_tasks(*args, **kwargs)
            elif operation == "list_managed_outputs": value = self._client.list_managed_outputs(*args, **kwargs)
            elif operation == "list_projects": value = self._client.list_projects(*args, **kwargs)
            elif operation == "list_run_events": value = self._client.list_run_events(*args, **kwargs)
            elif operation == "list_timeline_history": value = self._client.list_timeline_history(*args, **kwargs)
            elif operation == "list_timelines": value = self._client.list_timelines(*args, **kwargs)
            elif operation == "list_variants": value = self._client.list_variants(*args, **kwargs)
            elif operation == "mark_generation_variants_viewed": value = self._client.mark_generation_variants_viewed(*args, **kwargs)
            elif operation == "mark_variant_viewed": value = self._client.mark_variant_viewed(*args, **kwargs)
            elif operation == "publish_timeline_render": value = self._client.publish_timeline_render(*args, **kwargs)
            elif operation == "publish_parent_composition": value = self._client.publish_parent_composition(*args, **kwargs)
            elif operation == "promote_project_shot_candidate": value = self._client.promote_project_shot_candidate(*args, **kwargs)
            elif operation == "recover_project_reference": value = self._client.recover_project_reference(*args, **kwargs)
            elif operation == "recover_project_shot": value = self._client.recover_project_shot(*args, **kwargs)
            elif operation == "recover_timeline": value = self._client.recover_timeline(*args, **kwargs)
            elif operation == "register_capability": value = self._client.register_capability(*args, **kwargs)
            elif operation == "register_executor": value = self._client.register_executor(*args, **kwargs)
            elif operation == "remove_shot_item": value = self._client.remove_shot_item(*args, **kwargs)
            elif operation == "reorder_shot_items": value = self._client.reorder_shot_items(*args, **kwargs)
            elif operation == "set_project_shot_text_binding": value = self._client.set_project_shot_text_binding(*args, **kwargs)
            elif operation == "set_project_shot_text_binding_by_id": value = self._client.set_project_shot_text_binding_by_id(*args, **kwargs)
            elif operation == "rebind_project_shot_text_binding": value = self._client.rebind_project_shot_text_binding(*args, **kwargs)
            elif operation == "retry_run": value = self._client.retry_run(*args, **kwargs)
            elif operation == "retry_task": value = self._client.retry_task(*args, **kwargs)
            elif operation == "select_project": value = self._client.select_project(*args, **kwargs)
            elif operation == "set_primary_reference": value = self._client.set_primary_reference(*args, **kwargs)
            elif operation == "settle_attempt": value = self._client.settle_attempt(*args, **kwargs)
            elif operation == "update_project": value = self._client.update_project(*args, **kwargs)
            elif operation == "update_project_reference": value = self._client.update_project_reference(*args, **kwargs)
            elif operation == "update_project_shot": value = self._client.update_project_shot(*args, **kwargs)
            else: raise ValueError(f"unsupported generated operation: {operation}")
            receipt = None
            if isinstance(value, dict) and set(value) >= {"data", "receipt"}:
                receipt = CommandReceipt.from_dict(value["receipt"]) if value["receipt"] is not None else None
                value = value["data"]
            return DomainResult.success(value, receipt=receipt, idempotency_key=key or "")
        except WorkspaceClientError as exc:
            return DomainResult.failure(ErrorObject(code=exc.code, message=exc.message, details=exc.details), idempotency_key=key or "")


class RemoteProjects(_RemoteFamily):
    def create(self, *, slug: str, name: str, metadata: Mapping[str, Any] | None = None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("create_project", name, key=key, idempotency_key=key, slug=slug, metadata=metadata)
    def list(self, *, cursor=None, limit=50):
        return self._typed("list_projects", cursor=cursor, limit=limit)
    def show(self, ref): return self._typed("get_project", ref)
    def update(self, ref, *, name=None, metadata=None, expected_version=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("update_project", ref, key=key, idempotency_key=key, name=name, metadata=metadata, expected_version=expected_version)
    def select(self, ref, *, scope="workspace", idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("select_project", key=key, project=ref, scope=scope, idempotency_key=key)
    def current(self):
        return self._typed("current_project")


class RemoteTimelines(_RemoteFamily):
    def __init__(self, client, *, invoker=None):
        super().__init__(client)
        self._invoker = invoker

    @staticmethod
    def _retired_document_route(operation: str, *, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return DomainResult.failure(
            ErrorObject(
                "retired_route",
                f"timeline document route {operation!r} is retired; use canonical current-head inspection",
                {
                    "status": 410,
                    "operation": operation,
                    "replacement": "timelines.inspect/open_composition",
                },
            ),
            idempotency_key=key,
        )

    def create(self, *, project, config: Mapping[str, Any], registry: Mapping[str, Any], slug=None, name=None, timeline_id=None, idempotency_key=None):
        return self._retired_document_route("create", idempotency_key=idempotency_key)

    def create_empty(
        self,
        *,
        project,
        timeline_id=None,
        config: Mapping[str, Any] | None = None,
        idempotency_key=None,
    ):
        """Create a timeline that already has an empty current head.

        Runtime ``create_timeline`` makes the identity shell only; until a
        parent composition is published the timeline has no head, so checkout,
        show, visualize and render all refuse it.  The first publication (with
        ``expected_head`` null, no shots, and the given parent config) gives it
        that head.  Both writes derive from one caller key, so a retry replays
        the committed shell and head instead of creating a second timeline.
        """
        from astrid.core.ids import generate_ulid

        key = idempotency_key or uuid.uuid4().hex
        resolved = self._resolve_project_ref(project)
        if not resolved.ok:
            return resolved
        project_ref = str(resolved.data)
        timeline_key = str(timeline_id).strip() if timeline_id not in (None, "") else generate_ulid()
        shell = self._typed(
            "create_timeline", project_ref, timeline_key,
            key=f"{key}-shell", idempotency_key=f"{key}-shell",
        )
        if not shell.ok:
            return shell
        shell_row = shell.data if isinstance(shell.data, Mapping) else {}
        project_id = shell_row.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            return DomainResult.failure(
                ErrorObject("protocol_error", "runtime timeline create returned no project identity", {"timeline_id": timeline_key}),
                idempotency_key=key,
            )
        publication = {
            "project_id": project_id,
            "timeline_id": timeline_key,
            "expected_head": None,
            "parent_composition": {
                "config": dict(config) if config is not None else {"tracks": []},
                "registry": {},
                "clips": [],
                "occurrences": [],
            },
            "shot_revisions": [],
            "internal_timeline_revisions": [],
        }
        head = self._typed(
            "publish_parent_composition", project_id, timeline_key, publication,
            key=f"{key}-head", idempotency_key=f"{key}-head",
        )
        if not head.ok:
            return head
        published = head.data if isinstance(head.data, Mapping) else {}
        return DomainResult.success(
            {
                "project_id": project_id,
                "timeline_id": timeline_key,
                "head_revision_id": published.get("new_head") or published.get("revision_id"),
                "created": True,
            },
            receipt=head.receipt,
            idempotency_key=key,
        )

    def _resolve_project_ref(self, project) -> DomainResult[str]:
        """Resolve an explicit project or the runtime's selected project.

        Timeline reads share this one scope rule: an explicit project always
        wins; otherwise the workspace runtime's durable current selection is
        used.  No local preference or timeline-name inference is allowed.
        """
        if project not in (None, "") and str(project).strip():
            return DomainResult.success(str(project).strip())
        selected = self._typed("current_project")
        if not selected.ok:
            return selected
        data = selected.data
        row = data.get("project") if isinstance(data, Mapping) else None
        if not isinstance(row, Mapping):
            return DomainResult(
                ok=False,
                data=None,
                error=ErrorObject(
                    "not_found",
                    "no current project is selected",
                    {"next_action": "astrid projects select <project>"},
                ),
                receipt=None,
                idempotency_key="",
            )
        ref = row.get("project_id") or row.get("id") or row.get("slug")
        if not isinstance(ref, str) or not ref.strip():
            return DomainResult(
                ok=False,
                data=None,
                error=ErrorObject(
                    "protocol_error",
                    "runtime current project returned no project identity",
                    {},
                ),
                receipt=None,
                idempotency_key="",
            )
        return DomainResult.success(
            ref.strip(),
            receipt=selected.receipt,
            idempotency_key=selected.idempotency_key,
        )

    def list(self, project, *, cursor=None, limit=50, include_archived=False):
        resolved_project = self._resolve_project_ref(project)
        if not resolved_project.ok:
            return resolved_project
        result = self._typed(
            "list_timelines", resolved_project.data, cursor=cursor, limit=limit
        )
        if not result.ok or include_archived or not isinstance(result.data, (list, tuple)):
            return result
        # Older/runtime deployments return archived rows from this endpoint
        # despite the product route's active-only default. Filter only rows
        # carrying the authoritative archived flag and retain page shape and
        # cursor so callers can continue pagination normally.
        page = result.data
        if len(page) != 2 or not isinstance(page[0], list):
            return result
        active = [
            row for row in page[0]
            if not isinstance(row, Mapping) or not bool(row.get("archived", False))
        ]
        return DomainResult.success(
            [active, page[1]],
            receipt=result.receipt,
            idempotency_key=result.idempotency_key,
        )

    def _resolve_timeline(self, project, ref):
        """Resolve a product reference to one Runtime timeline identity.

        Current reads must resolve through the Runtime listing and native
        inspection routes.  In particular, do not use ``get_timeline`` here:
        that endpoint returns the legacy document projection (including
        mutable config/registry bytes) and is not a current-head authority.
        """
        resolved_project = self._resolve_project_ref(project)
        if not resolved_project.ok:
            return resolved_project
        project_ref = str(resolved_project.data)
        resolved_ref = ref
        if resolved_ref in (None, ""):
            shown = self._typed("get_project", project_ref)
            if not shown.ok:
                return shown
            project_row = shown.data
            metadata = project_row.get("metadata") if isinstance(project_row, Mapping) else None
            default = metadata.get("default_timeline_id") if isinstance(metadata, Mapping) else None
            if default in (None, ""):
                return DomainResult.failure(
                    ErrorObject(
                        "not_found",
                        "project has no configured default canonical timeline",
                        {
                            "project": project_ref,
                            "next_action": (
                                "set metadata.default_timeline_id with "
                                "astrid projects update <project> --settings "
                                "'{\"default_timeline_id\": \"<timeline-id>\"}'"
                            ),
                        },
                    )
                )
            resolved_ref = str(default)
        rows = paged_rows(self._client.list_timelines, project_ref, limit=50)
        if rows is None:
            return DomainResult.failure(
                ErrorObject("not_found", "timeline not found", {"project": project_ref, "ref": str(resolved_ref)})
            )
        match = next(
            (
                item for item in rows
                if isinstance(item, Mapping)
                and str(resolved_ref) in {str(item.get("timeline_id", "")), str(item.get("slug", ""))}
            ),
            None,
        )
        if match is None:
            return DomainResult.failure(
                ErrorObject("not_found", "timeline not found", {"project": project_ref, "ref": str(resolved_ref)})
            )
        timeline_id = match.get("timeline_id") or match.get("id")
        if not isinstance(timeline_id, str) or not timeline_id:
            return DomainResult.failure(
                ErrorObject(
                    "protocol_error",
                    "Runtime timeline listing omitted its canonical timeline id",
                    {"project": project_ref, "ref": str(resolved_ref)},
                )
            )
        return DomainResult.success(
            {
                **dict(match),
                "timeline_id": timeline_id,
                "project_ref": project_ref,
                "timeline_ref": str(resolved_ref),
            }
        )

    def resolve_scope(self, project=None, ref=None):
        """Resolve the canonical project/timeline pair without reading a document."""
        return self._resolve_timeline(project, ref)

    def head_canonical(self, project_id, timeline_id):
        """One compact Runtime read for an already resolved canonical scope."""
        result = self._typed("get_project_timeline", project_id, timeline_id)
        if not result.ok:
            return result
        data = result.data
        if (not isinstance(data, Mapping) or data.get("project_id") != project_id
                or data.get("timeline_id") != timeline_id or "head_revision_id" not in data):
            return DomainResult.failure(ErrorObject("protocol_error", "compact timeline head has wrong scope or no revision identity", {}))
        head = {}
        for public_field, state_field in (
            ("head_revision_id", "revision_id"),
            ("head_publication_id", "publication_id"),
            ("head_updated_at", "updated_at"),
        ):
            value = data.get(public_field)
            if value is not None and not isinstance(value, str):
                return DomainResult.failure(ErrorObject(
                    "protocol_error",
                    f"compact timeline head field {public_field!r} must be a string or null",
                    {"field": public_field},
                ))
            head[state_field] = value
        # These fields describe the same current-head row. The exact accepted
        # receipt remains the only authority for what this client published.
        return DomainResult.success(head)

    def _parent_composition_scope(self, project, ref):
        """Resolve the canonical Runtime IDs required by the composition revision routes."""
        resolved = self._resolve_timeline(project, ref)
        if not resolved.ok or not isinstance(resolved.data, Mapping):
            return resolved
        shown = self._typed("get_project", resolved.data["project_ref"])
        if not shown.ok:
            return shown
        if not isinstance(shown.data, Mapping):
            return DomainResult.failure(ErrorObject("protocol_error", "project response is not an object", {}))
        project_id = shown.data.get("project_id") or shown.data.get("id")
        if not isinstance(project_id, str) or not project_id:
            return DomainResult.failure(ErrorObject("protocol_error", "project response has no canonical identity", {}))
        listed_project_id = resolved.data.get("project_id")
        if listed_project_id not in (None, "", project_id):
            return DomainResult.failure(ErrorObject("integrity_error", "timeline listing belongs to a different project", {}))
        return DomainResult.success(
            {"project_id": project_id, "timeline_id": str(resolved.data["timeline_id"])}
        )

    def list_parent_composition_revisions(self, project, ref, *, cursor=None, limit=50):
        """List immutable composition revisions for a product project/timeline reference."""
        scope = self._parent_composition_scope(project, ref)
        if not scope.ok:
            return scope
        project_id = scope.data["project_id"]
        timeline_id = scope.data["timeline_id"]
        return self._typed(
            "list_project_parent_composition_revisions",
            project_id,
            timeline_id,
            cursor=cursor,
            limit=limit,
        )

    def restore_parent_composition_revision(
        self,
        project,
        ref,
        revision,
        *,
        expected_head: str | None,
        idempotency_key=None,
    ):
        """Restore an immutable revision as a new head with Runtime's CAS and replay contract."""
        key = idempotency_key or uuid.uuid4().hex
        scope = self._parent_composition_scope(project, ref)
        if not scope.ok:
            return DomainResult.failure(
                scope.error or ErrorObject("not_found", "timeline not found", {}),
                idempotency_key=key,
            )
        project_id = scope.data["project_id"]
        timeline_id = scope.data["timeline_id"]
        return self._typed(
            "restore_project_parent_composition_revision",
            project_id,
            timeline_id,
            revision,
            key=key,
            expected_head=expected_head,
            idempotency_key=key,
        )

    def show(self, project, ref):
        """Canonical current-head alias retained for SDK callers."""
        return self.open_composition(project, ref)

    @staticmethod
    def _native_options(
        *,
        revision_id=None,
        limit=50,
        occurrence=None,
        shot=None,
        clip=None,
        track=None,
        asset=None,
        range_value=None,
        detail=False,
        neighbors=0,
        cursor=None,
        formats=None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "limit": int(limit),
            "detail": bool(detail),
            "neighbors": int(neighbors),
        }
        if revision_id:
            options["revision_id"] = str(revision_id)
        for name, value in (("occurrence", occurrence), ("shot", shot), ("clip", clip), ("track", track), ("asset", asset)):
            if value not in (None, "", []):
                options[name] = value
        if range_value not in (None, "", []):
            options["range"] = range_value
        if cursor not in (None, ""):
            options["cursor"] = cursor
        if formats is not None:
            options["formats"] = list(formats)
        return options

    def inspect(
        self,
        project,
        ref,
        *,
        revision_id=None,
        limit=50,
        occurrence=None,
        shot=None,
        clip=None,
        track=None,
        asset=None,
        range_value=None,
        detail=False,
        neighbors=0,
        cursor=None,
        _resolved_timeline=None,
    ):
        """Inspect an exact Runtime timeline closure through the native route."""
        if not callable(getattr(self._client, "inspect_timeline", None)):
            return DomainResult.failure(
                ErrorObject(
                    "unavailable",
                    "Runtime does not expose canonical timeline inspection",
                    {"project": str(project), "timeline": str(ref)},
                )
            )
        timeline = (
            _resolved_timeline
            if isinstance(_resolved_timeline, DomainResult)
            else self._resolve_timeline(project, ref)
        )
        if not timeline.ok or not isinstance(timeline.data, Mapping):
            return timeline
        timeline_id = timeline.data["timeline_id"]
        project_ref = timeline.data.get("project_ref") or project
        pinned_revision = revision_id or timeline.data.get("head_revision_id") or timeline.data.get("parent_revision_id")
        options = self._native_options(
            revision_id=pinned_revision,
            limit=limit,
            occurrence=occurrence,
            shot=shot,
            clip=clip,
            track=track,
            asset=asset,
            range_value=range_value,
            detail=detail,
            neighbors=neighbors,
            cursor=cursor,
        )
        return self._typed("inspect_timeline", str(project_ref), timeline_id, options=options)

    def visualize(
        self,
        project,
        ref,
        *,
        mode="auto",
        options: Mapping[str, Any] | None = None,
        revision_id=None,
        limit=50,
        occurrence=None,
        shot=None,
        clip=None,
        track=None,
        asset=None,
        range_value=None,
        detail=False,
        neighbors=0,
        formats=("md", "png"),
        out=None,
    ):
        """Render-free input view or composed frames from a saved revision.

        ``inputs`` never reads render history. ``auto`` and ``composed`` both
        capture requested frames from the exact pinned canonical composition.
        The default composed request includes synchronized input lanes so the
        source placements remain apparent beside the output; callers can use
        ``show``/``hide`` to choose a narrower surface.
        An explicit run may select a saved revision only after exact-run
        admission; its rendered video is never used as the frame source.
        """
        selected_mode = str(mode or "auto").strip().lower()
        if selected_mode not in {"auto", "inputs", "composed"}:
            return DomainResult.failure(
                ErrorObject("validation_error", "mode must be auto, inputs, or composed", {"field": "mode"})
            )
        if out not in (None, ""):
            return DomainResult.failure(
                ErrorObject("validation_error", "native timeline views do not accept an output path", {"field": "out"})
            )
        view_options = dict(options or {})
        requested_view = str(view_options.get("view") or "filmstrip").strip().lower()
        if requested_view == "overview":
            requested_view = "contact"
            view_options["view"] = requested_view
        if requested_view not in {"filmstrip", "contact", "motion", "diff"}:
            return DomainResult.failure(
                ErrorObject(
                    "validation_error",
                    "view must be filmstrip, contact, motion, or diff",
                    {"field": "view", "value": requested_view},
                )
            )
        if requested_view == "diff":
            missing = [
                name for name in ("before_revision_id", "after_revision_id")
                if not isinstance(view_options.get(name), str) or not view_options[name].strip()
            ]
            if not isinstance(project, str) or not project.strip():
                return DomainResult.failure(
                    ErrorObject("validation_error", "view=diff requires an explicit project", {"field": "project"})
                )
            if not isinstance(ref, str) or not ref.strip():
                return DomainResult.failure(
                    ErrorObject("validation_error", "view=diff requires an explicit timeline", {"field": "timeline"})
                )
            if missing:
                return DomainResult.failure(
                    ErrorObject(
                        "validation_error",
                        "view=diff requires both explicit before_revision_id and after_revision_id",
                        {"fields": missing, "view": "diff"},
                    )
                )
            if view_options.get("revision_id") not in (None, ""):
                return DomainResult.failure(
                    ErrorObject(
                        "validation_error",
                        "view=diff uses only explicit before/after revision selectors",
                        {"field": "revision_id", "view": "diff"},
                    )
                )
            if selected_mode == "inputs":
                return DomainResult.failure(
                    ErrorObject(
                        "validation_error",
                        "render-free input mode supports only view=filmstrip; diff requires admitted immutable bundles",
                        {"field": "mode", "view": "diff"},
                    )
                )
        timeline = self._resolve_timeline(project, ref)
        if not timeline.ok or not isinstance(timeline.data, Mapping):
            return timeline
        timeline_id = timeline.data["timeline_id"]
        project_ref = timeline.data.get("project_ref") or project
        timeline_ref = timeline.data.get("timeline_ref") or ref
        if selected_mode == "inputs" and requested_view not in {"filmstrip"}:
            return DomainResult.failure(
                ErrorObject(
                    "validation_error",
                    "render-free input mode supports only view=filmstrip; composed views require output authority",
                    {"field": "view", "mode": selected_mode},
                )
            )
        requested_revision = revision_id or view_options.get("revision_id")
        pinned_revision = requested_revision or timeline.data.get("head_revision_id") or timeline.data.get("parent_revision_id")
        requested_run = view_options.get("render_run")
        explicit_run = requested_run not in (None, "", "latest")
        project_id = str(timeline.data.get("project_id") or project_ref)
        if requested_view == "diff":
            if self._invoker is None:
                return DomainResult.failure(
                    ErrorObject(
                        "render_required",
                        "revision diff requires the canonical invocation route",
                        {"project_id": project_id, "timeline_id": timeline_id, "view": "diff"},
                    )
                )
            executor_inputs: dict[str, Any] = {
                "project_slug": str(project_ref),
                "timeline_slug": str(timeline_ref),
                "view": "diff",
                "formats": list(formats or ("md", "png")),
            }
            executor_inputs.update(view_options)
            # Keep the invocation caller-bound even if a duplicate alias was
            # included in the options mapping.  revision_diff itself remains
            # host-owned and is intentionally left for native preflight.
            executor_inputs["project_slug"] = str(project_ref)
            executor_inputs["timeline_slug"] = str(timeline_ref)
            executor_inputs["view"] = "diff"
            return self._invoker(
                "rendering.timeline_visualize",
                kind="executor",
                project=project_ref,
                inputs=executor_inputs,
                out=None,
                wait=True,
            )
        if selected_mode != "inputs":
            if self._invoker is None:
                # A native transport may expose the cheap declared-input
                # projection, but output visualization requires the canonical
                # invocation route to materialize the saved composition and
                # capture frames from it.
                return DomainResult.failure(
                    ErrorObject(
                        "render_required",
                        "composed frame capture requires the canonical invocation route",
                        {
                            "project_id": project_id,
                            "timeline_id": timeline_id,
                            "input_only_available": True,
                            "next_actions": [
                                {"label": "Inspect declared inputs", "command": f"astrid timelines visualize --project {project_ref} {timeline_ref} --mode inputs"},
                            ],
                        },
                    )
                )
            executor_inputs: dict[str, Any] = {
                "timeline_slug": timeline_ref,
                "view": requested_view,
                "formats": list(formats or ("md", "png")),
                "composed_capture": True,
            }
            executor_inputs.update(view_options)
            if explicit_run:
                executor_inputs["render_run"] = str(requested_run)
            if requested_revision not in (None, ""):
                executor_inputs["revision_id"] = str(requested_revision)
            elif explicit_run:
                # The exact run chooses its own pinned saved revision in
                # composed-capture admission; do not accidentally substitute
                # today's head.
                executor_inputs.pop("revision_id", None)
            else:
                executor_inputs["revision_id"] = pinned_revision
            executor_inputs["composed_capture"] = True
            executor_inputs.setdefault("timeline_slug", timeline_ref)
            if not explicit_run:
                # The matching lookup selected this run from the current head;
                # keep that provenance distinct from a user-pinned historical
                # or candidate render when the executor builds its receipt.
                executor_inputs["auto_selected_render"] = True
            executor_inputs.setdefault("show", ["output", "inputs", "text", "audio"])
            return self._invoker(
                "rendering.timeline_visualize",
                kind="executor",
                project=project_ref,
                inputs=executor_inputs,
                out=None,
                wait=True,
            )

        if view_options:
            formats = view_options.get("formats", formats)
            occurrence = view_options.get("occurrence", occurrence)
            shot = view_options.get("shot", shot)
            clip = view_options.get("clip", clip)
            track = view_options.get("track", track)
            asset = view_options.get("asset", asset)
            range_value = view_options.get("range", range_value)
            detail = view_options.get("detail", detail)
            neighbors = view_options.get("neighbors", neighbors)
        options = self._native_options(
            revision_id=pinned_revision,
            limit=limit,
            occurrence=occurrence,
            shot=shot,
            clip=clip,
            track=track,
            asset=asset,
            range_value=range_value,
            detail=detail,
            neighbors=neighbors,
            cursor=view_options.get("cursor"),
            formats=formats,
        )
        return self._typed("create_timeline_view", str(project_ref), timeline_id, options=options)

    @staticmethod
    def _native_projection(data: Mapping[str, Any], *, project, ref) -> dict[str, Any]:
        """Adapt native inspection rows to the established show shape.

        This is result-shape adaptation only: closure expansion, selection,
        pagination identity, and snapshot authority stay in Runtime.
        """
        selected = data.get("selected") if isinstance(data.get("selected"), list) else []
        selected_parent = data.get("selected_parent_clips") if isinstance(data.get("selected_parent_clips"), list) else []
        clips: list[dict[str, Any]] = []
        targets: list[dict[str, Any]] = []
        for item in selected_parent:
            if not isinstance(item, Mapping):
                continue
            clip = dict(item)
            clip.setdefault("track", clip.get("track_id"))
            clip.setdefault("target_kind", "parent_clip")
            clips.append(clip)
            targets.append({
                "kind": "parent_clip",
                "target_kind": "parent_clip",
                "timeline_id": data.get("timeline_id"),
                "clip_id": item.get("clip_id"),
                "track_id": item.get("track_id"),
                "element_ref": item.get("element_ref"),
                "addressable": True,
            })
        for row in selected:
            if not isinstance(row, Mapping):
                continue
            occurrence = row.get("occurrence") if isinstance(row.get("occurrence"), Mapping) else {}
            oid = occurrence.get("occurrence_id")
            target = row.get("role") == "target"
            if target:
                targets.append({
                    "kind": "occurrence",
                    "timeline_id": data.get("timeline_id"),
                    "occurrence_id": oid,
                    "shot_id": occurrence.get("shot_id"),
                    "addressable": True,
                })
            for item in row.get("clips", []) if isinstance(row.get("clips"), list) else []:
                if not isinstance(item, Mapping):
                    continue
                clip = dict(item)
                clip.setdefault("occurrence_id", oid)
                clip.setdefault("shot_id", occurrence.get("shot_id"))
                clip.setdefault("track", clip.get("track_id"))
                clips.append(clip)
                if target:
                    # Keep the occurrence target as the stable parent address
                    # and add one exact child target per clip. Mutating the
                    # last target collapsed multi-child occurrences onto the
                    # final child and made visualization navigation ambiguous.
                    targets.append({
                        "kind": "clip",
                        "target_kind": "clip",
                        "timeline_id": data.get("timeline_id"),
                        "occurrence_id": oid,
                        "shot_id": occurrence.get("shot_id"),
                        "clip_id": item.get("clip_id"),
                        "track_id": item.get("track_id"),
                        "element_ref": item.get("element_ref"),
                        "addressable": True,
                    })
        from astrid.packs.rendering.actions.timeline_visualize.readable_timing import project_readable_timing
        clips = project_readable_timing(clips, data.get("render_timing_context"))
        selectors = data.get("selectors") if isinstance(data.get("selectors"), Mapping) else {}
        projection = {
            "kind": "timeline-inspection",
            "summary": {
                "authority": "canonical_head",
                "representation": data.get("representation", "canonical_head"),
                "is_current_head": data.get("is_current_head"),
                "revision_id": data.get("revision_id"),
                "head_revision_id": data.get("head_revision_id"),
                "snapshot_digest": data.get("snapshot_digest"),
                "head_content_digest": data.get("head_content_digest"),
                "evidence_kind": data.get("evidence_kind", "declared_inputs"),
            },
            "query": dict(selectors),
            "targets": targets,
            "clips": clips,
            "media": [],
            "outputs": [],
            "diagnostics": {"selection_status": data.get("selection_status"), "target_count": data.get("target_count", 0)},
            "pagination": {"next_cursor": data.get("next_cursor"), "limit": selectors.get("limit")},
            "native_inspection": dict(data),
            "scope": {"authority": "canonical_head", "project": str(project), "timeline": str(ref), "read_only": True},
        }
        # Keep bounded paging/omission facts at the public inspection boundary
        # as well as in the lossless native payload, so human and machine
        # callers can state exactly what was returned without unpacking a
        # transport-specific field.
        for key in ("bounds", "page", "omission_metadata"):
            if isinstance(data.get(key), Mapping):
                projection[key] = dict(data[key])
        return projection
    def open_composition(
        self,
        project,
        ref,
        *,
        revision_id=None,
        limit=50,
        cursor=None,
        clip=None,
        occurrence=None,
        shot=None,
        track=None,
        asset=None,
        range_value=None,
        detail=False,
        neighbors=0,
    ):
        """Open one bounded, read-only composition projection.

        This is intentionally an adapter over the existing immutable timeline
        read.  It is the shared textual authority for ``show`` and the visual
        sister command; it does not create a second document or imply that
        source-media playback is available.
        """
        if not callable(getattr(self._client, "inspect_timeline", None)):
            return DomainResult.failure(
                ErrorObject(
                    "unavailable",
                    "Runtime does not expose canonical timeline inspection",
                    {"project": str(project), "timeline": str(ref)},
                )
            )
        resolved = self._resolve_timeline(project, ref)
        if not resolved.ok or not isinstance(resolved.data, Mapping):
            return resolved
        resolved_project = resolved.data.get("project_ref") or project
        resolved_ref = resolved.data.get("timeline_ref") or ref
        inspected = self.inspect(
            resolved_project,
            resolved_ref,
            revision_id=revision_id,
            limit=limit,
            clip=clip,
            occurrence=occurrence,
            shot=shot,
            track=track,
            asset=asset,
            range_value=range_value,
            detail=detail,
            neighbors=neighbors,
            cursor=cursor,
            _resolved_timeline=resolved,
        )
        if not inspected.ok or not isinstance(inspected.data, Mapping):
            return inspected
        projection = self._native_projection(
            inspected.data, project=resolved_project, ref=resolved_ref
        )
        return DomainResult.success(
            projection,
            receipt=inspected.receipt,
            idempotency_key=inspected.idempotency_key,
        )

    def script(self, project, ref, *, revision_id=None, occurrence=None, kind="voiceover_script"):
        """Read selected placed narration from one immutable composition closure.

        Text is resolved from each pinned shot revision, never the mutable
        binding head. Missing bindings and deliberately empty text differ.
        """
        from astrid.sdk.timeline_script import read_composition_script

        return read_composition_script(
            self, self._client, project, ref, revision_id=revision_id,
            occurrence=occurrence, kind=kind,
        )

    def open_bundle(self, project, ref, *, revision_id=None):
        """Read one complete, read-only authoring bundle (parent → shots → internal timelines).

        The same closure ``timeline_document.py checkout`` writes, pinned to the
        current head or to ``revision_id``. ``timelines show`` builds its cut
        table from it and ``timelines diff`` compares two of them. Nothing is
        written. Returns ``{bundle, project_id, timeline_id, revision_id,
        is_current_head}``.
        """
        from astrid.core.timeline.authoring_bundle import open_authoring_bundle

        opened = self.open_composition(project, ref, revision_id=revision_id, limit=1)
        if not opened.ok or not isinstance(opened.data, Mapping):
            return opened
        native = opened.data.get("native_inspection") if isinstance(opened.data.get("native_inspection"), Mapping) else {}
        summary = opened.data.get("summary") if isinstance(opened.data.get("summary"), Mapping) else {}
        project_id = native.get("project_id")
        timeline_id = native.get("timeline_id")
        head = summary.get("head_revision_id") or native.get("head_revision_id")
        revision = revision_id or summary.get("revision_id") or head
        if not all(isinstance(value, str) and value for value in (project_id, timeline_id, revision)):
            return DomainResult.failure(ErrorObject(
                "protocol_error",
                "timeline inspection did not return a project id, timeline id and revision",
                {"project": str(project), "timeline": str(ref)},
            ))
        parent = self._typed("get_project_parent_composition_revision", project_id, timeline_id, revision)
        if not parent.ok or not isinstance(parent.data, Mapping):
            return parent
        payload = parent.data.get("payload") if isinstance(parent.data.get("payload"), Mapping) else {}
        shots: dict[str, Any] = {}
        internals: dict[str, Any] = {}
        for occurrence in payload.get("occurrences") or []:
            if not isinstance(occurrence, Mapping):
                continue
            shot_revision = occurrence.get("shot_revision_id") or occurrence.get("revision_id")
            if shot_revision not in shots:
                shot = self._typed("get_project_shot_revision", project_id, occurrence.get("shot_id"), shot_revision)
                if not shot.ok or not isinstance(shot.data, Mapping):
                    return shot
                shots[shot_revision] = shot.data
            shot_data = shots[shot_revision]
            shot_payload = shot_data.get("payload") if isinstance(shot_data.get("payload"), Mapping) else {}
            internal_revision = shot_data.get("internal_timeline_revision_id") or shot_payload.get("internal_timeline_revision_id")
            if internal_revision not in internals:
                internal = self._typed("get_project_timeline_revision", project_id, timeline_id, internal_revision)
                if not internal.ok or not isinstance(internal.data, Mapping):
                    return internal
                internals[internal_revision] = internal.data
        try:
            bundle = open_authoring_bundle(
                parent.data,
                shot_revisions=list(shots.values()),
                internal_timeline_revisions=list(internals.values()),
            )
        except Exception as exc:  # the bundle compiler names the inconsistent field
            return DomainResult.failure(ErrorObject(
                "protocol_error", f"could not open the timeline closure: {exc}",
                {"timeline": timeline_id, "revision_id": revision},
            ))
        from .authoring_revision_diff import retain_revision_closure
        retain_revision_closure(bundle, parent.data, shots, internals)
        return DomainResult.success({
            "bundle": bundle,
            "project_id": project_id,
            "timeline_id": timeline_id,
            "revision_id": revision,
            "head_revision_id": head,
            "is_current_head": revision == head,
        })

    def save(self, project, ref, *, config: Mapping[str, Any], registry: Mapping[str, Any], expected_version=1, slug=None, name=None, idempotency_key=None):
        return self._retired_document_route("save", idempotency_key=idempotency_key)
    def replace_clip(
        self,
        project,
        ref,
        *,
        clip_id: str,
        source_object_id: str,
        expected_version: int,
        timing: str = "preserve-duration",
        idempotency_key=None,
    ):
        return self._retired_document_route("replace-clip", idempotency_key=idempotency_key)

    def replace_parent_media(
        self,
        project,
        ref,
        *,
        occurrence_id: str,
        clip_id: str,
        source_object_id: str,
        expected_head: str,
        idempotency_key=None,
    ):
        """Replace one selected clip in the canonical parent composition.

        A parent composition is an immutable parent -> shot revision ->
        internal-timeline closure.  This route opens that exact closure,
        edits the selected internal clip in a detached authoring bundle, and
        publishes one parent-CAS transaction.  It never falls back to the
        legacy timeline-document store used by ``replace_clip``.
        """
        key = idempotency_key or uuid.uuid4().hex
        required = {
            "project": project,
            "occurrence_id": occurrence_id,
            "clip_id": clip_id,
            "source_object_id": source_object_id,
            "expected_head": expected_head,
        }
        missing = [name for name, value in required.items() if not isinstance(value, str) or not value.strip()]
        if missing:
            return DomainResult.failure(
                ErrorObject("validation_error", "parent-composition replacement has missing required fields", {"fields": missing}),
                idempotency_key=key,
            )

        # Resolve only canonical Runtime identity here.  The replacement
        # operation already opens the pinned parent composition below; a
        # legacy document read would be both unnecessary and stale once the
        # Runtime document route is retired.
        timeline_result = self._resolve_timeline(project, ref)
        if not timeline_result.ok:
            return timeline_result
        timeline = timeline_result.data
        if not isinstance(timeline, Mapping):
            return DomainResult.failure(
                ErrorObject("protocol_error", "timeline read did not return an object", {"project": str(project), "timeline": str(ref)}),
                idempotency_key=key,
            )
        project_id = str(timeline.get("project_id") or project)
        timeline_id = str(timeline.get("timeline_id") or ref)

        parent_result = self._typed(
            "get_project_parent_composition_revision",
            project_id,
            timeline_id,
            expected_head,
        )
        if not parent_result.ok:
            return parent_result
        parent = parent_result.data
        if not isinstance(parent, Mapping):
            return DomainResult.failure(
                ErrorObject("protocol_error", "parent-composition read did not return an object", {"timeline": timeline_id}),
                idempotency_key=key,
            )
        payload = parent.get("payload")
        occurrences = payload.get("occurrences", []) if isinstance(payload, Mapping) else []
        target_occurrence = next(
            (row for row in occurrences if isinstance(row, Mapping) and row.get("occurrence_id") == occurrence_id),
            None,
        )
        if not isinstance(target_occurrence, Mapping):
            return DomainResult.failure(
                ErrorObject("not_found", "parent-composition occurrence was not found at the expected head", {"occurrence_id": occurrence_id, "expected_head": expected_head}),
                idempotency_key=key,
            )

        # Read every pinned child, not mutable child heads.  The candidate is
        # therefore a complete exact closure and can be published atomically.
        shot_revisions: dict[str, Mapping[str, Any]] = {}
        internal_revisions: dict[str, Mapping[str, Any]] = {}
        for row in occurrences:
            if not isinstance(row, Mapping):
                continue
            shot_id = row.get("shot_id")
            shot_revision_id = row.get("shot_revision_id", row.get("revision_id"))
            if not isinstance(shot_id, str) or not isinstance(shot_revision_id, str):
                return DomainResult.failure(
                    ErrorObject("protocol_error", "parent-composition occurrence is missing pinned shot identity", {"occurrence_id": row.get("occurrence_id")}),
                    idempotency_key=key,
                )
            if shot_revision_id not in shot_revisions:
                result = self._typed("get_project_shot_revision", project_id, shot_id, shot_revision_id)
                if not result.ok:
                    return result
                if not isinstance(result.data, Mapping):
                    return DomainResult.failure(ErrorObject("protocol_error", "shot revision read did not return an object", {"shot_revision_id": shot_revision_id}), idempotency_key=key)
                shot_revisions[shot_revision_id] = result.data
            shot = shot_revisions[shot_revision_id]
            internal_revision_id = shot.get("internal_timeline_revision_id")
            if not isinstance(internal_revision_id, str):
                shot_payload = shot.get("payload")
                internal_revision_id = shot_payload.get("internal_timeline_revision_id") if isinstance(shot_payload, Mapping) else None
            if not isinstance(internal_revision_id, str):
                return DomainResult.failure(ErrorObject("protocol_error", "shot revision is missing its pinned internal timeline", {"shot_revision_id": shot_revision_id}), idempotency_key=key)
            if internal_revision_id not in internal_revisions:
                result = self._typed("get_project_timeline_revision", project_id, timeline_id, internal_revision_id)
                if not result.ok:
                    return result
                if not isinstance(result.data, Mapping):
                    return DomainResult.failure(ErrorObject("protocol_error", "internal timeline revision read did not return an object", {"revision_id": internal_revision_id}), idempotency_key=key)
                internal_revisions[internal_revision_id] = result.data

        try:
            from astrid.core.timeline.authoring_bundle import (
                diff_authoring_candidate,
                open_authoring_bundle,
                preview_authoring_candidate,
                publish_authoring_candidate,
                validate_authoring_candidate,
            )

            candidate = open_authoring_bundle(
                parent,
                shot_revisions=list(shot_revisions.values()),
                internal_timeline_revisions=list(internal_revisions.values()),
            )
            placements = candidate.get("source_mapping", {}).get("placements", {})
            placement = placements.get(occurrence_id) if isinstance(placements, Mapping) else None
            if not isinstance(placement, Mapping):
                return DomainResult.failure(ErrorObject("not_found", "occurrence is absent from the authoring bundle", {"occurrence_id": occurrence_id}), idempotency_key=key)
            authoring_shot_id = placement.get("shot_id")
            shot = candidate.get("shots", {}).get(authoring_shot_id) if isinstance(candidate.get("shots"), Mapping) else None
            internal = shot.get("internal_timeline") if isinstance(shot, Mapping) else None
            clips = internal.get("clips", []) if isinstance(internal, Mapping) else []
            clip = next((row for row in clips if isinstance(row, dict) and row.get("id") == clip_id), None)
            if not isinstance(clip, dict):
                return DomainResult.failure(ErrorObject("not_found", "selected clip is absent from the target internal timeline", {"clip_id": clip_id, "occurrence_id": occurrence_id}), idempotency_key=key)

            source = str(source_object_id).strip()
            normalized = source if source.startswith("sha256:") else ("sha256:" + source if re.fullmatch(r"[0-9a-fA-F]{64}", source) else source)
            registry_candidates: list[Mapping[str, Any]] = []
            for registry_owner in (internal, candidate.get("parent")):
                registry = registry_owner.get("registry") if isinstance(registry_owner, Mapping) else None
                assets = registry.get("assets") if isinstance(registry, Mapping) else None
                if isinstance(assets, Mapping):
                    registry_candidates.append(assets)
            asset_key = None
            for assets in registry_candidates:
                for key_name, metadata in assets.items():
                    if not isinstance(key_name, str) or not isinstance(metadata, Mapping):
                        continue
                    values = {str(metadata.get(field)) for field in ("media_id", "object_id", "digest", "content_sha256") if metadata.get(field) is not None}
                    normalized_values = {value if value.startswith("sha256:") else "sha256:" + value for value in values if re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", value)}
                    if source in values or normalized in normalized_values:
                        asset_key = key_name
                        break
                if asset_key is not None:
                    break
            for field in ("asset", "asset_id", "media_id", "object_id"):
                clip.pop(field, None)
            if asset_key is not None:
                clip["asset"] = asset_key
            elif re.fullmatch(r"sha256:[0-9a-fA-F]{64}", normalized):
                clip["media_id"] = normalized.lower()
            else:
                return DomainResult.failure(ErrorObject("validation_error", "replacement media must be an admitted project-owned digest or registry asset", {"source_object_id": source}), idempotency_key=key)

            validation = validate_authoring_candidate(candidate)
            diff = diff_authoring_candidate(candidate)
            preview = preview_authoring_candidate(candidate)

            class _Writer:
                def __init__(self, owner):
                    self.owner = owner
                    self.result = None

                def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
                    self.result = self.owner._typed(
                        "publish_parent_composition",
                        project_id,
                        timeline_id,
                        publication,
                        key=idempotency_key,
                        idempotency_key=idempotency_key,
                    )
                    if not self.result.ok:
                        raise RuntimeError(self.result.error.message if self.result.error else "parent-composition publication failed")
                    return self.result.data

            writer = _Writer(self)
            published = publish_authoring_candidate(candidate, writer, idempotency_key=key)
            return DomainResult.success(
                {
                    "representation": "parent_composition",
                    "project_id": project_id,
                    "timeline_id": timeline_id,
                    "occurrence_id": occurrence_id,
                    "clip_id": clip_id,
                    "expected_head": expected_head,
                    "candidate_digest": preview.get("candidate_digest"),
                    "validation": validation,
                    "diff": diff,
                    "preview": preview,
                    "publication": published,
                },
                receipt=writer.result.receipt if writer.result is not None else None,
                idempotency_key=key,
            )
        except RuntimeError:
            # A typed publication failure was already captured by the writer;
            # return it without attempting a second route or mutation.
            if "writer" in locals() and writer.result is not None:
                return writer.result
            return DomainResult.failure(
                ErrorObject(
                    "validation_error",
                    "parent-composition candidate could not be published",
                    {"occurrence_id": occurrence_id, "clip_id": clip_id},
                ),
                idempotency_key=key,
            )
        except Exception as exc:
            return DomainResult.failure(
                ErrorObject("validation_error", "parent-composition candidate could not be prepared", {"reason": str(exc), "occurrence_id": occurrence_id, "clip_id": clip_id}),
                idempotency_key=key,
            )
    def history(self, project, ref, *, cursor=None, limit=50):
        return self._typed("list_timeline_history", ref, cursor=cursor, limit=limit)
    def diff(self, project, ref, *, from_version=None, to_version=None,
             from_revision=None, to_revision=None):
        """Compare two exact canonical closures, or explicit legacy versions."""
        revisions = from_revision is not None or to_revision is not None
        versions = from_version is not None or to_version is not None
        if revisions == versions or (revisions and not all(
            isinstance(value, str) and value.strip() for value in (from_revision, to_revision)
        )) or (versions and (from_version is None or to_version is None)):
            return DomainResult.failure(ErrorObject(
                "validation_error", "supply exactly one complete from_revision/to_revision or from_version/to_version pair", {}
            ))
        if versions:
            return self._typed("diff_timeline", ref, from_version=from_version, to_version=to_version)
        resolved = self._resolve_timeline(project, ref)
        if not resolved.ok:
            return resolved
        timeline_id = resolved.data["timeline_id"]
        from astrid.core.timeline.authoring_bundle import AuthoringBundleError
        from astrid.core.timeline.authoring_revision_diff import diff_authoring_revisions
        from .authoring_revision_diff import read_authoring_revision

        shown = self._typed("get_project", resolved.data["project_ref"])
        if not shown.ok:
            return shown
        if not isinstance(shown.data, Mapping):
            return DomainResult.failure(ErrorObject("protocol_error", "project response is not an object", {}))
        project_id = shown.data.get("project_id") or shown.data.get("id")
        if not isinstance(project_id, str) or not project_id:
            return DomainResult.failure(ErrorObject("protocol_error", "project response has no canonical identity", {}))
        if resolved.data.get("project_id", project_id) != project_id:
            return DomainResult.failure(ErrorObject("integrity_error", "timeline listing belongs to a different project", {}))
        before = read_authoring_revision(self, project_id, timeline_id, from_revision)
        if not before.ok:
            return before
        after = read_authoring_revision(self, project_id, timeline_id, to_revision)
        if not after.ok:
            return after
        try:
            return DomainResult.success(diff_authoring_revisions(before.data, after.data))
        except AuthoringBundleError as exc:
            return DomainResult.failure(ErrorObject("integrity_error", str(exc), {
                "project_id": project_id, "timeline_id": timeline_id,
                "from_revision": from_revision, "to_revision": to_revision,
            }))
    def _version(self, project, ref, expected_version):
        if expected_version is not None: return int(expected_version)
        current = self._resolve_timeline(project, ref)
        if not current.ok or not isinstance(current.data, Mapping):
            raise WorkspaceClientError(404, "not_found", "timeline not found", {"project": str(project), "ref": str(ref)})
        value = current.data.get("version", current.data.get("config_version", 1))
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise WorkspaceClientError(502, "protocol_error", "Runtime timeline listing has no valid version", {}) from exc
    def archive(self, project, ref, *, expected_version=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        try:
            version = self._version(project, ref, expected_version)
        except WorkspaceClientError as exc:
            return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=key)
        return self._typed("archive_timeline", ref, key=key, expected_version=version, idempotency_key=key)
    def recover(self, project, ref, *, expected_version=None, version=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        try:
            current_version = self._version(project, ref, expected_version)
        except WorkspaceClientError as exc:
            return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=key)
        return self._typed("recover_timeline", ref, key=key, expected_version=current_version, version=int(version) if version is not None else max(1, current_version - 1), idempotency_key=key)


class RemoteMedia(_RemoteFamily):
    def open(
        self,
        project: str,
        ref: Mapping[str, Any] | str,
        *,
        materialize: bool = False,
        cache_root: str | Path | None = None,
        preview_bytes: int = 16 * 1024,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> DomainResult[Any]:
        """Open one returned source/artifact reference through one read facade.

        Resolution and bounded delivery live in ``media_open`` so the SDK and
        evaluator-facing callers share exactly the same identity, scope,
        integrity, and unavailable behavior.  This operation has no Runtime
        mutation or render/task side effects.
        """
        from .media_open import open_reference

        return open_reference(
            self._client,
            project,
            ref,
            materialize=materialize,
            cache_root=cache_root,
            preview_bytes=preview_bytes,
            max_bytes=max_bytes,
        )

    @staticmethod
    def _managed_realm_error(realm: str | None, *, idempotency_key: str | None = None) -> DomainResult[Any] | None:
        if realm == "managed_local":
            return None
        return DomainResult.failure(
            ErrorObject(
                "validation_error",
                "only the managed_local media realm is supported",
                {"field": "realm", "value": realm, "valid_options": ["managed_local"]},
            ),
            idempotency_key=idempotency_key or "",
        )

    def import_file(self, *, project: str, path: Path, realm="managed_local", idempotency_key=None):
        realm_error = self._managed_realm_error(realm, idempotency_key=idempotency_key)
        if realm_error is not None:
            return realm_error
        try: data = path.read_bytes()
        except OSError: return DomainResult.failure(ErrorObject("not_found", "media source is unavailable", {}), idempotency_key=idempotency_key or "")
        if project is None: return DomainResult.failure(ErrorObject("validation_error", "media import requires a project", {"field": "project"}), idempotency_key=idempotency_key or "")
        key = idempotency_key or uuid.uuid4().hex
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        result = self._typed("ingest_project_object", project, data, key=key, media_type=media_type, idempotency_key=key, filename=path.name)
        if not result.ok or not isinstance(result.data, Mapping):
            return result
        object_id = result.data.get("object_id")
        if not isinstance(object_id, str):
            return result
        thumbnail_error: str | None
        try:
            from astrid.core.execution.thumbnail_backfill import (
                ThumbnailBackfillError,
                ensure_imported_source_thumbnail,
            )
            from astrid.core.execution.thumbnails import ThumbnailError

            thumbnail = ensure_imported_source_thumbnail(
                self._client,
                project_id=str(project),
                source_object_id=object_id,
                source_bytes=data,
                media_type=media_type,
                idempotency_key=key,
            )
        except (ThumbnailBackfillError, ThumbnailError, WorkspaceClientError, OSError, ValueError) as exc:
            thumbnail = None
            thumbnail_error = str(exc)
        else:
            thumbnail_error = None
        payload = dict(result.data)
        if payload.get("filename") not in (None, path.name):
            # The object already existed (often as a generation output, whose
            # stored name is its port); report the file the caller imported.
            payload["stored_filename"] = payload["filename"]
            payload["filename"] = path.name
        if thumbnail is not None:
            payload["thumbnail"] = dict(thumbnail)
        if thumbnail_error:
            payload["thumbnail_diagnostics"] = [{"code": "thumbnail_extraction_failed", "message": thumbnail_error[:240]}]
        return DomainResult.success(payload, receipt=result.receipt, idempotency_key=result.idempotency_key)
    def import_bytes(self, *, project: str, data: bytes, filename: str, media_type: str = "application/octet-stream", idempotency_key=None):
        """Import in-memory bytes (an SDK-built document) as one managed project object."""
        if project is None:
            return DomainResult.failure(ErrorObject("validation_error", "media import requires a project", {"field": "project"}), idempotency_key=idempotency_key or "")
        if not isinstance(data, (bytes, bytearray)):
            return DomainResult.failure(ErrorObject("validation_error", "import_bytes takes bytes", {"field": "data"}), idempotency_key=idempotency_key or "")
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("ingest_project_object", project, bytes(data), key=key, media_type=media_type, idempotency_key=key, filename=Path(filename).name)
    def import_directory(self, *, project: str, directory: Path, realm="managed_local", idempotency_key=None):
        realm_error = self._managed_realm_error(realm, idempotency_key=idempotency_key)
        if realm_error is not None:
            return realm_error
        items = []
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            result = self.import_file(project=project, path=path, realm=realm, idempotency_key=f"{idempotency_key or 'import'}-{path.name}")
            if not result.ok: return result
            items.append(result.data)
        return DomainResult.success(items, idempotency_key=idempotency_key or "")
    def list(self, project, *, cursor=None, limit=50):
        return self._typed("list_project_objects", project, cursor=cursor, limit=limit)
    def show(self, project, ref):
        rows = paged_rows(self._client.list_project_objects, str(project), limit=50)
        if rows is None:
            return DomainResult.failure(ErrorObject("not_found", "media object is not in the selected project", {"project": str(project)}))
        match = next(
            (
                item
                for item in rows
                if isinstance(item, dict)
                and str(ref) in {str(item.get("digest")), str(item.get("object_id"))}
            ),
            None,
        )
        if match is None:
            return DomainResult.failure(ErrorObject("not_found", "media object is not in the selected project", {"project": str(project)}))
        # ``get_object`` is the byte-download endpoint. ``media show`` is a
        # metadata read and must remain JSON-safe; returning the download
        # response would put raw bytes in the stable product envelope and make
        # ``--json`` fail during canonicalization.
        return DomainResult.success(match)
    def read_bytes(self, object_id: str) -> bytes:
        """Read runtime-authorized artifact bytes, including unscoped outputs.

        This Python-only method deliberately returns bytes rather than placing
        them in the JSON-safe product result envelope used by media.show.
        """
        from .exceptions import ServiceError, _SERVICE_ERROR_CLASSES

        try:
            response = self._client.get_object(object_id)
        except WorkspaceClientError as exc:
            error_type = _SERVICE_ERROR_CLASSES.get(exc.code, ServiceError)
            error = error_type(exc.message, details=exc.details)
            error.code = exc.code
            raise error from exc
        data = response.get("data") if isinstance(response, Mapping) else None
        if not isinstance(data, bytes):
            error = ServiceError(
                "runtime object download returned no bytes",
                details={"object_id": object_id},
            )
            error.code = "protocol_error"
            raise error
        return data

    def verify(self, project, ref, *, realm="managed_local", idempotency_key=None):
        realm_error = self._managed_realm_error(realm, idempotency_key=idempotency_key)
        if realm_error is not None:
            return realm_error
        scoped = self.show(project, ref)
        if not scoped.ok: return scoped
        result = self._typed("head_object", ref, key=idempotency_key)
        return DomainResult.success({"verified": True, "object_id": str(ref)}, idempotency_key=result.idempotency_key) if result.ok else result
    def relate(self, project, *, from_object_id: str, to_object_id: str, kind: str, metadata=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return self._typed(
            "create_media_relation",
            project,
            from_object_id,
            to_object_id,
            kind,
            key=key,
            metadata=metadata,
            idempotency_key=key,
        )

    def list_relations(self, project, *, cursor=None, limit=50):
        return self._typed("list_media_relations", project, cursor=cursor, limit=limit)

    def backfill_thumbnails(
        self,
        project: str,
        *,
        generation_id: str | None = None,
        limit: int = 50,
        dry_run: bool = False,
    ) -> DomainResult[Any]:
        """Run the bounded Runtime-owned thumbnail backfill on demand."""
        from astrid.core.execution.thumbnail_backfill import (
            ThumbnailBackfillError,
            run_thumbnail_backfill,
        )

        try:
            report = run_thumbnail_backfill(
                self._client,
                project=project,
                actor_id=getattr(self._client, "actor_id", None),
                limit=limit,
                generation_id=generation_id,
                dry_run=dry_run,
            )
        except ThumbnailBackfillError as exc:
            return DomainResult.failure(ErrorObject("validation_error", str(exc), {}))
        except WorkspaceClientError as exc:
            return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details))
        return DomainResult.success(report.as_dict())

    def backfill_variant_thumbnails(
        self,
        project: str,
        *,
        generation_id: str | None = None,
        limit: int = 100,
        dry_run: bool = False,
    ) -> DomainResult[Any]:
        """Backfill source-correct posters on every Runtime variant."""
        from astrid.core.execution.thumbnail_backfill import (
            ThumbnailBackfillError,
            run_variant_thumbnail_backfill,
        )

        try:
            report = run_variant_thumbnail_backfill(
                self._client,
                project=project,
                generation_id=generation_id,
                limit=limit,
                dry_run=dry_run,
            )
        except ThumbnailBackfillError as exc:
            return DomainResult.failure(ErrorObject("validation_error", str(exc), {}))
        except WorkspaceClientError as exc:
            return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details))
        return DomainResult.success(report.as_dict())

    def backfill_timeline_thumbnails(
        self,
        project: str,
        *,
        timeline: str,
        limit: int = 100,
        dry_run: bool = False,
    ) -> DomainResult[Any]:
        """Repair missing frame thumbnails referenced by a pinned canonical timeline."""
        from astrid.core.execution.thumbnail_backfill import (
            ThumbnailBackfillError,
            run_timeline_thumbnail_backfill,
        )

        try:
            report = run_timeline_thumbnail_backfill(
                self._client,
                project=project,
                timeline=timeline,
                limit=limit,
                dry_run=dry_run,
            )
        except ThumbnailBackfillError as exc:
            return DomainResult.failure(ErrorObject("validation_error", str(exc), {}))
        except WorkspaceClientError as exc:
            return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details))
        return DomainResult.success(report.as_dict())



class RemoteTasks(_RemoteFamily):
    def register_executor(self, *, executor_id: str, capabilities: list[str], idempotency_key: str):
        return self._typed("register_executor", {"executor_id": executor_id, "capabilities": capabilities}, key=idempotency_key, idempotency_key=idempotency_key)
    def create(
        self,
        *,
        project_id: str | None,
        capability: str,
        spec: Mapping[str, Any],
        input_manifest=None,
        idempotency_key=None,
        settlement_effect=None,
        storage_estimate: Mapping[str, int] | None = None,
        capability_digest: str | None = None,
        generation_intent: Mapping[str, Any] | None = None,
        execution_request: ExecutionRequest | Mapping[str, Any] | None = None,
        child_delegation: Mapping[str, Any] | None = None,
        deterministic_idempotency: bool = False,
    ):
        """Admit once, retaining explicit-key precedence and default fresh keys."""
        prepared = self.prepare_admission(
            project_id=project_id, capability=capability, spec=spec,
            input_manifest=input_manifest, idempotency_key=idempotency_key,
            settlement_effect=settlement_effect, storage_estimate=storage_estimate,
            capability_digest=capability_digest, generation_intent=generation_intent,
            execution_request=execution_request, child_delegation=child_delegation,
            deterministic_idempotency=deterministic_idempotency,
        )
        return self.dispatch_admission(prepared.data) if prepared.ok else prepared

    def prepare_admission(
        self,
        *,
        project_id: str | None,
        capability: str,
        spec: Mapping[str, Any],
        input_manifest=None,
        idempotency_key=None,
        settlement_effect=None,
        storage_estimate: Mapping[str, int] | None = None,
        capability_digest: str | None = None,
        generation_intent: Mapping[str, Any] | None = None,
        execution_request: ExecutionRequest | Mapping[str, Any] | None = None,
        child_delegation: Mapping[str, Any] | None = None,
        deterministic_idempotency: bool = False,
    ):
        """Complete admission and key without dispatching a task.

        Explicit caller keys take precedence. Preserve the existing key
        calculation and complete the transport's generated arguments before
        a caller may freeze them in a recovery receipt.
        """
        key = idempotency_key or (None if deterministic_idempotency else uuid.uuid4().hex)
        try:
            normalized_request, submitted_spec, admitted_manifest = _prepare_execution_admission(
                self._client,
                execution_request=execution_request,
                spec=spec,
                supplied_input_object_ids=input_manifest,
            )
        except ExecutionRequestError as exc:
            return DomainResult.failure(
                ErrorObject("validation_error", str(exc), {"field": "execution_request"}),
                idempotency_key=key or "",
            )
        # The runtime's merged capability catalog can place a live executor
        # registration beyond the first page of the static catalog. Fetch a
        # wide page so admission sees the live ready definition and can still
        # prefer it over an unavailable static duplicate.
        capabilities = paged_rows(self._client.list_capabilities, limit=50)
        if capabilities is None:
            return DomainResult.failure(
                ErrorObject(
                    "protocol_error",
                    "runtime capability listing returned an invalid page",
                    {},
                ),
                idempotency_key=key or "",
            )
        match = select_capability_row(capabilities, capability)
        if match is None:
            return DomainResult.failure(
                ErrorObject(
                    "not_found",
                    "capability is not registered",
                    {"capability_id": capability},
                ),
                idempotency_key=key or "",
            )
        if (
            project_id
            and isinstance(settlement_effect, Mapping)
            and settlement_effect.get("effect_type") == "generation.publish_v1"
            and settlement_effect.get("target_id") == project_id
        ):
            resolved = self._typed("get_project", project_id)
            if not resolved.ok:
                return resolved
            project_row = _full_mapping(resolved.data)
            canonical_id = (project_row or {}).get("project_id") or (project_row or {}).get("id")
            if not isinstance(canonical_id, str) or not canonical_id:
                return DomainResult.failure(ErrorObject("protocol_error", "project lookup returned no canonical id", {}))
            project_id = canonical_id
            settlement_effect = {**settlement_effect, "target_id": canonical_id}
        if capability == "vibecomfy.run":
            try:
                preflight_receipt = _vibecomfy_invocation_preflight(
                    self._client,
                    submitted_spec,
                    strict=(
                        isinstance(normalized_request, Mapping)
                        and "workflow" in normalized_request
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - normalize admission boundary
                return DomainResult.failure(
                    ErrorObject(
                        "validation_error",
                        "vibecomfy invocation preflight failed",
                        {"reason": str(exc)},
                    ),
                    idempotency_key=key or "",
                )
            if preflight_receipt is not None:
                submitted_spec["invocation_preflight"] = preflight_receipt
        admission = {
            "capability_id": capability,
            "capability_digest": str(
                capability_digest or match["definition_digest"]
            ),
            "input_object_ids": admitted_manifest,
            "project_id": project_id,
            "spec": submitted_spec,
            "settlement_effect": settlement_effect,
            "storage_estimate": storage_estimate,
        }
        if generation_intent is not None:
            admission["generation_intent"] = generation_intent
        if normalized_request is not None:
            admission["execution_request"] = normalized_request
        if child_delegation is not None:
            admission["child_delegation"] = dict(child_delegation)
        if key is None:
            # Hash the completed wire payload, after selection, normalization,
            # project resolution, and preflight. Sort mapping keys only: input
            # object order is part of Runtime's admission identity. Keeping this
            # at the transport boundary also includes future admission fields.
            key = hashlib.sha256(
                json.dumps(
                    admission,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
        prepare = getattr(self._client, "prepare_task_admission", None)
        if callable(prepare):
            try:
                admission = prepare(idempotency_key=key, **admission)
            except WorkspaceClientError as exc:
                return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=key)
            admission.pop("idempotency_key")
        # Round-trip with the shared SDK serializer so the receipt owns a
        # detached JSON payload, including list order and generated defaults.
        from astrid.core.receipts.canonical import canonical_json, parse_json
        from .results import _json_safe

        arguments = parse_json(canonical_json(_json_safe({**admission, "idempotency_key": key})), max_bytes=4 * 1024 * 1024)
        return DomainResult.success(arguments, idempotency_key=key)

    def dispatch_admission(self, arguments: Mapping[str, Any]):
        """Replay frozen generated arguments under current transport authority."""
        key = arguments["idempotency_key"]
        return self._typed("dispatch_task_admission", dict(arguments), key=key)

    def claim(
        self,
        *,
        executor_id: str,
        capability_ids: list[str],
        idempotency_key: str,
        runtime_epoch: int | None = None,
        target: Mapping[str, Any] | None = None,
    ):
        return self._typed(
            "claim_task",
            key=idempotency_key,
            executor_id=executor_id,
            capability_ids=capability_ids,
            runtime_epoch=runtime_epoch,
            target=target,
            idempotency_key=idempotency_key,
        )
    def settle(
        self,
        attempt_id: str,
        *,
        lease_id: str,
        fence: int,
        outputs: list[dict],
        idempotency_key: str,
        effect: dict | None = None,
        runtime_epoch: int | None = None,
    ):
        if runtime_epoch is None:
            health = self._client.health()
            runtime_epoch = int(health.get("runtime_epoch", 0)) if isinstance(health, dict) else 0
        settlement = {"lease_id": lease_id, "fence": fence, "outputs": outputs, "runtime_epoch": runtime_epoch}
        if effect is not None:
            settlement["effect"] = effect
        return self._typed("settle_attempt", attempt_id, settlement, key=idempotency_key, idempotency_key=idempotency_key)
    def fail(
        self,
        task_id: str,
        lease_id: str,
        error: str,
        *,
        retryable: bool = False,
        attempt_id: str,
        fence: int,
        runtime_epoch: int,
        failure_diagnostic: Mapping[str, Any] | None = None,
    ):
        payload: dict[str, Any] = {
            "message": str(error),
            "retryable": bool(retryable),
        }
        if failure_diagnostic is not None:
            payload["diagnostic"] = dict(failure_diagnostic)
        return self._typed(
            "fail_attempt",
            attempt_id,
            key=f"fail-{attempt_id}-{int(fence)}",
            lease_id=lease_id,
            fence=int(fence),
            error=payload,
            runtime_epoch=int(runtime_epoch),
            idempotency_key=f"fail-{attempt_id}-{int(fence)}",
        )
    def publish_timeline_render(self, attempt_id: str, publication: Mapping[str, Any], *, idempotency_key: str):
        """Use the Runtime publication checkpoint with a typed wire body."""
        if not isinstance(publication, Mapping):
            return DomainResult.failure(
                ErrorObject("validation_error", "timeline publication must be an object", {}),
                idempotency_key=idempotency_key,
            )
        required = (
            "lease_id", "fence", "runtime_epoch", "timeline_id",
            "expected_version", "config", "registry", "render",
        )
        missing = [field for field in required if field not in publication]
        if missing:
            return DomainResult.failure(
                ErrorObject("validation_error", "timeline publication is missing required fields", {"fields": missing}),
                idempotency_key=idempotency_key,
            )
        return self._typed(
            "publish_timeline_render", attempt_id, key=idempotency_key,
            idempotency_key=idempotency_key,
            lease_id=publication["lease_id"], fence=publication["fence"],
            runtime_epoch=publication["runtime_epoch"],
            timeline_id=publication["timeline_id"],
            expected_version=publication["expected_version"],
            config=publication["config"], registry=publication["registry"],
            render=publication["render"],
            **{key: publication[key] for key in ("slug", "name") if key in publication},
        )
    def list(self, project_id, *, cursor=None, limit=50):
        return self._typed("list_project_tasks", project_id, cursor=cursor, limit=limit)
    def show(self, task_id): return self._typed("get_task", task_id)
    def list_managed_outputs(self, task_id):
        """Read Runtime-owned managed outputs for one admitted task."""
        return self._typed("list_managed_outputs", task_id)
    def get_managed_output(self, association_id):
        """Read one Runtime-owned managed-output association."""
        return self._typed("get_managed_output", association_id)
    def cancel(self, task_id, *, idempotency_key=None): return self._typed("cancel_task", task_id, key=idempotency_key, idempotency_key=idempotency_key or uuid.uuid4().hex)
    def retry(self, task_id, *, idempotency_key=None): return self._typed("retry_task", task_id, key=idempotency_key, idempotency_key=idempotency_key or uuid.uuid4().hex)
    def events(self, task_id, *, cursor=None, limit=50):
        return self._typed(
            "list_events", cursor=cursor, limit=limit, aggregate_id=task_id
        )
class RemoteRuns(_RemoteFamily):
    def list(self, project_id, *, cursor=None, limit=50):
        return self._typed("list_project_runs", project_id, cursor=cursor, limit=limit)
    def show(self, run_id): return self._typed("get_run", run_id)
    def cancel(self, run_id, *, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("cancel_run", run_id, key=key, idempotency_key=key)
    def retry(self, run_id, *, selected_task_ids=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("retry_run", run_id, key=key, idempotency_key=key, selected_task_ids=selected_task_ids)
    def events(self, run_id, *, cursor=None, limit=50):
        return self._typed(
            "list_events", cursor=cursor, limit=limit, aggregate_id=run_id
        )
    def open(
        self,
        run_id=None,
        *,
        project=None,
        timeline=None,
        project_id=None,
        timeline_id=None,
        default_timeline=False,
        cache_root=None,
        opener=None,
        no_open=False,
    ):
        from .project_render import open_project_render
        from .contracts import DomainResult, ErrorObject
        if project is not None and project_id is not None and project != project_id:
            return DomainResult.failure(ErrorObject("validation_error", "project and project_id conflict", {"fields": ["project", "project_id"]}))
        if timeline is not None and timeline_id is not None and timeline != timeline_id:
            return DomainResult.failure(ErrorObject("validation_error", "timeline and timeline_id conflict", {"fields": ["timeline", "timeline_id"]}))
        selected_project = project if project is not None else project_id
        if selected_project is not None and (not isinstance(selected_project, str) or not selected_project.strip()):
            return DomainResult.failure(ErrorObject("validation_error", "project_id must be a non-empty string", {"field": "project_id"}))
        selected_timeline = timeline if timeline is not None else timeline_id
        if selected_timeline is not None and (not isinstance(selected_timeline, str) or not selected_timeline.strip()):
            return DomainResult.failure(ErrorObject("validation_error", "timeline_id must be a non-empty string", {"field": "timeline_id"}))
        return open_project_render(
            self._client, selected_project, run_id=run_id,
            timeline_ref=selected_timeline,
            default_timeline=default_timeline,
            cache_root=cache_root,
            opener=opener,
            no_open=no_open,
        )


class RemoteReferences(_RemoteFamily):
    def create(self, *, project: str, reference_id: str | None = None, kind: str = "other", name: str | None = None, media_id: str, description: str = "", metadata: Mapping[str, Any] | None = None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        if not project:
            return DomainResult.failure(ErrorObject("validation_error", "reference creation requires a project", {}), idempotency_key=key)
        reference_id = reference_id or str(uuid.uuid5(uuid.NAMESPACE_URL, f"astrid:reference:{key}"))
        body = {"reference_id": reference_id, "kind": kind, "name": name or reference_id, "media_id": media_id, "description": description, "metadata": metadata or {}}
        return self._typed("create_project_reference", project, body, key=key, idempotency_key=key)
    def list(self, project, *, cursor=None, limit=50, include_archived=False):
        return self._typed("list_project_references", project, cursor=cursor, limit=limit, include_archived=include_archived)
    def _resolve(self, project, ref):
        """Return ``(reference_id, None)`` for an id or exact name, else ``(None, failure)``."""
        if project is None or not isinstance(ref, str) or not ref.strip():
            return ref, None
        rows = paged_rows(self._client.list_project_references, project, limit=50, include_archived=True)
        if rows is None:
            return ref, None  # let the Runtime answer for the id
        if any(isinstance(row, Mapping) and row.get("reference_id") == ref for row in rows):
            return ref, None
        named = [row for row in rows if isinstance(row, Mapping) and row.get("name") == ref]
        if not named:
            folded = ref.strip().casefold()
            named = [row for row in rows if isinstance(row, Mapping) and str(row.get("name", "")).strip().casefold() == folded]
        active = [row for row in named if not row.get("archived")]
        chosen = active if len(active) == 1 else named
        if len(chosen) == 1:
            return str(chosen[0]["reference_id"]), None
        if chosen:
            return None, DomainResult.failure(ErrorObject("conflict", f"reference name {ref!r} is ambiguous; use an id", {"candidates": [str(row.get("reference_id")) for row in chosen]}))
        names = sorted(str(row.get("name")) for row in rows if isinstance(row, Mapping) and not row.get("archived"))
        return None, DomainResult.failure(ErrorObject("not_found", f"reference {ref!r} not found in project", {"project": project, "names": names}))
    def show(self, project, ref):
        ref, failure = self._resolve(project, ref)
        return failure or self._typed("get_project_reference", project, ref)
    def _version(self, ref, expected_version, project=None):
        if expected_version is not None: return int(expected_version)
        current = self._client.get_project_reference(project, ref)
        return int(current.get("version", 1))
    def update(self, project, ref, *, expected_version=None, name=None, description=None, metadata=None, idempotency_key=None):
        ref, failure = self._resolve(project, ref)
        if failure is not None: return failure
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped references are required", {"operation": "update_reference"}), idempotency_key=idempotency_key or "")
        try: version = self._version(ref, expected_version, project)
        except WorkspaceClientError as exc: return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=idempotency_key or "")
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("update_project_reference", project, ref, key=key, expected_version=version, name=name, description=description, metadata=metadata, idempotency_key=key)
    def archive(self, project, ref, *, expected_version=None, idempotency_key=None):
        ref, failure = self._resolve(project, ref)
        if failure is not None: return failure
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped references are required", {"operation": "archive_reference"}), idempotency_key=idempotency_key or "")
        try: version = self._version(ref, expected_version, project)
        except WorkspaceClientError as exc: return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=idempotency_key or "")
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("archive_project_reference", project, ref, key=key, expected_version=version, idempotency_key=key)
    def recover(self, project, ref, *, expected_version=None, idempotency_key=None):
        ref, failure = self._resolve(project, ref)
        if failure is not None: return failure
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped references are required", {"operation": "recover_reference"}), idempotency_key=idempotency_key or "")
        try: version = self._version(ref, expected_version, project)
        except WorkspaceClientError as exc: return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=idempotency_key or "")
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("recover_project_reference", project, ref, key=key, expected_version=version, idempotency_key=key)
    def associate(self, project, ref, *, media_id: str, role="depicts", association_id=None, metadata=None, context_task=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped references are required", {"operation": "associate_reference"}), idempotency_key=key)
        ref, failure = self._resolve(project, ref)
        if failure is not None: return failure
        metadata = dict(metadata or {})
        if context_task:
            metadata.setdefault("context_task", str(context_task))
        if role == "used_as_input" and not metadata.get("context_task"):
            return DomainResult.failure(ErrorObject("validation_error", "role used_as_input requires a context task", {"field": "context_task"}), idempotency_key=key)
        body = {"media_id": media_id, "role": role, **({"association_id": association_id} if association_id else {}), **({"metadata": metadata} if metadata else {})}
        return self._typed("associate_reference", project, ref, body, key=key, idempotency_key=key)
    def link(self, project, from_reference_id, to_reference_id, kind, *, metadata=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped references are required", {"operation": "link_references"}), idempotency_key=key)
        return self._typed("link_references", project, {"from_reference_id": from_reference_id, "to_reference_id": to_reference_id, "kind": kind, "metadata": metadata or {}}, key=key, idempotency_key=key)
    def set_primary(self, project, ref, *, association_id=None, expected_version=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped references are required", {"operation": "set_primary_reference"}), idempotency_key=key)
        if not association_id:
            return DomainResult.failure(ErrorObject("validation_error", "media reference association is required", {}), idempotency_key=key)
        ref, failure = self._resolve(project, ref)
        if failure is not None: return failure
        try: version = self._version(ref, expected_version, project)
        except WorkspaceClientError as exc: return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=key)
        return self._typed("set_primary_reference", project, ref, association_id, key=key, expected_version=version, idempotency_key=key)


class RemoteShots(_RemoteFamily):
    def group(self, project, timeline, *, clip_ids, name, expected_version, hold=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return DomainResult.failure(
            ErrorObject(
                "retired_route",
                "legacy shot grouping is retired; publish a canonical parent composition",
                {"status": 410, "operation": "shots.group", "replacement": "publish_parent_composition"},
            ),
            idempotency_key=key,
        )

    def list(self, project, *, cursor=None, limit=50, include_archived=False):
        return self._typed("list_project_shots", project, cursor=cursor, limit=limit, include_archived=include_archived)
    def show(self, project, shot_id): return self._typed("get_project_shot", project, shot_id)
    def create(self, *, project: str, shot: Mapping[str, Any] | None = None, name: str = "Shot", metadata: Mapping[str, Any] | None = None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        if not project:
            return DomainResult.failure(ErrorObject("validation_error", "shot creation requires a project", {}), idempotency_key=key)
        body = dict(shot or {})
        body.setdefault("shot_id", str(uuid.uuid5(uuid.NAMESPACE_URL, f"astrid:shot:{key}")))
        body.setdefault("name", name)
        if metadata is not None:
            body.setdefault("metadata", dict(metadata))
        return self._typed("create_project_shot", project, body, key=key, idempotency_key=key)
    def _version(self, shot_id, expected_version, project=None):
        if expected_version is not None: return int(expected_version)
        current = self._client.get_project_shot(project, shot_id)
        return int(current.get("version", 1))
    def update(self, project, shot_id, *, expected_version=None, name=None, metadata=None, idempotency_key=None):
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped shots are required", {"operation": "update_shot"}), idempotency_key=idempotency_key or "")
        try: version = self._version(shot_id, expected_version, project)
        except WorkspaceClientError as exc: return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=idempotency_key or "")
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("update_project_shot", project, shot_id, key=key, expected_version=version, name=name, metadata=metadata, idempotency_key=key)
    def archive(self, project, shot_id, *, expected_version=None, idempotency_key=None):
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped shots are required", {"operation": "archive_shot"}), idempotency_key=idempotency_key or "")
        try: version = self._version(shot_id, expected_version, project)
        except WorkspaceClientError as exc: return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=idempotency_key or "")
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("archive_project_shot", project, shot_id, key=key, expected_version=version, idempotency_key=key)
    def recover(self, project, shot_id, *, expected_version=None, idempotency_key=None):
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped shots are required", {"operation": "recover_shot"}), idempotency_key=idempotency_key or "")
        try: version = self._version(shot_id, expected_version, project)
        except WorkspaceClientError as exc: return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=idempotency_key or "")
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("recover_project_shot", project, shot_id, key=key, expected_version=version, idempotency_key=key)

    def add_item(self, project, shot_id, *, media_id, position=None, source_frame=None, metadata=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped shots are required", {"operation": "add_shot_item"}), idempotency_key=key)
        return self._typed("add_shot_item", project, shot_id, {"media_id": media_id, "position": position, "source_frame": source_frame, "metadata": metadata or {}}, key=key, idempotency_key=key)
    def remove_item(self, project, shot_id, item_id, *, expected_version=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped shots are required", {"operation": "remove_shot_item"}), idempotency_key=key)
        try: version = self._version(shot_id, expected_version, project)
        except WorkspaceClientError as exc: return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=key)
        return self._typed("remove_shot_item", project, shot_id, item_id, key=key, expected_version=version, idempotency_key=key)

    def promote_candidate(
        self,
        project,
        shot_id,
        candidate_item_id,
        *,
        expected_head_seq,
        timeline_assets=(),
        idempotency_key=None,
    ):
        key = idempotency_key or uuid.uuid4().hex
        if project is None:
            return DomainResult.failure(
                ErrorObject(
                    "unsupported_operation",
                    "project-scoped shots are required",
                    {"operation": "promote_shot_candidate"},
                ),
                idempotency_key=key,
            )
        return self._typed(
            "promote_project_shot_candidate",
            project,
            shot_id,
            candidate_item_id,
            key=key,
            expected_head_seq=expected_head_seq,
            timeline_assets=timeline_assets,
            idempotency_key=key,
        )
    def reorder(self, project, shot_id, item_ids=None, *, expected_version=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        if project is None:
            return DomainResult.failure(ErrorObject("unsupported_operation", "project-scoped shots are required", {"operation": "reorder_shot_items"}), idempotency_key=key)
        try: version = self._version(shot_id, expected_version, project)
        except WorkspaceClientError as exc: return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details), idempotency_key=key)
        return self._typed("reorder_shot_items", project, shot_id, list(item_ids or []), key=key, expected_version=version, idempotency_key=key)

    def list_text_bindings(self, project, *, shot_id=None, kind=None, slot=None, include_text=False):
        result = self._typed("list_project_shot_text_bindings", project, shot_id=shot_id, kind=kind, slot=slot)
        if not result.ok or not include_text:
            return result
        rows, cursor = result.data
        enriched = []
        for row in rows:
            content = self._text_binding_content(row)
            if not content.ok:
                return content
            enriched.append(content.data)
        return DomainResult.success([enriched, cursor])

    def show_text_binding(self, project, binding_id, *, include_text=False):
        result = self._typed("get_project_shot_text_binding", project, binding_id)
        return self._text_binding_content(result.data) if result.ok and include_text else result

    def _text_binding_content(self, binding):
        """Read the exact immutable text referenced by an authorized binding row."""
        import hashlib
        try:
            response = self._client.get_object(binding["media_id"])
            raw = response["data"] if isinstance(response, Mapping) else response.data
            if (not isinstance(raw, bytes) or
                    "sha256:" + hashlib.sha256(raw).hexdigest() != binding["content_hash"] or
                    len(raw) != binding["byte_size"]):
                return DomainResult.failure(ErrorObject("integrity_error", "Shot text bytes do not match the binding", {"binding_id": binding["binding_id"]}))
            return DomainResult.success({**binding, "text": raw.decode("utf-8")})
        except UnicodeDecodeError:
            return DomainResult.failure(ErrorObject("integrity_error", "Shot text is not valid UTF-8", {"binding_id": binding["binding_id"]}))
        except WorkspaceClientError as exc:
            return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details))

    def set_text_binding(self, project, *, shot_id, kind, text, expected_head, slot=None, binding_id=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        body = {"shot_id": shot_id, "kind": kind, "text": text, "expected_head": expected_head}
        if slot is not None: body["slot"] = slot
        if binding_id is not None:
            body = {"binding_id": binding_id, "text": text, "expected_head": expected_head}
        operation = "set_project_shot_text_binding_by_id" if binding_id is not None else "set_project_shot_text_binding"
        args = (project, binding_id, body) if binding_id is not None else (project, body)
        return self._typed(operation, *args, key=key, idempotency_key=key)

    def rebind_text_binding(self, project, binding_id, *, media_id, expected_head, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return self._typed("rebind_project_shot_text_binding", project, binding_id, key=key, media_id=media_id, expected_head=expected_head, idempotency_key=key)


class RemoteGenerations(_RemoteFamily):
    def _managed_outputs(self, task_id: str) -> DomainResult[list[dict[str, Any]]]:
        result = self._typed("list_managed_outputs", task_id)
        if not result.ok:
            return result
        page = page_pair(result.data)
        if page is None:
            return DomainResult.failure(
                ErrorObject(
                    "protocol_error",
                    "managed-output listing returned an invalid page",
                    {"task_id": task_id},
                ),
                idempotency_key=result.idempotency_key,
            )
        rows, _ = page
        mapped: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            normalized = _full_mapping(row)
            if normalized is None:
                return DomainResult.failure(
                    ErrorObject(
                        "protocol_error",
                        "managed-output listing returned an invalid row",
                        {"task_id": task_id, "index": index},
                    ),
                    idempotency_key=result.idempotency_key,
                )
            mapped.append(normalized)
        return DomainResult.success(mapped, idempotency_key=result.idempotency_key)

    def _managed_outputs_for_generation(
        self,
        task_id: str,
        generation_id: str,
    ) -> DomainResult[list[dict[str, Any]]]:
        result = self._managed_outputs(task_id)
        if not result.ok:
            return result
        return DomainResult.success(
            [
                row
                for row in result.data or []
                if row.get("generation_id") is not None
                and row.get("generation_id") == generation_id
            ],
            idempotency_key=result.idempotency_key,
        )

    @staticmethod
    def _with_data(result: DomainResult[Any], data: Any) -> DomainResult[Any]:
        return DomainResult.success(
            data,
            receipt=result.receipt,
            idempotency_key=result.idempotency_key,
        )

    def _enrich_generation(self, result: DomainResult[Any]) -> DomainResult[Any]:
        if not result.ok:
            return result
        resource = _full_mapping(result.data)
        if resource is None:
            return result
        task_id = _source_task_id(resource)
        generation_id = resource.get("generation_id")
        if task_id is None or not isinstance(generation_id, str) or not generation_id:
            return result
        managed = self._managed_outputs_for_generation(task_id, generation_id)
        if not managed.ok:
            return managed
        return self._with_data(
            result,
            {**resource, "managed_outputs": managed.data or []},
        )

    def _generation_source_task_id(self, generation_id: str) -> str | None:
        result = self._typed("get_generation", generation_id)
        if not result.ok:
            return None
        return _source_task_id(result.data)

    def create(self, *, project: str, generation_id: str, metadata=None, type="image", source_task_id=None, idempotency_key=None):
        key = idempotency_key or uuid.uuid4().hex
        return self._typed(
            "create_generation",
            project,
            generation_id,
            key=key,
            metadata=metadata or {},
            type=type,
            source_task_id=source_task_id,
            idempotency_key=key,
        )
    def list(self, project, *, cursor=None, limit=50):
        result = self._typed("list_generations", project, cursor=cursor, limit=limit)
        if not result.ok:
            return result
        page = page_pair(result.data)
        if page is None:
            return result
        rows, next_cursor = page
        cached: dict[str, list[dict[str, Any]]] = {}
        enriched: list[Any] = []
        for row in rows:
            resource = _full_mapping(row)
            task_id = _source_task_id(resource)
            generation_id = resource.get("generation_id") if resource is not None else None
            if (
                resource is None
                or task_id is None
                or not isinstance(generation_id, str)
                or not generation_id
            ):
                enriched.append(row)
                continue
            if task_id not in cached:
                managed = self._managed_outputs(task_id)
                if not managed.ok:
                    return managed
                cached[task_id] = managed.data or []
            enriched.append(
                {
                    **resource,
                    "managed_outputs": [
                        managed_row
                        for managed_row in cached[task_id]
                        if managed_row.get("generation_id") is not None
                        and managed_row.get("generation_id") == generation_id
                    ],
                }
            )
        return self._with_data(result, [enriched, next_cursor])

    def show(self, project, generation_id):
        return self._enrich_generation(self._typed("get_generation", generation_id))

    def variants(self, project, generation_id, *, cursor=None, limit=50):
        result = self._typed("list_variants", generation_id, cursor=cursor, limit=limit)
        if not result.ok:
            return result
        page = page_pair(result.data)
        if page is None:
            return result
        rows, next_cursor = page
        task_id = self._generation_source_task_id(generation_id)
        if task_id is None:
            return result
        managed = self._managed_outputs_for_generation(task_id, generation_id)
        if not managed.ok:
            return managed
        enriched: list[Any] = []
        for row in rows:
            resource = _full_mapping(row)
            if resource is None:
                enriched.append(row)
                continue
            if resource.get("generation_id") != generation_id:
                enriched.append(row)
                continue
            variant_id = resource.get("variant_id")
            object_id = resource.get("object_id")
            matches = [
                managed_row
                for managed_row in managed.data or []
                if (
                    isinstance(variant_id, str)
                    and managed_row.get("variant_key") == variant_id
                )
                or (
                    object_id is not None
                    and managed_row.get("object_id") == object_id
                )
            ]
            enriched.append({**resource, "managed_outputs": matches})
        return self._with_data(result, [enriched, next_cursor])

    def create_variant(
        self,
        generation_id: str,
        *,
        variant_id: str,
        object_id: str | None = None,
        variant_type="original",
        metadata=None,
        idempotency_key=None,
    ):
        key = idempotency_key or uuid.uuid4().hex
        return self._typed(
            "create_variant",
            generation_id,
            variant_id,
            key=key,
            object_id=object_id,
            variant_type=variant_type,
            metadata=metadata or {},
            idempotency_key=key,
        )


class RemoteAstridClient:
    def __init__(self, transport: WorkspaceClient):
        self._transport = transport
        from .documents import Documents, Preferences

        self.projects = RemoteProjects(transport)
        self.documents = Documents(transport)
        self.preferences = Preferences(transport)
        self.timelines = RemoteTimelines(transport, invoker=self._timeline_invoke_result)
        self.media = RemoteMedia(transport)
        self.tasks = RemoteTasks(transport)
        self.runs = RemoteRuns(transport)
        self.references = RemoteReferences(transport)
        self.shots = RemoteShots(transport)
        self.generations = RemoteGenerations(transport)

    def _timeline_invoke_result(self, capability_id: str, *, kind: str, **kwargs: Any):
        """Private dispatch used by the canonical timeline visualization facade."""
        from astrid.sdk.invocation import _invoke_internal_result

        return _invoke_internal_result(capability_id, kind=kind, client=self, **kwargs)

    def health(self): return self._transport.health()
    def handshake(self, client_name: str, client_version: str, requested_scopes: list[str]):
        return self._transport.handshake(client_name, client_version, requested_scopes)
    def doctor(self): return self._transport.doctor()
    def create_backup(self, destination): return self._transport.create_backup(destination)
    def restore_backup(self, backup, destination): return self._transport.restore_backup(backup, destination)
    def export_realm(self): return self._transport.export_realm()
    def tombstone_realm(self, *, reason=None, expected_version=None):
        return self._transport.tombstone_realm(reason=reason, expected_version=expected_version)
    def recover_realm(self, *, expected_realm_id, expected_version, confirmation=None, noninteractive=True):
        return self._transport.recover_realm(
            expected_realm_id=expected_realm_id,
            expected_version=expected_version,
            confirmation=confirmation,
            noninteractive=noninteractive,
        )
    def purge_realm(self, confirmation): return self._transport.purge_realm(confirmation)

    def invoke(
        self,
        capability_id: str,
        *,
        project_id: str,
        spec: Mapping[str, Any],
        capability_digest: str | None = None,
        input_object_ids: list[str] | None = None,
        idempotency_key: str | None = None,
        settlement_effect: Mapping[str, Any] | None = None,
        execution_request: ExecutionRequest | Mapping[str, Any] | None = None,
    ):
        capability_id = str(capability_id)
        key = idempotency_key or uuid.uuid4().hex
        try:
            normalized_request, submitted_spec, admitted_manifest = _prepare_execution_admission(
                self._transport,
                execution_request=execution_request,
                spec=spec,
                supplied_input_object_ids=input_object_ids,
            )
        except ExecutionRequestError as exc:
            return DomainResult.failure(
                ErrorObject("validation_error", str(exc), {"field": "execution_request"}),
                idempotency_key=key,
            )
        capabilities = paged_rows(self._transport.list_capabilities, limit=50)
        if capabilities is None:
            return DomainResult.failure(
                ErrorObject("protocol_error", "runtime capability listing returned an invalid page", {}),
                idempotency_key=key,
            )
        capability = select_capability_row(capabilities, capability_id)
        if capability is None:
            return DomainResult.failure(
                ErrorObject("not_found", "capability is not registered", {"capability_id": capability_id}),
                idempotency_key=key,
            )
        if capability_id == "vibecomfy.run":
            try:
                preflight_receipt = _vibecomfy_invocation_preflight(
                    self._transport,
                    submitted_spec,
                    strict=(
                        isinstance(normalized_request, Mapping)
                        and "workflow" in normalized_request
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - normalize admission boundary
                return DomainResult.failure(
                    ErrorObject(
                        "validation_error",
                        "vibecomfy invocation preflight failed",
                        {"reason": str(exc)},
                    ),
                    idempotency_key=key,
                )
            if preflight_receipt is not None:
                submitted_spec["invocation_preflight"] = preflight_receipt
        admission = {
            "key": key,
            "capability_id": capability_id,
            "capability_digest": str(
                capability_digest or capability["definition_digest"]
            ),
            "input_object_ids": admitted_manifest,
            "idempotency_key": key,
            "project_id": project_id,
            "spec": submitted_spec,
            "settlement_effect": settlement_effect,
        }
        if normalized_request is not None:
            admission["execution_request"] = normalized_request
        return self.tasks._typed("admit_task", **admission)
