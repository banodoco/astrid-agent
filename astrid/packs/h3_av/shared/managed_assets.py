"""The managed-assets archive compile writes beside its graph (the media a trio binds).

The request's own files arrive as a ``file_map`` input (``stages.materialize_file_map``);
there is no input bundle.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .engine_facts import facts
from .errors import CompilationError


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
