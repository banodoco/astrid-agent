"""Canonical managed-asset/input manifest shared by VibeComfy boundaries.

The manifest is intentionally small.  It records the immutable archive members,
the public workflow input values that refer to those members, and optional
lineage for derived media.  Admission and worker staging must resolve inputs
through this module rather than each inventing a second binding contract.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping


class AssetManifestError(ValueError):
    """A managed-assets archive or its resolved input bindings are invalid."""


@dataclass(frozen=True, slots=True)
class ResolvedAssetManifest:
    """Validated archive manifest plus the inputs it authorizes."""

    manifest: dict[str, Any]
    bindings: dict[str, str]
    members: dict[str, bytes]


def _safe_member(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AssetManifestError(f"managed assets {field} must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.name:
        raise AssetManifestError(f"managed assets {field} must be a safe relative member")
    return value


def _validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AssetManifestError("managed assets manifest must be an object")
    kind = value.get("kind")
    version = value.get("schema_version")
    if kind != "vibecomfy_managed_assets" or version not in {1, 2}:
        raise AssetManifestError("managed assets manifest has an unsupported contract")
    records = value.get("assets")
    if not isinstance(records, list) or not records:
        raise AssetManifestError("managed assets manifest must declare at least one asset")
    normalized: dict[str, Any] = {
        "schema_version": int(version),
        "kind": kind,
        "assets": [],
    }
    bindings: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise AssetManifestError(f"managed assets record {index} is not an object")
        binding = record.get("binding")
        member = _safe_member(record.get("member"), field=f"record {index}.member")
        digest = record.get("sha256")
        size = record.get("size")
        if (
            not isinstance(binding, str)
            or not binding.strip()
            or binding in bindings
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or type(size) is not int
            or size < 0
        ):
            raise AssetManifestError(f"managed assets record {index} is invalid")
        bindings.add(binding)
        normalized_record = {
            "binding": binding,
            "member": member,
            "sha256": digest,
            "size": size,
        }
        lineage = record.get("lineage")
        if lineage is not None:
            if not isinstance(lineage, Mapping):
                raise AssetManifestError(f"managed assets record {index}.lineage must be an object")
            normalized_record["lineage"] = dict(lineage)
        normalized["assets"].append(normalized_record)

    workflow_inputs = value.get("workflow_inputs", {})
    if not isinstance(workflow_inputs, Mapping) or any(
        not isinstance(name, str) or not name for name in workflow_inputs
    ):
        raise AssetManifestError("managed assets workflow_inputs must be an object")
    try:
        json.dumps(workflow_inputs, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AssetManifestError("managed assets workflow_inputs must be JSON-safe") from exc
    normalized["workflow_inputs"] = dict(workflow_inputs)
    if "lineage" in value:
        if not isinstance(value["lineage"], Mapping):
            raise AssetManifestError("managed assets lineage must be an object")
        normalized["lineage"] = dict(value["lineage"])
    return normalized


def build_asset_manifest(
    bindings: Mapping[str, Path],
    *,
    workflow_inputs: Mapping[str, Any] | None = None,
    lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic manifest for one set of managed assets."""

    records: list[dict[str, Any]] = []
    for binding, source in sorted(bindings.items()):
        if not isinstance(binding, str) or not binding.strip():
            raise AssetManifestError("managed asset binding names must be non-empty strings")
        path = Path(source).expanduser().resolve(strict=True)
        if path.is_symlink() or not path.is_file():
            raise AssetManifestError(f"managed asset {binding!r} is not a regular file")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        safe_stem = "".join(
            character if character.isalnum() or character in "._-" else "-"
            for character in path.stem
        ).strip(".-") or binding.replace("/", "-") or "asset"
        member = f"assets/{digest[:16]}-{safe_stem}{path.suffix.lower()}"
        record: dict[str, Any] = {
            "binding": binding,
            "member": member,
            "sha256": digest,
            "size": len(data),
        }
        if isinstance(lineage, Mapping) and binding in lineage:
            record["lineage"] = lineage[binding]
        else:
            # Even an un-derived input gets a stable origin witness.  A later
            # preparation step can replace this with a derived record that
            # points back to the original digest without changing the archive
            # contract.
            record["lineage"] = {
                "kind": "source_bytes",
                "sha256": "sha256:" + digest,
                "filename": path.name,
            }
        records.append(record)
    return _validate_manifest(
        {
            "schema_version": 2,
            "kind": "vibecomfy_managed_assets",
            "assets": records,
            "workflow_inputs": dict(workflow_inputs or {}),
        }
    )


