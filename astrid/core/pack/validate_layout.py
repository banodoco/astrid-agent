"""Pack layout contract validation.

Extracted from ``astrid.core.pack.validate``.
Provides layout exception types, canonical layout rules, and standalone
validation functions that PackValidator delegates to.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


class LayoutExceptionClass(str, Enum):
    """Named exceptions the pack layout contract allows temporarily or permanently."""

    IMPORTABLE_COMPONENT_CODE = "importable_component_code"
    DOMAIN_EXCEPTION = "domain_exception"
    GENERATED_IGNORED = "generated_ignored"
    SKILL_ONLY_SHELL = "skill_only_shell"


class LayoutExceptionLifecycle(str, Enum):
    """Lifecycle for declared layout exceptions."""

    M1 = "M1"
    M2 = "M2"
    PERMANENT = "permanent"


_TEMPORARY_LAYOUT_EXCEPTION_CLASSES: set[LayoutExceptionClass] = set()

_LAYOUT_EXCEPTION_DECLARATION_PATH = "metadata.layout.exceptions"
_CANONICAL_PACK_ROOT_DIRS = {
    "build",
    "docs",
    "elements",
    "examples",
    "executors",
    "fixtures",
    "golden",
    "orchestrators",
    "schemas",
    "skill",
}
_CANONICAL_COMPONENT_SUPPORT_FILES = {"STAGE.md"}
_CANONICAL_COMPONENT_SUPPORT_DIRS = {"skill"}
_V3_ROLE_ROOTS = {"actions", "ui", "rendering", "shared"}
_V3_SUPPORT_ROOTS = {"build", "docs", "examples", "fixtures", "golden", "schemas"}


@dataclass(frozen=True)
class CanonicalLayoutRule:
    """Agent-readable description of a canonical pack path family."""

    pattern: str
    description: str


@dataclass(frozen=True)
class PackLayoutException:
    """One declared exception to the canonical pack layout contract."""

    path: str
    exception_class: LayoutExceptionClass
    reason: str
    defer_to: LayoutExceptionLifecycle = LayoutExceptionLifecycle.PERMANENT

    @property
    def is_temporary(self) -> bool:
        return self.exception_class in _TEMPORARY_LAYOUT_EXCEPTION_CLASSES


@dataclass(frozen=True)
class LayoutValidationIssue:
    """One layout-contract problem surfaced under the aggregate failure heading."""

    path: str
    message: str


CANONICAL_PACK_LAYOUT_RULES: tuple[CanonicalLayoutRule, ...] = (
    CanonicalLayoutRule("pack.yaml|pack.yml|pack.json", "pack manifest at the pack root"),
    CanonicalLayoutRule("actions/<name>/...", "callable action implementation directories"),
    CanonicalLayoutRule("ui/<name>/...", "existing-host UI contribution directories"),
    CanonicalLayoutRule("rendering/<name>/...", "renderer, planner, finalizer, or element directories"),
    CanonicalLayoutRule("shared/...", "explicitly shared support files"),
    CanonicalLayoutRule("docs/SKILL.md", "the one authored pack skill, when declared"),
    CanonicalLayoutRule("skill/SKILL.md", "optional pack-level skill guidance"),
    CanonicalLayoutRule("executors/<name>/...", "executor capability directories"),
    CanonicalLayoutRule("orchestrators/<name>/...", "orchestrator capability directories"),
    CanonicalLayoutRule("elements/<kind>/<name>/...", "element capability directories"),
    CanonicalLayoutRule("fixtures/...|golden/...", "optional checked-in fixture data"),
    CanonicalLayoutRule("build/...", "generated build output when explicitly classified"),
    CanonicalLayoutRule("docs/...|examples/...|schemas/...", "declared supporting assets"),
)


# ---------------------------------------------------------------------------
# Special non-manifest directories (classified in this contract)
# ---------------------------------------------------------------------------
# These directories under ``astrid/packs/`` are not regular packs (they
# lack a ``pack.yaml``) but serve documented roles.  They are listed here
# so the layout contract remains explicit about every known deviation.
#
#   _core/
#       class:       skill_only_shell
#       defer_to:    permanent (not a pack — skill documentation surface)
#       reason:      Contains only ``docs/SKILL.md`` providing the root
#                    Astrid skill description for agent harnesses.  No
#                    ``pack.yaml``, executors, or orchestrators exist.
#                    Discovered via ``astrid.skills.discovery``, not via
#                    ``astrid.core.pack.discover_packs``.
#
# ---------------------------------------------------------------------------
# Relocated / removed directories (validated by contract tests)
# ---------------------------------------------------------------------------
#   astrid/packs/schemas/
#       status:      relocated to astrid/core/pack/schemas/
#       contract:    ``test_schemas_directory_absent_from_pack_tree`` in
#                    ``tests/test_pack_layout_contract.py`` enforces
#                    continued absence.
#   astrid/core/pack/schemas/
#       status:      relocated to astrid/core/pack/schemas/
#       contract:    ``test_schemas_relocated_to_pack`` in
#                    ``tests/test_pack_layout_contract.py`` enforces
#                    presence at the new canonical location.


def is_canonical_pack_path(relpath: str) -> bool:
    """Return True when *relpath* matches a known canonical pack layout entry."""
    normalized = PurePosixPath(relpath)
    if normalized.name in {"pack.yaml", "pack.yml", "pack.json"} and len(normalized.parts) == 1:
        return True
    parts = normalized.parts
    if len(parts) == 2 and parts[0] == "skill" and parts[1] == "SKILL.md":
        return True
    if not parts:
        return False
    if parts[0] in _V3_ROLE_ROOTS | _V3_SUPPORT_ROOTS:
        return len(parts) >= 2
    if parts[0] in {"fixtures", "golden", "build", "docs", "examples", "schemas"}:
        return True
    if len(parts) >= 2 and parts[0] in {"executors", "orchestrators"}:
        if len(parts) == 3 and parts[2] in {
            f"{parts[0][:-1]}.yaml",
            f"{parts[0][:-1]}.yml",
            f"{parts[0][:-1]}.json",
            "run.py",
        }:
            return True
        if len(parts) == 3 and parts[2] in _CANONICAL_COMPONENT_SUPPORT_FILES:
            return True
        if len(parts) == 4 and parts[2] in _CANONICAL_COMPONENT_SUPPORT_DIRS and parts[3] == "SKILL.md":
            return True
        return False
    if len(parts) >= 3 and parts[0] == "elements":
        if len(parts) == 4 and parts[3] in {"element.yaml", "element.yml", "element.json"}:
            return True
        return False
    if parts[0] in _CANONICAL_PACK_ROOT_DIRS:
        return True
    return False


def validate_pack_layout(
    pack_root: Path,
    pack_data: dict[str, Any],
) -> list[LayoutValidationIssue]:
    """Validate v3-only physical layout invariants.

    Canonical admission validates every declared path. This function checks
    only pack-wide skill authority, leaving ordinary supporting files free to
    live beside their owning declaration. It does not require any role folder
    to exist, so action/UI/rendering-only packs need no placeholder folders.
    """
    if pack_data.get("schema_version") != 3:
        return []

    issues: list[LayoutValidationIssue] = []
    skill_paths = sorted(
        path.relative_to(pack_root).as_posix()
        for path in pack_root.rglob("SKILL.md")
        if not path.is_symlink()
    )
    documentation = pack_data.get("documentation")
    declared_skill = (
        isinstance(documentation, dict)
        and documentation.get("kind") == "skill"
        and documentation.get("path") == "docs/SKILL.md"
    )
    for path in skill_paths:
        if path != "docs/SKILL.md" or not declared_skill:
            issues.append(
                LayoutValidationIssue(
                    path,
                    "v3 packs allow one authored skill at docs/SKILL.md declared by pack.yaml; "
                    "nested or legacy SKILL.md files are not separate metadata authorities",
                )
            )
    return issues


def parse_layout_exceptions(
    pack_data: dict[str, Any],
) -> tuple[list[PackLayoutException], list[LayoutValidationIssue]]:
    """Parse and validate layout exceptions from pack metadata.

    Returns (parsed_exceptions, issues).  Issues are validation problems
    encountered during parsing; the caller should surface them as errors.
    """
    issues: list[LayoutValidationIssue] = []

    metadata = pack_data.get("metadata")
    if metadata is None:
        return [], issues
    if not isinstance(metadata, dict):
        issues.append(
            LayoutValidationIssue(
                "pack.yaml",
                "metadata must be an object before layout exceptions can be parsed",
            )
        )
        return [], issues

    layout = metadata.get("layout")
    if layout is None:
        return [], issues
    if not isinstance(layout, dict):
        issues.append(
            LayoutValidationIssue("pack.yaml", "metadata.layout must be an object")
        )
        return [], issues

    declarations = layout.get("exceptions")
    if declarations is None:
        return [], issues
    if not isinstance(declarations, list):
        issues.append(
            LayoutValidationIssue(
                "pack.yaml",
                f"{_LAYOUT_EXCEPTION_DECLARATION_PATH} must be an array",
            )
        )
        return [], issues

    parsed: list[PackLayoutException] = []
    seen_paths: dict[str, int] = {}
    for index, raw in enumerate(declarations):
        item_path = f"{_LAYOUT_EXCEPTION_DECLARATION_PATH}[{index}]"
        parsed_decl, decl_issues = _parse_layout_exception_declaration(raw, item_path)
        issues.extend(decl_issues)
        if parsed_decl is None:
            continue
        first_index = seen_paths.get(parsed_decl.path)
        if first_index is not None:
            issues.append(
                LayoutValidationIssue(
                    "pack.yaml",
                    f"{item_path}.path duplicates {_LAYOUT_EXCEPTION_DECLARATION_PATH}[{first_index}].path ({parsed_decl.path!r})",
                )
            )
            continue
        seen_paths[parsed_decl.path] = index
        if is_canonical_pack_path(parsed_decl.path):
            issues.append(
                LayoutValidationIssue(
                    parsed_decl.path,
                    "declared as a layout exception even though the path already matches the canonical pack layout",
                )
            )
            continue
        parsed.append(parsed_decl)
    return parsed, issues


def _parse_layout_exception_declaration(
    raw: Any,
    item_path: str,
) -> tuple[PackLayoutException | None, list[LayoutValidationIssue]]:
    """Parse one layout exception declaration dict.

    Returns (parsed_exception_or_None, issues).
    """
    issues: list[LayoutValidationIssue] = []

    if not isinstance(raw, dict):
        issues.append(
            LayoutValidationIssue("pack.yaml", f"{item_path} must be an object")
        )
        return None, issues

    allowed_fields = {"path", "class", "reason", "defer_to"}
    extra_fields = sorted(set(raw) - allowed_fields)
    if extra_fields:
        fields = ", ".join(extra_fields)
        issues.append(
            LayoutValidationIssue(
                "pack.yaml",
                f"{item_path} has unknown field(s): {fields}",
            )
        )
        return None, issues

    path_value = raw.get("path")
    normalized_path, path_issues = _normalize_layout_exception_path(path_value, item_path)
    issues.extend(path_issues)
    if normalized_path is None:
        return None, issues

    class_value = raw.get("class")
    try:
        exception_class = LayoutExceptionClass(str(class_value))
    except Exception:
        valid = ", ".join(member.value for member in LayoutExceptionClass)
        issues.append(
            LayoutValidationIssue(
                normalized_path,
                f"{item_path}.class must be one of [{valid}]",
            )
        )
        return None, issues

    reason_value = raw.get("reason")
    if not isinstance(reason_value, str) or not reason_value.strip():
        issues.append(
            LayoutValidationIssue(
                normalized_path,
                f"{item_path}.reason must be a non-empty string",
            )
        )
        return None, issues

    lifecycle, lc_issues = _parse_layout_lifecycle(
        raw.get("defer_to"),
        item_path=item_path,
        path=normalized_path,
        exception_class=exception_class,
    )
    issues.extend(lc_issues)
    if lifecycle is None:
        return None, issues

    return PackLayoutException(
        path=normalized_path,
        exception_class=exception_class,
        reason=reason_value.strip(),
        defer_to=lifecycle,
    ), issues


def _parse_layout_lifecycle(
    raw_lifecycle: Any,
    *,
    item_path: str,
    path: str,
    exception_class: LayoutExceptionClass,
) -> tuple[LayoutExceptionLifecycle | None, list[LayoutValidationIssue]]:
    """Parse and validate a layout exception's defer_to / lifecycle field.

    Returns (lifecycle_or_None, issues).
    """
    issues: list[LayoutValidationIssue] = []

    if raw_lifecycle is None:
        if exception_class in _TEMPORARY_LAYOUT_EXCEPTION_CLASSES:
            issues.append(
                LayoutValidationIssue(
                    path,
                    f"{item_path}.defer_to is required for temporary exception class {exception_class.value!r}",
                )
            )
            return None, issues
        return LayoutExceptionLifecycle.PERMANENT, issues

    try:
        lifecycle = LayoutExceptionLifecycle(str(raw_lifecycle))
    except Exception:
        valid = ", ".join(member.value for member in LayoutExceptionLifecycle)
        issues.append(
            LayoutValidationIssue(
                path,
                f"{item_path}.defer_to must be one of [{valid}]",
            )
        )
        return None, issues

    if exception_class in _TEMPORARY_LAYOUT_EXCEPTION_CLASSES:
        if lifecycle == LayoutExceptionLifecycle.PERMANENT:
            issues.append(
                LayoutValidationIssue(
                    path,
                    f"{item_path}.defer_to must be M1 or M2 for temporary exception class {exception_class.value!r}",
                )
            )
            return None, issues
        return lifecycle, issues

    if lifecycle != LayoutExceptionLifecycle.PERMANENT:
        issues.append(
            LayoutValidationIssue(
                path,
                f"{item_path}.defer_to must be permanent for non-temporary exception class {exception_class.value!r}",
            )
        )
        return None, issues
    return lifecycle, issues


def _normalize_layout_exception_path(
    raw_path: Any,
    item_path: str,
) -> tuple[str | None, list[LayoutValidationIssue]]:
    """Normalize and validate a layout exception path.

    Returns (normalized_path_or_None, issues).
    """
    issues: list[LayoutValidationIssue] = []

    if not isinstance(raw_path, str) or not raw_path.strip():
        issues.append(
            LayoutValidationIssue(
                "pack.yaml",
                f"{item_path}.path must be a non-empty relative path",
            )
        )
        return None, issues
    try:
        normalized = PurePosixPath(raw_path.strip())
    except Exception:
        issues.append(
            LayoutValidationIssue(
                "pack.yaml",
                f"{item_path}.path must be a valid relative path",
            )
        )
        return None, issues
    if (
        PureWindowsPath(raw_path.strip()).drive
        or normalized.is_absolute()
        or ".." in normalized.parts
        or "." in normalized.parts
    ):
        issues.append(
            LayoutValidationIssue(
                "pack.yaml",
                f"{item_path}.path must stay within the pack root",
            )
        )
        return None, issues
    if str(normalized) in {"", "."}:
        issues.append(
            LayoutValidationIssue(
                "pack.yaml",
                f"{item_path}.path must be a non-empty relative path",
            )
        )
        return None, issues
    return str(normalized), issues


__all__ = [
    "CANONICAL_PACK_LAYOUT_RULES",
    "CanonicalLayoutRule",
    "LayoutExceptionClass",
    "LayoutExceptionLifecycle",
    "LayoutValidationIssue",
    "PackLayoutException",
    "is_canonical_pack_path",
    "parse_layout_exceptions",
    "validate_pack_layout",
]
