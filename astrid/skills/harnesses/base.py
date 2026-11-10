"""Abstract base for the per-harness install adapters."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Literal, Optional

from astrid.core.contracts.errors import AstridError

from ..discovery import SkillDescriptor

def sync_target_name(descriptor: SkillDescriptor) -> str:
    """Return the shared harness directory name for a skill."""
    if descriptor.pack_id == "_core":
        return "astrid"
    return descriptor.sync_name or f"astrid-{descriptor.skill_id}"


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
    """Select names in the skill namespace, without claiming ownership.

    Replacement/removal still requires evidence about the current entry.
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
    skills_dir: Path, known_pack_ids: set[str], *,
    owned_sources: dict[Path, set[Path]] | None = None,
    known_names: set[str] | None = None,
    declared_sources: Iterable[Path] = (),
    conflicts: list[PlannedStep] | None = None,
) -> list[Path]:
    """Prune vanished units only when their current entry has ownership proof."""
    removed: list[Path] = []
    if not skills_dir.is_dir():
        return removed
    for entry in sorted(skills_dir.iterdir()):
        pack_id = ours_link_to_pack_id(entry.name)
        if entry.name in (known_names or set()) or pack_id is None or pack_id == "_core" or pack_id in known_pack_ids:
            continue
        try:
            changed = remove_symlink(entry, sources=(*declared_sources, *(owned_sources or {}).get(entry, ())))
        except AstridError as exc:
            if conflicts is not None:
                conflicts.append(PlannedStep(str(exc), target=entry,
                    extras={"changed": False, "conflict": str(exc)}))
            continue
        if changed:
            removed.append(entry)
    return removed


def is_managed_copy(target: Path, source: Path | None = None) -> bool:
    if not target.is_dir() or target.is_symlink():
        return False
    try:
        marker = json.loads((target / _COPY_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if (not isinstance(marker, dict) or not isinstance(marker.get("digest"), str)
            or not isinstance(marker.get("source"), str) or not Path(marker["source"]).is_absolute()
            or (target / _COPY_MARKER).is_symlink()):
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


def _resolved(path: Path) -> Path | None:
    try:
        # Missing declared sources still identify owned broken links. Do not
        # use strict=True or infer ownership from the destination's spelling.
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def require_owned_target(target: Path, sources: Iterable[Path] = ()) -> None:
    """Refuse replacement/removal without evidence about the current entry.

    Sources are canonical paths held by records or selected declarations; do
    not resolve them again through a subsequently retargeted source symlink.
    A record of the destination alone is insufficient after retargeting. An
    intact copy marker proves its bytes even if its source has since changed.
    """
    if target.is_symlink():
        linked = _resolved(target)
        if linked is not None and any(linked == Path(os.path.abspath(source)) for source in sources):
            return
        raise AstridError(f"Foreign skill link preserved (target not confirmed Astrid-owned): {target}")
    if edited_managed_copy(target):
        raise AstridError(f"edited Astrid managed copy preserved: {target}")
    if is_managed_copy(target):
        return
    if target.exists():
        raise AstridError(f"Skill target is occupied by a foreign entry; preserved: {target}")


def ensure_symlink(
    target: Path, source: Path, *, force: bool = False, mechanism: str = "symlink",
    owned_sources: Iterable[Path] = (),
) -> bool:
    """Create a link/copy, replacing only an entry with exact ownership proof.

    ``force`` is retained for CLI compatibility; it cannot grant ownership.
    """
    if mechanism not in {"symlink", "copy"}:
        raise ValueError(f"Unknown skill mechanism: {mechanism}")
    source_resolved = source.resolve()
    require_owned_target(target, (source_resolved, *owned_sources))
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
    if mechanism == "symlink" and target.is_symlink() and _resolved(target) == source_resolved:
        return False
    if target.is_symlink():
        target.unlink()
    elif is_managed_copy(target):
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    copy_mode = "copy"
    if mechanism == "symlink":
        try:
            target.symlink_to(source_resolved, target_is_directory=source_resolved.is_dir())
            return True
        except (OSError, NotImplementedError) as exc:
            fallback_errors = {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.EOPNOTSUPP}
            if isinstance(exc, OSError) and exc.errno not in fallback_errors and getattr(exc, "winerror", None) != 1314:
                raise
            copy_mode = "symlink-fallback"
    shutil.copytree(source_resolved, target)
    (target / _COPY_MARKER).write_text(
        json.dumps({"source": str(source_resolved), "digest": _directory_digest(source_resolved),
                    "mechanism": copy_mode}, sort_keys=True) + "\n", encoding="utf-8",
    )
    return True


def remove_symlink(target: Path, *, sources: Iterable[Path] = (), dry_run: bool = False) -> bool:
    require_owned_target(target, sources)
    if target.is_symlink():
        if not dry_run:
            target.unlink()
        return True
    if is_managed_copy(target):
        if not dry_run:
            shutil.rmtree(target)
        return True
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
    "require_owned_target",
]
