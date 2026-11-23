from __future__ import annotations

import json
import os
import shutil
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


def test_source_only_parser_accepts_checked_in_rendering_pack_manifest(tmp_path: Path) -> None:
    isolated_remotion = tmp_path / "isolated-remotion"
    isolated_remotion.mkdir()
    shutil.copy2(MODULE, isolated_remotion / MODULE.name)
    assert not (isolated_remotion / "node_modules").exists()

    node = shutil.which(os.environ.get("ASTRID_NODE_EXECUTABLE") or "node")
    assert node, "pinned Node executable unavailable (set ASTRID_NODE_EXECUTABLE)"
    script = """
    import {pathToFileURL} from 'node:url';
    const mod = await import(pathToFileURL(process.argv[1]).href);
    console.log(JSON.stringify(mod.packElementAliases({
      astridDir: process.argv[2], env: {},
    })));
    """
    result = subprocess.run(
        [node, "--no-global-search-paths", "--input-type=module", "-e", script,
         str(isolated_remotion / MODULE.name), str(ROOT)],
        cwd=isolated_remotion,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.defpath, **({"SystemRoot": os.environ["SystemRoot"]} if "SystemRoot" in os.environ else {})},
    )
    assert result.returncode == 0, result.stderr
    aliases = json.loads(result.stdout)
    assert aliases["@pack-rendering-elements-effects"] == str(ROOT / "astrid/packs/rendering/rendering/elements/effects")


