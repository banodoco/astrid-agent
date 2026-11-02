"""Parity tests for the consolidated pack discovery helper (Step 12).

These exercise the shared ``astrid.core.pack.discovery`` module that the
executor, orchestrator, and element registries now delegate to. The focus is
the source / managed / explicit-extra / environment walk, identical ordering
across registry consumers, and that skills
discovery can consume the same metadata.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack import (
    discover_packs,
    load_pack_manifest,
    loader,
    pack_manifest_path,
)
from astrid.core.pack.discovery import (
    ASTRID_PACKS_PATH_ENV,
    SOURCE_KINDS,
    DiscoveredPack,
    discover_canonical_pack_metadata,
    discover_pack_metadata,
    discover_packs_ordered,
)


def write_pack(root: Path, pack_id: str, *, folder: str | None = None) -> Path:
    pack_root = root / (folder or pack_id)
    pack_root.mkdir(parents=True)
    (pack_root / "pack.yaml").write_text(
        f"schema_version: 3\nid: {pack_id}\nname: {pack_id.title()} Pack\n"
        "version: 1.0.0\npack_api: \"1.0\"\nrendering: {}\n",
        encoding="utf-8",
    )
    return pack_root


def write_executor(root: Path, folder: str, executor_id: str) -> Path:
    executor_root = root / folder
    executor_root.mkdir()
    kind = "external" if executor_id.startswith("external.") else "built_in"
    (executor_root / "executor.yaml").write_text(
        json.dumps(
            {
                "id": executor_id,
                "name": executor_id,
                "kind": kind,
                "version": "1.0",
                "command": {"argv": ["echo", executor_id]},
                "cache": {"mode": "none"},
            }
        ),
        encoding="utf-8",
    )
    return executor_root


def write_orchestrator(root: Path, folder: str, orchestrator_id: str) -> Path:
    orchestrator_root = root / folder
    orchestrator_root.mkdir()
    (orchestrator_root / "orchestrator.yaml").write_text(
        json.dumps(
            {
                "id": orchestrator_id,
                "name": orchestrator_id,
                "kind": "built_in",
                "version": "1.0",
                "runtime": {
                    "kind": "command",
                    "command": {"argv": ["echo", orchestrator_id]},
                },
            }
        ),
        encoding="utf-8",
    )
    return orchestrator_root


def _make_pack(root: Path, pack_id: str, *, folder: str | None = None):
    pack_root = write_pack(root, pack_id, folder=folder)
    return load_pack_manifest(pack_manifest_path(pack_root))


class PackDiscoveryMetadataTest(unittest.TestCase):
    def test_project_local_directory_is_not_a_discovery_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            packs_root = repo_root / "astrid" / "packs"
            local_pack = packs_root / "local"
            element_root = local_pack / "elements" / "effects" / "stamp"
            element_root.mkdir(parents=True)
            (element_root / "component.tsx").write_text("export default function Element() { return null; }\n")
            (element_root / "element.yaml").write_text(
                json.dumps(
                    {
                        "id": "stamp",
                        "kind": "effect",
                        "pack_id": "local",
                        "metadata": {"label": "stamp"},
                        "schema": {"type": "object"},
                        "defaults": {},
                        "dependencies": {"js_packages": [], "python_requirements": []},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            def scan(arg=None):
                if arg is None or Path(arg).resolve() == packs_root.resolve():
                    return discover_packs(packs_root)
                return ()

            with mock.patch("astrid.core.pack.discovery.REPO_ROOT", repo_root):
                discovered = discover_pack_metadata(
                    project_root=repo_root,
                    discover_packs_fn=scan,
                )

            self.assertFalse((local_pack / "pack.yaml").is_file())
            self.assertEqual([dp.id for dp in discovered], [])

    def test_project_local_manifest_is_not_read_or_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp) / "repo"
            packs_root = repo_root / "astrid" / "packs"
            local_pack = packs_root / "local"
            element_root = local_pack / "elements" / "effects" / "stamp"
            element_root.mkdir(parents=True)
            (element_root / "element.yaml").write_text("id: stamp\n", encoding="utf-8")
            local_manifest = local_pack / "pack.yaml"
            local_manifest.write_text(
                "schema_version: 3\nid: not_local\nname: Broken\nversion: 1.0.0\n"
                "pack_api: \"1.0\"\nrendering: {}\n",
                encoding="utf-8",
            )

            def scan(arg=None):
                if arg is None:
                    return ()
                if Path(arg).resolve() == packs_root.resolve():
                    return discover_packs(packs_root)
                return ()

            discovered = discover_pack_metadata(
                project_root=repo_root,
                discover_packs_fn=scan,
            )

            self.assertEqual(discovered, ())
            self.assertIn("id: not_local", local_manifest.read_text(encoding="utf-8"))

    def test_source_layer_treats_local_as_an_ordinary_source_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alpha = _make_pack(root, "alpha")
            local = _make_pack(root, "local")
            beta = _make_pack(root, "beta")

            def scan(arg=None):
                return (alpha, local, beta)

            discovered = discover_pack_metadata(discover_packs_fn=scan)

        self.assertEqual([dp.id for dp in discovered], ["alpha", "local", "beta"])
        self.assertTrue(all(dp.source_kind == "source" for dp in discovered))
        self.assertEqual([dp.priority_index for dp in discovered], [0, 1, 2])

    def test_checkout_hides_legacy_local_but_selected_external_local_is_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extra_root = Path(tmp) / "extra"
            extra_local = _make_pack(extra_root, "local")
            with mock.patch(
                "astrid.core.pack.source_setup.active_source_inventory",
                return_value=SimpleNamespace(sources=(), identity="empty-inventory"),
            ):
                discovered = discover_pack_metadata(
                    project_root=REPO_ROOT,
                    extra_pack_roots=(str(extra_root),),
                )

            locals_ = [row for row in discovered if row.id == "local"]
            self.assertEqual(
                [(row.source_kind, row.pack_dir) for row in locals_],
                [("extra", extra_local.root)],
            )
            legacy_local = loader.packs_root().resolve() / "local"
            self.assertNotIn(
                ("source", legacy_local),
                [(row.source_kind, row.pack_dir.resolve()) for row in discovered],
            )

    def test_project_local_pack_is_not_loaded_as_a_special_layer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            source_pack = _make_pack(src, "alpha")

            project_root = Path(tmp) / "proj"
            project_packs = project_root / "astrid" / "packs"
            project_packs.mkdir(parents=True)
            local_pack = _make_pack(project_packs, "local")
            stray_pack = _make_pack(project_packs, "stray")

            project_pack_root = (project_root / "astrid" / "packs").resolve()

            def scan(arg=None):
                if arg is None:
                    return (source_pack,)
                if Path(arg).resolve() == project_pack_root:
                    return (local_pack, stray_pack)
                return ()

            discovered = discover_pack_metadata(
                project_root=project_root,
                discover_packs_fn=scan,
            )

        # A project-local directory is not implicitly admitted by discovery.
        self.assertEqual([dp.id for dp in discovered], ["alpha"])
        self.assertEqual([dp.source_kind for dp in discovered], ["source"])

    def test_canonical_discovery_does_not_admit_project_local_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp) / "project"
            _make_pack(project_root / "astrid" / "packs", "local")

            discovered = discover_canonical_pack_metadata(project_root=project_root)

        self.assertEqual(discovered, ())

    def test_extra_roots_layer_treats_local_as_an_ordinary_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            extra_dir = Path(tmp) / "extra"
            extra_dir.mkdir()
            extra_pack = _make_pack(extra_dir, "gamma")
            extra_local = _make_pack(extra_dir, "local")
            source_pack_root = Path(tmp) / "src"
            source_pack_root.mkdir()
            source_pack = _make_pack(source_pack_root, "alpha")

            extra_resolved = extra_dir.resolve()

            def scan(arg=None):
                if arg is None:
                    return (source_pack,)
                if Path(arg).resolve() == extra_resolved:
                    return (extra_pack, extra_local)
                return ()

            discovered = discover_pack_metadata(
                discover_packs_fn=scan,
                extra_pack_roots=(str(extra_dir),),
            )

        self.assertEqual([dp.id for dp in discovered], ["alpha", "gamma", "local"])
        self.assertEqual([dp.source_kind for dp in discovered], ["source", "extra", "extra"])

    def test_source_kinds_include_env_in_priority_order(self) -> None:
        self.assertEqual(SOURCE_KINDS, ("source", "managed", "extra", "env"))

    def test_env_layer_uses_pathsep_and_skips_empty_or_missing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            source_pack = _make_pack(tmp_root / "src", "alpha")
            env_root = tmp_root / "env-root"
            env_root.mkdir()
            env_pack = _make_pack(env_root, "gamma")
            env_local = _make_pack(env_root, "local")
            missing_root = tmp_root / "missing-root"

            env_value = os.pathsep.join(["", str(missing_root), str(env_root), ""])

            def scan(arg=None):
                if arg is None:
                    return (source_pack,)
                if Path(arg).resolve() == env_root.resolve():
                    return (env_pack, env_local)
                return ()

            with mock.patch.dict(os.environ, {ASTRID_PACKS_PATH_ENV: env_value}, clear=False):
                discovered = discover_pack_metadata(
                    discover_packs_fn=scan,
                )

        self.assertEqual([dp.id for dp in discovered], ["alpha", "gamma", "local"])
        self.assertEqual([dp.source_kind for dp in discovered], ["source", "env", "env"])

    def test_same_canonical_external_root_is_scanned_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pack = _make_pack(root / "src", "alpha")
            external_root = root / "external"
            external_pack = _make_pack(external_root, "gamma")

            def scan(arg=None):
                if arg is None:
                    return (source_pack,)
                if Path(arg).resolve() == external_root.resolve():
                    return (external_pack,)
                return ()

            with mock.patch.dict(
                os.environ,
                {ASTRID_PACKS_PATH_ENV: str(external_root)},
                clear=False,
            ):
                discovered = discover_pack_metadata(
                    discover_packs_fn=scan,
                    extra_pack_roots=(str(external_root / ".." / "external"),),
                )

        self.assertEqual([dp.id for dp in discovered], ["alpha", "gamma"])
        self.assertEqual([dp.source_kind for dp in discovered], ["source", "extra"])

    def test_skill_roots_expose_only_the_declared_pack_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            packs_root = Path(tmp) / "packs"
            pack_root = write_pack(packs_root, "builtin")
            with (pack_root / "pack.yaml").open("a", encoding="utf-8") as manifest:
                manifest.write("documentation:\n  kind: skill\n  path: docs/SKILL.md\n")
            (pack_root / "docs").mkdir()
            (pack_root / "docs" / "SKILL.md").write_text("---\nname: builtin\n---\n", encoding="utf-8")
            executor_root = write_executor(pack_root, "sample_executor", "builtin.sample_executor")
            orchestrator_root = write_orchestrator(pack_root, "sample_orchestrator", "builtin.sample_orchestrator")
            for nested in (executor_root, orchestrator_root):
                (nested / "skill").mkdir()
                (nested / "skill" / "SKILL.md").write_text("---\nname: nested\n---\n", encoding="utf-8")
            packs = discover_packs(packs_root)
            discovered = DiscoveredPack(pack=packs[0], source_kind="source", priority_index=0)

            skill_roots = discovered.skill_roots()
            declared_root = (pack_root / "docs").resolve()

        # Nested component guides are never exported as extra skills.
        self.assertEqual(tuple(p.resolve() for p in skill_roots), (declared_root,))

    def test_skill_roots_are_empty_without_a_skill_documentation_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            packs_root = Path(tmp) / "packs"
            pack_root = write_pack(packs_root, "builtin")
            (pack_root / "skill").mkdir()
            (pack_root / "skill" / "SKILL.md").write_text("---\nname: builtin\n---\n", encoding="utf-8")
            packs = discover_packs(packs_root)
            discovered = DiscoveredPack(pack=packs[0], source_kind="source", priority_index=0)

            self.assertEqual(discovered.skill_roots(), ())

    def test_registries_share_identical_ordering(self) -> None:
        """Executor, orchestrator, and element discovery observe the same
        ordered pack sequence as the shared helper."""
        from astrid.core.execution.executor import registry as exec_registry
        from astrid.core.execution.orchestrator import registry as orch_registry

        with tempfile.TemporaryDirectory() as tmp:
            packs_root = Path(tmp) / "packs"
            write_pack(packs_root, "alpha")
            write_pack(packs_root, "beta")
            packs = discover_packs(packs_root)

            with mock.patch("astrid.core.execution.executor.registry.discover_packs", return_value=packs), \
                 mock.patch("astrid.core.execution.orchestrator.registry.discover_packs", return_value=packs):
                exec_ids = [p.id for p in exec_registry._discover_executor_packs(
                    project_root=REPO_ROOT, extra_pack_roots=())]
                orch_ids = [p.id for p in orch_registry._discover_orchestrator_packs(
                    project_root=REPO_ROOT, extra_pack_roots=())]
                helper_ids = [
                    dp.id
                    for dp in discover_pack_metadata(
                        discover_packs_fn=lambda arg=None: packs,
                    )
                ]

        self.assertEqual(exec_ids, ["alpha", "beta"])
        self.assertEqual(orch_ids, ["alpha", "beta"])
        self.assertEqual(helper_ids, ["alpha", "beta"])

    def test_discover_packs_ordered_returns_pack_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            packs_root = Path(tmp) / "packs"
            write_pack(packs_root, "alpha")
            packs = discover_packs(packs_root)

            def scan(arg=None):
                return packs

            ordered = discover_packs_ordered(discover_packs_fn=scan)

        self.assertEqual([p.id for p in ordered], ["alpha"])

    def test_rendering_consumers_receive_source_kind_and_priority_metadata(self) -> None:
        """Rendering registries consume metadata, not the compatibility wrapper
        that drops source/priority context."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_pack = _make_pack(root / "source", "source_pack")
            extra_root = root / "extra"
            extra_pack = _make_pack(extra_root, "extra_pack")

            def scan(arg=None):
                if arg is None:
                    return (source_pack,)
                if Path(arg).resolve() == extra_root.resolve():
                    return (extra_pack,)
                return ()

            discovered = discover_pack_metadata(
                project_root=root / "project",
                extra_pack_roots=(str(extra_root),),
                discover_packs_fn=scan,
            )

        self.assertEqual(
            [(item.id, item.source_kind, item.priority_index) for item in discovered],
            [
                ("source_pack", "source", 0),
                ("extra_pack", "extra", 1),
            ],
        )


if __name__ == "__main__":
    unittest.main()
