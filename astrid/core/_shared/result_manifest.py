"""Shared universal result-manifest helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NotRequired, Sequence, TypedDict

from astrid.core.contracts.errors import AstridError
from astrid.core.foundation.atomic_io import write_json_atomic as _atomic_write_json
from astrid.core.foundation.hash import sha256_file


def write_json_atomic(path: str | Path, payload: Any) -> None:
    """Atomically write *payload* as JSON to *path*.

    Delegates to :func:`astrid.core.foundation.atomic_io.write_json_atomic`,
    wrapping low-level ``OSError`` in :class:`AstridError`.
    """
    try:
        _atomic_write_json(path, payload)
    except OSError as exc:
        raise AstridError(f"failed to write {path}: {exc}") from exc

_REQUIRED_FIELDS = frozenset(
    {"schema_version", "kind", "inputs", "outputs", "created", "warnings"}
)
_KIND_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    return value


def _prefixed_hash(value: str) -> str:
    if value.startswith("sha256:"):
        return value
    return f"sha256:{value}"


def _resolve_hash(entry: Mapping[str, Any], *, path: Path) -> tuple[str, str | None]:
    content_hash = entry.get("content_hash")
    if isinstance(content_hash, str) and content_hash:
        return content_hash, None

    sha256_value = entry.get("sha256")
    if isinstance(sha256_value, str) and sha256_value:
        return _prefixed_hash(sha256_value), sha256_value

    digest = sha256_file(path)
    return f"sha256:{digest}", None


def _directory_children(path: Path) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
        rel_path = child.relative_to(path).as_posix()
        digest = sha256_file(child)
        children.append(
            {
                "path": rel_path,
                "bytes": child.stat().st_size,
                "content_hash": f"sha256:{digest}",
            }
        )
    return children


def _tree_hash(children: Sequence[Mapping[str, Any]]) -> str:
    canonical = json.dumps(list(children), sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _resolve_output_path(root_dir: Path, entry: Mapping[str, Any]) -> tuple[str, Path]:
    raw_path = entry.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("outputs[].path must be a non-empty string")
    candidate = Path(raw_path)
    resolved = candidate if candidate.is_absolute() else root_dir / candidate
    return raw_path, resolved


def complete_output_metadata(
    outputs: Sequence[Mapping[str, Any]],
    *,
    root_dir: str | Path,
) -> list[dict[str, Any]]:
    """Populate deterministic metadata for file and directory outputs."""

    completed: list[dict[str, Any]] = []
    base_dir = Path(root_dir)
    for entry in outputs:
        output = dict(_require_mapping(entry, "outputs[]"))
        raw_path, resolved_path = _resolve_output_path(base_dir, output)
        is_optional = bool(output.get("optional"))

        if not resolved_path.exists():
            if is_optional:
                output["path"] = raw_path
                output["missing"] = True
                completed.append(output)
                continue
            raise AstridError(
                f"required output missing: {resolved_path}",
                recovery_command="re-run the producing step so it writes the declared output",
            )

        output["path"] = raw_path
        output.pop("missing", None)

        if resolved_path.is_dir():
            children = _directory_children(resolved_path)
            output["entries"] = children
            output["bytes"] = int(output.get("bytes", sum(item["bytes"] for item in children)))
            output.setdefault("type", "directory")
            if not isinstance(output.get("content_hash"), str) or not output.get("content_hash"):
                output["content_hash"] = _tree_hash(children)
            completed.append(output)
            continue

        content_hash, sha256_value = _resolve_hash(output, path=resolved_path)
        output["content_hash"] = content_hash
        output["bytes"] = int(output.get("bytes", resolved_path.stat().st_size))
        output.setdefault("type", "file")
        if sha256_value is not None:
            output.setdefault("sha256", sha256_value)
        completed.append(output)

    return completed


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    missing_fields = sorted(_REQUIRED_FIELDS.difference(manifest))
    if missing_fields:
        raise ValueError(f"manifest missing required fields: {', '.join(missing_fields)}")

    schema_version = manifest["schema_version"]
    if not isinstance(schema_version, int):
        raise ValueError("schema_version must be an integer")

    kind = manifest["kind"]
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
        raise ValueError("kind must be a lowercase slug-like string")

    if not isinstance(manifest["warnings"], list):
        raise ValueError("warnings must be a list")

    if not isinstance(manifest["outputs"], list):
        raise ValueError("outputs must be a list")

    _require_mapping(manifest["inputs"], "inputs")


def write_manifest(path: str | Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Validate, enrich, and atomically write a universal result manifest."""

    _validate_manifest(manifest)
    manifest_payload = dict(manifest)
    manifest_path = Path(path)
    manifest_payload["outputs"] = complete_output_metadata(
        manifest_payload["outputs"],
        root_dir=manifest_path.parent,
    )
    write_json_atomic(manifest_path, manifest_payload)
    return manifest_payload


