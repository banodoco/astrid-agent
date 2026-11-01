"""Unit tests for astrid/packs/validate.py.

Covers:
- valid/invalid pack manifests (schema_version present/missing/unknown,
  required fields, malformed YAML)
- missing docs/runtime files
- undeclared content roots
- file-specific error formatting
- validation does NOT import or execute run.py (static safety)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from astrid.core.pack import PackValidationError
from astrid.core.pack.validate import (
    PackValidator,
    validate_first_party_packs_root,
    validate_pack,
)
from astrid.core.pack.validate_layout import parse_layout_exceptions

_FIRST_PARTY_PACKS_ROOT = Path(__file__).resolve().parents[2] / "astrid" / "packs"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mirror_first_party_packs_root(dest: Path) -> None:
    for child in sorted(_FIRST_PARTY_PACKS_ROOT.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not _has_tracked_files(child):
            continue
        shutil.copytree(
            child,
            dest / child.name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )


def _has_tracked_files(path: Path) -> bool:
    rel = path.resolve().relative_to(_FIRST_PARTY_PACKS_ROOT.parents[1])
    result = subprocess.run(
        ["git", "ls-files", "--", rel.as_posix()],
        cwd=_FIRST_PARTY_PACKS_ROOT.parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


class MinimalPackTestCase(unittest.TestCase):
    """Shared helpers for pack test cases."""

    def make_pack_root(self, pack_id: str = "test_pack") -> Path:
        base = Path(tempfile.mkdtemp(prefix="test-validate-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        path = base / pack_id
        path.mkdir()
        return path

    def write_valid_pack(self, root: Path, pack_id: str = "test_pack") -> None:
        """Write a minimal valid v2 capability pack."""
        _write(
            root / "pack.yaml",
            f"""schema_version: 2
id: {pack_id}
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test Pack\n\nAgent guide.")
        (root / "executors").mkdir(parents=True, exist_ok=True)
        (root / "orchestrators").mkdir(parents=True, exist_ok=True)

    def write_valid_executor(
        self, root: Path, exec_path: str = "executors/test_exec", exec_id: str = "test_pack.test_exec"
    ) -> None:
        """Write a valid executor manifest and supporting files."""
        comp_dir = root / exec_path
        _write(
            comp_dir / "executor.yaml",
            f"""schema_version: 1
id: {exec_id}
name: Test Executor
kind: built_in
version: 0.1.0
description: A test executor.
runtime:
  type: python-cli
  entrypoint: run.py
""",
        )
        _write(comp_dir / "run.py", "# Test executor\nprint('hello')\n")
        _write(comp_dir / "STAGE.md", "# Test Executor\n\nPurpose: Testing.\n")

    def write_valid_orchestrator(
        self,
        root: Path,
        orch_path: str = "orchestrators/test_orch",
        orch_id: str = "test_pack.test_orch",
    ) -> None:
        comp_dir = root / orch_path
        _write(
            comp_dir / "orchestrator.yaml",
            f"""schema_version: 1
id: {orch_id}
name: Test Orchestrator
kind: built_in
version: 0.1.0
runtime:
  kind: python
  module: run
  function: main
""",
        )
        _write(comp_dir / "run.py", "def main():\n    return None\n")
        _write(comp_dir / "STAGE.md", "# Test Orchestrator\n")

    def write_valid_element(
        self,
        root: Path,
        element_path: str = "elements/effects/test_effect",
        *,
        element_id: str = "test_effect",
        pack_id: str = "test_pack",
    ) -> None:
        comp_dir = root / element_path
        _write(
            comp_dir / "element.yaml",
            f"""schema_version: 1
id: {element_id}
kind: effect
pack_id: {pack_id}
metadata:
  name: Test Effect
schema: {{}}
defaults: {{}}
dependencies: {{}}
""",
        )
        _write(comp_dir / "component.tsx", "export const Component = () => null;\n")


