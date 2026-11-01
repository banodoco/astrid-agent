"""Discover the one authored skill declared by each pack manifest.

A ``SkillDescriptor`` is a Claude-style frontmatter document (``name`` and
``description``) plus the directory it lives in.  The pack's singular
``documentation: {kind: skill, path: ...}`` declaration chooses the source;
supporting files beside it remain available through the existing directory-link
sync.  Nested component guides are ordinary linked documentation, never extra
skills.  Hermes-only extras live under an optional ``metadata.hermes.*`` block
in the same file; Claude/Codex ignore unknown keys.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack import load_pack_manifest, pack_manifest_path
from astrid.core.search import short_description_or_truncated

PACKS_DIR = REPO_ROOT / "astrid" / "packs"

# Tokens forbidden in the shared SKILL.md (they leak Hermes-specific dynamic
# behavior into a file Claude/Codex also read).
FORBIDDEN_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$\{HERMES_[A-Z0-9_]+\}"),
    re.compile(r"!`[^`]+`"),
)


@dataclass(frozen=True)
class SkillDescriptor:
    pack_id: str
    name: str
    description: str
    short_description: str
    skill_dir: Path
    skill_md: Path
    hermes_metadata: dict = field(default_factory=dict)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    # Frontmatter ends at the next standalone "---" line.
    lines = text.splitlines()
    if len(lines) < 2:
        return {}, text
    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, text
    frontmatter_block = "\n".join(lines[1:end_index])
    body = "\n".join(lines[end_index + 1 :])
    try:
        import yaml

        data = yaml.safe_load(frontmatter_block) or {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return data, body


def _declared_skill_path(pack_dir: Path, pack: object) -> tuple[Path, bool] | None:
    """Return the declared skill source and whether strict frontmatter applies.

    Real ``PackDefinition`` instances always expose ``documentation``.  The
    small compatibility fallback is only for older test seams that supplied a
    light-weight external-pack object before the manifest declaration became
    authoritative; real manifests without a skill declaration return ``None``.
    """
    if not hasattr(pack, "documentation"):
        return pack_dir / "skill" / "SKILL.md", False
    documentation = getattr(pack, "documentation") or {}
    if not isinstance(documentation, dict) or documentation.get("kind") != "skill":
        return None
    raw_path = documentation.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    relative = Path(raw_path)
    root = pack_dir.resolve()
    resolved = (root / relative).resolve()
    if relative.is_absolute() or not resolved.is_relative_to(root):
        return None
    schema_version = str(getattr(pack, "schema_version", ""))
    return resolved, schema_version == "3"


def _try_add_skill(
    descriptors: list[SkillDescriptor],
    skill_md: Path,
    pack_id: str,
    *,
    strict_frontmatter: bool = False,
) -> bool:
    """Parse *skill_md* and append a SkillDescriptor if valid.

    Returns ``True`` when a descriptor was appended.
    """
    if not skill_md.is_file():
        return False
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return False
    front, body = _parse_frontmatter(text)
    if strict_frontmatter:
        if not text.startswith("---"):
            return False
        if not isinstance(front.get("name"), str) or not front["name"].strip():
            return False
        if not isinstance(front.get("description"), str) or not front["description"].strip():
            return False
    name = str(front.get("name") or pack_id)
    description = str(front.get("description") or "")
    short = short_description_or_truncated(
        short=str(front.get("short_description") or ""),
        description=description,
    )
    hermes_meta = {}
    metadata = front.get("metadata") or {}
    if isinstance(metadata, dict) and isinstance(metadata.get("hermes"), dict):
        hermes_meta = dict(metadata["hermes"])
    descriptors.append(
        SkillDescriptor(
            pack_id=pack_id,
            name=name,
            description=description,
            short_description=short,
            skill_dir=skill_md.parent,
            skill_md=skill_md,
            hermes_metadata=hermes_meta,
        )
    )
    return True


# Problems hit by the most recent ``list_skills()`` scan.  Discovery degrades
# rather than failing, so callers surface these as warnings.
_DISCOVERY_PROBLEMS: list[str] = []


def discovery_problems() -> list[str]:
    """Return problems recorded by the most recent :func:`list_skills` call."""
    return list(_DISCOVERY_PROBLEMS)


def _scan_discovered_packs(descriptors: list[SkillDescriptor]) -> None:
    """Append skills from discovered extra-root packs via shared metadata.

    The source-tree walk above already covers source packs (including
    manifest-less ones such as ``_core`` that pack discovery does not
    enumerate), so this layer only adds the ``extra`` and ``installed``
    source kinds. It reuses :func:`discover_pack_metadata` so skills track the
    same roots and priority ordering as the executor/orchestrator/element
    registries. Source packs win on id collision (they were appended first).
    """
    from astrid.core.pack.discovery import discover_pack_metadata

    try:
        discovered_packs = list(discover_pack_metadata())
    except Exception as exc:  # noqa: BLE001 - optional discovery must not hide source skills
        _DISCOVERY_PROBLEMS.append(f"external and managed pack discovery skipped: {exc}")
        return

    seen_ids = {descriptor.pack_id for descriptor in descriptors}
    for discovered in discovered_packs:
        # The shared inventory calls environment/extra roots ``env``. Keep
        # ``installed`` as a compatibility spelling for older inventory
        # records, but never invent a second source scan here.
        if discovered.source_kind not in ("extra", "env", "managed", "installed"):
            continue
        pack = discovered.pack
        if pack.status == "deprecated" or pack.visibility == "hidden":
            continue
        declared = _declared_skill_path(discovered.pack_dir, pack)
        if declared is not None:
            pack_skill, strict_frontmatter = declared
        else:
            pack_skill = None
            strict_frontmatter = False
        if (
            pack_skill is not None
            and pack.id not in seen_ids
            and _try_add_skill(
                descriptors,
                pack_skill,
                pack.id,
                strict_frontmatter=strict_frontmatter,
            )
        ):
            seen_ids.add(pack.id)


def list_skills(packs_dir: Path | None = None) -> list[SkillDescriptor]:
    base = packs_dir or PACKS_DIR
    _DISCOVERY_PROBLEMS.clear()
    descriptors: list[SkillDescriptor] = []
    if not base.exists():
        return descriptors
    for pack_dir in sorted(base.iterdir()):
        if not pack_dir.is_dir():
            continue

        manifest_path = pack_manifest_path(pack_dir)
        pack = None
        if manifest_path is not None:
            try:
                pack = load_pack_manifest(manifest_path)
            except Exception:
                pack = None
        if pack is not None and (pack.status == "deprecated" or pack.visibility == "hidden"):
            continue

        if pack is not None:
            declared = _declared_skill_path(pack_dir, pack)
            if declared is None:
                continue
            skill_md, strict_frontmatter = declared
            _try_add_skill(
                descriptors,
                skill_md,
                pack.id,
                strict_frontmatter=strict_frontmatter,
            )
            continue

        # _core is the intentional manifestless gateway shell.  No other
        # manifestless directory can become an implicit skill.
        if pack_dir.name == "_core":
            _try_add_skill(
                descriptors,
                pack_dir / "docs" / "SKILL.md",
                "_core",
                strict_frontmatter=True,
            )

    # When scanning the default source tree, layer in installed/extra packs
    # via the shared discovery metadata. An explicit *packs_dir* keeps the
    # legacy single-root scan untouched (used by parity tests).
    if packs_dir is None:
        _scan_discovered_packs(descriptors)

    return descriptors


def lint_shared_skill_md(text: str) -> list[str]:
    """Return human-readable findings if `text` contains forbidden tokens."""
    findings: list[str] = []
    for pattern in FORBIDDEN_TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            findings.append(f"forbidden token in shared SKILL.md: {match.group(0)!r}")
    return findings


def get(pack_id: str, packs_dir: Path | None = None) -> SkillDescriptor:
    for descriptor in list_skills(packs_dir):
        if descriptor.pack_id == pack_id:
            return descriptor
    raise KeyError(f"no installable skill for pack {pack_id!r}")


__all__ = [
    "FORBIDDEN_TOKEN_PATTERNS",
    "PACKS_DIR",
    "SkillDescriptor",
    "discovery_problems",
    "get",
    "lint_shared_skill_md",
    "list_skills",
]
