"""Tests for the multi-harness skills install layer."""

from __future__ import annotations

import argparse
import os
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pytest
import yaml

from astrid import skills
from astrid.pack_api import VERSION
from astrid.core.cli_choices import StaticChoices
from astrid.skills import cli as skills_cli
from astrid.skills import discovery, state
from astrid.skills.harnesses import (
    ClaudeAdapter,
    CodexAdapter,
    HermesAdapter,
)
from astrid.skills.harnesses.codex import BEGIN_MARKER, END_MARKER
from astrid.skills.harnesses.hermes import PACKS_DIR_FOR_EXTERNAL


class _Tmp:
    """Test fixture that pins a tmpdir as $HOME and as the state home."""

    def __init__(self) -> None:
        self._td = TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        (self.home / ".claude").mkdir()
        (self.home / ".codex").mkdir()
        (self.home / ".hermes").mkdir()
        self.state_path = self.tmp / "state.json"
        self._patches = [
            mock.patch.dict("os.environ", {
                "HOME": str(self.home),
                "ASTRID_STATE_HOME": str(self.tmp / "_state"),
            }, clear=False),
            mock.patch.object(Path, "home", return_value=self.home),
        ]
        for patch in self._patches:
            patch.start()

    def close(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._td.cleanup()


def _descriptors():
    return discovery.list_skills()


def test_fresh_sync_selects_and_links_declared_defaults():
    fx = _Tmp()
    try:
        defaults = skills.default_descriptors()
        assert skills_cli.main(["sync", "--skills", "on", "--harness", "claude", "--json"]) == 0
        adapter = ClaudeAdapter()
        assert all(adapter.target_for(descriptor).is_symlink() for descriptor in defaults)
        selected = state.load()["skills"]["claude"]["skills"]
        assert all(selected[descriptor.skill_id]["selected"] for descriptor in defaults)
    finally:
        fx.close()


def test_fresh_sync_preserves_an_explicit_empty_selection():
    fx = _Tmp()
    try:
        state.save(state.load())
        defaults = skills.default_descriptors()
        assert skills_cli.main(["sync", "--skills", "on", "--harness", "claude", "--json"]) == 0
        adapter = ClaudeAdapter()
        assert not any(adapter.target_for(descriptor).exists() or adapter.target_for(descriptor).is_symlink()
                       for descriptor in defaults if descriptor.pack_id != "_core")
        assert not any(item.get("selected") for item in
                       state.load()["skills"]["claude"]["skills"].values())
    finally:
        fx.close()


def test_installer_sync_preserves_default_opt_out():
    fx = _Tmp()
    try:
        args = ["sync", "--skills", "on", "--skill-copies", "off", "--harness", "claude", "--json"]
        assert skills_cli.main(args) == 0
        assert skills_cli.main(["uninstall", "vibecomfy", "--harness", "claude", "--json"]) == 0
        assert skills_cli.main(args) == 0
        data = state.load()
        assert "vibecomfy" in data["disabled_defaults"]["claude"]
        descriptors = {d.pack_id: d for d in _descriptors()}
        adapter = ClaudeAdapter()
        assert not adapter.target_for(descriptors["vibecomfy"]).exists()
        assert adapter.target_for(descriptors["rendering"]).exists()
    finally:
        fx.close()


def test_installer_sync_preserves_existing_selection():
    fx = _Tmp()
    try:
        assert skills_cli.main(["install", "generation", "--harness", "claude", "--json"]) == 0
        assert skills_cli.main(["sync", "--skills", "on", "--skill-copies", "off", "--harness", "claude", "--json"]) == 0
        selected = state.load()["skills"]["claude"]["skills"]
        assert selected["generation"]["selected"]
        assert not any(selected.get(pack, {}).get("selected") for pack in ("setup", "rendering", "vibecomfy"))
        descriptors = {d.pack_id: d for d in _descriptors()}
        assert ClaudeAdapter().target_for(descriptors["generation"]).exists()
    finally:
        fx.close()


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError(f"missing subparser {name!r}")


def _write_fake_descriptor(root: Path, pack_id: str) -> discovery.SkillDescriptor:
    skill_dir = root / pack_id / "skill"
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: astrid-{pack_id}\ndescription: {pack_id} skill.\n---\nBody.\n",
        encoding="utf-8",
    )
    return discovery.SkillDescriptor(
        pack_id=pack_id,
        name=f"astrid-{pack_id}",
        description=f"{pack_id} skill.",
        short_description=f"{pack_id} skill.",
        skill_dir=skill_dir,
        skill_md=skill_md,
    )


class AdapterPlanTest(unittest.TestCase):
    def test_claude_target_for_core_uses_astrid_path(self) -> None:
        fx = _Tmp()
        try:
            adapter = ClaudeAdapter()
            descriptor = next(d for d in _descriptors() if d.pack_id == "_core")
            self.assertEqual(adapter.target_for(descriptor), fx.home / ".claude" / "skills" / "astrid")
        finally:
            fx.close()

    def test_codex_target_for_core_uses_astrid_path(self) -> None:
        fx = _Tmp()
        try:
            adapter = CodexAdapter()
            descriptor = next(d for d in _descriptors() if d.pack_id == "_core")
            self.assertEqual(adapter.target_for(descriptor), fx.home / ".codex" / "skills" / "astrid")
        finally:
            fx.close()

    def test_hermes_target_for_core_uses_astrid_path(self) -> None:
        fx = _Tmp()
        try:
            adapter = HermesAdapter()
            descriptor = next(d for d in _descriptors() if d.pack_id == "_core")
            self.assertEqual(adapter.target_for(descriptor), fx.home / ".hermes" / "skills" / "astrid")
        finally:
            fx.close()

    def test_cli_enum_args_use_static_choices_wrappers(self) -> None:
        parser = skills_cli.build_parser()
        install_parser = _subparser(parser, "install")
        uninstall_parser = _subparser(parser, "uninstall")
        sync_parser = _subparser(parser, "sync")

        install_harness = next(action for action in install_parser._actions if action.dest == "harness")
        install_mechanism = next(action for action in install_parser._actions if action.dest == "mechanism")
        uninstall_harness = next(action for action in uninstall_parser._actions if action.dest == "harness")
        sync_mechanism = next(action for action in sync_parser._actions if action.dest == "mechanism")

        for action in (install_harness, install_mechanism, uninstall_harness, sync_mechanism):
            self.assertIsInstance(action.choices, StaticChoices)

    def test_sync_all_alias_sets_deep(self) -> None:
        parser = skills_cli.build_parser()
        deep_args = parser.parse_args(["sync", "--deep"])
        all_args = parser.parse_args(["sync", "--all"])
        self.assertTrue(deep_args.deep)
        self.assertTrue(all_args.deep)


