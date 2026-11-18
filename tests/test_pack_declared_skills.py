"""Every skill a pack manifest declares must exist, load, and carry real guidance.

A pack declares its skills in ``pack.yaml``: the one ``documentation: {kind: skill}``
file and any ``resources: - kind: skill`` extras. Discovery drops a declared skill
silently when its file is missing, its frontmatter lacks ``name``/``description``,
or an extra sits outside ``docs/<name>/SKILL.md``. This test makes those mistakes
fail loudly, and checks that discovery actually returns each declared skill.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from astrid.skills import discovery

ROOT = Path(__file__).resolve().parents[1]
PACKS = ROOT / "astrid" / "packs"

MIN_WORDS = 150
# Skills already shorter than MIN_WORDS when this test was added. They are kept
# to their recorded word count as a floor: they may grow, not shrink further.
KNOWN_SHORT_WORDS: dict[tuple[str, str], int] = {
    ("local", "docs/SKILL.md"): 51,
    ("rendering", "docs/SKILL.md"): 124,
}
EXTRA_SKILL_PATH = re.compile(r"docs/[a-z0-9][a-z0-9-]*/SKILL\.md")


def _declared_skills() -> list[tuple[str, str, str]]:
    """Return (pack_id, role, relative path) for each declared skill in a pack manifest."""
    declared: list[tuple[str, str, str]] = []
    for manifest_path in sorted(PACKS.glob("*/pack.yaml")):
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(manifest, dict):
            continue
        pack_id = manifest_path.parent.name
        documentation = manifest.get("documentation")
        if isinstance(documentation, dict) and documentation.get("kind") == "skill":
            declared.append((pack_id, "documentation", str(documentation.get("path"))))
        for resource in manifest.get("resources") or []:
            if isinstance(resource, dict) and resource.get("kind") == "skill":
                declared.append((pack_id, "resource", str(resource.get("path"))))
    return declared


DECLARED = _declared_skills()


def _id(entry: tuple[str, str, str]) -> str:
    pack_id, role, path = entry
    return f"{pack_id}:{role}:{path}"


def _split_frontmatter(text: str) -> tuple[dict, str] | None:
    if not text.startswith("---\n"):
        return None
    parts = text.split("\n---", 1)
    if len(parts) < 2:
        return None
    meta = yaml.safe_load(parts[0][len("---\n"):]) or {}
    return (meta if isinstance(meta, dict) else {}), parts[1]


def _skill_file(entry: tuple[str, str, str]) -> Path:
    pack_id, _role, path = entry
    return PACKS / pack_id / path


def test_declared_skills_were_found() -> None:
    assert len(DECLARED) >= 10
    assert any(role == "resource" for _pack, role, _path in DECLARED)


@pytest.mark.parametrize("entry", DECLARED, ids=_id)
def test_declared_skill_file_exists(entry: tuple[str, str, str]) -> None:
    skill = _skill_file(entry)
    assert skill.is_file(), f"{entry[0]} declares {entry[2]} but {skill} does not exist"


@pytest.mark.parametrize("entry", DECLARED, ids=_id)
def test_declared_skill_has_name_and_description(entry: tuple[str, str, str]) -> None:
    skill = _skill_file(entry)
    if not skill.is_file():
        pytest.skip("missing file is reported by test_declared_skill_file_exists")
    parsed = _split_frontmatter(skill.read_text(encoding="utf-8"))
    assert parsed is not None, f"{skill}: no YAML frontmatter at the top of the file"
    meta, _body = parsed
    for key in ("name", "description"):
        value = meta.get(key)
        assert isinstance(value, str) and value.strip(), f"{skill}: frontmatter needs a non-empty {key}"


@pytest.mark.parametrize("entry", DECLARED, ids=_id)
def test_declared_skill_has_substantive_body(entry: tuple[str, str, str]) -> None:
    skill = _skill_file(entry)
    if not skill.is_file():
        pytest.skip("missing file is reported by test_declared_skill_file_exists")
    parsed = _split_frontmatter(skill.read_text(encoding="utf-8"))
    if parsed is None:
        pytest.skip("missing frontmatter is reported by test_declared_skill_has_name_and_description")
    _meta, body = parsed
    words = len(body.split())
    floor = KNOWN_SHORT_WORDS.get((entry[0], entry[2]))
    if floor is not None:
        assert words >= floor, f"{skill}: body shrank to {words} words (floor {floor})"
    else:
        assert words >= MIN_WORDS, f"{skill}: body has {words} words, needs at least {MIN_WORDS}"


@pytest.mark.parametrize(
    "entry",
    [entry for entry in DECLARED if entry[1] == "resource"],
    ids=_id,
)
def test_declared_extra_skill_has_discoverable_path(entry: tuple[str, str, str]) -> None:
    assert EXTRA_SKILL_PATH.fullmatch(entry[2]), (
        f"{entry[0]}: extra skill {entry[2]!r} must be docs/<name>/SKILL.md "
        "or discovery ignores it"
    )


def test_every_declared_skill_is_discovered() -> None:
    mains = discovery.list_skills(PACKS)
    discovered = {d.skill_md.resolve() for d in mains}
    discovered |= {d.skill_md.resolve() for d in discovery.list_extras(mains)}
    missing = []
    for entry in DECLARED:
        manifest = yaml.safe_load((PACKS / entry[0] / "pack.yaml").read_text(encoding="utf-8")) or {}
        if manifest.get("status") == "deprecated" or manifest.get("visibility") == "hidden":
            continue
        skill = _skill_file(entry)
        if skill.is_file() and skill.resolve() not in discovered:
            missing.append(f"{entry[0]}:{entry[2]}")
    assert not missing, f"declared but not discovered by skills discovery: {missing}"
