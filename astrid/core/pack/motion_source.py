"""Strict, additive witness for the reviewed direct motion source.

This stdlib-only leaf is shared by the renderer and the existing offline
projection generator. The record is an Agent release resource, never a claim
read from a selected pack, collection, request, or PIN.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

SCHEMA = "astrid-motion-release-source/v1"
RESOURCE = "astrid_motion_release_source.json"
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FIELDS = {
    "schema", "pack_id", "repo", "upstream_commit", "upstream_subpath",
    "upstream_tree", "digest_rule", "upstream_digest", "tree_digest",
    "file_count", "transform",
}


def validate_release_source(record: Any) -> dict[str, Any]:
    """Validate the complete direct-source profile; no legacy coercion."""
    if not isinstance(record, dict) or set(record) != _FIELDS:
        raise ValueError("Direct motion witness has unexpected fields")
    if (
        record["schema"] != SCHEMA
        or record["pack_id"] != "astrid_motion"
        or record["repo"] != "https://github.com/banodoco/poms_packs"
        or record["upstream_subpath"] != "astrid_motion"
        or record["digest_rule"] != "pack-tree-v1"
        or record["transform"] != "identity"
        or type(record["file_count"]) is not int
        or record["file_count"] <= 0
        or not all(isinstance(record[key], str) and _COMMIT.fullmatch(record[key])
                   for key in ("upstream_commit", "upstream_tree"))
        or not all(isinstance(record[key], str) and _DIGEST.fullmatch(record[key])
                   for key in ("upstream_digest", "tree_digest"))
        or record["upstream_digest"] != record["tree_digest"]
    ):
        raise ValueError("Invalid direct motion source witness")
    return dict(record)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"Duplicate direct motion witness field: {key}")
        record[key] = value
    return record


def read_release_source(path: Path) -> dict[str, Any]:
    """Read only a regular independent resource, refusing duplicate JSON keys."""
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("Direct motion witness must be an independent regular file")
    return validate_release_source(json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs,
    ))


def source_file_count(root: Path, excluded_dirs: set[str]) -> int:
    """Count canonical files after pack_digest has validated the source tree.

    Use the digest implementation's directory exclusions, not a new content
    identity. PIN, Git metadata and bytecode retain their canonical exclusions.
    """
    def failed(error: OSError) -> None:
        raise error

    count = 0
    for directory, dirs, files in os.walk(root, onerror=failed, followlinks=False):
        dirs[:] = [name for name in dirs if name not in excluded_dirs]
        count += sum(
            name != ".git" and not (Path(directory) == root and name == "PIN")
            and not name.endswith((".pyc", ".pyo"))
            for name in files
        )
    return count