class TestValidPack(MinimalPackTestCase):
    """Valid pack manifests should pass validation."""

    def test_valid_minimal_pack(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        errors, warnings = validate_pack(root)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

    def test_valid_pack_with_executor(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root)
        errors, warnings = validate_pack(root)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

    def test_valid_pack_no_content_roots_warns(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
capabilities: [testing]
""",
        )
        errors, warnings = validate_pack(root)
        # Missing content roots should only produce warnings, not errors
        # (content roots are optional in the schema)
        self.assertEqual(errors, [])

    def test_pack_schema_version_float_is_rejected(self) -> None:
        """Canonical v2 requires the exact integer version token."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        # YAML safe_load parses `1` as int by default, but let's also
        # verify that a float 1.0 works
        _write(
            root / "pack.yaml",
            """schema_version: 2.0
id: test_pack
name: Test Pack
version: 0.1.0
capabilities: [testing]
""",
        )
        errors, warnings = validate_pack(root)
        self.assertTrue(errors)

    def test_discovery_pack_definition_preserves_taxonomy_fields(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
origin: external
install_tier: optional
pack_type: adapter
domain: image
support: community
status: experimental
agent:
  purpose: Testing
""",
        )
        validator = PackValidator(root)
        validator._pack_data = validator._load_yaml(root / "pack.yaml")
        pack = validator._pack_definition_for_discovery({"executors": "executors"})
        self.assertEqual(pack.origin, "external")
        self.assertEqual(pack.install_tier, "optional")
        self.assertEqual(pack.pack_type, "adapter")
        self.assertEqual(pack.domain, "image")
        self.assertEqual(pack.support, "community")
        self.assertEqual(pack.stability, "experimental")

    def test_discovery_pack_definition_preserves_permissions(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
permissions:
  - id: network
    reason: Contacts hosted APIs.
    services:
      - github
agent:
  purpose: Testing
""",
        )
        validator = PackValidator(root)
        validator._pack_data = validator._load_yaml(root / "pack.yaml")
        pack = validator._pack_definition_for_discovery({"executors": "executors"})
        self.assertEqual(
            pack.to_dict()["permissions"],
            [
                {
                    "id": "network",
                    "reason": "Contacts hosted APIs.",
                    "services": ["github"],
                }
            ],
        )

    def test_docs_scaffold_templates_validate_in_minimal_pack(self) -> None:
        """Scaffold templates should validate together in a minimal pack shell."""
        root = self.make_pack_root("example_pack")
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: example_pack
name: Example Pack
version: 0.1.0
content:
  executors: executors
  orchestrators: orchestrators
  elements: elements
""",
        )
        _write(root / "skill" / "SKILL.md", "# Example Pack\n")

        template_pairs = (
            ("docs/templates/executor/executor.yaml", "executors/example/executor.yaml"),
            ("docs/templates/executor/STAGE.md", "executors/example/STAGE.md"),
            ("docs/templates/executor/run.py", "executors/example/run.py"),
            ("docs/templates/orchestrator/orchestrator.yaml", "orchestrators/example/orchestrator.yaml"),
            ("docs/templates/orchestrator/STAGE.md", "orchestrators/example/STAGE.md"),
            ("docs/templates/orchestrator/run.py", "orchestrators/example/run.py"),
            ("docs/templates/element/element.yaml", "elements/effects/example/element.yaml"),
            ("docs/templates/element/STAGE.md", "elements/effects/example/STAGE.md"),
            ("docs/templates/element/component.tsx", "elements/effects/example/component.tsx"),
        )
        repo_root = Path(__file__).resolve().parents[2]
        for src_rel, dst_rel in template_pairs:
            src = repo_root / src_rel
            dst = root / dst_rel
            _write(dst, src.read_text(encoding="utf-8"))

        errors, warnings = validate_pack(root)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(warnings, [], f"Unexpected warnings: {warnings}")


class TestPermissionValidation(MinimalPackTestCase):
    def test_invalid_permission_key_is_reported(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
permissions:
  - id: network
    reason: Contacts hosted APIs.
    unsupported: true
agent:
  purpose: Testing
""",
        )
        errors, warnings = validate_pack(root)
        self.assertTrue(any("permissions" in error for error in errors), errors)


class TestLayoutValidation(MinimalPackTestCase):
    def test_non_builtin_pack_uses_discovery_iterators_for_declared_content_roots(self) -> None:
        from astrid.core.pack import (
            iter_element_roots as real_iter_element_roots,
        )
        from astrid.core.pack import (
            iter_executor_roots as real_iter_executor_roots,
        )
        from astrid.core.pack import (
            iter_orchestrator_roots as real_iter_orchestrator_roots,
        )

        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: capability_roots/executors
  orchestrators: capability_roots/orchestrators
  elements: capability_roots/elements
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test Pack\n")
        self.write_valid_executor(
            root,
            "capability_roots/executors/test_exec",
            "test_pack.test_exec",
        )
        self.write_valid_orchestrator(
            root,
            "capability_roots/orchestrators/test_orch",
            "test_pack.test_orch",
        )
        self.write_valid_element(
            root,
            "capability_roots/elements/effects/test_effect",
        )

        with (
            mock.patch(
                "astrid.core.pack.validate.iter_executor_roots",
                side_effect=real_iter_executor_roots,
            ) as executor_roots,
            mock.patch(
                "astrid.core.pack.validate.iter_orchestrator_roots",
                side_effect=real_iter_orchestrator_roots,
            ) as orchestrator_roots,
            mock.patch(
                "astrid.core.pack.validate.iter_element_roots",
                side_effect=real_iter_element_roots,
            ) as element_roots,
        ):
            errors, _warnings = validate_pack(root)

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        executor_roots.assert_called_once()
        orchestrator_roots.assert_called_once()
        element_roots.assert_called_once()

    def test_duplicate_capability_ids_are_errors(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root, "executors/first", "test_pack.duplicate")
        self.write_valid_executor(root, "executors/second", "test_pack.duplicate")
        errors, _ = validate_pack(root)
        self.assertTrue(any("duplicate capability id" in error for error in errors), errors)

    def test_misplaced_executor_id_is_an_error(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root, "executors/test_exec", "rendering.render")
        errors, _warnings = validate_pack(root)
        self.assertTrue(
            any("belongs to pack 'rendering' but was found in pack 'test_pack'" in error for error in errors),
            errors,
        )

    def test_misplaced_orchestrator_id_is_an_error(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_orchestrator(root, "orchestrators/test_orch", "video_editing.hype")
        errors, _warnings = validate_pack(root)
        self.assertTrue(
            any(
                "belongs to pack 'video_editing' but was found in pack 'test_pack'" in error
                for error in errors
            ),
            errors,
        )

    def test_misplaced_element_pack_id_is_an_error(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_element(root, "elements/effects/test_effect", pack_id="rendering")
        errors, _warnings = validate_pack(root)
        self.assertTrue(
            any("declares pack_id 'rendering' but was found in pack 'test_pack'" in error for error in errors),
            errors,
        )

    def test_alias_targets_must_exist(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        comp_dir = root / "executors" / "test_exec"
        _write(
            comp_dir / "executor.yaml",
            """schema_version: 1
id: test_pack.test_exec
name: Test Executor
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
metadata:
  aliases:
    - canonical_id: test_pack.missing
""",
        )
        _write(comp_dir / "run.py", "# Test executor\n")
        errors, _ = validate_pack(root)
        self.assertTrue(any("unknown capability id" in error for error in errors), errors)

    def test_unsupported_content_roots_warn(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  executors: executors
  strange: strange
agent:
  purpose: Testing
""",
        )
        (root / "strange").mkdir()
        errors, warnings = validate_pack(root)
        self.assertEqual(errors, [])
        self.assertTrue(any("unsupported content root" in warning for warning in warnings), warnings)

    def test_non_builtin_pack_standard_content_roots_validates(self) -> None:
        """A non-builtin pack with standard content roots (executors, orchestrators, elements)
        should pass validation using discovery-based iterators."""
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack with standard content roots.
content:
  executors: executors
  orchestrators: orchestrators
  elements: elements
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test Pack\n")
        self.write_valid_executor(root, "executors/test_exec", "test_pack.test_exec")
        self.write_valid_orchestrator(root, "orchestrators/test_orch", "test_pack.test_orch")
        self.write_valid_element(
            root, "elements/effects/test_effect", element_id="test_effect", pack_id="test_pack"
        )

        errors, _warnings = validate_pack(root)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

    def test_pack_declared_custom_element_kind_registers_canonical_capability_id(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  elements: elements
extensions:
  elements:
    kinds:
      - id: widgets
        singular: widget
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test Pack\n")
        _write(
            root / "elements" / "widgets" / "glow" / "element.yaml",
            """schema_version: 1
id: glow
kind: widget
pack_id: test_pack
metadata:
  name: Glow
schema: {}
defaults: {}
dependencies: {}
""",
        )
        _write(
            root / "elements" / "widgets" / "glow" / "component.tsx",
            "export default function Glow() { return null; }\n",
        )

        validator = PackValidator(root)
        errors = validator.validate()

        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertEqual(
            validator._capability_locations["widgets/glow"],
            "elements/widgets/glow/element.yaml",
        )

    def test_pack_declared_custom_element_kind_rejects_undeclared_typo_root(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  elements: elements
extensions:
  elements:
    kinds:
      - id: widgets
        singular: widget
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test Pack\n")
        _write(
            root / "elements" / "widgets" / "glow" / "element.yaml",
            """schema_version: 1
id: glow
kind: widget
pack_id: test_pack
metadata:
  name: Glow
schema: {}
defaults: {}
dependencies: {}
""",
        )
        _write(
            root / "elements" / "widgets" / "glow" / "component.tsx",
            "export default function Glow() { return null; }\n",
        )
        _write(
            root / "elements" / "widgtes" / "typo" / "element.yaml",
            """schema_version: 1
id: typo
kind: widget
pack_id: test_pack
metadata:
  name: Typo
schema: {}
defaults: {}
dependencies: {}
""",
        )
        _write(
            root / "elements" / "widgtes" / "typo" / "component.tsx",
            "export default function Typo() { return null; }\n",
        )

        with self.assertRaisesRegex(PackValidationError, "element kind must be one of .*widgets"):
            validate_pack(root)

    def test_non_builtin_pack_with_aliases_and_standard_roots_validates(self) -> None:
        """A non-builtin pack with standard content roots and pack-level aliases
        should pass validation."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root, "executors/test_exec", "test_pack.test_exec")
        self.write_valid_orchestrator(root, "orchestrators/test_orch", "test_pack.test_orch")
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack with aliases.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy_exec
    canonical_id: test_pack.test_exec
  - kind: orchestrator
    alias: test_pack.legacy_orch
    canonical_id: test_pack.test_orch
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

    def test_non_builtin_pack_rejects_executor_with_wrong_pack_prefix(self) -> None:
        """An executor in a non-builtin pack whose qualified id prefix does not
        match the containing pack should be rejected with a clear error."""
        root = self.make_pack_root("other_pack")
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: other_pack
name: Other Pack
version: 0.1.0
description: A pack that should not contain test_pack capabilities.
content:
  executors: executors
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Other Pack\n")
        # Write executor with a qualified id whose pack prefix is test_pack, not other_pack
        self.write_valid_executor(root, "executors/wrong_pack", "test_pack.some_exec")

        errors, _warnings = validate_pack(root)
        self.assertTrue(
            any("belongs to pack 'test_pack' but was found in pack 'other_pack'" in error for error in errors),
            errors,
        )

    def test_non_builtin_pack_rejects_orchestrator_with_wrong_pack_prefix(self) -> None:
        """An orchestrator in a non-builtin pack whose qualified id prefix does not
        match the containing pack should be rejected."""
        root = self.make_pack_root("other_pack")
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: other_pack
name: Other Pack
version: 0.1.0
description: A pack that should not contain foreign capabilities.
content:
  orchestrators: orchestrators
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Other Pack\n")
        self.write_valid_orchestrator(
            root, "orchestrators/foreign_orch", "video_editing.hype"
        )

        errors, _warnings = validate_pack(root)
        self.assertTrue(
            any("belongs to pack 'video_editing' but was found in pack 'other_pack'" in error for error in errors),
            errors,
        )

    def test_non_builtin_pack_rejects_element_with_wrong_pack_id(self) -> None:
        """An element whose pack_id does not match the containing pack should be rejected."""
        root = self.make_pack_root("other_pack")
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: other_pack
name: Other Pack
version: 0.1.0
description: Another test pack.
content:
  elements: elements
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Other Pack\n")
        self.write_valid_element(
            root, "elements/effects/wrong_elem", element_id="wrong_elem", pack_id="rendering"
        )

        errors, _warnings = validate_pack(root)
        self.assertTrue(
            any("declares pack_id 'rendering' but was found in pack 'other_pack'" in error for error in errors),
            errors,
        )


class TestSchemaVersionErrors(MinimalPackTestCase):
    """Schema version validation edge cases."""

    def test_missing_schema_version(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """id: test_pack
name: Test Pack
version: 0.1.0
agent:
  purpose: Testing
""",
        )
        errors, _ = validate_pack(root)
        self.assertEqual(len(errors), 1)
        self.assertIn("missing required field schema_version", errors[0])
        self.assertIn("pack.yaml", errors[0])

    def test_unknown_schema_version(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 99
id: test_pack
name: Test Pack
version: 0.1.0
agent:
  purpose: Testing
""",
        )
        errors, _ = validate_pack(root)
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown schema_version 99", errors[0])
        self.assertIn("pack.yaml", errors[0])

    def test_schema_version_string(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: "1"
id: test_pack
name: Test Pack
version: 0.1.0
agent:
  purpose: Testing
""",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("schema_version" in e for e in errors),
            f"No error mentions schema_version: {errors}",
        )

    def test_schema_version_null(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: null
id: test_pack
name: Test Pack
version: 0.1.0
agent:
  purpose: Testing
""",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("schema_version" in e.lower() for e in errors),
            f"No error mentions schema_version: {errors}",
        )


class TestMissingRequiredFields(MinimalPackTestCase):
    """Validation should catch missing required fields."""

    def test_missing_pack_id(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
name: Test Pack
version: 0.1.0
agent:
  purpose: Testing
""",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("id" in e.lower() and "missing" in e.lower() for e in errors),
            f"Expected missing id error, got: {errors}",
        )

    def test_missing_pack_name(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
version: 0.1.0
capabilities: [testing]
""",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("name" in e.lower() and "missing" in e.lower() for e in errors),
            f"Expected missing name error, got: {errors}",
        )

    def test_executor_missing_runtime_entrypoint(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "executors" / "bad_exec" / "executor.yaml",
            """schema_version: 1
id: test_pack.bad_exec
name: Bad Executor
version: 0.1.0
runtime:
  type: python-cli
""",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        # The "oneOf" on runtime reports "not valid under any of the given schemas"
        # because python-cli requires entrypoint. The error still correctly
        # identifies the runtime field as the problem.
        error_text = " ".join(errors)
        self.assertTrue(
            "runtime" in error_text.lower() and "not valid" in error_text.lower(),
            f"Expected runtime validation error, got: {errors}",
        )


class TestMalformedYaml(MinimalPackTestCase):
    """Malformed YAML should produce clear error messages."""

    def test_invalid_yaml_syntax(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 1
id: test_pack
  name: Test Pack  # bad indentation
version: 0.1.0
""",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertIn("invalid YAML", errors[0])
        self.assertIn("pack.yaml", errors[0])

    def test_yaml_not_a_mapping(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            "- item1\n- item2\n",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("mapping" in e.lower() for e in errors),
            f"Expected mapping error, got: {errors}",
        )

    def test_empty_yaml(self) -> None:
        root = self.make_pack_root()
        _write(root / "pack.yaml", "")
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertIn("empty YAML", errors[0])

    def test_yaml_null_document(self) -> None:
        root = self.make_pack_root()
        _write(root / "pack.yaml", "---\n...\n")
        errors, _ = validate_pack(root)
        # This parses to None/null
        self.assertGreater(len(errors), 0)


class TestMissingDocsAndFiles(MinimalPackTestCase):
    """Missing docs, STAGE.md, and runtime files should be flagged."""

    def test_missing_agents_md_is_no_longer_warned(self) -> None:
        """T3 removed root AGENTS.md warnings; skill/SKILL.md is the canonical doc."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        (root / "skill" / "SKILL.md").unlink()
        errors, warnings = validate_pack(root)
        self.assertEqual(errors, [])
        self.assertFalse(
            any("AGENTS.md" in w for w in warnings),
            f"AGENTS.md should not appear in warnings after T3: {warnings}",
        )

    def test_missing_readme_md_is_no_longer_warned(self) -> None:
        """T3 removed root README.md warnings; skill/SKILL.md is the canonical doc."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        errors, warnings = validate_pack(root)
        self.assertEqual(errors, [])
        self.assertFalse(
            any("README.md" in w for w in warnings),
            f"README.md should not appear in warnings after T3: {warnings}",
        )

    def test_missing_runtime_entrypoint_file(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root)
        (root / "executors" / "test_exec" / "run.py").unlink()
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("entrypoint" in e.lower() and "not found" in e.lower() for e in errors),
            f"Expected entrypoint not found error, got: {errors}",
        )

    def test_missing_stage_md_now_warns(self) -> None:
        """T3 downgraded component STAGE.md missing from error to warning."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root)
        (root / "executors" / "test_exec" / "STAGE.md").unlink()
        errors, warnings = validate_pack(root)
        self.assertTrue(
            any("STAGE.md" in w for w in warnings),
            f"Expected STAGE.md warning, got errors={errors}, warnings={warnings}",
        )
        self.assertFalse(
            any("STAGE.md" in e for e in errors),
            f"STAGE.md should not appear in errors after T3: {errors}",
        )

    def test_missing_pack_yaml(self) -> None:
        root = self.make_pack_root()
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertIn("pack manifest not found", errors[0])


class TestUndeclaredContentRoots(MinimalPackTestCase):
    """Undeclared content roots should produce errors for executors/orchestrators/elements."""

    def test_undeclared_content_root_errors(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  executors: nonexistent_executors
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test")
        errors, warnings = validate_pack(root)
        self.assertTrue(
            any("nonexistent_executors" in e and "resource" in e for e in errors),
            f"Expected content root error, got errors={errors}, warnings={warnings}",
        )

    def test_missing_orchestrators_root_is_error(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  orchestrators: nonexistent_orchestrators
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test")
        errors, warnings = validate_pack(root)
        self.assertTrue(
            any("nonexistent_orchestrators" in e and "resource" in e for e in errors),
            f"Expected content root error for orchestrators, got errors={errors}, warnings={warnings}",
        )

    def test_missing_elements_root_is_error(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  elements: nonexistent_elements
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test")
        errors, warnings = validate_pack(root)
        self.assertTrue(
            any("nonexistent_elements" in e and "resource" in e for e in errors),
            f"Expected content root error for elements, got errors={errors}, warnings={warnings}",
        )

    def test_missing_schemas_root_is_rejected_by_resource_closure(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  schemas: nonexistent_schemas
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test")
        errors, warnings = validate_pack(root)
        self.assertTrue(
            any("nonexistent_schemas" in error and "resource" in error for error in errors),
            f"Expected content-root closure error for schemas, got: {errors}",
        )


class TestTaxonomyEnumRejection(MinimalPackTestCase):
    """Taxonomy fields with unknown values should be rejected by the JSON Schema."""

    def _check_enum_error(self, field: str, bad_value: str) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            f"""schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
{field}: {bad_value}
agent:
  purpose: Testing
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test")
        errors, _warnings = validate_pack(root)
        self.assertTrue(
            any(
                field in e
                and ("must be one of" in e or "unknown field" in e or "not valid" in e)
                for e in errors
            ),
            f"Expected enum rejection for {field}={bad_value!r}, got errors={errors}",
        )

    def test_origin_rejects_unknown_value(self) -> None:
        self._check_enum_error("origin", "vendor")

    def test_install_tier_rejects_unknown_value(self) -> None:
        self._check_enum_error("install_tier", "premium")

    def test_pack_type_rejects_unknown_value(self) -> None:
        self._check_enum_error("pack_type", "plugin")

    def test_domain_rejects_unknown_value(self) -> None:
        self._check_enum_error("domain", "blockchain")

    def test_stability_rejects_unknown_value(self) -> None:
        self._check_enum_error("stability", "beta")

    def test_support_rejects_unknown_value(self) -> None:
        self._check_enum_error("support", "vendor")


class TestFileSpecificErrors(MinimalPackTestCase):
    """Errors should reference the specific file path."""

    def test_pack_yaml_error_mentions_file(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
unknown_field: value
agent:
  purpose: Testing
""",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertIn("pack.yaml", errors[0])

    def test_executor_yaml_error_mentions_file(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "executors" / "bad_exec" / "executor.yaml",
            """schema_version: 1
id: test_pack.bad_exec
name: Bad Executor
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
bad_field: true
""",
        )
        _write(root / "executors" / "bad_exec" / "run.py", "print('ok')")
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertIn("executor.yaml", error_text)

    def test_executor_yaml_path_includes_component_dir(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root, "executors/my_exec", "test_pack.my_exec")
        # Corrupt the executor.yaml
        _write(
            root / "executors" / "my_exec" / "executor.yaml",
            """schema_version: 1
id: test_pack.my_exec
name: My Exec
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
illegal: yes
""",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertIn("executors/my_exec/executor.yaml", error_text)


class TestNoExecutionSafety(MinimalPackTestCase):
    """Validation must NOT import or execute run.py files."""

    def test_validate_does_not_execute_run_py(self) -> None:
        """A run.py with side effects must NOT be triggered during validation."""
        root = self.make_pack_root()
        self.write_valid_pack(root)

        # Write a run.py that would create a sentinel file if executed
        sentinel = root / "SENTINEL_WAS_EXECUTED"
        _write(
            root / "executors" / "side_effect_exec" / "executor.yaml",
            """schema_version: 1
id: test_pack.side_effect_exec
name: Side Effect Executor
kind: built_in
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
""",
        )
        _write(
            root / "executors" / "side_effect_exec" / "run.py",
            f"""# This file has side effects that MUST NOT run during validation
import os
# Write a sentinel file to prove we were executed
with open({sentinel!r}, 'w') as f:
    f.write('EXECUTED')
# Potentially dangerous operation (won't actually run)
print('THIS SHOULD NOT PRINT')
""",
        )
        _write(
            root / "executors" / "side_effect_exec" / "STAGE.md",
            "# Side Effect Executor\n\nPurpose: testing.\n",
        )

        # Reset any pre-existing sentinel
        if sentinel.exists():
            sentinel.unlink()

        errors, warnings = validate_pack(root)

        # Validation should succeed (valid pack)
        self.assertEqual(errors, [], f"Unexpected validation errors: {errors}")

        # The sentinel MUST NOT exist — run.py was NOT imported or executed
        self.assertFalse(
            sentinel.exists(),
            "SENTINEL: run.py was EXECUTED during validation! "
            "Validation must be static and never import run.py.",
        )

    def test_validate_does_not_import_run_py_module(self) -> None:
        """A run.py with import-time side effects must NOT trigger."""
        root = self.make_pack_root()
        self.write_valid_pack(root)

        sentinel = root / "IMPORT_SENTINEL"
        # Use a less obvious approach — write a file that sys.modules
        # would record if imported
        _write(
            root / "executors" / "import_test" / "executor.yaml",
            """schema_version: 1
id: test_pack.import_test
name: Import Test
kind: built_in
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
""",
        )
        _write(
            root / "executors" / "import_test" / "run.py",
            f"""import sys
# If this module gets imported, this file should appear in sys.modules
# But let's create a sentinel for certainty
from pathlib import Path
Path({sentinel!r}).write_text('imported')
""",
        )
        _write(
            root / "executors" / "import_test" / "STAGE.md",
            "# Import Test\n\nPurpose: testing.\n",
        )

        if sentinel.exists():
            sentinel.unlink()

        errors, _ = validate_pack(root)
        self.assertEqual(errors, [])
        self.assertFalse(
            sentinel.exists(),
            "IMPORT SENTINEL: run.py was IMPORTED during validation!",
        )

    @pytest.mark.non_root  # chmod 000 does not block reads for root
    def test_validate_rejects_unreadable_run_py(self) -> None:
        """An unreadable declared resource (run.py) is reported, not a crash."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root, "executors/test_exec", "test_pack.test_exec")
        # Make run.py unreadable
        run_py = root / "executors" / "test_exec" / "run.py"
        run_py.chmod(0o000)
        self.addCleanup(run_py.chmod, 0o644)

        # Validation should succeed because we only check existence
        errors, _ = validate_pack(root)
        self.assertTrue(any("cannot be read" in error for error in errors), errors)


class TestPackValidatorClass(MinimalPackTestCase):
    """Direct tests of the PackValidator class API."""

    def test_validator_returns_errors_and_warnings(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        validator = PackValidator(root)
        errors = validator.validate()
        self.assertEqual(errors, [])
        self.assertIsInstance(validator.warnings, list)

    def test_validator_with_missing_pack_yaml(self) -> None:
        root = self.make_pack_root()
        validator = PackValidator(root)
        errors = validator.validate()
        self.assertGreater(len(errors), 0)
        self.assertIn("pack manifest not found", errors[0])

    def test_validate_pack_function(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        errors, warnings = validate_pack(root)
        self.assertEqual(errors, [])
        self.assertIsInstance(warnings, list)

    def test_validate_pack_function_invalid(self) -> None:
        root = self.make_pack_root()
        errors, warnings = validate_pack("/nonexistent/path")
        self.assertGreater(len(errors), 0)


class TestLayoutContractExceptions(MinimalPackTestCase):
    """Declared layout exceptions are parsed and surfaced as one aggregate failure."""

    def test_layout_exception_rejects_windows_drive_path(self) -> None:
        for path in ("C:relative", "C:/absolute"):
            with self.subTest(path=path):
                parsed, issues = parse_layout_exceptions(
                    {
                        "metadata": {
                            "layout": {
                                "exceptions": [
                                    {
                                        "path": path,
                                        "class": "domain_exception",
                                        "reason": "Legacy path.",
                                    }
                                ]
                            }
                        }
                    }
                )
                self.assertEqual(parsed, [])
                self.assertTrue(issues)

    def test_layout_exception_preserves_relative_posix_path(self) -> None:
        parsed, issues = parse_layout_exceptions(
            {
                "metadata": {
                    "layout": {
                        "exceptions": [
                            {
                                "path": "executors/demo/legacy.py",
                                "class": "domain_exception",
                                "reason": "Legacy path.",
                            }
                        ]
                    }
                }
            }
        )
        self.assertEqual(issues, [])
        self.assertEqual(parsed[0].path, "executors/demo/legacy.py")

    def test_permanent_layout_exception_with_lifecycle_passes(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(root / "legacy.py", "# legacy shim\n")
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
metadata:
  layout:
    exceptions:
      - path: legacy.py
        class: domain_exception
        reason: Preserves legacy import path.
        defer_to: permanent
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertTrue(any("unknown field" in error for error in errors), errors)

    def test_layout_exceptions_fail_under_single_aggregate_surface(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root)
        _write(root / "legacy.py", "# legacy shim\n")
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
metadata:
  layout:
    exceptions:
      - path: legacy.py
        class: invalid_class
        reason: Unknown exception class.
      - path: executors/test_exec/run.py
        class: domain_exception
        reason: This path is already canonical.
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertTrue(any("unknown field" in error for error in errors), errors)


class TestFirstPartyPacksRootValidation(MinimalPackTestCase):
    """The first-party packs root is validated as one aggregate surface."""

    def test_repo_first_party_packs_root_validates_cleanly(self) -> None:
        errors, warnings = validate_first_party_packs_root(_FIRST_PARTY_PACKS_ROOT)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")
        self.assertIsInstance(warnings, list)

    def test_inventory_and_layout_fail_under_one_aggregate_surface(self) -> None:
        root = self.make_pack_root() / "packs"
        root.mkdir()
        _mirror_first_party_packs_root(root)
        shutil.rmtree(root / "blender")
        _write(
            root / "blender" / "pack.yaml",
            """id: blender
name: Blender
version: 0.1.0
agent:
  purpose: Broken test fixture
""",
        )
        (root / "rogue").mkdir()

        errors, _warnings = validate_first_party_packs_root(root)

        self.assertGreaterEqual(len(errors), 3, errors)
        self.assertEqual(
            errors[0],
            "first-party pack validation failed (2 issues)",
        )
        self.assertIn(
            "[internal-inventory] unexpected top-level directory: rogue",
            errors,
        )
        self.assertTrue(
            any(
                line.startswith("[layout] blender: pack.yaml: missing required field schema_version")
                for line in errors[1:]
            ),
            errors,
        )

    def test_non_temporary_layout_exception_cannot_defer_to_future_milestone(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(root / "domain.txt", "domain-specific note\n")
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
metadata:
  layout:
    exceptions:
      - path: domain.txt
        class: domain_exception
        reason: Domain-specific top-level asset.
        defer_to: M1
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertTrue(any("unknown field" in error for error in errors), errors)


class TestInvalidPackIdPattern(MinimalPackTestCase):
    """Invalid pack ids should fail schema validation."""

    def test_pack_id_with_hyphens(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: my-pack
name: My Pack
version: 0.1.0
agent:
  purpose: Testing
""",
        )
        errors, _ = validate_pack(root)
        # Hyphens are not allowed in pack_id pattern
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("pattern" in e.lower() or "my-pack" in e for e in errors),
            f"Expected pattern error for id 'my-pack', got: {errors}",
        )

    def test_pack_id_with_uppercase(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: MyPack
name: My Pack
version: 0.1.0
agent:
  purpose: Testing
""",
        )
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("MyPack" in e or "pattern" in e.lower() for e in errors),
            f"Expected pattern error for id 'MyPack', got: {errors}",
        )


class TestExecutorIdMustBeQualified(MinimalPackTestCase):
    """Executor ids must be qualified (<pack>.<slug>)."""

    def test_unqualified_executor_id(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "executors" / "bad_exec" / "executor.yaml",
            """schema_version: 1
id: bad_exec
name: Bad Executor
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
""",
        )
        _write(root / "executors" / "bad_exec" / "run.py", "print('ok')")
        errors, _ = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("pattern" in e.lower() or "bad_exec" in e for e in errors),
            f"Expected pattern/qualified error for 'bad_exec', got: {errors}",
        )


class TestPackLevelAliases(MinimalPackTestCase):
    """Top-level pack alias validation through validate_pack() and PackValidator."""

    def test_valid_pack_with_aliases_passes_validation(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.test_exec
""",
        )
        errors, warnings = validate_pack(root)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

    def test_pack_alias_unknown_key_fails_validation(self) -> None:
        """An alias with extra keys fails JSON schema validation
        because pack.json has additionalProperties: false on alias items."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.test_exec
    extra_field: true
""",
        )
        errors, warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(
                term in error_text.lower()
                for term in ("extra_field", "additionalproperties", "unknown field")
            ),
            f"Expected schema error about extra field, got: {errors}",
        )

    def test_pack_alias_unqualified_alias_fails_schema(self) -> None:
        """An unqualified alias value fails the JSON schema pattern check."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: bare_name
    canonical_id: test_pack.test_exec
""",
        )
        errors, warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(term in error_text.lower() for term in ("pattern", "bare_name")),
            f"Expected schema pattern error for unqualified alias, got: {errors}",
        )

    def test_pack_alias_invalid_kind_fails_schema(self) -> None:
        """An invalid kind value fails JSON schema enum check."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: invalid
    alias: test_pack.legacy
    canonical_id: test_pack.test_exec
""",
        )
        errors, warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            "kind" in error_text.lower()
            and any(t in error_text.lower() for t in ("enum", "invalid", "not valid", "must be one of")),
            f"Expected schema enum error for kind, got: {errors}",
        )

    def test_pack_alias_missing_required_fields_fails_schema(self) -> None:
        """Missing required alias fields fail JSON schema validation."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
""",
        )
        errors, warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(t in error_text.lower() for t in ("required", "alias", "canonical_id")),
            f"Expected required field error, got: {errors}",
        )

    def test_pack_alias_not_array_fails_schema(self) -> None:
        """aliases as string fails JSON schema type check."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases: not_an_array
""",
        )
        errors, warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(t in error_text.lower() for t in ("array", "type", "aliases")),
            f"Expected type error for aliases, got: {errors}",
        )

    def test_discovery_preserves_pack_aliases(self) -> None:
        """_pack_definition_for_discovery preserves aliases from pack data."""
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.old_name
    canonical_id: test_pack.new_name
    deprecated: true
    deprecation_message: Moved
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test")
        validator = PackValidator(root)
        validator._pack_data = validator._load_yaml(root / "pack.yaml")
        pack = validator._pack_definition_for_discovery({"executors": "executors"})
        self.assertEqual(len(pack.aliases), 1)
        self.assertEqual(pack.aliases[0]["kind"], "executor")
        self.assertEqual(pack.aliases[0]["alias"], "test_pack.old_name")
        self.assertEqual(pack.aliases[0]["canonical_id"], "test_pack.new_name")
        self.assertEqual(pack.aliases[0]["deprecated"], True)
        self.assertEqual(pack.aliases[0]["deprecation_message"], "Moved")

    def test_valid_pack_with_extensions_passes_validation(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
agent:
  purpose: Testing
extensions:
  generation:
    backends:
      - id: synthetic_cloud
        module: vendor.synthetic
        class: SyntheticBackend
        init_kwargs:
          retries: 2
    features:
      - t2i
      - id: img2img
        label: Image to Image
    modes:
      - edit
  elements:
    kinds:
      - id: overlays
        singular: overlay
        plural: overlays
  schemas:
    manifest:
      kind: pack
""",
        )
        errors, warnings = validate_pack(root)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

    def test_pack_extensions_unknown_root_key_fails_validation(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
agent:
  purpose: Testing
extensions:
  unknown_root: true
""",
        )
        errors, warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("unknown field" in error.lower() or "additionalproperties" in error.lower() for error in errors),
            f"Expected extensions schema error, got: {errors}",
        )

    def test_pack_extensions_invalid_backend_shape_fails_validation(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
agent:
  purpose: Testing
extensions:
  generation:
    backends:
      - id: synthetic_cloud
        module: vendor.synthetic
""",
        )
        errors, warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        self.assertTrue(
            any("class" in error.lower() and "required" in error.lower() for error in errors),
            f"Expected backend class required error, got: {errors}",
        )

    def test_discovery_preserves_pack_extensions(self) -> None:
        root = self.make_pack_root()
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
agent:
  purpose: Testing
extensions:
  generation:
    features:
      - t2i
  schemas:
    manifest:
      version: 1
""",
        )
        _write(root / "skill" / "SKILL.md", "# Test")
        validator = PackValidator(root)
        validator._pack_data = validator._load_yaml(root / "pack.yaml")
        pack = validator._pack_definition_for_discovery({"executors": "executors"})
        self.assertEqual(
            pack.extensions,
            {
                "generation": {"features": [{"id": "t2i"}]},
                "schemas": {"manifest": {"version": 1}},
            },
        )

    def test_pack_alias_duplicate_alias_same_kind_fails_validation(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.test_exec
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.other_exec
""",
        )
        self.write_valid_executor(root, "executors/other_exec", "test_pack.other_exec")
        errors, _warnings = validate_pack(root)
        self.assertTrue(any("duplicates existing executor alias" in error for error in errors), errors)

    def test_pack_alias_duplicate_alias_across_kinds_is_allowed(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        self.write_valid_executor(root)
        orch_dir = root / "orchestrators" / "test_orch"
        _write(
            orch_dir / "orchestrator.yaml",
            """schema_version: 1
id: test_pack.test_orch
name: Test Orchestrator
kind: built_in
version: 0.1.0
runtime:
  kind: command
  command:
    argv: ["python3", "run.py"]
""",
        )
        _write(orch_dir / "run.py", "print('ok')\n")
        _write(orch_dir / "STAGE.md", "# Test Orchestrator\n")
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.test_exec
  - kind: orchestrator
    alias: test_pack.legacy
    canonical_id: test_pack.test_orch
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

    def test_pack_alias_cycle_fails_validation(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.alias_one
    canonical_id: test_pack.alias_two
  - kind: executor
    alias: test_pack.alias_two
    canonical_id: test_pack.alias_one
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertTrue(any("alias cycle detected" in error for error in errors), errors)

    def test_pack_alias_missing_local_target_fails_validation(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.missing_exec
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertTrue(any("unknown executor id 'test_pack.missing_exec'" in error for error in errors), errors)

    def test_pack_alias_qualified_cross_pack_target_is_rejected(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: external.remote_exec
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertTrue(any("owned by 'test_pack'" in error for error in errors), errors)

    # -- non-object shapes inside the aliases array -----------------------

    def test_pack_alias_string_entry_in_array_fails(self) -> None:
        """A bare string inside the aliases array is not an object."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - just_a_string
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(
                term in error_text.lower()
                for term in ("object", "type", "string", "must be")
            ),
            f"Expected error about non-object alias entry, got: {errors}",
        )

    def test_pack_alias_number_entry_in_array_fails(self) -> None:
        """A bare number inside the aliases array is not an object."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - 42
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(
                term in error_text.lower()
                for term in ("object", "type", "number", "must be")
            ),
            f"Expected error about non-object alias entry, got: {errors}",
        )

    def test_pack_alias_null_entry_in_array_fails(self) -> None:
        """A null inside the aliases array is not an object."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - null
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(
                term in error_text.lower()
                for term in ("object", "type", "null", "must be")
            ),
            f"Expected error about non-object alias entry, got: {errors}",
        )

    # -- invalid deprecation metadata ------------------------------------

    def test_pack_alias_deprecated_non_bool_string_fails(self) -> None:
        """deprecated must be a boolean, not a string."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.test_exec
    deprecated: "yes"
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(
                term in error_text.lower()
                for term in ("deprecated", "boolean", "string", "type", "unknown field")
            ),
            f"Expected error about non-boolean deprecated, got: {errors}",
        )

    def test_pack_alias_deprecated_non_bool_int_fails(self) -> None:
        """deprecated must be a boolean, not an integer."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.test_exec
    deprecated: 1
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(
                term in error_text.lower()
                for term in ("deprecated", "boolean", "integer", "type", "unknown field")
            ),
            f"Expected error about non-boolean deprecated, got: {errors}",
        )

    def test_pack_alias_deprecation_message_non_string_int_fails(self) -> None:
        """deprecation_message must be a string, not an integer."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.test_exec
    deprecated: true
    deprecation_message: 123
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(
                term in error_text.lower()
                for term in ("deprecation_message", "string", "integer", "type", "unknown field")
            ),
            f"Expected error about non-string deprecation_message, got: {errors}",
        )

    def test_pack_alias_deprecation_message_non_string_bool_fails(self) -> None:
        """deprecation_message must be a string, not a boolean."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases:
  - kind: executor
    alias: test_pack.legacy
    canonical_id: test_pack.test_exec
    deprecated: true
    deprecation_message: true
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertGreater(len(errors), 0)
        error_text = " ".join(errors)
        self.assertTrue(
            any(
                term in error_text.lower()
                for term in ("deprecation_message", "string", "boolean", "type", "unknown field")
            ),
            f"Expected error about non-string deprecation_message, got: {errors}",
        )

    # -- empty aliases array ---------------------------------------------

    def test_pack_alias_empty_array_passes(self) -> None:
        """An empty aliases array is valid (no aliases declared)."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        _write(
            root / "pack.yaml",
            """schema_version: 2
id: test_pack
name: Test Pack
version: 0.1.0
description: A test pack.
content:
  executors: executors
  orchestrators: orchestrators
agent:
  purpose: Testing
aliases: []
""",
        )
        errors, _warnings = validate_pack(root)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")


class TestLegacyComponentMetadataAliases(MinimalPackTestCase):
    """Legacy component-level metadata.aliases validation (separate from top-level pack aliases)."""

    def test_legacy_alias_targets_must_exist(self) -> None:
        """Legacy metadata.aliases pointing to unknown capability ids fail."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        comp_dir = root / "executors" / "test_exec"
        _write(
            comp_dir / "executor.yaml",
            """schema_version: 1
id: test_pack.test_exec
name: Test Executor
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
metadata:
  aliases:
    - canonical_id: test_pack.missing
""",
        )
        _write(comp_dir / "run.py", "# Test executor\n")
        errors, _ = validate_pack(root)
        self.assertTrue(
            any("unknown capability id" in error for error in errors), errors
        )

    def test_legacy_alias_string_target_must_exist(self) -> None:
        """Legacy string-form metadata.aliases must resolve to known capability ids."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        comp_dir = root / "executors" / "test_exec"
        _write(
            comp_dir / "executor.yaml",
            """schema_version: 1
id: test_pack.test_exec
name: Test Executor
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
metadata:
  aliases:
    - test_pack.missing
""",
        )
        _write(comp_dir / "run.py", "# Test executor\n")
        errors, _ = validate_pack(root)
        self.assertTrue(
            any("unknown capability id" in error for error in errors), errors
        )

    def test_legacy_alias_must_be_string_or_object(self) -> None:
        """Legacy metadata.aliases entries must be strings or objects, not numbers."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        comp_dir = root / "executors" / "test_exec"
        _write(
            comp_dir / "executor.yaml",
            """schema_version: 1
id: test_pack.test_exec
name: Test Executor
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
metadata:
  aliases:
    - 42
""",
        )
        _write(comp_dir / "run.py", "# Test executor\n")
        errors, _ = validate_pack(root)
        self.assertTrue(
            any(
                t in " ".join(errors).lower()
                for t in ("string or object", "must be")
            ),
            f"Expected error about non-string/non-object alias, got: {errors}",
        )

    def test_legacy_alias_must_declare_canonical_id(self) -> None:
        """Legacy object-form metadata.aliases must declare canonical_id."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        comp_dir = root / "executors" / "test_exec"
        _write(
            comp_dir / "executor.yaml",
            """schema_version: 1
id: test_pack.test_exec
name: Test Executor
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
metadata:
  aliases:
    - description: "no canonical_id here"
""",
        )
        _write(comp_dir / "run.py", "# Test executor\n")
        errors, _ = validate_pack(root)
        self.assertTrue(
            any("must declare canonical_id" in error for error in errors), errors
        )

    def test_legacy_alias_metadata_not_array_fails(self) -> None:
        """Legacy metadata.aliases must be an array."""
        root = self.make_pack_root()
        self.write_valid_pack(root)
        comp_dir = root / "executors" / "test_exec"
        _write(
            comp_dir / "executor.yaml",
            """schema_version: 1
id: test_pack.test_exec
name: Test Executor
version: 0.1.0
runtime:
  type: python-cli
  entrypoint: run.py
metadata:
  aliases: not_an_array
""",
        )
        _write(comp_dir / "run.py", "# Test executor\n")
        errors, _ = validate_pack(root)
        self.assertTrue(
            any("must be an array" in error for error in errors), errors
        )


class TestRuntimeModuleCanonicalization(unittest.TestCase):
    """T5: metadata.runtime_module is the single runtime declaration.

    A python ``runtime`` block may legacy-declare the same module; the parser
    folds it into metadata.runtime_module and rejects a conflicting
    double-declaration.
    """

    def test_executor_only_metadata_runtime_module_loads(self) -> None:
        from astrid.core.execution.executor.schema import validate_executor_definition

        definition = validate_executor_definition(
            {
                "schema_version": 1,
                "id": "test_pack.test_exec",
                "name": "Test Executor",
                "kind": "built_in",
                "version": "0.1.0",
                "metadata": {"runtime_module": "astrid.packs.test_pack.executors.test_exec.run"},
            }
        )
        self.assertEqual(
            definition.metadata["runtime_module"],
            "astrid.packs.test_pack.executors.test_exec.run",
        )

    def test_executor_runtime_module_folded_into_metadata(self) -> None:
        from astrid.core.execution.executor.schema import validate_executor_definition

        definition = validate_executor_definition(
            {
                "schema_version": 1,
                "id": "test_pack.test_exec",
                "name": "Test Executor",
                "kind": "built_in",
                "version": "0.1.0",
                "runtime": {"kind": "python", "module": "pkg.mod.run", "function": "main"},
            }
        )
        self.assertEqual(definition.metadata["runtime_module"], "pkg.mod.run")

    def test_executor_conflicting_double_declaration_rejected(self) -> None:
        from astrid.core.execution.executor.schema import (
            ExecutorValidationError,
            validate_executor_definition,
        )

        with self.assertRaises(ExecutorValidationError) as ctx:
            validate_executor_definition(
                {
                    "schema_version": 1,
                    "id": "test_pack.test_exec",
                    "name": "Test Executor",
                    "kind": "built_in",
                    "version": "0.1.0",
                    "runtime": {"kind": "python", "module": "pkg.a.run", "function": "main"},
                    "metadata": {"runtime_module": "pkg.b.run"},
                }
            )
        self.assertIn("twice with conflicting", str(ctx.exception))

    def test_orchestrator_only_metadata_runtime_module_loads(self) -> None:
        from astrid.core.execution.orchestrator.schema import validate_orchestrator_definition

        definition = validate_orchestrator_definition(
            {
                "schema_version": 1,
                "id": "test_pack.test_orch",
                "name": "Test Orchestrator",
                "kind": "built_in",
                "version": "0.1.0",
                "runtime": {"kind": "command", "command": {"argv": ["x"]}},
                "metadata": {"runtime_module": "pkg.orch.run"},
            }
        )
        self.assertEqual(definition.metadata["runtime_module"], "pkg.orch.run")

    def test_orchestrator_runtime_module_folded_into_metadata(self) -> None:
        from astrid.core.execution.orchestrator.schema import validate_orchestrator_definition

        definition = validate_orchestrator_definition(
            {
                "schema_version": 1,
                "id": "test_pack.test_orch",
                "name": "Test Orchestrator",
                "kind": "built_in",
                "version": "0.1.0",
                "runtime": {"kind": "python", "module": "pkg.orch.run", "function": "main"},
            }
        )
        self.assertEqual(definition.metadata["runtime_module"], "pkg.orch.run")

    def test_orchestrator_conflicting_double_declaration_rejected(self) -> None:
        from astrid.core.execution.orchestrator.schema import (
            OrchestratorValidationError,
            validate_orchestrator_definition,
        )

        with self.assertRaises(OrchestratorValidationError) as ctx:
            validate_orchestrator_definition(
                {
                    "schema_version": 1,
                    "id": "test_pack.test_orch",
                    "name": "Test Orchestrator",
                    "kind": "built_in",
                    "version": "0.1.0",
                    "runtime": {"kind": "python", "module": "pkg.a.run", "function": "main"},
                    "metadata": {"runtime_module": "pkg.b.run"},
                }
            )
        self.assertIn("twice with conflicting", str(ctx.exception))


class TestRuntimeValidatorParity(MinimalPackTestCase):
    """T6: packs validate runs the raising runtime validators after JSON Schema.

    A manifest that passes the JSON-Schema pass but fails the runtime validator
    (conflicting double-declaration of the runtime module) is now rejected by
    ``packs validate``.
    """

    def test_orchestrator_runtime_invalid_but_schema_valid_is_rejected(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        comp_dir = root / "orchestrators/test_orch"
        # python runtime.module conflicts with metadata.runtime_module: both
        # shapes are JSON-Schema valid, but the runtime parser rejects the
        # conflicting double-declaration.
        _write(
            comp_dir / "orchestrator.yaml",
            """schema_version: 1
id: test_pack.test_orch
name: Test Orchestrator
kind: built_in
version: 0.1.0
runtime:
  kind: python
  module: pkg.a.run
  function: main
metadata:
  runtime_module: pkg.b.run
""",
        )
        _write(comp_dir / "run.py", "def main():\n    return None\n")
        _write(comp_dir / "STAGE.md", "# Test Orchestrator\n")
        errors, _ = validate_pack(root)
        self.assertTrue(
            any("twice with conflicting" in error for error in errors),
            f"expected runtime-validator rejection, got: {errors}",
        )

    def test_executor_runtime_invalid_but_schema_valid_is_rejected(self) -> None:
        root = self.make_pack_root()
        self.write_valid_pack(root)
        comp_dir = root / "executors/test_exec"
        _write(
            comp_dir / "executor.yaml",
            """schema_version: 1
id: test_pack.test_exec
name: Test Executor
version: 0.1.0
kind: built_in
runtime:
  kind: python
  module: pkg.a.run
  function: main
metadata:
  runtime_module: pkg.b.run
""",
        )
        _write(comp_dir / "run.py", "# Test executor\n")
        _write(comp_dir / "STAGE.md", "# Test Executor\n")
        errors, _ = validate_pack(root)
        self.assertTrue(
            any("twice with conflicting" in error for error in errors),
            f"expected runtime-validator rejection, got: {errors}",
        )


if __name__ == "__main__":
    unittest.main()
