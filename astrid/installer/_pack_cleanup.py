"""Removal of private pack trees, including Git's read-only object files."""

import os
import shutil
import stat


def remove_pack_tree(path, *, ignore_errors=False):
    """Remove a tree, retrying permission failures after clearing read-only.

    ``onerror`` works on every supported Python version (3.11 and later).
    Other errors and failed retries remain fatal unless explicitly suppressed.
    """
    def retry(function, name, exc_info):
        if (not isinstance(exc_info[1], PermissionError)
                or function not in (os.unlink, os.remove, os.rmdir)):
            raise exc_info[1].with_traceback(exc_info[2])
        os.chmod(name, stat.S_IWRITE)
        function(name)

    try:
        shutil.rmtree(path, onerror=retry)
    except OSError:
        if not ignore_errors:
            raise
