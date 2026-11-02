"""Interactive dependency group update and rollback commands."""
from __future__ import annotations

import sys
from importlib import metadata
from pathlib import Path
from typing import Any, Mapping

from .install import PackInstallError
from .loader import packs_root as default_packs_root
from .transaction import install_group, read_group, rollback_group


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
        root = Path(args.packs_root).expanduser() if args.packs_root else default_packs_root()
        packages = {dist.metadata["Name"]: dist.version for dist in metadata.distributions()
                    if dist.metadata.get("Name")}
        result = install_group(source=args.source, revision=args.revision, packs=packs,
                               packs_root=root, python_packages=packages, intent="update",
                               consent=_confirm_update)
    except (PackInstallError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Updated {', '.join(sorted(packs))} in committed generation {result.transaction_id}.")
    return 0


def _handle_rollback(args) -> int:
    try:
        root = Path(args.packs_root).expanduser() if args.packs_root else default_packs_root()
        result = rollback_group(packs_root=root, pack_ids=args.pack_ids,
                                transaction_id=args.transaction_id, consent=_confirm_rollback)
    except (PackInstallError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"Rolled back {', '.join(sorted(set(args.pack_ids)))} in committed generation {result.transaction_id}.")
    return 0


__all__ = ["_handle_update", "_handle_rollback"]
