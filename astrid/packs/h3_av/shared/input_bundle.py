"""Portable H3 dependencies using the existing verified asset archive contract.

Only asset IDs declared by the request are resolved. JSON strings are never
scanned for paths. Each executor derives private paths from the same immutable
bundle; preparation records carry member identities, not previous-attempt paths.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .engine_facts import facts
from .errors import CompilationError, PreparationError
from .request import H3Request, _asset_ids, normalize_request
from .request_v2 import read_prepared_request


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")



def _write_asset_bundle(
    path: Path,
    bindings: Mapping[str, Path],
    *,
    workflow_inputs: Mapping[str, Any] | None = None,
    lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        manifest = facts().build_asset_manifest(
            bindings,
            workflow_inputs=workflow_inputs,
            lineage=lineage,
        )
    except ValueError as exc:
        raise CompilationError(str(exc)) from exc
    payloads: list[tuple[str, bytes]] = []
    seen_members: set[str] = set()
    for record in manifest["assets"]:
        member = str(record["member"])
        source = Path(bindings[str(record["binding"])])
        if member not in seen_members:
            payloads.append((member, source.read_bytes()))
            seen_members.add(member)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for member, data in [("manifest.json", _canonical_bytes(manifest)), *payloads]:
            info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100600 << 16
            archive.writestr(info, data)
    return manifest



def build_input_bundle(request: H3Request, asset_map: Mapping[str, Any], destination: Path) -> Path:
    bindings: dict[str, Path] = {}
    for asset_id in _asset_ids(request):
        value = asset_map.get(asset_id)
        if not isinstance(value, str) or not value:
            raise PreparationError(f"asset {asset_id!r} needs a file in asset_map before task submission")
        bindings[asset_id] = Path(value).expanduser().resolve(strict=True)
    _write_asset_bundle(destination, bindings)
    # Re-read the completed bytes before importing: also catches a file changed
    # between manifest construction and archive writing.
    facts().read_archive(destination)
    return destination


def materialize_input_bundle(
    request: H3Request, bundle: Path, destination: Path,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    resolved = facts().read_archive(bundle)
    records = resolved.manifest["assets"]
    if {row["binding"] for row in records} != set(_asset_ids(request)):
        raise PreparationError("input bundle must contain exactly the request's declared assets")
    destination = destination.resolve()
    paths: dict[str, str] = {}
    identities: list[dict[str, Any]] = []
    for row in records:
        target = (destination / row["member"]).resolve()
        if not target.is_relative_to(destination):
            raise PreparationError("input bundle member escapes its attempt directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(resolved.members[row["member"]])
        paths[row["binding"]] = str(target)
        identities.append({
            "asset": row["binding"], "member": row["member"],
            "sha256": row["sha256"], "size": row["size"],
            "kind": "bundle_member", "status": "resolved",
        })
    return paths, identities


def bundle_digest(bundle: Path) -> str:
    return hashlib.sha256(bundle.read_bytes()).hexdigest()


def resolve_preparation_assets(
    preparation: Mapping[str, Any], bundle: Path, destination: Path,
) -> dict[str, Any]:
    if preparation.get("input_bundle_sha256") != bundle_digest(bundle):
        raise PreparationError("input bundle digest does not match preparation")
    raw_request = preparation.get("request")
    if not isinstance(raw_request, Mapping):
        raise PreparationError("preparation.request must be an object")
    if raw_request.get("version") == 2:
        request = read_prepared_request(
            raw_request, preparation.get("request_digest"), require_normalized_v2=True,
        )
    else:
        request = normalize_request(raw_request)
    paths, identities = materialize_input_bundle(request, bundle, destination)
    if preparation.get("assets") != identities:
        raise PreparationError("input bundle members do not match preparation asset evidence")
    return {**preparation, "assets": [
        {**record, "path": paths[record["asset"]]} for record in identities
    ]}