def build_manifest(
    *,
    kind: str,
    inputs: Mapping[str, Any],
    outputs: Sequence[Mapping[str, Any]],
    created: str,
    schema_version: int = 1,
    warnings: list[str] | None = None,
    **extras: Any,
) -> dict[str, Any]:
    """Build a manifest dict suitable for write_manifest().

    Centralizes common boilerplate so executors don't repeat the same
    dict literal.  Callers supply domain-specific fields; the helper fills
    in defaults for *schema_version* and *warnings*, and carries any
    extra keyword arguments as top-level passthrough fields.
    """
    manifest: dict[str, Any] = {
        "schema_version": schema_version,
        "kind": kind,
        "inputs": dict(inputs),
        "outputs": [dict(output) for output in outputs],
        "created": created,
        "warnings": list(warnings) if warnings is not None else [],
    }
    manifest.update(extras)
    return manifest


# ---------------------------------------------------------------------------
# Strict result-manifest reading and validation
# ---------------------------------------------------------------------------
#
# The executor boundary quarantines handler outputs: a handler result is
# accepted only as a universal result manifest whose every declared output is
# a *concrete, contained, verified* file inside the assigned staging
# directory. The strict validators below enforce that quarantine contract:
#
# - containment: every output path must resolve inside ``staging_root`` even
#   under symlink and parent-traversal adversarial cases (absolute paths,
#   ``..`` segments, and symlinks that escape staging are all rejected);
# - concrete files: a declared output must exist as a regular file inside
#   staging — missing files, directories (directory identities), and
#   non-regular files are rejected, and the optional flag never excuses a
#   missing concrete output;
# - exact hashes: every output must declare ``content_hash`` and the declared
#   byte SHA-256 must equal the file's recomputed digest;
# - unique ordinals: declared output ordinals must be unique (an absent
#   ordinal falls back to the output's position, which is unique by
#   construction);
# - at most one primary: no more than one output may be primary, and the
#   ``task_outputs`` DDL shape (``role = 'result' OR is_primary = 0``) is
#   enforced before materialization.


class ResultManifestError(AstridError):
    """Raised when a universal result manifest fails strict validation.

    Subclasses :class:`astrid.core.contracts.errors.AstridError` so callers
    can catch one error family for both parse and validation failures.
    """


@dataclass(frozen=True, slots=True)
class ValidatedResultOutput:
    """One validated, concrete file output of a result manifest.

    ``path`` is the posix path relative to the assigned staging directory —
    the only form the executor may hand to media materialization, so a
    handler can never smuggle an absolute or escaping location.
    """

    path: str
    ordinal: int
    content_hash: str
    bytes: int
    is_primary: bool
    role: str | None = None
    label: str | None = None
    filename: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe materialization descriptor for this output."""
        descriptor: dict[str, Any] = {
            "path": self.path,
            "ordinal": self.ordinal,
            "content_hash": self.content_hash,
            "bytes": self.bytes,
            "is_primary": self.is_primary,
        }
        if self.role is not None:
            descriptor["role"] = self.role
        if self.label is not None:
            descriptor["label"] = self.label
        if self.filename is not None:
            descriptor["filename"] = self.filename
        return descriptor


@dataclass(frozen=True, slots=True)
class ValidatedResultManifest:
    """A universal result manifest that passed strict quarantine validation."""

    schema_version: int
    kind: str
    created: str
    inputs: Mapping[str, Any]
    warnings: tuple[str, ...]
    outputs: tuple[ValidatedResultOutput, ...]
    staging_root: Path

    @property
    def primary_output(self) -> ValidatedResultOutput | None:
        """The single primary output, or ``None`` when none is declared."""
        for output in self.outputs:
            if output.is_primary:
                return output
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-safe manifest surface the executor consumes."""
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "created": self.created,
            "inputs": dict(self.inputs),
            "warnings": list(self.warnings),
            "outputs": [output.to_dict() for output in self.outputs],
        }