def test_yaml_fallback_accepts_legacy_and_empty_but_rejects_flow_and_malformed(
    tmp_path: Path,
) -> None:
    isolated_remotion = tmp_path / "isolated-remotion"
    isolated_remotion.mkdir()
    shutil.copy2(MODULE, isolated_remotion / MODULE.name)
    assert not (isolated_remotion / "node_modules").exists()

    pack = tmp_path / "external-pack"
    effects = pack / "rendering" / "effects"
    (effects / "probe").mkdir(parents=True)
    (effects / "probe" / "element.yaml").write_text("id: probe-effect\n", encoding="utf-8")
    manifest = pack / "pack.yaml"
    node = shutil.which(os.environ.get("ASTRID_NODE_EXECUTABLE") or "node")
    assert node, "pinned Node executable unavailable (set ASTRID_NODE_EXECUTABLE)"
    script = """
    import {pathToFileURL} from 'node:url';
    const mod = await import(pathToFileURL(process.argv[1]).href);
    console.log(JSON.stringify(mod.packElementAliases({
      astridDir: process.argv[2], env: {ASTRID_PACKS_PATH: process.argv[3]},
    })));
    """

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [node, "--no-global-search-paths", "--input-type=module", "-e", script,
             str(isolated_remotion / MODULE.name), str(tmp_path), str(pack)],
            cwd=isolated_remotion, check=False, capture_output=True, text=True, timeout=30,
            env={"PATH": os.defpath, **({"SystemRoot": os.environ["SystemRoot"]} if "SystemRoot" in os.environ else {})},
        )

    manifest.write_text(
        "schema_version: 3\nid: probe\nrendering:\n  effects/probe:\n"
        "    type: element\n    path: rendering/effects/probe/element.yaml\n",
        encoding="utf-8",
    )
    legacy = run()
    assert legacy.returncode == 0, legacy.stderr
    assert json.loads(legacy.stdout)["@pack-probe-elements-effects"] == str(effects)

    (effects / "probe title").mkdir()
    (effects / "probe title" / "element.yaml").write_text("id: probe-title\n", encoding="utf-8")
    manifest.write_text(
        "id: probe\nrendering:\n  effects/probe title:\n"
        "    type: 'element'\n    path: 'rendering/effects/probe title/element.yaml'\n",
        encoding="utf-8",
    )
    quoted = run()
    assert quoted.returncode == 0, quoted.stderr
    assert json.loads(quoted.stdout)["@pack-probe-elements-effects"] == str(effects)

    (effects / "probe %!& markers").mkdir()
    (effects / "probe %!& markers" / "element.yaml").write_text("id: markers\n", encoding="utf-8")
    manifest.write_text(
        "id: probe\nmetadata:\n  rendering:\n    ignored: value\n"
        "description: |\n  literal rendering: text\nrendering:\n"
        "  effects/probe %!& markers:\n    type: element\n"
        "    path: rendering/effects/probe %!& markers/element.yaml\n",
        encoding="utf-8",
    )
    nested_and_scalar = run()
    assert nested_and_scalar.returncode == 0, nested_and_scalar.stderr
    assert json.loads(nested_and_scalar.stdout)["@pack-probe-elements-effects"] == str(effects)

    manifest.write_text("schema_version: 3\nid: probe\nrendering: {}\n", encoding="utf-8")
    empty = run()
    assert empty.returncode == 0, empty.stderr
    assert json.loads(empty.stdout) == {}

    manifest.write_text(
        '{schema_version: 3, id: probe, rendering: {"effects/probe": {type: element}}}\n',
        encoding="utf-8",
    )
    flow = run()
    assert flow.returncode != 0
    assert str(manifest) in flow.stderr
    assert "unsupported YAML" in flow.stderr and "flow" in flow.stderr

    manifest.write_text("schema_version: 3\nid: probe\nrendering: [broken}\n", encoding="utf-8")
    malformed = run()
    assert malformed.returncode != 0
    assert str(manifest) in malformed.stderr
    assert "unsupported YAML rendering flow or scalar form" in malformed.stderr

    for unsupported in (
        "id: probe\nrendering:\n  effects/probe: {type: element}\n",
        "id: probe\nrendering:\n    effects/probe:\n      type: element\n",
        "id: probe\nrendering:\n  effects/probe:\n    type: *alias\n    path: x\n",
        "id: probe\nrendering:\n  effects/probe:\n    type: element\n    path: 'x#y'\n",
    ):
        manifest.write_text(unsupported, encoding="utf-8")
        result = run()
        assert result.returncode != 0
        assert str(manifest) in result.stderr and "unsupported YAML" in result.stderr

    for unsupported, expected in (
        ("? rendering\nid: probe\n", "root key"),
        ('"rendering":\n  effects/probe:\n    type: element\n    path: x\n', "rendering declaration"),
        ("id: probe\nrendering:\n", "empty rendering block"),
        ("id: probe\nrendering:\n  effects/probe:\n    type:element\n    path: x\n", "rendering field"),
        ("id: probe\nrendering:\n  effects/probe:\n    type: element\n    path:x\n", "rendering field"),
        ('{"id":"probe","rendering":{},"rendering":{}}\n', "duplicate JSON key"),
    ):
        manifest.write_text(unsupported, encoding="utf-8")
        result = run()
        assert result.returncode != 0
        assert str(manifest) in result.stderr and expected in result.stderr


