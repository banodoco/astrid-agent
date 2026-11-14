"""Core engine facts must not depend on a pack implementation."""

import ast
from pathlib import Path

from astrid.core.generation import engine_facts


def test_core_engine_facts_imports_no_packs() -> None:
    tree = ast.parse(Path(engine_facts.__file__).read_text(encoding="utf-8"))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module or "")
            imports.extend(f"{node.module}.{alias.name}" for alias in node.names)
    assert not [name for name in imports if name == "astrid.packs" or name.startswith("astrid.packs.")]
