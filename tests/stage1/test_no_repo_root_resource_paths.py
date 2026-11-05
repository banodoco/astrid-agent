"""PK-12 (new): core never resolves pack resources from a repository root.

A pack's files are read from the selected root it was discovered in (or, for
core packs, from the selected ``astrid`` package, checkout or wheel), never by
joining a repository/checkout root with ``astrid/...``. This static scan
refuses ``<REPO_ROOT|_REPO_ROOT|ROOT> / "astrid" ...`` and
``Path(__file__).parents[N] / "astrid" ...`` path constructions in
``astrid/core``; use ``astrid.core.foundation.paths.PACKAGE_ROOT`` or
``astrid.core.foundation.pack_resources`` instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "astrid" / "core"
REPO_NAMES = {"REPO_ROOT", "_REPO_ROOT", "ROOT", "_ROOT", "CHECKOUT_ROOT", "TOOLS_DIR"}


def _chain(node: ast.AST) -> list[ast.AST]:
    """Flatten ``a / b / c`` into [a, b, c]."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _chain(node.left) + [node.right]
    return [node]


def _is_repo_base(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and node.id in REPO_NAMES:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path":
        return False
    # Path(__file__).resolve().parents[N] with N >= 3 climbs above the package.
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
        index = node.slice
        return isinstance(index, ast.Constant) and isinstance(index.value, int) and index.value >= 3
    return False


def _first_segment(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def test_core_builds_no_repository_root_astrid_resource_path() -> None:
    offenders = []
    for path in sorted(CORE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            parts = _chain(node)
            if len(parts) < 2 or not _is_repo_base(parts[0]):
                continue
            segment = _first_segment(parts[1])
            if segment is not None and (segment == "astrid" or segment.startswith("astrid/")):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert sorted(set(offenders)) == []
