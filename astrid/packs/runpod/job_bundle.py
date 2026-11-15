"""Stdlib-only dispatch bundle builder and embedded pod verifier."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path
from typing import Callable

NAME = 'dispatch.tar'
MANIFEST = 'dispatch-manifest.json'
MARK = 'DISPATCH_VERIFIED'


def build(uploads: list[tuple[Path, str]], selection: Path, destination: Path,
          check: Callable[[], None]) -> str:
    """Flatten selection members into the same authenticated job archive."""
    recorded = {}
    with tarfile.open(destination, 'w') as target:
        def add(name, source, size, mode):
            check()
            path = Path(name)
            if (name in recorded or name == MANIFEST or path.is_absolute() or '..' in path.parts
                    or not path.parts or name != path.as_posix()):
                raise ValueError('unsafe or duplicate dispatch member')
            digest = hashlib.sha256()

            class Reader:
                def read(self, size=-1):
                    check()
                    data = source.read(size)
                    digest.update(data)
                    return data

            member = tarfile.TarInfo(name)
            member.size, member.mode = size, mode & 0o777
            target.addfile(member, Reader())
            recorded[name] = {'size': size, 'sha256': digest.hexdigest(), 'mode': member.mode}

        for local, relative in uploads:
            fd = os.open(local, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('dispatch source is not a regular file')
                add(relative, source, info.st_size, info.st_mode)
        with tarfile.open(selection, 'r:gz') as source:
            for member in source.getmembers():
                if not member.isfile():
                    raise ValueError('selection member is not regular')
                stream = source.extractfile(member)
                assert stream is not None
                with stream:
                    add('selection/' + member.name, stream, member.size, member.mode)
        body = json.dumps({'files': recorded}, sort_keys=True).encode()
        member = tarfile.TarInfo(MANIFEST)
        member.size = len(body)
        target.addfile(member, io.BytesIO(body))
    return hashlib.sha256(body).hexdigest()


# One exec authenticates the entire upload, validates every member and writes
# regular files only. No tar extraction API ever receives a destination path.
UNPACK = r'''
import hashlib, json, os, stat, sys, tarfile
from pathlib import Path
bundle, root, expected, manifest_digest = sys.argv[1:]
def sha(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b''):
        digest.update(chunk)
    return digest.hexdigest()
with open(bundle, 'rb') as stream:
    if sha(stream) != expected:
        raise ValueError('dispatch archive digest mismatch')
root = Path(root)
if any(p.is_symlink() for p in (root, *root.parents)):
    raise ValueError('dispatch destination is a symlink')
with tarfile.open(bundle, 'r:') as archive:
    members = archive.getmembers()
    by_name = {m.name: m for m in members}
    if len(by_name) != len(members) or any(not m.isfile() for m in members):
        raise ValueError('duplicate or nonregular dispatch member')
    manifest = by_name.get('dispatch-manifest.json')
    if manifest is None or manifest.size > 1024 * 1024:
        raise ValueError('missing or oversized dispatch manifest')
    body = archive.extractfile(manifest).read()
    if hashlib.sha256(body).hexdigest() != manifest_digest:
        raise ValueError('dispatch manifest digest mismatch')
    recorded = json.loads(body)['files']
    if not isinstance(recorded, dict) or not recorded or set(by_name) != {'dispatch-manifest.json', *recorded}:
        raise ValueError('dispatch members differ from manifest')
    for name, entry in recorded.items():
        path = Path(name)
        if (not isinstance(entry, dict) or path.is_absolute() or not path.parts
                or '..' in path.parts or name != path.as_posix()
                or type(entry.get('size')) is not int or entry['size'] < 0
                or type(entry.get('mode')) is not int or not 0 <= entry['mode'] <= 0o777
                or by_name[name].mode != entry['mode']
                or by_name[name].size != entry['size']):
            raise ValueError('unsafe dispatch path or size')
        with archive.extractfile(by_name[name]) as source:
            if sha(source) != entry.get('sha256'):
                raise ValueError('dispatch member digest mismatch')
    for name, entry in sorted(recorded.items()):
        destination = root / name
        if any(p.is_symlink() for p in (destination, *destination.parents)):
            raise ValueError('dispatch destination is a symlink')
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as target, archive.extractfile(by_name[name]) as source:
            for chunk in iter(lambda: source.read(1 << 20), b''):
                target.write(chunk)
        if destination.stat().st_size != entry['size']:
            raise ValueError('dispatch extracted size mismatch')
        os.chmod(destination, entry['mode'])
os.unlink(bundle)
print('DISPATCH_VERIFIED')
'''
