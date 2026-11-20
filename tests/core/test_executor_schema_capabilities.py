from __future__ import annotations

import argparse
import contextlib
import io
import json
import unittest

from astrid.core.execution.executor.registry import ExecutorRegistry
from astrid.core.execution.executor.schema import ExecutorValidationError, validate_executor_definition


def _manifest(**overrides):
    data = {
        "id": "builtin.demo",
        "name": "Demo",
        "kind": "built_in",
        "version": "1.0",
        "description": "Demo executor.",
    }
    data.update(overrides)
    return data


class ExecutorSchemaCapabilityTest(unittest.TestCase):
    def test_clip_kinds_and_requirements_round_trip(self) -> None:
        executor = validate_executor_definition(
            _manifest(
                clip_kinds_supported=["VIDEO", "image"],
                pipeline_requirements=["brief", "pool"],
            )
        )

        self.assertEqual(executor.clip_kinds_supported, ("video", "image"))
        self.assertEqual(executor.pipeline_requirements, ("brief", "pool"))
        payload = executor.to_dict()
        self.assertEqual(payload["clip_kinds_supported"], ["video", "image"])
        self.assertEqual(payload["pipeline_requirements"], ["brief", "pool"])

    def test_inspect_json_exposes_capability_fields(self) -> None:
        executor = validate_executor_definition(
            _manifest(
                clip_kinds_supported=["audio"],
                pipeline_requirements=["transcript"],
            )
        )
        payload = executor.to_dict()
        self.assertEqual(payload["clip_kinds_supported"], ["audio"])
        self.assertEqual(payload["pipeline_requirements"], ["transcript"])

    def test_invalid_clip_kind_rejected(self) -> None:
        with self.assertRaisesRegex(ExecutorValidationError, "clip_kinds_supported"):
            validate_executor_definition(_manifest(clip_kinds_supported=["banana"]))

    def test_invalid_pipeline_requirement_rejected(self) -> None:
        with self.assertRaisesRegex(ExecutorValidationError, "pipeline_requirements"):
            validate_executor_definition(_manifest(pipeline_requirements=["banana"]))

    def test_project_scope_defaults_to_required_and_round_trips(self) -> None:
        default = validate_executor_definition(_manifest())
        self.assertEqual(default.metadata.get("project_scope"), None)
        optional = validate_executor_definition(
            _manifest(metadata={"project_scope": "optional"})
        )
        self.assertEqual(optional.to_dict()["metadata"]["project_scope"], "optional")

    def test_invalid_project_scope_rejected(self) -> None:
        with self.assertRaisesRegex(ExecutorValidationError, "project_scope"):
            validate_executor_definition(_manifest(metadata={"project_scope": "none"}))

    def test_fixed_inputs_require_declared_string_ports_and_match_defaults(self) -> None:
        valid = validate_executor_definition(
            _manifest(
                inputs=[
                    {"name": "model", "type": "string", "required": True},
                    {"name": "mode", "type": "string", "required": True},
                ],
                metadata={"fixed_inputs": {"model": "z-image", "mode": "i2i"}},
            )
        )
        self.assertEqual(valid.metadata["fixed_inputs"]["model"], "z-image")
        cases = [
            ({"fixed_inputs": {"missing": "value"}}, "undeclared"),
            ({"fixed_inputs": {"model": 1}}, "non-empty string"),
            ({"fixed_inputs": {"model": "other"}}, "conflicts with default"),
        ]
        for metadata, message in cases:
            inputs = [
                {"name": "model", "type": "string", "required": False, "default": "z-image"},
                {"name": "mode", "type": "string", "required": True},
            ]
            with self.assertRaisesRegex(ExecutorValidationError, message):
                validate_executor_definition(_manifest(inputs=inputs, metadata=metadata))

    def test_retired_produces_for_alias_is_rejected(self) -> None:
        with self.assertRaisesRegex(ExecutorValidationError, "produces_for is retired"):
            validate_executor_definition(_manifest(produces_for=["AUDIO", "TEXT"]))

    def test_external_runtime_package_metadata_and_output_extension_parse(self) -> None:
        executor = validate_executor_definition(
            _manifest(
                id="external.demo",
                kind="external",
                outputs=[{"name": "video", "extension": ".mp4"}],
                metadata={
                    "external_runtime": {
                        "mode": "package",
                        "source": {
                            "kind": "git",
                            "url": "https://example.invalid/demo.git",
                            "ref": "abc123",
                        },
                        "install": {"strategy": "pyproject", "target": "pyproject.toml"},
                        "import_check": "demo_pkg.cli",
                        "binary_check": ["ffmpeg"],
                    },
                    "legacy": {"preserved": True},
                },
            )
        )

        self.assertEqual(executor.outputs[0].extension, ".mp4")
        self.assertEqual(executor.external_runtime.mode, "package")
        self.assertEqual(executor.external_runtime.source.kind, "git")
        self.assertEqual(executor.external_runtime.source.ref, "abc123")
        self.assertEqual(executor.external_runtime.install.strategy, "pyproject")
        self.assertEqual(executor.external_runtime.binary_check, ("ffmpeg",))
        payload = executor.to_dict()
        self.assertEqual(payload["outputs"][0]["extension"], ".mp4")
        self.assertEqual(payload["metadata"]["legacy"], {"preserved": True})
        self.assertEqual(payload["metadata"]["external_runtime"]["source"]["url"], "https://example.invalid/demo.git")
        self.assertNotIn("external_runtime", {key for key in payload if key != "metadata"})

    def test_external_runtime_api_mode_does_not_require_source_or_install(self) -> None:
        executor = validate_executor_definition(
            _manifest(
                id="external.api",
                kind="external",
                metadata={"external_runtime": {"mode": "api", "binary_check": ["curl"]}},
            )
        )

        self.assertEqual(executor.external_runtime.mode, "api")
        self.assertIsNone(executor.external_runtime.source)
        self.assertIsNone(executor.external_runtime.install)

    def test_external_runtime_rejects_package_missing_source_or_install(self) -> None:
        with self.assertRaisesRegex(ExecutorValidationError, "source is required"):
            validate_executor_definition(
                _manifest(
                    id="external.demo",
                    kind="external",
                    metadata={
                        "external_runtime": {
                            "mode": "package",
                            "install": {"strategy": "pyproject", "target": "pyproject.toml"},
                        }
                    },
                )
            )
        with self.assertRaisesRegex(ExecutorValidationError, "install is required"):
            validate_executor_definition(
                _manifest(
                    id="external.demo",
                    kind="external",
                    metadata={
                        "external_runtime": {
                            "mode": "package",
                            "source": {"kind": "pypi", "package": "demo"},
                        }
                    },
                )
            )

    def test_external_runtime_rejects_api_source_or_install(self) -> None:
        with self.assertRaisesRegex(ExecutorValidationError, "api.*source or install"):
            validate_executor_definition(
                _manifest(
                    id="external.api",
                    kind="external",
                    metadata={
                        "external_runtime": {
                            "mode": "api",
                            "source": {"kind": "pypi", "package": "demo"},
                        }
                    },
                )
            )

    def test_external_runtime_rejects_invalid_source_install_import_and_binary(self) -> None:
        invalid_cases = [
            (
                {
                    "source": {"kind": "git", "ref": "main"},
                    "install": {"strategy": "pyproject", "target": "pyproject.toml"},
                },
                "source.url",
            ),
            (
                {
                    "source": {"kind": "path", "url": "https://example.invalid/repo.git", "path": "/tmp/repo"},
                    "install": {"strategy": "requirements", "target": "requirements.txt"},
                },
                "source.url",
            ),
            (
                {
                    "source": {"kind": "pypi", "package": "demo"},
                    "install": {"strategy": "unknown", "target": "requirements.txt"},
                },
                "install.strategy",
            ),
            (
                {
                    "source": {"kind": "pypi", "package": "demo"},
                    "install": {"strategy": "pip_args", "target": "demo"},
                    "import_check": "demo-package",
                },
                "import_check",
            ),
            (
                {
                    "source": {"kind": "pypi", "package": "demo"},
                    "install": {"strategy": "pip_args", "target": "demo"},
                    "binary_check": [""],
                },
                "binary_check",
            ),
        ]
        for external_runtime, expected in invalid_cases:
            subtest_context = {"expected": expected}
            if expected == "source.url":
                subtest_context["source_kind"] = external_runtime["source"]["kind"]
            with self.subTest(**subtest_context), self.assertRaisesRegex(ExecutorValidationError, expected):
                validate_executor_definition(
                    _manifest(
                        id="external.demo",
                        kind="external",
                        metadata={"external_runtime": external_runtime},
                    )
                )

    def test_external_runtime_rejects_invalid_output_extension(self) -> None:
        invalid_outputs = [
            ({"name": "video", "extension": "mp4"}, "start with"),
            ({"name": "video", "extension": ".thisextensionistoolong"}, "16 characters"),
            ({"name": "video", "extension": "../mp4"}, "path separators"),
        ]
        for output, expected in invalid_outputs:
            with self.subTest(output=output), self.assertRaisesRegex(ExecutorValidationError, expected):
                validate_executor_definition(_manifest(outputs=[output]))


if __name__ == "__main__":
    unittest.main()