@pytest.mark.parametrize(
    ("filename", "manifest_text", "expected_alias", "expected_error"),
    [
        (
            "pack.yaml",
            "id: probe\nrendering:\n  effects/probe:\n    type: element\n"
            "    path: rendering/effects/probe/element.yaml\n",
            True,
            None,
        ),
        (
            "pack.json",
            '{"id":"probe","rendering":{"effects/probe":{"type":"element",'
            '"path":"rendering/effects/probe/element.yaml"}}}\n',
            True,
            None,
        ),
        ("pack.yaml", "id: probe\nrendering: {}\n", False, None),
        (
            "pack.yaml",
            "id: probe\nmetadata:\n  rendering:\n    ignored: true\n"
            "description: |\n  literal rendering: text\nrendering:\n"
            "  effects/probe:\n    type: element\n"
            "    path: rendering/effects/probe/element.yaml\n",
            True,
            None,
        ),
        (
            "pack.yaml",
            "id: probe\nrendering:\n  effects/probe %!&:\n    type: element\n"
            "    path: rendering/effects/probe %!&/element.yaml\n",
            True,
            None,
        ),
        ("pack.yaml", "? rendering\nid: probe\n", False, "root key"),
        (
            "pack.yaml",
            '"rendering":\n  effects/probe:\n    type: element\n    path: x\n',
            False,
            "rendering declaration",
        ),
        ("pack.yaml", "id: probe\nrendering:bad\n", False, "rendering separator"),
        ("pack.yaml", "id: probe\nrendering:\n", False, "empty rendering block"),
        (
            "pack.yaml",
            "id: probe\nrendering: {effects/probe: {type: element}}\n",
            False,
            "rendering flow",
        ),
        (
            "pack.yaml",
            "id: probe\nrendering:\n  effects/probe:\n    type: !tag element\n    path: x\n",
            False,
            "alias or tag",
        ),
        (
            "pack.yaml",
            "id: probe\nrendering:\n  effects/probe:\n    type:element\n    path:x\n",
            False,
            "rendering field",
        ),
        (
            "pack.json",
            '{"id":"probe","rendering":{},"rendering":{}}\n',
            False,
            "duplicate JSON key",
        ),
        (
            "pack.yaml",
            "id: probe\nrendering:\n  effects/probe:\n    type: element\n"
            "    path: x\n  effects/probe:\n    type: element\n    path: y\n",
            False,
            "duplicate rendering entry",
        ),
        ("pack.yaml", "id: probe\nunexpected root scalar\n", False, "document line form"),
        ("pack.yaml", "  id: probe\n", False, "root indentation"),
        ("pack.yaml", "id: null\nrendering: {}\n", False, "id scalar"),
        ("pack.yaml", "id: probe\n  continued\nrendering: {}\n", False, "id continuation"),
        (
            "pack.yaml",
            "description: [one,,two]\nid: probe\nrendering: {}\n",
            False,
            "document flow",
        ),
        (
            "pack.yaml",
            "schema_version: 3\nschema_version: 3\nid: probe\nrendering: {}\n",
            False,
            "duplicate mapping key",
        ),
        (
            "pack.yaml",
            "metadata:\n  labels:\n    owner: one\n    owner: two\nid: probe\nrendering: {}\n",
            False,
            "duplicate mapping key",
        ),
        (
            "pack.yaml",
            "description: {owner: one, owner: two}\nid: probe\nrendering: {}\n",
            False,
            "duplicate flow mapping key",
        ),
        (
            "pack.yaml",
            "description:\n  arbitrary continuation\nid: probe\nrendering: {}\n",
            False,
            "document line form",
        ),
        (
            "pack.yaml",
            "description: text\n    [bad] continuation\nid: probe\nrendering: {}\n",
            False,
            "document line form",
        ),
        (
            "pack.yaml",
            "id: probe\nrendering:\n  effects/probe:\n    type: element\n"
            "    path: rendering/effects/probe/element.yaml\n    data: arbitrary\n"
            "      arbitrary continuation\n",
            False,
            "document line form",
        ),
    ],
)
def test_source_only_yaml_parser_whitelist(
    tmp_path: Path,
    filename: str,
    manifest_text: str,
    expected_alias: bool,
    expected_error: str | None,
) -> None:
    isolated_remotion = tmp_path / "isolated-remotion"
    isolated_remotion.mkdir()
    shutil.copy2(MODULE, isolated_remotion / MODULE.name)
    assert not (isolated_remotion / "node_modules").exists()

    pack = tmp_path / "external-pack"
    effects = pack / "rendering" / "effects"
    (effects / "probe").mkdir(parents=True)
    (effects / "probe" / "element.yaml").write_text("id: probe-effect\n", encoding="utf-8")
    manifest = pack / filename
    manifest.write_text(manifest_text, encoding="utf-8")
    node = shutil.which(os.environ.get("ASTRID_NODE_EXECUTABLE") or "node")
    assert node, "pinned Node executable unavailable (set ASTRID_NODE_EXECUTABLE)"
    script = """
    import {pathToFileURL} from 'node:url';
    const mod = await import(pathToFileURL(process.argv[1]).href);
    console.log(JSON.stringify(mod.packElementAliases({
      astridDir: process.argv[2], env: {ASTRID_PACKS_PATH: process.argv[3]},
    })));
    """
    result = subprocess.run(
        [node, "--no-global-search-paths", "--input-type=module", "-e", script,
         str(isolated_remotion / MODULE.name), str(tmp_path), str(pack)],
        cwd=isolated_remotion,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.defpath, **({"SystemRoot": os.environ["SystemRoot"]} if "SystemRoot" in os.environ else {})},
    )
    if expected_error is not None:
        assert result.returncode != 0
        assert str(manifest) in result.stderr and expected_error in result.stderr
        return
    assert result.returncode == 0, result.stderr
    aliases = json.loads(result.stdout)
    if expected_alias:
        assert aliases["@pack-probe-elements-effects"] == str(effects)
    else:
        assert aliases == {}


