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
from .transaction import install_group, rollback_group

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _selected_root(override: str | None) -> Path:
    # ASTRID_PACKS_PATH extends discovery; it does not choose the collection
    # that management commands mutate. Only --packs-root may override the
    # default writable personal collection.
    default_root = Path.home() / "Astrid" / "packs"
    root = resolve_personal_packs_root(override if override is not None else default_root)
    if root is None:
        raise PackInstallError("unsafe_path", "Use an absolute writable collection with --packs-root.")
    return root


def _composition_roots(target_root: Path, *, include_default: bool) -> tuple[Path, ...]:
    """Freeze the host's explicitly selected external roots at CLI entry."""
    configured = os.environ.get("ASTRID_PACKS_PATH")
    values = ([Path.home() / "Astrid" / "packs"] if include_default else [])
    if configured is not None:
        values.extend(Path(item).expanduser() for item in configured.split(os.pathsep) if item)
    roots = []
    for value in values:
        if not value.is_absolute():
            raise PackInstallError("unsafe_path", "Selected external pack roots must be absolute paths.")
        resolved = value.resolve()
        if resolved != target_root.resolve():
            roots.append(resolved)
    return tuple(dict.fromkeys(roots))


def _core_root() -> Path:
    from .loader import packs_root
    return packs_root().resolve()


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
        selected_roots = _composition_roots(root, include_default=args.packs_root is None)
        packages, pins = _validation_snapshot()
        result = install_group(source=args.source, revision=args.revision, packs=packs,
                               packs_root=root, selected_roots=selected_roots,
                               core_root=_core_root(),
                               python_packages=packages, release_pins=pins, intent="update",
                               consent=_confirm_update)
    except (PackInstallError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Updated {', '.join(sorted(packs))} in committed generation {result.transaction_id}.")
    return 0


def _handle_rollback(args) -> int:
    try:
        root = _selected_root(args.packs_root)
        selected_roots = _composition_roots(root, include_default=args.packs_root is None)
        packages, pins = _validation_snapshot()
        result = rollback_group(packs_root=root, pack_ids=args.pack_ids,
                                transaction_id=args.transaction_id, consent=_confirm_rollback,
                                selected_roots=selected_roots, core_root=_core_root(),
                                python_packages=packages, release_pins=pins)
    except (PackInstallError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Rolled back {', '.join(sorted(set(args.pack_ids)))} in committed generation {result.transaction_id}.")
    return 0


__all__ = ["_handle_update", "_handle_rollback"]
