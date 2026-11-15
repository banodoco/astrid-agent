"""Whether Windows long paths are enabled, for generation that writes deep run directories.

Generation under ``%LOCALAPPDATA%`` runs past the classic 260-character limit unless the
machine has ``LongPathsEnabled`` set. Unknown (unreadable registry) is ``None``: never block on it.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

LONG_PATHS_MESSAGE = "\n".join((
    "Generating with your own ComfyUI on Windows currently needs Windows long paths to be enabled.",
    "Enable them once, in PowerShell opened as Administrator: New-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force",
    "Then restart Astrid (sign out and in, or restart the computer) and run the same command again.",
))

_KEY = r"SYSTEM\CurrentControlSet\Control\FileSystem"
_VALUE = "LongPathsEnabled"


def _read_registry_value() -> int:
    # Imported here: winreg exists only on Windows, and this module must import everywhere.
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _KEY) as key:  # type: ignore[attr-defined]
        value, _kind = winreg.QueryValueEx(key, _VALUE)  # type: ignore[attr-defined]
    return int(value)


def long_paths_enabled(platform_name: str | None = None,
                       reader: Callable[[], int] | None = None) -> bool | None:
    """True or False on Windows when the setting is readable; None off Windows or when unknown."""
    if (sys.platform if platform_name is None else platform_name) != "win32":
        return None
    try:
        value = (reader or _read_registry_value)()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    return value == 1
