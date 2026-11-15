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
# Core packs ship with core; naming them is not a boundary finding (Plan 06 Decision 12).
CORE_PACKS = frozenset({"vibecomfy", "generation", "rendering", "timeline"})
# The pack-loading modules necessarily name the bare ``astrid.packs`` namespace.
# This list is explicit and pinned by tests/stage1/test_core_pack_boundary.py.
PACK_LOADER_MODULES = frozenset({
    "astrid/core/execution/generic_host_worker.py",
    "astrid/core/pack/install.py",
    "astrid/core/pack/loader.py",
    "astrid/core/pack/transaction.py",
    "astrid/pack_imports.py",
    "astrid/core/pack/imports.py",
})


def _pack_inventory(root: Path = ROOT) -> tuple[set[str], set[str]]:
    """Return first-party pack identities, even when their manifests are absent."""
    import yaml

    pack_names: set[str] = set()
    capability_ids: set[str] = set()
    packs_root = root / "astrid" / "packs"
    if not packs_root.is_dir():
        return pack_names, capability_ids
    # The package directory is the stable identity source. Manifests are optional
    # and can be temporarily missing while a pack is moved or reconstructed.
    pack_names.update(
        path.name for path in packs_root.iterdir()
        if path.is_dir() and not path.name.startswith("_")
    )
    for manifest in sorted(packs_root.glob("*/pack.yaml")):
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        pack_id = data.get("id")
        if not isinstance(pack_id, str) or not pack_id:
            continue
        pack_names.add(pack_id)
        capabilities = data.get("capabilities", [])
        if isinstance(capabilities, list):
            for capability in capabilities:
                if isinstance(capability, str):
                    capability_ids.add(f"{pack_id}.{capability}")
                elif isinstance(capability, dict) and isinstance(capability.get("id"), str):
                    capability_ids.add(f"{pack_id}.{capability['id']}")
        actions = data.get("actions", {})
        if isinstance(actions, dict):
            capability_ids.update(
                f"{pack_id}.{action}" for action in actions if isinstance(action, str)
            )
    return pack_names, capability_ids


def _core_vocabulary(root: Path = ROOT) -> set[str]:
    """Read core's declarations without importing runtime modules or their dependencies."""
    words: set[str] = set()
    for path, name in (
        ("astrid/core/generation/placement.py", "BUILTIN_KINDS"),
        ("astrid/omp_agent.py", "_PACK_ROUTE_BLOCKLIST"),
    ):
        tree = ast.parse((root / path).read_text(encoding="utf-8"), filename=path)
        assignment = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        )
        value = assignment.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "frozenset" and len(value.args) == 1 and not value.keywords:
            value = value.args[0]
        declared = ast.literal_eval(value)
        if not isinstance(declared, (tuple, list, set, frozenset)) or not all(isinstance(word, str) for word in declared):
            raise ValueError(f"{path}: {name} must be a collection of strings")
        words.update(declared)
    return words


def _module_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import):
        return ",".join(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.module == "astrid" and any(alias.name == "packs" for alias in node.names):
            return "astrid.packs"
        return "." * node.level + (node.module or "")
    return None


def _module_names(node: ast.AST) -> list[str]:
    """Return each imported module separately, including aliased mixed imports."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.module == "astrid" and any(alias.name == "packs" for alias in node.names):
            return ["astrid.packs"]
        module = "." * node.level + (node.module or "")
        return [module] if module else []
    return []


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


def _names_core_pack_module(module: str) -> bool:
    parts = module.split(".")
    return len(parts) > 2 and parts[:2] == ["astrid", "packs"] and parts[2] in CORE_PACKS


def _bare_namespace(path: str, module: str) -> bool:
    return module in {"astrid.packs", "astrid.packs."} and path in PACK_LOADER_MODULES


def scan_source(path: str, source: str, pack_names: set[str], capability_ids: set[str], core_vocabulary: set[str]) -> Counter:
    """Return normalized findings with multiplicity; line numbers are deliberately omitted.

    Rule (PK-3 guard rule): core-pack names are never findings; another pack's
    name is a finding only as an exact string value (not as a word inside prose
    or a dotted path); the bare ``astrid.packs`` namespace is exempt only in
    PACK_LOADER_MODULES.
    """
    tree = ast.parse(source, filename=path)
    findings: Counter = Counter()
    for node in ast.walk(tree):
        for module in _module_names(node):
            if module.startswith("astrid.packs.") or module == "astrid.packs":
                if _names_core_pack_module(module) or _bare_namespace(path, module):
                    continue
                findings[(path, "pack_import", module)] += 1
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if value in capability_ids and value.split(".", 1)[0] not in CORE_PACKS:
                findings[(path, "capability_id", value)] += 1
            if (value.startswith("astrid.packs.") and not _names_core_pack_module(value)
                    and not _bare_namespace(path, value)):
                findings[(path, "dynamic_pack_import", value)] += 1
            if _is_repo_pack_path(value, pack_names - CORE_PACKS):
                findings[(path, "repo_pack_path", value)] += 1
            if value in pack_names and value not in CORE_PACKS and value not in core_vocabulary:
                findings[(path, "pack_name_literal", value)] += 1
    return findings


def collect_findings(root: Path = ROOT) -> Counter:
    """Scan only the supplied checkout, including its pack inventory."""
    root = root.resolve()
    pack_names, capability_ids = _pack_inventory(root)
    findings: Counter = Counter()
    core_vocabulary = _core_vocabulary(root)
    files: list[Path] = []
    for relative in SCAN_ROOTS:
        scan_root = root / relative
        if scan_root.is_dir():
            files.extend(scan_root.rglob("*.py"))
    # Root-level facade modules are shared production surfaces too.
    facade_root = root / "astrid"
    if facade_root.is_dir():
        files.extend(facade_root.glob("*.py"))
    for source_path in sorted(set(files)):
        relative_path = source_path.relative_to(root).as_posix()
        findings.update(scan_source(relative_path, source_path.read_text(encoding="utf-8"), pack_names, capability_ids, core_vocabulary))
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
    if args == ["--check"]:
        args = []
    findings = collect_findings()
    if args == ["--write-baseline"]:
        violations = check_baseline(findings)
        if violations:
            print("refusing to grow core pack-boundary baseline:", file=sys.stderr)
            print("\n".join(f"  {item}" for item in violations), file=sys.stderr)
            return 1
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps({"schema": 1, "findings": _serialized(findings)}, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {sum(findings.values())} normalized findings to {BASELINE.relative_to(ROOT)}")
        return 0
    if args:
        print("usage: python scripts/check_core_pack_boundary.py [--check|--write-baseline]", file=sys.stderr)
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
