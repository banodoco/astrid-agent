"""Tests for the multi-harness skills install layer."""

from __future__ import annotations

import argparse
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import yaml

from astrid import skills
from astrid.core.cli_choices import StaticChoices
from astrid.skills import cli as skills_cli
from astrid.skills import discovery, state
from astrid.skills.harnesses import (
    ClaudeAdapter,
    CodexAdapter,
    HermesAdapter,
)
from astrid.skills.harnesses.codex import BEGIN_MARKER, END_MARKER


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
                skills.install(pack_ids=["foley"], harness_names=["claude"], state_path=fx.state_path)
                claude = ClaudeAdapter()
                core = next(d for d in _descriptors() if d.pack_id == "_core")
                foley = next(d for d in _descriptors() if d.pack_id == "foley")
                target = claude.target_for(foley)
                target.unlink()
                target.symlink_to(core.skill_dir)

                skills.sync(skill_md_path=md, state_path=fx.state_path)

                data = state.load(fx.state_path)
                self.assertIn("foley", data["installs"]["claude"])
                self.assertTrue(target.is_symlink())
                self.assertEqual(target.resolve(), foley.skill_dir.resolve())
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
                skills.install(pack_ids=["foley"], harness_names=["claude"], state_path=fx.state_path)
                skills.sync(skill_md_path=md, state_path=fx.state_path)

                clean = skills.check(skill_md_path=md, state_path=fx.state_path)
                self.assertFalse(clean["has_drift"], msg=str(clean))

                foley = next(d for d in _descriptors() if d.pack_id == "foley")
                ClaudeAdapter().target_for(foley).unlink()
                drift = skills.check(skill_md_path=md, state_path=fx.state_path)
                self.assertTrue(drift["has_drift"])
                self.assertIn({"harness": "claude", "pack": "foley"}, drift["missing"])
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
        """The generation skill summary describes the ``astrid.generate`` facade."""
        self.assertIn("astrid.generate", self._desc.description)

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
        """The SKILL.md body contains ``astrid.generate.image(`` examples."""
        self.assertIn("astrid.generate.image(", self._raw_text)

    def test_skill_text_contains_facade_video_example(self) -> None:
        """The SKILL.md body contains ``astrid.generate.video(`` examples."""
        self.assertIn("astrid.generate.video(", self._raw_text)

    def test_skill_text_contains_openai_exclusion_note(self) -> None:
        """The SKILL.md body states that ``execution=\"openai\"`` is rejected by the
        facade and that ``generate_image_openai`` remains CLI/executor-only."""
        self.assertIn("generate_image_openai", self._raw_text)
        self.assertIn("execution=\"openai\"", self._raw_text)


if __name__ == "__main__":
    unittest.main()