@pytest.mark.remotion
def test_installed_yaml_parser_accepts_flow_and_rejects_malformed(tmp_path: Path) -> None:
    isolated_remotion = tmp_path / "isolated-remotion"
    isolated_remotion.mkdir()
    shutil.copy2(MODULE, isolated_remotion / MODULE.name)
    node_modules = isolated_remotion / "node_modules"
    node_modules.mkdir()
    shutil.copytree(ROOT / "remotion" / "node_modules" / "js-yaml", node_modules / "js-yaml")

    pack = tmp_path / "external-pack"
    effects = pack / "rendering" / "effects"
    (effects / "probe").mkdir(parents=True)
    (effects / "probe" / "element.yaml").write_text("id: probe-effect\n", encoding="utf-8")
    manifest = pack / "pack.yaml"
    node = shutil.which(os.environ.get("ASTRID_NODE_EXECUTABLE") or "node")
    assert node, "pinned Node executable unavailable (set ASTRID_NODE_EXECUTABLE)"
    script = """
    import {pathToFileURL} from 'node:url';
    const mod = await import(pathToFileURL(process.argv[1]).href);
    console.log(JSON.stringify(mod.packElementAliases({
      astridDir: process.argv[2], env: {ASTRID_PACKS_PATH: process.argv[3]},
    })));
    """

    def run() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [node, "--no-global-search-paths", "--input-type=module", "-e", script,
             str(isolated_remotion / MODULE.name), str(tmp_path), str(pack)],
            cwd=isolated_remotion, check=False, capture_output=True, text=True, timeout=30,
            env={"PATH": os.defpath, **({"SystemRoot": os.environ["SystemRoot"]} if "SystemRoot" in os.environ else {})},
        )

    manifest.write_text(
        '{schema_version: 3, id: probe, rendering: '
        '{"effects/probe": {type: element, path: "rendering/effects/probe/element.yaml"}}}\n',
        encoding="utf-8",
    )
    flow = run()
    assert flow.returncode == 0, flow.stderr
    assert json.loads(flow.stdout)["@pack-probe-elements-effects"] == str(effects)

    manifest.write_text("{schema_version: 3, id: probe, rendering: [broken}\n", encoding="utf-8")
    malformed = run()
    assert malformed.returncode != 0
    assert str(manifest) in malformed.stderr
    assert "invalid YAML form" in malformed.stderr

    manifest.write_text('{"id":"probe","rendering":{},"rendering":{}}\n', encoding="utf-8")
    duplicate = run()
    assert duplicate.returncode != 0
    assert str(manifest) in duplicate.stderr
    assert "invalid YAML form" in duplicate.stderr


