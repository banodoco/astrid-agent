"""Pure compatibility checks for already-parsed pack metadata snapshots."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict

from packaging.markers import UndefinedComparison, UndefinedEnvironmentName, _evaluate_markers
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from astrid.pack_imports import ImportViolation


class PackFacts(TypedDict, total=False):
    id: str
    canonical_root: str
    schema_version: int
    pack_api: str
    dependencies: Mapping[str, object]
    origin: str
    source: str
    commit: str
    installed_digest: str
    observed_digest: str


class PinFacts(TypedDict, total=False):
    commit: str
    digest: str


class CompatibilityResult(TypedDict):
    version: int
    pack_id: str
    ok: bool
    code: str
    reason: str
    fix: str
    dependencies: list[str]
    warnings: list[str]


_API_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_CODES = {
    "unsupported_schema": ("Pack manifest schema is not supported.", "Use a supported pack schema version."),
    "schema_upgrade_required": ("Pack manifest needs an explicit schema upgrade.", "Migrate the manifest to schema v3 and add it again."),
    "invalid_manifest": ("Pack manifest is invalid.", "Correct the manifest fields and try again."),
    "duplicate_id": ("Another selected pack uses this id.", "Choose a unique pack id."),
    "reserved_id": ("This pack id is reserved by Astrid.", "Choose a non-reserved pack id."),
    "basename_mismatch": ("Pack id does not match its directory name.", "Rename the pack directory to match its id."),
    "api_missing": ("Pack does not declare pack_api compatibility.", "Add pack_api as major.minor to the manifest."),
    "api_major": ("Pack requires an incompatible pack_api major version.", "Update the pack for this Astrid release."),
    "api_minor": ("Pack requires a newer pack_api minor version.", "Update Astrid or lower the pack API requirement."),
    "missing_dep": ("A declared pack dependency is unavailable.", "Install the dependency pack or correct dependencies.packs."),
    "cycle": ("Pack dependencies contain a cycle.", "Remove a dependency edge to break the cycle."),
    "default_pin_conflict": ("Pack conflicts with a release default pin.", "Use the release-pinned pack version."),
    "pin_mismatch": ("Installed pack does not match its release pin.", "Reinstall the pinned pack source."),
    "missing_python": ("A declared Python dependency is unavailable.", "Install the required Python package."),
    "import_violation": ("Pack imports code outside its declared public dependencies.", "Fix the reported imports before installing this pack."),
}


def _api_version(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = _API_RE.fullmatch(value)
    return (int(match.group(1)), int(match.group(2))) if match else None


def _dep_ids(manifest: Mapping[str, object]) -> list[str] | None:
    raw_deps = manifest.get("dependencies", {})
    if not isinstance(raw_deps, Mapping):
        return None
    raw = raw_deps.get("packs", [])
    if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) or not item for item in raw):
        return None
    return sorted(set(raw))


_MARKER_FIELDS = frozenset({
    "implementation_name", "implementation_version", "os_name", "platform_machine",
    "platform_release", "platform_system", "platform_version", "python_full_version",
    "platform_python_implementation", "python_version", "sys_platform",
})


def _requirements(manifest: Mapping[str, object], packages: Mapping[str, str],
                  environment: Mapping[str, str] | None) -> tuple[str, str] | None:
    """Check PEP 508 against supplied immutable interpreter facts, never the host."""
    deps = manifest.get("dependencies", {})
    raw = deps.get("python_requirements", []) if isinstance(deps, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return "invalid_manifest", "dependencies.python_requirements must be a list."
    installed = {canonicalize_name(name): version for name, version in packages.items()}
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            return "invalid_manifest", f"Invalid Python requirement {item!r}."
        try:
            req = Requirement(item)
        except InvalidRequirement as exc:
            return "invalid_manifest", f"Invalid Python requirement {item!r}: {exc}"
        if req.marker:
            if environment is None or not _MARKER_FIELDS.issubset(environment):
                return "invalid_manifest", f"Python requirement {item!r} needs an explicit interpreter marker environment (including python_version)."
            try:
                # Marker.evaluate() reads the host environment even when overrides
                # are supplied; evaluate its parsed expression with only the snapshot.
                marker_env = {**environment, "extra": ""}
                if not _evaluate_markers(req.marker._markers, marker_env):
                    continue
            except (UndefinedComparison, UndefinedEnvironmentName, ValueError, TypeError) as exc:
                return "invalid_manifest", f"Python requirement {item!r} has an invalid marker environment: {exc}"
        if req.url or req.extras:
            return "invalid_manifest", f"Python requirement {item!r} cannot be verified from the locked version snapshot (URL or extras)."
        version = installed.get(canonicalize_name(req.name))
        if version is None:
            return "missing_python", f"Python requirement {item!r} is missing from the locked interpreter."
        try:
            parsed_version = Version(version)
        except (InvalidVersion, TypeError):
            return "missing_python", f"Python requirement {item!r} has invalid installed version {version!r}."
        if not req.specifier.contains(parsed_version, prereleases=None):
            return "missing_python", f"Python requirement {item!r} is incompatible with locked version {version!r}."
    return None


def _has_cycle(pack_id: str, deps: list[str], selected: Mapping[str, PackFacts]) -> bool:
    visiting: set[str] = {pack_id}
    visited: set[str] = set()

    def visit(current: str) -> bool:
        if current in visiting:
            return True
        if current in visited:
            return False
        visiting.add(current)
        facts = selected.get(current)
        dep_map = facts.get("dependencies", {}) if isinstance(facts, Mapping) else {}
        children = dep_map.get("packs", ()) if isinstance(dep_map, Mapping) else ()
        if isinstance(children, (list, tuple, set, frozenset)):
            for child in children:
                if isinstance(child, str) and visit(child):
                    return True
        visiting.remove(current)
        visited.add(current)
        return False

    return any(visit(dep) for dep in deps)


def validate_pack(*, mode: Literal["install", "inventory", "execute"],
                  manifest: Mapping[str, object], candidate: PackFacts,
                  selected: Mapping[str, PackFacts], core_api: tuple[int, int],
                  release_pins: Mapping[str, PinFacts], python_packages: Mapping[str, str],
                  import_violations: Sequence[ImportViolation],
                  canonical_personal_root: str | None = None,
                  internal_pack_ids: frozenset[str] = frozenset(),
                  python_environment: Mapping[str, str] | None = None) -> CompatibilityResult:
    """Validate immutable manifest and inventory snapshots without side effects."""
    pack_id_value = manifest.get("id", candidate.get("id", ""))
    pack_id = pack_id_value if isinstance(pack_id_value, str) else ""
    code = "compatible"
    deps: list[str] = []
    warnings: list[str] = []
    detail = ""

    def fail(name: str) -> None:
        nonlocal code
        code = name

    if mode not in {"install", "inventory", "execute"} or not isinstance(manifest, Mapping) or not isinstance(candidate, Mapping):
        fail("invalid_manifest")
    schema = manifest.get("schema_version", manifest.get("version")) if isinstance(manifest, Mapping) else None
    if code == "compatible":
        if type(schema) is not int:
            fail("invalid_manifest")
        elif schema not in {1, 2, 3}:
            fail("unsupported_schema")
        elif schema in {1, 2}:
            fail("schema_upgrade_required")
    if code == "compatible":
        if not pack_id or not re.fullmatch(r"[a-z][a-z0-9_]*", pack_id):
            fail("invalid_manifest")
    if code == "compatible":
        collision = any(
            isinstance(facts, Mapping) and (key == pack_id or facts.get("id") == pack_id) and
            (facts.get("id") != pack_id or not candidate.get("canonical_root") or
             facts.get("canonical_root") != candidate.get("canonical_root"))
            for key, facts in selected.items()
        )
        if collision:
            fail("duplicate_id")
        elif pack_id in {"core", "astrid", "_core"} or (
            pack_id == "personal" and (not canonical_personal_root or candidate.get("id") != "personal" or
                                       candidate.get("canonical_root") != canonical_personal_root)
        ):
            fail("reserved_id")
            if pack_id == "personal":
                detail = f"Personal pack root {candidate.get('canonical_root')!r} is not the configured canonical personal identity {canonical_personal_root!r}."
        elif candidate.get("canonical_root") and str(candidate["canonical_root"]).rstrip("/\\").split("/")[-1].split("\\")[-1] != pack_id:
            fail("basename_mismatch")
    if code == "compatible":
        api_raw = manifest.get("pack_api")
        api = _api_version(api_raw)
        if api_raw is None:
            fail("api_missing")
        elif api_raw == "internal":
            pin = release_pins.get(pack_id, {})
            commit = pin.get("commit")
            digest = pin.get("digest")
            if (pack_id not in internal_pack_ids or not isinstance(commit, str) or
                not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit) or
                not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                candidate.get("commit") != commit or candidate.get("installed_digest") != digest):
                fail("api_major")
                detail = f"Pack {pack_id!r} cannot use internal API without its authorized, exact release commit and digest PIN."
        elif api is None:
            fail("invalid_manifest")
        elif api[0] != core_api[0]:
            fail("api_major")
        elif api[1] > core_api[1]:
            fail("api_minor")
    if code == "compatible":
        parsed = _dep_ids(manifest)
        if parsed is None:
            fail("invalid_manifest")
        else:
            deps = parsed
            if any(dep not in selected for dep in deps):
                fail("missing_dep")
            elif _has_cycle(pack_id, deps, selected):
                fail("cycle")
            elif any(dep in release_pins and isinstance(selected.get(dep), Mapping) and
                     selected[dep].get("commit") != release_pins[dep].get("commit") for dep in deps):
                fail("default_pin_conflict")
            elif mode == "install" and pack_id in release_pins and (
                candidate.get("commit") != release_pins[pack_id].get("commit") or
                candidate.get("installed_digest") != release_pins[pack_id].get("digest")
            ):
                fail("pin_mismatch")
            else:
                requirement_failure = _requirements(manifest, python_packages, python_environment)
                if requirement_failure:
                    fail(requirement_failure[0])
                    detail = requirement_failure[1]
                elif import_violations:
                    fail("import_violation")
    reason, fix = ("", "") if code == "compatible" else _CODES[code]
    if detail:
        reason = detail
    return {
        "version": 1, "pack_id": pack_id, "ok": code == "compatible", "code": code,
        "reason": reason, "fix": fix, "dependencies": deps, "warnings": sorted(set(warnings)),
    }
