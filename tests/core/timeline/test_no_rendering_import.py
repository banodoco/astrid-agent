"""PK-10 (new): core timeline is split from the rendering pack.

Core timeline code may be used by any pack; it must never import the
rendering pack, neither at import time nor lazily inside a function.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CORE_TIMELINE = ROOT / "astrid" / "core" / "timeline"
FORBIDDEN = "astrid.packs.rendering"


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append((node.lineno, node.module))
            if node.module in {"astrid.packs", "astrid"}:
                found.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in {
                "import_module", "__import__"} and node.args and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            found.append((node.lineno, node.args[0].value))
    return found


def _is_rendering(name: str) -> bool:
    return name == FORBIDDEN or name.startswith(FORBIDDEN + ".") or name == "astrid.packs.rendering"


def test_core_timeline_sources_name_no_rendering_import() -> None:
    sources = sorted(CORE_TIMELINE.rglob("*.py"))
    assert sources, "core timeline sources not found"
    offenders = [f"{path.relative_to(ROOT)}:{line} {name}"
                 for path in sources for line, name in _imports(path) if _is_rendering(name)]
    assert offenders == []


def test_importing_every_core_timeline_module_loads_no_rendering_module() -> None:
    probe = (
        "import importlib, json, pkgutil, sys\n"
        "import astrid.core.timeline as base\n"
        "failed = []\n"
        "for info in pkgutil.walk_packages(base.__path__, base.__name__ + '.'):\n"
        "    try:\n"
        "        importlib.import_module(info.name)\n"
        "    except Exception as exc:\n"
        "        failed.append([info.name, type(exc).__name__])\n"
        "loaded = sorted(n for n in sys.modules if n == 'astrid.packs.rendering' or n.startswith('astrid.packs.rendering.'))\n"
        "print(json.dumps({'loaded': loaded, 'failed': failed}))\n"
    )
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["loaded"] == []
    assert report["failed"] == []
