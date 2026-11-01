"""Pack layout contract hygiene tests.

Covers:
- No generated artifacts (``__pycache__/``, ``.pyc``, ``.DS_Store``) in the pack tree
- No build artifacts in ``astrid/packs/*/build/``
- ``.gitignore`` rules are in place for all required ignore patterns
- Special non-manifest directory classification (``_core`` as ``skill_only_shell``)
- Pack discovery and shipped pack ID stability
- Skill discovery coverage
- Relocated/removed directory enforcement (``schemas/`` absent)
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

from astrid.core.pack import discover_packs
from astrid.core.pack.validate_first_party import (
    _FIRST_PARTY_PACK_IDS,
    _TRACKED_FIRST_PARTY_PACK_IDS,
)

# ── repo-root-relative paths ────────────────────────────────────────────────

_PACKS_ROOT = Path(__file__).resolve().parents[2] / "astrid" / "packs"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_GITIGNORE_PATH = _REPO_ROOT / ".gitignore"
_REMOVED_DATA_ONLY_PACKAGE_MARKERS = (
    "astrid/packs/_core/__init__.py",
    "astrid/packs/builtin/__init__.py",
    "astrid/packs/builtin/executors/__init__.py",
    "astrid/packs/builtin/orchestrators/__init__.py",
    "astrid/packs/iteration/__init__.py",
    "astrid/packs/iteration/executors/__init__.py",
    "astrid/packs/media/executors/__init__.py",
    "astrid/packs/media/executors/clip_extract/__init__.py",
)
_PRESERVED_HELPER_PACKAGE_MARKERS = (
    "astrid/packs/runpod/actions/session/__init__.py",
    "astrid/packs/training/actions/training_run/trainer_adapters/__init__.py",
    "astrid/packs/editorial/actions/refine/src/reviewers/__init__.py",
)
_REMOVED_MARKER_IMPORT_PATHS = (
    "astrid.packs.media.actions.clip_extract.run",
)

# ── known shipped pack ids (must remain stable across layout changes) ───────
# Every immediate subdirectory of astrid/packs/ that contains a pack
# manifest is a "shipped pack".  The single source of truth is the first-party
# validator's inventory; the tracked subset excludes the local-only packs that
# may be gitignored in a given checkout.
_SHIPPED_PACK_IDS = frozenset(_FIRST_PARTY_PACK_IDS)
_TRACKED_SHIPPED_PACK_IDS = frozenset(_TRACKED_FIRST_PARTY_PACK_IDS)


# ── helpers ──────────────────────────────────────────────────────────────────


def _packs_with_pack_yaml() -> list[Path]:
    """Return every immediate subdirectory of ``astrid/packs/`` that contains
    a ``pack.yaml``, ``pack.yml``, or ``pack.json``."""
    packs: list[Path] = []
    if not _PACKS_ROOT.is_dir():
        return packs
    for child in sorted(_PACKS_ROOT.iterdir()):
        if not child.is_dir():
            continue
        for manifest_name in ("pack.yaml", "pack.yml", "pack.json"):
            if (child / manifest_name).is_file():
                packs.append(child)
                break
    return packs


def _walk_pack_tree(pack_root: Path) -> list[Path]:
    """Return every file and directory path under *pack_root* (recursive)."""
    return list(pack_root.rglob("*"))


def _tracked_pack_paths() -> list[Path]:
    """Return tracked repository paths under ``astrid/packs/``.

    The hygiene contract is about committed source shape. Other tests may
    legitimately create ignored runtime byproducts such as ``__pycache__/`` or
    pack ``build/`` files during the same pytest process.
    """
    result = subprocess.run(
        ["git", "ls-files", "astrid/packs"],
        check=True,
        capture_output=True,
        cwd=_REPO_ROOT,
        text=True,
    )
    return [Path(line) for line in result.stdout.splitlines() if line]


# ── generated-artifact hygiene ───────────────────────────────────────────────


def test_no_pycache_dirs_in_packs() -> None:
    """No tracked path may exist inside a pack ``__pycache__/`` directory."""
    offending = [
        path.as_posix()
        for path in _tracked_pack_paths()
        if "__pycache__" in path.parts
    ]
    assert not offending, (
        f"__pycache__/ directories found in pack tree: {offending}"
    )


def test_no_pyc_files_in_packs() -> None:
    """No tracked ``.pyc`` file may exist inside any pack directory."""
    offending = [
        path.as_posix()
        for path in _tracked_pack_paths()
        if path.suffix == ".pyc"
    ]
    assert not offending, (
        f".pyc files found in pack tree: {offending}"
    )


def test_no_ds_store_in_packs() -> None:
    """No tracked ``.DS_Store`` file may exist inside any pack directory."""
    offending = [
        path.as_posix()
        for path in _tracked_pack_paths()
        if path.name == ".DS_Store"
    ]
    assert not offending, (
        f".DS_Store files found in pack tree: {offending}"
    )


def test_no_build_artifacts_in_packs() -> None:
    """No tracked build/ directory artifacts may exist inside pack dirs."""
    offending = [
        path.as_posix()
        for path in _tracked_pack_paths()
        if len(path.parts) >= 4 and path.parts[0:2] == ("astrid", "packs") and "build" in path.parts[3:]
    ]
    assert not offending, (
        f"Build artifacts found in pack build/ directories: {offending}"
    )


# ── .gitignore rule verification ─────────────────────────────────────────────


def test_gitignore_covers_pycache() -> None:
    """``.gitignore`` must contain a ``__pycache__/`` rule."""
    _assert_gitignore_contains("__pycache__/")


def test_gitignore_covers_ds_store() -> None:
    """``.gitignore`` must contain a ``.DS_Store`` rule."""
    _assert_gitignore_contains(".DS_Store")


def test_gitignore_covers_pack_build_dirs() -> None:
    """``.gitignore`` must contain ``astrid/packs/*/build/`` rule."""
    _assert_gitignore_contains("astrid/packs/*/build/")


