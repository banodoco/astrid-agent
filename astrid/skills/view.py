"""Build the writable skill view consumed by agent harnesses.

The package checkout (and an installed wheel) is an input, never a generated
output location.  A view contains a real root ``SKILL.md``, real copies of the
core and pack guidance files whose links and commands are rewritten for the view.

Every relative link in a materialised file is resolved from where it lives in
the checkout and then re-pointed at what it means in the view:

* a composed pack skill      -> ``packs/<id>/...`` inside the view;
* another materialised file  -> its view-relative path;
* any other existing file    -> its absolute checkout path (docs, config, ...);
* a ``packs/<id>/...`` route into a pack that is not available -> the link text
  is kept but the link is dropped, so the view never advertises a dead route.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from astrid.core.contracts.errors import AstridError

from .commands import rewrite_commands
from .discovery import SkillDescriptor
from .harnesses.base import PlannedStep
from .links import DROP, iter_link_targets, iter_markdown_files, rewrite_links, split_target

# Core guidance copied into every view.  Keys are view-relative paths; the
# root ``SKILL.md`` is written separately from the descriptor's skill_md.
_CORE_FILES = (
    "creative-work/SKILL.md",
    "pack-builder/SKILL.md",
    "pack-testing/SKILL.md",
    "references/capabilities.md",
)
_PACKS_REGISTRY = "creative-work/references/packs.md"

# The pack-builder and pack-testing guides point at the canonical public docs
# rather than at checkout paths: the guides, scripts and tests they link are not
# shipped in a clean wheel.
_PACK_BUILDER_DOC_PREFIX = "https://github.com/banodoco/astrid-agent/blob/main/"
_PACK_BUILDER_DOC_REWRITES = {
    f"../../../../../{top}": _PACK_BUILDER_DOC_PREFIX + top
    for top in ("docs/", "scripts/", "tests/", "pyproject.toml")
}
_PACK_AUTHORING_FILES = frozenset({"pack-builder/SKILL.md", "pack-testing/SKILL.md"})

_NEEDS_ANGLE_BRACKETS = re.compile(r"[\s()<>]")


def _relative_within(path: Path, root: Path) -> str | None:
    """Return *path* relative to *root* (``"."`` for root itself), else None."""
    if path == root:
        return "."
    if root in path.parents:
        return path.relative_to(root).as_posix()
    return None


def _format_target(path: str, tail: str) -> str:
    text = path + tail
    return f"<{text}>" if _NEEDS_ANGLE_BRACKETS.search(text) else text


def _relative_target(target: Path, dest_dir: Path, tail: str) -> str:
    return _format_target(Path(os.path.relpath(target, dest_dir)).as_posix(), tail)


@dataclass(frozen=True)
class _ViewLinks:
    """Decides what a link in a materialised view file should point at."""

    root: Path  # view root, as the harness sees it
    core_dir: Path  # physical source directory of the core skill
    packs: dict[str, Path]  # composed pack id -> physical pack skill directory
    materialised: frozenset[str]  # view-relative core files written into the view

    def target(self, raw: str, *, source: Path, dest: Path) -> object:
        path, tail = split_target(raw)
        if path is None or os.path.isabs(path):
            return None
        dest_dir = dest.parent
        source_target = Path(os.path.normpath(source.parent / path))

        # 1. The link means a file in the checkout: a composed pack skill or a
        #    core file that the view materialises.
        for pack_id, skill_dir in self.packs.items():
            inner = _relative_within(source_target, skill_dir)
            if inner is not None:
                return _relative_target(self.root / "packs" / pack_id / inner, dest_dir, tail)
        core_rel = _relative_within(source_target, self.core_dir)
        if core_rel is not None and core_rel in self.materialised:
            return _relative_target(self.root / core_rel, dest_dir, tail)

        # 2. Any other real file in the checkout is linked absolutely.
        if os.path.exists(source_target):
            return _format_target(os.path.normpath(str(source_target)), tail)

        # 3. The link was written against the view itself (``packs/<id>/...``).
        view_target = Path(os.path.normpath(dest_dir / path))
        pack_rel = _relative_within(view_target, self.root / "packs")
        if pack_rel is not None:
            pack_id = pack_rel.split("/", 1)[0]
            return None if pack_id in self.packs else DROP
        if _relative_within(view_target, self.root) in self.materialised:
            return None

        # 4. Unresolvable: point at the absolute location so the link check
        #    reports exactly where it was expected.
        return _format_target(os.path.normpath(str(source_target)), tail)


def _write_view_file(target: Path, text: str) -> None:
    """Write a regular file in the view, never through a symlink into the source."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    target.write_text(text, encoding="utf-8")


def _materialise(source: Path, target: Path, links: _ViewLinks, *, pre: dict[str, str] | None = None) -> None:
    text = source.read_text(encoding="utf-8")
    for old, new in (pre or {}).items():
        text = text.replace(old, new)
    text = rewrite_links(text, lambda raw: links.target(raw, source=source, dest=target))
    _write_view_file(target, rewrite_commands(text))


