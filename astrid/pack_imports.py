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

# Distribution names differ from their top-level import packages.  These
# mappings describe ownership, not permission: the distribution must still be
# declared in the pack manifest.  Keep defaults deterministic across hosts.
_PYTHON_DISTRIBUTIONS = {"PIL": "Pillow", "yaml": "PyYAML"}
# Namespace distributions own a specific subtree, not the entire namespace.
_PYTHON_NAMESPACE_DISTRIBUTIONS = {"pyannote.audio": "pyannote-audio"}


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


# Names a pack-root module may never take: Astrid's own packages and the
# runtimes the host installs, whether or not they are present on this host.
_RESERVED_TOP_LEVEL = frozenset({"runtime_protocol", "vibecomfy"})


def _pack_module_path(pack_dir: Path, module: str) -> Path | None:
    """Return the pack-root package directory or module file *module* resolves to, or None.

    Walks every dotted component from the pack root the way Python's path
    finder does (a regular package directory before a ``.py`` module): each
    intermediate component must be a package, and every directory,
    ``__init__.py`` and module file on the way must have its real path (after
    resolving symlinks) under the pack root. A component that is missing, or
    that escapes the pack (such as ``pkg/sub`` symlinked outside), makes the
    import not pack-local, so no outside file is accepted unscanned.
    """
    parts = module.split(".")
    if not all(part.isidentifier() for part in parts):
        return None
    try:
        root = pack_dir.resolve(strict=True)
    except OSError:
        return None

    def inside(path: Path) -> bool:
        try:
            return path.resolve(strict=True).is_relative_to(root)
        except OSError:
            return False

    current = pack_dir
    for index, part in enumerate(parts):
        package = current / part
        init = package / "__init__.py"
        if package.is_dir() and init.is_file():
            if not (inside(package) and inside(init)):
                return None
            current = package
            continue
        if index == len(parts) - 1:
            module_file = current / f"{part}.py"
            return module_file if module_file.is_file() and inside(module_file) else None
        return None
    return current


def _host_can_import(name: str, *, pack_root: Path) -> bool:
    """True when the host interpreter could import top-level *name* without this pack.

    Asks every ``sys.meta_path`` finder (including editable-install finders),
    giving the path finder ``sys.path`` without the pack root. Nothing is
    imported. A pack-root module with such a name is a decoy: the installed
    package can load at run time instead of the scanned pack file.
    """
    if name in sys.modules or name in sys.builtin_module_names:
        return True
    try:
        root = pack_root.resolve(strict=True)
    except OSError:
        root = pack_root
    search_path = []
    for entry in sys.path:
        try:
            resolved = Path(entry or ".").resolve()
        except OSError:
            continue
        if resolved != root and not resolved.is_relative_to(root):
            search_path.append(entry)
    import importlib.machinery

    for finder in sys.meta_path:
        find_spec = getattr(finder, "find_spec", None)
        if find_spec is None:
            continue
        try:
            if finder is importlib.machinery.PathFinder or isinstance(finder, type) and issubclass(
                    finder, importlib.machinery.PathFinder):
                spec = find_spec(name, search_path)
            else:
                spec = find_spec(name, None)
        except Exception:  # noqa: BLE001 - a broken finder must not decide the policy
            continue
        if spec is not None:
            return True
    return False


def _shadowed_name(name: str, *, pack_root: Path) -> str | None:
    """Why a pack-root module named *name* may not be pack-private, if it may not."""
    if name in _RESERVED_TOP_LEVEL or name.startswith("astrid_"):
        return "a name reserved for Astrid and its runtimes"
    if _host_can_import(name, pack_root=pack_root):
        return "a package importable on this host, which could load instead of the pack file"
    return None


def _shadowed_requirement(name: str, *, contract: ImportContract, declared_python: set[str]) -> str | None:
    """The declared Python requirement a pack-root module named *name* would shadow, if any."""
    candidates = {name.lower().replace("_", "-").replace(".", "-")}
    mapped = contract.python_modules.get(name, _PYTHON_DISTRIBUTIONS.get(name))
    if mapped:
        candidates.add(mapped.lower().replace("_", "-").replace(".", "-"))
    for prefix, distribution in _PYTHON_NAMESPACE_DISTRIBUTIONS.items():
        if prefix.split(".", 1)[0] == name:
            candidates.add(distribution)
    shadowed = sorted(candidates & declared_python)
    return shadowed[0] if shadowed else None


def _classify(module: str, *, level: int, pack_id: str, contract: ImportContract,
              declared_packs: set[str], declared_python: set[str]) -> tuple[str, str] | None:
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
    for prefix, distribution in _PYTHON_NAMESPACE_DISTRIBUTIONS.items():
        if module == prefix or module.startswith(prefix + "."):
            if distribution in declared_python:
                return None
            return "undeclared_python", "Declare this Python package in dependencies.python_requirements."
    mapped = contract.python_modules.get(root, _PYTHON_DISTRIBUTIONS.get(root))
    if mapped:
        normalized = mapped.lower().replace("_", "-").replace(".", "-")
        if normalized in declared_python:
            return None
        return "undeclared_python", "Declare this Python package in dependencies.python_requirements."
    normalized_root = root.lower().replace("_", "-").replace(".", "-")
    if normalized_root in declared_python:
        return None
    return "undeclared_python", "Declare this Python package in dependencies.python_requirements."