def test_gitignore_covers_example_pack_build_dirs() -> None:
    """``.gitignore`` must contain ``examples/packs/*/build/`` rule."""
    _assert_gitignore_contains("examples/packs/*/build/")


def _assert_gitignore_contains(pattern: str) -> None:
    """Assert *pattern* appears as a line in ``.gitignore``."""
    assert _GITIGNORE_PATH.is_file(), ".gitignore not found"
    lines = _GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    assert pattern in lines, (
        f".gitignore must contain line {pattern!r}; "
        f"got {len(lines)} lines"
    )


def test_selected_data_only_package_markers_are_removed() -> None:
    """Known-safe data-only package markers should be absent from the pack tree."""
    offending = [
        path for path in _REMOVED_DATA_ONLY_PACKAGE_MARKERS
        if (_REPO_ROOT / path).exists()
    ]
    assert not offending, (
        f"Expected safe data-only package markers to be removed: {offending}"
    )


def test_helper_package_markers_still_exist() -> None:
    """Helper packages that support imports must keep their ``__init__.py``."""
    missing = [
        path for path in _PRESERVED_HELPER_PACKAGE_MARKERS
        if not (_REPO_ROOT / path).is_file()
    ]
    assert not missing, (
        f"Helper package markers were removed but must remain importable: {missing}"
    )


def test_removed_package_markers_do_not_break_runtime_imports() -> None:
    """Namespace-packaged pack paths must still import after marker cleanup."""
    for module_name in _REMOVED_MARKER_IMPORT_PATHS:
        module = importlib.import_module(module_name)
        assert module is not None


# ── T15: special directory classification & discovery stability ──────────────


def test_core_is_skill_only_shell_no_pack_manifest() -> None:
    """``_core`` is a ``skill_only_shell``: it must NOT contain any pack
    manifest (``pack.yaml``, ``pack.yml``, or ``pack.json``)."""
    core_root = _PACKS_ROOT / "_core"
    assert core_root.is_dir(), f"_core directory missing at {core_root}"
    for manifest_name in ("pack.yaml", "pack.yml", "pack.json"):
        assert not (core_root / manifest_name).is_file(), (
            f"_core is classified as skill_only_shell but contains "
            f"{manifest_name}"
        )


def test_core_has_skill_md() -> None:
    """``_core`` skill_only_shell must provide ``docs/SKILL.md`` (the one
    authored skill in the v3 layout) for agent harness skill discovery."""
    skill_md = _PACKS_ROOT / "_core" / "docs" / "SKILL.md"
    assert skill_md.is_file(), (
        f"_core is classified as skill_only_shell but is missing "
        f"docs/SKILL.md at {skill_md}"
    )


def test_core_only_contains_skill_directory() -> None:
    """``_core`` skill_only_shell must contain *only* skill content: the
    ``docs/`` authored skill (no actions, rendering, executors,
    orchestrators, elements, or build directories)."""
    core_root = _PACKS_ROOT / "_core"
    forbidden_dirs = {
        "actions", "rendering", "executors", "orchestrators", "elements", "build",
    }
    found: list[str] = []
    for child in core_root.iterdir():
        if child.is_dir() and child.name in forbidden_dirs:
            found.append(str(child.relative_to(_REPO_ROOT)))
    assert not found, (
        f"_core (skill_only_shell) contains forbidden content roots: {found}"
    )
    # Verify that only the docs/ directory exists as a top-level child.
    actual_dirs = sorted(
        child.name for child in core_root.iterdir() if child.is_dir()
    )
    assert actual_dirs == ["docs"], (
        f"_core (skill_only_shell) expected only the 'docs/' directory; "
        f"got {actual_dirs}"
    )


