"""Canonical, persisted environment for every managed installer launch."""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

from runtime_protocol.door import DEFAULT_DOOR_PORT


_PORT_RE = re.compile(r"[1-9][0-9]{0,4}")
_FIRST_UNPRIVILEGED_PORT = 1024
_LAST_PORT = 65535
# Keep the search deterministic: the first available port after Runtime's
# historic default wins, and a fully occupied reservation window is an error.
_DOOR_PORT_CANDIDATES = 256


def runtime_door_port(value: str | int) -> str:
    """Validate and normalize one persisted loopback Runtime port."""
    text = str(value)
    if _PORT_RE.fullmatch(text) is None:
        raise ValueError("The Runtime door port must be a decimal TCP port.")
    port = int(text)
    if not _FIRST_UNPRIVILEGED_PORT <= port <= _LAST_PORT:
        raise ValueError("The Runtime door port must be an unprivileged TCP port.")
    return str(port)


def door_port_available(port: str | int) -> bool:
    """Probe the exact loopback bind Runtime will need, without reusing it."""
    number = int(runtime_door_port(port))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        # Windows otherwise permits another SO_REUSEADDR listener to obscure a
        # collision. POSIX does not expose this option, so leave its default.
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            listener.bind(("127.0.0.1", number))
        except OSError:
            return False
    return True


def fresh_runtime_door_port(requested: str | None = None) -> str:
    """Select a free Runtime door port for a fresh installation.

    An explicit installer request is a pin, so accepting a busy requested port
    would hide a configuration error. The implicit historic default instead
    starts a deterministic scan at 17340 and records the first free result.
    """
    if requested is not None:
        port = runtime_door_port(requested)
        if not door_port_available(port):
            raise ValueError(f"The requested Runtime door port {port} is unavailable.")
        return port
    first = int(runtime_door_port(DEFAULT_DOOR_PORT))
    last = min(_LAST_PORT, first + _DOOR_PORT_CANDIDATES - 1)
    for port in range(first, last + 1):
        if door_port_available(port):
            return str(port)
    raise ValueError("No free Runtime door port is available in the installer reservation range.")


def retained_tools_environment(version_root: Path, python: Path) -> dict[str, str]:
    """Reconstruct a legacy version's owned tool PATH without ambient lookup."""
    tools = Path(version_root) / "tools"
    suffix = ".exe" if os.name == "nt" else ""
    directories = [tools / "omp" / "bin"]
    environment: dict[str, str] = {}
    for folder, name, key in (
        (tools / "bun", "bun" + suffix, "ASTRID_BUN_EXECUTABLE"),
        (tools / "node", "node" + suffix, "ASTRID_NODE_EXECUTABLE"),
        (tools / "ffmpeg", "ffmpeg" + suffix, "ASTRID_FFMPEG_EXECUTABLE"),
        (tools / "ffmpeg", "ffprobe" + suffix, "ASTRID_FFPROBE_EXECUTABLE"),
    ):
        matches = sorted(path for path in folder.rglob(name) if path.is_file()) if folder.is_dir() else []
        if len(matches) == 1:
            environment[key] = str(matches[0])
            if matches[0].parent not in directories:
                directories.append(matches[0].parent)
    path = [python.parent, *(directory for directory in directories if directory.is_dir())]
    environment["PATH"] = os.pathsep.join([*(str(directory) for directory in path), os.defpath])
    return environment


def canonical_environment(environment: Mapping[str, str], data: Path, *, home: Path | None = None) -> dict[str, str]:
    """Return the single environment rendered into all installer launch forms.

    ``Version.env`` remains the durable source for the tool chain and Runtime
    port. Profile roots and the data root are overwritten deliberately so a
    launchd/systemd/task-manager environment can never choose a different
    profile merely because its manager starts with a sparse environment.
    """
    env = dict(environment)
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()):
        raise ValueError("The installer environment must contain string keys and values.")
    data_text = str(data)
    if not (Path(data_text).is_absolute() or PureWindowsPath(data_text).is_absolute()):
        raise ValueError("The Runtime data root must be absolute.")
    configured_home = env.get("HOME") or home or Path.home()
    home_text = str(configured_home)
    profile_home = Path(home_text).expanduser()
    if not profile_home.is_absolute() and PureWindowsPath(home_text).is_absolute():
        profile_home = PureWindowsPath(home_text)
    if not profile_home.is_absolute():
        raise ValueError("The installer HOME must be absolute.")
    port = runtime_door_port(env.get("ASTRID_RUNTIME_DOOR_PORT", str(DEFAULT_DOOR_PORT)))
    # A legacy Version without PATH is still launchable. Fresh and repaired
    # versions always supply install_tools()' pinned path before reaching here.
    path = env.get("PATH") or os.defpath
    env.update({
        "HOME": str(profile_home),
        "USERPROFILE": str(profile_home),
        "XDG_CONFIG_HOME": str(profile_home / ".config"),
        "XDG_DATA_HOME": str(profile_home / ".local" / "share"),
        "LOCALAPPDATA": str(profile_home / "AppData" / "Local"),
        "APPDATA": str(profile_home / "AppData" / "Roaming"),
        "ASTRID_RUNTIME_DATA_ROOT": data_text,
        "ASTRID_RUNTIME_DOOR_PORT": port,
        "PATH": path,
    })
    return env
