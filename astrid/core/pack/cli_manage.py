"""Explicit pack installation and removal commands.

Mutations go through the M1-16 dependency-group transaction. This module only
collects source facts and obtains user consent; it never imports pack code.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping

import yaml

from astrid.core.pack.canonical import validate_canonical_pack
from astrid.core.pack.identity import pack_digest, read_pin
from astrid.core.pack.install import PackInstallError, _fetch_tree, _git_source
from astrid.core.pack.personal import resolve_personal_packs_root
from astrid.core.pack.transaction import GroupCommitError, install_group, rollback_group

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _consent_details(pack_root: Path, *, source: str, commit: str | None) -> dict[str, Any]:
    manifest = validate_canonical_pack(pack_root).definition.to_dict()
    secrets = manifest.get("secrets", [])
    actions = manifest.get("actions", {})
    action_secrets = {
        action_id: action["secrets"]
        for action_id, action in actions.items()
        if isinstance(action, dict) and action.get("secrets")
    }
    permissions = manifest.get("permissions", [])
    network = [permission for permission in permissions
               if isinstance(permission, dict) and permission.get("id") in {"network", "external_services"}]
    return {"id": manifest["id"], "name": manifest["name"], "source": source,
            "commit": commit, "secrets": secrets, "action_secrets": action_secrets,
            "network": network}


def _show_consent(details: dict[str, Any]) -> None:
    print(f"Pack: {details['id']} ({details['name']})")
    print(f"Source: {details['source']}")
    print(f"Commit: {details['commit'] or '(local folder; unpinned)'}")
    print("Declared secrets:")
    print(json.dumps(details["secrets"], indent=2, sort_keys=True) if details["secrets"] else "  none")
    print("Action-scoped secrets:")
    if details["action_secrets"]:
        for action_id, secrets in details["action_secrets"].items():
            print(f"  actions.{action_id}.secrets:")
            print(json.dumps(secrets, indent=2, sort_keys=True))
    else:
        print("  none")
    print("Declared network access:")
    print(json.dumps(details["network"], indent=2, sort_keys=True) if details["network"] else "  none")


def _confirm(prompt: str, *, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except EOFError:
        return False



# ── Browser build inputs (PK-14 prerequisite) ───────────────────────────────
# A pack's `ui.<name>` editor entry is built by the pinned builder inside the
# transaction (astrid.pack_build). The CLI only derives the frozen build inputs
# from the pack bytes and selects the release-pinned toolchain; it never runs
# pack code and never downloads a tool.
TOOLCHAIN_ENV = "ASTRID_PACK_TOOLCHAIN"
_EXTENSION_MANIFEST_NAMES = ("astrid-extension.json", "extension.json")
_TOOLCHAIN_FIELDS = frozenset({"version", "node_path", "node_version", "node_sha256", "esbuild_path",
                               "esbuild_version", "esbuild_sha256", "shared_modules_lock"})


def _editor_build(pack_root: Path) -> dict[str, Any] | None:
    """Return the transaction build input for the pack's one `ui.<name>` editor entry.

    A missing or unreadable manifest yields None: the transaction then refuses it
    with its own validation error. A declared editor entry must be complete.
    """
    try:
        data = yaml.safe_load((pack_root / "pack.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return None
    ui = data.get("ui") if isinstance(data, dict) else None
    if not isinstance(ui, dict):
        return None
    editors = sorted(name for name, item in ui.items() if isinstance(item, dict) and item.get("type") == "editor")
    if not editors:
        return None
    if len(editors) > 1:
        raise PackInstallError("invalid_request", "The pack declares more than one ui editor entry ("
                               + ", ".join(f"ui.{name}" for name in editors)
                               + "); the build hook admits one browser entry per pack.")
    name = editors[0]
    entry = ui[name].get("entry")
    parts = PurePosixPath(entry).parts if isinstance(entry, str) else ()
    if (not isinstance(entry, str) or not entry.endswith(".tsx") or "\\" in entry
            or PurePosixPath(entry).is_absolute() or any(part in {"", ".", ".."} for part in parts)):
        raise PackInstallError("invalid_request", f"ui.{name}.entry must be a relative .tsx path inside the pack.")
    folder = pack_root.joinpath(*parts[:-1])
    found = [folder / manifest_name for manifest_name in _EXTENSION_MANIFEST_NAMES
             if (folder / manifest_name).is_file() and not (folder / manifest_name).is_symlink()]
    if len(found) != 1:
        raise PackInstallError("invalid_request", f"ui.{name} needs exactly one extension manifest beside its entry ("
                               + " or ".join(_EXTENSION_MANIFEST_NAMES) + ").")
    try:
        manifest = json.loads(found[0].read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PackInstallError("invalid_request", f"ui.{name} extension manifest {found[0].name} is not valid JSON.") from exc
    if not isinstance(manifest, dict):
        raise PackInstallError("invalid_request", f"ui.{name} extension manifest {found[0].name} must be an object.")
    return {"entry": entry, "extension_manifest": manifest}


def _source_builds(source: str, revision: str | None, packs: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Read build inputs from the selected source before any collection state is written.

    A Git source is read from the same declared refs the transaction fetches, into
    a private temporary directory outside every packs root. The consent check
    re-derives them from the staged bytes, so a moved ref cannot change them.
    """
    builds: dict[str, dict[str, Any]] = {}
    if revision is None:
        for pack_id, subpath in packs.items():
            build = _editor_build(Path(source).expanduser() / subpath)
            if build is not None:
                builds[pack_id] = build
        return builds
    work = Path(tempfile.mkdtemp(prefix="astrid-pack-review-"))
    try:
        text = _git_source(str(source))
        for index, (pack_id, subpath) in enumerate(sorted(packs.items())):
            prepared = work / str(index)
            prepared.mkdir()
            stage = prepared / "pack"
            stage.mkdir()
            _fetch_tree(prepared, text, revision, subpath, stage)
            build = _editor_build(stage)
            if build is not None:
                builds[pack_id] = build
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return builds


