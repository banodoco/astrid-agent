"""Report the source identities of the elements selected for a render."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml

from astrid.core.pack.identity import pack_digest, read_pin

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def _pack_root(element_root: Path, pack_id: str) -> Path | None:
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
    rows = []
    for pack_id in sorted(set(roots) | set(editor or {})):
        pack_roots = roots.get(pack_id, set())
        expected = (editor or {}).get(pack_id)
        row: dict[str, Any] = {"pack_id": pack_id, "editor_revision": expected,
                               "resolved_revision": None, "derived_from": None,
                               "identity_verification": "unavailable"}
        if len(pack_roots) == 1 and None not in pack_roots:
            root = next(iter(pack_roots))
            assert root is not None
            try:
                pin = read_pin(root / "PIN")
                if pin["commit"] is not None and pack_digest(root) == pin["digest"]:
                    row["resolved_revision"] = pin["commit"]
                    row["identity_verification"] = "unknown"
                    if _matches_release_pin(pin, release_pins.get(pack_id)):
                        provenance = json.loads((root / "render-provenance.json").read_text(encoding="utf-8"))
                        derived = provenance.get("derived_from") if isinstance(provenance, dict) else None
                        if isinstance(derived, str) and _COMMIT.fullmatch(derived):
                            row["derived_from"] = derived
                            row["identity_verification"] = "release pin and source digest verified"
            except (OSError, UnicodeError, ValueError):
                pass
        if expected is None:
            row["comparison"] = "editor revision not supplied"
        elif row["identity_verification"] == "unknown":
            row["comparison"] = "unknown"
        elif row["derived_from"] is None:
            row["comparison"] = "resolved upstream revision unavailable"
        elif row["derived_from"] == expected:
            row["comparison"] = "match"
        else:
            row["comparison"] = "mismatch"
        rows.append(row)
    return rows
