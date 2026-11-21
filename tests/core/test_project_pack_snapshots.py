"""Portable admission bytes and pure decoding; Runtime custody is tested by Runtime."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from astrid.core.element import project_packs as packs
from tests.core import test_project_packs
from tests.core.test_project_pack_inputs import capture_input

configured = test_project_packs.configured


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('utf-8')


@pytest.fixture
def admitted(configured):
    root, directory, _ = test_project_packs.seed()
    result = packs.admit_project_pack('realm', 'project')
    record = result['elements'][0]
    source = capture_input(configured, record)
    admission = source[1]['document']['content']['admissions'][0]
    descriptor = admission['snapshot_object']
    data = configured.media.objects[('project', descriptor['object_id'])]
    snapshot = {**descriptor, 'bytes_base64': base64.b64encode(data).decode('ascii')}
    return result, record, source, snapshot, data, root, directory


def test_admission_uploads_original_resources_and_receipt_once(admitted, configured):
    result, record, source, snapshot, data, _, _ = admitted
    envelope = json.loads(data)
    assert canonical(envelope) == data
    assert [item['path'] for item in envelope['files']] == sorted(source[2])
    assert {item['path']: base64.b64decode(item['bytes_base64'])
            for item in envelope['files']} == source[2]
    assert base64.b64decode(envelope['build_receipt_base64']) == source[3]
    closure = [{'path': name, 'sha256': hashlib.sha256(raw).hexdigest()}
               for name, raw in sorted(source[2].items())]
    assert source[1]['document']['content']['admissions'][0]['snapshot_digest'] == hashlib.sha256(canonical(closure)).hexdigest()
    assert snapshot['digest'] == 'sha256:' + hashlib.sha256(data).hexdigest()
    assert snapshot['size'] == len(data)
    before = copy.deepcopy(configured.rows)
    writes, uploads = len(configured.writes), len(configured.media.imports)
    again = packs.admit_project_pack('realm', 'project')
    assert again['documentVersion'] == result['documentVersion']
    assert configured.rows == before
    assert (len(configured.writes), len(configured.media.imports)) == (writes, uploads)
    assert (Path(record['snapshot']['root']) / '.build-receipt.json').read_bytes() == source[3]


def test_portable_resolution_after_local_sources_and_cache_are_removed(admitted, monkeypatch):
    _, record, source, snapshot, _, root, _ = admitted
    shutil.rmtree(root)
    shutil.rmtree(Path(record['snapshot']['root']).parents[3])

    def forbidden(*args, **kwargs):
        raise AssertionError('portable resolver attempted external I/O')

    monkeypatch.setattr(packs, '_open_client', forbidden)
    for name in ('read_bytes', 'read_text', 'open', 'resolve', 'is_file', 'is_dir', 'rglob'):
        monkeypatch.setattr(Path, name, forbidden)
    assert packs.admitted_element_snapshot_descriptor('realm', 'project', *source[:2], snapshot) == record['descriptor']


def test_noop_readmission_after_cache_loss_reuses_original_cas_receipt(admitted, configured):
    result, record, source, _, _, _, _ = admitted
    shutil.rmtree(Path(record['snapshot']['root']))
    before = copy.deepcopy(configured.rows)
    uploads = len(configured.media.imports)
    again = packs.admit_project_pack('realm', 'project')
    assert again['documentVersion'] == result['documentVersion']
    assert configured.rows == before and len(configured.media.imports) == uploads
    assert (Path(record['snapshot']['root']) / '.build-receipt.json').read_bytes() == source[3]


def test_mixed_admission_retains_old_records_closures_and_pins(configured):
    root, directory, manifest = test_project_packs.seed()
    other = test_project_packs.add_second_element(root, directory, manifest)
    first = packs.admit_project_pack('realm', 'project')
    old_records = {e['descriptor']['id']: e for e in first['elements']}
    old_sources = [capture_input(configured, record) for record in first['elements']]
    old_row = copy.deepcopy(configured.rows[('project', first['documentId'])])
    old_objects = copy.deepcopy(configured.media.objects)
    component = other / 'component.tsx'
    component.write_text(component.read_text() + '\n// B only, preserve original bytes\n')
    second = packs.admit_project_pack('realm', 'project')
    current = {e['descriptor']['id']: e for e in second['elements']}
    updated = configured.rows[('project', second['documentId'])]
    assert second['documentVersion'] == first['documentVersion'] + 1
    assert updated['content']['admissions'][:2] == old_row['content']['admissions']
    assert len(updated['content']['admissions']) == 3
    assert current['reveal'] == old_records['reveal']
    index = Path(second['manifestPath'])
    selected = json.loads(index.read_bytes())
    assert selected['elements'][:2] == first['elements']
    for source, record in zip(old_sources, first['elements']):
        assert capture_input(configured, record)[2:] == source[2:]
        assert source[1]['document']['content'] == old_row['content']
        assert source[1]['pin']['content_digest'] == packs._json_digest(old_row['content'])
    for key, raw in old_objects.items():
        assert configured.media.objects[key] == raw

    sources = [capture_input(configured, record)
               for record in (*first['elements'], current['fade'])]
    new_admission = updated['content']['admissions'][-1]
    new_data = configured.media.objects[('project', new_admission['snapshot_object']['object_id'])]
    new_files, new_receipt = packs._snapshot_resources(new_data)
    snapshot_root = Path(current['fade']['snapshot']['root'])
    entry, resources = packs._resources(snapshot_root, expected_pack_id=current['fade']['descriptor']['packId'])
    assert new_files == {item['path']: (snapshot_root / item['path']).read_bytes() for item in packs._closure(snapshot_root, entry, resources)}
    assert {name for name in new_files if name.startswith('compiled/')} == {'compiled/rendering/elements/effects/reveal/component.tsx.mjs', 'compiled/rendering/elements/effects/fade/component.tsx.mjs'}
    assert new_receipt == sources[-1][3]
    assert {e['id'] for e in json.loads(new_receipt)['elements']} == {'reveal', 'fade'}
    assert new_admission['snapshot_object'] != old_row['content']['admissions'][0]['snapshot_object']

    # A retry restores the current closure's original receipt after cache loss.
    before = copy.deepcopy(configured.rows)
    writes, uploads, index_bytes = len(configured.writes), len(configured.media.imports), index.read_bytes()
    shutil.rmtree(Path(current['fade']['snapshot']['root']))
    assert packs.admit_project_pack('realm', 'project')['elements'] == second['elements']
    assert configured.rows == before and index.read_bytes() == index_bytes
    assert (len(configured.writes), len(configured.media.imports)) == (writes, uploads)
    assert (Path(current['fade']['snapshot']['root']) / '.build-receipt.json').read_bytes() == new_receipt

    shutil.rmtree(root)
    shutil.rmtree(packs.artifact_root())
    for source, record in zip(sources, (*first['elements'], current['fade'])):
        admission = next(a for a in source[1]['document']['content']['admissions']
                         if all(a[k] == source[0][k] for k in ('id', 'kind', 'revision')))
        descriptor = admission['snapshot_object']
        data = configured.media.objects[('project', descriptor['object_id'])]
        snapshot = {**descriptor, 'bytes_base64': base64.b64encode(data).decode('ascii')}
        assert packs.admitted_element_snapshot_descriptor('realm', 'project', *source[:2], snapshot) == record['descriptor']
    # Immutable old pins still decode their old full closures too.
    for source, record in zip(old_sources, first['elements']):
        assert packs.admitted_element_descriptor('realm', 'project', *source) == record['descriptor']


@pytest.mark.parametrize('two_elements', [False, True])
def test_manifest_only_readmission_keeps_current_selection(configured, two_elements):
    root, directory, manifest = test_project_packs.seed()
    if two_elements:
        test_project_packs.add_second_element(root, directory, manifest)
    first = packs.admit_project_pack('realm', 'project')
    before = copy.deepcopy(configured.rows)
    index = Path(first['manifestPath'])
    index_bytes = index.read_bytes()
    writes, uploads = len(configured.writes), len(configured.media.imports)
    manifest['description'] += ' metadata only'
    (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    again = packs.admit_project_pack('realm', 'project')
    assert again['documentVersion'] == first['documentVersion']
    assert again['elements'] == first['elements']
    assert configured.rows == before and index.read_bytes() == index_bytes
    assert (len(configured.writes), len(configured.media.imports)) == (writes, uploads)


@pytest.mark.parametrize('failure', ['build', 'upload', 'document_cas'])
def test_failed_mixed_admission_keeps_old_selection(configured, monkeypatch, failure):
    root, directory, manifest = test_project_packs.seed()
    other = test_project_packs.add_second_element(root, directory, manifest)
    first = packs.admit_project_pack('realm', 'project')
    before, objects = copy.deepcopy(configured.rows), copy.deepcopy(configured.media.objects)
    index = Path(first['manifestPath'])
    index_bytes = index.read_bytes()
    component = other / 'component.tsx'
    good = component.read_text() + '\n// B only\n'
    component.write_text('export default function Reveal( {\n' if failure == 'build' else good)
    configured.media.reject = failure == 'upload'
    configured.reject_update = failure == 'document_cas'
    with pytest.raises(ValueError, match='failed to compile|ingest failed|CAS rejected'):
        packs.admit_project_pack('realm', 'project')
    component.write_text(good)
    assert configured.rows == before and index.read_bytes() == index_bytes
    assert all(configured.media.objects[key] == raw for key, raw in objects.items())
    configured.media.reject = configured.reject_update = False
    second = packs.admit_project_pack('realm', 'project')
    assert second['documentVersion'] == first['documentVersion'] + 1
    if failure != 'build':
        assert configured.media.imports[-2] == configured.media.imports[-1]


def test_explicit_legacy_readmission_backfills_one_identity(admitted, configured):
    _, _, source, _, _, _, _ = admitted
    row = configured.rows[('project', source[1]['pin']['document_id'])]
    del row['content']['admissions'][0]['snapshot_object']
    packs.materialize_project_pack('realm', 'project')
    old_digest = row['content']['admissions'][0]['snapshot_digest']
    before = row['version']
    result = packs.admit_project_pack('realm', 'project')
    updated = configured.rows[('project', source[1]['pin']['document_id'])]
    assert result['documentVersion'] == before + 1
    assert len(updated['content']['admissions']) == 1
    assert updated['content']['admissions'][0]['snapshot_digest'] == old_digest
    assert updated['content']['admissions'][0]['snapshot_object'] == source[1]['document']['content']['admissions'][0]['snapshot_object']


def test_legacy_without_snapshot_fails_with_readmission_instruction(admitted):
    _, _, source, snapshot, _, _, _ = admitted
    verified = copy.deepcopy(source[1])
    del verified['document']['content']['admissions'][0]['snapshot_object']
    with pytest.raises(ValueError, match='pack snapshot unavailable; re-admit pack'):
        packs.admitted_element_snapshot_descriptor('realm', 'project', source[0], verified, snapshot)


@pytest.mark.parametrize('metadata', [
    {'media_type': 'application/octet-stream'}, {'size': True}, {'size': 1},
    {'digest': 'sha256:' + '0' * 64}, {'object_id': 'sha256:' + '0' * 64},
])
def test_dedup_metadata_mismatch_does_not_advance_selection(configured, metadata):
    test_project_packs.seed()
    before = copy.deepcopy(configured.rows)
    selection = Path(packs.inspect_project_pack('realm', 'project')['manifestPath'])
    index = selection.read_bytes()
    configured.media.metadata = metadata
    with pytest.raises(ValueError, match='descriptor|metadata mismatch'):
        packs.admit_project_pack('realm', 'project')
    assert configured.rows == before
    assert selection.read_bytes() == index


def test_failed_upload_then_retry_uses_original_bytes_and_key(configured):
    test_project_packs.seed()
    before = copy.deepcopy(configured.rows)
    configured.media.reject = True
    with pytest.raises(ValueError, match='ingest failed'):
        packs.admit_project_pack('realm', 'project')
    assert configured.rows == before
    configured.media.reject = False
    packs.admit_project_pack('realm', 'project')
    assert configured.media.imports[0] == configured.media.imports[1]


def test_document_cas_retry_reuses_original_bytes_and_upload_identity(configured):
    test_project_packs.seed()
    before = copy.deepcopy(configured.rows)
    configured.reject_update = True
    with pytest.raises(ValueError, match='CAS rejected'):
        packs.admit_project_pack('realm', 'project')
    assert configured.rows == before
    configured.reject_update = False
    packs.admit_project_pack('realm', 'project')
    assert configured.media.imports[0] == configured.media.imports[1]


def test_backfill_cannot_replace_original_closure(admitted, configured):
    _, _, source, _, _, root, _ = admitted
    row = configured.rows[('project', source[1]['pin']['document_id'])]
    del row['content']['admissions'][0]['snapshot_object']
    packs.materialize_project_pack('realm', 'project')
    # A pack-only description change leaves the selected element revision fixed.
    manifest = yaml.safe_load((root / 'pack.yaml').read_text())
    manifest['description'] += ' changed'
    (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    before = copy.deepcopy(configured.rows)
    with pytest.raises(ValueError, match='closure changed'):
        packs.admit_project_pack('realm', 'project')
    assert configured.rows == before


@pytest.mark.parametrize('damage', ['missing', 'extra', 'digest', 'size', 'bool_size', 'mime', 'bytes', 'base64'])
def test_verified_object_must_match_exact_descriptor(admitted, damage):
    _, _, source, snapshot, _, _, _ = admitted
    snapshot = copy.deepcopy(snapshot)
    if damage == 'missing':
        del snapshot['size']
    elif damage == 'extra':
        snapshot['path'] = '/tmp/caller'
    elif damage == 'digest':
        snapshot['digest'] = snapshot['object_id'] = 'sha256:' + '0' * 64
    elif damage in ('size', 'bool_size'):
        snapshot['size'] = 1 if damage == 'size' else True
    elif damage == 'mime':
        snapshot['media_type'] = 'text/plain'
    elif damage == 'bytes':
        snapshot['bytes_base64'] = base64.b64encode(b'changed').decode('ascii')
    else:
        snapshot['bytes_base64'] += '\n'
    with pytest.raises(ValueError):
        packs.admitted_element_snapshot_descriptor('realm', 'project', *source[:2], snapshot)


@pytest.mark.parametrize('damage', [
    'duplicate_key', 'duplicate_path', 'order', 'hash', 'base64', 'noncanonical_base64',
    'extra', 'absolute', 'traversal', 'dot', 'backslash', 'nul', 'receipt', 'spaces',
])
def test_portable_envelope_rejects_malformed_resources(admitted, damage):
    _, _, _, _, data, _, _ = admitted
    envelope = json.loads(data)
    first = envelope['files'][0]
    if damage == 'duplicate_key':
        data = b'{"files":[],"files":[],"build_receipt_base64":"e30="}'
    elif damage == 'duplicate_path':
        envelope['files'].append(first)
    elif damage == 'order':
        envelope['files'].reverse()
    elif damage == 'hash':
        first['sha256'] = '0' * 64
    elif damage == 'base64':
        first['bytes_base64'] += '\n'
    elif damage == 'noncanonical_base64':
        first['bytes_base64'] = 'Zh=='  # Decodes as f; canonical spelling is Zg==.
    elif damage == 'extra':
        first['source'] = '/tmp/source'
    elif damage in ('absolute', 'traversal', 'dot', 'backslash', 'nul'):
        first['path'] = {'absolute': '/pack.yaml', 'traversal': '../pack.yaml',
                         'dot': './pack.yaml', 'backslash': 'a\\pack.yaml',
                         'nul': 'pack\x00.yaml'}[damage]
    elif damage == 'receipt':
        envelope['build_receipt_base64'] = base64.b64encode(b'{"x":1,"x":2}').decode('ascii')
    else:
        data = json.dumps(envelope, indent=2).encode()
    if damage not in ('duplicate_key', 'spaces'):
        data = canonical(envelope)
    with pytest.raises(ValueError):
        packs._snapshot_resources(data)


def test_nonascii_resource_paths_keep_original_admission_closure(configured):
    root, directory, manifest = test_project_packs.seed()
    (directory / 'café.png').write_bytes(b'asset')
    manifest['rendering']['effects/reveal']['resources'].append(
        {'kind': 'asset', 'path': 'rendering/elements/effects/reveal/café.png'})
    (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    # Main's canonical resource grammar is ASCII-only; do not broaden admission.
    with pytest.raises(ValueError, match='does not match'):
        packs.admit_project_pack('realm', 'project')
    files = {'pack.yaml': b'manifest', 'café.png': b'asset'}
    closure = [{'path': name, 'sha256': hashlib.sha256(raw).hexdigest()}
               for name, raw in sorted(files.items())]
    assert packs._closure_digest(closure) == hashlib.sha256(canonical(closure)).hexdigest()
    assert packs._closure_digest(closure) != packs._json_digest(closure).removeprefix('sha256:')
    portable = packs._portable_snapshot(files, b'{"original":"receipt"}')
    assert b'caf\\u00e9.png' in portable
    assert packs._snapshot_resources(portable) == (files, b'{"original":"receipt"}')


def _closure_files(files):
    return [{'path': name, 'sha256': hashlib.sha256(raw).hexdigest()} for name, raw in sorted(files.items())]


def test_compiled_module_is_in_snapshot_and_closure(admitted):
    _, record, source, _, data, _, _ = admitted
    files, _ = packs._snapshot_resources(data)
    compiled = record['compiled']
    assert compiled == {'path': 'compiled/rendering/elements/effects/reveal/component.tsx.mjs',
                        'sha256': hashlib.sha256(files[compiled['path']]).hexdigest()}
    assert b'export' in files[compiled['path']]
    # The raw TSX stays in the snapshot for render; the compiled module joins the closure.
    assert files['rendering/elements/effects/reveal/component.tsx'].startswith(b"import React from 'react';")
    admission = source[1]['document']['content']['admissions'][0]
    assert admission['snapshot_digest'] == hashlib.sha256(canonical(_closure_files(files))).hexdigest()
    assert record['snapshot']['files'] == _closure_files(files) or {f['path'] for f in record['snapshot']['files']} == set(files)


def test_admission_record_carries_compiled_descriptor_and_authoring(admitted):
    _, record, source, _, _, _, _ = admitted
    admission = source[1]['document']['content']['admissions'][0]
    assert admission['compiled'] == record['compiled']
    assert admission['descriptor'] == record['descriptor']
    assert {k: admission['descriptor'][k] for k in ('packId', 'id', 'kind', 'revision')} == {
        'packId': record['descriptor']['packId'], 'id': 'reveal', 'kind': 'effect', 'revision': admission['revision']}
    assert admission['authoring'] == {
        'packRoot': record['authoring']['packRoot'],
        'descriptorPath': 'rendering/elements/effects/reveal/element.yaml',
        'componentPath': 'rendering/elements/effects/reveal/component.tsx',
        'status': 'admitted'}


def test_render_snapshot_checks_accept_compiled_bundle_and_raw_tsx(admitted):
    """Render's digest and file checks (stage_project_elements) run on the same portable bytes."""
    _, record, source, snapshot, _, _, _ = admitted
    reference, verified, _, _ = source
    descriptor = packs.admitted_element_snapshot_descriptor('realm', 'project', reference, verified, snapshot)
    assert descriptor == record['descriptor']
    files, _ = packs._snapshot_object_resources(
        {key: snapshot[key] for key in ('object_id', 'digest', 'size', 'media_type')},
        packs._decode_base64(snapshot['bytes_base64']))
    assert files[record['compiled']['path']] and files[record['snapshot']['componentPath']]
    assert files[record['snapshot']['componentPath']].startswith(b"import React from 'react';")


