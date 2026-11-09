"""Every resource an in-tree pack.yaml references must ship in the built package.

Core-hosted packs (astrid/core/**/pack.yaml) and astrid/packs/** packs are both
loaded from the installed wheel. A pack whose declared docs/SKILL.md or other
resource is absent from [tool.setuptools.package-data] fails the installed host
at startup, so this test checks the declared package-data globs against every
pack.yaml path, not a per-file list.
"""
from __future__ import annotations

import glob
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ASTRID = ROOT / "astrid"


def _package_data_patterns() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data["tool"]["setuptools"]["package-data"]["astrid"])


def _packaged_files() -> set[Path]:
    selected: set[Path] = set()
    for pattern in _package_data_patterns():
        for match in glob.glob(str(ASTRID / pattern), recursive=True):
            path = Path(match)
            if path.is_file():
                selected.add(path.resolve())
    return selected


def _referenced_paths(node, found: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "path" and isinstance(value, str):
                found.add(value)
            else:
                _referenced_paths(value, found)
    elif isinstance(node, list):
        for item in node:
            _referenced_paths(item, found)


def _pack_manifests() -> list[Path]:
    manifests = list((ASTRID / "packs").glob("**/pack.yaml"))
    manifests += list((ASTRID / "core").glob("**/pack.yaml"))
    return sorted(manifests)


def test_every_pack_yaml_resource_is_in_package_data() -> None:
    packaged = _packaged_files()
    missing: list[str] = []
    checked = 0
    for manifest in _pack_manifests():
        loaded = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        referenced: set[str] = set()
        _referenced_paths(loaded, referenced)
        for rel in sorted(referenced):
            if rel.endswith(".py") or "://" in rel:
                continue  # Python modules ship through package discovery.
            target = (manifest.parent / rel).resolve()
            if not target.is_file():
                continue  # Absent files are a pack-manifest error, reported by its own tests.
            checked += 1
            if target not in packaged:
                missing.append(f"{manifest.relative_to(ASTRID)} -> {rel}")
    assert checked > 0, "no pack resources were checked; the test is not seeing the manifests"
    assert not missing, "pack resources referenced but not in package-data:\n" + "\n".join(missing)
