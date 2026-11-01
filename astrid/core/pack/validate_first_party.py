"""First-party packs root validation.

Extracted from ``astrid.core.pack.validate``.
Validates the ``astrid/packs/`` source tree structure and inventory.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

import yaml

# ---------------------------------------------------------------------------
# Known first-party pack IDs and internal directories
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIRST_PARTY_PACKS_ROOT = _REPO_ROOT / "astrid" / "packs"
_FIRST_PARTY_PACK_IDS = (
    "astrid_motion",
    "blender",
    "capture",
    "chiptune",
    "comfy_wrap",
    "discord_local",
    "editorial",
    "fal",
    "foley",
    "generation",
    "h3_av",
    "iteration",
    "local",
    "media",
    "moirae",
    "music",
    "pixel",
    "rendering",
    "runpod",
    "seedance_local",
    "stream_content",
    "training",
    "understanding",
    "vibecomfy",
    "video_editing",
    "wan2gp",
    "youtube",
    "typed_timeline",
)
_TRACKED_FIRST_PARTY_PACK_IDS = tuple(
    pack_id for pack_id in _FIRST_PARTY_PACK_IDS
    if pack_id not in {"discord_local", "seedance_local"}
)
# ``_core`` is a skill-only shell. The three runtime product mount directories
# retain executable CLI adapters but intentionally have no ``pack.yaml``:
# their durable state belongs to the neutral workspace runtime, not a local
# schema host.
_FIRST_PARTY_INTERNAL_DIRS = {"_core", "references", "shots", "timeline"}
_EXCLUDED_FIRST_PARTY_DIRS = {"external", "hivemind"}
_IGNORED_PACKS_ROOT_DIRS = {"__pycache__"}

ValidationContext = Literal["standalone", "assembled"]


@dataclass(frozen=True)
class PackRootValidation:
    """Static result for one explicit pack root in an aggregate check."""

    path: Path
    pack_id: str | None
    status: str
    visibility: str
    support: str
    documentation: str
    documentation_path: str | None
    skill_name: str | None
    skill_description: str | None
    skill_exports: tuple[str, ...]
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def active(self) -> bool:
        return self.status != "deprecated" and self.visibility != "hidden"


@dataclass(frozen=True)
class PackValidationReport:
    """Aggregate output shared by standalone and assembled CLI validation."""

    context: ValidationContext
    root: Path | None
    results: tuple[PackRootValidation, ...]
    inventory_errors: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    runtime_mounts: tuple[str, ...] = ()

    @property
    def errors(self) -> list[str]:
        errors = [f"[internal-inventory] {error}" for error in self.inventory_errors]
        seen_ids: dict[str, Path] = {}
        for result in self.results:
            label = result.pack_id or str(result.path)
            for error in result.errors:
                errors.append(f"[layout] {label}: {error}")
            if result.pack_id is not None and result.active:
                previous = seen_ids.setdefault(result.pack_id, result.path)
                if previous != result.path:
                    errors.append(
                        f"[catalog] duplicate active pack id {result.pack_id!r}: "
                        f"{previous} and {result.path}"
                    )
            if len(result.skill_exports) > 1 and not any(
                "one authored skill" in error for error in result.errors
            ):
                exports = ", ".join(result.skill_exports)
                errors.append(
                    f"[skills] {label}: multiple skill exports are not allowed: {exports}"
                )
        return errors

    @property
    def warnings(self) -> list[str]:
        return [
            f"{result.pack_id or result.path}: {warning}"
            for result in self.results
            for warning in result.warnings
        ]

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def counts(self) -> dict[str, int]:
        documentation = Counter(result.documentation for result in self.results)
        active = sum(result.active for result in self.results)
        return {
            "roots": len(self.results),
            "active": active,
            "deprecated": sum(result.status == "deprecated" for result in self.results),
            "hidden": sum(result.visibility == "hidden" for result in self.results),
            "valid": sum(not result.errors for result in self.results),
            "invalid": sum(bool(result.errors) for result in self.results),
            "skill": documentation.get("skill", 0),
            "agents": documentation.get("agents", 0),
            "none": documentation.get("none", 0),
            "undocumented": documentation.get("undocumented", 0),
            "skill_exports": sum(len(result.skill_exports) for result in self.results),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "path": str(self.root) if self.root is not None else None,
            "valid": self.valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "counts": self.counts,
            "roots": [
                {
                    "path": str(result.path),
                    "id": result.pack_id,
                    "status": result.status,
                    "visibility": result.visibility,
                    "support": result.support,
                    "active": result.active,
                    "documentation": result.documentation,
                    "documentation_path": result.documentation_path,
                    "skill_name": result.skill_name,
                    "skill_description": result.skill_description,
                    "skill_exports": list(result.skill_exports),
                    "valid": not result.errors,
                    "errors": list(result.errors),
                    "warnings": list(result.warnings),
                }
                for result in self.results
            ],
            "dispositions": {
                "excluded": list(self.excluded),
                "runtime_mounts": list(self.runtime_mounts),
            },
        }


def is_first_party_packs_root_candidate(path: str | Path) -> bool:
    """Return True when *path* looks like Astrid's multi-pack source root."""
    root = Path(path).resolve()
    if not root.is_dir():
        return False

    # Late import to avoid circular dependency at module level.
    from astrid.core.pack import pack_manifest_path

    if pack_manifest_path(root) is not None:
        return False
    recognized_dirs = {
        child.name
        for child in root.iterdir()
        if child.is_dir()
        and not child.name.startswith(".")
        and child.name not in _IGNORED_PACKS_ROOT_DIRS
        and child.name in set(_FIRST_PARTY_PACK_IDS) | _FIRST_PARTY_INTERNAL_DIRS
    }
    if root == _FIRST_PARTY_PACKS_ROOT:
        return True
    return "_core" in recognized_dirs and any(
        (child / "pack.yaml").is_file()
        for child in root.iterdir()
        if child.is_dir() and child.name not in _IGNORED_PACKS_ROOT_DIRS
    )


