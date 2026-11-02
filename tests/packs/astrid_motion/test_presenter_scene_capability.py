"""Actual registered presenter draw and capability against immutable public13."""
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from astrid.core.rendering.node_pin import resolve_pinned_node
from astrid.core.theme._schema import BUILTIN_DEFAULT_THEME
from scripts import gen_effect_registry
from tests.packs.rendering._helpers import _extract_private_tarball, _stage_private_node_modules

SHA256 = "9412ae25c306d5118241f4d631e8eecc6eef9b2fb5a98302d40938e9f6d54738"

ROOT = Path(__file__).resolve().parents[3]
PRESENTER = Path("astrid/packs/astrid_motion/rendering/elements/effects/am-presenter")
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def presenter_query(tmp_path_factory):
    artifact = Path(os.environ.get("ASTRID_COMPOSITION_TEST_ARTIFACT", str(
        ROOT / "remotion/artifacts/banodoco-timeline-composition-0.0.13.tgz")))
    # Explicit immutable releases only; this override does not change the host pin.
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == SHA256
    peers = Path(os.environ.get("ASTRID_COMPOSITION_TEST_NODE_MODULES", str(
        ROOT / "remotion/node_modules"))).resolve()
    node = resolve_pinned_node(ROOT / "remotion", override=os.environ.get("ASTRID_NODE_EXECUTABLE"))
    assert node.ok, node.problem
    assert node.version == "v20.19.4"
    project_root = tmp_path_factory.mktemp("presenter-scene")
    project = project_root / "remotion"
    source = project / "src"
    source.mkdir(parents=True)
    (project_root / "astrid").symlink_to(ROOT / "astrid", target_is_directory=True)
    modules = project / "node_modules"
    package = modules / "@banodoco/timeline-composition"
    package.parent.mkdir(parents=True)
    with tarfile.open(artifact) as archive:
        expected = {m.name.removeprefix("package/"): hashlib.sha256(archive.extractfile(m).read()).hexdigest()
                    for m in archive.getmembers() if m.isfile()}
    _extract_private_tarball(artifact, project, borrowed=peers)
    (project / "package").rename(package)
    _stage_private_node_modules(peers, modules, skip_scopes={"@banodoco"})
    for name in ("package.json", "webpack-alias.mjs"):
        shutil.copyfile(ROOT / "remotion" / name, project / name)
    shutil.copyfile(ROOT / "remotion/src/host-registries.ts", source / "host-registries.ts")
    # Use only actual pack discovery and the current host's ordinary owner dispatch.
    owner = gen_effect_registry._element_registry().get("effects", "am-presenter", pack_id="astrid_motion")
    assert owner.source == "pack:astrid_motion"
    assert owner.root == ROOT / PRESENTER
    for kind in ("effects", "animations", "transitions"):
        (source / f"{kind}.generated.ts").write_text(gen_effect_registry.generate_element_registry(kind))
    shutil.copyfile(Path(__file__).with_name("presenter_scene_capability.mjs"), project / "query.mjs")
    (project / "builtin-default-theme.json").write_text(json.dumps(BUILTIN_DEFAULT_THEME))
    env = {k: v for k, v in os.environ.items() if k not in ("NODE_OPTIONS", "NODE_PATH")}

    def hashes():
        return {str(p.relative_to(package)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in package.rglob("*") if p.is_file()}

    assert len(expected) == 77 and hashes() == expected
    peer_identity = {p.name: p.resolve() for p in modules.iterdir() if p.name != "@banodoco"}
    preflight = """
      import {createRequire} from 'node:module';
      import {realpathSync} from 'node:fs';
      import {fileURLToPath} from 'node:url';
      const require = createRequire(import.meta.url);
      console.log(JSON.stringify(Object.fromEntries(['react', 'react-dom', 'remotion',
        '@remotion/media', '@remotion/renderer', '@remotion/layout-utils'].map(name =>
          [name, {path: realpathSync(fileURLToPath(import.meta.resolve(name))), version: require(`${name}/package.json`).version}]))));
    """
    resolved = json.loads(subprocess.check_output([str(node.path), "--input-type=module", "-e", preflight],
                                                cwd=project, env=env, text=True, timeout=30))
    for name, peer in resolved.items():
        assert Path(peer["path"]).is_relative_to(peers)
        assert peer["version"] == ("18.3.1" if name in ("react", "react-dom") else "4.0.509")

    def query(case):
        result = subprocess.run([str(node.path), "query.mjs", case], cwd=project, env=env,
                                text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    query.project = project
    query.node = node.path
    query.env = env
    try:
        yield query
        assert hashes() == expected
        assert {p.name: p.resolve() for p in modules.iterdir() if p.name != "@banodoco"} == peer_identity
    finally:
        shutil.rmtree(project_root)


def test_presenter_existing_registered_ssr_markup(presenter_query):
    # Captured before the refactor from 09cfd10a4, with this same host/peer graph.
    expected = json.loads(Path(__file__).with_name("presenter_ssr_before.json").read_text())
    assert presenter_query("snapshots") == expected


@pytest.mark.parametrize("case", ["states", "geometry", "unknowns", "clock", "registration"])
def test_presenter_public_scene_contract(presenter_query, case):
    assert presenter_query(case)["ok"] is True


def test_presenter_public13_declarations(presenter_query, tmp_path):
    # Check copied source against the archive's declarations through ordinary
    # node_modules lookup; no source alias or installed package is patched.
    project = tmp_path / "typecheck"
    (project / "node_modules").parent.mkdir(parents=True)
    (project / "node_modules").symlink_to(presenter_query.project / "node_modules", target_is_directory=True)
    files = [*ROOT.joinpath(PRESENTER).glob("*.ts"), *ROOT.joinpath(PRESENTER).glob("*.tsx")]
    files += [ROOT / "astrid/packs/astrid_motion/rendering/elements/_shared" / name
              for name in ("am.tsx", "mosaic.tsx")]
    files += [ROOT / "astrid/packs/rendering/shared/element_contracts.ts"]
    for file in files:
        destination = project / file.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, destination)
    config = {"compilerOptions": {"target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler",
              "jsx": "react-jsx", "strict": True, "skipLibCheck": True, "noEmit": True}, "include": ["astrid"]}
    (project / "tsconfig.json").write_text(json.dumps(config))
    result = subprocess.run([str(presenter_query.node), str(project / "node_modules/typescript/bin/tsc"),
                             "--project", str(project / "tsconfig.json")],
                            cwd=project, env=presenter_query.env, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_presenter_pack_declares_source_closure():
    pack = ROOT / "astrid/packs/astrid_motion"
    manifest = json.loads((pack / "pack.yaml").read_text())
    entry = manifest["rendering"]["effects/am-presenter"]
    assert entry["path"] == "rendering/elements/effects/am-presenter/element.yaml"
    declared = {pack / resource["path"] for resource in entry["resources"]
                if resource["kind"] == "implementation"}
    expected = {ROOT / PRESENTER / name for name in
                ("component.tsx", "motion.ts", "presenter-core.ts", "scene.ts", "element.yaml")}
    expected |= {pack / "rendering/elements/_shared" / name for name in ("am.tsx", "mosaic.tsx")}
    assert declared == expected
    assert all(path.is_file() for path in declared)
