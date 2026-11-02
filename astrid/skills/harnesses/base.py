"""Abstract base for the per-harness install adapters."""

from __future__ import annotations

import errno
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Optional

from astrid.core.contracts.errors import AstridError

from ..discovery import SkillDescriptor

Action = Literal["install", "uninstall"]
_COPY_MARKER = ".astrid-managed-skill.json"


def _directory_digest(root: Path, *, exclude_marker: bool = False) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if exclude_marker and relative == _COPY_MARKER:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + str(path.readlink()).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0" + path.read_bytes())
        else:
            digest.update(b"dir\0")
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class PlannedStep:
    description: str
    target: Path | None = None
    extras: dict = field(default_factory=dict)


@dataclass(frozen=True)
class InstallRecord:
    """Filesystem-discovered evidence that a pack is installed for a harness.

    Returned by ``HarnessAdapter.discover_installed``. Mirrors the shape we
    persist in the state file (target/mechanism) but is reconstructed purely
    from on-disk reality, so it can be used to detect and self-heal drift.
    """

    pack_id: str
    target: Path
    mechanism: str


class HarnessAdapter:
    name: str = ""

    def __init__(self, home: Path | None = None) -> None:
        self._home = home

    @property
    def home(self) -> Path:
        return self._home if self._home is not None else self._default_home()

    def _default_home(self) -> Path:  # pragma: no cover - overridden
        raise NotImplementedError

    def detect(self) -> bool:
        return self.home.exists() and self.home.is_dir()

    def target_for(self, descriptor: SkillDescriptor) -> Path:  # pragma: no cover - overridden
        raise NotImplementedError

    def plan(self, action: Action, descriptors: Iterable[SkillDescriptor], **opts) -> list[PlannedStep]:  # pragma: no cover - overridden
        raise NotImplementedError

    def apply(self, action: Action, descriptors: Iterable[SkillDescriptor], **opts) -> list[PlannedStep]:  # pragma: no cover - overridden
        raise NotImplementedError

    def verify(self, descriptor: SkillDescriptor) -> tuple[bool, str]:  # pragma: no cover - overridden
        raise NotImplementedError

    def discover_installed(self, descriptor: SkillDescriptor) -> Optional[InstallRecord]:
        """Probe the filesystem and return an InstallRecord if the pack is installed.

        Default implementation reuses ``verify`` semantics: if verify passes,
        the install is considered present at ``target_for(descriptor)`` with
        mechanism "link". Adapters with multi-mechanism or composite
        evidence (codex AGENTS.md, hermes external-dir) override.
        """
        try:
            ok, _ = self.verify(descriptor)
        except Exception:
            return None
        if not ok:
            return None
        return InstallRecord(
            pack_id=descriptor.pack_id,
            target=self.target_for(descriptor),
            mechanism="copy" if is_managed_copy(self.target_for(descriptor)) else "symlink",
        )


def is_ours(link_name: str) -> bool:
    """Return True when *link_name* is a skill entry this layer manages.

    "Ours" is the gateway ``astrid`` link and any ``astrid-<pack>`` per-pack
    link. Foreign entries (e.g. a global ``image-generation`` skill) are never
    matched, so the prune/refresh logic can refuse to touch them.
    """
    return link_name == "astrid" or link_name.startswith("astrid-")


def ours_link_to_pack_id(link_name: str) -> str | None:
    """Map an ``astrid``/``astrid-<pack>`` link name back to its pack id.

    Returns ``"_core"`` for the gateway ``astrid`` link, the suffix for an
    ``astrid-<pack>`` link, or ``None`` when the name is not ours.
    """
    if link_name == "astrid":
        return "_core"
    if link_name.startswith("astrid-"):
        return link_name[len("astrid-") :]
    return None


def prune_orphan_skill_links(
    skills_dir: Path, known_pack_ids: set[str], *, owned_targets: set[Path] | None = None,
) -> list[Path]:
    """Remove ``astrid-*`` symlinks whose pack id is not in *known_pack_ids*.

    Only our own symlinks are considered, and only symlinks are removed; a
    foreign entry or a real (non-symlink) file/dir is never touched. The
    gateway ``astrid`` link is never pruned here (it is always re-linked).
    Returns the list of removed link paths.
    """
    removed: list[Path] = []
    if not skills_dir.is_dir():
        return removed
    for entry in sorted(skills_dir.iterdir()):
        name = entry.name
        if name == "astrid" or not name.startswith("astrid-"):
            continue
        if not entry.is_symlink() and not is_managed_copy(entry):
            continue
        pack_id = ours_link_to_pack_id(name)
        if pack_id is not None and pack_id not in known_pack_ids:
            if is_managed_copy(entry):
                shutil.rmtree(entry)
            else:
                try:
                    linked = entry.resolve(strict=True)
                except OSError:
                    # A stale install record is useful only when the current
                    # link still names that recorded pack. An unrelated or
                    # unresolved user link at the same path remains untouched.
                    unresolved = entry.readlink()
                    unresolved = unresolved if unresolved.is_absolute() else entry.parent / unresolved
                    recorded_target = any(
                        entry == target or entry.resolve(strict=False) == target.resolve(strict=False)
                        for target in (owned_targets or set())
                    )
                    if not recorded_target or pack_id not in unresolved.parts:
                        continue
                    entry.unlink()
                    removed.append(entry)
                    continue
                if "astrid" not in linked.parts and entry not in (owned_targets or set()):
                    continue
                entry.unlink()
            removed.append(entry)
    return removed


