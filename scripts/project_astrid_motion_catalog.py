#!/usr/bin/env python3
"""Generate only the motion descriptors from a verified RN-5b-min pack."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACK_ID = "astrid_motion"
OPEN = "Object.freeze(\n"
CLOSE = "\n);\n"


def motion_rows(root: Path) -> list[dict]:
    # Import the actual product admission, registry and descriptor functions,
    # not a second implementation of the element fingerprint algorithm.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from astrid.core.element.catalog import _element_descriptor
    from astrid.core.element.registry import _load_pack_elements_with_diagnostics
    from astrid.core.pack import ELEMENT_KIND_REGISTRY, load_pack_manifest
    from scripts.project_astrid_motion import _check_digest, witness

    root = root.resolve(strict=True)
    _check_digest(root, witness()["tree_digest"], "projection")
    pack = load_pack_manifest(root / "pack.yaml", expected_pack_id=PACK_ID)
    elements, skipped = _load_pack_elements_with_diagnostics(
        (pack,), element_kind_registry=ELEMENT_KIND_REGISTRY
    )
    expected = {
        key for key, declaration in pack.rendering.items()
        if declaration["type"] == "element"
    }
    actual = {f"{item.kind}/{item.id}" for item in elements}
    if skipped or len(actual) != len(elements) or actual != expected:
        raise ValueError(f"Incomplete motion catalog: {sorted(expected - actual)}; {skipped}")
    rows = []
    for element in elements:
        descriptor = _element_descriptor(element)
        # The runtime deliberately suppresses checkout-relative module paths
        # for external packs. Here the verified source tree is being copied
        # verbatim into this exact browser-vendor module prefix.
        relative = element.component.resolve().relative_to(root).as_posix()
        descriptor["componentPath"] = f"packs/{PACK_ID}/{relative}"
        rows.append(descriptor)
    return rows


def replace_motion_rows(source: str, rows: list[dict]) -> str:
    """Replace only motion descriptor objects, preserving every other byte."""
    start = source.index(OPEN) + len(OPEN)
    end = source.index(CLOSE, start)
    payload = source[start:end]
    expected = {(row["kind"], row["id"]): row for row in rows}
    if len(expected) != len(rows) or not rows or any(row["packId"] != PACK_ID for row in rows):
        raise ValueError("Invalid or duplicate motion catalog rows")
    decoded = json.loads(payload)
    if not isinstance(decoded, list):
        raise ValueError("Expected the generated catalog array")
    decoder = json.JSONDecoder()
    replacements: list[tuple[int, int, str]] = []
    cursor = 1  # opening [
    found = set()
    for _ in decoded:
        while payload[cursor].isspace() or payload[cursor] == ",":
            cursor += 1
        row, row_end = decoder.raw_decode(payload, cursor)
        if row.get("packId") == PACK_ID:
            key = (row["kind"], row["id"])
            if key in found or key not in expected:
                raise ValueError(f"Unexpected or duplicate old motion row: {key}")
            found.add(key)
            replacement = json.dumps(
                expected[key], indent=2, sort_keys=True, ensure_ascii=False
            ).replace("\n", "\n  ")
            replacements.append((cursor, row_end, replacement))
        cursor = row_end
    if found != set(expected):
        raise ValueError(f"Motion catalog membership changed: {sorted(set(expected) - found)}")
    for row_start, row_end, replacement in reversed(replacements):
        payload = payload[:row_start] + replacement + payload[row_end:]
    return source[:start] + payload + source[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projection", required=True, type=Path)
    parser.add_argument("--source-catalog", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows = motion_rows(args.projection)
    source = args.source_catalog.read_bytes().decode("utf-8")
    result = replace_motion_rows(source, rows)
    if args.output.resolve().is_relative_to(args.projection.resolve()):
        raise ValueError("Catalog output must be outside the hashed projection")
    # This is a generated App input; refuse to overwrite a file or source.
    with args.output.open("xb") as stream:
        stream.write(result.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
