"""Dependency-free executable lookup shared by bootstrap and pack installation."""

from __future__ import annotations

import ntpath
import os
import posixpath


def _path_entries(path_value: str, *, windows: bool, cwd: str | os.PathLike[str]) -> list[str]:
    """Absolute PATH entries only: never the working directory, relative or empty entries."""
    module = ntpath if windows else posixpath
    here = module.normcase(module.normpath(os.fspath(cwd)))
    entries = []
    for entry in path_value.split(";" if windows else os.pathsep):
        # The host's own check: ntpath on Windows, posixpath elsewhere. A relative
        # entry such as \\x would name a directory inside the working directory.
        if not entry or not os.path.isabs(entry):
            continue
        if module.normcase(module.normpath(entry)) == here:
            continue
        entries.append(entry)
    return entries


def _find_executable(path_value: str, names: tuple[str, ...], *, windows: bool,
                     cwd: str | os.PathLike[str]) -> str | None:
    for entry in _path_entries(path_value, windows=windows, cwd=cwd):
        for name in names:
            candidate = os.path.join(entry, name)
            if os.path.isfile(candidate) and (windows or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _git_executable(path_value: str, *, windows: bool, cwd: str | os.PathLike[str],
                    recorded: str | None = None) -> str | None:
    """Find git: the install-recorded executable first, then absolute PATH entries only.

    shutil.which searches the current directory first on Windows, so a planted
    git.exe in the working tree would run. A recorded path must be absolute and
    an existing executable file; anything else falls through to the PATH lookup.
    """
    if (recorded and os.path.isabs(recorded) and os.path.isfile(recorded)
            and (windows or os.access(recorded, os.X_OK))):
        return recorded
    return _find_executable(path_value, ("git.exe",) if windows else ("git",), windows=windows, cwd=cwd)


