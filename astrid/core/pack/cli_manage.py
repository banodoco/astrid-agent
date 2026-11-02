"""Interactive dependency group update and rollback commands."""
from __future__ import annotations

import json
import os
import re
import sys
from importlib import metadata
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .install import PackInstallError
from .personal import resolve_personal_packs_root
from .transaction import _facts, install_group, read_group, rollback_group

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _selected_root(override: str | None) -> Path:
    root = resolve_personal_packs_root(override)
    if root is None:
        raise PackInstallError("unsafe_path", "Configure an absolute user pack collection with ASTRID_PACKS_PATH.")
    return root


def _validation_snapshot() -> tuple[Mapping[str, str], Mapping[str, Mapping[str, str]]]:
    packages = {
        dist.metadata.get("Name", "").lower().replace("_", "-").replace(".", "-"): dist.version
        for dist in metadata.distributions() if dist.metadata.get("Name")
    }
    # Release policy is an independently supplied, verified snapshot. Source
    # declarations describe acquisition and may intentionally be unpinned.
    pins: dict[str, Mapping[str, str]] = {}
    pin_file = os.environ.get("ASTRID_RELEASE_PACK_PINS")
    if pin_file:
        try:
            raw_pins = json.loads(Path(pin_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PackInstallError("invalid_pin", "The authoritative release pack-pin snapshot is unreadable.") from exc
        if not isinstance(raw_pins, dict):
            raise PackInstallError("invalid_pin", "The authoritative release pack-pin snapshot must be an object.")
        for pack_id, value in raw_pins.items():
            if (not isinstance(pack_id, str) or not isinstance(value, dict)
                    or set(value) != {"commit", "digest"}
                    or not isinstance(value["commit"], str) or not _OBJECT_ID.fullmatch(value["commit"])
                    or not isinstance(value["digest"], str) or not _SHA256.fullmatch(value["digest"])
                    or value["commit"] != value["commit"].lower()):
                raise PackInstallError("invalid_pin", "Release pack pins require a full lowercase commit and tree digest.")
            pins[pack_id] = _freeze(dict(value))
    return MappingProxyType(packages), MappingProxyType(pins)


def _selected_snapshot(root: Path) -> Mapping[str, Mapping[str, Any]]:
    """Compose selected core/external facts once for a real management call.

    Roots are explicit: the bundled core collection and configured external
    roots. No ambient home, project, or unrelated checkout is searched.
    """
    from .loader import packs_root

    roots = [packs_root()]
    roots.extend(Path(item).expanduser() for item in os.environ.get("ASTRID_PACKS_PATH", "").split(os.pathsep) if item)
    roots = list(dict.fromkeys(path.resolve() for path in roots if path.resolve() != root.resolve()))
    selected: dict[str, Mapping[str, Any]] = {}
    for selected_root in roots:
        if not selected_root.is_dir():
            continue
        for child in sorted(selected_root.iterdir()):
            if not child.is_dir() or child.is_symlink() or not (child / "pack.yaml").is_file():
                continue
            pack_id = child.name
            if pack_id in selected:
                continue
            _, facts = _facts(child)
            selected[pack_id] = _freeze(dict(facts))
    return MappingProxyType(selected)


def _pack_specs(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        pack_id, separator, subpath = value.partition("=")
        if not separator or not pack_id or not subpath or pack_id in result:
            raise PackInstallError("invalid_request", "Select packs as unique ID=SUBPATH values.")
        result[pack_id] = subpath
    return result


def _confirm_update(changes: Mapping[str, Any]) -> bool:
    print("Proposed dependency group update:", file=sys.stderr)
    print(f"  source: {changes['source']}", file=sys.stderr)
    print(f"  target revision: {changes['revision']}", file=sys.stderr)
    for pack_id, change in changes["packs"].items():
        before = change["before"] or "not installed"
        after = change["after"]
        print(f"  {pack_id}: {before} -> {after}", file=sys.stderr)
        print(f"    tree: {change['before_digest'] or 'none'} -> {change['after_digest']}", file=sys.stderr)
    print(f"  preserved removals: {', '.join(changes['removals']) or 'none'}", file=sys.stderr)
    return input("Commit this validated group update? [y/N] ").strip().lower() in {"y", "yes"}


def _confirm_rollback(changes: Mapping[str, Any]) -> bool:
    print("Proposed dependency group rollback:", file=sys.stderr)
    for pack_id, change in changes["packs"].items():
        print(f"  {pack_id}: {change['current'] or 'not installed'} -> {change['restore'] or 'absent'}", file=sys.stderr)
        print(f"    tree: {change['current_digest'] or 'none'} -> {change['restore_digest'] or 'none'}", file=sys.stderr)
    print(f"  preserved removals: {', '.join(changes['removals']) or 'none'}", file=sys.stderr)
    return input("Commit this rollback? [y/N] ").strip().lower() in {"y", "yes"}


def _handle_update(args) -> int:
    try:
        packs = _pack_specs(args.packs)
        root = _selected_root(args.packs_root)
        packages, pins = _validation_snapshot()
        selected = _selected_snapshot(root)
        result = install_group(source=args.source, revision=args.revision, packs=packs,
                               packs_root=root, python_packages=packages, release_pins=pins, selected=selected, intent="update",
                               consent=_confirm_update)
    except (PackInstallError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Updated {', '.join(sorted(packs))} in committed generation {result.transaction_id}.")
    return 0


def _handle_rollback(args) -> int:
    try:
        root = _selected_root(args.packs_root)
        packages, pins = _validation_snapshot()
        selected = _selected_snapshot(root)
        result = rollback_group(packs_root=root, pack_ids=args.pack_ids,
                                transaction_id=args.transaction_id, consent=_confirm_rollback,
                                python_packages=packages, release_pins=pins, selected=selected)
    except (PackInstallError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Rolled back {', '.join(sorted(set(args.pack_ids)))} in committed generation {result.transaction_id}.")
    return 0


__all__ = ["_handle_update", "_handle_rollback"]
