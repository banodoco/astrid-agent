from __future__ import annotations

import subprocess
from pathlib import Path


import astrid.core.structure as structure
from astrid.core.structure import (
    StructureReport,
    TOP_LEVEL_ASTRID_DIRS,
    TOP_LEVEL_ASTRID_FILES,
    validate_cli_domain_boundary,
    validate_import_layering,
    validate_migration_completion,
    validate_repo_structure,
)


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _bootstrap_structure_root(root: Path) -> None:
    _write(root, "astrid/core/__init__.py", "")
    _write(root, "astrid/packs/__init__.py", "")


def test_validate_import_layering_flags_absolute_and_relative_core_pack_imports(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "astrid/core/bad_absolute.py",
        "from astrid.packs.youtube.executors.upload.src import social_publish\n",
    )
    _write(
        tmp_path,
        "astrid/core/bad_relative.py",
        "from ..packs.youtube.executors.upload.src import social_publish\n",
    )
    _write(
        tmp_path,
        "astrid/core/dynamic_ok.py",
        "import importlib\n"
        "publish = importlib.import_module('astrid.packs.youtube.actions.upload.src.social_publish')\n",
    )

    violations = validate_import_layering(tmp_path)

    assert "astrid/core/bad_absolute.py:1 imports forbidden module 'astrid.packs.youtube.executors.upload.src'" in violations
    assert "astrid/core/bad_relative.py:1 imports forbidden module 'astrid.packs.youtube.executors.upload.src'" in violations
    assert (
        "astrid/core/dynamic_ok.py:2 dynamically imports forbidden concrete pack module "
        "'astrid.packs.youtube.actions.upload.src.social_publish'"
    ) in violations


def test_validate_import_layering_allows_core_subsystem_imports(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "astrid/core/uses_audit.py",
        "from astrid.core.audit.recorder import record_event\n",
    )

    violations = validate_import_layering(tmp_path)

    assert violations == []


def test_validate_import_layering_flags_dynamic_concrete_pack_imports_and_respects_bridge_exemptions(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "astrid/core/bad_dynamic.py",
        "import importlib\n"
        "runtime = importlib.import_module('astrid.packs.video_editing.actions.hype.entrypoint')\n",
    )
    _write(
        tmp_path,
        "astrid/core/pack/resolver.py",
        "from importlib import import_module\n"
        "runtime = import_module('astrid.packs.video_editing.actions.hype.entrypoint')\n",
    )

    violations = validate_import_layering(tmp_path)

    assert set(violations) == {
        "astrid/core/bad_dynamic.py:2 dynamically imports forbidden concrete pack module 'astrid.packs.video_editing.actions.hype.entrypoint'"
    }


def test_validate_cli_domain_boundary_flags_domains_importing_cli_modules(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "astrid/packs/editorial/hype/rules.py",
        "from astrid.core.pack.cli import build_parser\n",
    )
    _write(
        tmp_path,
        "astrid/core/session/cli_status.py",
        "from astrid.core.cli.session import _json_mode\n",
    )

    violations = validate_cli_domain_boundary(tmp_path)

    assert set(violations) == {
        "astrid/packs/editorial/hype/rules.py:1 imports CLI module 'astrid.core.pack.cli'; move CLI-only logic to a cli.py entrypoint or shared helper"
    }


def test_validate_repo_structure_flags_generated_debris_and_non_golden_build_dirs(tmp_path: Path) -> None:
    _bootstrap_structure_root(tmp_path)
    _write(tmp_path, "astrid/__init__.py", "")
    _write(tmp_path, "astrid/core/runtime.py", "")
    _write(tmp_path, "tests/test_example.py", "")
    _write(tmp_path, "scripts/tool.py", "")
    _write(tmp_path, "astrid/core/__pycache__/runtime.cpython-313.pyc", "")
    _write(tmp_path, "tests/.DS_Store", "")
    _write(tmp_path, "scripts/build/generated.json", "{}\n")

    report = validate_repo_structure(tmp_path)

    assert "astrid/core/__pycache__: generated debris directory must not exist" in report.errors
    assert "tests/.DS_Store: generated debris file must not exist" in report.errors
    assert (
        "scripts/build: generated build directory must not exist outside documented golden fixtures"
        in report.errors
    )
    assert "top-level astrid directory is not a canonical concept: astrid/__pycache__" not in report.errors


def test_validate_repo_structure_rejects_invalid_golden_build_exemption_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _bootstrap_structure_root(tmp_path)
    _write(tmp_path, "astrid/__init__.py", "")

    monkeypatch.setattr(
        structure,
        "_COMMITTED_GOLDEN_BUILD_DIR_EXEMPTIONS",
        {
            "fixtures/not-build": "wrong leaf name",
            "tests/fixtures/golden/build": "",
            "docs/build": "wrong root",
        },
    )

    report = validate_repo_structure(tmp_path)

    assert "docs/build: documented golden build exemption must live under astrid/, tests/, or scripts/" in report.errors
    assert "fixtures/not-build: documented golden build exemption must point to an exact build/ directory" in report.errors
    assert "tests/fixtures/golden/build: documented golden build exemption must include a rationale" in report.errors


def test_validate_repo_structure_ignores_untracked_runtime_debris_in_git_worktree(tmp_path: Path) -> None:
    _bootstrap_structure_root(tmp_path)
    _write(tmp_path, "astrid/__init__.py", "")
    _write(tmp_path, "astrid/core/runtime.py", "")
    _write(tmp_path, "tests/test_example.py", "")
    _write(tmp_path, "scripts/tool.py", "")
    _write(tmp_path, ".gitignore", "__pycache__/\n.DS_Store\nbuild/\n")
    subprocess.run(["git", "init"], check=True, cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "astrid@example.com"], check=True, cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Astrid Tests"], check=True, cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "add", "astrid/__init__.py", "astrid/core/runtime.py", "tests/test_example.py", "scripts/tool.py", ".gitignore"],
        check=True,
        cwd=tmp_path,
        capture_output=True,
    )
    subprocess.run(["git", "commit", "-m", "init"], check=True, cwd=tmp_path, capture_output=True)
    _write(tmp_path, "astrid/core/__pycache__/runtime.cpython-313.pyc", "")
    _write(tmp_path, "tests/.DS_Store", "")
    _write(tmp_path, "scripts/build/generated.json", "{}\n")

    report = validate_repo_structure(tmp_path)

    assert report.ok, report.errors


