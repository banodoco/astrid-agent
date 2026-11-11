"""One declaration for the engine pins: astrid/packs/runpod/pins.json.

Every other place a VibeComfy, ComfyUI or runpod-lifecycle revision appears either derives
from it at runtime (canonical_pins.canonical_pin) or is checked against it here.  The App
install manifest is checked on the App side by scripts/installer/pin.mjs --check, which reads
this same file at the manifest's Agent SHA.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

import pytest

from astrid.core.generation.canonical_pins import CANONICAL_PINS_PATH, canonical_pin
from astrid.packs.runpod import managed_sources

ROOT = Path(__file__).resolve().parents[3]
PACKS = ROOT / "astrid" / "packs"
CANONICAL = json.loads((ROOT / CANONICAL_PINS_PATH).read_text())["sources"]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_canonical_reader_matches_the_file():
    for name in CANONICAL:
        assert canonical_pin(name) == CANONICAL[name]


@pytest.mark.parametrize("pins_file", sorted(PACKS.glob("*/pins.json")), ids=lambda p: p.parent.name)
def test_every_other_pin_file_agrees_on_shared_sources(pins_file):
    sources = json.loads(pins_file.read_text())["sources"]
    for name, pin in sources.items():
        for key, canonical in CANONICAL.items():
            if name == key or pin["repository"] == canonical["repository"]:
                assert pin == canonical, f"{pins_file.relative_to(ROOT)}:{name} differs from {CANONICAL_PINS_PATH}:{key}"


def test_pin_digests_bound_in_recipes_and_environments_match_the_files():
    for declaration in [*PACKS.glob("*/recipe.*.json"), *PACKS.glob("*/environment.*.json")]:
        ref = json.loads(declaration.read_text()).get("pins")
        if ref:
            assert ref["sha256"] == _digest(ROOT / "astrid" / ref["path"]), declaration.name


def test_pyproject_lifecycle_dependency_is_the_canonical_pin():
    lifecycle = CANONICAL["lifecycle"]
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    (spec,) = [item for item in dev if item.startswith("runpod-lifecycle")]
    assert spec == f"runpod-lifecycle @ git+{lifecycle['repository']}@{lifecycle['revision']}"


def test_pack_manifests_require_the_canonical_lifecycle_version():
    required = f"runpod-lifecycle>={CANONICAL['lifecycle']['version']},<0.4"
    declared = set(re.findall(r"runpod-lifecycle[><=][^\s'\"]*", (PACKS / "runpod/pack.yaml").read_text()))
    assert declared == {required}


def test_literal_comfy_commits_in_workflows_are_the_canonical_pin():
    pattern = re.compile(r"""comfy_commit['"]?\s*[:=]\s*['"]([0-9a-f]{40})['"]""")
    found = {}
    for base in (ROOT / "workflows", PACKS):
        for path in base.rglob("*.py"):
            for match in pattern.findall(path.read_text(errors="ignore")):
                found[str(path.relative_to(ROOT))] = match
    assert found
    assert {v for v in found.values()} == {CANONICAL["comfyui"]["revision"]}, found


def _manifest(tmp_path, monkeypatch, *, lifecycle_sha=None, vibecomfy_sha=None):
    from astrid.core.rendering import _local_schema
    runtime = "a" * 40
    lifecycle = canonical_pin("lifecycle")
    manifest = {
        "agent": {"sha": "b" * 40}, "runtime": {"sha": runtime},
        "lifecycle": {"repo": lifecycle["repository"], "sha": lifecycle_sha or lifecycle["revision"],
                      "version": lifecycle["version"]},
    }
    if vibecomfy_sha:
        manifest["vibecomfy"] = {"sha": vibecomfy_sha}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    lock = tmp_path / "runtime.lock.json"
    lock.write_text(json.dumps({"source_commit": runtime}))
    monkeypatch.setattr(managed_sources, "_installed_manifest_path", lambda: path)
    monkeypatch.setattr(_local_schema, "runtime_lock_path", lambda root: lock)


def test_pod_sources_come_from_the_canonical_pins(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch)
    pins = managed_sources._pins()
    assert pins["src/runpod-lifecycle"] == (CANONICAL["lifecycle"]["repository"], CANONICAL["lifecycle"]["revision"])
    assert pins["src/VibeComfy"][1] == CANONICAL["vibecomfy"]["revision"]


@pytest.mark.parametrize("override", [{"lifecycle_sha": "c" * 40}, {"vibecomfy_sha": "d" * 40}])
def test_a_manifest_override_that_misses_the_canonical_pin_is_refused(tmp_path, monkeypatch, override):
    _manifest(tmp_path, monkeypatch, **override)
    with pytest.raises(managed_sources.ManagedSourcesError, match="canonical pin"):
        managed_sources._pins()
