"""Composition admission stays aligned with the checked-in Remotion lock."""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.rendering import _remotion_package


def test_composition_package_pin_matches_remotion_lock():
    remotion = REPO_ROOT / "remotion"
    package = json.loads((remotion / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((remotion / "package-lock.json").read_text(encoding="utf-8"))
    key = f"node_modules/{_remotion_package._COMPOSITION_PACKAGE_NAME}"
    entry = lock["packages"][key]

    assert package["dependencies"][_remotion_package._COMPOSITION_PACKAGE_NAME] == entry["resolved"]
    assert entry["version"] == _remotion_package._COMPOSITION_PACKAGE_VERSION


def test_composition_package_admission_accepts_the_saved_pinned_tarball():
    remotion = REPO_ROOT / "remotion"
    package = json.loads((remotion / "package.json").read_text(encoding="utf-8"))
    dependency = package["dependencies"][_remotion_package._COMPOSITION_PACKAGE_NAME]
    assert dependency.startswith("file:")
    with tarfile.open(remotion / dependency.removeprefix("file:")) as archive:
        manifest = json.load(archive.extractfile("package/package.json"))

    assert manifest["name"] == _remotion_package._COMPOSITION_PACKAGE_NAME
    assert manifest["version"] == _remotion_package._COMPOSITION_PACKAGE_VERSION


def test_composition_package_admission_rejects_wrong_name_and_version(tmp_path: Path):
    package = tmp_path / "node_modules" / "@banodoco" / "timeline-composition"
    (package / "dist").mkdir(parents=True)
    exports = {}
    for export in _remotion_package._REQUIRED_COMPOSITION_EXPORTS:
        name = "index" if export == "." else export.removeprefix("./")
        target = package / "dist" / f"{name}.js"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("export {};\n", encoding="utf-8")
        exports[export] = {"import": f"./dist/{name}.js"}

    manifest = {
        "name": _remotion_package._COMPOSITION_PACKAGE_NAME,
        "version": _remotion_package._COMPOSITION_PACKAGE_VERSION,
        "exports": exports,
    }
    manifest_path = package / "package.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _remotion_package.composition_package_problem(tmp_path) is None

    manifest["version"] = f"{_remotion_package._COMPOSITION_PACKAGE_VERSION}-unexpected"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert "unexpected version" in _remotion_package.composition_package_problem(tmp_path)

    manifest["name"] = "@banodoco/other-package"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert "unexpected name" in _remotion_package.composition_package_problem(tmp_path)