def test_validate_repo_structure_allows_documented_golden_build_exemption(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A documented golden build/ exemption with the correct path leaf,
    a scan-root parent, and a non-empty rationale must allow a committed
    build/ directory to pass structure validation."""
    _bootstrap_structure_root(tmp_path)
    _write(tmp_path, "astrid/__init__.py", "")
    _write(tmp_path, "astrid/core/runtime.py", "")
    _write(tmp_path, "tests/test_example.py", "")
    _write(tmp_path, "scripts/tool.py", "")
    _write(tmp_path, ".gitignore", "build/\n__pycache__/\n.DS_Store\n")
    subprocess.run(["git", "init"], check=True, cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "astrid@example.com"],
        check=True,
        cwd=tmp_path,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Astrid Tests"],
        check=True,
        cwd=tmp_path,
        capture_output=True,
    )
    # Commit the golden build/ directory so it is tracked.  The .gitignore
    # above excludes build/ so we force-add the golden fixture.
    _write(tmp_path, "tests/fixtures/build/golden_data.json", "{}\n")
    subprocess.run(
        ["git", "add", "astrid/__init__.py", "astrid/core/runtime.py",
         "tests/test_example.py", "scripts/tool.py", ".gitignore"],
        check=True, cwd=tmp_path, capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-f", "tests/fixtures/build/golden_data.json"],
        check=True,
        cwd=tmp_path,
        capture_output=True,
    )
    subprocess.run(["git", "commit", "-m", "init"], check=True, cwd=tmp_path, capture_output=True)

    monkeypatch.setattr(
        structure,
        "_COMMITTED_GOLDEN_BUILD_DIR_EXEMPTIONS",
        {
            "tests/fixtures/build": "golden test fixture for offline reference data",
        },
    )

    report = validate_repo_structure(tmp_path)

    assert report.ok, report.errors


def test_validate_repo_structure_flags_debris_in_all_scan_roots(
    tmp_path: Path,
) -> None:
    """__pycache__ directories and .DS_Store files must be flagged in every
    scan root (astrid/, tests/, scripts/), not just a single root."""
    _bootstrap_structure_root(tmp_path)
    _write(tmp_path, "astrid/__init__.py", "")
    _write(tmp_path, "astrid/core/runtime.py", "")
    _write(tmp_path, "tests/test_example.py", "")
    _write(tmp_path, "scripts/tool.py", "")

    # __pycache__ in all three roots
    _write(tmp_path, "astrid/core/__pycache__/runtime.cpython-313.pyc", "")
    _write(tmp_path, "tests/__pycache__/test_example.cpython-313.pyc", "")
    _write(tmp_path, "scripts/__pycache__/tool.cpython-313.pyc", "")

    # .DS_Store in all three roots
    _write(tmp_path, "astrid/.DS_Store", "")
    _write(tmp_path, "tests/.DS_Store", "")
    _write(tmp_path, "scripts/.DS_Store", "")

    report = validate_repo_structure(tmp_path)

    assert "astrid/core/__pycache__: generated debris directory must not exist" in report.errors
    assert "tests/__pycache__: generated debris directory must not exist" in report.errors
    assert "scripts/__pycache__: generated debris directory must not exist" in report.errors
    assert "astrid/.DS_Store: generated debris file must not exist" in report.errors
    assert "tests/.DS_Store: generated debris file must not exist" in report.errors
    assert "scripts/.DS_Store: generated debris file must not exist" in report.errors
    # __pycache__ at the astrid/ top level must NOT be reported as a
    # top-level-canonical violation — debris owns the message.
    assert "top-level astrid directory is not a canonical concept: astrid/__pycache__" not in report.errors


def test_validate_repo_structure_allows_pack_declared_custom_element_kind(tmp_path: Path) -> None:
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/demo/pack.yaml",
        "schema_version: 2\n"
        "id: demo\n"
        "name: Demo Pack\n"
        "version: 0.1.0\n"
        "extensions:\n"
        "  elements:\n"
        "    kinds:\n"
        "      - id: widgets\n"
        "        singular: widget\n",
    )
    _write(
        tmp_path,
        "astrid/packs/demo/elements/widgets/glow/component.tsx",
        "export default function Glow() { return null; }\n",
    )
    _write(
        tmp_path,
        "astrid/packs/demo/elements/widgets/glow/element.yaml",
        "schema_version: 1\n"
        "id: glow\n"
        "kind: widget\n"
        "pack_id: demo\n"
        "schema: {}\n"
        "defaults: {}\n"
        "dependencies: {}\n",
    )

    report = validate_repo_structure(tmp_path)

    assert report.errors == ()


def test_validate_import_layering_allows_core_imports_but_flags_pack_imports(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "astrid/core/somewhere/leaf.py",
        "from astrid.core.contracts.errors import AstridError\n",
    )
    _write(
        tmp_path,
        "astrid/core/somewhere/other.py",
        "from astrid.core.util.time import utc_now_seconds\n",
    )
    _write(
        tmp_path,
        "astrid/core/somewhere/builder.py",
        "from astrid.packs.video_editing.actions.hype.plan_template import build_plan_v2\n",
    )
    _write(
        tmp_path,
        "astrid/core/somewhere/not_lifecycle.py",
        "from astrid.core.contracts.errors import AstridError\n",
    )

    violations = validate_import_layering(tmp_path)

    assert set(violations) == {
        "astrid/core/somewhere/builder.py:1 imports forbidden module 'astrid.packs.video_editing.actions.hype.plan_template'",
    }


def test_validate_migration_completion_reports_expected_advisories(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "astrid/core/deprecated_module.py",
        '"""DEPRECATED compatibility layer."""\n'
        "__all__ = ['new_name', 'old_name']\n"
        "old_name = 'value'\n"
        "new_name = old_name\n",
    )
    _write(
        tmp_path,
        "astrid/core/shim_module.py",
        '"""Compatibility shim for older imports."""\n'
        "from astrid.core.real_target import value\n",
    )
    _write(tmp_path, "astrid/core/real_target.py", "value = 1\n")
    _write(tmp_path, "astrid/core/shim_consumer.py", "from astrid.core.shim_module import value\n")
    _write(
        tmp_path,
        "astrid/core/sys_modules_alias.py",
        "import sys\n"
        "sys.modules['astrid.core.legacy'] = sys.modules[__name__]\n",
    )

    advisories = validate_migration_completion(tmp_path)

    assert "astrid/core/deprecated_module.py: DEPRECATED marker lacks TODO(milestone) removal target" in advisories
    assert any(
        advisory.endswith("__all__ exports alias new_name = old_name") for advisory in advisories
    )
    assert "astrid/core/shim_module.py: compatibility shim still has 1 live import caller(s)" in advisories
    assert "astrid/core/sys_modules_alias.py: sys.modules injection remains outside tests" in advisories


def test_validate_repo_structure_promotes_migration_completion_violations_to_errors(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "astrid/core/deprecated_module.py",
        '"""DEPRECATED compatibility layer."""\n'
        "__all__ = ['new_name', 'old_name']\n"
        "old_name = 'value'\n"
        "new_name = old_name\n",
    )
    _write(
        tmp_path,
        "astrid/core/shim_module.py",
        '"""Compatibility shim for older imports."""\n'
        "from astrid.core.real_target import value\n",
    )
    _write(tmp_path, "astrid/core/real_target.py", "value = 1\n")
    _write(tmp_path, "astrid/core/shim_consumer.py", "from astrid.core.shim_module import value\n")
    _write(
        tmp_path,
        "astrid/core/sys_modules_alias.py",
        "import sys\n"
        "sys.modules['astrid.core.legacy'] = sys.modules[__name__]\n",
    )

    report = validate_repo_structure(tmp_path)

    assert "astrid/core/deprecated_module.py: DEPRECATED marker lacks TODO(milestone) removal target" in report.errors
    assert any(
        error.endswith("__all__ exports alias new_name = old_name") for error in report.errors
    )
    assert "astrid/core/shim_module.py: compatibility shim still has 1 live import caller(s)" in report.errors
    assert "astrid/core/sys_modules_alias.py: sys.modules injection remains outside tests" in report.errors
    assert report.warnings == ()


def test_validate_repo_structure_flags_deprecated_without_todo_as_error(tmp_path: Path) -> None:
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/core/deprecated_module.py",
        '"""DEPRECATED compatibility layer."""\n'
        "value = 1\n",
    )

    report = validate_repo_structure(tmp_path)

    assert report.errors == (
        "astrid/core/deprecated_module.py: DEPRECATED marker lacks TODO(milestone) removal target",
    )


def test_validate_repo_structure_flags_non_exempt_sys_modules_injection_as_error(tmp_path: Path) -> None:
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/core/sys_modules_alias.py",
        "import sys\n"
        "sys.modules['astrid.core.legacy'] = sys.modules[__name__]\n",
    )

    report = validate_repo_structure(tmp_path)

    assert report.errors == (
        "astrid/core/sys_modules_alias.py: sys.modules injection remains outside tests",
    )


def test_validate_repo_structure_flags_dangling_all_alias_as_error(tmp_path: Path) -> None:
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/core/deprecated_module.py",
        "__all__ = ['new_name', 'old_name']\n"
        "old_name = 'value'\n"
        "new_name = old_name\n",
    )

    report = validate_repo_structure(tmp_path)

    assert report.errors == (
        "astrid/core/deprecated_module.py:3: __all__ exports alias new_name = old_name",
    )


def test_validate_repo_structure_flags_non_exempt_compatibility_shim_as_error(tmp_path: Path) -> None:
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/core/shim_module.py",
        '"""Compatibility shim for older imports."""\n'
        "from astrid.core.real_target import value\n",
    )
    _write(tmp_path, "astrid/core/real_target.py", "value = 1\n")
    _write(tmp_path, "astrid/core/shim_consumer.py", "from astrid.core.shim_module import value\n")

    report = validate_repo_structure(tmp_path)

    assert report.errors == (
        "astrid/core/shim_module.py: compatibility shim still has 1 live import caller(s)",
    )


def test_validate_migration_completion_flags_in_process_sys_modules_pattern(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "astrid/core/runtime/in_process.py",
        "import sys\n"
        "sys.modules['temp'] = object()\n",
    )
    _write(
        tmp_path,
        "astrid/core/not_in_process.py",
        "import sys\n"
        "sys.modules['temp'] = object()\n",
    )

    advisories = validate_migration_completion(tmp_path)

    assert "astrid/core/runtime/in_process.py: sys.modules injection remains outside tests" in advisories
    assert "astrid/core/not_in_process.py: sys.modules injection remains outside tests" in advisories


def test_validate_repo_structure_has_no_in_process_sys_modules_exemption(tmp_path: Path) -> None:
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/core/runtime/in_process.py",
        "import sys\n"
        "sys.modules['temp'] = object()\n",
    )

    report = validate_repo_structure(tmp_path)

    assert report.errors == (
        "astrid/core/runtime/in_process.py: sys.modules injection remains outside tests",
    )


def test_structure_report_ok_tracks_errors_only() -> None:
    assert StructureReport(errors=(), warnings=("warn",)).ok is True
    assert StructureReport(errors=("error",), warnings=()).ok is False


# ── compatibility shim detection (no exemptions) ───────────────────────────

def test_validate_repo_structure_still_flags_non_exempt_compatibility_shim(
    tmp_path: Path,
) -> None:
    """Prove the broader generic shim detector is not weakened: a non-exempt
    file that looks like a compatibility shim with live callers is still
    flagged as an error."""
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/core/legacy_shim.py",
        '\"\"\"Compatibility shim — should still be caught.\"\"\"\n'
        "from astrid.core.real_target import value\n",
    )
    _write(tmp_path, "astrid/core/real_target.py", "value = 1\n")
    _write(
        tmp_path,
        "astrid/core/shim_caller.py",
        "from astrid.core.legacy_shim import value\n",
    )

    report = validate_repo_structure(tmp_path)

    assert any(
        "compatibility shim still has" in err for err in report.errors
    ), f"Non-exempt shim was NOT flagged: {report.errors}"


def test_timeline_facade_files_are_strictly_thin_re_exports() -> None:
    """The ``astrid/core/timeline/__init__.py`` public surface module must be a
    thin re-export: no runtime logic, no ``_sync_private_hooks``, and
    no function/class definitions."""
    import ast

    from astrid.core.foundation.paths import REPO_ROOT

    facade_files = (
        REPO_ROOT / "astrid" / "core" / "timeline" / "__init__.py",
    )

    for path in facade_files:
        assert path.is_file(), f"Facade file missing: {path}"
        text = path.read_text(encoding="utf-8")

        # No _sync_private_hooks anywhere in the file
        assert "_sync_private_hooks" not in text, (
            f"{path.name} contains _sync_private_hooks"
        )

        # Parse and verify only docstrings, __future__, and imports exist
        tree = ast.parse(text, filename=str(path))
        for node in ast.iter_child_nodes(tree):
            # Module-level docstring (Expr -> Constant str)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue
            # from __future__ import annotations
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                continue
            # Regular imports and from-imports
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            # Allow __all__ assignment
            if isinstance(node, ast.Assign):
                targets = node.targets
                if len(targets) == 1 and isinstance(targets[0], ast.Name) and targets[0].id == "__all__":
                    continue
            # Fail on any other top-level statement (function def, class def,
            # assignment other than __all__, etc.)
            assert False, (
                f"{path.name}:{node.lineno} contains non-import statement: "
                f"{ast.dump(node)}"
            )


# ── M2 T13: top-level astrid/packs/ module enforcement (zero shims) ──────


def test_packs_top_level_rejects_thin_compatibility_shim(
    tmp_path: Path,
) -> None:
    """A thin documented compatibility/re-export shim at the top level of
    ``astrid/packs/`` must be flagged as an error — zero shims allowed."""
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/validate.py",
        '\"\"\"Compatibility re-export shim for astrid.core.pack.validate.\\n'
        'The canonical implementation lives at astrid.core.pack.validate.\\n'
        '\"\"\"\\n'
        'from astrid.core.pack.validate import *  # noqa\\n',
    )
    report = validate_repo_structure(tmp_path)
    assert any(
        "top-level astrid/packs/ module is not allowed" in err
        for err in report.errors
    ), f"Thin documented shim should have been flagged but wasn't: {report.errors}"


def test_packs_top_level_rejects_backward_compatibility_shim(
    tmp_path: Path,
) -> None:
    """A module marked as a backward-compatibility shim must be rejected —
    zero shims allowed at astrid/packs/."""
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/install.py",
        '\"\"\"Backward-compatibility shim for astrid.core.pack.install.\\n'
        'The implementation lives at astrid.core.pack.install.\\n'
        '\"\"\"\\n'
        'import sys as _sys\\n'
        '_sys.modules[__name__] = _sys.modules["astrid.core.pack.install"]\\n',
    )
    report = validate_repo_structure(tmp_path)
    assert any(
        "top-level astrid/packs/ module is not allowed" in err
        for err in report.errors
    ), f"Backward-compatibility shim should have been flagged but wasn't: {report.errors}"


def test_packs_top_level_allows_init_py(
    tmp_path: Path,
) -> None:
    """``astrid/packs/__init__.py`` is the package namespace and must never
    be flagged, even though it contains real imports."""
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/__init__.py",
        '\"\"\"Astrid pack content (executors, orchestrators, elements).\"\"\"\\n'
        'from .agent_index import build_agent_index\\n'
        'from .validate import validate_pack\\n\\n'
        '__all__ = ["build_agent_index", "validate_pack"]\\n',
    )
    report = validate_repo_structure(tmp_path)
    assert not any(
        "top-level astrid/packs/ module is not allowed" in err
        for err in report.errors
    ), f"__init__.py was incorrectly flagged: {report.errors}"


def test_packs_top_level_rejects_active_implementation_module(
    tmp_path: Path,
) -> None:
    """A top-level ``astrid/packs/*.py`` file that is an active implementation
    module must be flagged as an error."""
    _bootstrap_structure_root(tmp_path)
    _write(
        tmp_path,
        "astrid/packs/my_feature.py",
        '\"\"\"Active implementation of MyFeature.\\n'
        'This is real runtime logic, not a pass-through adapter.\\n'
        '\"\"\"\\n'
        'import os\\n\\n'
        'DEFAULT_TIMEOUT = 30\\n\\n'
        'def run_feature(path: str) -> dict:\\n'
        '    \"\"\"Execute the feature pipeline.\"\"\"\\n'
        '    with open(path) as f:\\n'
        '        data = f.read()\\n'
        '    return {"result": data}\\n',
    )
    report = validate_repo_structure(tmp_path)
    errs = [
        e for e in report.errors
        if "top-level astrid/packs/ module is not allowed" in e
    ]
    assert len(errs) == 1, (
        f"Expected exactly 1 pack top-level error; got {len(errs)}: {report.errors}"
    )
    assert "astrid/packs/my_feature.py" in errs[0], (
        f"Error should reference my_feature.py: {errs[0]}"
    )


def test_packs_top_level_rejects_oversized_shim(
    tmp_path: Path,
) -> None:
    """A module that claims to be a compatibility shim but exceeds the
    meaningful-line threshold must still be flagged — it is not a thin shim
    and no shims are allowed anyway."""
    _bootstrap_structure_root(tmp_path)
    # Build a module with a shim docstring but 15 meaningful lines.
    body_lines = [
        '\"\"\"Compatibility re-export shim for legacy imports.\\n'
        'This should be thin but is not.\\n'
        '\"\"\"',
    ]
    # Add 15 meaningful non-comment non-docstring lines.
    for i in range(15):
        body_lines.append(f"_VAR_{i} = {i}")
    _write(tmp_path, "astrid/packs/fat_shim.py", "\n".join(body_lines) + "\n")
    report = validate_repo_structure(tmp_path)
    errs = [
        e for e in report.errors
        if "top-level astrid/packs/ module is not allowed" in e
    ]
    assert len(errs) == 1, (
        f"Expected exactly 1 oversized-shim error; got {len(errs)}: {report.errors}"
    )
    assert "astrid/packs/fat_shim.py" in errs[0], (
        f"Error should reference fat_shim.py: {errs[0]}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# M0 real-repo smoke — T6
# ═══════════════════════════════════════════════════════════════════════════


def test_real_repo_top_level_matches_structure_constants() -> None:
    """M0 real-repo smoke: on-disk ``astrid/`` layout matches
    ``TOP_LEVEL_ASTRID_FILES`` and ``TOP_LEVEL_ASTRID_DIRS`` with the
    same filtering as ``_validate_top_level_astrid()``.

    * Every named file must exist on disk.
    * Every named directory must exist **or** be ``elements`` (the single
      planned-absent canonical concept per SD3).
    * No unexpected ``.py`` files or directories may be present on disk
      (dotfiles are skipped, matching the validator filter).
    """
    from astrid.core.foundation.paths import REPO_ROOT
    from astrid.core.structure import TOP_LEVEL_ASTRID_DIRS, TOP_LEVEL_ASTRID_FILES

    astrid_root = REPO_ROOT / "astrid"
    assert astrid_root.is_dir(), "astrid/ directory must exist on disk"

    # Canonical planned-absent entry (SD3).
    PLANNED_ABSENT_DIRS: frozenset[str] = frozenset({"elements"})
    # ── constants → disk ──────────────────────────────────────────────

    for fname in sorted(TOP_LEVEL_ASTRID_FILES):
        fpath = astrid_root / fname
        assert fpath.is_file(), (
            f"TOP_LEVEL_ASTRID_FILES entry 'astrid/{fname}' missing from disk"
        )

    for dname in sorted(TOP_LEVEL_ASTRID_DIRS):
        dpath = astrid_root / dname
        if dname in PLANNED_ABSENT_DIRS:
            assert not dpath.exists(), (
                f"Planned-absent directory 'astrid/{dname}' "
                f"must NOT exist on disk in M0"
            )
        else:
            assert dpath.is_dir(), (
                f"TOP_LEVEL_ASTRID_DIRS entry 'astrid/{dname}' "
                f"missing from disk"
            )

    # ── disk → constants (validator filter) ───────────────────────────
    # Entries already in the shrink-only structure baseline are known debt
    # (scripts/reshape/baselines/structure_astrid.json); anything else is new.
    import json

    baseline = set(json.loads(
        (REPO_ROOT / "scripts" / "reshape" / "baselines" / "structure_astrid.json").read_text(encoding="utf-8")
    )["violations"])

    for child in sorted(astrid_root.iterdir()):
        if (f"top-level astrid module must move to a canonical package: astrid/{child.name}" in baseline
                or f"top-level astrid directory is not a canonical concept: astrid/{child.name}" in baseline):
            continue
        # Skip dotfiles (same filter as _validate_top_level_astrid).
        if child.name.startswith("."):
            continue
        if child.name == "__pycache__":
            continue
        if child.is_file() and child.suffix == ".py":
            assert child.name in TOP_LEVEL_ASTRID_FILES, (
                f"Unexpected top-level .py file: astrid/{child.name}"
            )
        if child.is_dir():
            assert child.name in TOP_LEVEL_ASTRID_DIRS, (
                f"Unexpected top-level directory: astrid/{child.name}"
            )


def test_architecture_inventories_parse_and_have_required_structure() -> None:
    """M0 real-repo smoke: all three architecture inventories parse as valid
    JSON and contain the expected structural keys.

    The three inventories are:

    1. ``docs/architecture/top-level-inventory.json``
    2. ``docs/architecture/pack-layout-variants.json``
    3. ``docs/architecture/giant-file-split-candidates.json``
    """
    import json

    from astrid.core.foundation.paths import REPO_ROOT

    arch_dir = REPO_ROOT / "docs" / "architecture"

    # ── 1. top-level-inventory.json ───────────────────────────────────

    tli_path = arch_dir / "top-level-inventory.json"
    assert tli_path.is_file(), f"Missing: {tli_path}"
    tli = json.loads(tli_path.read_text(encoding="utf-8"))

    assert isinstance(tli.get("top_level_files"), list), (
        "top-level-inventory.json: 'top_level_files' must be a list"
    )
    assert len(tli["top_level_files"]) >= 1, (
        f"Expected at least one top-level file; got {len(tli['top_level_files'])}"
    )
    for entry in tli["top_level_files"]:
        assert "name" in entry, f"File entry missing 'name': {entry}"
        assert "classification" in entry, (
            f"File entry missing 'classification': {entry}"
        )

    assert isinstance(tli.get("top_level_directories"), list), (
        "top-level-inventory.json: 'top_level_directories' must be a list"
    )
    for entry in tli["top_level_directories"]:
        assert "name" in entry, f"Dir entry missing 'name': {entry}"
        assert "classification" in entry, (
            f"Dir entry missing 'classification': {entry}"
        )

    assert [entry["name"] for entry in tli["top_level_files"]] == sorted(
        TOP_LEVEL_ASTRID_FILES
    )
    assert [entry["name"] for entry in tli["top_level_directories"]] == sorted(
        TOP_LEVEL_ASTRID_DIRS
    )

    # ── 2. pack-layout-variants.json ──────────────────────────────────

    plv_path = arch_dir / "pack-layout-variants.json"
    assert plv_path.is_file(), f"Missing: {plv_path}"
    plv = json.loads(plv_path.read_text(encoding="utf-8"))

    assert isinstance(plv.get("packs"), list), (
        "pack-layout-variants.json: 'packs' must be a list"
    )
    assert len(plv["packs"]) >= 10, (
        f"Expected >= 10 packs; got {len(plv['packs'])}"
    )
    for entry in plv["packs"]:
        assert "id" in entry, f"Pack entry missing 'id': {entry}"
        assert "variant" in entry, f"Pack entry missing 'variant': {entry}"

    assert isinstance(plv.get("internal_directories"), dict), (
        "pack-layout-variants.json: 'internal_directories' must be a dict"
    )

    # ── 3. giant-file-split-candidates.json ───────────────────────────

    gfs_path = arch_dir / "giant-file-split-candidates.json"
    assert gfs_path.is_file(), f"Missing: {gfs_path}"
    gfs = json.loads(gfs_path.read_text(encoding="utf-8"))

    required = gfs.get("required_starting_set")
    assert isinstance(required, dict), (
        "giant-file-split-candidates.json: 'required_starting_set' must be a dict"
    )
    candidates = required.get("candidates")
    assert isinstance(candidates, list), (
        "giant-file-split-candidates.json: "
        "'required_starting_set.candidates' must be a list"
    )
    assert len(candidates) >= 4, (
        f"Expected >= 4 required starting-set candidates; got {len(candidates)}"
    )
    for entry in candidates:
        assert "file" in entry, f"Giant candidate missing 'file': {entry}"
        assert "lines" in entry, f"Giant candidate missing 'lines': {entry}"


# ═══════════════════════════════════════════════════════════════════════════
# M0 public import and compatibility-shim smoke — T7
# ═══════════════════════════════════════════════════════════════════════════


def test_public_import_and_shim_smoke() -> None:
    """M0 public import smoke (T7): ``import astrid`` resolves and exposes
    representative public SDK facade symbols; the stable ``astrid.core.gateway``
    public import surface remains importable.

    ``astrid.core.gateway`` aliases itself to ``astrid.core.gateway`` via
    ``sys.modules`` so assertions are order-insensitive and do not require
    object identity.
    """
    import sys

    import astrid

    # ── Representative public SDK facade symbols (subset of _SDK_EXPORTS) ──
    _REPRESENTATIVE_SDK_SYMBOLS: frozenset[str] = frozenset(
        {
            # Functions
            "discover",
            "invoke",
            "generate",
            # DTOs / data classes
            "Capability",
            "DiscoveryResult",
            "InvocationResult",
            "EventStreamRecord",
            # Exceptions
            "AstridSDKError",
            "CapabilityNotFoundError",
            # Contracts
            "CapabilityHandle",
            "Port",
            "Output",
            "ExecError",
        }
    )

    for sym in sorted(_REPRESENTATIVE_SDK_SYMBOLS):
        assert hasattr(astrid, sym), (
            f"astrid.{sym} must be accessible as a public SDK symbol"
        )

    # Verify __all__ contains the representative symbols.
    astrid_all = set(astrid.__all__)
    missing_from_all = _REPRESENTATIVE_SDK_SYMBOLS - astrid_all
    assert not missing_from_all, (
        f"Representative symbols missing from astrid.__all__: "
        f"{sorted(missing_from_all)}"
    )

    # pipeline aliases itself to astrid.core.gateway via sys.modules.
    # Only assert importability — do NOT rely on object identity.
    import astrid.core.gateway  # noqa: F401

    assert "astrid.core.gateway" in sys.modules, (
        "astrid.core.gateway must be registered in sys.modules"
    )


# ── M4 giant-file inventory contract ─────────────────────────────────────


def test_giant_file_inventory_matches_rationale() -> None:
    """M4 gate: every ``astrid/**/*.py`` file over 1,200 physical lines
    must appear in ``docs/giant-file-rationale.md``, and every entry in
    the rationale must correspond to an existing file that genuinely
    exceeds the threshold.  Files at or below 1,200 lines are watch-only
    and do not require rationale entries.
    """
    import re

    from astrid.core.foundation.paths import REPO_ROOT

    GIANT_THRESHOLD = 1200

    # ── 1. Scan every astrid/**/*.py file and count physical lines ─────
    astrid_root = REPO_ROOT / "astrid"
    py_files: dict[str, int] = {}
    for py_path in sorted(astrid_root.rglob("*.py")):
        # Skip __pycache__ and other dotted directories.
        parts = py_path.relative_to(astrid_root).parts
        if any(part.startswith(".") for part in parts):
            continue
        rel = py_path.relative_to(REPO_ROOT).as_posix()
        lines = py_path.read_text(encoding="utf-8").count("\n")
        py_files[rel] = lines

    # ── 2. Parse the rationale doc ──────────────────────────────────────
    rationale_path = REPO_ROOT / "docs" / "giant-file-rationale.md"
    rationale_text = rationale_path.read_text(encoding="utf-8")

    # Extract table entries of the form: | N | `path` | NNNN | ...
    # Pattern: | number | `astrid/...` | digits | ...
    entry_pattern = re.compile(
        r"\|\s*\d+\s*\|\s*`([^`]+)`\s*\|\s*([\d,]+)\s*\|"
    )
    rationale_entries: dict[str, int] = {}
    for match in entry_pattern.finditer(rationale_text):
        file_path = match.group(1)
        lines_str = match.group(2).replace(",", "")
        rationale_entries[file_path] = int(lines_str)

    m4_complete = "M4-complete" in rationale_text
    if not m4_complete:
        assert rationale_entries, (
            "giant-file-rationale.md must contain at least one table entry "
            "(or include an M4-complete marker when all files are below threshold)"
        )

    # ── 3. Every file over threshold must be in rationale ───────────────
    over_threshold = {
        rel: lines
        for rel, lines in py_files.items()
        if lines > GIANT_THRESHOLD
    }
    missing_from_rationale = over_threshold.keys() - rationale_entries.keys()
    assert not missing_from_rationale, (
        f"Files over {GIANT_THRESHOLD} lines missing from "
        f"giant-file-rationale.md: {sorted(missing_from_rationale)}"
    )

    # ── 4. Every rationale entry must match an existing over-threshold file ─
    for entry_path, entry_lines in rationale_entries.items():
        assert entry_path in py_files, (
            f"giant-file-rationale.md entry '{entry_path}' does not exist on disk"
        )
        actual_lines = py_files[entry_path]
        assert actual_lines > GIANT_THRESHOLD, (
            f"giant-file-rationale.md entry '{entry_path}' is only "
            f"{actual_lines} lines (threshold: {GIANT_THRESHOLD})"
        )
        # Line count should match within a reasonable tolerance (the doc
        # is a snapshot; a small drift from whitespace changes is acceptable).
        assert abs(actual_lines - entry_lines) <= 50, (
            f"giant-file-rationale.md entry '{entry_path}' claims "
            f"{entry_lines} lines but on-disk count is {actual_lines}"
        )


# ---------------------------------------------------------------------------
# Authority boundaries (m1 plan step 22 / NSA-3): the structure-contract view
# ---------------------------------------------------------------------------


_SCHEMA_PACK_MANIFEST = """\
id: timeline
version: 1
depends_on:
  - core >= 1
migrations:
  - version: 1
    name: initial
    path: migrations/0001_initial.sql
    tables:
      - timelines
stream_types:
  - timeline.timeline
event_kinds:
  - timeline.created
  - timeline.saved
  - timeline.config_replaced
command_kinds:
  - timeline.create
  - timeline.save
  - timeline.replace_config
repositories:
  - TimelineRepository
conformance:
  - replay
cli_mounts:
  timelines: timelines
bridge_mounts:
  - timelines
"""


def _bootstrap_authority_root(root: Path) -> None:
    """Minimal lint-scan root with a valid declared timeline schema pack."""
    _bootstrap_structure_root(root)
    _write(root, "astrid/packs/timeline/__init__.py", "")
    _write(
        root,
        "astrid/packs/timeline/schema-pack.yaml",
        _SCHEMA_PACK_MANIFEST,
    )
    _write(
        root,
        "astrid/packs/timeline/migrations/0001_initial.sql",
        "CREATE TABLE timelines (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  project_id TEXT NOT NULL,\n"
        "  event_stream_id TEXT NOT NULL,\n"
        "  name TEXT NOT NULL,\n"
        "  document_json TEXT NOT NULL,\n"
        "  asset_registry_json TEXT NOT NULL,\n"
        "  created_at TEXT NOT NULL,\n"
        "  updated_at TEXT NOT NULL\n"
        ");\n",
    )


def test_validate_authority_boundaries_flags_every_rule_family(
    tmp_path: Path,
) -> None:
    """Mutation fixtures for imports, writers, legacy authorities, and schema
    ownership are all surfaced by the structure validator's authority gate."""
    _bootstrap_authority_root(tmp_path)
    _write(
        tmp_path,
        "astrid/core/evil.py",
        "from astrid.packs.timeline.repository import TimelineRepository\n",
    )
    _write(
        tmp_path,
        "astrid/core/evil_writer.py",
        "from astrid.core.store.writer import DatabaseWriter\n"
        "writer = DatabaseWriter('/tmp/x.sqlite3', None)\n",
    )
    _write(
        tmp_path,
        "astrid/packs/__init__.py",
        "from astrid.core.timeline.eventlog import LocalFsBackend\n",
    )
    _write(
        tmp_path,
        "astrid/packs/timeline/migrations/0002_bad.sql",
        "CREATE TABLE events (\n"
        "  id TEXT PRIMARY KEY,\n"
        "  FOREIGN KEY (timeline_id) REFERENCES timelines (id)\n"
        ");\n"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);\n",
    )

    errors = structure.validate_authority_boundaries(tmp_path)

    joined = "\n".join(errors)
    assert "astrid/core/evil.py: kernel-to-pack import" in joined
    assert "astrid/core/evil_writer.py: SQLite writer construction outside" in joined
    assert "astrid/packs/__init__.py: legacy authority marker" in joined
    assert "kernel FK from events to pack table 'timelines'" in joined
    assert "forbidden table 'sessions'" in joined