def validate_pack_roots(
    pack_roots: Iterable[str | Path],
    *,
    context: ValidationContext = "standalone",
    root: str | Path | None = None,
) -> PackValidationReport:
    """Validate an explicit set of pack roots with the author validator.

    The caller supplies the root set. This keeps source inventory ownership
    with P01/discovery while reusing :func:`validate_pack` as the only pack
    validator. ``context`` is intentionally retained in the result so a
    standalone structural check cannot be mistaken for an assembled catalog
    check.
    """
    if context not in {"standalone", "assembled"}:
        raise ValueError(f"unknown pack validation context: {context!r}")

    # Late import keeps the validator's existing split free of a module cycle.
    from astrid.core.pack.validate import validate_pack

    results: list[PackRootValidation] = []
    for raw_path in pack_roots:
        path = Path(raw_path).expanduser().resolve()
        metadata = _read_pack_metadata(path)
        errors, warnings = validate_pack(path)
        if metadata["error"] is not None and metadata["error"] not in errors:
            errors = [metadata["error"], *errors]
        results.append(
            PackRootValidation(
                path=path,
                pack_id=metadata["id"],
                status=metadata["status"],
                visibility=metadata["visibility"],
                support=metadata["support"],
                documentation=metadata["documentation"],
                documentation_path=metadata["documentation_path"],
                skill_name=metadata["skill_name"],
                skill_description=metadata["skill_description"],
                skill_exports=metadata["skill_exports"],
                errors=tuple(errors),
                warnings=tuple(warnings),
            )
        )
    return PackValidationReport(
        context=context,
        root=Path(root).expanduser().resolve() if root is not None else None,
        results=tuple(results),
    )


def validate_first_party_packs_root_report(
    packs_root: str | Path,
    *,
    pack_roots: Iterable[str | Path] | None = None,
    context: ValidationContext = "assembled",
) -> PackValidationReport:
    """Return a detailed report for a first-party root and its explicit packs."""
    root = Path(packs_root).expanduser().resolve()
    inventory_errors, selected_roots, excluded, runtime_mounts = _first_party_inventory(
        root,
        pack_roots=pack_roots,
    )
    report = validate_pack_roots(selected_roots, context=context)
    return PackValidationReport(
        context=report.context,
        root=root,
        results=report.results,
        inventory_errors=tuple(inventory_errors),
        excluded=tuple(excluded),
        runtime_mounts=tuple(runtime_mounts),
    )


