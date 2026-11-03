#!/usr/bin/env python3
"""Generate the trusted whole-Tool catalog consumed by the host build."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from astrid.core.pack import PackValidationError, discover_packs  # noqa: E402
from astrid.core.pack.canonical import (  # noqa: E402
    CanonicalPackValidationError,
    ToolEntryProjection,
    validate_canonical_pack,
)
from astrid.core.pack.definition import PackDefinition  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "astrid" / "tools" / "catalog.json"
CATALOG_SCHEMA_VERSION = 1


def canonical_json_bytes(value: Any) -> bytes:
    """Encode canonical JSON for parity with the host's UTF-8 digest rule."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _entry_contract(tool: ToolEntryProjection) -> dict[str, Any]:
    if tool.target != "astrid" or dict(tool.compatibility) != {"host": "1"}:
        raise PackValidationError(
            f"{tool.canonical_id}: current Tool projection requires target: astrid and compatibility: {{host: '1'}}"
        )
    if tool.entry.file_kind != "file" or tool.entry.resolved.suffix != ".json":
        raise PackValidationError(
            f"{tool.canonical_id}: host Tool entry must be a declared JSON descriptor"
        )
    try:
        value = json.loads(tool.entry.resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackValidationError(f"{tool.canonical_id}: unreadable Tool entry: {exc}") from exc
    expected = {
        "schema_version": 1,
        "tool_id": tool.declaration_key,
        "host_entry": tool.declaration_key,
    }
    if value != expected:
        raise PackValidationError(
            f"{tool.canonical_id}: Tool entry must be exactly {expected!r}; got {value!r}"
        )
    return value


def _descriptor(tool: ToolEntryProjection) -> dict[str, Any]:
    entry_contract = _entry_contract(tool)
    descriptor: dict[str, Any] = {
        "id": tool.declaration_key,
        "canonical_id": tool.canonical_id,
        "pack_id": tool.pack_id,
        "pack_version": tool.pack_version,
        "target": tool.target,
        "compatibility": dict(tool.compatibility),
        "manifest": {"path": "pack.yaml", "sha256": tool.manifest.sha256},
        "entry": {
            "path": tool.entry.path,
            "sha256": tool.entry.sha256,
            "size": tool.entry.size,
            "host_entry": entry_contract["host_entry"],
        },
        "resources": [
            {"path": handle.path, "kind": kind, "sha256": handle.sha256, "size": handle.size}
            for handle, kind in zip(tool.resources, tool.resource_kinds, strict=True)
        ],
        "dependencies": {key: list(values) for key, values in tool.dependencies.items()},
    }
    descriptor["release_sha256"] = hashlib.sha256(canonical_json_bytes(descriptor)).hexdigest()
    return descriptor


def _tool_rows(pack: PackDefinition) -> tuple[dict[str, Any], ...]:
    if str(pack.schema_version) != "3" or not pack.ui:
        return ()
    try:
        canonical = validate_canonical_pack(pack.root, expected_pack_id=pack.id)
    except CanonicalPackValidationError as exc:
        raise PackValidationError(f"{pack.manifest_path}: Tool projection: {exc}") from exc
    return tuple(_descriptor(tool) for tool in canonical.tool_entry_projections())


def generate(*, selected_pack_ids=None) -> str:
    tools: list[dict[str, Any]] = []
    selected = None if selected_pack_ids is None else set(selected_pack_ids)
    for pack in discover_packs():
        if selected is not None and pack.id not in selected:
            continue
        tools.extend(_tool_rows(pack))
    tools.sort(key=lambda item: (item["id"], item["canonical_id"]))
    seen_ids: set[str] = set()
    for tool in tools:
        if tool["id"] in seen_ids:
            raise PackValidationError(f"duplicate host Tool ID {tool['id']!r}")
        seen_ids.add(tool["id"])
    return json.dumps(
        {"schema_version": CATALOG_SCHEMA_VERSION, "tools": tools},
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail when the catalog is stale")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = args.output.resolve()
    try:
        expected = generate()
    except (PackValidationError, OSError) as exc:
        print(f"Tool catalog generation failed: {exc}", file=sys.stderr)
        return 1
    actual = output.read_text(encoding="utf-8") if output.is_file() else None
    if args.check:
        if actual != expected:
            print(f"{output} is stale; run scripts/gen_tool_catalog.py", file=sys.stderr)
            return 1
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
