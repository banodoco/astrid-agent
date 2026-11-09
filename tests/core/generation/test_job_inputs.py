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
