"""Public SDK DTOs and JSON-safe result helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ._child_bridge import _ChildOutputBinding

from astrid.core.contracts.exec_error import ExecError
from astrid.core.contracts.schema import CapabilityHandle, Output, Port

from .exceptions import (
    AstridSDKError,
    CapabilityEventLogError,
    CapabilityInvocationError,
    CapabilityLeaseError,
    CapabilityMissingInputError,
    CapabilityPreconditionError,
    CapabilityRuntimeError,
    CapabilityValidationError,
)

CapabilityType = Literal["executor", "orchestrator", "element"]


def _json_safe(value: Any) -> Any:
    """Return a recursively JSON-safe copy of *value*."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, ExecError):
        return {
            "code": value.code,
            "type": value.type,
            "message": value.message,
            "recovery": value.recovery,
        }
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_safe(to_dict())
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        # Sets are accepted by a few repeatable SDK inputs for parity with
        # the CLI, but their iteration order is hash-seed dependent.  Emit a
        # sorted JSON array so result.to_dict() is both serializable and
        # stable across fresh Python processes.
        return [
            _json_safe(item)
            for item in sorted(value, key=lambda item: (type(item).__name__, str(item)))
        ]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if is_dataclass(value):
        return {field.name: _json_safe(getattr(value, field.name)) for field in fields(value)}
    return value


def _json_safe_mapping(value: Any) -> dict[str, Any]:
    payload = _json_safe(value)
    if not isinstance(payload, dict):
        raise TypeError(f"expected mapping payload, got {type(payload).__name__}")
    return payload


@dataclass(frozen=True)
class Capability:
    """Public inspectable capability DTO."""

    id: str
    capability_type: CapabilityType
    native_kind: str
    handle: CapabilityHandle
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Output, ...] = ()
    schema: Mapping[str, Any] = field(default_factory=dict)
    defaults: Mapping[str, Any] = field(default_factory=dict)
    definition: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "id": self.id,
                "capability_type": self.capability_type,
                "native_kind": self.native_kind,
                "handle": self.handle,
                "inputs": self.inputs,
                "outputs": self.outputs,
                "schema": self.schema,
                "defaults": self.defaults,
                "definition": self.definition,
            }
        )


@dataclass(frozen=True)
class DiscoveryResult:
    """Grouped public capability inventory."""

    executors: tuple[Capability, ...] = ()
    orchestrators: tuple[Capability, ...] = ()
    elements: tuple[Capability, ...] = ()
    capabilities: tuple[Capability, ...] = ()
    packs: tuple[Mapping[str, Any], ...] = ()
    generation_backends: tuple[Mapping[str, Any], ...] = ()
    element_kinds: tuple[Mapping[str, Any], ...] = ()
    generation_features: tuple[Mapping[str, Any], ...] = ()
    generation_modes: tuple[Mapping[str, Any], ...] = ()
    actions: tuple[Capability, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "actions": self.actions,
                "executors": self.executors,
                "orchestrators": self.orchestrators,
                "elements": self.elements,
                "capabilities": self.capabilities,
                "packs": self.packs,
                "generation_backends": self.generation_backends,
                "element_kinds": self.element_kinds,
                "generation_features": self.generation_features,
                "generation_modes": self.generation_modes,
            }
        )


@dataclass(frozen=True)
class MaterializedChildOutput:
    """Verified child descriptor and its host-selected local source path.

    ``filename`` is relative to the parent attempt and may include directories.
    The host chooses the flat Runtime registration filename separately.
    """

    output: Mapping[str, Any]
    filename: str

    def producer_file(self) -> dict[str, str]:
        """Return the existing producer-file input descriptor for this source.

        Pass the local attempt-relative ``filename`` through unchanged with
        its ``media_type`` and ``output_port`` for a subsequent child input.
        """
        return {"filename": self.filename, "media_type": self.output["media_type"],
                "output_port": self.output["output_port"]}

    def to_dict(self) -> dict[str, Any]:
        return {"output": dict(self.output), "filename": self.filename}


