"""Read a private native-definition preimage without following path links.

These observations do not establish manager identity, containment or authority
for a lifecycle transfer. A caller must independently verify those facts and
must not treat this read as a compare-and-publish primitive.
"""

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path


class PreimageUnavailable(ValueError):
    """A native preimage could not be read without ambiguity."""


@dataclass(frozen=True)
class NativePreimage:
    contents: bytes = field(repr=False)
    sha256: str
    device: int
    inode: int
    mode: int
    uid: int
    gid: int


def read_native_preimage(path: Path, *, max_bytes: int = 1024 * 1024) -> NativePreimage:
    """Capture one existing, owned regular file; refuse ambiguous observations.

    Parent components and the leaf are opened with no-follow semantics. An
    object replaced or modified during the read is refused. Subsequent changes
    remain possible and require separate native and publication checks.
    """
    path = Path(path)
    if (not path.is_absolute() or '..' in path.parts or path.name in ('', '.', '..')
            or type(max_bytes) is not int or max_bytes < 0):
        raise PreimageUnavailable('invalid native preimage path or limit')
    if not all(hasattr(os, flag) for flag in ('O_NOFOLLOW', 'O_DIRECTORY', 'geteuid')):
        raise PreimageUnavailable('no safe no-follow file observation')
    directories = []
    leaf = None
    try:
        directories.append(os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for component in path.parts[1:-1]:
            directories.append(os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=directories[-1]))
        directory = directories[-1]
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)
        before = os.fstat(leaf)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_size > max_bytes):
            raise PreimageUnavailable('native preimage is not an owned bounded regular file')
        chunks = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(leaf, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        contents = b''.join(chunks)
        after = os.fstat(leaf)
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        for parent, child, component in zip(directories, directories[1:], path.parts[1:-1]):
            pinned = os.fstat(child)
            named = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if (pinned.st_dev, pinned.st_ino) != (named.st_dev, named.st_ino):
                raise PreimageUnavailable('native preimage parent changed during observation')
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
                  'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (len(contents) > max_bytes or len(contents) != before.st_size
                or any(getattr(before, field) != getattr(after, field) for field in fields)
                or any(getattr(after, field) != getattr(current, field) for field in fields)):
            raise PreimageUnavailable('native preimage changed during observation')
        return NativePreimage(contents, hashlib.sha256(contents).hexdigest(), before.st_dev,
                              before.st_ino, stat.S_IMODE(before.st_mode), before.st_uid,
                              before.st_gid)
    except OSError as exc:
        raise PreimageUnavailable('native preimage could not be safely observed') from exc
    finally:
        if leaf is not None:
            os.close(leaf)
        for directory in reversed(directories):
            os.close(directory)