def _materialise_pack(target: Path, source: Path, links: _ViewLinks, *, managed: bool) -> None:
    if target.exists() or target.is_symlink():
        if target.is_symlink():
            target.unlink()
        elif managed and target.is_dir():
            shutil.rmtree(target)
        else:
            raise AstridError(
                f"Astrid skill view target is occupied: {target}",
                recovery_command="remove the foreign entry or choose another profile",
            )
    shutil.copytree(source, target)
    for md in iter_markdown_files(source):
        _materialise(md, target / md.relative_to(source), links)


def view_descriptors(root: Path, packs: list[SkillDescriptor]) -> list[SkillDescriptor]:
    """Return descriptors addressed through the generated view."""
    return [
        replace(
            descriptor,
            skill_dir=root / "packs" / descriptor.pack_id,
            skill_md=root / "packs" / descriptor.pack_id / "SKILL.md",
        )
        for descriptor in packs
    ]


def route_pack_ids(core: SkillDescriptor, packs: Iterable[SkillDescriptor]) -> set[str]:
    """Return ids of the packs that the gateway or the creative-work router links to.

    These routes must resolve in the default view, so the packs are composed
    into it even when no per-pack harness link (``astrid-<pack>``) was asked for.
    """
    core_dir = Path(os.path.realpath(core.skill_dir))
    pack_dirs = {
        d.pack_id: Path(os.path.realpath(d.skill_dir)) for d in packs if d.pack_id != "_core"
    }
    found: set[str] = set()
    for source in (core_dir / "SKILL.md", core_dir / "creative-work" / "SKILL.md"):
        if not source.is_file():
            continue
        for _line, raw in iter_link_targets(source.read_text(encoding="utf-8")):
            path, _tail = split_target(raw)
            if path is None or os.path.isabs(path):
                continue
            target = Path(os.path.normpath(source.parent / path))
            for pack_id, skill_dir in pack_dirs.items():
                if _relative_within(target, skill_dir) is not None:
                    found.add(pack_id)
    return found


def compose_view(
    root: Path,
    core: SkillDescriptor,
    packs: list[SkillDescriptor],
    *,
    dry_run: bool = False,
) -> tuple[SkillDescriptor, list[PlannedStep], list[SkillDescriptor]]:
    """Compose one harness view and return ``(gateway, steps, view_packs)``.

    Pack copies are keyed by pack id and are therefore safe to reconcile without
    touching unrelated user files.  The returned gateway descriptor points at
    the writable view, so registry writes cannot follow a symlink into source.
    """
    steps: list[PlannedStep] = []
    core_dir = Path(os.path.realpath(core.skill_dir))
    materialised = {"SKILL.md"}
    for rel in (*_CORE_FILES, _PACKS_REGISTRY):
        if (core_dir / rel).is_file():
            materialised.add(rel)
    links = _ViewLinks(
        root=root,
        core_dir=core_dir,
        packs={d.pack_id: Path(os.path.realpath(d.skill_dir)) for d in packs},
        materialised=frozenset(materialised),
    )

    view_packs = view_descriptors(root, packs)
    enabled_ids = {descriptor.pack_id for descriptor in view_packs}
    marker = root / ".astrid-managed-packs"
    previous_ids = set()
    if marker.is_file():
        previous_ids = {line.strip() for line in marker.read_text(encoding="utf-8").splitlines() if line.strip()}
    for pack_id in sorted(previous_ids - enabled_ids):
        target = root / "packs" / pack_id
        if target.is_symlink() or target.is_dir():
            steps.append(PlannedStep(f"prune disabled pack {target}", target=target))
            if not dry_run:
                if target.is_symlink():
                    target.unlink()
                else:
                    shutil.rmtree(target)

    for descriptor in view_packs:
        target = root / "packs" / descriptor.pack_id
        source = next(item.skill_dir for item in packs if item.pack_id == descriptor.pack_id)
        steps.append(PlannedStep(f"materialise {target} from {source}", target=target))
        if not dry_run:
            _materialise_pack(target, source, links, managed=descriptor.pack_id in previous_ids)

    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        _materialise(Path(os.path.realpath(core.skill_md)), root / "SKILL.md", links)
        for rel in _CORE_FILES:
            source = core_dir / rel
            if not source.is_file():
                continue
            pre = _PACK_BUILDER_DOC_REWRITES if rel in _PACK_AUTHORING_FILES else None
            _materialise(source, root / rel, links, pre=pre)
        # The registry is regenerated in place by sync, so copy it only once.
        source_packs = core_dir / _PACKS_REGISTRY
        if source_packs.is_file() and not (root / _PACKS_REGISTRY).exists():
            _materialise(source_packs, root / _PACKS_REGISTRY, links)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("\n".join(sorted(enabled_ids)) + "\n", encoding="utf-8")

    gateway = replace(
        core,
        skill_dir=root,
        skill_md=root / "SKILL.md",
    )
    return gateway, steps, view_packs


__all__ = ["compose_view", "route_pack_ids", "view_descriptors"]