def validate_first_party_packs_root(
    packs_root: str | Path,
) -> tuple[list[str], list[str]]:
    """Validate the canonical first-party ``astrid/packs`` source tree."""
    report = validate_first_party_packs_root_report(packs_root)
    layout_errors = [
        f"{result.pack_id or result.path}: {error}"
        for result in report.results
        for error in result.errors
    ]
    errors = _aggregate_first_party_packs_root_errors(
        internal_inventory_errors=list(report.inventory_errors),
        layout_errors=layout_errors,
    )
    return errors, report.warnings


def _read_pack_metadata(path: Path) -> dict[str, Any]:
    """Read only manifest metadata needed for aggregate diagnostics."""
    data: Any = None
    error: str | None = None
    manifest = path / "pack.yaml"
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except OSError as exc:
        error = f"{path}: cannot read pack manifest: {exc}"
    except yaml.YAMLError as exc:
        error = f"{path}: invalid YAML pack manifest: {exc}"
    if not isinstance(data, dict):
        data = {}
    documentation = data.get("documentation")
    documentation_kind = (
        documentation.get("kind")
        if isinstance(documentation, dict)
        and isinstance(documentation.get("kind"), str)
        else "undocumented"
    )
    documentation_path = (
        documentation.get("path")
        if isinstance(documentation, dict)
        and isinstance(documentation.get("path"), str)
        else None
    )
    skill_name: str | None = None
    skill_description: str | None = None
    if documentation_kind == "skill" and documentation_path is not None:
        skill_path = path / documentation_path
        try:
            resolved_skill = skill_path.resolve(strict=True)
            resolved_skill.relative_to(path.resolve())
            if skill_path.is_file() and not skill_path.is_symlink():
                skill_text = skill_path.read_text(encoding="utf-8")
                frontmatter, parse_error = _parse_skill_frontmatter(skill_text)
                if parse_error is None:
                    name = frontmatter.get("name")
                    description = frontmatter.get("description")
                    skill_name = name.strip() if isinstance(name, str) and name.strip() else None
                    skill_description = (
                        description.strip()
                        if isinstance(description, str) and description.strip()
                        else None
                    )
        except (OSError, ValueError):
            # The validator reports missing, invalid, and escaping resources.
            pass
    skill_paths: list[str] = []
    if path.is_dir():
        for candidate in sorted(path.rglob("SKILL.md")):
            if not candidate.is_symlink():
                skill_paths.append(candidate.relative_to(path).as_posix())
    return {
        "id": data.get("id") if isinstance(data.get("id"), str) else None,
        "status": data.get("status", "active") if isinstance(data.get("status", "active"), str) else "active",
        "visibility": data.get("visibility", "visible") if isinstance(data.get("visibility", "visible"), str) else "visible",
        "support": data.get("support", "project") if isinstance(data.get("support", "project"), str) else "project",
        "documentation": documentation_kind,
        "documentation_path": documentation_path,
        "skill_name": skill_name,
        "skill_description": skill_description,
        "skill_exports": tuple(skill_paths),
        "error": error,
    }


