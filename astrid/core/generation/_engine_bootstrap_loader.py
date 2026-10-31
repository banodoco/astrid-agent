"""Isolated bootstrap program; executed as python3 -I -S -c, not imported."""
import base64
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import zipfile
import lzma
from pathlib import Path, PurePosixPath

SOURCE_LIMIT = 65536
ARCHIVE_LIMIT = 524288
SPEC_LIMIT = 16384
EXPECTED_SPEC = '@SPEC_SHA256@'


def fail():
    raise ValueError('invalid engine bootstrap')


if (sum(len(k.encode()) + len(os.environ.get(k, '').encode()) + 2
        for k in ('ASTRID_ENGINE_SOURCE', 'ASTRID_ENGINE_SPEC')) +
        sum(len(arg.encode()) + 1 for arg in sys.orig_argv) > 98304):
    fail()


def decode(name, limit):
    value = os.environ.pop(name)
    if not value.isascii() or len(value) > limit:
        fail()
    return base64.b64decode(value, validate=True)


spec_bytes = decode('ASTRID_ENGINE_SPEC', ((SPEC_LIMIT + 2) // 3) * 4)
if len(spec_bytes) > SPEC_LIMIT or hashlib.sha256(spec_bytes).hexdigest() != EXPECTED_SPEC:
    fail()
spec = json.loads(spec_bytes)
if set(spec) != {'argv', 'files', 'source_sha256'}:
    fail()
compressed = decode('ASTRID_ENGINE_SOURCE', SOURCE_LIMIT)
decoder = lzma.LZMADecompressor(memlimit=32 * 1024 * 1024)
archive = decoder.decompress(compressed, max_length=ARCHIVE_LIMIT + 1)
if len(archive) > ARCHIVE_LIMIT or not decoder.eof or decoder.unused_data:
    fail()
if hashlib.sha256(archive).hexdigest() != spec['source_sha256']:
    fail()
files = spec['files']
if not isinstance(files, dict) or not files:
    fail()
sys.dont_write_bytecode = True
with tempfile.TemporaryDirectory(prefix='astrid-engine-cli-') as directory:
    root = Path(directory)
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        members = bundle.infolist()
        names = [item.filename for item in members]
        if len(names) != len(set(names)) or set(names) != set(files):
            fail()
        total = 0
        for item in members:
            name = item.filename
            path = PurePosixPath(name)
            mode = item.external_attr >> 16
            if (not name or '\\' in name or path.is_absolute() or
                    any(part in ('', '.', '..') for part in name.split('/')) or
                    str(path) != name or ':' in name or
                    item.create_system != 3 or not stat.S_ISREG(mode) or
                    item.compress_type != zipfile.ZIP_STORED or item.flag_bits & 1 or
                    item.file_size > ARCHIVE_LIMIT):
                fail()
            total += item.file_size
            if total > ARCHIVE_LIMIT:
                fail()
            data = bundle.read(item)
            if len(data) != item.file_size or hashlib.sha256(data).hexdigest() != files[name]:
                fail()
            target = root.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(data)
    sys.path.insert(0, directory)
    from importlib.metadata import distribution
    dist = distribution('astrid')
    from astrid.version import __version__
    if dist.metadata.get('Name') != 'astrid' or dist.version != __version__:
        fail()
    entries = [entry for entry in dist.entry_points
               if entry.group == 'console_scripts' and entry.name == 'astrid']
    if len(entries) != 1 or entries[0].value != 'astrid.console:main':
        fail()
    sys.argv = ['astrid', *spec['argv']]
    raise SystemExit(entries[0].load()())
