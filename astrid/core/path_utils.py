"""Small helpers for distinguishing native paths from URL references."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")


def is_network_share_path(value: str | os.PathLike[str]) -> bool:
    """Recognize Windows UNC and device namespace paths without accessing them."""
    # Both separators are accepted by Windows, including mixed and extended UNC.
    return os.fspath(value).replace("\\", "/").startswith("//")


def is_native_absolute_path(
    value: str | os.PathLike[str], *, windows: bool | None = None
) -> bool:
    """Return whether *value* is absolute according to the current OS."""

    text = os.fspath(value)
    use_windows = os.name == "nt" if windows is None else windows
    path_module = ntpath if use_windows else posixpath
    return path_module.isabs(text)


def has_url_scheme(value: str) -> bool:
    """Recognize URLs without mistaking a native drive path for a scheme."""

    if is_native_absolute_path(value) or _WINDOWS_DRIVE_PATH.match(value):
        return False
    return bool(urlsplit(value).scheme)


def file_uri_to_path(value: str, *, windows: bool | None = None) -> Path:
    """Decode a file URI into a native path, including ``/C:/...`` on Windows."""

    parsed = urlsplit(value)
    if parsed.scheme.lower() != "file":
        raise ValueError("expected a file URI")
    if _INVALID_PERCENT_ESCAPE.search(parsed.path):
        raise ValueError("file URI has a malformed percent escape")
    # Validate percent escapes as UTF-8 before passing the encoded URI path to
    # the platform converter, which performs the single actual unquote.
    unquote(parsed.path, errors="strict")
    use_windows = os.name == "nt" if windows is None else windows
    if use_windows:
        import nturl2path

        decoded_path = nturl2path.url2pathname(parsed.path)
    else:
        from urllib.request import url2pathname

        decoded_path = url2pathname(parsed.path)
    return Path(decoded_path)