def _parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str | None]:
    """Read authored skill identity for diagnostics without importing pack code."""
    if not text.startswith("---"):
        return {}, "missing YAML frontmatter opening delimiter '---'"
    lines = text.splitlines()
    end_index = next(
        (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
        None,
    )
    if end_index is None:
        return {}, "missing YAML frontmatter closing delimiter '---'"
    try:
        payload = yaml.safe_load("\n".join(lines[1:end_index]))
    except yaml.YAMLError as exc:
        return {}, f"invalid YAML frontmatter: {exc}"
    if not isinstance(payload, dict):
        return {}, "YAML frontmatter must be a mapping"
    return payload, None


def _first_party_inventory(
    root: Path,
    *,
    pack_roots: Iterable[str | Path] | None,
) -> tuple[list[str], tuple[Path, ...], list[str], list[str]]:
    """Resolve first-party root dispositions without discovering packs."""
    if not root.is_dir():
        return [f"{root}: first-party packs root does not exist"], (), [], []

    child_dirs = {
        child.name: child
        for child in root.iterdir()
        if child.is_dir()
        and not child.name.startswith(".")
        and child.name not in _IGNORED_PACKS_ROOT_DIRS
    }
    runtime_mounts = sorted(
        name for name in _FIRST_PARTY_INTERNAL_DIRS - {"_core"} if name in child_dirs
    )
    excluded = sorted(
        name for name in _EXCLUDED_FIRST_PARTY_DIRS if name in child_dirs
    )
    if pack_roots is not None:
        selected = tuple(Path(path).expanduser().resolve() for path in pack_roots)
        return [], selected, excluded, runtime_mounts

    expected = set(_FIRST_PARTY_PACK_IDS if root == _FIRST_PARTY_PACKS_ROOT else _TRACKED_FIRST_PARTY_PACK_IDS)
    # Mirrored/test roots may omit optional personal packs, but a known
    # optional pack is still valid when explicitly present. Keep the allowed
    # inventory wider than the required inventory so only unknown roots fail.
    allowed = set(_FIRST_PARTY_PACK_IDS) | _FIRST_PARTY_INTERNAL_DIRS | _EXCLUDED_FIRST_PARTY_DIRS
    errors: list[str] = []
    for pack_id in sorted(expected - set(child_dirs)):
        errors.append(f"missing first-party pack directory: {pack_id}")
    for name in sorted(set(child_dirs) - allowed):
        errors.append(f"unexpected top-level directory: {name}")
    selected: list[Path] = []
    for pack_id in _FIRST_PARTY_PACK_IDS:
        pack_dir = child_dirs.get(pack_id)
        if pack_dir is None:
            continue
        if not (pack_dir / "pack.yaml").is_file():
            errors.append(f"{pack_id}: pack manifest not found (pack.yaml)")
            continue
        selected.append(pack_dir)
    core_dir = child_dirs.get("_core")
    if core_dir is None:
        errors.append("missing internal directory: _core")
    else:
        for manifest_name in ("pack.yaml", "pack.yml", "pack.json"):
            if (core_dir / manifest_name).is_file():
                errors.append(f"_core: skill-only shell must not contain {manifest_name}")
        if not (core_dir / "docs" / "SKILL.md").is_file():
            errors.append("_core: skill-only shell must provide docs/SKILL.md")
        for forbidden in sorted(
            child.name
            for child in core_dir.iterdir()
            if child.is_dir() and child.name in {"executors", "orchestrators", "elements", "build"}
        ):
            errors.append(f"_core: skill-only shell must not contain top-level {forbidden}/")
    return errors, tuple(selected), excluded, runtime_mounts


def _validate_first_party_packs_root_inventory(root: Path) -> list[str]:
    """Check that the first-party packs root has the expected directory inventory."""
    errors, _selected, _excluded, _runtime = _first_party_inventory(root, pack_roots=None)
    return errors


def _aggregate_first_party_packs_root_errors(
    *,
    internal_inventory_errors: list[str],
    layout_errors: list[str],
) -> list[str]:
    """Combine inventory and layout errors into one aggregate result."""
    body: list[str] = []
    body.extend(f"[internal-inventory] {error}" for error in internal_inventory_errors)
    body.extend(f"[layout] {error}" for error in layout_errors)
    if not body:
        return []
    count = len(body)
    noun = "issue" if count == 1 else "issues"
    return [f"first-party pack validation failed ({count} {noun})", *body]


__all__ = [
    "is_first_party_packs_root_candidate",
    "PackRootValidation",
    "PackValidationReport",
    "validate_pack_roots",
    "validate_first_party_packs_root",
    "validate_first_party_packs_root_report",
]