def test_missing_renderer_esbuild_is_a_plain_error_naming_the_fix(configured, tmp_path, monkeypatch):
    test_project_packs.seed()
    before = copy.deepcopy(configured.rows)
    bare = tmp_path / 'bare-remotion'
    bare.mkdir()
    (bare / 'package.json').write_text(json.dumps({'dependencies': {'esbuild': '0.27.7'}}))
    monkeypatch.setenv('ASTRID_REMOTION_PROJECT_DIR', str(bare))
    with pytest.raises(ValueError, match=r'Remotion esbuild.*is missing.*Repair the install'):
        packs.admit_project_pack('realm', 'project')
    assert configured.rows == before


def test_component_compile_failure_names_component_and_never_advances(configured):
    root, directory, _ = test_project_packs.seed()
    first = packs.admit_project_pack('realm', 'project')
    selection = Path(first['manifestPath'])
    before = selection.read_bytes()
    (directory / 'component.tsx').write_text('export default function Reveal( {\n')
    with pytest.raises(ValueError, match='component.tsx.*failed to compile'):
        packs.admit_project_pack('realm', 'project')
    assert selection.read_bytes() == before


def _stage_selection(source, snapshot):
    reference, verified, _, _ = source
    config = {'clips': [{'id': 'c', 'elementRef': reference}],
              'app': {'astrid_project_element_snapshots': [
                  {'realm': 'realm', 'project': 'project', 'reference': reference, 'verified': verified, 'snapshot': snapshot}]}}
    return config


