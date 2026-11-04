#!/usr/bin/env python3
"""Mechanically move pack imports to the frozen Astrid pack APIs.

Run from any directory with ``python scripts/codemod_pack_api.py``.  Only
imports whose names and defining modules match ``astrid.pack_api`` exports are
moved.  Private symbols, same-name exports from another module, module imports,
and the explicitly identified core-pack imports are retained when they cannot
be represented by the public surface; the JSON report makes those follow-up
items reviewable.  A second run makes no changes.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "astrid" / "packs"
CORE_PACK_MODULES = {
    "astrid.packs.vibecomfy.asset_manifest": "astrid.packs.vibecomfy.api",
    "astrid.packs.rendering.actions.timeline_visualize.layers.base": "astrid.packs.rendering.api",
    "astrid.packs.rendering.shared.runtime_stitch": "astrid.packs.rendering.api",
}
EXCLUDED_PACKS = {"h3_av", "runpod"}


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    offsets.extend(i + 1 for i, char in enumerate(source) if char == "\n")
    return offsets


def _absolute(offsets: list[int], line: int, column: int) -> int:
    return offsets[line - 1] + column


def _statement_span(node: ast.ImportFrom, offsets: list[int]) -> tuple[int, int]:
    # Include the complete logical statement (including parenthesized imports),
    # while leaving the following newline and surrounding comments untouched.
    start = _absolute(offsets, node.lineno, node.col_offset)
    end = _absolute(offsets, node.end_lineno, node.end_col_offset)
    return start, end


def _format_import(module: str, names: list[ast.alias]) -> str:
    return ast.unparse(ast.ImportFrom(module=module, names=names, level=0))


def _indent_replacement(replacement: str, column: int) -> str:
    if column == 0:
        return replacement
    indent = " " * column
    return replacement.replace("\n", "\n" + indent)


def _pack_for(path: Path) -> str | None:
    try:
        return path.relative_to(PACKS).parts[0]
    except (ValueError, IndexError):
        return None


def _is_export_from(module: str, name: str, export_origins: dict[str, str]) -> bool:
    """Return whether a public name is exported from this defining module."""
    return export_origins.get(name) == module


def codemod(*, write: bool) -> dict[str, object]:
    from astrid import pack_api

    export_origins = pack_api._EXPORTS
    report: dict[str, object] = {
        "converted_statements": 0,
        "converted_names": 0,
        "converted_files": [],
        "unconvertible": [],
        "excluded_packs": sorted(EXCLUDED_PACKS),
    }
    changed_files: list[str] = []
    for path in sorted(PACKS.rglob("*.py")):
        pack_id = _pack_for(path)
        if pack_id is None or pack_id in EXCLUDED_PACKS:
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offsets = _line_offsets(source)
        edits: list[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    parts = module.split(".")
                    is_external_pack = (
                        len(parts) > 2 and parts[:2] == ["astrid", "packs"]
                        and parts[2] != pack_id
                    )
                    if module.startswith("astrid.core") or is_external_pack:
                        report["unconvertible"].append({
                            "path": path.relative_to(ROOT).as_posix(),
                            "line": node.lineno,
                            "module": module,
                            "names": [alias.asname or alias.name],
                        })
                continue
            if not isinstance(node, ast.ImportFrom) or node.level:
                continue
            module = node.module or ""
            converted: list[ast.alias] = []
            replacement_module: str | None = None
            unresolved: list[str] = []

            if module.startswith("astrid.core"):
                replacement_module = "astrid.pack_api"
                for alias in node.names:
                    if _is_export_from(module, alias.name, export_origins):
                        converted.append(alias)
                    else:
                        unresolved.append(alias.name)
            elif module in CORE_PACK_MODULES and not module.startswith(f"astrid.packs.{pack_id}."):
                replacement_module = CORE_PACK_MODULES[module]
                # The public api.py is the designated boundary.  Preserve the
                # import shape and report any name absent from that source API.
                api_path = ROOT / replacement_module.replace(".", "/")
                api_path = api_path.with_suffix(".py")
                api_tree = ast.parse(api_path.read_text(encoding="utf-8"), filename=str(api_path))
                api_names = {
                    alias.name
                    for item in ast.walk(api_tree)
                    if isinstance(item, ast.ImportFrom)
                    for alias in item.names
                }
                api_names.update(
                    target.id
                    for item in ast.walk(api_tree)
                    if isinstance(item, (ast.Assign, ast.AnnAssign))
                    for target in (item.targets if isinstance(item, ast.Assign) else [item.target])
                    if isinstance(target, ast.Name)
                )
                for alias in node.names:
                    if alias.name == "*" or alias.name in api_names:
                        converted.append(alias)
                    else:
                        unresolved.append(alias.name)
            elif module.startswith("astrid.packs."):
                parts = module.split(".")
                if len(parts) > 2 and parts[2] != pack_id:
                    report["unconvertible"].append({
                        "path": path.relative_to(ROOT).as_posix(),
                        "line": node.lineno,
                        "module": module,
                        "names": [alias.name for alias in node.names],
                    })
                continue
            else:
                continue

            if replacement_module is None:
                continue
            if unresolved:
                report["unconvertible"].append({
                    "path": path.relative_to(ROOT).as_posix(),
                    "line": node.lineno,
                    "module": module,
                    "names": unresolved,
                })
            if not converted:
                continue
            start, end = _statement_span(node, offsets)
            replacement = _format_import(replacement_module, converted)
            if unresolved:
                replacement += "\n" + _format_import(module, [a for a in node.names if a.name in unresolved])
            replacement = _indent_replacement(replacement, node.col_offset)
            if replacement != source[start:end]:
                edits.append((start, end, replacement))
                report["converted_statements"] = int(report["converted_statements"]) + 1
                report["converted_names"] = int(report["converted_names"]) + len(converted)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = node.func
            dynamic = (
                isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}
            ) or (
                isinstance(function, ast.Attribute) and function.attr == "import_module"
            )
            argument = node.args[0]
            if dynamic and isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                if argument.value.startswith("astrid.core") or argument.value.startswith("astrid.packs."):
                    report["unconvertible"].append({
                        "path": path.relative_to(ROOT).as_posix(),
                        "line": node.lineno,
                        "module": argument.value,
                        "names": ["<dynamic import>"],
                    })

        if edits:
            for start, end, replacement in sorted(edits, reverse=True):
                source = source[:start] + replacement + source[end:]
            ast.parse(source, filename=str(path))
            changed_files.append(path.relative_to(ROOT).as_posix())
            if write:
                path.write_text(source, encoding="utf-8")
    report["converted_files"] = changed_files
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report whether a conversion would change files")
    parser.add_argument("--report", type=Path, help="write the conversion report as JSON")
    args = parser.parse_args()
    report = codemod(write=not args.check)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if args.check and report["converted_statements"] else 0


if __name__ == "__main__":
    sys.exit(main())
