import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astrid.core.contracts.errors import AstridError
from astrid.core.element.schema import load_element_definition
from astrid.core.execution.executor.registry import load_default_registry as load_executor_registry
from astrid.core.execution.executor.runner import ExecutorRunRequest, run_executor
from astrid.packs.rendering.actions.html_canvas_effect.run import main, scaffold


class HtmlCanvasEffectExecutorTest(unittest.TestCase):
    def test_executor_is_discoverable(self) -> None:
        registry = load_executor_registry()
        executor = registry.get("rendering.html_canvas_effect")

        self.assertEqual(executor.metadata["runtime_module"], "astrid.packs.rendering.actions.html_canvas_effect.run")
        self.assertIn("HtmlInCanvas", executor.description)

    def test_scaffold_writes_discoverable_personal_effect_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            packs_root = Path(tmp) / "packs"
            report_path = project / "runs" / "effect" / "report.json"

            with patch.dict(os.environ, {"ASTRID_PACKS_PATH": str(packs_root)}):
                report = scaffold(
                    effect_id="glass-product-card",
                    label="Glass Product Card",
                    description="A test effect.",
                    project_root=project,
                    out_path=report_path,
                )

            personal_pack = packs_root / "personal"
            element_root = personal_pack / "rendering" / "elements" / "effects" / "glass-product-card"
            personal_pack_manifest = personal_pack / "pack.yaml"
            self.assertEqual(Path(report["element_root"]), element_root)
            self.assertTrue(personal_pack_manifest.is_file())
            self.assertFalse((project / "astrid" / "packs" / "local").exists())
            self.assertTrue((element_root / "component.tsx").is_file())
            self.assertTrue((element_root / "element.yaml").is_file())
            self.assertTrue(report_path.is_file())

            component = (element_root / "component.tsx").read_text(encoding="utf-8")
            self.assertIn("HtmlInCanvas", component)
            self.assertIn("drawElementImage", component)

            manifest = json.loads((element_root / "element.yaml").read_text(encoding="utf-8"))
            self.assertEqual(manifest["id"], "glass-product-card")
            self.assertEqual(manifest["pack_id"], "personal")
            self.assertTrue(manifest["metadata"]["render_requirements"]["uses_html_in_canvas"])
            self.assertEqual(manifest["metadata"]["render_requirements"]["final_renderer"], "rendering.render")

            from astrid.core.element.registry import load_pack_elements

            discovered = load_pack_elements(
                project_root=project, extra_pack_roots=(str(personal_pack),)
            )
            self.assertIn("glass-product-card", {item.id for item in discovered})
            element = load_element_definition(element_root, kind="effects", source="pack:personal", editable=True, priority=10)
            self.assertEqual(element.id, "glass-product-card")
            self.assertEqual(element.metadata["pack_id"], "personal")

    def test_scaffold_refuses_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            packs_root = Path(tmp) / "packs"
            report_path = project / "report.json"
            with patch.dict(os.environ, {"ASTRID_PACKS_PATH": str(packs_root)}):
                scaffold(effect_id="canvas-card", label=None, description=None, project_root=project, out_path=report_path)

                with self.assertRaisesRegex(FileExistsError, "already exists"):
                    scaffold(effect_id="canvas-card", label=None, description=None, project_root=project, out_path=report_path)

                scaffold(effect_id="canvas-card", label=None, description=None, project_root=project, out_path=report_path, force=True)

    def test_main_validates_effect_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(AstridError, "kebab-case"):
                main(["--effect-id", "Bad_ID", "--project-root", tmp, "--out", str(Path(tmp) / "report.json")])

    def test_main_writes_result_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            report_path = project / "runs" / "effect" / "report.json"
            packs_root = Path(tmp) / "packs"
            timeline_path = project / "runs" / "effect" / "timeline.json"
            assets_path = project / "runs" / "effect" / "assets.json"

            with patch.dict(os.environ, {"ASTRID_PACKS_PATH": str(packs_root)}):
                result = main(
                    [
                        "--effect-id", "glass-product-card",
                        "--label", "Glass Product Card",
                        "--description", "A test effect.",
                        "--project-root", str(project),
                        "--out", str(report_path),
                        "--timeline", str(timeline_path),
                        "--assets", str(assets_path),
                    ]
                )

            self.assertEqual(result, 0)
            manifest_path = report_path.parent / "manifest.json"
            self.assertTrue(manifest_path.is_file(), f"manifest not found at {manifest_path}")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["kind"], "html_canvas_effect")
            self.assertEqual(manifest["schema_version"], 1)
            self.assertIsInstance(manifest["inputs"], dict)
            self.assertIn("effect_id", manifest["inputs"])
            self.assertEqual(manifest["inputs"]["effect_id"], "glass-product-card")
            self.assertIsInstance(manifest["outputs"], list)
            output_paths = {o["path"] for o in manifest["outputs"]}
            self.assertIn(report_path.name, output_paths)
            # element_root is relative to manifest dir, so it uses ../
            self.assertTrue(any("glass-product-card" in p for p in output_paths),
                            f"element_root path not found in {output_paths}")
            self.assertIsInstance(manifest["warnings"], list)

    def test_runner_dry_run_uses_executor_runtime(self) -> None:
        result = run_executor(
            ExecutorRunRequest(
                executor_id="rendering.html_canvas_effect",
                out="runs/html-canvas-effect",
                inputs={"effect_id": "glass-product-card"},
                dry_run=True,
                project="demo",
            ),
            load_executor_registry(),
        )

        # Dry runs never execute, so no returncode is produced (v10 runner).
        self.assertIsNone(result.returncode)
        self.assertIn(
            "astrid.packs.rendering.actions.html_canvas_effect.run",
            " ".join(result.command),
        )


if __name__ == "__main__":
    unittest.main()