def test_managed_skill_policy_resolves_saved_flags_and_explicit_developer(tmp_path, monkeypatch):
    import json
    from astrid.installer import service

    record = tmp_path / "install.json"
    record.write_text(json.dumps({"owner": service.LABEL, "program_root": str(tmp_path / "program"),
                                  "choices": {"skills": False, "skill_copies": True}}), encoding="utf-8")
    monkeypatch.setattr(service, "install_selection", lambda *_args: record)
    parser = skills_cli.build_parser()
    saved = parser.parse_args(["sync"])
    assert skills_cli._skill_policy(saved) == (False, "copy")
    developer = parser.parse_args(["install", "--developer"])
    assert skills_cli._skill_policy(developer) == (True, "copy")
    explicit = parser.parse_args(["sync", "--skills", "on", "--skill-copies", "off"])
    assert skills_cli._skill_policy(explicit) == (True, "symlink")
    skills_cli._save_explicit_skill_policy(explicit)
    saved_values = json.loads(record.read_text(encoding="utf-8"))["choices"]
    assert saved_values == {"skills": True, "skill_copies": False}
    assert skills_cli._skill_policy(developer) == (True, "symlink")

    off = parser.parse_args(["sync", "--skills", "off"])
    skills_cli._save_explicit_skill_policy(off)
    assert skills_cli._skill_policy(saved) == (False, "symlink")
    assert skills_cli._skill_policy(developer) == (True, "symlink")


def test_explicit_skills_off_install_creates_no_skill_targets():
    fx = _Tmp()
    try:
        assert skills_cli.main(["sync", "--skills", "off", "--dry-run", "--harness", "claude", "--json"]) == 0
        assert not (fx.home / ".claude" / "skills").exists()
        assert not fx.state_path.exists()
        assert skills_cli.main(["install", "--skills", "off", "--harness", "claude", "--json"]) == 0
        assert not (fx.home / ".claude" / "skills").exists()
        assert not fx.state_path.exists()
    finally:
        fx.close()


