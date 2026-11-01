from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from astrid.core.execution.executor.folder import load_folder_executors
from astrid.core.execution.executor.registry import load_default_registry
from astrid.packs.vibecomfy import production_engine
from astrid.packs.vibecomfy.actions.run.run import _run_and_settle


def _fake_vibecomfy_modules(result: object) -> tuple[dict[str, types.ModuleType], Mock]:
    load_workflow = Mock(return_value=object())
    package = types.ModuleType("vibecomfy")
    package.__path__ = []  # type: ignore[attr-defined]
    package.load_workflow_any = load_workflow  # type: ignore[attr-defined]
    runtime = types.ModuleType("vibecomfy.runtime")
    runtime.__path__ = []  # type: ignore[attr-defined]
    run_module = types.ModuleType("vibecomfy.runtime.run")
    run_module.run_sync = Mock(return_value=result)  # type: ignore[attr-defined]
    package.runtime = runtime  # type: ignore[attr-defined]
    runtime.run = run_module  # type: ignore[attr-defined]
    return {
        "vibecomfy": package,
        "vibecomfy.runtime": runtime,
        "vibecomfy.runtime.run": run_module,
    }, load_workflow


class VibeComfyStructuredMetadataTest(unittest.TestCase):
    def test_executor_inspect_exposes_structured_vibecomfy_metadata(self) -> None:
        executor = load_default_registry().get("vibecomfy.run")
        payload = executor.to_dict()
        metadata = payload["metadata"]

        self.assertEqual(payload["id"], "vibecomfy.run")
        self.assertEqual(
            payload["command"]["argv"],
            [
                "{python_exec}",
                "-m",
                "astrid.packs.vibecomfy.actions.run.run",
                "run",
                "--python",
                "{python}",
                "--companion",
                "{companion}",
                "--source",
                "{source}",
                "--out",
                "{out}",
                "--task-identity",
                "{task_identity}",
                "--attempt-identity",
                "{attempt_identity}",
                "--execution-identity",
                "{execution_identity}",
                "--readiness-profile-path",
                "{readiness_profile_path}",
                "--readiness-profile-hash",
                "{readiness_profile_hash}",
                "--source-video",
                "{source_video}",
                "--source-video-node",
                "{source_video_node}",
                "--source-video-widget",
                "{source_video_widget}",
                "--managed-assets",
                "{managed_assets}",
                "--workflow-inputs",
                "{workflow_inputs}",
                "--comfy-url",
                "{comfy_url}",
                "--comfy-output-dir",
                "{comfy_output_dir}",
            ],
        )
        self.assertEqual(
            payload["command"]["input_args"],
            [{"input": "workflow", "repeatable": False, "optional": True, "before": "--out"}],
        )
        inputs = {item["name"]: item for item in payload["inputs"]}
        self.assertEqual(inputs["comfy_url"]["type"], "string")
        self.assertFalse(inputs["comfy_url"]["required"])
        self.assertEqual(inputs["comfy_url"]["default"], "")
        self.assertEqual(inputs["comfy_output_dir"]["type"], "string")
        self.assertFalse(inputs["comfy_output_dir"]["required"])
        self.assertEqual(inputs["comfy_output_dir"]["default"], "")
        self.assertEqual(payload["isolation"]["requirements"], ["vibecomfy"])
        self.assertTrue(payload["isolation"]["network"])
        self.assertEqual(metadata["pack_id"], "vibecomfy")
        self.assertEqual(metadata["homepage"], "https://github.com/peteromallet/VibeComfy")
        self.assertEqual(metadata["cli_module"], "vibecomfy.cli")
        self.assertEqual(metadata["vibecomfy_command"], "run")
        self.assertEqual(metadata["command_names"], ["run", "validate"])
        self.assertEqual(metadata["requirements"], ["vibecomfy"])
        self.assertEqual(
            metadata["workflow_input_contract"],
            {
                "name": "workflow",
                "type": "file",
                "required": False,
                "description": "UI JSON or complete canonical Python/companion/source sibling bundle.",
                "format": "ComfyUI/VibeComfy workflow JSON or canonical bundle",
            },
        )
        self.assertEqual(metadata["network_behavior"], {"run": True, "validate": False})
        self.assertEqual(metadata["catalog_source"], "none_declared")
        self.assertEqual(metadata["workflows"], [])
        self.assertEqual(metadata["nodes"], [])
        self.assertEqual(metadata["prompts"], [])
        self.assertTrue(metadata["output_result_manifest"])
        self.assertEqual(
            [(output["name"], output["type"]) for output in payload["outputs"]],
            [("vibecomfy_run", "file"), ("result_manifest", "file")],
        )

    def test_run_settles_every_engine_output_with_port_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            out = root / "spool"
            engine_output = out / "engine-output"
            engine_output.mkdir(parents=True)
            source_a = engine_output / "source-a.png"
            source_b = engine_output / "source-b.mp4"
            source_a.write_bytes(b"image")
            source_b.write_bytes(b"video")
            result = SimpleNamespace(
                outputs=[str(source_a), str(source_b)],
                run_id="run-1",
                prompt_id="prompt-1",
            )
            with patch.object(
                production_engine,
                "run_workflow_path",
                return_value=(source_a, source_b),
            ) as run_workflow:
                manifest = _run_and_settle(
                    root / "workflow.json",
                    out,
                    task_identity="task-1",
                )

            run_workflow.assert_called_once_with(
                root / "workflow.json",
                out,
                task_identity="task-1",
                expected_execution_identity=None,
                profile_id="pip_embedded",
                hc03_profile=None,
            )
            self.assertEqual(len(manifest["outputs"]), 2)
            for ordinal, entry in enumerate(manifest["outputs"]):
                self.assertEqual(entry["name"], "vibecomfy_run")
                self.assertEqual(entry["ordinal"], ordinal)
                self.assertEqual(entry["role"], "result")
                self.assertEqual(entry["is_primary"], ordinal == 0)
                self.assertEqual(
                    entry["media_type"],
                    "image/png" if ordinal == 0 else "video/mp4",
                )
                self.assertTrue(entry["content_hash"].startswith("sha256:"))
                self.assertGreater(entry["bytes"], 0)
                self.assertTrue((out / entry["path"]).is_file())
            self.assertEqual(
                json.loads((out / "manifest.json").read_text(encoding="utf-8")),
                manifest,
            )

    def test_run_rejects_missing_or_empty_engine_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = ([root / "missing.png"], [])
            for inventory in cases:
                with (
                    self.subTest(inventory=tuple(str(path) for path in inventory)),
                    patch.object(
                        production_engine,
                        "run_workflow_path",
                        return_value=tuple(inventory),
                    ),
                ):
                    with self.assertRaises((FileNotFoundError, ValueError)):
                        _run_and_settle(
                            root / "workflow.json",
                            root / "spool",
                            task_identity="task-1",
                        )

    def test_vibecomfy_validate_metadata_is_structured_and_network_false(self) -> None:
        validate = load_default_registry().get("vibecomfy.validate")

        self.assertFalse(validate.isolation.network)
        self.assertEqual(validate.metadata["vibecomfy_command"], "validate")
        self.assertEqual(validate.metadata["network_behavior"], {"run": True, "validate": False})
        self.assertEqual(validate.metadata["catalog_source"], "none_declared")
        self.assertEqual(validate.metadata["workflows"], [])
        self.assertEqual(validate.metadata["nodes"], [])
        self.assertEqual(validate.metadata["prompts"], [])

    def test_vibecomfy_catalogs_do_not_scrape_skill_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executor_root = root / "vibecomfy"
            executor_root.mkdir()
            (executor_root / "requirements.txt").write_text("different-package\n", encoding="utf-8")
            (executor_root / "STAGE.md").write_text(
                "Fake structured catalog that must not be scraped: workflows=fake nodes=fake prompts=fake\n",
                encoding="utf-8",
            )
            (executor_root / "executor.yaml").write_text(
                "\n".join(
                    [
                        "executors:",
                        "  - id: vibecomfy.run",
                        "    name: VibeComfy Run",
                        "    kind: external",
                        "    version: 0.1.0",
                        "    inputs:",
                        "      - name: workflow",
                        "        type: file",
                        "    command:",
                        "      argv: [\"{python_exec}\", \"-m\", \"vibecomfy.cli\", \"run\", \"{workflow}\"]",
                        "    cache:",
                        "      mode: none",
                        "    isolation:",
                        "      mode: subprocess",
                        "      requirements: [\"vibecomfy\"]",
                        "      network: true",
                        "    metadata:",
                        "      pack_id: vibecomfy",
                        "      catalog_source: none_declared",
                        "      workflows: []",
                        "      nodes: []",
                        "      prompts: []",
                    ]
                ),
                encoding="utf-8",
            )

            executors = load_folder_executors(executor_root)

        by_id = {executor.id: executor for executor in executors}
        run = by_id["vibecomfy.run"]
        self.assertEqual(run.metadata["catalog_source"], "none_declared")
        self.assertEqual(run.metadata["workflows"], [])
        self.assertEqual(run.metadata["nodes"], [])
        self.assertEqual(run.metadata["prompts"], [])
        self.assertNotIn("fake", json.dumps(run.metadata).lower())
        self.assertTrue(run.metadata["stage_file"].endswith("STAGE.md"))


if __name__ == "__main__":
    unittest.main()