def is_managed_copy(target: Path, source: Path | None = None) -> bool:
    if not target.is_dir() or target.is_symlink():
        return False
    try:
        marker = json.loads((target / _COPY_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(marker, dict) or not isinstance(marker.get("digest"), str):
        return False
    if _directory_digest(target, exclude_marker=True) != marker["digest"]:
        return False
    return source is None or (
        marker.get("source") == str(source.resolve())
        and marker.get("digest") == _directory_digest(source)
    )


def edited_managed_copy(target: Path) -> bool:
    """Whether a valid Astrid marker remains but the copy no longer matches it."""
    try:
        marker = json.loads((target / _COPY_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(marker, dict)
        and isinstance(marker.get("source"), str)
        and isinstance(marker.get("digest"), str)
        and _directory_digest(target, exclude_marker=True) != marker["digest"]
    )


def _managed_copy_mode(target: Path) -> str | None:
    try:
        marker = json.loads((target / _COPY_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = marker.get("mechanism") if isinstance(marker, dict) else None
    return mode if mode in {"copy", "symlink-fallback"} else None


def managed_target_matches(target: Path, source: Path) -> bool:
    if target.is_symlink():
        try:
            return target.resolve(strict=True) == source.resolve()
        except OSError:
            return False
    return is_managed_copy(target, source)


def target_mechanism(target: Path, default: str = "symlink") -> str:
    return "copy" if is_managed_copy(target) else default


def ensure_symlink(target: Path, source: Path, *, force: bool = False, mechanism: str = "symlink") -> bool:
    """Create a symlink to ``source``, falling back to a managed directory copy."""
    # Returns True if the FS state changed, False if already correct.
    target.parent.mkdir(parents=True, exist_ok=True)
    source_resolved = source.resolve()
    if mechanism not in {"symlink", "copy"}:
        raise ValueError(f"Unknown skill mechanism: {mechanism}")
    if is_managed_copy(target, source_resolved):
        mode = _managed_copy_mode(target)
        if mechanism == "copy":
            if mode != "copy":
                marker_path = target / _COPY_MARKER
                marker = json.loads(marker_path.read_text(encoding="utf-8"))
                marker["mechanism"] = "copy"
                marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
            return False
        if mode != "copy":  # Keep an existing symlink fallback stable.
            return False
    if edited_managed_copy(target):
        raise AstridError(f"Edited Astrid managed copy preserved: {target}")
    if mechanism == "copy":
        if target.is_symlink():
            try:
                linked = target.resolve(strict=True)
                if linked != source_resolved and "astrid" not in linked.parts:
                    raise AstridError(f"Foreign skill link preserved: {target}")
            except OSError:
                raise AstridError(f"Foreign skill link preserved: {target}") from None
            target.unlink()
        elif target.exists() and not is_managed_copy(target):
            if not force:
                raise AstridError(f"Skill target is occupied by a foreign entry: {target}")
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if target.exists() or target.is_symlink():
            shutil.rmtree(target) if target.is_dir() and not target.is_symlink() else target.unlink()
        shutil.copytree(source_resolved, target)
        (target / _COPY_MARKER).write_text(
            json.dumps({"source": str(source_resolved), "digest": _directory_digest(source_resolved),
                        "mechanism": "copy"}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return True
    if target.is_symlink():
        try:
            existing = Path(target).resolve()
        except OSError:
            existing = None
        if existing == source_resolved:
            return False
        if existing is None or "astrid" not in existing.parts:
            raise AstridError(f"Foreign skill link preserved: {target}")
        target.unlink()
    elif is_managed_copy(target):
        shutil.rmtree(target)
    elif target.exists():
        if not force:
            raise AstridError(
                f"{target} exists and is not a symlink",
                recovery_command="re-run with --force to replace it",
            )
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    try:
        target.symlink_to(source_resolved, target_is_directory=source_resolved.is_dir())
    except (OSError, NotImplementedError) as exc:
        fallback_errors = {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EOPNOTSUPP}
        if isinstance(exc, OSError) and exc.errno not in fallback_errors and getattr(exc, "winerror", None) != 1314:
            raise
        shutil.copytree(source_resolved, target)
        (target / _COPY_MARKER).write_text(
            json.dumps(
                {"source": str(source_resolved), "digest": _directory_digest(source_resolved),
                 "mechanism": "symlink-fallback"},
                sort_keys=True,
            ) + "\n",
            encoding="utf-8",
        )
    return True


def remove_symlink(target: Path) -> bool:
    if target.is_symlink():
        target.unlink()
        return True
    if is_managed_copy(target):
        shutil.rmtree(target)
        return True
    if target.exists():
        # Refuse to remove a non-symlink we didn't create.
        return False
    return False


__all__ = [
    "Action",
    "HarnessAdapter",
    "InstallRecord",
    "PlannedStep",
    "ensure_symlink",
    "edited_managed_copy",
    "is_managed_copy",
    "managed_target_matches",
    "target_mechanism",
    "is_ours",
    "ours_link_to_pack_id",
    "prune_orphan_skill_links",
    "remove_symlink",
]
