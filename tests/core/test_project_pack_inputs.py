"""Exact selected declarations from real admission bytes, independent of I/O."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from astrid.core.element import project_packs as packs
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._scalar_commands import _validate_param_values
from tests.core import test_project_packs

configured = test_project_packs.configured


def capture_input(documents, record):
    # Read actual admission output before exercising the pure source resolver.
    reference = {key: record['descriptor'][key] for key in ('packId', 'id', 'kind', 'revision')}
    row = next(copy.deepcopy(row) for (project, _), row in documents.rows.items()
               if project == 'project' and row['content']['pack_id'] == reference['packId'])
    row['project_id'] = 'project'
    verified = {'document': row}
    repin(verified)
    root = Path(record['snapshot']['root'])
    files = {item['path']: (root / item['path']).read_bytes() for item in record['snapshot']['files']}
    return reference, verified, files, (root / '.build-receipt.json').read_bytes()


def repin(verified):
    row = verified['document']
    verified['pin'] = {'document_id': row['document_id'], 'version': row['version'],
                       'content_digest': packs._json_digest(row['content'])}


def resolve(source, *, realm='realm', project='project'):
    return packs.admitted_element_descriptor(realm, project, *source)


@pytest.fixture
def admitted_input(configured):
    root, directory, manifest = test_project_packs.seed()
    (directory / 'helper.ts').write_text('export const seed=4;')
    (directory / 'a').mkdir()
    (directory / 'a/inner.ts').write_text('export const nested=true;')
    (directory / 'a.ts').write_text('export const sibling=true;')
    (directory / 'style.css').write_text('.reveal { opacity: 1; }')
    (directory / 'image.png').write_bytes(b'immutable asset bytes')
    declaration = manifest['rendering']['effects/reveal']
    for kind, name in [('implementation', 'helper.ts'), ('implementation', 'a/inner.ts'),
                       ('implementation', 'a.ts'), ('implementation', 'style.css'),
                       ('asset', 'image.png')]:
        declaration['resources'].append({'kind': kind, 'path': f'rendering/elements/effects/reveal/{name}'})
    (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    definition = yaml.safe_load((directory / 'element.yaml').read_text())
    definition['assets'] = {'image': 'image.png'}
    definition['schema']['properties']['seed']['maximum'] = 10
    # Leaf edits must not acquire whole-object validation or default policies.
    definition['schema']['required'] = ['unrelated']
    definition['schema']['additionalProperties'] = False
    (directory / 'element.yaml').write_text(yaml.safe_dump(definition))
    record = packs.admit_project_pack('realm', 'project')['elements'][0]
    source = capture_input(configured, record)
    return source, record, directory


def test_real_admission_descriptor_and_known_value_validation_without_io(admitted_input, monkeypatch):
    source, record, _ = admitted_input
    reference, verified, files, receipt = source
    before = copy.deepcopy(source)

    def forbidden(*args, **kwargs):
        raise AssertionError('pure selected declaration resolution attempted external I/O')

    monkeypatch.setattr(packs, '_open_client', forbidden)
    for name in ('read_bytes', 'read_text', 'open', 'resolve', 'is_file', 'is_dir', 'rglob'):
        monkeypatch.setattr(Path, name, forbidden)
    # Immutable input views are supported; no source mappings are modified.
    descriptor = resolve((reference, verified, MappingProxyType(files), receipt))
    assert descriptor == record['descriptor']
    _validate_param_values('parent/clip:effect', {'seed': 8, 'opaque': [None, False, {}]}, descriptor['schema'])
    with pytest.raises(CommandError, match='10') as error:
        _validate_param_values('parent/clip:effect', {'seed': 16}, descriptor['schema'])
    assert error.value.path == ['params', 'seed']
    assert source == before
    descriptor['schema']['properties']['seed']['maximum'] = 100
    assert resolve(source) == record['descriptor']


def test_same_captured_declaration_survives_new_admission_and_removed_local_source(admitted_input, configured):
    source, record, directory = admitted_input
    definition = yaml.safe_load((directory / 'element.yaml').read_text())
    definition['schema']['properties']['seed']['maximum'] = 20
    (directory / 'element.yaml').write_text(yaml.safe_dump(definition))
    (directory / 'helper.ts').write_text('export const seed=16;')
    later = packs.admit_project_pack('realm', 'project')['elements'][0]
    later_source = capture_input(configured, later)
    assert later_source[1]['pin']['version'] > source[1]['pin']['version']
    assert later_source[0]['revision'] != source[0]['revision']
    assert resolve(source) == record['descriptor']
    _validate_param_values('clip', {'seed': 16}, resolve(later_source)['schema'])
    with pytest.raises(CommandError, match='10'):
        _validate_param_values('clip', {'seed': 16}, resolve(source)['schema'])
    # Retained bytes are sufficient; this does not claim Runtime CAS retention.
    for path in [directory / 'element.yaml', Path(record['snapshot']['root']) / 'pack.yaml']:
        path.unlink()
    assert resolve(source) == record['descriptor']


@pytest.mark.parametrize('damage', [
    'missing_document', 'pin_digest', 'pin_version', 'pin_id', 'project', 'realm',
    'kind', 'schema', 'binding', 'owner', 'document_id', 'version', 'revoked',
    'duplicate', 'reference_id', 'reference_kind', 'reference_revision', 'source_revision',
    'missing_resource', 'corrupt_helper', 'corrupt_asset', 'extra_resource',
    'missing_build', 'wrong_build_project', 'wrong_build_revision',
])
def test_missing_revoked_corrupt_and_wrong_scope_inputs_refuse(admitted_input, damage):
    source, _, _ = admitted_input
    reference, verified, files, receipt = copy.deepcopy(source)
    row = verified['document']
    content = row['content']
    if damage == 'missing_document':
        verified = {}
    elif damage.startswith('pin_'):
        key = {'pin_digest': 'content_digest', 'pin_version': 'version', 'pin_id': 'document_id'}[damage]
        verified['pin'][key] = 999 if key == 'version' else 'wrong'
    elif damage == 'project':
        row['project_id'] = 'other'
    elif damage == 'realm':
        content['relative_manifest'] = content['relative_manifest'].replace('realm/', 'other/')
        repin(verified)
    elif damage == 'kind':
        row['kind'] = 'forged.config'
    elif damage in ('schema', 'binding', 'owner'):
        key = {'schema': 'schema', 'binding': 'source_binding_key', 'owner': 'pack_id'}[damage]
        content[key] = 'wrong'
        repin(verified)
    elif damage == 'document_id':
        row['document_id'] = 'other-document'
        repin(verified)
    elif damage == 'version':
        row['version'] = True
        repin(verified)
    elif damage in ('revoked', 'duplicate'):
        content['admissions'] = [] if damage == 'revoked' else content['admissions'] * 2
        repin(verified)
    elif damage.startswith('reference_'):
        key = damage.removeprefix('reference_')
        reference[key] = 'wrong'
    elif damage == 'source_revision':
        reference['revision'] = 'sha256:' + 'f' * 64
        content['admissions'][0]['revision'] = reference['revision']
        build = json.loads(receipt)
        build['elements'][0]['revision'] = reference['revision']
        receipt = json.dumps(build).encode()
        repin(verified)
    elif damage == 'missing_resource':
        del files['pack.yaml']
    elif damage.startswith('corrupt_'):
        suffix = 'helper.ts' if damage == 'corrupt_helper' else 'image.png'
        name = next(name for name in files if name.endswith(suffix))
        files[name] += b'corrupt'
    elif damage == 'extra_resource':
        files['injected.yaml'] = b'not admitted'
    else:
        build = json.loads(receipt)
        if damage == 'missing_build':
            build['elements'] = []
        else:
            key = 'projectId' if damage == 'wrong_build_project' else 'revision'
            build['elements'][0][key] = 'wrong'
        receipt = json.dumps(build).encode()
    with pytest.raises(ValueError):
        resolve((reference, verified, files, receipt))


def test_named_pack_uses_its_actual_document_identity(configured):
    test_project_packs.seed()
    packs.init_project_pack('realm', 'project', collection='other_effects')
    # The real default pack cannot be selected through another collection's pin.
    record = packs.admit_project_pack('realm', 'project')['elements'][0]
    source = capture_input(configured, record)
    _, verified, _, _ = source
    verified['document']['document_id'] = packs.stable_document_id('project', packs.DOCUMENT_KIND, 'other_effects')
    repin(verified)
    with pytest.raises(ValueError, match='scope'):
        resolve(source)


def test_property_local_refs_and_remote_refs_never_fetch(monkeypatch):
    import urllib.request

    def forbidden(*args, **kwargs):
        raise AssertionError('parameter validation attempted network access')

    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    schema = {'$defs': {'seed': {'type': 'number', 'maximum': 10}},
              'properties': {'seed': {'$ref': '#/$defs/seed'}}}
    _validate_param_values('clip', {'seed': 8}, schema)
    with pytest.raises(CommandError, match='10'):
        _validate_param_values('clip', {'seed': 16}, schema)
    with pytest.raises(CommandError, match='unavailable'):
        _validate_param_values('clip', {'seed': 8}, {
            'properties': {'seed': {'$ref': 'https://invalid.example/schema'}}})
