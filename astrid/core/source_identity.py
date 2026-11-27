"""Executable source measurement, usable before importing Runtime code."""

from __future__ import annotations

import hashlib
import importlib.machinery
import json
import sys
from pathlib import Path
from typing import Any


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _source_digest(root: Path) -> str:
    """Hash the complete executor source tree without retaining a source path in a task."""
    entries: list[tuple[str, str]] = []
    if root.is_file():
        return hashlib.sha256(root.read_bytes()).hexdigest()
    for path in sorted(root.rglob("*"), key=lambda path: path.parts):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        entries.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return _canonical_digest(entries)


def _reject_alias(path: Path) -> None:
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise ValueError(f"source closure contains a symlink: {ancestor}")


def _closure_definition() -> dict[str, Any]:
    """Read the Runtime-owned vendored definition without importing Runtime."""
    path = Path(__file__).absolute().with_name("execution") / "host-source-closure-v1.json"
    _reject_alias(path)
    try:
        definition = json.loads(path.read_text(encoding="utf-8"))
        if definition["schema_version"] != 1:
            raise ValueError("unsupported source closure definition version")
        if not isinstance(definition["components"], dict) or not isinstance(
            definition["installed_pair"]["components"], dict
        ):
            raise ValueError("invalid source closure component mapping")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("source closure definition is unavailable or invalid") from exc
    return definition


def source_checkout_closure(checkout: str | Path) -> dict[str, Any]:
    """Independently measure the executable host closure before Runtime imports.

    The component definition is produced and vendored by Runtime's artifact
    lock; the legacy pack admission digest remains separate.
    """
    root = Path(checkout).expanduser().absolute()
    if ".." in root.parts:
        raise ValueError("source closure root is not canonical")
    _reject_alias(root)
    definition = _closure_definition()
    components = definition["components"].copy()
    pair = definition["installed_pair"]["components"]
    if any((root / relative).exists() or (root / relative).is_symlink()
           for relative in pair.values()):
        for relative in pair.values():
            path = root / relative
            _reject_alias(path)
            if not path.is_dir():
                raise ValueError("source closure installed pair component is missing or not a directory")
        components.update(pair)

    measured: list[dict[str, str]] = []
    for name, relative in components.items():
        path = root / relative
        _reject_alias(path)
        if not path.exists():
            raise ValueError(f"source closure component is missing: {name}")
        if path.is_dir() and any(child.is_symlink() for child in path.rglob("*")):
            raise ValueError(f"source closure component contains a symlink: {name}")
        measured.append({
            "component": name,
            "path": relative,
            "sha256": _source_digest(path),
        })
    measured.sort(key=lambda item: item["component"])
    return {
        "schema_version": definition["schema_version"],
        "components": measured,
        "digest": _canonical_digest(measured),
    }


def source_checkout_closure_digest(checkout: str | Path) -> str:
    """Return the deterministic digest for the executable source closure."""
    return str(source_checkout_closure(checkout)["digest"])


def installed_runtime_origins(root: Path) -> dict[str, str]:
    """Resolve required Runtime code without executing package initializers."""
    root = root.absolute()
    origins = {}
    for name in ("runtime_protocol", "runtime_protocol.platform", "astrid_runtime"):
        parent = name.rpartition(".")[0]
        search = [str(root / parent)] if parent else sys.path
        spec = importlib.machinery.PathFinder.find_spec(name, search)
        expected = root.joinpath(*name.split("."))
        expected = expected / "__init__.py" if not parent else expected.with_suffix(".py")
        if spec is None or spec.origin != str(expected):
            raise ValueError(f"installed Runtime origin differs or is missing: {name}")
        if (not expected.is_file()
                or any(path.is_symlink() for path in (expected, *expected.parents))):
            raise ValueError(f"installed Runtime origin is missing or symlinked: {name}")
        if spec.submodule_search_locations is not None and list(spec.submodule_search_locations) != [str(expected.parent)]:
            raise ValueError(f"installed Runtime package search path differs: {name}")
        origins[name] = str(expected)
    for name in ("runtime_protocol", "astrid_runtime"):
        if any(path.is_symlink() for path in (root / name).rglob("*")):
            raise ValueError(f"installed Runtime package contains a symlink: {name}")
    return origins


def verify_installed_host_startup() -> None:
    """Check the existing launch binding before execution-package imports."""
    original = sys.orig_argv
    if (not sys.flags.isolated or sys.argv[:1] != ["-m"] or "-m" not in original
            or original[original.index("-m") + 1:original.index("-m") + 2]
            != ["astrid.core.execution.generic_host"]):
        return
    # The selected interpreter's actual package tree is authority. The launch
    # checkout must name it, never supply a different import root.
    root = Path(__file__).absolute().parents[2]
    def argument(name: str) -> str:
        if sys.argv.count(name) != 1:
            raise ValueError(f"installed host requires one {name}")
        index = sys.argv.index(name) + 1
        if index >= len(sys.argv):
            raise ValueError(f"installed host requires a value for {name}")
        return sys.argv[index]
    if argument("--source-checkout") != str(root):
        raise ValueError("installed host source differs from selected interpreter tree")
    installed_runtime_origins(root)
    expected = argument("--source-closure-digest").removeprefix("sha256:")
    if source_checkout_closure_digest(root) != expected:
        raise ValueError("installed host executable source closure mismatch")
