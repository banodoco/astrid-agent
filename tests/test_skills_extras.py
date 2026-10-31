from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from astrid.core.pack.validate import validate_pack
from astrid.core.pack.validate_layout import validate_pack_layout
from astrid.skills.discovery import list_extras, list_skills, unit_digest
from astrid.skills.registry import render_registry_block


def _make_pack(root: Path, *, resources: str = "") -> Path:
    pack = root / "demo_pack"
    (pack / "docs").mkdir(parents=True)
    manifest = textwrap.dedent(
        """\
        schema_version: 3
        id: demo_pack
        name: Demo Pack
        version: 0.1.0
        documentation:
          kind: skill
          path: docs/SKILL.md
        resources:
        """
    )
    manifest += textwrap.indent(resources, "  ") if resources else "  []\n"
    (pack / "pack.yaml").write_text(manifest, encoding="utf-8")
    (pack / "docs" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Main skill.\n---\nMain.\n", encoding="utf-8"
    )
    return pack


def test_discovery_finds_only_declared_extra_skill(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path, resources="- kind: skill\n  path: docs/extra/SKILL.md\n")
    extra_dir = pack / "docs" / "extra"
    extra_dir.mkdir()
    (extra_dir / "SKILL.md").write_text(
        "---\nname: extra\ndescription: Extra skill.\n---\nExtra.\n", encoding="utf-8"
    )
    undeclared = pack / "docs" / "hidden"
    undeclared.mkdir()
    (undeclared / "SKILL.md").write_text(
        "---\nname: hidden\ndescription: Hidden skill.\n---\nHidden.\n", encoding="utf-8"
    )

    mains = list_skills(tmp_path)
    extras = list_extras(mains)

    assert [item.pack_id for item in mains] == ["demo_pack"]
    assert [(item.skill, item.skill_id) for item in extras] == [("extra", "demo_pack-extra")]
    registry = render_registry_block(mains + extras)
    assert "available: `astrid skills install demo_pack --skill extra`" in registry


def test_extra_discovery_rejects_paths_outside_the_declared_unit(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path, resources="- kind: skill\n  path: docs/extra/SKILL.md\n")
    extra_dir = pack / "docs" / "extra"
    extra_dir.mkdir()
    outside = pack / "docs" / "outside.md"
    outside.write_text("---\nname: outside\ndescription: Outside.\n---\n", encoding="utf-8")
    (extra_dir / "SKILL.md").symlink_to(outside)

    assert list_extras(list_skills(tmp_path)) == []


def test_duplicate_extra_skill_resource_is_rejected_and_not_discovered(tmp_path: Path) -> None:
    resource = "- kind: skill\n  path: docs/extra/SKILL.md\n"
    pack = _make_pack(tmp_path, resources=resource + resource)
    (pack / "docs" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Main skill.\n---\nMain.\n", encoding="utf-8"
    )
    extra_dir = pack / "docs" / "extra"
    extra_dir.mkdir()
    (extra_dir / "SKILL.md").write_text(
        "---\nname: extra\ndescription: Extra skill.\n---\nExtra.\n", encoding="utf-8"
    )

    errors, _warnings = validate_pack(pack)

    assert any("duplicate skill path 'docs/extra/SKILL.md'" in error for error in errors), errors
    assert list_extras(list_skills(tmp_path)) == []


def test_layout_rejects_undeclared_nested_skill_and_accepts_declared_extra(tmp_path: Path) -> None:
    (tmp_path / "docs" / "extra").mkdir(parents=True)
    (tmp_path / "docs" / "extra" / "SKILL.md").write_text("skill")
    data = {
        "schema_version": 3,
        "documentation": {"kind": "skill", "path": "docs/SKILL.md"},
        "resources": [],
    }

    assert [issue.path for issue in validate_pack_layout(tmp_path, data)] == ["docs/extra/SKILL.md"]
    data["resources"] = [{"kind": "skill", "path": "docs/extra/SKILL.md"}]
    assert validate_pack_layout(tmp_path, data) == []


def test_unit_digest_is_stable_excludes_generated_files_and_separates_main(tmp_path: Path) -> None:
    pack = _make_pack(tmp_path, resources="- kind: skill\n  path: docs/extra/SKILL.md\n")
    extra_dir = pack / "docs" / "extra"
    extra_dir.mkdir()
    extra_file = extra_dir / "SKILL.md"
    extra_file.write_text(
        "---\nname: extra\ndescription: Extra skill.\n---\nextra one\n", encoding="utf-8"
    )
    docs = pack / "docs"
    (docs / "guide.md").write_text("guide", encoding="utf-8")
    pycache = docs / "__pycache__"
    pycache.mkdir()
    (pycache / "ignored.pyc").write_bytes(b"first")

    main = list_skills(tmp_path)[0]
    extra = list_extras([main])[0]
    main_digest = unit_digest(main, [extra])
    extra_digest = unit_digest(extra, [extra])
    (pycache / "ignored.pyc").write_bytes(b"second")
    assert unit_digest(main, [extra]) == main_digest
    assert unit_digest(extra, [extra]) == extra_digest

    extra_file.write_text(
        "---\nname: extra\ndescription: Extra skill.\n---\nextra two\n", encoding="utf-8"
    )
    assert unit_digest(extra, [extra]) != extra_digest
    assert unit_digest(main, [extra]) == main_digest

    (docs / "guide.md").write_text("changed", encoding="utf-8")
    assert unit_digest(main, [extra]) != main_digest


def test_timeline_extras_list_and_selective_cli_install(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".claude").mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "ASTRID_STATE_HOME": str(tmp_path / "state"),
    }

    listing = subprocess.run(
        [sys.executable, "-m", "astrid", "skills", "list"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert "timeline" in listing.stdout
    assert "timeline-series" in listing.stdout
    assert "timeline-narrated" in listing.stdout
    assert "timeline-review" in listing.stdout
    assert "timeline-shots" in listing.stdout

    subprocess.run(
        [sys.executable, "-m", "astrid", "skills", "install", "timeline",
         "--skill", "series", "--harness", "claude"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    state = json.loads((tmp_path / "state" / "astrid" / "skills.json").read_text(encoding="utf-8"))
    selected = {
        skill_id
        for skill_id, entry in state["skills"]["claude"]["skills"].items()
        if entry.get("selected")
    }
    assert selected == {"timeline-series"}
