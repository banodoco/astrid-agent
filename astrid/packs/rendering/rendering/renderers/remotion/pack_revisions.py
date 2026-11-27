"""Report the source identities of the elements selected for a render."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

from astrid.core.foundation.paths import PACKAGE_ROOT
from astrid.core.pack.identity import _EXCLUDED_DIRS, pack_digest, read_pin
from astrid.core.pack.motion_source import RESOURCE, read_release_source, source_file_count

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_TREE_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MOTION_PACK = "astrid_motion"
_MOTION_TREE_KEY = "astrid_motion_tree"


def _motion_projection_witness() -> Mapping[str, Any] | None:
    """Read the independent product resource, never a collection or PIN claim.

    PACKAGE_ROOT is anchored in Agent's core, including when the selected
    rendering pack is captured into an attempt-local source snapshot.
    """
    resources = PACKAGE_ROOT / "resources"
    path = resources / "astrid_motion_projection.json"
    try:
        if resources.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return None
    if not isinstance(record, dict) or set(record) != {
        "schema", "pack_id", "repo", "upstream_commit", "upstream_subpath",
        "upstream_tree", "digest_rule", "upstream_digest", "alias_only_digest",
        "tree_digest", "file_count",
    }:
        return None
    if (
        record["schema"] != "astrid-motion-projection/v1"
        or record["pack_id"] != _MOTION_PACK
        or record["repo"] != "https://github.com/banodoco/astrid-agent"
        or record["upstream_subpath"] != "astrid/packs/astrid_motion"
        or record["digest_rule"] != "pack-tree-v1"
        or type(record["file_count"]) is not int
        or record["file_count"] <= 0
        or not all(isinstance(record[key], str) and _COMMIT.fullmatch(record[key])
                   for key in ("upstream_commit", "upstream_tree"))
        or not all(isinstance(record[key], str) and _TREE_DIGEST.fullmatch(record[key])
                   for key in ("upstream_digest", "alias_only_digest", "tree_digest"))
    ):
        return None
    return record


def _motion_projection_row(
    editor: Mapping[str, str], observed: str | None,
    witness: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compare content identities; attach upstream provenance only if witnessed."""
    expected = editor.get(_MOTION_TREE_KEY)
    upstream = editor.get(_MOTION_PACK)
    verified = witness is not None and observed == witness["tree_digest"]
    row: dict[str, Any] = {
        "pack_id": _MOTION_PACK,
        "editor_revision": expected,
        "resolved_revision": observed,
        "derived_from": None,
        "upstream_revision": witness["upstream_commit"] if verified and witness is not None else None,
        "editor_upstream_revision": upstream,
        "digest_rule": "pack-tree-v1",
        "identity_verification": (
            "agent projection witness and source digest verified" if verified
            else "unknown" if observed is not None else "unavailable"
        ),
    }
    if expected is None and upstream is None:
        row["comparison"] = "editor revision not supplied"
    elif not isinstance(expected, str) or not _TREE_DIGEST.fullmatch(expected):
        # A commit is provenance, not a fallback identity for the projection.
        row["comparison"] = "unknown"
    elif observed is None:
        row["comparison"] = "resolved upstream revision unavailable"
    elif observed != expected:
        # The actual bytes differ even if their upstream provenance is unknown.
        row["comparison"] = "mismatch"
    else:
        # The request may claim any digest. Equality alone cannot authorize
        # an unreviewed projection or transfer its claimed upstream identity.
        row["comparison"] = "match" if verified else "unknown"
    return row


def _motion_release_source_witness() -> Mapping[str, Any] | None:
    """Read the additive witness from the running Agent, never a pack claim."""
    try:
        return read_release_source(PACKAGE_ROOT / "resources" / RESOURCE)
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return None


def _motion_source_row(
    editor: Mapping[str, str], observed: str | None,
    witness: Mapping[str, Any],
) -> dict[str, Any]:
    """Direct source match requires both witnessed bytes and upstream identity."""
    row = _motion_projection_row(editor, observed, witness)
    if row["upstream_revision"] is not None:
        row["identity_verification"] = "agent direct-source witness and source digest verified"
    if row["comparison"] == "match":
        upstream = editor.get(_MOTION_PACK)
        if not isinstance(upstream, str) or not _COMMIT.fullmatch(upstream):
            row["comparison"] = "unknown"
        elif upstream != witness["upstream_commit"]:
            row["comparison"] = "mismatch"
    return row


def _pack_root(element_root: Path, pack_id: str) -> Path | None:
    if not element_root.is_dir():
        return None
    for root in (element_root, *element_root.parents):
        manifest = root / "pack.yaml"
        if not manifest.is_file():
            continue
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError):
            return None
        return root if isinstance(data, dict) and data.get("id") == pack_id else None
    return None


def _installed_release_pins() -> Mapping[str, Any]:
    """Read the running installer version's manifest, never the selected pack's claims.

    The installer writes this manifest before creating its version-specific py
    environment. Bind to the running interpreter, not a mutable current pointer.
    Source runs without an installed version have no release-pin authority.
    """
    program_root = os.environ.get("ASTRID_INSTALL_PROGRAM_ROOT")
    if not program_root:
        return {}
    try:
        prefix = Path(sys.prefix).resolve(strict=True)
        versions = (Path(program_root) / "versions").resolve(strict=True)
        if prefix.name != "py" or prefix.parent.parent != versions:
            return {}
        manifest_path = prefix.parent / "manifest.json"
        if manifest_path.is_symlink():
            return {}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        pins = manifest.get("packs") if isinstance(manifest, dict) else None
        return pins if isinstance(pins, dict) else {}
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return {}