class ApplyTest(unittest.TestCase):
    def test_apply_creates_symlink_and_is_idempotent(self) -> None:
        fx = _Tmp()
        try:
            adapter = ClaudeAdapter()
            descriptors = _descriptors()
            adapter.apply("install", descriptors)
            target = adapter.target_for(descriptors[0])
            self.assertTrue(target.is_symlink())
            steps = adapter.apply("install", descriptors)
            # Second apply must be a no-op.
            self.assertTrue(all(not step.extras.get("changed") for step in steps))
        finally:
            fx.close()

    def test_codex_agents_md_block_added_then_byte_stable(self) -> None:
        fx = _Tmp()
        try:
            adapter = CodexAdapter()
            descriptors = _descriptors()
            adapter.apply("install", descriptors, all_after_descriptors=descriptors)
            agents_md = fx.home / ".codex" / "AGENTS.md"
            self.assertTrue(agents_md.exists())
            text1 = agents_md.read_text(encoding="utf-8")
            self.assertIn(BEGIN_MARKER, text1)
            self.assertIn(END_MARKER, text1)
            adapter.apply("install", descriptors, all_after_descriptors=descriptors)
            text2 = agents_md.read_text(encoding="utf-8")
            self.assertEqual(text1, text2)
        finally:
            fx.close()

    def test_codex_agents_md_block_removed_on_uninstall(self) -> None:
        fx = _Tmp()
        try:
            adapter = CodexAdapter()
            descriptors = _descriptors()
            adapter.apply("install", descriptors, all_after_descriptors=descriptors)
            adapter.apply("uninstall", descriptors, all_after_descriptors=[])
            text = (fx.home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn(BEGIN_MARKER, text)
            self.assertIn("_no Astrid skills installed_", text)
        finally:
            fx.close()

    def test_codex_block_preserves_surrounding_user_content(self) -> None:
        fx = _Tmp()
        try:
            agents_md = fx.home / ".codex" / "AGENTS.md"
            agents_md.write_text("# my notes\n\npreface line\n", encoding="utf-8")
            adapter = CodexAdapter()
            descriptors = _descriptors()
            adapter.apply("install", descriptors, all_after_descriptors=descriptors)
            text = agents_md.read_text(encoding="utf-8")
            self.assertIn("# my notes", text)
            self.assertIn("preface line", text)
            self.assertIn(BEGIN_MARKER, text)
        finally:
            fx.close()


class HermesExternalDirTest(unittest.TestCase):
    def test_external_dir_install_adds_entry_and_preserves_other_keys(self) -> None:
        fx = _Tmp()
        try:
            cfg_path = fx.home / ".hermes" / "config.yaml"
            cfg_path.write_text(yaml.safe_dump({"other": {"keep": True}, "skills": {"external_dirs": ["/already/here"]}}), encoding="utf-8")
            adapter = HermesAdapter()
            descriptors = _descriptors()
            adapter.apply("install", descriptors, mechanism="external-dir")
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(data["other"], {"keep": True})
            self.assertIn("/already/here", data["skills"]["external_dirs"])
            self.assertEqual(len([entry for entry in data["skills"]["external_dirs"] if entry.endswith("/astrid/packs")]), 1)
        finally:
            fx.close()

    def test_external_dir_uninstall_removes_entry(self) -> None:
        fx = _Tmp()
        try:
            adapter = HermesAdapter()
            descriptors = _descriptors()
            adapter.apply("install", descriptors, mechanism="external-dir")
            adapter.apply("uninstall", descriptors, mechanism="external-dir")
            data = yaml.safe_load((fx.home / ".hermes" / "config.yaml").read_text(encoding="utf-8"))
            external_dirs = (data.get("skills") or {}).get("external_dirs") or []
            self.assertFalse(any(entry.endswith("/astrid/packs") for entry in external_dirs))
        finally:
            fx.close()

    def test_external_dir_off_dry_run_preserves_config_and_install_records(self) -> None:
        fx = _Tmp()
        try:
            skills.install(["rendering"], ["hermes"], mechanism="external-dir", state_path=fx.state_path)
            config_path = fx.home / ".hermes" / "config.yaml"
            config_bytes = config_path.read_bytes()
            state_bytes = fx.state_path.read_bytes()
            entry = str(PACKS_DIR_FOR_EXTERNAL.resolve())

            report = skills.disable_owned(state_path=fx.state_path, dry_run=True)

            self.assertEqual(config_path.read_bytes(), config_bytes)
            self.assertEqual(fx.state_path.read_bytes(), state_bytes)
            action = next(row for row in report["actions"] if row["harness"] == "hermes")
            self.assertIn(entry, action["removed"])
        finally:
            fx.close()

    def test_external_dir_off_removes_only_recorded_entry_and_reports_it(self) -> None:
        fx = _Tmp()
        try:
            skills.install(["rendering"], ["hermes"], mechanism="external-dir", state_path=fx.state_path)
            config_path = fx.home / ".hermes" / "config.yaml"
            entry = str(PACKS_DIR_FOR_EXTERNAL.resolve())
            config_before = (
                b"# preserve header spacing  \r\n"
                b'unrelated:\r\n  raw: "keep  these bytes"\r\n'
                b"skills:\r\n  mode: precise\r\n  external_dirs:\r\n"
                + f"  - {entry}\r\n".encode("utf-8")
                + b"  - /unrelated/external-skills # keep inline comment\r\n"
                + b"  # preserve list note bytes\r\n"
            )
            config_path.write_bytes(config_before)
            expected_config = config_before.replace(f"  - {entry}\r\n".encode("utf-8"), b"", 1)

            report = skills.disable_owned(state_path=fx.state_path)

            self.assertEqual(config_path.read_bytes(), expected_config)
            after = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(after["skills"]["external_dirs"], ["/unrelated/external-skills"])
            self.assertEqual(after["unrelated"], {"raw": "keep  these bytes"})
            action = next(row for row in report["actions"] if row["harness"] == "hermes")
            self.assertIn(entry, action["removed"])
        finally:
            fx.close()


class DoctorTest(unittest.TestCase):
    def test_doctor_reports_drift_when_target_renamed(self) -> None:
        fx = _Tmp()
        try:
            skills.install(pack_ids=["_core"], harness_names=["claude"])
            adapter = ClaudeAdapter()
            descriptor = next(d for d in _descriptors() if d.pack_id == "_core")
            target = adapter.target_for(descriptor)
            target.unlink()
            target.symlink_to(fx.tmp / "does-not-exist")
            report = skills.doctor()
            failures = [r for r in report["results"] if not r["ok"] and r["pack"] == "_core"]
            self.assertTrue(failures, msg=str(report))
        finally:
            fx.close()


class SyncPreservesInstalledPacksTest(unittest.TestCase):
    def test_default_sync_preserves_and_refreshes_individual_pack_install(self) -> None:
        fx = _Tmp()
        try:
            with TemporaryDirectory() as tmp:
                md = Path(tmp) / "SKILL.md"
                md.write_text("---\nname: astrid\n---\n\n# Astrid\n", encoding="utf-8")
                skills.install(pack_ids=["rendering"], harness_names=["claude"], state_path=fx.state_path)
                claude = ClaudeAdapter()
                core = next(d for d in _descriptors() if d.pack_id == "_core")
                rendering = next(d for d in _descriptors() if d.pack_id == "rendering")
                target = claude.target_for(rendering)
                target.unlink()
                target.symlink_to(core.skill_dir)

                skills.sync(skill_md_path=md, state_path=fx.state_path)

                data = state.load(fx.state_path)
                self.assertIn("rendering", data["installs"]["claude"])
                self.assertTrue(target.is_symlink())
                self.assertEqual(target.resolve(), rendering.skill_dir.resolve())
                report = skills.check(skill_md_path=md, state_path=fx.state_path)
                self.assertFalse(report["has_drift"], msg=str(report))
        finally:
            fx.close()

    def test_default_sync_prunes_state_record_and_orphan_link_for_deleted_pack(self) -> None:
        fx = _Tmp()
        try:
            with TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                md = tmp_path / "SKILL.md"
                md.write_text("---\nname: astrid\n---\n\n# Astrid\n", encoding="utf-8")
                core = next(d for d in _descriptors() if d.pack_id == "_core")
                vanished = _write_fake_descriptor(tmp_path, "vanished")

                with mock.patch("astrid.skills.list_skills", return_value=[core, vanished]):
                    skills.install(
                        pack_ids=["vanished"],
                        harness_names=["claude"],
                        state_path=fx.state_path,
                    )
                claude = ClaudeAdapter()
                target = claude.target_for(vanished)
                self.assertTrue(target.is_symlink())
                shutil.rmtree(vanished.skill_dir.parent)

                with mock.patch("astrid.skills.list_skills", return_value=[core]):
                    skills.sync(skill_md_path=md, state_path=fx.state_path)

                data = state.load(fx.state_path)
                self.assertNotIn("vanished", data["installs"]["claude"])
                self.assertFalse(target.exists())
                self.assertFalse(target.is_symlink())
        finally:
            fx.close()

    def test_check_tracks_individual_install_and_missing_link(self) -> None:
        fx = _Tmp()
        try:
            with TemporaryDirectory() as tmp:
                md = Path(tmp) / "SKILL.md"
                md.write_text("---\nname: astrid\n---\n\n# Astrid\n", encoding="utf-8")
                skills.install(pack_ids=["rendering"], harness_names=["claude"], state_path=fx.state_path)
                skills.sync(skill_md_path=md, state_path=fx.state_path)

                clean = skills.check(skill_md_path=md, state_path=fx.state_path)
                self.assertFalse(clean["has_drift"], msg=str(clean))

                rendering = next(d for d in _descriptors() if d.pack_id == "rendering")
                ClaudeAdapter().target_for(rendering).unlink()
                drift = skills.check(skill_md_path=md, state_path=fx.state_path)
                self.assertTrue(drift["has_drift"])
                self.assertIn({"harness": "claude", "pack": "rendering"}, drift["missing"])
        finally:
            fx.close()


class DriftAndHealTest(unittest.TestCase):
    """list/doctor must cross-check the filesystem and self-heal on demand."""

    def _install_then_wipe_state(self, harness: str, *, mechanism: str = "symlink") -> _Tmp:
        fx = _Tmp()
        kwargs: dict = {}
        if harness == "hermes":
            kwargs["mechanism"] = mechanism
        skills.install(pack_ids=["_core"], harness_names=[harness], **kwargs)
        # Wipe the state file but leave the filesystem install intact.
        sp = state.state_path()
        if sp.exists():
            sp.unlink()
        return fx

    def test_list_with_state_missing_but_symlinks_present_reports_drift_claude(self) -> None:
        fx = self._install_then_wipe_state("claude")
        try:
            report = skills.list_state()
            core = next(p for p in report["packs"] if p["pack_id"] == "_core")
            entry = core["harnesses"]["claude"]
            self.assertTrue(entry["installed"])
            self.assertTrue(entry["fs_installed"])
            self.assertFalse(entry["state_installed"])
            self.assertTrue(entry["drift"])
        finally:
            fx.close()

    def test_list_with_state_missing_but_symlinks_present_reports_drift_codex(self) -> None:
        fx = self._install_then_wipe_state("codex")
        try:
            report = skills.list_state()
            core = next(p for p in report["packs"] if p["pack_id"] == "_core")
            entry = core["harnesses"]["codex"]
            self.assertTrue(entry["fs_installed"])
            self.assertTrue(entry["drift"])
        finally:
            fx.close()

    def test_list_with_state_missing_but_symlinks_present_reports_drift_hermes(self) -> None:
        fx = self._install_then_wipe_state("hermes")
        try:
            report = skills.list_state()
            core = next(p for p in report["packs"] if p["pack_id"] == "_core")
            entry = core["harnesses"]["hermes"]
            self.assertTrue(entry["fs_installed"])
            self.assertTrue(entry["drift"])
        finally:
            fx.close()

    def test_list_with_state_claiming_but_symlink_missing_reports_drift(self) -> None:
        fx = _Tmp()
        try:
            # Manually record an install in the state file with no on-disk evidence.
            data = state.load()
            state.record_install(data, "claude", "_core", target="/tmp/fake", mechanism="symlink")
            state.save(data)
            report = skills.list_state()
            core = next(p for p in report["packs"] if p["pack_id"] == "_core")
            entry = core["harnesses"]["claude"]
            self.assertTrue(entry["state_installed"])
            self.assertFalse(entry["fs_installed"])
            self.assertTrue(entry["drift"])
        finally:
            fx.close()

    def test_doctor_heal_rewrites_state_from_filesystem_reality(self) -> None:
        fx = self._install_then_wipe_state("claude")
        try:
            # Pre-condition: state file gone.
            self.assertFalse(state.state_path().exists())
            report = skills.doctor(heal=True)
            self.assertTrue(any(d["pack"] == "_core" and d["harness"] == "claude" for d in report["drift"]))
            self.assertTrue(any(h["pack"] == "_core" and h["harness"] == "claude" for h in report["healed"]))
            # Post-condition: state file now records the install.
            data = state.load()
            self.assertIn("_core", data["installs"]["claude"])
            # Re-running doctor without heal should now show no drift.
            report2 = skills.doctor()
            self.assertEqual(report2["drift"], [])
        finally:
            fx.close()

    def test_doctor_heal_works_for_codex_and_hermes(self) -> None:
        for harness in ("codex", "hermes"):
            fx = self._install_then_wipe_state(harness)
            try:
                report = skills.doctor(heal=True)
                self.assertTrue(
                    any(h["harness"] == harness for h in report["healed"]),
                    msg=f"{harness}: no healed entries: {report}",
                )
                data = state.load()
                self.assertIn("_core", data["installs"][harness])
            finally:
                fx.close()

    def test_doctor_heal_clears_state_when_filesystem_disagrees(self) -> None:
        fx = _Tmp()
        try:
            data = state.load()
            state.record_install(data, "claude", "_core", target="/tmp/fake", mechanism="symlink")
            state.save(data)
            report = skills.doctor(heal=True)
            self.assertTrue(any(d["kind"] == "fs-missing" for d in report["drift"]))
            self.assertTrue(any(h["action"] == "removed-from-state" for h in report["healed"]))
            data2 = state.load()
            self.assertNotIn("_core", data2["installs"]["claude"])
        finally:
            fx.close()


class StateRoundtripTest(unittest.TestCase):
    def test_state_round_trip(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "skills.json"
            data = state.load(path)
            state.record_install(data, "claude", "_core", target="/x", mechanism="symlink")
            state.save(data, path)
            reloaded = state.load(path)
            self.assertEqual(reloaded["installs"]["claude"]["_core"]["target"], "/x")


class ExternalDiscoveryTest(unittest.TestCase):
    """External skills enter discovery only through the canonical opt-in path."""

    def _write_external_pack(self, root: Path, pack_id: str) -> Path:
        pack_root = root / pack_id
        skill_dir = pack_root / "skill"
        skill_dir.mkdir(parents=True)
        (pack_root / "pack.yaml").write_text(
            f"schema_version: 1\nid: {pack_id}\nname: Installed Demo\nversion: 0.1.0\n",
            encoding="utf-8",
        )
        (skill_dir / "SKILL.md").write_text(
            "---\nname: installed-demo\ndescription: Installed demo skill.\n---\nBody.\n",
            encoding="utf-8",
        )
        return pack_root

    def test_external_pack_is_not_a_default_discovery_authority(self) -> None:
        with TemporaryDirectory() as tmp:
            self._write_external_pack(Path(tmp), "external_demo")
            # A pack outside the source tree is not consulted by default
            # discovery; only the canonical source-tree scan is implicit.
            with mock.patch.dict("os.environ", {"ASTRID_PACKS_PATH": ""}, clear=False):
                descriptors = discovery.list_skills()
            pack_ids = [d.pack_id for d in descriptors]
            self.assertNotIn("external_demo", pack_ids)
            self.assertIn("_core", pack_ids)

    def test_explicit_packs_dir_does_not_pull_external(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            external_root = root / "external"
            self._write_external_pack(external_root, "external_demo")
            scan_dir = root / "scan"
            scan_dir.mkdir()
            descriptors = discovery.list_skills(scan_dir)
            self.assertEqual(descriptors, [])


class LintTest(unittest.TestCase):
    def test_lint_flags_hermes_token(self) -> None:
        findings = discovery.lint_shared_skill_md("Hello ${HERMES_HOME}!")
        self.assertTrue(findings)

    def test_lint_flags_shell_backtick_token(self) -> None:
        findings = discovery.lint_shared_skill_md("see !`uname -a` here")
        self.assertTrue(findings)

    def test_lint_passes_clean_text(self) -> None:
        self.assertEqual(discovery.lint_shared_skill_md("nothing forbidden here"), [])


class GenerationSkillDiscoveryTest(unittest.TestCase):
    """Verify that ``astrid skills list`` surfaces the updated generation skill."""

    @classmethod
    def setUpClass(cls) -> None:
        descriptors = discovery.list_skills()
        cls._descriptors = descriptors
        gen = next((d for d in descriptors if d.pack_id == "generation"), None)
        assert gen is not None, "generation pack must be discoverable via list_skills()"
        cls._desc = gen
        cls._raw_text = gen.skill_md.read_text(encoding="utf-8")

    def test_generation_pack_is_discovered(self) -> None:
        self.assertIsNotNone(self._desc)
        self.assertEqual(self._desc.pack_id, "generation")

    def test_description_references_facade(self) -> None:
        """The generation skill summary names the one front door, ``generate(entry, inputs, placement)``."""
        self.assertIn("generate(entry, inputs, placement)", self._desc.description)

    def test_short_description_includes_facade_or_generation(self) -> None:
        """The short description (what ``astrid skills list`` prints) is non-empty
        and references generation or the facade."""
        sd = self._desc.short_description
        self.assertTrue(sd, "short_description must be non-empty")
        self.assertTrue(
            "generate" in sd.lower() or "generation" in sd.lower(),
            f"short_description should mention generation: {sd!r}",
        )

    def test_skill_text_contains_facade_image_example(self) -> None:
        """The SKILL.md body shows the Python front door with an image entry:
        ``generate.generate("image/...", {...}, placement=...)``."""
        self.assertIn('generate.generate("image/', self._raw_text)
        self.assertIn("placement=", self._raw_text)

    def test_skill_text_contains_facade_video_example(self) -> None:
        """The SKILL.md body shows a video entry through the CLI, which takes the same entry, inputs and placement."""
        self.assertIn("generate video --entry h3", self._raw_text)

    def test_skill_text_contains_openai_exclusion_note(self) -> None:
        """The SKILL.md body states that hosted provider APIs, OpenAI among them, are a separate
        route and not a placement, so the skill never offers one as a placement."""
        text = " ".join(self._raw_text.split())  # the skill wraps its prose; match across line breaks
        self.assertIn("OpenAI", text)
        self.assertIn("are not part of this placement model", text)


if __name__ == "__main__":
    unittest.main()


def test_view_sync_retains_selection_and_migrates_old_installs():
    import json

    fx = _Tmp()
    try:
        old = state._empty_state()
        old.pop("skills")
        old["installs"]["claude"]["generation"] = {"target": "old", "mechanism": "symlink"}
        old["installs"]["claude"]["rendering"] = {"target": "old", "mechanism": "symlink"}
        fx.state_path.write_text(json.dumps(old), encoding="utf-8")
        skills.sync(harness_names=["claude"], state_path=fx.state_path)
        data = state.load(fx.state_path)
        assert data["skills"]["claude"]["skills"]["rendering"]["selected"]
        assert (fx.home / ".claude/skills/astrid-rendering").is_symlink()
        view = fx.tmp / "skills/claude/packs"
        assert (view / "generation/SKILL.md").is_file()
        skills.uninstall(["generation"], ["claude"], state_path=fx.state_path)
        skills.sync(harness_names=["claude"], state_path=fx.state_path)
        assert not (view / "generation").exists()
    finally:
        fx.close()


def test_copy_sync_is_idempotent_and_off_preserves_edited_copy_and_foreign_link():
    fx = _Tmp()
    try:
        skills.sync(harness_names=["claude"], mechanism="copy", state_path=fx.state_path)
        first = skills.sync(harness_names=["claude"], mechanism="copy", state_path=fx.state_path)
        second = skills.sync(harness_names=["claude"], mechanism="copy", state_path=fx.state_path)
        adapter = ClaudeAdapter()
        core = next(d for d in _descriptors() if d.pack_id == "_core")
        target = adapter.target_for(core)
        assert target.is_dir() and not target.is_symlink()
        assert (target / ".astrid-managed-skill.json").is_file()
        assert all(not step["extras"].get("changed") for action in second["actions"] for step in action["steps"])
        skills.sync(harness_names=["claude"], mechanism="symlink", state_path=fx.state_path)
        assert target.is_symlink()
        skills.sync(harness_names=["claude"], mechanism="copy", state_path=fx.state_path)
        assert target.is_dir() and not target.is_symlink()
        data_before_off = state.load(fx.state_path)

        edited = target / "SKILL.md"
        edited.write_text(edited.read_text(encoding="utf-8") + "user edit\n", encoding="utf-8")
        foreign = adapter.skills_dir / "astrid-foreign"
        foreign.symlink_to(fx.tmp / "foreign-target")
        report = skills.disable_owned(state_path=fx.state_path)
        assert edited.exists()
        assert foreign.is_symlink()
        assert any("edited Astrid managed copy preserved" in row["reason"] for row in report["conflicts"])
        assert any("not confirmed Astrid-owned" in row["reason"] for row in report["ambiguous"])
        data_after_off = state.load(fx.state_path)
        assert data_after_off["disabled_defaults"] == data_before_off["disabled_defaults"]
    finally:
        fx.close()


def test_copy_mechanism_uses_managed_copies_for_all_harnesses():
    fx = _Tmp()
    try:
        report = skills.sync(harness_names=["claude", "codex", "hermes"], mechanism="copy", state_path=fx.state_path)
        for harness, adapter_type in (("claude", ClaudeAdapter), ("codex", CodexAdapter), ("hermes", HermesAdapter)):
            adapter = adapter_type()
            core = next(d for d in _descriptors() if d.pack_id == "_core")
            target = adapter.target_for(core)
            assert target.is_dir() and not target.is_symlink()
            assert (target / ".astrid-managed-skill.json").is_file()
        assert report["actions"]
    finally:
        fx.close()


def test_off_reconciles_marker_owned_state_view_without_install_records():
    fx = _Tmp()
    try:
        skills.sync(harness_names=["claude"], state_path=fx.state_path)
        adapter = ClaudeAdapter()
        target = adapter.skills_dir / "astrid"
        view = fx.state_path.parent / "skills" / "claude"
        assert target.is_symlink()
        assert (view / ".astrid-managed-packs").is_file()

        data = state.load(fx.state_path)
        data["installs"]["claude"] = {}
        state.save(data, fx.state_path)
        report = skills.disable_owned(state_path=fx.state_path)

        assert not target.exists() and not target.is_symlink()
        assert not view.exists()
        assert any(str(view) in action["removed"] for action in report["actions"])
    finally:
        fx.close()


def test_extra_selective_refresh_and_uninstall():
    from dataclasses import replace

    fx = _Tmp()
    try:
        core = next(d for d in discovery.list_skills() if d.pack_id == "_core")
        main = _write_fake_descriptor(fx.tmp, "fixture")
        extras = []
        for name in ("a", "b"):
            directory = main.skill_dir / name
            directory.mkdir()
            md = directory / "SKILL.md"
            md.write_text(f"---\nname: {name}\ndescription: test\n---\n[ref](../references/x.md)\n", encoding="utf-8")
            extras.append(replace(main, skill=name, skill_dir=directory, skill_md=md))
        reference = main.skill_dir / "references/x.md"
        reference.parent.mkdir()
        reference.write_text("reference", encoding="utf-8")
        with mock.patch.object(skills, "list_skills", return_value=[core, main]), mock.patch.object(discovery, "list_extras", return_value=extras):
            skills.install(["fixture"], ["claude"], all_skills=True, state_path=fx.state_path)
            view = fx.tmp / "skills/claude/packs/fixture"
            before = (view / "b/SKILL.md").read_bytes()
            for extra in extras:
                extra.skill_md.write_text(extra.skill_md.read_text() + "changed\n")
            report = skills.sync(harness_names=["claude"], state_path=fx.state_path, refresh_ids={"fixture-a"})
            assert report["actions"][0]["outdated"] == ["fixture-a", "fixture-b"]
            assert report["actions"][0]["changed"] == ["fixture-a"]
            assert (view / "b/SKILL.md").read_bytes() == before
            listing = skills.list_state(state_path=fx.state_path)
            b_row = next(row for row in listing["packs"] if row["id"] == "fixture-b")
            assert b_row["role"] == "extra"
            assert b_row["harnesses"]["claude"]["selected"]
            assert b_row["harnesses"]["claude"]["outdated"]
            registry_text = (fx.tmp / "skills/claude/creative-work/references/packs.md").read_text()
            assert "fixture / a" in registry_text and "selected" in registry_text
            link = fx.home / ".claude/skills/astrid-fixture-a/SKILL.md"
            assert str(reference).replace(str(main.skill_dir), str(view)) in link.read_text()
            skills.uninstall(["fixture"], ["claude"], skill_names=["a"], state_path=fx.state_path)
            assert not link.exists()
            assert (fx.home / ".claude/skills/astrid-fixture-b").is_symlink()
            import errno
            original_symlink = Path.symlink_to

            def no_extra_symlink(path, *args, **kwargs):
                if path.name == "astrid-fixture-a":
                    raise OSError(errno.EPERM, "symlinks unavailable")
                return original_symlink(path, *args, **kwargs)

            with mock.patch.object(Path, "symlink_to", no_extra_symlink):
                skills.install(["fixture"], ["claude"], skill_names=["a"], state_path=fx.state_path)
            assert link.is_file() and not link.parent.is_symlink()
            assert str(view / "references/x.md") in link.read_text()
            skills.uninstall(["fixture"], ["claude"], skill_names=["a"], state_path=fx.state_path)
            skills.sync(harness_names=["claude"], deep=True, state_path=fx.state_path)
            assert link.is_file()
    finally:
        fx.close()


def test_concurrent_installs_keep_both_selections(tmp_path):
    import os
    import subprocess
    import sys

    home = tmp_path / "claude"
    home.mkdir()
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(home), "ASTRID_STATE_HOME": str(tmp_path / "state")}
    commands = [
        subprocess.Popen([sys.executable, "-m", "astrid", "skills", "install", pack, "--harness", "claude"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for pack in ("rendering", "generation")
    ]
    for process in commands:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, (stdout, stderr)
    syncs = [
        subprocess.Popen([sys.executable, "-m", "astrid", "skills", "sync", "--harness", "claude"],
                         env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    for process in syncs:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, (stdout, stderr)
    data = state.load(tmp_path / "state/astrid/skills.json")
    assert all(data["skills"]["claude"]["skills"][pack]["selected"] for pack in ("rendering", "generation"))


def test_hermes_external_directory_reports_visibility():
    fx = _Tmp()
    try:
        report = skills.install(["rendering"], ["hermes"], mechanism="external-dir", state_path=fx.state_path)
        assert report["actions"][0]["visibility"] == "external-dir: all skills visible"
        listing = skills.list_state(state_path=fx.state_path)
        rendering = next(row for row in listing["packs"] if row["id"] == "rendering")
        assert rendering["harnesses"]["hermes"]["visibility"] == "external-dir: all skills visible"
    finally:
        fx.close()


def test_install_survives_normal_view_sync():
    fx = _Tmp()
    try:
        skills.install(["rendering"], ["claude"], state_path=fx.state_path)
        skills.sync(harness_names=["claude"], state_path=fx.state_path)
        assert state.load(fx.state_path)["skills"]["claude"]["skills"]["rendering"]["selected"]
        assert (fx.home / ".claude/skills/astrid-rendering").is_symlink()
    finally:
        fx.close()


def test_update_alias_accepts_pack_and_skill():
    args = skills_cli.build_parser().parse_args(["update", "rendering", "--skill", "worlds"])
    assert args.handler is skills_cli._cmd_sync
    assert args.pack == "rendering"
    assert args.skill == ["worlds"]


def test_timeline_main_skill_stays_under_budget_and_covers_first_run():
    skill = Path(__file__).parents[1] / "astrid/packs/timeline/docs/SKILL.md"
    content = skill.read_text(encoding="utf-8")
    frontmatter, body = content.split("---", 2)[1:]

    assert len(content.split()) < 500
    assert "video" in frontmatter.lower() and "timeline" in frontmatter.lower()
    assert "timelines list --recent" in body
    assert "ask the user and name the candidates" in body
    assert "same Python that runs Astrid" in " ".join(body.split())
    assert "timelines create" in body and "t.add(path, at=at" in body
    assert 'Timeline.open("<project>", "<timeline>")' in body
    assert "timelines visualize" in body and 'r[\"data\"][\"editor_url\"]' in body
    assert "`timeline-series`" in body
    assert "astrid-timeline-series" not in body
    assert "Not possible yet" not in body


def test_timeline_main_skill_code_blocks_run_with_scratch_runtime(real_runtime, tmp_path, monkeypatch):
    import os
    import re
    import subprocess
    import sys

    from tests.helpers.runtime import runtime_checkout, vendored_runtime_commit

    runtime_root = runtime_checkout(vendored_runtime_commit())
    daemon = real_runtime(tmp_path / "timeline-skill-realm", support_root=tmp_path / "timeline-skill-support",
                          commit=vendored_runtime_commit(), authoring_profile="astrid")
    env = {
        **os.environ,
        "PYTHONPATH": f"{Path(__file__).parents[1]}:{runtime_root}",
        "ASTRID_RUNTIME_CHECKOUT": str(runtime_root),
        "ASTRID_RUNTIME_DATA_ROOT": str(tmp_path / "timeline-skill-data"),
        "ASTRID_TIMELINE_EVAL_ENDPOINT": daemon.endpoint,
        "ASTRID_TIMELINE_EVAL_CREDENTIAL": str(daemon.credential_path),
        "ASTRID_TIMELINE_EVAL_REALM_ID": daemon.service.realm["id"],
        "ASTRID_TIMELINE_EVAL_ACTOR_ID": "owner",
    }
    skill_bin = tmp_path / "skill-bin"
    skill_bin.mkdir()
    shim = skill_bin / "python3"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    shim.chmod(0o755)
    env["PATH"] = f"{skill_bin}{os.pathsep}{env['PATH']}"
    project = daemon.service.create_project({"slug": "skill-demo", "name": "Skill demo"})["id"]
    monkeypatch.setenv("ASTRID_TIMELINE_EVAL_ENDPOINT", daemon.endpoint)
    monkeypatch.setenv("ASTRID_TIMELINE_EVAL_CREDENTIAL", str(daemon.credential_path))
    monkeypatch.setenv("ASTRID_TIMELINE_EVAL_REALM_ID", daemon.service.realm["id"])
    monkeypatch.setenv("ASTRID_TIMELINE_EVAL_ACTOR_ID", "owner")

    created = subprocess.run(
        [sys.executable, "-m", "astrid", "timelines", "create", "--project", project,
         "--slug", "intro", "--name", "Intro"],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=120,
    )
    assert created.returncode == 0, created.stdout + created.stderr

    from astrid.sdk.timeline.api import Timeline

    source = Path(__file__).parent / "fixtures/reshape/hype_regression/main.mp4"
    still = Path(__file__).parent / "fixtures/reshape/hype_regression/poster.png"
    tl = Timeline.open(project, "intro")
    tl.commit(lambda t: t.add(str(source), at=0, track="V1", id="intro"), label="Seed intro",
              idempotency_key="seed-intro")

    skill = Path(__file__).parents[1] / "astrid/packs/timeline/docs/SKILL.md"
    blocks = re.findall(r"```(?:python|bash)\n(.*?)\n```", skill.read_text(encoding="utf-8"), re.S)
    assert len(blocks) == 3
    replacements = {
        "<project>": project,
        "<clip-one>": str(still),
        "<clip-two>": str(still),
    }
    for block in blocks:
        replacements["<timeline>"] = "intro" if block.startswith("from astrid.sdk") else "new-opening"
        for marker, value in replacements.items():
            block = block.replace(marker, value)
        if block.startswith("from astrid.sdk"):
            exec(compile(block, str(skill), "exec"), {"__name__": "__main__"})
        else:
            result = subprocess.run(["bash", "-c", block], cwd=tmp_path, env=env, text=True,
                                    capture_output=True, timeout=120)
            assert result.returncode == 0, result.stdout + result.stderr


# R1-X1: exercise the real adapters and public compositions at each boundary.

_ADAPTER_TYPES = (ClaudeAdapter, CodexAdapter, HermesAdapter)


@pytest.fixture
def foreign_root():
    # The task-local TMPDIR may itself contain an "astrid" component. These
    # tiny protected inputs must distinguish that heuristic's two branches.
    root = os.environ.get("TEMP") if os.name == "nt" else "/tmp"
    with TemporaryDirectory(prefix="skill-foreign-", dir=root) as directory:
        yield Path(directory)


@pytest.mark.parametrize("adapter_type", _ADAPTER_TYPES)
@pytest.mark.parametrize("mechanism", ["symlink", "copy"])
@pytest.mark.parametrize("foreign_name", ["notes", "projects/astrid/notes"])
def test_replacement_and_uninstall_preserve_foreign_links(tmp_path, foreign_root, adapter_type, mechanism, foreign_name):
    descriptor = _write_fake_descriptor(tmp_path / "declared", "demo")
    adapter = adapter_type(home=tmp_path / "harness")
    target = adapter.target_for(descriptor)
    foreign = foreign_root / foreign_name
    foreign.mkdir(parents=True)
    assert ("astrid" in foreign.resolve().parts) == ("astrid" in Path(foreign_name).parts)
    protected = foreign / "SKILL.md"
    protected.write_bytes(b"user bytes\r\n  \n")
    target.parent.mkdir(parents=True)
    target.symlink_to(foreign)
    for action in ("install", "uninstall"):
        planned = adapter.plan(action, [descriptor], mechanism=mechanism, force=True)
        assert any(step.target == target and step.extras.get("conflict") for step in planned)
        steps = adapter.apply(action, [descriptor], mechanism=mechanism, force=True)
        assert any(step.target == target and step.extras.get("conflict") for step in steps)
        assert target.is_symlink() and target.readlink() == foreign
        assert protected.read_bytes() == b"user bytes\r\n  \n"


@pytest.mark.parametrize("adapter_type", _ADAPTER_TYPES)
@pytest.mark.parametrize("edited", [False, True])
def test_adapter_uninstall_reports_copy_outcome(tmp_path, adapter_type, edited):
    descriptor = _write_fake_descriptor(tmp_path / "declared", "demo")
    adapter = adapter_type(home=tmp_path / "harness")
    adapter.apply("install", [descriptor], mechanism="copy")
    target = adapter.target_for(descriptor)
    if edited:
        (target / "SKILL.md").write_bytes(b"my edited copy\r\n")
    steps = adapter.apply("uninstall", [descriptor])
    removal = next(step for step in steps if step.target == target)
    if edited:
        assert removal.extras.get("conflict")
        assert "edited" in removal.description
        assert (target / "SKILL.md").read_bytes() == b"my edited copy\r\n"
    else:
        assert removal.extras["changed"] and not target.exists()
        assert not any(step.extras.get("changed") for step in adapter.apply("uninstall", [descriptor]))


@pytest.mark.parametrize("adapter_type", _ADAPTER_TYPES)
@pytest.mark.parametrize("known", [False, True])
def test_adapter_uninstall_distinguishes_broken_declared_link(tmp_path, adapter_type, known):
    descriptor = _write_fake_descriptor(tmp_path / "declared", "demo")
    adapter = adapter_type(home=tmp_path / "harness")
    target = adapter.target_for(descriptor)
    target.parent.mkdir(parents=True)
    source = descriptor.skill_dir if known else tmp_path / "foreign/astrid/demo/skill"
    target.symlink_to(source)
    shutil.rmtree(descriptor.skill_dir)
    steps = adapter.apply("uninstall", [descriptor])
    assert target.is_symlink() is (not known)
    assert any(step.extras.get("conflict") for step in steps) is (not known)


@pytest.mark.parametrize("adapter_type", _ADAPTER_TYPES)
@pytest.mark.parametrize("boundary", ["sync", "off", "uninstall"])
@pytest.mark.parametrize("foreign_name", ["notes", "projects/astrid/_core/notes"])
def test_public_boundaries_preserve_retargeted_record(tmp_path, foreign_root, adapter_type, boundary, foreign_name):
    fx = _Tmp()
    try:
        harness = adapter_type.name
        skills.sync(harness_names=[harness], state_path=fx.state_path)
        adapter = adapter_type()
        target = adapter.skills_dir / "astrid"
        before = state.load(fx.state_path)["installs"][harness]["_core"]
        target.unlink()
        foreign = foreign_root / foreign_name
        foreign.mkdir(parents=True)
        assert ("astrid" in foreign.resolve().parts) == ("astrid" in Path(foreign_name).parts)
        protected = foreign / "note"
        protected.write_bytes(b"protected\r\n  ")
        target.symlink_to(foreign)
        if boundary == "sync":
            report = skills.sync(harness_names=[harness], state_path=fx.state_path)
        elif boundary == "off":
            report = skills.disable_owned(state_path=fx.state_path)
        else:
            report = skills.uninstall(["_core"], [harness], state_path=fx.state_path)
        assert target.readlink() == foreign
        assert protected.read_bytes() == b"protected\r\n  "
        assert any(row["path"] == str(target) for row in report["conflicts"])
        assert state.load(fx.state_path)["installs"][harness]["_core"] == before
    finally:
        fx.close()


@pytest.mark.parametrize("harness", ["claude", "codex", "hermes"])
def test_public_uninstall_preserves_edited_copy_record_and_selection(harness):
    fx = _Tmp()
    try:
        skills.install(["rendering"], [harness], mechanism="copy", state_path=fx.state_path)
        adapter = {"claude": ClaudeAdapter, "codex": CodexAdapter, "hermes": HermesAdapter}[harness]()
        target = adapter.skills_dir / "astrid-rendering"
        edited = target / "SKILL.md"
        edited.write_bytes(edited.read_bytes() + b"\nmy change\r\n")
        snapshot = edited.read_bytes()
        before = state.load(fx.state_path)
        report = skills.uninstall(["rendering"], [harness], state_path=fx.state_path)
        after = state.load(fx.state_path)
        assert edited.read_bytes() == snapshot
        assert report["conflicts"]
        assert after["installs"][harness]["rendering"] == before["installs"][harness]["rendering"]
        assert after["skills"][harness]["skills"]["rendering"] == before["skills"][harness]["skills"]["rendering"]
    finally:
        fx.close()


@pytest.mark.parametrize("boundary", ["sync", "off"])
@pytest.mark.parametrize("recorded", [False, True])
def test_orphan_link_ownership_requires_exact_recorded_source(tmp_path, boundary, recorded):
    fx = _Tmp()
    try:
        target = fx.home / ".claude/skills/astrid-vanished"
        target.parent.mkdir()
        source = tmp_path / "source/vanished/skill"
        source.mkdir(parents=True)
        target.symlink_to(source)
        if recorded:
            data = state.load(fx.state_path)
            # Legacy destination-only record cannot prove a retargeted link.
            data["installs"]["claude"]["vanished"] = {"target": str(target), "mechanism": "symlink"}
            state.save(data, fx.state_path)
        for _ in range(2):
            report = (skills.sync(harness_names=["claude"], state_path=fx.state_path) if boundary == "sync"
                      else skills.disable_owned(state_path=fx.state_path))
            assert target.is_symlink() and target.readlink() == source
            assert any(row["path"] == str(target) for row in report["conflicts"])
            if recorded:
                assert "vanished" in state.load(fx.state_path)["installs"]["claude"]
    finally:
        fx.close()


@pytest.mark.parametrize("boundary", ["off", "sync"])
@pytest.mark.parametrize("retargeted", [False, True])
def test_vanished_source_record_removes_only_unchanged_broken_link(tmp_path, monkeypatch, boundary, retargeted):
    fx = _Tmp()
    try:
        # Discover a real declared external pack through the public source edge.
        source_root = tmp_path / "external-packs"
        minimal = source_root / "minimal"
        (minimal / "docs").mkdir(parents=True)
        (minimal / "pack.yaml").write_text(yaml.safe_dump({
            "schema_version": 3, "id": "minimal", "name": "Minimal", "version": "0.1.0",
            "pack_api": ".".join(map(str, VERSION)),
            "documentation": {"kind": "skill", "path": "docs/SKILL.md"},
        }), encoding="utf-8")
        (minimal / "docs/SKILL.md").write_text(
            "---\nname: minimal\ndescription: Minimal skill.\n---\nMinimal.\n", encoding="utf-8")
        monkeypatch.setenv("ASTRID_PACKS_PATH", str(source_root))
        vanished = next(d for d in skills.list_skills() if d.pack_id == "minimal")
        skills.install([vanished.pack_id], ["claude"], state_path=fx.state_path)
        target = ClaudeAdapter().target_for(vanished)
        if retargeted:
            target.unlink()
            target.symlink_to(tmp_path / "unknown/astrid/minimal/docs")
        shutil.rmtree(vanished.skill_dir)
        report = (skills.disable_owned(state_path=fx.state_path) if boundary == "off" else
                  skills.sync(harness_names=["claude"], state_path=fx.state_path))
        assert target.is_symlink() is retargeted
        assert (vanished.pack_id in state.load(fx.state_path)["installs"]["claude"]) is retargeted
        assert bool(report.get("conflicts")) is retargeted
    finally:
        fx.close()


@pytest.mark.parametrize("boundary", ["install", "uninstall", "off"])
def test_codex_changes_only_bytes_within_fence(boundary):
    fx = _Tmp()
    try:
        adapter = CodexAdapter()
        descriptors = _descriptors()[:1]
        before = b"# user\xff\r\n\r\ntrailing spaces  \r\n\n\n"
        after = b"\r\n\r\n\n  after  \r\n"
        adapter.agents_md.write_bytes(before + BEGIN_MARKER.encode() + b"\nold\n" + END_MARKER.encode() + after)
        if boundary == "off":
            skills.disable_owned(state_path=fx.state_path)
            assert adapter.agents_md.read_bytes() == before + after
        else:
            adapter.apply(boundary, descriptors, all_after_descriptors=descriptors if boundary == "install" else [])
            result = adapter.agents_md.read_bytes()
            assert result[:result.index(BEGIN_MARKER.encode())] == before
            assert result[result.index(END_MARKER.encode()) + len(END_MARKER):] == after
    finally:
        fx.close()


@pytest.mark.parametrize("adapter_type", _ADAPTER_TYPES)
@pytest.mark.parametrize("mechanism", ["copy", "symlink"])
@pytest.mark.parametrize("edited", [False, True])
def test_refresh_owned_copy_after_source_changes(tmp_path, adapter_type, mechanism, edited):
    descriptor = _write_fake_descriptor(tmp_path / "declared", "demo")
    adapter = adapter_type(home=tmp_path / "harness")
    adapter.apply("install", [descriptor], mechanism="copy")
    target = adapter.target_for(descriptor)
    if edited:
        (target / "SKILL.md").write_bytes(b"keep my edit")
    descriptor.skill_md.write_bytes(b"updated source")
    steps = adapter.apply("install", [descriptor], mechanism=mechanism)
    if edited:
        assert any(step.extras.get("conflict") for step in steps)
        assert not target.is_symlink()
        assert (target / "SKILL.md").read_bytes() == b"keep my edit"
    else:
        assert not any(step.extras.get("conflict") for step in steps)
        assert (target / "SKILL.md").read_bytes() == b"updated source"
        assert not any(step.extras.get("changed") for step in adapter.apply("install", [descriptor], mechanism=mechanism))


@pytest.mark.parametrize("entry_kind", ["directory", "file"])
@pytest.mark.parametrize("mechanism", ["copy", "symlink"])
def test_force_preserves_ordinary_foreign_entry(tmp_path, entry_kind, mechanism):
    descriptor = _write_fake_descriptor(tmp_path / "declared", "demo")
    adapter = ClaudeAdapter(home=tmp_path / "harness")
    target = adapter.target_for(descriptor)
    target.parent.mkdir(parents=True)
    if entry_kind == "directory":
        target.mkdir()
        protected = target / "SKILL.md"
    else:
        protected = target
    protected.write_bytes(b"user bytes\r\n")
    steps = adapter.apply("install", [descriptor], force=True, mechanism=mechanism)
    assert any(step.extras.get("conflict") for step in steps)
    assert protected.read_bytes() == b"user bytes\r\n"


@pytest.mark.parametrize("boundary", ["off", "sync"])
def test_recorded_source_is_not_re_resolved_through_later_foreign_symlink(tmp_path, boundary):
    fx = _Tmp()
    try:
        original_source = tmp_path / "original-source"
        original_source.mkdir()
        target = fx.home / ".claude/skills/astrid-vanished"
        target.parent.mkdir()
        target.symlink_to(original_source)
        data = state.load(fx.state_path)
        data["installs"]["claude"]["vanished"] = {
            "target": str(target), "mechanism": "symlink", "source": str(original_source)}
        state.save(data, fx.state_path)
        foreign = tmp_path / "foreign/astrid/notes"
        foreign.mkdir(parents=True)
        (foreign / "note").write_bytes(b"keep these bytes")
        original_source.rmdir()
        original_source.symlink_to(foreign)
        target.unlink()
        target.symlink_to(foreign)
        report = (skills.disable_owned(state_path=fx.state_path) if boundary == "off" else
                  skills.sync(harness_names=["claude"], state_path=fx.state_path))
        assert target.is_symlink() and target.readlink() == foreign
        assert original_source.is_symlink() and original_source.readlink() == foreign
        assert (foreign / "note").read_bytes() == b"keep these bytes"
        assert "vanished" in state.load(fx.state_path)["installs"]["claude"]
        assert any(row["path"] == str(target) for row in report["conflicts"])
    finally:
        fx.close()


@pytest.mark.parametrize("adapter_type", _ADAPTER_TYPES)
@pytest.mark.parametrize("mechanism", ["copy", "symlink"])
def test_extra_install_keeps_preservation_conflict_after_followup_sync(tmp_path, monkeypatch, adapter_type, mechanism):
    fx = _Tmp()
    try:
        pack = tmp_path / "external/demo_pack"
        extra = pack / "docs/extra"
        extra.mkdir(parents=True)
        (pack / "pack.yaml").write_text(yaml.safe_dump({
            "schema_version": 3, "id": "demo_pack", "name": "Demo Pack", "version": "0.1.0",
            "pack_api": ".".join(map(str, VERSION)),
            "documentation": {"kind": "skill", "path": "docs/SKILL.md"},
            "resources": [{"kind": "skill", "path": "docs/extra/SKILL.md"}],
        }), encoding="utf-8")
        (pack / "docs/SKILL.md").write_text("---\nname: demo\ndescription: Main skill.\n---\nMain.\n", encoding="utf-8")
        (extra / "SKILL.md").write_text("---\nname: extra\ndescription: Extra skill.\n---\nExtra.\n", encoding="utf-8")
        monkeypatch.setenv("ASTRID_PACKS_PATH", str(pack.parent))
        assert any(d.skill_id == "demo_pack-extra" for d in discovery.list_extras(skills.list_skills()))
        adapter = adapter_type()
        target = adapter.skills_dir / "astrid-demo_pack-extra"
        target.parent.mkdir()
        foreign = tmp_path / "projects/astrid/demo_pack-extra/notes"
        foreign.mkdir(parents=True)
        (foreign / "note").write_bytes(b"protected extra target\r\n")
        target.symlink_to(foreign)
        report = skills.install(["demo_pack"], [adapter.name], skill_names=["extra"],
                                mechanism=mechanism, state_path=fx.state_path)
        assert target.is_symlink() and target.readlink() == foreign
        assert (foreign / "note").read_bytes() == b"protected extra target\r\n"
        assert any(row["path"] == str(target) for row in report["conflicts"])
        assert "demo_pack-extra" not in state.load(fx.state_path)["installs"][adapter.name]
    finally:
        fx.close()
