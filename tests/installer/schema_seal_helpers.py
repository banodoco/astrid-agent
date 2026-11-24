"""Shared test-only forging of an installed schema with the seal restored.

The helper is also loaded by path in the isolated installed interpreter. It
has no pytest or product imports and does not change that interpreter's path.
"""

from __future__ import annotations

import stat
from contextlib import contextmanager


@contextmanager
def _temporary_schema_write_access(path):
    """Test-only permission scope: restore the seal even if forging raises.

    A missing file is intentionally missing and a forged symlink is left for
    the reader to reject. Never chmod the target of such a symlink.
    """
    parent_mode = stat.S_IMODE(path.parent.lstat().st_mode)
    original = path.lstat()
    if not stat.S_ISREG(original.st_mode):
        raise ValueError("The schema being forged must start as a regular file.")
    file_mode = stat.S_IMODE(original.st_mode)
    path.parent.chmod(parent_mode | stat.S_IWUSR)
    try:
        path.chmod(file_mode | stat.S_IWUSR)
        yield
    finally:
        try:
            try:
                info = path.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISREG(info.st_mode):
                    path.chmod(file_mode)
        finally:
            path.parent.chmod(parent_mode)


def _forge_installed_file(path, how, target=None):
    """Forge only the named corrupt fixture and restore its permission rule."""
    with _temporary_schema_write_access(path):
        if how == "missing":
            path.unlink()
        elif how == "symlink":
            path.unlink()
            path.symlink_to(target)
        elif how == "mismatch":
            path.write_text("{}", encoding="utf-8")
        else:
            raise ValueError(f"Unknown schema forging operation: {how}")
