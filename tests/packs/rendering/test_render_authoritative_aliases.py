"""The renderer consumes Python-selected aliases and scopes host substitutions."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from astrid.core.rendering import _effect_registry as generator


MODULE = Path(__file__).resolve().parents[3] / 'remotion' / 'webpack-alias.mjs'


def _node(program, payload):
    return subprocess.run([os.environ.get('ASTRID_NODE_EXECUTABLE', 'node'), '--input-type=module', '-e',
                           program, MODULE.as_uri(), json.dumps(payload)],
                          capture_output=True, text=True, check=False)


def test_python_selected_alias_ignores_yaml_spelling_and_rejects_escape(tmp_path, monkeypatch):
    root = tmp_path / 'pack'
    effect = root / 'rendering/elements/effects/foo'
    effect.mkdir(parents=True)
    (root / 'pack.yaml').write_text('id: demo\nrendering: {effects/foo: {type: element, path: rendering/elements/effects/foo/element.yaml}}\n')
    (effect / 'component.tsx').write_text('export default 1')
    plugin = generator.PluginRecord('foo', 'effects', 'pack:demo', effect, {}, {}, 'hash', 'pack-demo-elements')
    registry = SimpleNamespace(list=lambda **_: ())
    monkeypatch.setattr(generator, 'discover_plugins', lambda kind, **_: {'foo': plugin} if kind == 'effects' else {})
    rows = generator.render_pack_roots(registry=registry, include_aliases=True)
    assert rows == [{'root': str(root), 'scope': 'pack-demo-elements', 'kind_roots': {'effects': str(effect.parent)}}]
    js = "const m=await import(process.argv[1]); const p=JSON.parse(process.argv[2]); console.log(JSON.stringify(m.packElementAliases({env:{ASTRID_RENDER_PACK_ROOTS:JSON.stringify(p)}})));"
    result = _node(js, rows)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['@pack-demo-elements-effects'] == str(effect.parent)
    foreign = tmp_path / 'outside/effects'
    foreign.mkdir(parents=True)
    rows[0]['kind_roots']['effects'] = str(foreign)
    assert _node(js, rows).returncode != 0


def test_selected_host_resources_do_not_redirect_pack_importers(tmp_path):
    host = tmp_path / 'host'
    (host / 'src').mkdir(parents=True)
    registry = tmp_path / 'selected'
    schema = tmp_path / 'r13-legacy-elements.json'
    schema.write_text('{}')
    pack = tmp_path / 'rendering'
    pack.mkdir()
    (pack / 'pack.yaml').write_text('id: rendering\n')
    data = {'host': str(host), 'registry': str(registry), 'pack': str(pack), 'schema': str(schema)}
    js = """
const m=await import(process.argv[1]); const p=JSON.parse(process.argv[2]);
const env={ASTRID_RENDER_REGISTRY_ROOT:p.registry,ASTRID_RENDER_PACK_ROOTS:JSON.stringify([{root:p.pack,scope:'pack-rendering-elements',kind_roots:{},legacy_schema:p.schema}])};
const query=(issuer,request)=>m.selectedHostResource({remotionRoot:p.host,issuer,request,env}) ?? null;
console.log(JSON.stringify([query(p.host+'/src/host-registries.ts','./effects.generated'),query(p.host+'/src/other.ts','./effects.generated'),query(p.host+'/src/r13-clip-presentation.tsx','../../astrid/core/timeline/schema/r13-legacy-elements.json'),query(p.pack+'/other.ts','../../astrid/core/timeline/schema/r13-legacy-elements.json')]));
"""
    result = _node(js, data)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [str(registry / 'src/effects.generated.ts'), None, str(schema), None]