def test_validate_repo_structure_authority_exemptions_stay_green(
    tmp_path: Path,
) -> None:
    """The single composition exemption, legacy files outside the supported
    v10 entry paths, and read-only SQLite probes produce zero authority
    errors inside the full structure report."""
    _bootstrap_authority_root(tmp_path)
    _write(tmp_path, "astrid/__init__.py", "")
    _write(tmp_path, "astrid/core/runtime.py", "")
    _write(tmp_path, "tests/test_example.py", "")
    _write(tmp_path, "scripts/tool.py", "")
    # The one documented kernel-to-pack composition exemption (serve root).
    _write(
        tmp_path,
        "astrid/core/gateway/dispatch.py",
        "import astrid.packs.timeline.cli\n",
    )
    # Legacy files stay in-tree and are never scanned for authority markers.
    _write(
        tmp_path,
        "astrid/core/timeline/legacy_thing.py",
        "from astrid.core.timeline.eventlog import LocalFsBackend\n",
    )
    # Read-only probes are not writers.
    _write(
        tmp_path,
        "astrid/core/reader.py",
        "import sqlite3\n"
        "conn = sqlite3.connect('file:db.sqlite3?mode=ro', uri=True)\n",
    )

    report = validate_repo_structure(tmp_path)

    assert structure.validate_authority_boundaries(tmp_path) == []
    assert report.ok, report.errors


