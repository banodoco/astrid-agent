"""Static, read-only import policy checks for packs.

The checker deliberately parses source instead of importing it.  Callers pass
the frozen public surfaces and dependency metadata they already selected.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class ImportViolation:
    path: str
    line: int
    module: str
    code: str
    fix: str


@dataclass(frozen=True)
class ImportContract:
    """Immutable allow-list snapshot for one validation run.

    ``sdk_names`` are importable names below ``astrid.sdk``; ``core_pack_apis``
    maps core pack ids to their public module (usually ``astrid.packs.ID.api``).
    ``python_modules`` maps import roots to declared Python requirement names,
    allowing distributions whose import name differs from their PyPI name.
    """

    facade_exports: frozenset[str] = frozenset()
    facade_version: tuple[int, int] = (1, 0)
    sdk_names: frozenset[str] = frozenset()
    core_pack_apis: Mapping[str, str] = field(default_factory=dict)
    python_modules: Mapping[str, str] = field(default_factory=dict)


def _requirements(manifest: Mapping[str, object]) -> set[str]:
    deps = manifest.get("dependencies", {})
    if not isinstance(deps, Mapping):
        return set()
    raw = deps.get("python_requirements", ())
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return set()
    result: set[str] = set()
    for item in raw:
        if isinstance(item, str) and item.strip():
            # PEP 508 requirement: keep the normalized distribution name.
            name = item.split(";", 1)[0].strip()
            for sep in ("<", ">", "=", "!", "~", "[", " "):
                name = name.split(sep, 1)[0]
            result.add(name.lower().replace("_", "-").replace(".", "-"))
    return result


def _classify(module: str, *, level: int, pack_id: str, contract: ImportContract,
              declared_packs: set[str], declared_python: set[str]) -> tuple[str, str] | None:
    if level or module.startswith("."):
        return None  # Relative imports remain within the pack tree.
    if not module:
        return "dynamic_import", "Use a literal, policy-approved import."
    root = module.split(".", 1)[0]
    if root in sys.stdlib_module_names:
        return None
    if module == "astrid.pack_api":
        return None
    if module.startswith("astrid.pack_api."):
        return "deep_import", "Import public names from astrid.pack_api."
    if module == "astrid.sdk" or module.startswith("astrid.sdk."):
        suffix = module.removeprefix("astrid.sdk").lstrip(".")
        if suffix and (suffix in contract.sdk_names or module in contract.sdk_names):
            return None
        if not suffix and contract.sdk_names:
            return None
        return "deep_import", "Use a frozen public SDK import."
    if module.startswith("astrid.packs."):
        parts = module.split(".")
        if len(parts) >= 3 and parts[2] == pack_id:
            return None
        if len(parts) >= 4 and parts[-1] == "api":
            dep_id = parts[2]
            allowed = contract.core_pack_apis.get(dep_id) == module
            allowed = allowed or (dep_id in declared_packs and module == f"astrid.packs.{dep_id}.api")
            if allowed:
                return None
            if dep_id not in declared_packs:
                return "undeclared_pack", "Declare this pack in dependencies.packs."
            return "deep_import", "Import only the declared pack's public api module."
        if len(parts) >= 3 and parts[2] not in declared_packs:
            return "undeclared_pack", "Declare this pack in dependencies.packs."
        return "deep_import", "Import only a declared pack's public api module."
    if module == "astrid" or module.startswith("astrid."):
        return "deep_import", "Import public core names from astrid.pack_api."
    mapped = contract.python_modules.get(root)
    if mapped:
        normalized = mapped.lower().replace("_", "-").replace(".", "-")
        if normalized in declared_python:
            return None
    normalized_root = root.lower().replace("_", "-").replace(".", "-")
    if normalized_root in declared_python:
        return None
    return "undeclared_python", "Declare this Python package in dependencies.python_requirements."


def check_pack_imports(pack_dir: Path, *, contract: ImportContract,
                       manifest: Mapping[str, object]) -> list[ImportViolation]:
    """Return deterministic import violations without importing pack code."""
    pack_dir = Path(pack_dir)
    pack_id = manifest.get("id", pack_dir.name)
    pack_id = pack_id if isinstance(pack_id, str) else pack_dir.name
    if pack_id == "personal":
        return []
    deps = manifest.get("dependencies", {})
    raw_packs = deps.get("packs", ()) if isinstance(deps, Mapping) else ()
    declared_packs = set(raw_packs) if isinstance(raw_packs, (list, tuple, set, frozenset)) else set()
    declared_python = _requirements(manifest)
    violations: list[ImportViolation] = []

    def inspect_module(path: Path, line: int, module: str, level: int = 0,
                       symbols: tuple[str, ...] = ()) -> None:
        classification = _classify(module, level=level, pack_id=pack_id,
                                    contract=contract, declared_packs=declared_packs,
                                    declared_python=declared_python)
        if classification is None and module == "astrid.pack_api" and symbols:
            unknown = sorted(set(symbols) - contract.facade_exports - {"VERSION"})
            if unknown:
                module = f"{module}.{unknown[0]}"
                classification = ("deep_import", "Use a name present in the frozen astrid.pack_api surface.")
        if classification is None and module == "astrid.sdk" and symbols:
            unknown = sorted(set(symbols) - contract.sdk_names)
            if unknown:
                module = f"{module}.{unknown[0]}"
                classification = ("deep_import", "Use a name present in the frozen public SDK surface.")
        if classification:
            code, fix = classification
            violations.append(ImportViolation(path.relative_to(pack_dir).as_posix(), line,
                                              module, code, fix))

    files = sorted(pack_dir.rglob("*.py"))
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            line = getattr(exc, "lineno", None) or 1
            violations.append(ImportViolation(path.relative_to(pack_dir).as_posix(), line,
                                              "", "syntax_error", "Fix the Python source so it parses."))
            continue
        importlib_names = {"importlib"}
        dynamic_names = {"__import__", "import_module"}
        for imported in ast.walk(tree):
            if isinstance(imported, ast.Import):
                for alias in imported.names:
                    if alias.name == "importlib":
                        importlib_names.add(alias.asname or "importlib")
            elif isinstance(imported, ast.ImportFrom):
                if imported.module == "importlib":
                    dynamic_names.update(alias.asname or alias.name
                                         for alias in imported.names if alias.name == "import_module")
                elif imported.module == "builtins":
                    dynamic_names.update(alias.asname or alias.name
                                         for alias in imported.names if alias.name == "__import__")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    inspect_module(path, node.lineno, alias.name)
            elif isinstance(node, ast.ImportFrom):
                inspect_module(path, node.lineno, node.module or "", node.level,
                               tuple(alias.name for alias in node.names))
            elif isinstance(node, ast.Call):
                is_dynamic_import = False
                if isinstance(node.func, ast.Name):
                    if node.func.id in dynamic_names:
                        is_dynamic_import = True
                elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    if ((node.func.value.id in importlib_names and node.func.attr == "import_module") or
                            (node.func.value.id == "builtins" and node.func.attr == "__import__")):
                        is_dynamic_import = True
                if is_dynamic_import:
                    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        inspect_module(path, node.lineno, node.args[0].value)
                    else:
                        inspect_module(path, node.lineno, "", 0)
    return sorted(violations, key=lambda item: (item.path, item.line, item.module, item.code))
