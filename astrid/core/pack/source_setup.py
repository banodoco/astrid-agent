"""Pinned external pack setup and the shared installed-source inventory.

This module owns acquisition state for external capability packs.  Discovery
calls the read-only inventory helpers below; it never calls ``git`` to acquire
or activate a source.  The state file is deliberately small and contains
only source custody metadata, not runtime/project data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping

from astrid.core.foundation.atomic_io import read_json, write_json_atomic
from astrid.core.pack.canonical import (
    CanonicalPackValidationError,
    ExternalPackSource,
    read_normalize_validate,
)

SOURCE_STATE_VERSION = 1
DEFAULT_PROFILE = "default"
SOURCE_DECLARATIONS_ENV = "ASTRID_SOURCE_DECLARATIONS"
SOURCE_STATE_ENV = "ASTRID_SOURCE_STATE"
SOURCE_DATA_ENV = "ASTRID_SOURCE_DATA"
_OID_LENGTHS = {40, 64}


class SourceSetupError(RuntimeError):
    """A bounded source acquisition, validation, or inventory failure."""


@dataclass(frozen=True)
class SourceDeclaration:
    """An immutable external source identity selected by setup policy."""

    pack_id: str
    repository: str
    revision: str
    pack_subpath: str = "."
    manifest_sha256: str | None = None
    tree_sha256: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceDeclaration":
        required = ("pack_id", "repository", "revision")
        if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
            raise SourceSetupError("source declaration requires non-blank pack_id, repository, and revision")
        revision = str(value["revision"]).strip().lower()
        if len(revision) not in _OID_LENGTHS or any(char not in "0123456789abcdef" for char in revision):
            raise SourceSetupError("source revision must be a full immutable Git object id")
        pack_id = str(value["pack_id"]).strip()
        if not pack_id or "/" in pack_id or "\\" in pack_id or pack_id.startswith("."):
            raise SourceSetupError(f"invalid source pack id {pack_id!r}")
        subpath = str(value.get("pack_subpath", ".")).strip() or "."
        if subpath != "." and (
            PureWindowsPath(subpath).drive
            or subpath.startswith("/")
            or "\\" in subpath
            or any(part in {"", ".", ".."} for part in subpath.split("/"))
        ):
            raise SourceSetupError("source pack_subpath must be a safe relative POSIX path")
        if subpath == ".":
            normalized_subpath = "."
        else:
            normalized_subpath = "/".join(subpath.split("/"))
        return cls(
            pack_id=pack_id,
            repository=str(value["repository"]).strip(),
            revision=revision,
            pack_subpath=normalized_subpath,
            manifest_sha256=_digest_field(value.get("manifest_sha256"), "manifest_sha256"),
            tree_sha256=_digest_field(value.get("tree_sha256"), "tree_sha256"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "pack_id": self.pack_id,
            "repository": self.repository,
            "revision": self.revision,
            "pack_subpath": self.pack_subpath,
        }
        if self.manifest_sha256:
            result["manifest_sha256"] = self.manifest_sha256
        if self.tree_sha256:
            result["tree_sha256"] = self.tree_sha256
        return result


@dataclass(frozen=True)
class InstalledSource:
    """One validated active managed pack root."""

    pack_id: str
    pack_root: Path
    repository: str
    revision: str
    pack_subpath: str
    manifest_sha256: str
    tree_sha256: str
    source_kind: str = "managed"

    @property
    def identity(self) -> str:
        return f"{self.repository}@{self.revision}#{self.pack_subpath}:{self.pack_root}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_id": self.pack_id,
            "pack_root": str(self.pack_root),
            "repository": self.repository,
            "revision": self.revision,
            "pack_subpath": self.pack_subpath,
            "manifest_sha256": self.manifest_sha256,
            "tree_sha256": self.tree_sha256,
            "source_kind": self.source_kind,
        }


@dataclass(frozen=True)
class ManagedSourceInventory:
    """The verified, read-only managed source selection for one profile."""

    sources: tuple[InstalledSource, ...]
    identity: str

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(source.pack_root for source in self.sources)


def _digest_field(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise SourceSetupError(f"{name} must be a lowercase SHA-256 hex digest")
    return value.lower()


def source_state_path(*, profile: str = DEFAULT_PROFILE, path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    configured = os.environ.get(SOURCE_STATE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return (state_home / "astrid" / profile / "pack-sources.json").resolve()


def source_data_root(*, path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()
    configured = os.environ.get(SOURCE_DATA_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (data_home / "astrid" / "packs").resolve()


def _empty_state() -> dict[str, Any]:
    return {"version": SOURCE_STATE_VERSION, "active": {}, "cached": {}, "disabled": []}


def load_source_state(path: str | Path | None = None) -> dict[str, Any]:
    target = source_state_path(path=path)
    if not target.exists():
        return _empty_state()
    try:
        data = read_json(target)
    except (OSError, ValueError, TypeError) as exc:
        raise SourceSetupError(f"invalid source state {target}") from exc
    if not isinstance(data, dict) or data.get("version") != SOURCE_STATE_VERSION:
        raise SourceSetupError(f"unsupported source state {target}")
    for key in ("active", "cached"):
        if not isinstance(data.get(key), dict):
            raise SourceSetupError(f"source state field {key!r} must be an object")
    if not isinstance(data.get("disabled", []), list):
        raise SourceSetupError("source state field 'disabled' must be an array")
    return data


def _write_state(data: Mapping[str, Any], path: str | Path | None = None) -> None:
    write_json_atomic(source_state_path(path=path), dict(data))


def _run_git(repository: str, *args: str, cwd: Path | None = None) -> str:
    # Only the four inventory reads may use the original command owner's
    # private channel. Custody failures deliberately bypass SourceSetupError:
    # _tree_digest must never turn a failed ownership proof into a fallback.
    from astrid.core.execution.custody_broker import command_inventory_git
    delegated = command_inventory_git(args, cwd=cwd)
    try:
        result = delegated if delegated is not None else subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None,
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceSetupError("git source operation could not be started") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SourceSetupError(f"git source operation failed: {detail or 'unknown git error'}")
    return result.stdout.strip()


def _tree_digest(root: Path) -> str:
    """Hash the immutable checkout contents without generated cache files.

    Managed packs are Python packages, so importing one can create ignored
    ``__pycache__`` bytecode in an otherwise clean checkout.  The source
    inventory already rejects tracked or non-ignored working-tree changes via
    ``git status``; the digest therefore needs to cover the tracked tree only
    so ordinary interpreter caches do not invalidate an installed pack.
    """
    digest = hashlib.sha256()
    root = root.resolve()
    try:
        repo_root = Path(_run_git("", "-C", str(root), "rev-parse", "--show-toplevel")).resolve()
        tracked = _run_git("", "-C", str(root), "ls-files", "--full-name", "-z").split("\0")
        paths = []
        for item in tracked:
            if not item:
                continue
            path = repo_root / item
            try:
                relative_path = path.relative_to(root)
            except ValueError:
                continue
            if path.is_file():
                paths.append((relative_path.as_posix(), path))
    except SourceSetupError:
        paths = [
            (path.relative_to(root).as_posix(), path)
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        ]
    for relative_name, path in sorted(paths):
        relative = relative_name.encode()
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def _materialized_root(checkout: Path, declaration: SourceDeclaration) -> Path:
    return checkout if declaration.pack_subpath == "." else checkout.joinpath(*declaration.pack_subpath.split("/"))


def _validate_checkout(checkout: Path, declaration: SourceDeclaration) -> InstalledSource:
    if checkout.is_symlink() or not checkout.is_dir():
        raise SourceSetupError(f"managed source checkout is unavailable: {checkout}")
    revision = _run_git("", "-C", str(checkout), "rev-parse", "HEAD")
    if revision != declaration.revision:
        raise SourceSetupError(
            f"managed source revision mismatch for {declaration.pack_id!r}: {revision} != {declaration.revision}"
        )
    status = _run_git("", "-C", str(checkout), "status", "--porcelain=v1")
    if status:
        raise SourceSetupError(f"managed source checkout is dirty: {checkout}")
    pack_root = _materialized_root(checkout, declaration)
    manifest = pack_root / "pack.yaml"
    if pack_root.is_symlink() or not pack_root.is_dir() or manifest.is_symlink():
        raise SourceSetupError(f"managed source pack root is unavailable: {pack_root}")
    try:
        read_normalize_validate(
            manifest, source=ExternalPackSource.MANAGED,
            expected_pack_id=declaration.pack_id,
        )
    except CanonicalPackValidationError as exc:
        raise SourceSetupError(f"managed source failed canonical v2/v3 admission: {exc}") from exc
    manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    tree_digest = _tree_digest(pack_root)
    if declaration.manifest_sha256 and declaration.manifest_sha256 != manifest_digest:
        raise SourceSetupError(f"managed source manifest digest mismatch for {declaration.pack_id!r}")
    if declaration.tree_sha256 and declaration.tree_sha256 != tree_digest:
        raise SourceSetupError(f"managed source tree digest mismatch for {declaration.pack_id!r}")
    return InstalledSource(
        declaration.pack_id, pack_root.resolve(), declaration.repository,
        declaration.revision, declaration.pack_subpath, manifest_digest, tree_digest,
    )


def _cached_checkout(declaration: SourceDeclaration, *, data_root: Path) -> Path:
    return data_root / declaration.pack_id / declaration.revision


def _stage_checkout(declaration: SourceDeclaration, *, data_root: Path) -> InstalledSource:
    destination_parent = _cached_checkout(declaration, data_root=data_root).parent
    destination_parent.mkdir(parents=True, exist_ok=True)
    # Canonical v2/v3 pack roots must be non-hidden; the staging directory is
    # validated before activation, so it must obey that rule too.
    temporary = Path(tempfile.mkdtemp(prefix=f"stage-{declaration.pack_id}-", dir=destination_parent))
    try:
        _run_git("", "clone", "--quiet", "--no-hardlinks", "--no-checkout", declaration.repository, str(temporary))
        _run_git("", "-C", str(temporary), "checkout", "--quiet", "--detach", declaration.revision)
        _validate_checkout(temporary, declaration)
        final = _cached_checkout(declaration, data_root=data_root)
        if final.exists() or final.is_symlink():
            existing = _validate_checkout(final, declaration)
            shutil.rmtree(temporary)
            return existing
        temporary.replace(final)
        return _validate_checkout(final, declaration)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _record_from_mapping(value: Mapping[str, Any]) -> InstalledSource:
    raw_root = Path(str(value.get("pack_root", ""))).expanduser()
    if not raw_root.is_absolute() or raw_root.is_symlink():
        raise SourceSetupError("active source pack_root must be an absolute non-symlink directory")
    fields = ("pack_id", "repository", "revision", "pack_subpath", "manifest_sha256", "tree_sha256")
    if any(not isinstance(value.get(field), str) or not value[field] for field in fields):
        raise SourceSetupError("active source record is incomplete")
    declaration = SourceDeclaration.from_mapping(
        {
            "pack_id": value["pack_id"],
            "repository": value["repository"],
            "revision": value["revision"],
            "pack_subpath": value["pack_subpath"],
            "manifest_sha256": value["manifest_sha256"],
            "tree_sha256": value["tree_sha256"],
        }
    )
    return InstalledSource(
        declaration.pack_id, raw_root.resolve(), declaration.repository, declaration.revision,
        declaration.pack_subpath, declaration.manifest_sha256 or "", declaration.tree_sha256 or "",
    )


def active_sources(*, state_path: str | Path | None = None, verify: bool = True) -> tuple[InstalledSource, ...]:
    """Return the active managed roots, read-only and in stable id order."""
    state = load_source_state(state_path)
    disabled = {str(item) for item in state.get("disabled", [])}
    result: list[InstalledSource] = []
    for pack_id, raw in sorted(state["active"].items()):
        if pack_id in disabled:
            continue
        source = _record_from_mapping(raw)
        if source.pack_id != pack_id:
            raise SourceSetupError(f"source state key does not match pack id {pack_id!r}")
        if verify:
            declaration = SourceDeclaration(
                source.pack_id, source.repository, source.revision,
                source.pack_subpath, source.manifest_sha256, source.tree_sha256,
            )
            source = _validate_checkout(next_parent_checkout(source.pack_root, declaration), declaration)
        result.append(source)
    return tuple(result)


def active_source_inventory(
    *, state_path: str | Path | None = None, verify: bool = True
) -> ManagedSourceInventory:
    """Resolve the selected managed roots once for discovery/host callers."""
    sources = active_sources(state_path=state_path, verify=verify)
    return ManagedSourceInventory(sources, source_inventory_identity(sources))


def next_parent_checkout(pack_root: Path, declaration: SourceDeclaration) -> Path:
    if declaration.pack_subpath == ".":
        return pack_root
    checkout = pack_root
    for _ in declaration.pack_subpath.split("/"):
        checkout = checkout.parent
    return checkout


def source_inventory_identity(sources: Iterable[InstalledSource]) -> str:
    payload = [source.to_dict() for source in sorted(sources, key=lambda item: item.pack_id)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def provision(
    declarations: Iterable[SourceDeclaration] = (),
    *,
    offline: bool = False,
    check: bool = False,
    disable_pack: str | None = None,
    restore_pack: str | None = None,
    state_path: str | Path | None = None,
    data_root: str | Path | None = None,
) -> dict[str, Any]:
    """Stage/validate/activate declarations without disturbing prior state."""
    declarations = tuple(declarations)
    by_id = {item.pack_id: item for item in declarations}
    if len(by_id) != len(declarations):
        raise SourceSetupError("duplicate source declaration pack id")
    state = load_source_state(state_path)
    if disable_pack:
        state["active"].pop(disable_pack, None)
        state["disabled"] = sorted(set(state.get("disabled", [])) | {disable_pack})
        if not check:
            _write_state(state, state_path)
        report = inventory_report(state, check=check)
        return report
    if restore_pack:
        if restore_pack not in by_id:
            raise SourceSetupError(f"no declaration for pack {restore_pack!r}")
        state["disabled"] = [item for item in state.get("disabled", []) if item != restore_pack]
    data_path = source_data_root(path=data_root)
    errors: list[str] = []
    for declaration in declarations:
        if declaration.pack_id in state.get("disabled", []):
            continue
        try:
            final = _cached_checkout(declaration, data_root=data_path)
            if final.is_dir() and not final.is_symlink():
                source = _validate_checkout(final, declaration)
            elif offline or check:
                raise SourceSetupError("verified cached revision is missing")
            else:
                source = _stage_checkout(declaration, data_root=data_path)
            state["cached"][declaration.pack_id] = source.to_dict()
            state["active"][declaration.pack_id] = source.to_dict()
        except SourceSetupError as exc:
            errors.append(str(exc))
        if errors:
            if check:
                report = inventory_report(state, check=True)
                report["ok"] = False
                report["errors"] = errors
                return report
            raise SourceSetupError("; ".join(errors))
    if not check:
        _write_state(state, state_path)
    return inventory_report(state, check=check)


def inventory_report(state: Mapping[str, Any] | None = None, *, check: bool = True) -> dict[str, Any]:
    state = state if state is not None else load_source_state()
    active = []
    for pack_id, raw in sorted((state.get("active") or {}).items()):
        if pack_id in set(state.get("disabled", [])):
            continue
        try:
            source = _record_from_mapping(raw)
            declaration = SourceDeclaration(
                source.pack_id,
                source.repository,
                source.revision,
                source.pack_subpath,
                source.manifest_sha256,
                source.tree_sha256,
            )
            active.append(
                _validate_checkout(
                    next_parent_checkout(source.pack_root, declaration), declaration
                ).to_dict()
            )
        except SourceSetupError as exc:
            active.append({"pack_id": pack_id, "ok": False, "error": str(exc)})
    return {
        "ok": not any(item.get("ok") is False for item in active),
        "changed": not check,
        "active": active,
        "disabled": sorted(str(item) for item in state.get("disabled", [])),
        "source_inventory_identity": source_inventory_identity(
            [_record_from_mapping(item) for item in active if item.get("ok", True) is not False]
        ),
    }


def declarations_from_json(path: str | Path | None = None) -> tuple[SourceDeclaration, ...]:
    configured = str(path or os.environ.get(SOURCE_DECLARATIONS_ENV, "")).strip()
    if not configured:
        return ()
    value = json.loads(Path(configured).expanduser().read_text(encoding="utf-8"))
    if isinstance(value, Mapping):
        value = value.get("sources", ())
    if not isinstance(value, list):
        raise SourceSetupError("source declarations must be a JSON array")
    declarations: list[SourceDeclaration] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise SourceSetupError(f"source declaration at index {index} must be an object")
        declarations.append(SourceDeclaration.from_mapping(item))
    return tuple(declarations)


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m astrid.core.pack.source_setup")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--disable-pack")
    parser.add_argument("--restore-pack")
    parser.add_argument("--declarations", type=Path)
    args = parser.parse_args(argv)
    try:
        result = provision(
            declarations_from_json(args.declarations), offline=args.offline,
            check=args.check, disable_pack=args.disable_pack, restore_pack=args.restore_pack,
        )
    except (OSError, SourceSetupError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok", False) else 1


__all__ = [
    "DEFAULT_PROFILE",
    "InstalledSource", "ManagedSourceInventory", "SOURCE_DATA_ENV",
    "SOURCE_DECLARATIONS_ENV", "SOURCE_STATE_ENV", "SourceDeclaration", "SourceSetupError",
    "active_source_inventory", "active_sources",
    "declarations_from_json",
    "inventory_report", "provision", "source_data_root",
    "source_inventory_identity", "source_state_path",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
