"""Report the source identities of the elements selected for a render."""

from __future__ import annotations

import json
import re
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


def resolved_pack_revisions(stage: Mapping[str, Any], editor: Mapping[str, str] | None) -> list[dict[str, Any]]:
    """Use selected element roots, never a second pack discovery, for the report."""
    selected: set[tuple[str, str]] = set()
    for kind in ("effects", "animations", "transitions"):
        for item in stage.get(kind, []):
            if isinstance(item, Mapping) and item.get("source_pack_id") and item.get("element_root"):
                selected.add((str(item["source_pack_id"]), str(item["element_root"])))
    roots: dict[str, set[Path]] = {}
    for pack_id, element_root in selected:
        root = _pack_root(Path(element_root), pack_id)
        if root is not None:
            roots.setdefault(pack_id, set()).add(root)
        else:
            roots.setdefault(pack_id, set())
    rows = []
    for pack_id in sorted(set(roots) | set(editor or {})):
        pack_roots = roots.get(pack_id, set())
        expected = (editor or {}).get(pack_id)
        row: dict[str, Any] = {"pack_id": pack_id, "editor_revision": expected,
                               "resolved_revision": None, "derived_from": None,
                               "identity_verification": "unavailable"}
        if len(pack_roots) == 1:
            root = next(iter(pack_roots))
            try:
                pin = read_pin(root / "PIN")
                if pin["commit"] is not None and pack_digest(root) == pin["digest"]:
                    row["resolved_revision"] = pin["commit"]
                    row["identity_verification"] = "pin digest matches; upstream is a declaration"
                    provenance = json.loads((root / "render-provenance.json").read_text(encoding="utf-8"))
                    derived = provenance.get("derived_from") if isinstance(provenance, dict) else None
                    if isinstance(derived, str) and _COMMIT.fullmatch(derived):
                        row["derived_from"] = derived
            except (OSError, UnicodeError, ValueError):
                pass
        if expected is None:
            row["comparison"] = "editor revision not supplied"
        elif row["derived_from"] is None:
            row["comparison"] = "resolved upstream revision unavailable"
        elif row["derived_from"] == expected:
            row["comparison"] = "match"
        else:
            row["comparison"] = "mismatch"
        rows.append(row)
    return rows
