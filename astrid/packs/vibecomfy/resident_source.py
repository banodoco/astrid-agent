"""Content generations for the pinned compiler used by a resident pack."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path


def compiler_content_identity():
    import vibecomfy
    from vibecomfy import resources

    root = Path(vibecomfy.__file__).resolve().parent
    paths = [path for path in root.rglob("*") if path.is_file()
             and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}]
    # Editable installs keep their catalogue beside the package. These exact
    # resource bytes also affect static declarations and prepared workflows;
    # observing only Python modules would let a warm cache outlive the data.
    resources_used = [resources._resource_location(name) for name in (
        "template_index", "coverage_manifest", "custom_nodes_lock", "strict_ready_exceptions", "comfy_metadata")]
    paths.extend(path for path in (*resources_used, root.parent / "pyproject.toml") if path.is_file())
    # Editable checkouts need content validation too. Git is diagnostic here;
    # no subprocess or mtime-only witness replaces reading executable bytes.
    git = root.parent / ".git"
    if git.is_dir():
        paths.extend(path for name in ("HEAD", "config", "packed-refs")
                     if (path := git / name).is_file())
        if (git / "refs").is_dir():
            paths.extend(path for path in (git / "refs").rglob("*") if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
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


def advisory_repository_revision():
    """Read the fixed cwd's Git HEAD without creating a native descendant.

    This is optional receipt metadata, never source attestation. Ordinary
    checkouts, detached HEADs and linked worktrees have an explicit on-disk
    identity. Unsupported discovery overrides or malformed metadata stay
    unknown, as they do when the producer's advisory Git command fails.
    """
    if any(os.environ.get(name) for name in (
            "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_NAMESPACE",
            "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM")):
        return None

    def read(path, limit=1024 * 1024):
        if path.is_symlink():
            raise ValueError("symbolic Git metadata is not an advisory identity")
        with path.open("rb") as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise ValueError("Git metadata exceeds the advisory read bound")
        return value.decode("utf-8").strip()

    def oid(value):
        return value if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) else None

    try:
        cwd = Path.cwd()
        device = cwd.stat().st_dev
        for parent in (cwd, *cwd.parents):
            if parent.stat().st_dev != device:
                return None
            marker = parent / ".git"
            if marker.is_symlink():
                return None
            if marker.is_dir():
                git = marker
                break
            if marker.is_file():
                value = read(marker, 16 * 1024)
                if not value.startswith("gitdir: "):
                    return None
                git = (parent / value.removeprefix("gitdir: ")).resolve(strict=True)
                break
        else:
            return None
        common = git
        if (git / "commondir").is_file():
            common = (git / read(git / "commondir", 16 * 1024)).resolve(strict=True)
        value = read(git / "HEAD", 16 * 1024)
        for _ in range(8):
            if oid(value):
                return value
            if not value.startswith("ref: refs/"):
                return None
            ref = value.removeprefix("ref: ")
            if ("\\" in ref or ":" in ref or any(ord(char) < 32 for char in ref)
                    or any(part in {"", ".", ".."} for part in ref.split("/"))):
                return None
            loose = next((root / ref for root in (git, common) if (root / ref).is_file()), None)
            if loose is not None:
                value = read(loose, 16 * 1024)
                continue
            rows = [line.split(" ", 1) for line in read(common / "packed-refs").splitlines()
                    if not line.startswith(("#", "^"))]
            matches = [revision for revision, name in rows if name == ref]
            return oid(matches[0]) if len(matches) == 1 else None
    except (OSError, ValueError, UnicodeError):
        return None
    return None