@dataclass(frozen=True)
class InvocationResult:
    """Public normalized execution result DTO."""

    capability_id: str
    capability_type: CapabilityType
    native_kind: str
    ok: bool
    error: Mapping[str, Any] | None = None
    manifest_path: str | None = None
    raw_result: Mapping[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    run_root: str | None = None
    outputs: Mapping[str, Any] = field(default_factory=dict)
    executor_version: str | None = None
    kernel_run_id: str | None = None
    kernel_task_id: str | None = None
    kernel_attempt_id: str | None = None
    # True when the Runtime returned an earlier task for identical admission
    # (idempotent replay) instead of starting a new one.
    replayed: bool = False
    replayed_task_id: str | None = None
    replayed_from: str | None = None
    _child_output_binding: _ChildOutputBinding | None = field(
        default=None, init=False, repr=False, compare=False,
    )

    def materialize_output(self, association_id: str) -> MaterializedChildOutput:
        """Materialize one verified output of this successful awaited child.

        The originating inherited bridge selects the destination. Each call,
        including repeats, asks the host to revalidate the exact association.
        """
        if self.ok is not True or self._child_output_binding is None:
            raise CapabilityValidationError("materialization requires a successful awaited child result")
        return self._child_output_binding.materialize(self, association_id)

    def __str__(self) -> str:
        """One line per output: port[n], its handle, and a viewable local path."""
        head = f"{self.capability_id} {'ok' if self.ok else 'FAILED'}" + (f"  run {self.run_id}" if self.run_id else "")
        if self.replayed:
            head += f"\n  replayed task {self.replayed_task_id} from {self.replayed_from}"
        if not self.ok:
            message = (self.error or {}).get("message") if isinstance(self.error, Mapping) else self.error
            return f"{head}\n  error: {message}"
        lines = [head]
        rows = [row for row in self.outputs.get("managed_outputs") or []
                if isinstance(row, Mapping) and row.get("role") != "thumbnail"]
        # Port, then ordinal: sprites[0..7] read in order, not in settlement order.
        rows.sort(key=lambda row: (str(row.get("output_port") or ""), int(row.get("ordinal") or 0)))
        for row in rows:
            label = f"{row.get('output_port')}[{row.get('ordinal', 0)}]"
            handle = row.get("handle")
            if not handle and self.run_id and row.get("output_port"):
                handle = f"run:{self.run_id}/{row.get('output_port')}#{row.get('ordinal', 0)}"
            lines.append(f"  {label:<22} {handle or row.get('digest')}  {row.get('path') or ''}".rstrip())
        return "\n".join(lines)

    def raise_for_error(self) -> InvocationResult:
        """Raise a :class:`CapabilityRuntimeError` when the task did not succeed.

        ``sdk.invoke(..., wait=True)`` returns normally for a failed task (``ok`` is
        False); call this right after the invoke so a failure stops the script.
        Returns ``self`` on success so it can be chained.
        """
        if self.ok:
            return self
        message = (self.error or {}).get("message") if isinstance(self.error, Mapping) else None
        if not message:
            raw_error = self.raw_result.get("error")
            if isinstance(raw_error, Mapping):
                message = raw_error.get("message")
        if not isinstance(message, str) or not message:
            message = "task did not succeed; inspect it with python3 -m astrid tasks"
        where = f" (run {self.run_id})" if self.run_id else ""
        raise CapabilityRuntimeError(f"{self.capability_id} FAILED{where}: {message}")

    def output_rows(self, port: str | None = None) -> list[dict[str, Any]]:
        """Return managed output rows for *port*, ordered by ordinal.

        With no port, select the first result port. Thumbnail rows are kept
        out of ordinary result selection unless the caller asks for them.
        Each returned row is also a media handle for the next invocation.
        """
        rows = [
            dict(row) for row in (self.outputs.get("managed_outputs") or [])
            if isinstance(row, Mapping)
        ]
        if port is None:
            port = next(
                (row.get("output_port") for row in rows if row.get("role") == "result"),
                None,
            )
        return sorted(
            (
                row for row in rows
                if row.get("output_port") == port
                and (port == "thumbnail" or row.get("role") != "thumbnail")
            ),
            key=lambda row: int(row.get("ordinal") or 0),
        )

    def output(self, port: str | None = None, n: int = 0) -> dict[str, Any]:
        """Return the *n*-th managed output row for *port*."""
        rows = self.output_rows(port)
        if not 0 <= n < len(rows):
            available = sorted({
                f"{row.get('output_port')}[{row.get('role')}]"
                for row in (self.outputs.get("managed_outputs") or [])
                if isinstance(row, Mapping)
            })
            raise LookupError(
                f"no output {port or '(result)'}[{n}] on this result "
                f"({len(rows)} matching row(s)); ports: "
                f"{', '.join(available) or 'none; invoke with wait=True'}"
            )
        return rows[n]

    def to_dict(self) -> dict[str, Any]:
        return _json_safe_mapping(
            {
                "capability_id": self.capability_id,
                "capability_type": self.capability_type,
                "native_kind": self.native_kind,
                "ok": self.ok,
                "error": self.error,
                "manifest_path": self.manifest_path,
                "run_id": self.run_id,
                "run_root": self.run_root,
                "outputs": self.outputs,
                "executor_version": self.executor_version,
                "raw_result": self.raw_result,
                "kernel_run_id": self.kernel_run_id,
                "kernel_task_id": self.kernel_task_id,
                "kernel_attempt_id": self.kernel_attempt_id,
            }
        )


_MANAGED_OUTPUT_FIELDS = (
    "association_id",
    "project_id",
    "run_id",
    "task_id",
    "attempt_id",
    "output_port",
    "group_key",
    "variant_key",
    "selector",
    "object_id",
    "digest",
    "manifest_ref",
    "size",
    "filename",
    "media_type",
    "ordinal",
    "role",
    "producer",
    "provenance",
    "durability",
    "state",
    "version",
    "lifecycle",
    "generation_id",
    "regeneration",
    "coverage",
    "expires_at",
    "pinned_at",
    "lease_id",
    "lease_owner",
    "lease_expires_at",
    "lifecycle_updated_at",
)
_MANAGED_OUTPUT_IDENTITY_FIELDS = (
    "association_id",
    "run_id",
    "task_id",
    "attempt_id",
    "output_port",
    "selector",
    "ordinal",
    "role",
    "filename",
    "media_type",
    "size",
    "digest",
    "durability",
)


def _managed_output_source(value: Any) -> Mapping[str, Any] | None:
    """Return an explicit managed-output row, never a generic output."""
    if isinstance(value, Mapping):
        source = value
    elif is_dataclass(value):
        source = {field.name: getattr(value, field.name) for field in fields(value)}
    else:
        return None
    if not all(field in source for field in _MANAGED_OUTPUT_IDENTITY_FIELDS):
        return None
    return source


def _managed_output_to_result_output(value: Any) -> Any:
    """Map one typed row into the existing manifest output entry shape."""
    source = _managed_output_source(value)
    if source is None:
        return value
    return {
        field: _json_safe(source[field])
        for field in _MANAGED_OUTPUT_FIELDS
        if field in source
    }


def _managed_generation_output_rows(
    raw_result: Mapping[str, Any],
) -> list[dict[str, Any]] | None:
    """Select only managed rows explicitly joined to a generation."""
    raw_outputs = raw_result.get("managed_outputs")
    if not isinstance(raw_outputs, list):
        return None
    mapped: list[dict[str, Any]] = []
    for output in raw_outputs:
        source = _managed_output_source(output)
        if source is None or source.get("generation_id") is None:
            continue
        result_output = _managed_output_to_result_output(output)
        if isinstance(result_output, dict):
            mapped.append(result_output)
    return mapped


def _normalize_generation_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Normalize explicit rows already carried by a result manifest."""
    normalized = dict(payload)
    manifest = payload.get("manifest")
    if not isinstance(manifest, Mapping):
        return normalized
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list):
        return normalized
    mapped = [_managed_output_to_result_output(output) for output in outputs]
    if all(mapped_output is output for mapped_output, output in zip(mapped, outputs)):
        return normalized
    normalized_manifest = dict(manifest)
    normalized_manifest["outputs"] = mapped
    normalized["manifest"] = normalized_manifest
    return normalized


def _join_managed_generation_outputs(
    payload: Mapping[str, Any],
    managed_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Join rows, replacing legacy copies of the same deliverable."""
    normalized = _normalize_generation_payload(payload)
    manifest = normalized.get("manifest")
    normalized_manifest = dict(manifest) if isinstance(manifest, Mapping) else {}
    outputs = normalized_manifest.get("outputs")
    existing_outputs = list(outputs) if isinstance(outputs, list) else []
    def identity_details(output: Any) -> dict[str, Any] | None:
        if not isinstance(output, Mapping):
            return None
        association_id = output.get("association_id")
        name = output.get("output_port", output.get("name"))
        if name is None:
            return None
        digest = output.get("digest") or output.get("content_hash") or ""
        try:
            ordinal = int(output["ordinal"]) if "ordinal" in output else None
        except (TypeError, ValueError):
            return None
        selector = output.get("selector")
        selector = selector if isinstance(selector, Mapping) else {}
        return {
            "association_id": str(association_id) if association_id else None,
            "output_port": str(name),
            "group_key": output.get("group_key", selector.get("group_key")),
            "variant_key": output.get("variant_key", selector.get("variant_key")),
            "ordinal": ordinal,
            "digest": str(digest),
        }

    def matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
        if left["output_port"] != right["output_port"]:
            return False
        for field in ("group_key", "variant_key", "ordinal", "digest"):
            left_value = left[field]
            right_value = right[field]
            if left_value is not None and right_value is not None and left_value != right_value:
                return False
        return True

    joined = list(existing_outputs)
    for output in managed_outputs:
        managed = identity_details(output)
        if managed is None:
            joined.append(output)
            continue
        if managed["association_id"] and any(
            (existing := identity_details(item)) is not None
            and existing["association_id"] == managed["association_id"]
            for item in joined
        ):
            continue
        legacy_matches = [
            index
            for index, item in enumerate(joined)
            if (existing := identity_details(item)) is not None
            and existing["association_id"] is None
            and matches(existing, managed)
        ]
        if legacy_matches:
            # A managed association is the canonical copy of this deliverable;
            # replace legacy manifest rows so their stale local paths cannot win.
            joined[legacy_matches[0]] = output
            for index in reversed(legacy_matches[1:]):
                del joined[index]
            continue
        if any(
            (existing := identity_details(item)) is not None
            and existing["association_id"] is not None
            and matches(existing, managed)
            for item in joined
        ):
            continue
        joined.append(output)
    normalized_manifest["outputs"] = joined
    normalized["manifest"] = normalized_manifest
    # The executor's local spool is removed after Runtime settlement. A
    # managed row is canonical, but it is not a caller-local materialization.
    normalized["image_paths"] = []
    normalized["run_dir"] = None
    return normalized


def _sdk_exception_from_payload(error: Mapping[str, Any] | None) -> AstridSDKError:
    message = "generation invocation failed"
    if error:
        raw_message = error.get("message")
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
    sdk_error_name = error.get("sdk_error") if error else None
    if isinstance(sdk_error_name, str):
        exc_type = globals().get(sdk_error_name)
        if isinstance(exc_type, type) and issubclass(exc_type, AstridSDKError):
            return exc_type(message)
    sdk_category = error.get("sdk_category") if error else None
    if sdk_category == "validation":
        return CapabilityValidationError(message)
    if sdk_category == "missing_input":
        return CapabilityMissingInputError(message)
    if sdk_category == "precondition":
        return CapabilityPreconditionError(message)
    if sdk_category == "runtime":
        return CapabilityRuntimeError(message)
    if sdk_category == "lease":
        return CapabilityLeaseError(message)
    if sdk_category == "event_log":
        return CapabilityEventLogError(message)
    return CapabilityInvocationError(message)


def _load_generation_result_type() -> tuple[str, Any]:
    from astrid.core.generation import GENERATION_RESULT_KEY
    from astrid.core.generation.backends.base import GenerationResult

    return GENERATION_RESULT_KEY, GenerationResult


def _reconstruct_generation_result(result: InvocationResult) -> Any:
    generation_result_key, generation_result_type = _load_generation_result_type()

    if not result.ok:
        raise _sdk_exception_from_payload(result.error)

    raw_result = result.raw_result
    if not isinstance(raw_result, Mapping):
        raise CapabilityRuntimeError("generation executor returned a non-mapping raw_result")

    managed_outputs = _managed_generation_output_rows(raw_result)
    payload_candidates: list[Mapping[str, Any]] = []
    payload = raw_result.get("payload")
    if isinstance(payload, Mapping):
        payload_candidates.append(payload)
    settled = raw_result.get("result")
    if isinstance(settled, Mapping):
        payload_candidates.append(settled)
        settled_payload = settled.get("payload")
        if isinstance(settled_payload, Mapping):
            payload_candidates.append(settled_payload)
    generation_payload: Any = None
    generation_payload_found = False
    explicit_generation_payload = False
    for candidate in payload_candidates:
        if generation_result_key in candidate:
            generation_payload = candidate[generation_result_key]
            generation_payload_found = True
            explicit_generation_payload = True
            break
    if not generation_payload_found:
        for candidate in payload_candidates:
            manifest = candidate.get("manifest")
            if isinstance(manifest, Mapping):
                generation_payload = {"manifest": manifest}
                generation_payload_found = True
                break
    if not generation_payload_found and managed_outputs:
        generation_payload = {"manifest": {"outputs": managed_outputs}}
        generation_payload_found = True
    if not generation_payload_found:
        if not payload_candidates:
            raise CapabilityRuntimeError("generation executor returned a non-mapping payload")
        raise CapabilityRuntimeError(
            f"generation executor payload is missing {generation_result_key!r}"
        )
    if isinstance(generation_payload, generation_result_type):
        if managed_outputs:
            generation_payload = generation_payload.to_dict()
        else:
            return generation_payload
    if not isinstance(generation_payload, Mapping):
        raise CapabilityRuntimeError(
            f"generation executor payload {generation_result_key!r} must be a mapping or GenerationResult"
        )
    if managed_outputs:
        generation_payload = _join_managed_generation_outputs(
            generation_payload,
            managed_outputs,
        )
    else:
        generation_payload = _normalize_generation_payload(generation_payload)
    from_dict = getattr(generation_result_type, "from_dict", None)
    if not callable(from_dict):
        raise CapabilityRuntimeError("GenerationResult.from_dict is unavailable")

    reconstructed = from_dict(dict(generation_payload))
    if not isinstance(reconstructed, generation_result_type):
        raise CapabilityRuntimeError("GenerationResult.from_dict returned an unexpected type")
    return reconstructed
