"""A provisioned source renderer can bundle without a Python generator."""
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.integration
def test_provisioned_source_bundle_without_render_root_carrier(tmp_path):
    project = Path(__file__).resolve().parents[3] / "remotion"
    if not (project / "node_modules/@remotion/bundler").is_dir():
        pytest.skip("provisioned Remotion dependencies unavailable")
    script = """
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {createRequire} = require('node:module');
const project = process.argv[1];
(async () => {
  const aliases = await import(pathToFileURL(path.join(project, 'webpack-alias.mjs')).href);
  const packs = aliases.packElementAliases({env:{}, astridDir:path.dirname(project)});
  if (!packs['@astrid-rendering']) throw Error('missing provisioned rendering alias');
  const bundle = createRequire(path.join(project, 'package.json'))('@remotion/bundler').bundle;
  await bundle({entryPoint:path.join(project, 'src/index.ts'), outDir:process.argv[2],
    onProgress:()=>{}, webpackOverride:aliases.remotionPrimitiveAliasesFor(project)});
})().catch(e => {console.error(e); process.exitCode=1});
"""
    env = {key: value for key, value in os.environ.items()
           if key not in {"ASTRID_RENDER_PACK_ROOTS", "ASTRID_RENDER_REGISTRY_ROOT", "ASTRID_PACKS_PATH"}}
    result = subprocess.run([env.get("ASTRID_NODE_EXECUTABLE", "node"), "-e", script,
                             str(project), str(tmp_path / "bundle")], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "bundle/index.html").is_file()
