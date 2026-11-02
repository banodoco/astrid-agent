"""Resolve one explicitly selected target provider through admitted packs.

The caller owns caching and lifetime.  Local tasks never call this module's
resolver, and no provider package is imported by importing this module.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path
from typing import Any


def resolve_target_provider(kind: str, pack_roots: tuple[Path, ...]) -> Any:
    from astrid.core.pack.loader import load_pack_manifest

    if not isinstance(kind, str) or re.fullmatch(r"[a-z][a-z0-9_]*", kind) is None:
        raise ValueError("target provider id is invalid")
    candidates = []
    for root in pack_roots:
        root = Path(root).resolve()
        folder = root if root.name == kind else root / kind
        manifest = folder / "pack.yaml"
        if not manifest.exists():
            continue
        if folder.is_symlink() or manifest.is_symlink() or not folder.resolve().is_relative_to(root):
            raise ValueError("target provider escaped its admitted pack root")
        pack = load_pack_manifest(manifest, expected_pack_id=kind)
        if pack.status != "active":
            raise ValueError("target provider pack is not active")
        if pack.root not in [candidate.root for candidate in candidates]:
            candidates.append(pack)
    if len(candidates) != 1:
        raise ValueError("target provider must resolve to exactly one admitted pack")
    pack = candidates[0]
    path = pack.root / "adapter.py"
    if path.is_symlink() or not path.is_file() or path.resolve().parent != pack.root:
        raise ValueError("target provider adapter is unavailable in its admitted pack")
    # Adapter imports must resolve inside the same pack selected by the loader.
    package = importlib.import_module(f"astrid.packs.{kind}")
    locations = tuple(Path(item).resolve() for item in getattr(package, "__path__", ()))
    if locations != (pack.root,):
        raise ValueError("target provider imports do not match the admitted pack")
    spec = importlib.util.spec_from_file_location(f"astrid.packs.{kind}._host_adapter", path)
    if spec is None or spec.loader is None:
        raise ValueError("target provider adapter cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_target_provider(kind: str, pack_roots: tuple[Path, ...], **context: Any) -> Any:
    module = resolve_target_provider(kind, pack_roots)
    factory = getattr(module, "build_host_adapter", None)
    if not callable(factory):
        raise ValueError("target provider has no host adapter factory")
    return factory(**context)