def resolve_inputs(
    manifest: Mapping[str, Any],
    supplied: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge caller inputs with manifest inputs, accepting only equal duplicates."""

    normalized = _validate_manifest(manifest)
    resolved = dict(normalized.get("workflow_inputs", {}))
    for name, value in dict(supplied or {}).items():
        if name in resolved and resolved[name] != value:
            raise AssetManifestError(
                "managed assets conflict with workflow inputs: " + str(name)
            )
        resolved[name] = value
    asset_values = {
        str(record["binding"]): PurePosixPath(str(record["member"])).name
        for record in normalized["assets"]
    }
    for name, value in asset_values.items():
        if name in resolved and resolved[name] != value:
            raise AssetManifestError(
                "managed assets conflict with workflow inputs: " + str(name)
            )
        resolved[name] = value
    return resolved


def _validate_archive_members(names: list[str]) -> None:
    if len(names) > 65 or len(names) != len(set(names)) or "manifest.json" not in names:
        raise AssetManifestError("managed assets archive has an invalid member inventory")


def read_archive_bytes(data: bytes) -> ResolvedAssetManifest:
    """Validate an archive from CAS bytes, as admission does."""

    try:
        archive = zipfile.ZipFile(io.BytesIO(data), "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise AssetManifestError("managed assets archive is unreadable") from exc
    with archive:
        names = archive.namelist()
        _validate_archive_members(names)
        try:
            raw_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssetManifestError("managed assets manifest is unreadable") from exc
        manifest = _validate_manifest(raw_manifest)
        expected = {"manifest.json"}
        members: dict[str, bytes] = {}
        for record in manifest["assets"]:
            member = str(record["member"])
            expected.add(member)
            try:
                info = archive.getinfo(member)
            except KeyError as exc:
                raise AssetManifestError(f"managed asset {member!r} is missing") from exc
            if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise AssetManifestError(f"managed asset {member!r} is not a regular file")
            payload = archive.read(member)
            if len(payload) != record["size"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
                raise AssetManifestError(f"managed asset {member!r} failed integrity validation")
            members[member] = payload
        if set(names) != expected:
            raise AssetManifestError("managed assets archive contains undeclared members")
    return ResolvedAssetManifest(
        manifest=manifest,
        bindings=resolve_inputs(manifest),
        members=members,
    )


def read_archive(path: str | Path) -> ResolvedAssetManifest:
    source = Path(path).expanduser().resolve(strict=True)
    if source.is_symlink() or not source.is_file():
        raise AssetManifestError(f"managed assets archive is not a regular file: {source}")
    return read_archive_bytes(source.read_bytes())


def file_map_bindings(name: str, files: Mapping[str, Any]) -> dict[str, Path]:
    """Binding names for a file-map input: ``<input>/<id>``, one managed asset per id."""

    if not isinstance(name, str) or not name or "/" in name:
        raise AssetManifestError("file map input names must be non-empty and contain no '/'")
    bindings: dict[str, Path] = {}
    for key, path in files.items():
        if not isinstance(key, str) or not key or "/" in key:
            raise AssetManifestError(f"file map {name!r} ids must be non-empty and contain no '/'")
        bindings[f"{name}/{key}"] = Path(path)
    return bindings


def file_map_members(name: str, resolved: ResolvedAssetManifest) -> dict[str, bytes]:
    """The ``{id: bytes}`` of one file-map input, read back from a validated archive by binding name."""

    prefix = f"{name}/"
    return {
        str(record["binding"])[len(prefix):]: resolved.members[str(record["member"])]
        for record in resolved.manifest["assets"]
        if str(record["binding"]).startswith(prefix)
    }


def write_archive(bindings: Mapping[str, Path], target: str | Path, **manifest_kwargs: Any) -> dict[str, Any]:
    """Write the managed-assets archive for ``bindings`` (exclusive create) and return its manifest."""

    manifest = build_asset_manifest(bindings, **manifest_kwargs)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        for record in manifest["assets"]:
            archive.write(Path(bindings[record["binding"]]), record["member"])
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(payload.getvalue())
    return manifest


__all__ = [
    "AssetManifestError",
    "file_map_bindings",
    "file_map_members",
    "write_archive",
    "ResolvedAssetManifest",
    "build_asset_manifest",
    "read_archive",
    "read_archive_bytes",
    "resolve_inputs",
]
