"""Verify shipped (non-builtin) executor and orchestrator ids live in matching packs."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from astrid.core.execution.executor.registry import load_default_registry as load_executor_registry
from astrid.core.execution.orchestrator.registry import load_default_registry as load_orchestrator_registry
from astrid.core.pack import qualified_id_pack_id


REPO_ROOT = Path(__file__).resolve().parents[2]


class ShippedPackAlignmentTest(unittest.TestCase):
    def test_every_shipped_executor_first_segment_matches_owning_pack(self) -> None:
        registry = load_executor_registry()
        for executor in registry.list():
            with self.subTest(executor_id=executor.id):
                source_pack = executor.metadata.get("source_pack")
                self.assertIsNotNone(
                    source_pack,
                    f"executor {executor.id!r} missing metadata.source_pack",
                )
                self.assertEqual(
                    qualified_id_pack_id(executor.id),
                    source_pack,
                    f"executor {executor.id!r} first segment does not match source_pack {source_pack!r}",
                )

    def test_every_shipped_orchestrator_first_segment_matches_owning_pack(self) -> None:
        registry = load_orchestrator_registry()
        for orchestrator in registry.list():
            with self.subTest(orchestrator_id=orchestrator.id):
                source_pack = orchestrator.metadata.get("source_pack")
                self.assertIsNotNone(
                    source_pack,
                    f"orchestrator {orchestrator.id!r} missing metadata.source_pack",
                )
                self.assertEqual(
                    qualified_id_pack_id(orchestrator.id),
                    source_pack,
                    f"orchestrator {orchestrator.id!r} first segment does not match source_pack {source_pack!r}",
                )

    def test_known_non_builtin_ids_resolve_to_their_packs(self) -> None:
        registry = load_executor_registry()
        cases = [
            ("vibecomfy.run", "vibecomfy"),
            ("vibecomfy.validate", "vibecomfy"),
        ]
        for executor_id, pack in cases:
            with self.subTest(executor_id=executor_id):
                executor = registry.get(executor_id)
                self.assertEqual(executor.metadata["source_pack"], pack)
                pack_root = Path(executor.metadata["pack_root"])
                self.assertEqual(
                    pack_root,
                    REPO_ROOT / "astrid" / "packs" / pack,
                    f"pack_root for {executor_id} did not land under packs/{pack}/",
                )
                runtime_file = pack_root / executor.metadata["runtime_file"]
                self.assertTrue(
                    runtime_file.is_file(),
                    f"runtime_file for {executor_id} missing at {runtime_file}",
                )

    def test_cli_lists_do_not_register_seinfeld_pack_ids(self) -> None:
        # The 8-family CLI no longer exposes `executors`/`orchestrators`
        # verbs; enumerate ids from the shipped pack.yaml actions instead and
        # assert no Seinfeld pack leaked into any first-party pack.
        packs_root = REPO_ROOT / "astrid" / "packs"
        ids: list[str] = []
        for manifest in sorted(packs_root.glob("*/pack.yaml")):
            actions = yaml.safe_load(manifest.read_text(encoding="utf-8")).get("actions") or {}
            ids.extend(f"{manifest.parent.name}.{action_id}" for action_id in actions)
        self.assertTrue(ids, "expected at least one pack action")
        self.assertFalse(
            any(identifier.startswith("seinfeld.") for identifier in ids),
            f"unexpected Seinfeld ids in the shipped pack tree: {ids}",
        )


if __name__ == "__main__":
    unittest.main()
