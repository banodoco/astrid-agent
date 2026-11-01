#!/usr/bin/env python3
"""Shrink-only AST ratchet for pack coupling in core-facing Python code."""

from __future__ import annotations

import ast
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "reshape" / "baselines" / "core_pack_boundary.json"
SCAN_ROOTS = (Path("astrid/core"), Path("astrid/sdk"))
PACKS_ROOT = ROOT / "astrid" / "packs"


def _pack_inventory() -> tuple[set[str], set[str]]:
    """Read pack ids and action capability ids from first-party manifests."""
    import yaml

    pack_names: set[str] = set()
    capability_ids: set[str] = set()
    for manifest in sorted(PACKS_ROOT.glob("*/pack.yaml")):
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        pack_id = data.get("id")
        if not isinstance(pack_id, str) or not pack_id:
            continue
        pack_names.add(pack_id)
        actions = data.get("capabilities", [])
        if isinstance(actions, list):
            capability_ids.update(f"{pack_id}.{action}" for action in actions if isinstance(action, str))
    return pack_names, capability_ids


def _module_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        return ",".join(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.module == "astrid" and any(alias.name == "packs" for alias in node.names):
            return "astrid.packs"
        return "." * node.level + (node.module or "")
    return None


def _pack_name_hits(value: str, pack_names: set[str]) -> set[str]:
    hits = set()
    for name in pack_names:
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", value):
            hits.add(name)
    return hits


def _is_repo_pack_path(value: str, pack_names: set[str]) -> bool:
    normalized = value.replace("\\", "/")
    return any(
        re.search(rf"(?:^|/)(?:astrid/)?packs/{re.escape(name)}(?:/|$)", normalized)
        for name in pack_names
    )


def scan_source(path: str, source: str, pack_names: set[str], capability_ids: set[str]) -> Counter:
    """Return normalized findings with multiplicity; line numbers are deliberately omitted."""
    tree = ast.parse(source, filename=path)
    findings: Counter = Counter()
    for node in ast.walk(tree):
        module = _module_name(node)
        if module and (module.startswith("astrid.packs.") or module == "astrid.packs"):
            findings[(path, "pack_import", module)] += 1
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value in capability_ids:
                findings[(path, "capability_id", value)] += 1
            if value.startswith("astrid.packs."):
                findings[(path, "dynamic_pack_import", value)] += 1
            if _is_repo_pack_path(value, pack_names):
                findings[(path, "repo_pack_path", value)] += 1
            for pack_name in _pack_name_hits(value, pack_names):
                findings[(path, "pack_name_literal", pack_name)] += 1
    return findings


def collect_findings(root: Path = ROOT) -> Counter:
    pack_names, capability_ids = _pack_inventory()
    findings: Counter = Counter()
    files: list[Path] = []
    for relative in SCAN_ROOTS:
        files.extend((root / relative).rglob("*.py"))
    # Root-level facade modules are shared production surfaces too.
    files.extend((root / "astrid").glob("*.py"))
    for source_path in sorted(set(files)):
        relative_path = source_path.relative_to(root).as_posix()
        findings.update(scan_source(relative_path, source_path.read_text(encoding="utf-8"), pack_names, capability_ids))
    return findings


def _serialized(findings: Counter) -> list[dict[str, object]]:
    return [
        {"file": key[0], "kind": key[1], "value": key[2], "count": count}
        for key, count in sorted(findings.items())
    ]


def check_baseline(findings: Counter, baseline_path: Path = BASELINE) -> list[str]:
    baseline_data = json.loads(baseline_path.read_text(encoding="utf-8"))
    allowed = Counter()
    for row in baseline_data["findings"]:
        allowed[(row["file"], row["kind"], row["value"])] = int(row["count"])
    added = findings - allowed
    return [f"{key[0]}: {key[1]} {key[2]!r} x{count}" for key, count in sorted(added.items())]


def main(argv: Iterable[str] = sys.argv[1:]) -> int:
    args = list(argv)
    findings = collect_findings()
    if args == ["--write-baseline"]:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps({"schema": 1, "findings": _serialized(findings)}, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {sum(findings.values())} normalized findings to {BASELINE.relative_to(ROOT)}")
        return 0
    if args:
        print("usage: python scripts/check_core_pack_boundary.py [--write-baseline]", file=sys.stderr)
        return 2
    violations = check_baseline(findings)
    if violations:
        print("core pack-boundary ratchet grew:", file=sys.stderr)
        print("\n".join(f"  {item}" for item in violations), file=sys.stderr)
        return 1
    print(f"core pack-boundary ratchet passed ({sum(findings.values())} findings; baseline only shrinks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
