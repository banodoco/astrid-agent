"""Validation for the server-owned built Composition package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

_COMPOSITION_PACKAGE_NAME = "@banodoco/timeline-composition"
# Release wheels omit remotion/package-lock.json; this runtime pin is guarded
# against the checkout lock by tests/core/rendering/test_remotion_package_pin.py.
_COMPOSITION_PACKAGE_VERSION = "0.0.17"
_REQUIRED_COMPOSITION_EXPORTS = (
    ".",
    "./evaluation",
    "./theme-api",
    "./registries",
    "./effects.generated",
    "./animations.generated",
    "./transitions.generated",
    "./registry.generated",
)


def composition_package_problem(project: Path) -> str | None:
    """Validate the locked Composition package without importing JavaScript."""

    package = project / "node_modules" / "@banodoco" / "timeline-composition"
    manifest_path = package / "package.json"
    if not manifest_path.is_file():
        return f"installed Remotion package is missing: {manifest_path}"
    try:
        package_root = package.resolve(strict=True)
        project_root = project.resolve(strict=True)
    except OSError as exc:
        return f"installed Remotion package manifest is unreadable: {manifest_path} ({exc})"
    try:
        package_root.relative_to(project_root)
    except ValueError:
        return f"installed Remotion package escapes the project: {package_root}"
    try:
        manifest_path.resolve(strict=True).relative_to(package_root)
    except (OSError, ValueError):
        return f"installed Remotion package manifest escapes the package: {manifest_path}"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return f"installed Remotion package manifest is unreadable: {manifest_path} ({exc})"
    if not isinstance(manifest, Mapping):
        return f"installed Remotion package manifest is not an object: {manifest_path}"
    if manifest.get("name") != _COMPOSITION_PACKAGE_NAME:
        return (f"installed Remotion package manifest has unexpected name: "
                f"{manifest.get('name')!r}")
    if manifest.get("version") != _COMPOSITION_PACKAGE_VERSION:
        return (f"installed Remotion package manifest has unexpected version: "
                f"{manifest.get('version')!r}; expected {_COMPOSITION_PACKAGE_VERSION}")
    exports = manifest.get("exports")
    if not isinstance(exports, Mapping):
        return f"installed Remotion package manifest has no exports map: {manifest_path}"

    dist_root = package_root / "dist"
    try:
        dist_resolved = dist_root.resolve(strict=True)
        dist_resolved.relative_to(package_root)
    except (OSError, ValueError):
        return f"installed Remotion package dist directory is missing or outside package: {dist_root}"
    if not dist_resolved.is_dir():
        return f"installed Remotion package dist path is not a directory: {dist_root}"

    def targets(value: Any, condition: str = "") -> list[tuple[str, str]]:
        if isinstance(value, str):
            return [(condition, value)]
        if isinstance(value, Mapping):
            result: list[tuple[str, str]] = []
            for key, nested in value.items():
                if not isinstance(key, str):
                    return []
                result.extend(targets(nested, f"{condition}/{key}" if condition else key))
            return result
        return []

    for export in _REQUIRED_COMPOSITION_EXPORTS:
        if export not in exports:
            return f"installed Remotion package export is missing: {export}"
        export_targets = targets(exports[export])
        if not export_targets:
            return f"installed Remotion package export has no file targets: {export}"
        for condition, target in export_targets:
            label = f"{export} ({condition})" if condition else export
            if target.startswith("../") or target.startswith("/") or ".." in Path(target).parts:
                return (f"installed Remotion package export target escapes the package: "
                        f"{label} -> {target!r}")
            if not target.startswith("./dist/"):
                return (f"installed Remotion package export target is not built: "
                        f"{label} -> {target!r}")
            target_path = package_root / target[2:]
            try:
                resolved = target_path.resolve(strict=True)
                resolved.relative_to(dist_resolved)
            except (OSError, ValueError):
                return (f"installed Remotion package export target is missing or outside dist: "
                        f"{label} -> {target!r}")
            if not resolved.is_file():
                return (f"installed Remotion package export target is not a file: "
                        f"{label} -> {target!r}")
    return None
