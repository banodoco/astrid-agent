"""Exercise the built release through the host's ordinary generated element maps."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from astrid.core.element.registry import ElementRegistry
from astrid.core.element.schema import ElementDefinition
from astrid.core.rendering import _local_schema, remotion_runtime
from scripts import gen_effect_registry

ROOT = Path(__file__).resolve().parents[3]
REMOTION = ROOT / "remotion"
PACKAGE = json.loads((REMOTION / "package.json").read_text(encoding="utf-8"))
LOCK = json.loads((REMOTION / "package-lock.json").read_text(encoding="utf-8"))
COMPOSITION_NAME = "@banodoco/timeline-composition"
COMPOSITION_LOCK = LOCK["packages"][f"node_modules/{COMPOSITION_NAME}"]
ARTIFACT = (
    PACKAGE["dependencies"][COMPOSITION_NAME]
    .removeprefix("file:")
    .removeprefix("artifacts/")
)
ARTIFACT_PATH = REMOTION / "artifacts" / ARTIFACT
SHA256 = ARTIFACT_PATH.with_suffix(ARTIFACT_PATH.suffix + ".sha256").read_text(
    encoding="utf-8"
).split()[0]
INTEGRITY = COMPOSITION_LOCK["integrity"]
REQUIRED_EXPORTS = (".", "./evaluation", "./theme-api", "./registries", "./effects.generated",
                    "./animations.generated", "./transitions.generated", "./registry.generated")


def _stage_locked_package(project: Path) -> tuple[Path, dict]:
    cli = project / remotion_runtime.REMOTION_CLI_RELATIVE_PATH
    cli.parent.mkdir(parents=True)
    cli.write_text("// CLI")
    package = project / "node_modules/@banodoco/timeline-composition"
    package.parent.mkdir(parents=True)
    with tarfile.open(REMOTION / "artifacts" / ARTIFACT) as archive:
        archive.extractall(project)
    (project / "package").rename(package)
    return package, json.loads((package / "package.json").read_text())


def test_locked_release_is_built_and_requires_no_separate_schema_or_theme(tmp_path):
    artifact = REMOTION / "artifacts" / ARTIFACT
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == SHA256
    lock = json.loads((REMOTION / "package-lock.json").read_text())
    package = lock["packages"][f"node_modules/{COMPOSITION_NAME}"]
    assert package["version"] == COMPOSITION_LOCK["version"]
    assert package["resolved"] == f"file:artifacts/{ARTIFACT}"
    assert package["integrity"] == INTEGRITY
    with tarfile.open(artifact) as archive:
        manifest_file = archive.extractfile("package/package.json")
        assert manifest_file is not None
        manifest = json.load(manifest_file)
        assert manifest["version"] == COMPOSITION_LOCK["version"]
        members = archive.getmembers()
        files = {member.name for member in members}
    assert len(members) == 80
    assert all(member.isfile() for member in members)
    assert not any(name.startswith("package/typescript/") for name in files)
    for export in REQUIRED_EXPORTS:
        for target in manifest["exports"][export].values():
            assert target.startswith("./dist/")
            assert "package/" + target.removeprefix("./") in files
    assert not any(name in lock["packages"] for name in (
        "node_modules/@banodoco/timeline-schema", "node_modules/@banodoco/timeline-theme-2rp"))
    cli = tmp_path / remotion_runtime.REMOTION_CLI_RELATIVE_PATH
    cli.parent.mkdir(parents=True)
    cli.write_text("// CLI")
    package_json = tmp_path / "node_modules/@banodoco/timeline-composition/package.json"
    package_json.parent.mkdir(parents=True)
    package_json.write_text(json.dumps(manifest))
    problem = remotion_runtime.remotion_install_problem(tmp_path)
    assert problem is not None
    assert "dist directory is missing" in problem

    package, staged_manifest = _stage_locked_package(tmp_path / "valid")
    assert staged_manifest == manifest
    assert remotion_runtime.remotion_install_problem(package.parents[2]) is None


def test_public_evaluation_admits_valid_source_and_rejects_four_forged_sources(tmp_path):
    project = tmp_path / "api"
    _stage_locked_package(project)
    script = r'''
import assert from 'node:assert/strict';
import {AudioEnvelopeError, clipOpacity, computeMediaTrim, encodedEnvelopeSource,
  getSanitizedPlaybackRate} from '@banodoco/timeline-composition/evaluation';

assert.deepEqual(computeMediaTrim({from: 0.5, to: 1}, 30), {trimBefore: 15, trimAfter: 30});
assert.equal(getSanitizedPlaybackRate(1.5), 1.5);
assert.equal(getSanitizedPlaybackRate(0), 1);
assert.equal(clipOpacity({hold: 1, opacity: 0.5}, 0, 30), 0.5);

const envelope = {keyframes: [{at: 0, value: 1}, {at: 1, value: 1}]};
const prepared = {
  file: 'https://example.test/prepared.wav',
  envelope_key: '{"keyframes":[{"at":0,"value":1},{"at":1,"value":1}]}',
  source_from: 2 / 30,
  playback_rate: 1.5,
};
assert.equal(encodedEnvelopeSource(prepared, envelope, 2 / 30, 1.5), prepared.file);
assert.equal('useAudioEnvelopeSource' in await import('@banodoco/timeline-composition/evaluation'), false);

for (const [name, change] of Object.entries({
  envelope: {envelope_key: '{}'},
  sourceFrom: {source_from: 0.05},
  playbackRate: {playback_rate: 1},
  url: {file: 'blob:prepared'},
})) {
  const forged = {...prepared, ...change};
  const before = JSON.stringify(forged);
  assert.throws(() => encodedEnvelopeSource(forged, envelope, 2 / 30, 1.5),
    (error) => error instanceof AudioEnvelopeError && error.code === 'ENVELOPE_UNRENDERED', name);
  assert.equal(JSON.stringify(forged), before, name);
}
''';
    subprocess.run([os.environ.get("ASTRID_NODE_EXECUTABLE", "node"), "--input-type=module", "-e", script],
                   cwd=project, check=True, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("mutation, expected", [
    ("delete", "evaluation"),
    ("missing", "missing"),
    ("outside", "escapes the package"),
])
def test_installed_package_refuses_incomplete_or_unsafe_built_exports(tmp_path, mutation, expected):
    project = tmp_path / mutation
    package, manifest = _stage_locked_package(project)
    if mutation == "delete":
        (package / "dist/evaluation.js").unlink()
    else:
        target = manifest["exports"]["./evaluation"]
        if mutation == "missing":
            target["import"] = "./dist/missing-evaluation.js"
        else:
            target["import"] = "../evaluation.js"
        (package / "package.json").write_text(json.dumps(manifest))
    problem = remotion_runtime.remotion_install_problem(project)
    assert problem is not None
    assert expected in problem


def test_launch_input_binding_reuses_built_package_readiness(tmp_path):
    project = tmp_path / "runtime"
    _stage_locked_package(project)
    (project / "package.json").write_text("{}")
    (project / "package-lock.json").write_text("{}")
    schema = ROOT / _local_schema.SCHEMA_RELATIVE_PATH
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\n")
    node.chmod(0o755)
    environment = {
        remotion_runtime.NODE_EXECUTABLE_ENV: str(node),
        remotion_runtime.TIMELINE_SCHEMA_PYTHONPATH_ENV: str(schema),
    }
    assert remotion_runtime._runtime_inputs(project, environment, sys.executable, {})
    (project / "node_modules/@banodoco/timeline-composition/dist/evaluation.js").unlink()
    with pytest.raises(remotion_runtime.RemotionValidationError, match="evaluation"):
        remotion_runtime._runtime_inputs(project, environment, sys.executable, {})


@pytest.mark.integration
def test_generated_custom_elements_reach_shared_render_and_scene_unchanged(tmp_path, monkeypatch):
    # This lane requires the host's locked npm install; pure artifact checks run above.
    elements = []
    for kind, element_id in (("effects", "custom"), ("animations", "fade-up"), ("transitions", "fade")):
        root = tmp_path / kind / element_id
        root.mkdir(parents=True)
        (root / "component.tsx").write_text((REMOTION / "tests" / f"custom-{kind}.tsx").read_text())
        elements.append(ElementDefinition(
            id=element_id, kind=kind, root=root, source="pack:probe", editable=True,
            priority=0, component=root / "component.tsx", schema={}, defaults={"durationFrames": 8},
            metadata={"pack_id": "probe", "kind": "wrapper", "phase": ["entrance", "exit"],
                      "clipTypeAliases": ["custom-alias"]} if kind == "effects" else
                     {"pack_id": "probe", "kind": "wrapper", "phase": ["entrance", "exit"]},
        ))
    registry = ElementRegistry(elements)
    monkeypatch.setattr(gen_effect_registry, "_element_registry", lambda: registry)
    for kind in ("effects", "animations", "transitions"):
        (tmp_path / f"{kind}.generated.ts").write_text(gen_effect_registry.generate_element_registry(kind))
    (tmp_path / "host-registries.ts").write_text((REMOTION / "src/host-registries.ts").read_text())
    runtime_project = Path(os.environ.get("ASTRID_REMOTION_PROJECT_DIR", str(REMOTION))).resolve()
    peers = Path(os.environ.get("ASTRID_TEST_NODE_MODULES", str(runtime_project / "node_modules"))).resolve()
    host = tmp_path / "module-host"
    host.mkdir()
    (host / "node_modules").symlink_to(peers, target_is_directory=True)
    script = host / "composition-package.mjs"
    shutil.copyfile(REMOTION / "tests/composition-package.mjs", script)
    package = peers / "@banodoco/timeline-composition"
    before = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in package.rglob("*") if p.is_file()}
    subprocess.run([os.environ.get("ASTRID_NODE_EXECUTABLE", "node"),
                    str(script), str(tmp_path)],
                   cwd=host, check=True, capture_output=True, text=True, timeout=60)
    after = {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in package.rglob("*") if p.is_file()}
    assert before == after
