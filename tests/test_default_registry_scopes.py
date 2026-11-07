from __future__ import annotations

import unittest
from pathlib import Path

from astrid.core.execution.executor.registry import load_default_registry as load_executor_registry
from astrid.core.execution.orchestrator.registry import (
    load_default_registry as load_orchestrator_registry,
)


class DefaultRegistryScopeTest(unittest.TestCase):
    def test_default_executor_registries_include_packs(self) -> None:
        canonical = load_executor_registry()
        canonical_ids = set(canonical.as_mapping())

        self.assertIn("rendering.render", canonical_ids)
        self.assertIn("vibecomfy.run", canonical_ids)
        self.assertIn("vibecomfy.validate", canonical_ids)

        # youtube, moirae, understanding and video_editing live in poms_packs.
        for moved in ("youtube", "moirae", "understanding", "video_editing"):
            with self.subTest(moved=moved):
                self.assertFalse(
                    any(eid.split(".", 1)[0] == moved for eid in canonical_ids)
                )

        vibecomfy = canonical.get("vibecomfy.run")
        self.assertEqual(vibecomfy.kind, "external")
        self.assertEqual(vibecomfy.metadata["pack_id"], "vibecomfy")
        self.assertEqual(vibecomfy.metadata["source_pack"], "vibecomfy")
        self.assertEqual(vibecomfy.metadata["source"], "pack")
        self.assertTrue(vibecomfy.metadata["pack_root"].endswith("astrid/packs/vibecomfy"))
        self.assertEqual(vibecomfy.metadata["runtime_file"], "actions/run/run.py")

    def test_default_orchestrator_registries_do_not_classify_vibecomfy_as_orchestrator(self) -> None:
        canonical = load_orchestrator_registry(executor_registry=load_executor_registry())
        canonical_ids = set(canonical.as_mapping())

        # Only metadata.local_orchestrator actions are orchestrators; no core
        # pack declares one now (h3_av.transform is a plain executor).
        self.assertEqual(canonical_ids, set())
        self.assertIn("h3_av.transform", load_executor_registry().as_mapping())
        self.assertFalse(any("vibecomfy" in orchestrator_id for orchestrator_id in canonical_ids))
        self.assertFalse(any(orchestrator_id == "youtube.upload" for orchestrator_id in canonical_ids))
        with self.assertRaises(KeyError):
            canonical.get("vibecomfy.run")

    def test_canonical_builtin_executor_runtime_module(self) -> None:
        canonical = load_executor_registry()
        render = canonical.get("rendering.render")
        self.assertEqual(render.metadata["runtime_module"], "astrid.packs.rendering.actions.render.run")

    def test_external_executor_roots_are_pack_native(self) -> None:
        registry = load_executor_registry()

        with self.assertRaises(KeyError):
            registry.get("moirae.moirae")
        vibecomfy = registry.get("vibecomfy.run").metadata
        self.assertTrue(vibecomfy["pack_root"].endswith("astrid/packs/vibecomfy"))
        self.assertEqual(vibecomfy["runtime_file"], "actions/run/run.py")


if __name__ == "__main__":
    unittest.main()
