"""Content generations for the pinned compiler used by a resident pack."""
from __future__ import annotations

import hashlib
from pathlib import Path


def compiler_content_identity():
    import vibecomfy
    root = Path(vibecomfy.__file__).resolve().parent
    paths = [path for path in root.rglob("*") if path.is_file()
             and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}]
    # Editable checkouts need content validation too. Git is diagnostic here;
    # no subprocess or mtime-only witness replaces reading executable bytes.
    git = root.parent / ".git"
    if git.is_dir():
        paths.extend(path for name in ("HEAD", "config", "packed-refs")
                     if (path := git / name).is_file())
        if (git / "refs").is_dir():
            paths.extend(path for path in (git / "refs").rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError("resident compiler source contains a link")
        before = path.stat()
        payload = path.read_bytes()
        after = path.stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("resident compiler source changed during verification")
        digest.update(path.relative_to(root.parent).as_posix().encode())
        digest.update(b"\0" + hashlib.sha256(payload).digest())
    return digest.hexdigest()
