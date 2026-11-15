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


def _safe_name(name: str) -> bool:
    return (isinstance(name, str) and bool(name) and not any(c in name for c in "\\:\x00")
            and all(part not in {"", ".", ".."} for part in name.split("/"))
            and not name.startswith("/"))


def verify_response(bundle: Path, code: int, output: str, error: str) -> None:
    """A partial observation never authorizes launch; name all unverified inputs."""
    with tarfile.open(bundle, 'r:') as archive:
        expected = json.load(archive.extractfile(MANIFEST))['files']
    paths = ', '.join(sorted(expected))
    try:
        response = json.loads(output)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'invalid dispatch verifier JSON for {paths}') from exc
    if code != 0:
        details = response.get('errors') if isinstance(response, dict) else None
        raise ValueError(f'dispatch refused for {paths}: {details or error}')
    if (not isinstance(response, dict) or set(response) != {'status', 'files'}
            or response['status'] != MARK or not isinstance(response['files'], dict)):
        raise ValueError(f'invalid dispatch verifier response for {paths}')
    observed = response['files']
    mismatched = sorted(name for name in set(expected) | set(observed)
                        if name not in expected or name not in observed
                        or json.dumps(expected[name], sort_keys=True) != json.dumps(observed[name], sort_keys=True))
    if mismatched:
        raise ValueError('dispatch verification mismatch: ' + ', '.join(mismatched))


def build(uploads: list[tuple[Path, str]], selection: Path, destination: Path,
          check: Callable[[], None]) -> str:
    """Flatten selection members into the same authenticated job archive."""
    recorded = {}
    errors = []
    for local, relative in uploads:
        if not _safe_name(relative) or relative == MANIFEST:
            errors.append(f"unsafe dispatch member: {relative!r}")
        try:
            if not stat.S_ISREG(local.lstat().st_mode):
                errors.append(f"dispatch source is not regular: {local}")
        except OSError as error:
            errors.append(f"dispatch source {local}: {error}")
    if errors:
        raise ValueError("; ".join(errors))
    with tarfile.open(destination, 'w') as target:
        def add(name, source, size, mode):
            check()
            if name in recorded or name == MANIFEST or not _safe_name(name):
                raise ValueError(f'unsafe or duplicate dispatch member: {name!r}')
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
                    raise ValueError(f'dispatch source is not a regular file: {local}')
                add(relative, source, info.st_size, info.st_mode)
        with tarfile.open(selection, 'r:gz') as source:
            for member in source.getmembers():
                if not member.isfile():
                    raise ValueError(f'selection member is not regular: {member.name!r}')
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
def refuse(errors):
    print(json.dumps({'status': 'refused', 'errors': errors}))
    sys.exit(1)
def safe(name):
    return (isinstance(name, str) and bool(name) and not any(c in name for c in "\\:\x00")
            and all(part not in {'', '.', '..'} for part in name.split('/'))
            and not name.startswith('/'))
with open(bundle, 'rb') as stream:
    if sha(stream) != expected:
        refuse([bundle + ': dispatch archive digest mismatch'])
root = Path(root)
if any(p.is_symlink() for p in (root, *root.parents)):
    refuse([str(root) + ': dispatch destination is a symlink'])
with tarfile.open(bundle, 'r:') as archive:
    members = archive.getmembers()
    by_name, errors = {}, []
    for member in members:
        if member.name in by_name:
            errors.append(member.name + ': duplicate dispatch member')
        if not member.isfile():
            errors.append(member.name + ': nonregular dispatch member')
        if not safe(member.name):
            errors.append(repr(member.name) + ': unsafe dispatch path')
        by_name[member.name] = member
    if errors:
        refuse(errors)
    manifest = by_name.get('dispatch-manifest.json')
    if manifest is None or manifest.size > 1024 * 1024:
        refuse(['dispatch-manifest.json: missing or oversized dispatch manifest'])
    body = archive.extractfile(manifest).read()
    if hashlib.sha256(body).hexdigest() != manifest_digest:
        refuse(['dispatch-manifest.json: dispatch manifest digest mismatch'])
    recorded = json.loads(body)['files']
    if not isinstance(recorded, dict) or not recorded:
        refuse(['dispatch-manifest.json: invalid dispatch files'])
    for name in sorted(set(by_name) ^ {'dispatch-manifest.json', *recorded}):
        errors.append(name + ': dispatch member missing or extra')
    for name, entry in recorded.items():
        if not safe(name):
            errors.append(repr(name) + ': unsafe dispatch path')
            continue
        member = by_name.get(name)
        if member is None:
            continue
        if not isinstance(entry, dict):
            errors.append(name + ': malformed dispatch entry')
            continue
        if (type(entry.get('size')) is not int or entry['size'] < 0
                or member.size != entry['size']):
            errors.append(name + ': dispatch size mismatch')
        if (type(entry.get('mode')) is not int or not 0 <= entry['mode'] <= 0o777
                or member.mode != entry['mode']):
            errors.append(name + ': dispatch mode mismatch')
        with archive.extractfile(member) as source:
            if sha(source) != entry.get('sha256'):
                errors.append(name + ': dispatch member digest mismatch')
        destination = root / name
        if any(p.is_symlink() for p in (destination, *destination.parents)):
            errors.append(name + ': dispatch destination is a symlink')
        elif destination.exists() or any(p.exists() and not p.is_dir() for p in destination.parents):
            errors.append(name + ': dispatch destination already exists or has a non-directory parent')
        elif any(parent.as_posix() in recorded for parent in Path(name).parents):
            errors.append(name + ': dispatch file/directory conflict')
    if errors:
        refuse(errors)
    for name, entry in sorted(recorded.items()):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as target, archive.extractfile(by_name[name]) as source:
            for chunk in iter(lambda: source.read(1 << 20), b''):
                target.write(chunk)
        os.chmod(destination, entry['mode'])
        with destination.open('rb') as stream:
            if (sha(stream) != entry['sha256'] or os.fstat(stream.fileno()).st_size != entry['size']
                    or stat.S_IMODE(os.fstat(stream.fileno()).st_mode) != entry['mode']):
                refuse([name + ': dispatch extracted digest/size/mode mismatch'])
os.unlink(bundle)
print(json.dumps({'status': 'DISPATCH_VERIFIED', 'files': recorded}, sort_keys=True))
'''
