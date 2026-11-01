"""Auto-managed pack registry in the creative-work supporting reference.

``astrid skills sync`` keeps a sentinel-delimited block in the creative-work reference
current: one terse row per discovered pack skill, with the pack's
``short_description`` and an inward pointer to where its full skill lives. The
block is regenerated deterministically from :func:`astrid.skills.discovery.list_skills`
— the same discovery the install/sync machinery uses — so the registry never
drifts from the skills that are actually linkable.

Everything outside the sentinel markers is preserved byte-for-byte. When the
markers are absent the block is inserted under a new ``## Installed packs``
heading just after the intro paragraph.
"""

from __future__ import annotations

from pathlib import Path

from astrid.core.foundation.paths import REPO_ROOT

from .commands import rewrite_commands
from .discovery import SkillDescriptor, list_extras, list_skills

BEGIN_MARKER = "<!-- PACKS:BEGIN (managed by `astrid skills sync`) -->"
END_MARKER = "<!-- PACKS:END -->"

_HEADING = "## Installed packs"

# Default registry reference; constant name retained for API compatibility.
CORE_SKILL_MD = REPO_ROOT / "astrid/packs/_core/docs/creative-work/references/packs.md"


def _repo_relative(path: Path, *, view_root: Path | None = None) -> Path:
    """Return *path* relative to the repo root when it lives under it, else as-is."""
    if view_root is not None:
        # Keep composed `packs/<id>/SKILL.md` routes relative to the view.
        try:
            return path.relative_to(view_root)
        except ValueError:
            pass
    resolved = path.resolve()
    root = REPO_ROOT.resolve()
    if root in resolved.parents:
        return resolved.relative_to(root)
    return path


def _registry_descriptors(descriptors: list[SkillDescriptor] | None = None) -> list[SkillDescriptor]:
    """Return the pack skills that belong in the registry, gateway excluded.

    ``_core`` is the gateway itself (it *hosts* the block), so it never lists
    itself. The result is sorted by pack id for a byte-stable block.
    """
    if descriptors is None:
        mains = list_skills()
        descriptors = mains + list_extras(mains)
    rows = [d for d in descriptors if d.pack_id != "_core"]
    rows.sort(key=lambda d: (d.pack_id, d.skill or ""))
    return rows


def render_registry_block(
    descriptors: list[SkillDescriptor] | None = None,
    *,
    deep: bool = False,
    view_root: Path | None = None,
    selected: set[str] | None = None,
) -> str:
    """Render the managed registry block (markers included, no trailing newline).

    Each entry is one terse table row: pack name, short description, and an
    inward pointer to the pack skill path (relative to the repo root). When
    *deep* is set — i.e. per-pack skills are linked into the harness dirs —
    the ``astrid-<pack>`` skill name is included so an agent can find it by
    name as well as by path.
    """
    rows = _registry_descriptors(descriptors)
    lines = [BEGIN_MARKER, _HEADING, ""]
    if not rows:
        lines.append("_No pack skills discovered._")
        lines.append("")
        lines.append(END_MARKER)
        return "\n".join(lines)

    if deep:
        lines.append("| Pack | Skill name | Description | Full skill |")
        lines.append("| --- | --- | --- | --- |")
    else:
        lines.append("| Pack | Description | Full skill |")
        lines.append("| --- | --- | --- |")

    for descriptor in rows:
        summary = (descriptor.short_description or descriptor.description or "").replace("|", "\\|")
        if descriptor.skill is not None:
            status = "selected" if selected and descriptor.skill_id in selected else (
                f"available: `astrid skills install {descriptor.pack_id} --skill {descriptor.skill}`"
            )
            summary = f"{summary}; {status}"
        skill_path = _repo_relative(descriptor.skill_md, view_root=view_root)
        path_cell = f"`{skill_path}`"
        if deep:
            skill_name = f"`astrid-{descriptor.skill_id}`"
            lines.append(f"| {descriptor.pack_id} | {skill_name} | {summary} | {path_cell} |")
        else:
            pack_cell = descriptor.pack_id
            if descriptor.skill is not None:
                pack_cell = f"{pack_cell} / {descriptor.skill}"
            lines.append(f"| {pack_cell} | {summary} | {path_cell} |")

    lines.append("")
    lines.append(END_MARKER)
    return rewrite_commands("\n".join(lines))


def _splice_block(text: str, block: str) -> str:
    """Return *text* with the managed block replaced or inserted.

    Content outside the markers is preserved exactly. When the markers are
    absent the block is inserted after the intro paragraph (the first blank
    line following the top-level ``# `` heading), else appended.
    """
    begin = text.find(BEGIN_MARKER)
    if begin != -1:
        end = text.find(END_MARKER, begin)
        if end != -1:
            end += len(END_MARKER)
            return text[:begin] + block + text[end:]
        # Begin marker without a matching end: replace from begin to EOL only.
        eol = text.find("\n", begin)
        tail = text[eol:] if eol != -1 else ""
        return text[:begin] + block + tail

    lines = text.splitlines(keepends=True)
    insert_at = len(lines)
    seen_title = False
    for index, line in enumerate(lines):
        if line.startswith("# "):
            seen_title = True
            continue
        if seen_title and line.strip() == "":
            insert_at = index + 1
            break

    head = "".join(lines[:insert_at])
    tail = "".join(lines[insert_at:])
    if head and not head.endswith("\n"):
        head += "\n"
    return f"{head}\n{block}\n\n{tail}"


def regenerate(
    *,
    skill_md_path: Path | None = None,
    descriptors: list[SkillDescriptor] | None = None,
    deep: bool = False,
    view_root: Path | None = None,
    dry_run: bool = False,
    selected: set[str] | None = None,
) -> bool:
    """Regenerate the managed registry block in *skill_md_path*.

    Returns ``True`` when the file content would change (or did change, when
    not a dry run). Makes no write when the rendered block already matches.
    """
    target = skill_md_path or CORE_SKILL_MD
    original = target.read_text(encoding="utf-8")
    block = render_registry_block(descriptors, deep=deep, view_root=view_root, selected=selected)
    updated = _splice_block(original, block)
    if updated == original:
        return False
    if not dry_run:
        target.write_text(updated, encoding="utf-8")
    return True


def is_current(
    *,
    skill_md_path: Path | None = None,
    descriptors: list[SkillDescriptor] | None = None,
    deep: bool = False,
    view_root: Path | None = None,
    selected: set[str] | None = None,
) -> bool:
    """Return ``True`` when the registry block is already up to date."""
    return not regenerate(
        skill_md_path=skill_md_path,
        descriptors=descriptors,
        deep=deep,
        view_root=view_root,
        selected=selected,
        dry_run=True,
    )


__all__ = [
    "BEGIN_MARKER",
    "CORE_SKILL_MD",
    "END_MARKER",
    "is_current",
    "regenerate",
    "render_registry_block",
]
