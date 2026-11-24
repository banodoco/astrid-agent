"""Removal of private pack trees, including sealed installed package trees."""

import os
import shutil
import stat
import time


def _is_windows():
    return os.name == "nt"


def remove_pack_tree(path, *, ignore_errors=False):
    """Remove a tree, retrying permission failures after clearing read-only.

    ``onerror`` works on every supported Python version (3.11 and later).
    POSIX needs write access to parent directories, not files. Restore it
    top-down using directory descriptors, never through a symlink.
    Other errors and failed retries remain fatal unless explicitly suppressed.
    """
    def retry(function, name, exc_info):
        if (not isinstance(exc_info[1], PermissionError)
                or function not in (os.unlink, os.remove, os.rmdir)):
            raise exc_info[1].with_traceback(exc_info[2])
        if _is_windows():
            # Only Windows needs the read-only bit cleared; on POSIX removal depends
            # on the parent directory, and chmod would follow symlinks out of the tree.
            mode = os.lstat(name).st_mode
            if not stat.S_ISLNK(mode):
                os.chmod(name, mode | stat.S_IWRITE)
        function(name)

    try:
        if not _is_windows():
            # Sealed install directories are readable/searchable, but unlinking
            # their children requires owner write permission. fwalk never
            # follows a directory symlink and fchmod acts on its opened inode.
            def fail(error):
                raise error

            # Repair the root before fwalk tries to open it. For each child
            # use the parent's descriptor before descent; unreadable children
            # would otherwise fail in fwalk before its callback can see them.
            access = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
            root_mode = os.lstat(path).st_mode
            if stat.S_ISDIR(root_mode) and root_mode & access != access:
                os.chmod(path, stat.S_IMODE(root_mode) | access, follow_symlinks=False)
            for _name, dirs, _files, directory in os.fwalk(
                path, topdown=True, follow_symlinks=False, onerror=fail,
            ):
                for child in dirs:
                    mode = os.stat(child, dir_fd=directory, follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode) and mode & access != access:
                        os.chmod(child, stat.S_IMODE(mode) | access,
                                 dir_fd=directory, follow_symlinks=False)
        shutil.rmtree(path, onerror=retry)
    except OSError:
        if not ignore_errors:
            raise


def remove_tree_after_release(path, *, attempts=6, delay=0.25, sleep=time.sleep):
    """Remove a just-used tree, riding out a brief Windows sharing violation.

    A virus scanner or a just-exited child can hold a freshly written file for a
    moment; on Windows that surfaces as PermissionError, so the removal is retried
    a few times with a short, growing pause. POSIX has no such lock, so there the
    first failure is final.
    """
    for attempt in range(1, attempts + 1):
        try:
            return remove_pack_tree(path)
        except PermissionError:
            if not _is_windows() or attempt == attempts:
                raise
            sleep(delay * attempt)