def _require_staging_root(staging_root: str | Path) -> Path:
    root = Path(staging_root)
    if not root.exists() or not root.is_dir():
        raise ResultManifestError(
            f"staging root must be an existing directory, got {str(root)!r}"
        )
    return root


def _contained_output_path(
    staging_root: Path, raw_path: str, *, entry_index: int
) -> tuple[str, Path]:
    """Resolve one declared output path inside ``staging_root``.

    Returns ``(normalized_relative_path, resolved_absolute_path)``. Rejects
    absolute paths, explicit ``..`` traversal, and any resolution — including
    symlink-followed resolution — that lands outside the staging root. The
    containment proof uses the resolved absolute path (so a symlink that
    escapes staging is rejected); the returned relative path is the declared
    location normalized to posix form.
    """
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise ResultManifestError(
            f"output {raw_path!r} escapes the assigned staging directory: "
            "absolute paths are not allowed"
        )
    if ".." in candidate.parts:
        raise ResultManifestError(
            f"output {raw_path!r} escapes the assigned staging directory: "
            "parent traversal is not allowed"
        )
    declared_rel = candidate.as_posix()
    resolved_root = staging_root.resolve()
    resolved = (staging_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        raise ResultManifestError(
            f"output {raw_path!r} escapes the assigned staging directory"
        ) from None
    return declared_rel, resolved


def validate_result_manifest(
    manifest: Mapping[str, Any],
    *,
    staging_root: str | Path,
) -> ValidatedResultManifest:
    """Strictly validate a universal result manifest against a staging root.

    Accepts only concrete, contained, hash-verified file outputs with unique
    ordinals and at most one primary output. Raises
    :class:`ResultManifestError` (an :class:`AstridError`) on the first
    violation; returns an immutable :class:`ValidatedResultManifest` whose
    output paths are staging-relative posix paths.
    """
    root = _require_staging_root(staging_root)

    missing_fields = sorted(_REQUIRED_FIELDS.difference(manifest))
    if missing_fields:
        raise ResultManifestError(
            "manifest missing required fields: " + ", ".join(missing_fields)
        )

    schema_version = manifest["schema_version"]
    if not isinstance(schema_version, int):
        raise ResultManifestError("schema_version must be an integer")

    kind = manifest["kind"]
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
        raise ResultManifestError("kind must be a lowercase slug-like string")

    created = manifest["created"]
    if not isinstance(created, str) or not created:
        raise ResultManifestError("created must be a non-empty string")

    inputs = manifest["inputs"]
    if not isinstance(inputs, Mapping):
        raise ResultManifestError("inputs must be an object")

    warnings_raw = manifest["warnings"]
    if not isinstance(warnings_raw, list) or not all(
        isinstance(item, str) for item in warnings_raw
    ):
        raise ResultManifestError("warnings must be a list of strings")
    warnings = tuple(warnings_raw)

    outputs_raw = manifest["outputs"]
    if not isinstance(outputs_raw, list) or not outputs_raw:
        raise ResultManifestError("outputs must be a non-empty list")

    seen_ordinals: set[int] = set()
    primary_count = 0
    validated: list[ValidatedResultOutput] = []
    for index, entry in enumerate(outputs_raw):
        if not isinstance(entry, Mapping):
            raise ResultManifestError(f"outputs[{index}] must be an object")
        output = entry

        raw_path = output.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ResultManifestError(
                f"outputs[{index}].path must be a non-empty string"
            )
        relative, resolved = _contained_output_path(root, raw_path, entry_index=index)

        # Concrete files only: missing files are rejected even when marked
        # optional, and directories are never valid media identities.
        if not resolved.exists():
            raise ResultManifestError(f"output {raw_path!r} is missing")
        if resolved.is_dir():
            raise ResultManifestError(
                f"output {raw_path!r} is a directory; directory identities "
                "are not concrete file outputs"
            )
        if output.get("type") == "directory":
            raise ResultManifestError(
                f"output {raw_path!r} declares type 'directory'; directory "
                "identities are not concrete file outputs"
            )
        if not resolved.is_file():
            raise ResultManifestError(
                f"output {raw_path!r} is not a regular file"
            )

        # Exact byte SHA-256: the declared content hash must match the file.
        declared_hash = output.get("content_hash")
        if not isinstance(declared_hash, str) or not declared_hash:
            raise ResultManifestError(
                f"output {raw_path!r} must declare content_hash"
            )
        actual_digest = sha256_file(resolved)
        actual_hash = f"sha256:{actual_digest}"
        if declared_hash != actual_hash:
            raise ResultManifestError(
                f"output {raw_path!r} declares content_hash {declared_hash!r} "
                f"but the file hashes to {actual_hash!r}"
            )

        # Exact byte size when declared.
        declared_bytes = output.get("bytes")
        actual_bytes = resolved.stat().st_size
        if declared_bytes is not None:
            if not isinstance(declared_bytes, int) or declared_bytes < 0:
                raise ResultManifestError(
                    f"output {raw_path!r} bytes must be a non-negative integer"
                )
            if declared_bytes != actual_bytes:
                raise ResultManifestError(
                    f"output {raw_path!r} declares bytes {declared_bytes} but "
                    f"the file is {actual_bytes} bytes"
                )

        # Unique ordinals: an explicit ordinal must be a non-negative integer
        # and never repeat; an absent ordinal falls back to the position.
        ordinal = output.get("ordinal", index)
        if not isinstance(ordinal, int):
            raise ResultManifestError(
                f"output {raw_path!r} ordinal must be an integer"
            )
        if ordinal < 0:
            raise ResultManifestError(
                f"output {raw_path!r} ordinal must be a non-negative integer"
            )
        if ordinal in seen_ordinals:
            raise ResultManifestError(
                f"duplicate output ordinal {ordinal} for {raw_path!r}"
            )
        seen_ordinals.add(ordinal)

        # Primary selection: at most one primary, mirroring the frozen
        # task_outputs constraint (role = 'result' OR is_primary = 0).
        is_primary = output.get("is_primary", False)
        if not isinstance(is_primary, bool):
            raise ResultManifestError(
                f"output {raw_path!r} is_primary must be a boolean"
            )
        role = output.get("role")
        if role is not None and (not isinstance(role, str) or not role):
            raise ResultManifestError(
                f"output {raw_path!r} role must be a non-empty string"
            )
        if is_primary and role is not None and role != "result":
            raise ResultManifestError(
                f"output {raw_path!r} has role {role!r} and cannot be primary "
                "(task_outputs requires role = 'result' OR is_primary = 0)"
            )
        if is_primary:
            primary_count += 1
        if primary_count > 1:
            raise ResultManifestError(
                "more than one primary output is not allowed"
            )

        label = output.get("label")
        if label is not None and (not isinstance(label, str) or not label):
            raise ResultManifestError(
                f"output {raw_path!r} label must be a non-empty string"
            )

        validated.append(
            ValidatedResultOutput(
                path=relative,
                ordinal=ordinal,
                content_hash=declared_hash,
                bytes=actual_bytes,
                is_primary=is_primary,
                role=role,
                label=label,
                filename=Path(relative).name,
            )
        )

    return ValidatedResultManifest(
        schema_version=schema_version,
        kind=kind,
        created=created,
        inputs=inputs,
        warnings=warnings,
        outputs=tuple(validated),
        staging_root=root,
    )


def read_result_manifest(
    path: str | Path,
    *,
    staging_root: str | Path,
) -> ValidatedResultManifest:
    """Read and strictly validate a universal result manifest file.

    Parses ``path`` as UTF-8 JSON and runs :func:`validate_result_manifest`
    against ``staging_root``. Raises :class:`ResultManifestError` for both
    parse failures and validation violations.
    """
    manifest_path = Path(path)
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ResultManifestError(
            f"cannot read result manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise ResultManifestError(
            f"result manifest {manifest_path} must be a JSON object"
        )
    return validate_result_manifest(payload, staging_root=staging_root)


# ---------------------------------------------------------------------------
# Host-agnostic harvest: {out}/manifest.json is the receipt
# ---------------------------------------------------------------------------

RESULT_MANIFEST_NAME = "manifest.json"

class HarvestError(ResultManifestError):
    """Raised when staged outputs cannot be harvested into concrete files."""


class HarvestedOutput(TypedDict):
    """One receipt-authoritative file ready for host settlement."""

    name: str
    path: str
    ordinal: int
    content_hash: str
    bytes: int
    role: str
    is_primary: bool
    media_type: NotRequired[str]


def outputs_required(definition: Any) -> bool:
    """Return whether a capability must produce harvestable files.

    True when the definition declares output ports or opts into the universal
    result-manifest receipt. Side-effect-only commands (empty outputs, no
    receipt flag) may still complete with an empty harvest.
    """
    if tuple(getattr(definition, "outputs", ()) or ()):
        return True
    metadata = getattr(definition, "metadata", None) or {}
    if isinstance(metadata, Mapping) and metadata.get("output_result_manifest"):
        return True
    return False


def declares_media_outputs(definition: Any) -> bool:
    """Return whether any declared output is media rather than a receipt/json."""
    for output in tuple(getattr(definition, "outputs", ()) or ()):
        artifact_hint = str(getattr(output, "artifact_type", None) or "").lower()
        type_hint = str(getattr(output, "type", None) or "").lower()
        name_hint = str(getattr(output, "name", None) or "").lower()
        explicit_hint = f"{artifact_hint} {type_hint}"
        if any(
            token in explicit_hint
            for token in ("video", "clip", "image", "audio", "media")
        ):
            return True
        if "manifest" not in name_hint and "receipt" not in name_hint:
            if any(
                token in name_hint
                for token in ("video", "clip", "image", "audio", "media")
            ):
                return True
    return False


def harvest_staged_outputs(
    output_root: str | Path,
    *,
    declared_outputs: Sequence[Any] = (),
    values: Mapping[str, Any] | None = None,
    require: bool = False,
    definition: Any | None = None,
) -> list[HarvestedOutput]:
    """Harvest concrete files from a host-assigned ``{out}`` spool.

    ``{out}/manifest.json``, when present, is the exclusive artifact receipt.
    Its entries provide port identity directly via ``name`` or ``port``; the
    harvester never guesses identity from file names or suffixes.  The only
    legacy path is declared ``path_template`` files for non-media definitions
    that have not opted into ``output_result_manifest``.
    """
    root = Path(output_root).expanduser().resolve()
    placeholders = dict(values or {})
    if definition is not None:
        if not declared_outputs:
            declared_outputs = tuple(getattr(definition, "outputs", ()) or ())
        metadata = getattr(definition, "metadata", None) or {}
        receipt_required = bool(
            isinstance(metadata, Mapping)
            and metadata.get("output_result_manifest")
        )
        media_required = declares_media_outputs(definition)
    else:
        receipt_required = False
        proxy = type("HarvestDefinition", (), {"outputs": declared_outputs})()
        media_required = declares_media_outputs(proxy)

    if not root.is_dir():
        if require or receipt_required or media_required:
            raise HarvestError(f"output root is not a directory: {root}")
        return []

    manifest_path = root / RESULT_MANIFEST_NAME
    if manifest_path.exists():
        harvested = _collect_manifest_files(root, manifest_path)
        if (require or media_required) and not harvested:
            raise HarvestError(f"no concrete outputs harvested under {root}")
        return harvested

    if receipt_required or media_required:
        raise HarvestError(f"missing result manifest receipt: {manifest_path}")

    harvested = _collect_declared_files(root, declared_outputs, placeholders)
    if require and not harvested:
        raise HarvestError(f"no concrete outputs harvested under {root}")
    return harvested


def _expand_declared_path(
    output: Any, root: Path, values: Mapping[str, Any]
) -> Path | None:
    template = getattr(output, "path_template", None)
    if not template:
        return None
    path = str(template)
    for key, value in values.items():
        path = path.replace("{" + key + "}", str(value))
    candidate = Path(path)
    return candidate if candidate.is_absolute() else (root / candidate)


def _collect_manifest_files(
    root: Path, manifest_path: Path
) -> list[HarvestedOutput]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarvestError(f"cannot read result manifest {manifest_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise HarvestError(f"result manifest {manifest_path} must be a JSON object")
    outputs = payload.get("outputs")
    if not isinstance(outputs, list):
        raise HarvestError("manifest outputs must be a list")

    flattened: list[tuple[int, Mapping[str, Any]]] = []
    for index, entry in enumerate(outputs):
        if not isinstance(entry, Mapping):
            raise HarvestError(f"outputs[{index}] must be an object")
        entries = entry.get("entries")
        if entries is None:
            flattened.append((index, entry))
            continue
        if not isinstance(entries, list):
            raise HarvestError(f"outputs[{index}].entries must be a list")
        raw_parent = entry.get("path")
        if not isinstance(raw_parent, str) or not raw_parent.strip():
            raise HarvestError(f"outputs[{index}].path must be a non-empty string")
        for child_index, child in enumerate(entries):
            if not isinstance(child, Mapping):
                raise HarvestError(
                    f"outputs[{index}].entries[{child_index}] must be an object"
                )
            raw_child = child.get("path")
            if not isinstance(raw_child, str) or not raw_child.strip():
                raise HarvestError(
                    f"outputs[{index}].entries[{child_index}].path must be a non-empty string"
                )
            concrete = dict(entry)
            concrete.pop("entries", None)
            # Parent directory metadata describes the directory inventory,
            # never a child's concrete bytes.  Every flattened file must
            # carry its own path, hash, and byte count in the receipt.
            concrete.pop("content_hash", None)
            concrete.pop("bytes", None)
            concrete.update(child)
            concrete["path"] = (Path(raw_parent) / raw_child).as_posix()
            # A directory-level primary marks its first concrete child only.
            if "is_primary" not in child and entry.get("is_primary") is True:
                concrete["is_primary"] = child_index == 0
            # Directory ordinals cannot identify multiple children. Each child
            # therefore receives its own flattened-position default.
            if "ordinal" not in child:
                concrete.pop("ordinal", None)
            flattened.append((index, concrete))

    collected: list[HarvestedOutput] = []
    seen_ordinals: set[int] = set()
    primary_count = 0
    for flat_index, (source_index, entry) in enumerate(flattened):
        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise HarvestError(f"outputs[{source_index}].path must be a non-empty string")
        try:
            _relative, resolved = _contained_output_path(
                root, raw_path, entry_index=source_index
            )
        except ResultManifestError as exc:
            raise HarvestError(str(exc)) from exc
        if not resolved.exists():
            raise HarvestError(f"output {raw_path!r} is missing")
        if not resolved.is_file():
            raise HarvestError(f"output {raw_path!r} is not a concrete file")

        name = entry.get("name")
        port = entry.get("port")
        if name is not None and port is not None and name != port:
            raise HarvestError(
                f"output {raw_path!r} has conflicting name and port identities"
            )
        identity = name if name is not None else port
        if not isinstance(identity, str) or not identity.strip():
            raise HarvestError(f"output {raw_path!r} must declare name or port")

        declared_hash = entry.get("content_hash")
        if not isinstance(declared_hash, str) or not declared_hash:
            raise HarvestError(f"output {raw_path!r} must declare content_hash")
        actual_hash = f"sha256:{sha256_file(resolved)}"
        if declared_hash != actual_hash:
            raise HarvestError(
                f"output {raw_path!r} declares content_hash {declared_hash!r} "
                f"but the file hashes to {actual_hash!r}"
            )

        declared_bytes = entry.get("bytes")
        if (
            not isinstance(declared_bytes, int)
            or isinstance(declared_bytes, bool)
            or declared_bytes < 0
        ):
            raise HarvestError(
                f"output {raw_path!r} must declare bytes as a non-negative integer"
            )
        actual_bytes = resolved.stat().st_size
        if declared_bytes != actual_bytes:
            raise HarvestError(
                f"output {raw_path!r} declares bytes {declared_bytes} but "
                f"the file is {actual_bytes} bytes"
            )

        ordinal = entry.get("ordinal", flat_index)
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise HarvestError(
                f"output {raw_path!r} ordinal must be a non-negative integer"
            )
        if ordinal in seen_ordinals:
            raise HarvestError(f"duplicate output ordinal {ordinal} for {raw_path!r}")
        seen_ordinals.add(ordinal)

        role = entry.get("role", "result")
        if role not in ("result", "auxiliary"):
            raise HarvestError(
                f"output {raw_path!r} role must be 'result' or 'auxiliary'"
            )
        if resolved == manifest_path.resolve() and role == "result":
            raise HarvestError(
                "manifest.json is the receipt and cannot be a result artifact"
            )
        is_primary = entry.get("is_primary", False)
        if not isinstance(is_primary, bool):
            raise HarvestError(f"output {raw_path!r} is_primary must be a boolean")
        if is_primary and role != "result":
            raise HarvestError(
                f"output {raw_path!r} with role {role!r} cannot be primary"
            )
        primary_count += int(is_primary)
        if primary_count > 1:
            raise HarvestError("more than one primary output is not allowed")

        collected.append(
            {
                "name": identity,
                "path": str(resolved),
                "ordinal": ordinal,
                "content_hash": declared_hash,
                "bytes": actual_bytes,
                "role": role,
                "is_primary": is_primary,
                "ordinal_explicit": "ordinal" in entry,
                **(
                    {"media_type": entry["media_type"]}
                    if isinstance(entry.get("media_type"), str)
                    else {}
                ),
                **{
                    field: entry[field]
                    for field in (
                        "output_port", "group_key", "variant_key", "selector",
                        "producer", "provenance", "durability", "regeneration", "coverage",
                    )
                    if field in entry
                },
            }
        )
    return collected


def _collect_declared_files(
    root: Path,
    declared_outputs: Sequence[Any],
    values: Mapping[str, Any],
) -> list[HarvestedOutput]:
    collected: list[HarvestedOutput] = []
    for ordinal, output in enumerate(declared_outputs):
        expanded = _expand_declared_path(output, root, values)
        if expanded is None:
            continue
        candidate = expanded.resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            raise HarvestError(f"declared output path escapes output root: {candidate}") from None
        if not candidate.is_file() or candidate.name == RESULT_MANIFEST_NAME:
            continue
        digest = f"sha256:{sha256_file(candidate)}"
        collected.append(
            {
                "name": str(getattr(output, "name")),
                "path": str(candidate),
                "ordinal": ordinal,
                "content_hash": digest,
                "bytes": candidate.stat().st_size,
                "role": "result",
                "is_primary": False,
            }
        )
    return collected


__all__ = [
    "HarvestError",
    "HarvestedOutput",
    "RESULT_MANIFEST_NAME",
    "ResultManifestError",
    "ValidatedResultManifest",
    "ValidatedResultOutput",
    "build_manifest",
    "complete_output_metadata",
    "declares_media_outputs",
    "harvest_staged_outputs",
    "outputs_required",
    "read_result_manifest",
    "validate_result_manifest",
    "write_manifest",
]
