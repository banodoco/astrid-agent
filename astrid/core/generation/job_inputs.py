"""Portable public entry inputs, separate from a prepared workflow's execution files."""
from __future__ import annotations

import json
import shutil
import stat
import zipfile
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = 'astrid.entry-inputs.v1'
MAX_BYTES = 16 * 1024**3


def write_archive(path: Path, values: Mapping[str, Any], ports: list[dict[str, Any]]) -> Path:
    from astrid.core.generation.entries import _input_type

    files, file_inputs, file_maps = {}, {}, {}
    inputs = dict(values)

    def regular_file(value):
        source = Path(str(value)).expanduser().absolute()
        if any(parent.is_symlink() for parent in (source, *source.parents)) or not source.is_file():
            raise ValueError(f'entry input is not a regular file: {source}')
        return source

    def member(value):
        source = regular_file(value)
        name = f'files/{len(files)}{source.suffix}'
        files[name] = source
        return name

    for port in ports:
        name = port['name']
        if name not in inputs:
            continue
        kind = _input_type(port.get('type'))
        if kind == 'file':
            file_inputs[name] = member(inputs.pop(name))
        elif kind == 'file_map':
            value = inputs.pop(name)
            document_parent = None
            if isinstance(value, (str, Path)):
                document = regular_file(value)
                document_parent = document.parent
                value = json.loads(document.read_text())
            if not isinstance(value, Mapping):
                raise ValueError(f'entry input {name!r} must be a file map')
            def map_member(item):
                source = Path(str(item)).expanduser()
                if document_parent is not None and not source.is_absolute():
                    source = document_parent / source
                return member(source)
            file_maps[name] = {key: map_member(item) for key, item in value.items()}
    if sum(source.stat().st_size for source in files.values()) > MAX_BYTES:
        raise ValueError('entry inputs archive is too large')
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('inputs.json', json.dumps({'schema': SCHEMA, 'inputs': inputs,
                         'file_inputs': file_inputs, 'file_maps': file_maps}, allow_nan=False))
        for name, source in files.items():
            archive.write(source, name)
    return path


def read_archive(path: Path, destination: Path, *,
                 map_file: Callable[[Path], str] = str) -> dict[str, Any]:
    """Validate all members before writing, then materialize only declared regular files."""
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) > 1024 or len(names) != len(set(names)) or 'inputs.json' not in names:
            raise ValueError('entry inputs archive contains duplicate or missing members')
        if sum(item.file_size for item in infos) > MAX_BYTES or archive.getinfo('inputs.json').file_size > 1024**2:
            raise ValueError('entry inputs archive is too large')
        payload = json.loads(archive.read('inputs.json'))
        if not isinstance(payload, dict) or payload.get('schema') != SCHEMA:
            raise ValueError('entry inputs archive has an invalid manifest')
        inputs, file_inputs, file_maps = (payload.get(name) for name in ('inputs', 'file_inputs', 'file_maps'))
        if not all(isinstance(value, dict) for value in (inputs, file_inputs, file_maps)):
            raise ValueError('entry inputs archive has an invalid manifest')
        if any(not isinstance(value, dict) for value in file_maps.values()):
            raise ValueError('entry inputs archive has an invalid file map')
        keys = [*inputs, *file_inputs, *file_maps]
        if len(keys) != len(set(keys)):
            raise ValueError('entry inputs archive has conflicting public inputs')
        members = [*file_inputs.values(), *(item for mapping in file_maps.values() for item in mapping.values())]
        for name in members:
            if (not isinstance(name, str) or not name.startswith('files/')
                    or len(Path(name).parts) != 2 or '..' in Path(name).parts
                    or name != Path(name).as_posix()):
                raise ValueError('entry inputs archive has an unsafe file reference')
        expected = {'inputs.json', *members}
        if set(names) != expected:
            raise ValueError('entry inputs archive contains undeclared members')
        if any(item.is_dir() or stat.S_IFMT(item.external_attr >> 16) not in (0, stat.S_IFREG) for item in infos):
            raise ValueError('entry inputs archive members must be regular files')
        root = destination.absolute()
        if any(parent.is_symlink() for parent in (root, *root.parents)):
            raise ValueError('entry inputs destination must not be a symlink')
        root.mkdir(parents=True, exist_ok=True)
        for name in set(members):
            target = root / name
            if any(parent.is_symlink() for parent in (target, target.parent)):
                raise ValueError('entry inputs destination must not be a symlink')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
        mapped = {member: map_file(root / member) for member in set(members)}
        return {**inputs, **{name: mapped[member] for name, member in file_inputs.items()},
                **{name: {key: mapped[member] for key, member in mapping.items()}
                   for name, mapping in file_maps.items()}}