def test_execution_size_guard_detects_required_growth() -> None:
    import ast
    from scripts.reshape.compare_execution_size import HOST, compare

    source = (Path(__file__).resolve().parents[1] / HOST).read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    tree = ast.parse(source)
    host = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "GenericPackHost")
    method = next(n for n in host.body if isinstance(n, ast.FunctionDef) and n.name == "_run_task_impl")
    nested = next(n for n in ast.walk(method) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not method)
    base = {HOST: source}

    def inserted(at: int, text: str) -> str:
        return "".join(lines[:at] + [text] + lines[at:])

    assert any("lines grew" in e for e in compare(base, {HOST: source + "\n"}))
    added = inserted(host.end_lineno, "    def added_guard_method(self): pass\n")
    assert any("added method added_guard_method" in e for e in compare(base, {HOST: added}))
    direct = inserted(method.lineno, " " * (method.col_offset + 4) + "# added\n")
    nested_growth = inserted(nested.lineno, " " * (nested.col_offset + 4) + "# added\n")
    for changed in (direct, nested_growth):
        assert any("_run_task_impl grew" in e for e in compare(base, {HOST: changed}))
    sibling = "astrid/core/execution/new_sibling.py"
    assert any(sibling in e and "1200" in e for e in compare(base, {HOST: source, sibling: "# line\n" * 1300}))
    assert compare(base, {HOST: source.replace("\n\n", "\n", 1)}) == []
    paid = direct.replace("\n\n", "\n", 2)
    assert len(paid.splitlines()) <= len(lines)
    assert any("_run_task_impl grew" in e for e in compare(base, {HOST: paid}))