def _relative_target(path: Path, pack_dir: Path, level: int, module: str,
                     pack_id: str) -> str | None:
    """Resolve a relative import in its canonical ``astrid.packs`` context.

    Pack source roots are installed as ``astrid.packs.<id>`` even when the
    checker is given a temporary or external filesystem path.  Keeping that
    logical prefix lets policy distinguish private own-pack access from a
    sibling pack's public API or private implementation.
    """
    if level < 1:
        return None
    try:
        relative = path.relative_to(pack_dir)
    except ValueError:
        return None
    package_parts = ["astrid", "packs", pack_id, *relative.parent.parts]
    climb = level - 1
    # Keep the top-level ``astrid`` package intact. Relative imports may reach
    # its public facade or sibling packs, but cannot climb above it.
    if climb > len(package_parts) - 1:
        return None
    if climb:
        package_parts = package_parts[:len(package_parts) - climb]
    imported_parts = list(package_parts)
    if module:
        imported_parts.extend(module.split("."))
    return ".".join(imported_parts)


def check_pack_imports(pack_dir: Path, *, contract: ImportContract,
                       manifest: Mapping[str, object]) -> list[ImportViolation]:
    """Return deterministic import violations without importing pack code."""
    pack_dir = Path(pack_dir)
    pack_id = manifest.get("id", pack_dir.name)
    pack_id = pack_id if isinstance(pack_id, str) else pack_dir.name
    if pack_id == "personal":
        return []  # The canonical personal pack keeps its static-scan exemption.
    deps = manifest.get("dependencies", {})
    raw_packs = deps.get("packs", ()) if isinstance(deps, Mapping) else ()
    declared_packs = set(raw_packs) if isinstance(raw_packs, (list, tuple, set, frozenset)) else set()
    declared_python = _requirements(manifest)
    violations: list[ImportViolation] = []

    def inspect_module(path: Path, line: int, module: str, level: int = 0,
                       symbols: tuple[str, ...] = ()) -> None:
        name = module.split(".", 1)[0]
        # A module or package inside this pack's root is a private helper, for
        # every entrypoint kind. The standard library keeps its own rule, and
        # ``astrid`` is never pack-local, so a pack cannot ship an ``astrid``
        # package to step around the public SDK surface.
        top = (_pack_module_path(pack_dir, name)
               if not level and module and name != "astrid" and name not in sys.stdlib_module_names
               else None)
        if top is not None:
            # The top-level name lives in this pack: first refuse decoys of
            # declared, reserved or host-importable packages, whatever the
            # dotted rest of the import names.
            fix = None
            shadowed = _shadowed_requirement(name, contract=contract, declared_python=declared_python)
            if shadowed is not None:
                fix = (f"Rename the pack module {name!r}: it shadows the declared Python requirement "
                       f"{shadowed!r}.")
            else:
                reason = _shadowed_name(name, pack_root=pack_dir)
                if reason is not None:
                    fix = (f"Rename the pack module {name!r}: it shadows {reason}; declare the real "
                           "package in dependencies.python_requirements if the pack needs it.")
            if fix is not None:
                violations.append(ImportViolation(
                    path.relative_to(pack_dir).as_posix(), line, module, "shadowed_dependency", fix))
                return
            located = top if module == name else _pack_module_path(pack_dir, module)
            # A dotted component that is missing or escapes the pack falls
            # through to the normal classification below (undeclared).
            if located is not None:
                # ``from pkg import sub`` imports the submodule ``pkg.sub`` when
                # one exists on disk, so each such name must also stay in the pack.
                if symbols and located.is_dir():
                    for symbol in symbols:
                        entry, module_file = located / symbol, located / f"{symbol}.py"
                        if symbol != "*" and (entry.exists() or entry.is_symlink()
                                              or module_file.exists() or module_file.is_symlink()):
                            inspect_module(path, line, f"{module}.{symbol}")
                return
        if level:
            resolved = _relative_target(path, pack_dir, level, module, pack_id)
            if resolved is None:
                violations.append(ImportViolation(
                    path.relative_to(pack_dir).as_posix(), line,
                    "." * level + module, "relative_escape",
                    "Keep relative imports within the pack or use a declared pack's public API."))
                return
            own_prefix = f"astrid.packs.{pack_id}"
            if resolved == own_prefix or resolved.startswith(own_prefix + "."):
                # Private helpers are available within the owning pack.
                return
            # For ``from ... import sibling`` (and ``from ...sibling import
            # api``), classify each imported name as the resolved module path.
            # This enforces the same declared-pack/public-api rule as absolute
            # imports while retaining normal from-imports from sibling.api.
            if symbols and (resolved in {"astrid", "astrid.packs"} or
                            (resolved.startswith("astrid.packs.") and len(resolved.split(".")) == 3)):
                for symbol in symbols:
                    inspect_module(path, line, f"{resolved}.{symbol}")
                return
            module = resolved
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
