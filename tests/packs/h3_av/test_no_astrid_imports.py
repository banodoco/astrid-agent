"""h3_av's kept modules import nothing from Astrid (Plan 32 T6b-3, item 5).

The engine hands a hook what it needs (``shared/engine_facts.py``). Only a stage's
``actions/*/run.py`` shell, the CLI and the legacy ``actions/transform`` parent (retiring
with the action entry) may import ``astrid.*``.
"""
import ast
from pathlib import Path

import pytest

PACK = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "h3_av"
EXEMPT_DIRS = {"transform", "__pycache__"}


def _checked():
    for path in sorted(PACK.rglob("*.py")):
        relative = path.relative_to(PACK)
        if relative.parts[0] == "workflows":  # generated VibeComfy bundles
            continue
        if path.name == "run.py" and relative.parts[0] == "actions":
            continue
        if relative.parts == ("cli.py",) or (len(relative.parts) > 2 and relative.parts[1] in EXEMPT_DIRS):
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