def _matches_release_pin(pin: Mapping[str, Any], expected: Any) -> bool:
    return isinstance(expected, Mapping) and set(expected) == {
        "repo", "commit", "digest_rule", "digest"
    } and all(
        pin[key] == expected[expected_key]
        for key, expected_key in (
            ("source", "repo"), ("commit", "commit"),
            ("digest_rule", "digest_rule"), ("digest", "digest"),
        )
    )


def resolved_pack_revisions(stage: Mapping[str, Any], editor: Mapping[str, str] | None) -> list[dict[str, Any]]:
    """Use selected element roots, never a second pack discovery, for the report."""
    selected: set[tuple[str, str]] = set()
    for kind in ("effects", "animations", "transitions"):
        for item in stage.get(kind, []):
            if isinstance(item, Mapping) and item.get("source_pack_id") and item.get("element_root"):
                selected.add((str(item["source_pack_id"]), str(item["element_root"])))
    roots: dict[str, set[Path | None]] = {}
    for pack_id, element_root in selected:
        root = _pack_root(Path(element_root), pack_id)
        roots.setdefault(pack_id, set()).add(root)
    release_pins = _installed_release_pins()
    editor_revisions = editor or {}
    editor_pack_ids = set(editor_revisions) - {_MOTION_TREE_KEY}
    projection_requested = _MOTION_TREE_KEY in editor_revisions
    if projection_requested:
        editor_pack_ids.add(_MOTION_PACK)
    rows = []
    for pack_id in sorted(set(roots) | editor_pack_ids):
        pack_roots = roots.get(pack_id, set())
        root = next(iter(pack_roots)) if len(pack_roots) == 1 else None
        expected = editor_revisions.get(pack_id) if pack_id != _MOTION_TREE_KEY else None
        motion_digest = None
        if pack_id == _MOTION_PACK:
            legacy = _motion_projection_witness()
            direct = _motion_release_source_witness()
            motion_count = None
            if root is not None:
                try:
                    motion_digest = pack_digest(root)
                    if direct is not None and motion_digest == direct["tree_digest"]:
                        motion_count = source_file_count(root, _EXCLUDED_DIRS)
                except (OSError, UnicodeError, ValueError, RuntimeError):
                    pass
            candidates = [record for record in (legacy, direct)
                          if record is not None and motion_digest == record["tree_digest"]]
            # A digest shared by distinct witness profiles has ambiguous
            # provenance. Do not select whichever happens to be loaded first.
            witness = candidates[0] if len(candidates) == 1 else None
            if witness is direct and direct is not None and motion_count != direct["file_count"]:
                witness = None
            if direct is not None and (
                editor_revisions.get(_MOTION_PACK) == direct["upstream_commit"]
                or editor_revisions.get(_MOTION_TREE_KEY) == direct["tree_digest"]
            ) and witness is not direct:
                # A request identifying the new source cannot borrow the
                # legacy profile's deliberately content-only comparison.
                witness = None
            if projection_requested or candidates:
                if witness is direct and direct is not None:
                    rows.append(_motion_source_row(editor_revisions, motion_digest, direct))
                else:
                    rows.append(_motion_projection_row(editor_revisions, motion_digest, witness))
                continue
        row: dict[str, Any] = {"pack_id": pack_id, "editor_revision": expected,
                               "resolved_revision": None, "derived_from": None,
                               "upstream_revision": None,
                               "identity_verification": "unavailable"}
        if root is not None:
            try:
                pin = read_pin(root / "PIN")
                if pin["commit"] is not None and (
                    motion_digest if pack_id == _MOTION_PACK else pack_digest(root)
                ) == pin["digest"]:
                    row["resolved_revision"] = pin["commit"]
                    row["identity_verification"] = "unknown"
                    if _matches_release_pin(pin, release_pins.get(pack_id)):
                        # An independently pinned upstream Agent tree is already
                        # the editor's revision namespace. No derivative declaration
                        # is required or fabricated for the direct upstream source.
                        if pin["source"] in {
                            "https://github.com/banodoco/astrid-agent",
                            "https://github.com/banodoco/astrid-agent.git",
                        }:
                            row["upstream_revision"] = pin["commit"]
                            row["identity_verification"] = "release pin and source digest verified"
                        else:
                            provenance = json.loads((root / "render-provenance.json").read_text(encoding="utf-8"))
                            derived = provenance.get("derived_from") if isinstance(provenance, dict) else None
                            if isinstance(derived, str) and _COMMIT.fullmatch(derived):
                                row["derived_from"] = derived
                                row["upstream_revision"] = derived
                                row["identity_verification"] = "release pin and source digest verified"
            except (OSError, UnicodeError, ValueError):
                pass
        if expected is None:
            row["comparison"] = "editor revision not supplied"
        elif row["identity_verification"] == "unknown":
            row["comparison"] = "unknown"
        elif row["upstream_revision"] is None:
            row["comparison"] = "resolved upstream revision unavailable"
        elif row["upstream_revision"] == expected:
            row["comparison"] = "match"
        else:
            row["comparison"] = "mismatch"
        rows.append(row)
    return rows
