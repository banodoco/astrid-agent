"""h3_av's kept modules import nothing from Astrid (Plan 32 T6b-3, item 5).

The engine hands a hook what it needs (``shared/engine_facts.py``). Only the CLI may
import ``astrid.*``.
"""
import ast
from pathlib import Path

import pytest

PACK = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "h3_av"


def _checked():
    for path in sorted(PACK.rglob("*.py")):
        relative = path.relative_to(PACK)
        if relative.parts[0] in {"workflows", "__pycache__"}:  # generated VibeComfy bundles
            continue
        if relative.parts == ("cli.py",):
            continue
        yield path


def _astrid_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        for name in names:
            if name == "astrid" or name.startswith("astrid."):
                yield node.lineno, name


@pytest.mark.parametrize("path", list(_checked()), ids=lambda p: str(p.relative_to(PACK)))
def test_module_imports_nothing_from_astrid(path):
    assert list(_astrid_imports(path)) == []


def test_the_rule_sees_the_hooks_module():
    assert any(p.name == "hooks.py" for p in _checked())
