"""The declared vignette's actual v3 admission, not an alternate registry."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from astrid.core.pack.canonical import CanonicalPackValidationError, validate_canonical_pack
from astrid.core.pack.loader import load_pack_manifest
from astrid.sdk.timeline.commands import CommandError
from astrid.sdk.timeline.components import (
    UnsupportedComponent, bind_component, component_bounds, component_instance, resolve_component,
)

REPO = Path(__file__).resolve().parents[3]
IDENTITY = 'timeline.component/clip_looks.vignette'


@pytest.fixture
def component(tmp_path, monkeypatch):
    monkeypatch.delenv('ASTRID_PACKS_PATH', raising=False)
    root = tmp_path / 'packs'
    shutil.copytree(REPO / 'examples/packs/clip_looks', root / 'clip_looks')
    kwargs = dict(project_root=tmp_path / 'empty', extra_pack_roots=(str(root),))
    return resolve_component(IDENTITY, **kwargs), kwargs


def edit(component, change):
    path = component.pack_root / component.descriptor_path
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data))


def test_real_pack_validation_discovery_binding_and_shared_bounds(component):
    c, _ = component
    pack = load_pack_manifest(c.pack_root / 'pack.yaml')
    assert pack.extensions['timeline']['components'] == [c.descriptor_path]
    assert c.eligible and c.source_kind == 'extra'
    binding = bind_component(c, host='composition')
    assert binding['identity'] == IDENTITY
    assert set(binding['sources']) == {'docs/SKILL.md', c.descriptor_path, c.descriptor['implementation']['path']}
    assert binding['descriptor']['implementation']['export'] == 'bindVignette'
    assert component_instance(c, 'stable-1', {'strength': 0.5}, host='app') == {
        'id': 'stable-1', 'element': IDENTITY, 'params': {'strength': 0.5}}
    bounds = {'status': 'known', 'rect': {'x': 20, 'y': 10, 'width': 80, 'height': 40}}
    assert component_bounds(c, bounds) == bounds
    assert component_bounds(c, bounds) is not bounds
    # Caller mutation cannot replace the currently admitted declaration.
    c.descriptor['hosts'].clear()
    assert bind_component(c, host='app')['descriptor']['hosts'] == ['composition', 'app']


@pytest.mark.parametrize('params', [{}, {'strength': -1}, {'strength': 2}, {'strength': float('nan')},
                                  {'strength': True}, {'strength': '0.5'}, {'strength': {}},
                                  {'strength': 0.5, 'radius': 1}])
def test_invalid_params_refuse(component, params):
    with pytest.raises(CommandError, match='invalid component params'):
        component_instance(component[0], 'id', params, host='composition')


def test_stable_instance_and_exact_zero_are_serializable(component):
    c, _ = component
    instances = [component_instance(c, i, {'strength': v}, host='composition') for i, v in [('A', 0), ('B', 1)]]
    assert json.loads(json.dumps(instances))[::-1][0]['id'] == 'B'
    assert instances[0]['params']['strength'] == 0
    with pytest.raises(CommandError, match='nonempty'):
        component_instance(c, ' ', {'strength': 0}, host='composition')


@pytest.mark.parametrize('host', ['cli', 'remote', 'sdk', 'sheet'])
def test_typed_unsupported_hosts(component, host):
    with pytest.raises(UnsupportedComponent, match='unsupported') as error:
        bind_component(component[0], host=host)
    assert error.value.to_json()['code'] == 'unsupported'


def test_undeclared_bounds_are_unknown_with_named_finding(component):
    c, kwargs = component
    edit(c, lambda d: d.pop('bounds'))
    unknown = resolve_component(IDENTITY, **kwargs)
    assert component_bounds(unknown, {'status': 'known', 'rect': {}}) == {
        'status': 'unknown', 'finding': {'code': 'unknown-modifier-bounds', 'element': IDENTITY,
                                       'reason': 'Component declares no bounds'}}
    binding = bind_component(unknown, host='composition')
    assert 'bounds' not in binding['descriptor']
    implementation_path = unknown.descriptor['implementation']['path']
    assert binding['sources'][implementation_path]['sha256'] == hashlib.sha256(
        (unknown.pack_root / implementation_path).read_bytes()).hexdigest()
    instance = component_instance(unknown, 'stable-omitted-bounds', {'strength': 0.5}, host='composition')
    assert json.loads(json.dumps(instance)) == {
        'id': 'stable-omitted-bounds', 'element': IDENTITY, 'params': {'strength': 0.5}}


def test_exact_identity_closure_and_neighbour_substitution(component):
    c, kwargs = component
    for identity in ('vignette', 'timeline.operation/clip_looks.vignette'):
        with pytest.raises(UnsupportedComponent):
            resolve_component(identity, **kwargs)
    neighbour = c.pack_root / 'vignette.tsx'
    neighbour.write_text('throw new Error("undeclared neighbour");')
    assert resolve_component(IDENTITY, **kwargs).implementation_digest == c.implementation_digest
    source = c.pack_root / c.descriptor['implementation']['path']
    source.write_text(source.read_text() + '\n// changed implementation\n')
    with pytest.raises(CommandError, match='implementation changed'):
        bind_component(c, host='composition')
    assert resolve_component(IDENTITY, **kwargs).implementation_digest != c.implementation_digest


@pytest.mark.parametrize('change', [lambda d: d.update(point='timeline.operation'),
    lambda d: d.update(id='neighbour.vignette'), lambda d: d.update(bounds='expand'),
    lambda d: d['implementation'].update(path='undeclared.tsx'),
    lambda d: d['implementation'].update(path='../escape.tsx'),
    lambda d: d.update(params={'type': 'invalid'}),
    lambda d: d.update(params={'$ref': 'https://invalid.test/params'})])
def test_declaration_refusals_before_any_execution(component, change):
    c, _ = component
    edit(c, change)
    with pytest.raises(CanonicalPackValidationError, match='invalid timeline component'):
        validate_canonical_pack(c.pack_root)


def test_descriptor_requires_declared_file_resource(component):
    c, _ = component
    manifest = c.pack_root / 'pack.yaml'
    data = yaml.safe_load(manifest.read_text())
    data['resources'] = data['resources'][1:]
    manifest.write_text(yaml.safe_dump(data))
    with pytest.raises(CanonicalPackValidationError, match='declared file resource'):
        validate_canonical_pack(c.pack_root)


def test_environment_is_inspect_only_and_discovery_never_imports(component, monkeypatch, tmp_path):
    c, _ = component
    (c.pack_root / c.descriptor['implementation']['path']).write_text('throw new Error("not executed");')
    monkeypatch.setenv('ASTRID_PACKS_PATH', str(c.pack_root.parent))
    inspected = resolve_component(IDENTITY, project_root=tmp_path / 'empty')
    assert not inspected.eligible and inspected.source_kind == 'env'
    with pytest.raises(UnsupportedComponent, match='unsupported execution'):
        bind_component(inspected, host='composition')
