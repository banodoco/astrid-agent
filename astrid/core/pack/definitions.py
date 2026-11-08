"""Capability definitions recorded by the install transaction.

The install transaction writes ``.build/definitions.json`` for every installed
pack. Each digest is ``_capability_digest(definition.to_dict())`` of the
definition exactly as the generic host registers it at the pack's final
location, so it equals the digest the host's select check compares.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

_EXECUTOR_MANIFESTS = ("executor.yaml", "executor.yml", "executor.json")
_ORCHESTRATOR_MANIFESTS = ("orchestrator.yaml", "orchestrator.yml", "orchestrator.json")


def _rewrite(value: Any, prefixes: tuple[str, ...], final: str) -> Any:
    """Move staged absolute paths to the final install location."""
    if isinstance(value, str):
        for prefix in prefixes:
            if value == prefix or value.startswith(prefix + "/"):
                return final + value[len(prefix):]
        return value
    if isinstance(value, Mapping):
        return {key: _rewrite(item, prefixes, final) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_rewrite(item, prefixes, final) for item in value]
    return value


def pack_definitions(staged_collection: Path, final_collection: Path, pack_id: str) -> list[dict[str, str]]:
    """Return ``[{path, capability_kind, capability_id, definition_digest}]`` for one staged pack."""
    from astrid.core.execution.executor.folder import discover_folder_executor_roots, load_folder_executor
    from astrid.core.execution.generic_host import _attach_pack_metadata, _capability_digest
    from astrid.core.execution.orchestrator.folder import (
        discover_folder_orchestrator_roots, load_folder_orchestrator,
    )

    staged_pack = Path(staged_collection) / pack_id
    final_pack = Path(final_collection) / pack_id
    prefixes = tuple(dict.fromkeys((str(staged_pack.resolve()), str(staged_pack))))
    rows: list[dict[str, str]] = []

    def record(kind: str, root: Path, definition: Any, names: tuple[str, ...]) -> None:
        manifest = next((root / name for name in names if (root / name).is_file()), None)
        if manifest is None:
            return
        described = _attach_pack_metadata(definition, root)
        digest = _capability_digest(_rewrite(json.loads(json.dumps(described.to_dict(), default=str)),
                                             prefixes, str(final_pack.resolve())))
        rows.append({"path": manifest.relative_to(staged_pack.resolve()).as_posix(), "capability_kind": kind,
                     "capability_id": described.id, "definition_digest": digest})

    for root in discover_folder_executor_roots(staged_pack):
        record("executor", root, load_folder_executor(root), _EXECUTOR_MANIFESTS)
    for root in discover_folder_orchestrator_roots(staged_pack):
        record("orchestrator", root, load_folder_orchestrator(root), _ORCHESTRATOR_MANIFESTS)
    return sorted(rows, key=lambda row: (row["capability_kind"], row["capability_id"], row["path"]))


def definitions_record(rows: list[dict[str, str]], provenance: Mapping[str, Any]) -> dict[str, Any]:
    """The version-1 ``definitions.json`` body.

    Git installs carry ``source: {kind: "git", repository, revision, tree}``;
    local-folder installs keep repository/revision/tree null and carry
    ``source: {kind: "folder", path}`` naming the source folder.
    """
    if provenance.get("revision") is None:
        source: dict[str, Any] = {"kind": "folder", "path": provenance.get("local_path")}
    else:
        source = {"kind": "git", "repository": provenance.get("repository"),
                  "revision": provenance.get("revision"), "tree": provenance.get("tree")}
    return {"version": 1, "repository": provenance.get("repository"), "revision": provenance.get("revision"),
            "tree": provenance.get("tree"), "subpath": provenance.get("subpath"), "source": source,
            "definitions": rows}
