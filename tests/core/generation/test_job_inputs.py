"""Public entry inputs survive transport independently of execution controls."""
import json
import zipfile

import pytest

from astrid.core.generation.job_inputs import read_archive, write_archive


def test_public_input_archive_preserves_values_and_file_maps(tmp_path):
    request = tmp_path / 'request.json'
    video = tmp_path / 'source.mp4'
    request.write_text('{"request": true}')
    video.write_bytes(b'video')
    archive = write_archive(tmp_path / 'inputs.zip',
                            {'prompt': 'a cube', 'request': str(request), 'assets': {'source': str(video)}},
                            [{'name': 'request', 'type': 'file'}, {'name': 'assets', 'type': 'file_map'}])
    values = read_archive(archive, tmp_path / 'pod')
    assert values['prompt'] == 'a cube'
    from pathlib import Path
    assert Path(values['request']).read_bytes() == request.read_bytes()
    assert Path(values['assets']['source']).read_bytes() == video.read_bytes()
    assert str(tmp_path / 'pod') in values['assets']['source']


def test_public_input_archive_refuses_paths_outside_its_directory(tmp_path):
    archive = tmp_path / 'inputs.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('inputs.json', json.dumps({'schema': 'astrid.entry-inputs.v1',
                                                   'inputs': {}, 'file_inputs': {'request': '../outside'}, 'file_maps': {}}))
        output.writestr('../outside', 'bad')
    with pytest.raises(ValueError, match='unsafe file reference'):
        read_archive(archive, tmp_path / 'pod')
    assert not (tmp_path / 'outside').exists()


def test_scalar_objects_cannot_be_confused_with_file_metadata(tmp_path):
    original = {'object': {'file': 'ordinary scalar'}, 'map': {'files': {'key': 'scalar'}}}
    archive = write_archive(tmp_path / 'inputs.zip', original, [])
    assert read_archive(archive, tmp_path / 'pod') == original


def test_archive_refuses_existing_destination_symlinks_before_writing(tmp_path):
    source = tmp_path / 'source.txt'
    source.write_text('new')
    archive = write_archive(tmp_path / 'inputs.zip', {'source': str(source)}, [{'name': 'source', 'type': 'file'}])
    outside = tmp_path / 'outside'
    outside.mkdir()
    root = tmp_path / 'pod'
    root.mkdir()
    (root / 'files').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        read_archive(archive, root)
    assert not list(outside.iterdir())


def test_archive_checks_the_file_map_document_before_reading(tmp_path):
    source = tmp_path / 'mapping.json'
    source.write_text('{}')
    linked = tmp_path / 'linked.json'
    linked.symlink_to(source)
    with pytest.raises(ValueError, match='not a regular file'):
        write_archive(tmp_path / 'inputs.zip', {'assets': str(linked)}, [{'name': 'assets', 'type': 'file_map'}])


@pytest.mark.parametrize('cwd_file_exists', [False, True])
def test_file_map_document_resolves_relative_members_from_document(tmp_path, monkeypatch, cwd_file_exists):
    document_dir = tmp_path / 'document'
    document_dir.mkdir()
    (document_dir / 'source.txt').write_text('document content')
    document = document_dir / 'map.json'
    document.write_text(json.dumps({'source': 'source.txt'}))
    cwd = tmp_path / 'cwd'
    cwd.mkdir()
    if cwd_file_exists:
        (cwd / 'source.txt').write_text('different cwd content')
    monkeypatch.chdir(cwd)
    archive = write_archive(tmp_path / 'inputs.zip', {'assets': str(document)},
                            [{'name': 'assets', 'type': 'file_map'}])
    values = read_archive(archive, tmp_path / 'pod')
    from pathlib import Path
    assert Path(values['assets']['source']).read_text() == 'document content'


def test_file_map_document_refuses_relative_symlink_member(tmp_path):
    source = tmp_path / 'source.txt'
    source.write_text('content')
    (tmp_path / 'linked.txt').symlink_to(source)
    document = tmp_path / 'map.json'
    document.write_text(json.dumps({'source': 'linked.txt'}))
    with pytest.raises(ValueError, match='not a regular file'):
        write_archive(tmp_path / 'inputs.zip', {'assets': str(document)}, [{'name': 'assets', 'type': 'file_map'}])


def test_archive_maps_only_declared_file_identities(tmp_path):
    source = tmp_path / 'source.txt'
    source.write_text('content')
    archive = write_archive(tmp_path / 'inputs.zip', {'file': str(source), 'scalar': str(source)},
                            [{'name': 'file', 'type': 'file'}])
    seen = []
    values = read_archive(archive, tmp_path / 'pod', map_file=lambda path: seen.append(path) or 'remote/source.txt')
    assert values == {'file': 'remote/source.txt', 'scalar': str(source)}
    assert len(seen) == 1


def test_same_inputs_write_byte_identical_archives(tmp_path):
    """A resumed job rebuilds the archive; its digest is part of the recovery identity."""
    import hashlib
    import os
    request = tmp_path / 'request.json'
    video = tmp_path / 'source.mp4'
    request.write_text('{"request": true}')
    video.write_bytes(b'video')
    values = {'prompt': 'a cube', 'request': str(request), 'assets': {'source': str(video)}}
    ports = [{'name': 'request', 'type': 'file'}, {'name': 'assets', 'type': 'file_map'}]
    first = write_archive(tmp_path / 'first.zip', values, ports)
    # A later build sees different file times; the archive must not.
    os.utime(request, (1_000_000_000, 1_000_000_000))
    os.utime(video, (1_100_000_000, 1_100_000_000))
    second = write_archive(tmp_path / 'second.zip', values, ports)
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(second.read_bytes()).hexdigest()
    restored = read_archive(second, tmp_path / 'pod')
    assert restored['prompt'] == 'a cube'