def test_render_stages_the_admitted_compiled_module_as_the_component(configured, tmp_path):
    """Preview and render run the same compiled bytes: staging replaces the component source with them."""
    from astrid.core.rendering.project_elements import stage_project_elements
    from astrid.pack_api import VERSION
    root, directory, manifest = test_project_packs.seed()
    manifest['pack_api'] = '%d.%d' % VERSION  # the scaffold's own manifest omits it; the loader needs it
    (root / 'pack.yaml').write_text(yaml.safe_dump(manifest))
    record = packs.admit_project_pack('realm', 'project')['elements'][0]
    source = capture_input(configured, record)
    admission = source[1]['document']['content']['admissions'][0]
    snapshot = {**admission['snapshot_object'],
                'bytes_base64': base64.b64encode(configured.media.objects[('project', admission['snapshot_object']['object_id'])]).decode('ascii')}
    registry, roots = stage_project_elements(_stage_selection(source, snapshot), tmp_path)
    staged = Path(roots[0]['root']) / record['snapshot']['componentPath']
    compiled = (Path(record['snapshot']['root']) / record['compiled']['path']).read_bytes()
    assert staged.read_bytes() == compiled != (directory / 'component.tsx').read_bytes()
    assert b'react/jsx-runtime' in compiled  # externals stay for webpack to resolve
    # The stored closure still holds the original TSX; only the render staging swaps it.
    assert (Path(record['snapshot']['root']) / record['snapshot']['componentPath']).read_bytes().startswith(b"import React from 'react';")