def test_schemas_directory_absent_from_pack_tree() -> None:
    """``astrid/packs/schemas/`` was relocated to
    ``astrid/core/pack/schemas/`` in M2 T10 and must remain absent
    from the pack tree."""
    schemas_in_packs = _PACKS_ROOT / "schemas"
    assert not schemas_in_packs.exists(), (
        f"astrid/packs/schemas/ should have been relocated to "
        f"astrid/core/pack/schemas/ but still exists at "
        f"{schemas_in_packs}"
    )


def test_schemas_relocated_to_pack() -> None:
    """The relocated schemas must exist under
    ``astrid/core/pack/schemas/v1/`` (M2 T10)."""
    pack_schemas = (
        _REPO_ROOT / "astrid" / "core" / "pack" / "schemas" / "v1"
    )
    assert pack_schemas.is_dir(), (
        f"Schemas not found at expected pack location {pack_schemas}"
    )
    # At minimum, the five shared v1 manifest schemas must be present.
    required_schemas = (
        "_defs.json",
        "element.json",
        "executor.json",
        "orchestrator.json",
        "pack.json",
    )
    for schema_file in required_schemas:
        assert (pack_schemas / schema_file).is_file(), (
            f"Missing relocated schema file: {schema_file} in {pack_schemas}"
        )


def test_all_shipped_packs_discoverable() -> None:
    """Every shipped pack (directory containing a pack manifest) must be
    discoverable via ``astrid.core.pack.discover_packs``."""
    discovered = discover_packs(_PACKS_ROOT)
    discovered_ids = {pack.id for pack in discovered}
    # All shipped pack ids must appear.
    missing = _TRACKED_SHIPPED_PACK_IDS - discovered_ids
    assert not missing, (
        f"Shipped pack ids not discovered by discover_packs: {sorted(missing)}"
    )


def test_no_unexpected_pack_ids_ship() -> None:
    """No pack ids beyond the known shipped set should appear under
    ``astrid/packs/``.  Unexpected packs indicate unclassified additions."""
    discovered = [
        pack
        for pack in discover_packs(_PACKS_ROOT)
        if not _is_gitignored_repo_path(pack.root)
    ]
    discovered_ids = {pack.id for pack in discovered}
    unexpected = discovered_ids - _SHIPPED_PACK_IDS
    assert not unexpected, (
        f"Unexpected pack ids discovered under astrid/packs/ "
        f"(must be classified as shipped or special): {sorted(unexpected)}"
    )


def _is_gitignored_repo_path(path: Path) -> bool:
    try:
        rel = path.resolve().relative_to(_REPO_ROOT)
    except ValueError:
        return False
    try:
        subprocess.run(
            ["git", "check-ignore", "-q", "--", rel.as_posix()],
            check=True,
            capture_output=True,
            cwd=_REPO_ROOT,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def test_skill_discovery_finds_core_shell() -> None:
    """``astrid.skills.discovery.list_skills()`` must discover the ``_core``
    skill_only_shell via its ``skill/SKILL.md``."""
    from astrid.skills import discovery as skill_discovery

    descriptors = skill_discovery.list_skills()
    # _core's SKILL.md has name: astrid
    core_names = {d.name for d in descriptors if d.pack_id == "_core"}
    assert "astrid" in core_names, (
        f"skill discovery did not find _core skill; "
        f"got pack_ids: {sorted({d.pack_id for d in descriptors})}"
    )


def test_skill_discovery_finds_pack_skills() -> None:
    """``astrid.skills.discovery.list_skills()`` must discover at least the
    packs that have ``skill/SKILL.md`` and a pack manifest."""
    from astrid.skills import discovery as skill_discovery

    # Packs known to have skill/SKILL.md (not exhaustive — just a floor).
    expected_with_skill = {
        "editorial",
        "foley",
        "generation",
        "media",
        "rendering",
        "stream_content",
        "understanding",
        "video_editing",
    }
    descriptors = skill_discovery.list_skills()
    discovered_pack_ids = {d.pack_id for d in descriptors}
    missing = expected_with_skill - discovered_pack_ids
    assert not missing, (
        f"Pack skills not discovered: {missing}. "
        f"Discovered pack_ids: {sorted(discovered_pack_ids)}"
    )


def test_v2_integration_packs_are_discoverable() -> None:
    """Former external-origin packs remain discoverable after v3 conversion."""
    integration_ids = {
        "fal",
        "moirae",
        "runpod",
        "vibecomfy",
        "comfy_wrap",
    }
    discovered = discover_packs(_PACKS_ROOT)
    discovered_ids = {pack.id for pack in discovered}
    missing = integration_ids - discovered_ids
    assert not missing, (
        f"Integration packs not discovered: {sorted(missing)}"
    )
    # Strict v2/v3 intentionally remove the retired origin field.
    import yaml as _yaml
    for pack in discovered:
        if pack.id in integration_ids:
            manifest_path = pack.manifest_path
            data = _yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
            assert data["schema_version"] == 3, (
                f"{pack.id}: unsupported schema_version {data['schema_version']!r}"
            )
            assert "origin" not in data
