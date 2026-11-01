from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "remotion" / "webpack-alias.mjs"


def _node_json(script: str, *args: str) -> object:
    result = subprocess.run(
        [os.environ.get("ASTRID_NODE_EXECUTABLE", "node"), "--input-type=module", "-e", script, *args],
        cwd=ROOT / "remotion",
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env={k:v for k,v in os.environ.items() if k not in {"NODE_OPTIONS", "NODE_PATH", "ASTRID_PACKS_PATH"}},
    )
    return json.loads(result.stdout)


def test_in_tree_aliases_follow_v3_declared_element_roots() -> None:
    result = _node_json(
        """
        import {pathToFileURL} from 'node:url';
        const mod = await import(pathToFileURL(process.argv[2]).href);
        console.log(JSON.stringify(mod.packElementAliases({
          astridDir: process.argv[1],
          env: {ASTRID_PACKS_PATH: ''},
        })));
        """,
        str(ROOT),
        str(MODULE),
    )
    assert result["@pack-rendering-elements-effects"].endswith(
        "astrid/packs/rendering/rendering/elements/effects"
    )
    assert result["@pack-local-elements-effects"].endswith(
        "astrid/packs/local/rendering/elements/effects"
    )
    assert result["@pack-astrid_motion-elements-effects"].endswith(
        "astrid/packs/astrid_motion/rendering/elements/effects"
    )


def test_external_aliases_do_not_scan_legacy_element_roots(tmp_path: Path) -> None:
    pack = tmp_path / "probe"
    (pack / "elements" / "effects" / "legacy-only").mkdir(parents=True)
    (pack / "pack.yaml").write_text(
        "schema_version: 3\nid: probe\nrendering: {}\n", encoding="utf-8"
    )
    script = """
    import {pathToFileURL} from 'node:url';
    const mod = await import(pathToFileURL(process.argv[2]).href);
    console.log(JSON.stringify(mod.packElementAliases({
      astridDir: process.argv[1],
      env: {ASTRID_PACKS_PATH: process.argv[3]},
    })));
    """
    result = _node_json(script, str(tmp_path), str(MODULE), str(pack))
    assert "@pack-probe-elements-effects" not in result


def test_declared_public_resource_resolution_is_contained_and_nonrecursive() -> None:
    result = _node_json(
        """
        import {pathToFileURL} from 'node:url';
        const mod = await import(pathToFileURL(process.argv[2]).href);
        const publicRoot = process.argv[1];
        const output = {font: mod.resolvePublicResource(publicRoot, 'fonts/Gelasio-Regular.woff2')};
        try { mod.resolvePublicResource(publicRoot, '../escape'); output.escape = false; }
        catch { output.escape = true; }
        try { mod.assertOutsidePublicRoot(publicRoot, publicRoot + '/scratch'); output.recursion = false; }
        catch { output.recursion = true; }
        output.external = mod.assertOutsidePublicRoot(publicRoot, publicRoot + '/../scratch');
        console.log(JSON.stringify(output));
        """,
        str(ROOT / "remotion" / "public"),
        str(MODULE),
    )
    assert result["font"].endswith("remotion/public/fonts/Gelasio-Regular.woff2")
    assert result["escape"] is True
    assert result["recursion"] is True
    assert result["external"].endswith("scratch")


def test_native_defaults_and_explicit_roots_ignore_later_cwd(tmp_path: Path) -> None:
    result = _node_json(
        """
        import {pathToFileURL} from 'node:url';
        import assert from 'node:assert/strict';
        const mod = await import(pathToFileURL(process.argv[2]).href);
        const deferred = mod.remotionPrimitiveAliasesFor(process.argv[1] + '/remotion');
        assert.throws(() => mod.remotionPrimitiveAliasesFor(undefined), /absolute/);
        assert.throws(() => mod.remotionPrimitiveAliasesFor('relative'), /absolute/);
        process.chdir(process.argv[3]);
        const native = mod.applyRemotionPrimitiveAliases({});
        const explicit = deferred({});
        assert.deepEqual(native, explicit);
        assert.equal(explicit.resolve.alias['@theme-effects'], process.argv[1] + '/_active_theme/effects');
        assert.equal(explicit.resolve.modules.at(-1), process.argv[1] + '/remotion/node_modules');
        console.log(JSON.stringify(explicit.resolve.alias));
        """, str(ROOT), str(MODULE), str(tmp_path),
    )
    assert result['@pack-rendering-elements-effects'].startswith(str(ROOT / 'astrid/packs/rendering/'))


@pytest.mark.integration
def test_cjs_default_requires_native_esm_but_explicit_factory_works(tmp_path: Path) -> None:
    result = _node_json(
        """
        import {createRequire} from 'node:module';
        import fs from 'node:fs';
        import assert from 'node:assert/strict';
        const require = createRequire(process.argv[1] + '/remotion/package.json');
        const esbuild = require('esbuild');
        const code = esbuild.transformSync(fs.readFileSync(process.argv[2], 'utf8'), {format:'cjs',loader:'js'}).code;
        const module = {exports:{}};
        new Function('require','module','exports',code)(require,module,module.exports);
        assert.throws(() => module.exports.applyRemotionPrimitiveAliases({}), /native ESM only/);
        const fn = module.exports.remotionPrimitiveAliasesFor(process.argv[1] + '/remotion');
        process.chdir(process.argv[3]);
        console.log(JSON.stringify(fn({}).resolve.alias));
        """, str(ROOT), str(MODULE), str(tmp_path),
    )
    assert result['@pack-rendering-elements-transitions'].startswith(str(ROOT / 'astrid/packs/rendering/'))
