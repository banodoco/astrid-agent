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


def source_checkout_closure(checkout: str | Path) -> dict[str, Any]:
    """Measure the executable host closure owned by this checkout.

    The legacy pack digest remains the capability-admission identity.  This
    separate receipt covers the process host, Astrid core, SDK/bootstrap,
    package entrypoints, launcher, vendored workspace-client files, and the
    Runtime packages in an installed pair that can change execution.
    """
    root = Path(checkout).expanduser().resolve()
    components = {
        "host": root / "astrid" / "core" / "execution" / "generic_host.py",
        "core": root / "astrid" / "core",
        "sdk": root / "astrid" / "sdk",
        "launcher": root / "astrid" / "omp_agent.py",
        "astrid_entrypoint": root / "astrid" / "__init__.py",
        "astrid_version": root / "astrid" / "version.py",
        "astrid_main": root / "astrid" / "__main__.py",
        "astrid_runtime_cli": root / "astrid" / "runtime_cli.py",
        "vendored_workspace_client": root / "astrid_runtime_client",
    }
    # Source checkouts keep their existing Agent closure. An installed pair
    # contributes both Runtime packages to the same existing digest carrier.
    runtime_names = ("runtime_protocol", "astrid_runtime")
    if any((root / name).exists() or (root / name).is_symlink() for name in runtime_names):
        components.update({name: root / name for name in runtime_names})
    measured: list[dict[str, str]] = []
    for name, path in components.items():
        if path.is_symlink() or not path.exists():
            raise ValueError(f"source closure component is missing or symlinked: {name}")
        if path.is_dir() and any(child.is_symlink() for child in path.rglob("*")):
            raise ValueError(f"source closure component contains a symlink: {name}")
        measured.append({
            "component": name,
            "path": path.relative_to(root).as_posix(),
            "sha256": _source_digest(path),
        })
    measured.sort(key=lambda item: item["component"])
    return {
        "schema_version": 1,
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