def _check_staged_builds(changes: Mapping[str, Any], builds: Mapping[str, Any]) -> None:
    """Bind the frozen build inputs to the exact staged bytes the user reviews."""
    for pack_id, change in changes["packs"].items():
        if _editor_build(Path(change["staged_root"])) != builds.get(pack_id):
            raise PackInstallError("source_changed", f"The ui entry of {pack_id} changed between review and staging; review it again.")


def _file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _managed_toolchain() -> Path | None:
    """Find a descriptor only in the currently selected, executing install."""
    from astrid.installer.core import current
    from runtime_protocol import platform

    prefix = Path(sys.prefix)
    if not prefix.is_absolute() or prefix.name != "py":
        return None
    version_root = prefix.parent
    if version_root.parent.name != "versions":
        return None
    install_root = version_root.parent.parent
    descriptor = version_root / "pack-build" / "toolchain.json"
    try:
        # No aliases, reparse points, foreign ownership or group/world-writable
        # selection/descriptor components may authorize an implicit default.
        for item in (install_root, version_root.parent, version_root, prefix,
                     install_root / "current", version_root / "version.json",
                     descriptor.parent, descriptor):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                return None
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                return None
            if os.name == "nt":
                if not platform.is_owner_only(item):
                    return None
            elif info.st_mode & 0o022:
                return None
        if not descriptor.is_file() or descriptor.stat().st_nlink != 1:
            return None
        module = Path(__file__)
        if (not module.is_absolute() or module.resolve() != module
                or not module.is_relative_to(prefix)):
            return None
        if prefix.resolve() != prefix or descriptor.resolve() != descriptor:
            return None
        selected = current(install_root)
        if selected is None or selected.root != version_root or not selected.python.is_relative_to(prefix):
            return None
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return None
    return descriptor