def test_execution_size_guard_limits_new_functions_and_method_duplicates() -> None:
    from scripts.reshape.compare_execution_size import HOST, compare

    base = {HOST: "class GenericPackHost:\n    def existing(self): pass\n"}
    duplicate = "class GenericPackHost:\n    def existing(self): pass\n    def existing(self): pass\n"
    assert any("added method existing" in e for e in compare(base, {HOST: duplicate}))
    path = "astrid/core/execution/sibling.py"
    long_function = "def long():\n" + "    pass\n" * 200
    assert any("long grew" in e for e in compare(base, {**base, path: long_function}))


def test_execution_size_guard_counts_class_control_flow_and_bindings() -> None:
    from scripts.reshape.compare_execution_size import HOST, compare

    base = {HOST: "class GenericPackHost:\n    def _run_task_impl(self): pass\n\n\n"}
    additions = [
        "    if True:\n        def probe(self): return 42\n",
        "    probe = _run_task_impl\n",
        "    if True:\n        async def probe(self): pass\n",
        "    if True:\n        probe = staticmethod(_run_task_impl)\n",
    ]
    for addition in additions:
        head = {HOST: "class GenericPackHost:\n    def _run_task_impl(self): pass\n" + addition}
        assert len(head[HOST].splitlines()) <= len(base[HOST].splitlines())
        assert any("added method probe" in error for error in compare(base, head))


