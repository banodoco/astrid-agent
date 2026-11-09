"""Portable public entry inputs, separate from a prepared workflow's execution files."""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any, Mapping

SCHEMA = 'astrid.entry-inputs.v1'


def write_archive(path: Path, values: Mapping[str, Any], ports: list[dict[str, Any]]) -> Path:
    from astrid.core.generation.entries import _input_type

    files = {}
    inputs = dict(values)

    def member(value):
        source = Path(str(value)).expanduser()
        if source.is_symlink() or not source.is_file():
            raise ValueError(f'entry input is not a regular file: {source}')
        name = f'files/{len(files)}{source.suffix}'
        files[name] = source
        return {'file': name}

    for port in ports:
        name = port['name']
        if name not in inputs:
            continue
        kind = _input_type(port.get('type'))
        if kind == 'file':
            inputs[name] = member(inputs[name])
        elif kind == 'file_map':
            value = inputs[name]
            if isinstance(value, (str, Path)):
                value = json.loads(Path(value).read_text())
            if not isinstance(value, Mapping):
                raise ValueError(f'entry input {name!r} must be a file map')
            inputs[name] = {'files': {key: member(value) for key, value in value.items()}}
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('inputs.json', json.dumps({'schema': SCHEMA, 'inputs': inputs}, allow_nan=False))
        for name, source in files.items():
            archive.write(source, name)
    return path


def read_archive(path: Path, destination: Path) -> dict[str, Any]:
    """Materialize only declared regular members, never archive-controlled paths."""
    with zipfile.ZipFile(path) as archive:
        payload = json.loads(archive.read('inputs.json'))
        if payload.get('schema') != SCHEMA or not isinstance(payload.get('inputs'), dict):
            raise ValueError('entry inputs archive has an invalid manifest')
        expected = {'inputs.json'}

        def file_value(value):
            if not isinstance(value, dict) or set(value) != {'file'}:
                raise ValueError('entry inputs archive has an invalid file reference')
            name = value['file']
            if not isinstance(name, str) or not name.startswith('files/') or len(Path(name).parts) != 2 or '..' in Path(name).parts:
                raise ValueError('entry inputs archive has an unsafe file reference')
            expected.add(name)
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
            return str(target)

        inputs = {}
        for name, value in payload['inputs'].items():
            if isinstance(value, dict) and set(value) == {'file'}:
                inputs[name] = file_value(value)
            elif isinstance(value, dict) and set(value) == {'files'}:
                inputs[name] = {key: file_value(item) for key, item in value['files'].items()}
            else:
                inputs[name] = value
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != expected:
            raise ValueError('entry inputs archive contains undeclared members')
        return inputs