def _toolchain(selected: str | None) -> dict[str, Any]:
    """Load and verify the release-pinned browser toolchain lock; refuse when unavailable.

    The lock names absolute Node and esbuild binaries with their pinned versions
    and sha256, and the App's shared-module lock (its sha256 is the toolchain
    digest recorded in every artifact manifest).
    """
    from astrid import pack_build

    def refuse(reason: str) -> PackInstallError:
        return PackInstallError("toolchain_unavailable", reason + " Nothing was changed.")

    value = selected if selected is not None else os.environ.get(TOOLCHAIN_ENV)
    implicit = value is None
    if implicit:
        value = _managed_toolchain()
    if not value:
        raise refuse("This pack declares a browser ui entry, but no pinned build toolchain is selected:"
                     f" pass --toolchain <toolchain.json> or set {TOOLCHAIN_ENV}.")
    path = Path(value).expanduser()
    try:
        if implicit:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                actual = path.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or (info.st_dev, info.st_ino) != (actual.st_dev, actual.st_ino)
                        or (os.name != "nt" and (info.st_mode & 0o022 or info.st_uid != os.getuid()))
                        or _managed_toolchain() != path):
                    raise OSError("Managed descriptor changed or is unsafe")
                lock = json.load(stream)
        else:
            lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise refuse(f"The build toolchain lock {path} is unreadable.") from exc
    if (not isinstance(lock, dict) or set(lock) != _TOOLCHAIN_FIELDS or lock["version"] != 1
            or not all(isinstance(lock[key], str) and lock[key] for key in _TOOLCHAIN_FIELDS - {"version"})):
        raise refuse(f"The build toolchain lock {path} must hold exactly: " + ", ".join(sorted(_TOOLCHAIN_FIELDS)) + ".")
    node, esbuild, shared_lock = (Path(lock[key]) for key in ("node_path", "esbuild_path", "shared_modules_lock"))
    if not all(item.is_absolute() for item in (node, esbuild, shared_lock)):
        raise refuse("The build toolchain lock must name absolute node, esbuild and shared-module lock paths.")
    if lock["esbuild_version"] != "0.27.7":
        raise refuse("The pinned builder accepts only esbuild 0.27.7.")
    env = {"PATH": "", "LANG": "C.UTF-8", "ESBUILD_BINARY_PATH": str(esbuild)}
    for label, binary, version in (("node", node, "v" + lock["node_version"]),
                                   ("esbuild", esbuild, lock["esbuild_version"])):
        try:
            if not binary.is_file() or _file_sha256(binary) != lock[f"{label}_sha256"]:
                raise refuse(f"The {label} binary {binary} is missing or does not match its pinned sha256.")
            observed = subprocess.run([str(binary), "--version"], capture_output=True, text=True, env=env,
                                      timeout=15).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise refuse(f"The {label} binary {binary} cannot be run.") from exc
        if observed != version:
            raise refuse(f"The {label} binary {binary} reports {observed or 'nothing'}, not the pinned {version}.")
    api = esbuild.parent.parent / "lib" / "main.js"
    try:
        api_ok = _file_sha256(api) == pack_build._API_HASH
    except OSError:
        api_ok = False
    if not api_ok:
        raise refuse(f"The esbuild JavaScript API {api} does not match the pinned builder.")
    try:
        shared_bytes = shared_lock.read_bytes()
        shared = json.loads(shared_bytes)["shared_modules"]
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise refuse(f"The shared-module lock {shared_lock} is unreadable.") from exc
    if not isinstance(shared, dict) or set(shared) != pack_build._SINGLETONS:
        raise refuse(f"The shared-module lock {shared_lock} must map exactly: " + ", ".join(sorted(pack_build._SINGLETONS)) + ".")
    return {"node_path": str(node), "node_version": lock["node_version"], "node_sha256": lock["node_sha256"],
            "esbuild_path": str(esbuild), "esbuild_version": lock["esbuild_version"],
            "esbuild_sha256": lock["esbuild_sha256"], "lock_sha256": hashlib.sha256(shared_bytes).hexdigest(),
            "shared_modules": shared}


