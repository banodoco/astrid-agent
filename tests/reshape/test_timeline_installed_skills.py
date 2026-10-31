"""Prove Timeline skill sync against an installed Astrid wheel."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import runtime_protocol

from scripts.reshape.installed_artifact import InstalledArtifactHarness, build_once

ROOT = Path(__file__).parents[2]


def _run(harness: InstalledArtifactHarness, *args: str) -> dict:
    record = harness.run_module(
        "timeline-skills-" + "-".join(args[1:3]),
        "astrid",
        args,
        check=False,
    )
    assert not (record.error and "source-tree path" in record.error), record.output
    report = json.loads(record.stdout)
    if record.returncode:
        assert record.returncode == 1 and args[1] in {"sync", "update"}, record.output
        assert report.get("broken_links"), record.output
    return report


def _state(harness: InstalledArtifactHarness) -> dict:
    path = harness.roots.state / "state" / "astrid" / "skills.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _selected(harness: InstalledArtifactHarness) -> set[str]:
    state = _state(harness)
    return {
        skill_id
        for skill_id, entry in state["skills"]["claude"]["skills"].items()
        if entry.get("selected")
    }


def _view_files(harness: InstalledArtifactHarness) -> dict[str, str]:
    root = harness.roots.state / "state" / "astrid" / "skills" / "claude"
    return {
        path.relative_to(root).as_posix(): (
            f"symlink:{path.readlink()}"
            if path.is_symlink()
            else hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in root.rglob("*")
        if path.is_symlink() or path.is_file()
    }


def _timeline_broken_links(report: dict) -> list[dict]:
    return [
        link
        for link in report["broken_links"]
        if "packs" in Path(link["file"]).parts and "timeline" in Path(link["file"]).parts
    ]


def _add_runtime_protocol(harness: InstalledArtifactHarness) -> None:
    assert harness.identity is not None
    site_packages = Path(harness.identity.import_path).parent.parent
    runtime_root = Path(runtime_protocol.__file__).resolve().parent.parent
    (site_packages / "runtime-protocol-source.pth").write_text(
        f"{runtime_root}\n", encoding="utf-8"
    )
    harness.probe_identity()


def test_timeline_skill_lifecycle_on_installed_wheel(tmp_path: Path) -> None:
    workspace = tmp_path / "installed-timeline-skills"
    harness = build_once(ROOT, workspace=workspace, install_dependencies=True)
    try:
        _add_runtime_protocol(harness)
        (harness.roots.home / ".claude").mkdir(parents=True)

        synced = _run(harness, "skills", "sync", "--harness", "claude", "--json")
        selected = _selected(harness)
        assert "timeline-series" not in selected
        assert "timeline-narrated" not in selected
        assert "timeline-review" not in selected
        assert _timeline_broken_links(synced) == []
        view = harness.roots.state / "state" / "astrid" / "skills" / "claude"
        timeline_main = view / "packs" / "timeline" / "SKILL.md"
        assert "# Timeline editing" in timeline_main.read_text(encoding="utf-8")
        assert "(packs/timeline/SKILL.md)" in (view / "SKILL.md").read_text(encoding="utf-8")

        installed = _run(
            harness,
            "skills",
            "install",
            "timeline",
            "--skill",
            "series",
            "--harness",
            "claude",
            "--json",
        )
        assert _selected(harness) == selected | {"timeline-series"}
        assert installed["actions"]
        series_link = harness.roots.home / ".claude" / "skills" / "astrid-timeline-series"
        assert series_link.is_symlink()
        assert series_link.resolve(strict=True).is_dir()

        series_skill = (
            Path(harness.identity.import_path).parent
            / "packs"
            / "timeline"
            / "docs"
            / "series"
            / "SKILL.md"
        )
        series_skill.write_text(
            series_skill.read_text(encoding="utf-8") + "\nInstalled-copy change.\n",
            encoding="utf-8",
        )
        listing = _run(harness, "skills", "list", "--json")
        series_row = next(row for row in listing["packs"] if row["id"] == "timeline-series")
        assert series_row["harnesses"]["claude"]["outdated"] is True

        updated = _run(
            harness,
            "skills",
            "update",
            "timeline",
            "--skill",
            "series",
            "--harness",
            "claude",
            "--json",
        )
        assert _selected(harness) == selected | {"timeline-series"}
        assert any("timeline-series" in action["changed"] for action in updated["actions"])
        assert any(
            not row["harnesses"]["claude"]["outdated"]
            for row in _run(harness, "skills", "list", "--json")["packs"]
            if row["id"] == "timeline-series"
        )

        _run(
            harness,
            "skills",
            "uninstall",
            "timeline",
            "--skill",
            "series",
            "--harness",
            "claude",
            "--json",
        )
        assert _selected(harness) == selected
        assert timeline_main.is_file()
        assert not series_link.exists() and not series_link.is_symlink()

        first = _run(harness, "skills", "sync", "--harness", "claude", "--json")
        first_skills_state = _state(harness)["skills"]
        first_skills_state = {
            name: {key: value for key, value in entry.items() if key != "synced_at"}
            for name, entry in first_skills_state.items()
        }
        first_view = _view_files(harness)
        second = _run(harness, "skills", "sync", "--harness", "claude", "--json")
        assert second["broken_links"] == first["broken_links"]
        assert _timeline_broken_links(first) == _timeline_broken_links(second) == []
        second_skills_state = _state(harness)["skills"]
        second_skills_state = {
            name: {key: value for key, value in entry.items() if key != "synced_at"}
            for name, entry in second_skills_state.items()
        }
        assert second_skills_state == first_skills_state
        assert _view_files(harness) == first_view
        assert second["registry"]["changed"] is False
        assert all(
            not step["extras"].get("changed", False)
            for action in second["actions"]
            for step in action["steps"]
        )
        assert all(not action["changed"] for action in second["actions"])
    finally:
        harness.close()