@pytest.mark.remotion
def test_explicit_project_binding_resolves_yaml_parser_from_project(tmp_path: Path) -> None:
    module_source = tmp_path / "module-source"
    module_source.mkdir()
    shutil.copy2(MODULE, module_source / MODULE.name)

    explicit_project = tmp_path / "explicit-project"
    explicit_project.mkdir()
    node_modules = explicit_project / "node_modules"
    node_modules.mkdir()
    shutil.copytree(ROOT / "remotion" / "node_modules" / "js-yaml", node_modules / "js-yaml")
    assert not (module_source / "node_modules").exists()

    pack = tmp_path / "external-pack"
    effects = pack / "rendering" / "effects"
    (effects / "probe").mkdir(parents=True)
    (effects / "probe" / "element.yaml").write_text("id: probe-effect\n", encoding="utf-8")
    manifest = pack / "pack.yaml"
    manifest.write_text(
        '{schema_version: 3, id: probe, rendering: '
        '{"effects/probe": {type: element, path: "rendering/effects/probe/element.yaml"}}}\n',
        encoding="utf-8",
    )

    node = shutil.which(os.environ.get("ASTRID_NODE_EXECUTABLE") or "node")
    assert node, "pinned Node executable unavailable (set ASTRID_NODE_EXECUTABLE)"
    script = """
    import {pathToFileURL} from 'node:url';
    const mod = await import(pathToFileURL(process.argv[1]).href);
    const config = mod.remotionPrimitiveAliasesFor(process.argv[2])({});
    console.log(JSON.stringify(config.resolve.alias));
    """
    result = subprocess.run(
        [node, "--no-global-search-paths", "--input-type=module", "-e", script,
         str(module_source / MODULE.name), str(explicit_project)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.defpath,
            "ASTRID_PACKS_PATH": str(pack),
            **({"SystemRoot": os.environ["SystemRoot"]} if "SystemRoot" in os.environ else {}),
        },
    )
    assert result.returncode == 0, result.stderr
    aliases = json.loads(result.stdout)
    assert aliases["@pack-probe-elements-effects"] == str(effects)


@pytest.mark.remotion
def test_explicit_project_without_parser_does_not_use_module_source_parser(tmp_path: Path) -> None:
    explicit_project = tmp_path / "explicit-project"
    explicit_project.mkdir()
    assert not (explicit_project / "node_modules").exists()

    pack = tmp_path / "external-pack"
    effects = pack / "rendering" / "effects"
    (effects / "probe").mkdir(parents=True)
    (effects / "probe" / "element.yaml").write_text("id: probe-effect\n", encoding="utf-8")
    manifest = pack / "pack.yaml"
    manifest.write_text(
        '{schema_version: 3, id: probe, rendering: '
        '{"effects/probe": {type: element, path: "rendering/effects/probe/element.yaml"}}}\n',
        encoding="utf-8",
    )

    node = shutil.which(os.environ.get("ASTRID_NODE_EXECUTABLE") or "node")
    assert node, "pinned Node executable unavailable (set ASTRID_NODE_EXECUTABLE)"
    script = """
    import {pathToFileURL} from 'node:url';
    const mod = await import(pathToFileURL(process.argv[1]).href);
    const config = mod.remotionPrimitiveAliasesFor(process.argv[2])({});
    console.log(JSON.stringify(config.resolve.alias));
    """
    result = subprocess.run(
        [node, "--no-global-search-paths", "--input-type=module", "-e", script,
         str(MODULE), str(explicit_project)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.defpath,
            "ASTRID_PACKS_PATH": str(pack),
            **({"SystemRoot": os.environ["SystemRoot"]} if "SystemRoot" in os.environ else {}),
        },
    )
    assert result.returncode != 0
    assert str(manifest) in result.stderr
    assert "unsupported YAML" in result.stderr and "flow" in result.stderr