def _build_inputs(source: str, revision: str | None, packs: Mapping[str, str],
                  selected_toolchain: str | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    builds = _source_builds(source, revision, packs)
    return builds, (_toolchain(selected_toolchain) if builds else None)


def _handle_add(args: argparse.Namespace) -> int:
    pack_id = args.pack_id
    subpath = args.subpath
    source = args.source
    root = _selected_root(args.packs_root)

    builds, toolchain = _build_inputs(source, args.revision, {pack_id: subpath}, args.toolchain)

    def consent(changes: dict[str, Any]) -> bool:
        staged_root = Path(changes["packs"][pack_id]["staged_root"])
        _check_staged_builds(changes, builds)
        details = _consent_details(staged_root, source=source, commit=changes["revision"])
        if details["id"] != pack_id:
            raise PackInstallError("pack_id_mismatch", "Selected manifest id does not match --pack-id.")
        _show_consent(details)
        accepted = _confirm("Install this exact staged pack through the dependency-group transaction? [y/N] ",
                             yes=args.yes)
        if accepted and args.revision is None:
            live_root = Path(source).expanduser() / subpath
            if pack_digest(live_root) != pack_digest(staged_root):
                raise PackInstallError("source_changed", "Pack source changed during consent; review it again.")
        return accepted

    root_was_present = root.exists()
    try:
        packages, pins = _validation_snapshot()
        result = install_group(source=source, revision=args.revision, packs={pack_id: subpath},
                              packs_root=root,
                              selected_roots=_composition_roots(root, include_default=args.packs_root is None),
                              core_root=_core_root(), python_packages=packages, release_pins=pins,
                              origin="folder" if args.revision is None else "user",
                              builds=builds, toolchain=toolchain, consent=consent)
    except PackInstallError as exc:
        if exc.code in {"user_cancelled", "source_changed"} and not root_was_present:
            try:
                root.rmdir()
            except OSError:
                pass
        raise
    print(f"Installed {pack_id} at {result.paths[pack_id]} (transaction {result.transaction_id}).")
    if result.refresh_pending:
        print("Refresh acknowledgment is pending; retry the committed transaction refresh.", file=sys.stderr)
    return 0


def _handle_remove(args: argparse.Namespace) -> int:
    root = _selected_root(args.packs_root)
    ids = tuple(dict.fromkeys(args.pack_ids))
    if not ids:
        raise PackInstallError("invalid_request", "Supply at least one pack id to remove.")
    print("Remove packs through the dependency-group transaction: " + ", ".join(ids))
    if not _confirm("Continue? [y/N] ", yes=args.yes):
        print("Pack removal cancelled.")
        return 1
    packages, pins = _validation_snapshot()
    result = install_group(source=".", packs={}, packs_root=root,
                          selected_roots=_composition_roots(root, include_default=args.packs_root is None),
                          core_root=_core_root(), python_packages=packages, release_pins=pins, remove=ids)
    print(f"Removed {', '.join(ids)} (transaction {result.transaction_id}).")
    if result.refresh_pending:
        print("Refresh acknowledgment is pending; retry the committed transaction refresh.", file=sys.stderr)
    return 0


def _handle_add_error(args: argparse.Namespace) -> int:
    try:
        return _handle_add(args)
    except PackInstallError as exc:
        if exc.code == "user_cancelled":
            print("Pack installation cancelled.")
            return 1
        print(f"packs add: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, GroupCommitError, subprocess.SubprocessError, yaml.YAMLError) as exc:
        print(f"packs add: {exc}", file=sys.stderr)
        return 1


def _handle_remove_error(args: argparse.Namespace) -> int:
    try:
        return _handle_remove(args)
    except (OSError, ValueError, GroupCommitError) as exc:
        print(f"packs remove: {exc}", file=sys.stderr)
        return 1


def build_add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("add", help="Review and install a pack through one transaction.")
    parser.add_argument("source", help="Folder or Git source containing the pack.")
    parser.add_argument("--pack-id", required=True, help="Pack identifier declared in pack.yaml.")
    parser.add_argument("--subpath", default=".", help="Pack path inside the source (default: .).")
    parser.add_argument("--revision", help="Git ref or commit to resolve and show before consent.")
    parser.add_argument("--packs-root", help="User pack collection root (defaults to ~/Astrid/packs).")
    parser.add_argument("--yes", action="store_true", help="Accept the displayed source and permission declarations.")
    add_toolchain_argument(parser)
    parser.set_defaults(handler=_handle_add_error)


def add_toolchain_argument(parser: Any) -> None:
    parser.add_argument("--toolchain", metavar="TOOLCHAIN_JSON",
                        help="Release-pinned browser build toolchain lock, required for a pack with a ui editor"
                             f" entry (default: ${TOOLCHAIN_ENV}).")


def build_remove_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("remove", help="Remove packs through one dependency-group transaction.")
    parser.add_argument("pack_ids", nargs="+", help="Pack identifiers to remove.")
    parser.add_argument("--packs-root", help="User pack collection root (defaults to ~/Astrid/packs).")
    parser.add_argument("--yes", action="store_true", help="Confirm the removal transaction.")
    parser.set_defaults(handler=_handle_remove_error)
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
        for dist in importlib.metadata.distributions() if dist.metadata.get("Name")
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


def _confirm_update(changes: dict[str, Any]) -> bool:
    print("Proposed dependency group update:", file=sys.stderr)
    print(f"  source: {changes['source']}", file=sys.stderr)
    print(f"  target revision: {changes['revision']}", file=sys.stderr)
    for pack_id, change in changes["packs"].items():
        print(f"  {pack_id}: {change['before'] or 'not installed'} -> {change['after']}", file=sys.stderr)
        print(f"    tree: {change['before_digest'] or 'none'} -> {change['after_digest']}", file=sys.stderr)
    if changes.get("removed"):
        print(f"  removing: {', '.join(changes['removed'])}", file=sys.stderr)
    print(f"  preserved removals: {', '.join(changes['removals']) or 'none'}", file=sys.stderr)
    return input("Commit this validated group update? [y/N] ").strip().lower() in {"y", "yes"}


def _confirm_rollback(changes: dict[str, Any]) -> bool:
    print("Proposed dependency group rollback:", file=sys.stderr)
    for pack_id, change in changes["packs"].items():
        print(f"  {pack_id}: {change['current'] or 'not installed'} -> {change['restore'] or 'absent'}", file=sys.stderr)
        print(f"    tree: {change['current_digest'] or 'none'} -> {change['restore_digest'] or 'none'}", file=sys.stderr)
    print(f"  preserved removals: {', '.join(changes['removals']) or 'none'}", file=sys.stderr)
    return input("Commit this rollback? [y/N] ").strip().lower() in {"y", "yes"}


def _handle_update(args: argparse.Namespace) -> int:
    try:
        packs = _pack_specs(args.packs)
        root = _selected_root(args.packs_root)
        selected_roots = _composition_roots(root, include_default=args.packs_root is None)
        packages, pins = _validation_snapshot()
        builds, toolchain = _build_inputs(args.source, args.revision, packs, getattr(args, "toolchain", None))

        def consent(changes: dict[str, Any]) -> bool:
            _check_staged_builds(changes, builds)
            return _confirm_update(changes)

        result = install_group(source=args.source, revision=args.revision, packs=packs,
                               packs_root=root, selected_roots=selected_roots,
                               core_root=_core_root(),
                               python_packages=packages, release_pins=pins, intent="update",
                               builds=builds, toolchain=toolchain, consent=consent)
    except (PackInstallError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not result.changed_ids:
        # Equal-byte update: nothing was published or retired.
        print(f"{', '.join(sorted(packs))} unchanged (generation {result.transaction_id}).")
        return 0
    print(f"Updated {', '.join(sorted(packs))} in committed generation {result.transaction_id}.")
    return 0


def _handle_rollback(args: argparse.Namespace) -> int:
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


def origin_and_modified(pack: Any) -> tuple[str, bool | None]:
    """Return installed provenance, with unknown modification for unpinned packs."""
    from astrid.core.pack.transaction import group_read_lock

    pin_path = pack.root / "PIN"
    try:
        pin_mode = pin_path.lstat().st_mode
    except FileNotFoundError:
        return str(getattr(pack, "origin", "unknown")), None
    if not stat.S_ISREG(pin_mode):
        return "unknown", None
    try:
        with group_read_lock(pack.root.parent):
            pin = read_pin(pin_path)
            origin = pin["origin"]
            expected = pin["digest"]
            modified = None if expected is None else pack_digest(pack.root) != expected
            return origin, modified
    except (OSError, ValueError):
        return "unknown", None