def test_execution_size_guard_ignores_nested_scope_bindings() -> None:
    from scripts.reshape.compare_execution_size import HOST, compare

    base = {HOST: "class GenericPackHost:\n    def existing(self): pass\n\n\n\n\n"}
    head = {HOST: (
        "class GenericPackHost:\n"
        "    def existing(self):\n"
        "        def probe(): pass\n"
        "        alias = probe\n"
        "    class Inner:\n"
        "        def probe(self): pass\n"
    )}
    assert not any("added method" in error for error in compare(base, head))


def test_execution_size_guard_counts_decorated_function_threshold() -> None:
    from scripts.reshape.compare_execution_size import compare

    path = "astrid/core/execution/sibling.py"
    prefix = "@staticmethod\ndef decorated():\n"
    assert compare({}, {path: prefix + "    pass\n" * 198}) == []
    errors = compare({}, {path: prefix + "    pass\n" * 199})
    assert any("decorated grew from 0 to 201 lines" in error for error in errors)


def test_execution_size_guard_counts_multiline_decorators_and_nested_async() -> None:
    from scripts.reshape.compare_execution_size import _functions

    source = (
        "def outer():\n"
        "    @(\n"
        "        decorator\n"
        "    )\n"
        "    async def inner():\n"
        "        pass\n"
    )
    functions, _ = _functions(source)
    assert functions["outer"] == [6]
    assert functions["outer.inner"] == [5]


def test_execution_size_guard_counts_only_trailing_suite_comments() -> None:
    from scripts.reshape.compare_execution_size import _functions

    source = (
        "def first():\n"
        "    text = '''\n"
        "    # part of the string\n"
        "    '''\n"
        "    # trailing suite comment\n"
        "# peer comment\n"
        "def sibling():\n"
        "    pass\n"
        "    # sibling comment\n"
    )
    functions, _ = _functions(source)
    assert functions["first"] == [5]
    assert functions["sibling"] == [3]