@pytest.mark.remotion
def test_project_yaml_parser_entry_symlink_outside_project_is_rejected(tmp_path: Path) -> None:
    explicit_project = tmp_path / "explicit-project"
    package = explicit_project / "node_modules" / "js-yaml"
    package.mkdir(parents=True)
    outside_entry = tmp_path / "outside-parser.mjs"
    outside_entry.write_text(
        "export function load() { throw new Error('outside parser was executed'); }\n",
        encoding="utf-8",
    )
    (package / "package.json").write_text(
        json.dumps({"name": "js-yaml", "type": "module", "main": "./entry.mjs"}),
        encoding="utf-8",
    )
    (package / "entry.mjs").symlink_to(outside_entry)

    pack = tmp_path / "external-pack"
    effects = pack / "rendering" / "effects"
    (effects / "probe").mkdir(parents=True)
    (effects / "probe" / "element.yaml").write_text("id: probe-effect\n", encoding="utf-8")
    manifest = pack / "pack.yaml"
    manifest.write_text(
        '{schema_version: 3, id: probe, rendering: '
        '{"effects/probe": {type: element, path: "rendering/effects/probe/element.yaml"}}}\n',
        encoding="utf-8",
    )

    node = shutil.which(os.environ.get("ASTRID_NODE_EXECUTABLE") or "node")
    assert node, "pinned Node executable unavailable (set ASTRID_NODE_EXECUTABLE)"
    script = """
    import {pathToFileURL} from 'node:url';
    const mod = await import(pathToFileURL(process.argv[1]).href);
    const config = mod.remotionPrimitiveAliasesFor(process.argv[2])({});
    console.log(JSON.stringify(config.resolve.alias));
    """
    result = subprocess.run(
        [node, "--no-global-search-paths", "--input-type=module", "-e", script,
         str(MODULE), str(explicit_project)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.defpath,
            "ASTRID_PACKS_PATH": str(pack),
            **({"SystemRoot": os.environ["SystemRoot"]} if "SystemRoot" in os.environ else {}),
        },
    )
    assert result.returncode != 0
    assert str(manifest) in result.stderr
    assert "parser entry escapes the Remotion project" in result.stderr


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


def test_copied_frame_worker_clears_project_selectors_without_optional_sibling(tmp_path: Path) -> None:
    copied_remotion = tmp_path / "remotion"
    copied_source = copied_remotion / "src"
    copied_source.mkdir(parents=True)
    shutil.copy2(ROOT / "remotion" / "src" / "astrid-frame-worker.mjs",
                 copied_source / "astrid-frame-worker.mjs")
    shutil.copy2(ROOT / "remotion" / "shared-browser.mjs", copied_remotion / "shared-browser.mjs")
    assert not (copied_source / "frame-request-environment.mjs").exists()
    renderer_package = copied_remotion / "node_modules" / "@remotion" / "renderer"
    renderer_package.mkdir(parents=True)
    (renderer_package / "package.json").write_text(
        json.dumps({"type": "module", "exports": "./index.js"}), encoding="utf-8"
    )
    (renderer_package / "index.js").write_text(
        "export const openBrowser = async () => {};\n"
        "export const renderFrames = async () => {};\n"
        "export const selectComposition = async () => {};\n",
        encoding="utf-8",
    )

    result = _node_json(
        """
        import assert from 'node:assert/strict';
        import {pathToFileURL} from 'node:url';
        process.stdin.push(null);
        const {_test} = await import(pathToFileURL(process.argv[1]).href);
        const target = {PATH: '/owner/bin'};
        _test.applyFrameRequestEnvironment(target, {
          ASTRID_RENDER_PACK_ROOTS: '[{"root":"/project/packs"}]',
          ASTRID_RENDER_REGISTRY_ROOT: '/project/registry',
          ASTRID_PACKS_PATH: '/project/packs',
        });
        _test.applyFrameRequestEnvironment(target, {TMPDIR: '/task/tmp'});
        assert.equal('ASTRID_RENDER_PACK_ROOTS' in target, false);
        assert.equal('ASTRID_RENDER_REGISTRY_ROOT' in target, false);
        assert.equal('ASTRID_PACKS_PATH' in target, false);
        assert.equal(target.PATH, '/owner/bin');
        console.log(JSON.stringify({cleared: true, path: target.PATH}));
        """,
        str(copied_source / "astrid-frame-worker.mjs"),
    )
    assert result == {"cleared": True, "path": "/owner/bin"}


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
