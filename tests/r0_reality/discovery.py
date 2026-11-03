"""Source-shaped, explicitly modeled R0 bootstrap discovery and owner marker.

These constructors take caller-supplied values: they do not claim to reproduce
an observed host's records, query a process, or establish owner liveness.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any


def linux_birth_id(start_ticks: int) -> str:
    """Format Linux /proc/<pid>/stat field 22 (process start ticks)."""
    if isinstance(start_ticks, bool) or not isinstance(start_ticks, int) or start_ticks < 0:
        raise ValueError("start_ticks must be a nonnegative integer")
    return f"proc-start-ticks:{start_ticks}"


def windows_birth_id(creation_filetime: int) -> str:
    """Format the unsigned 64-bit GetProcessTimes creation FILETIME."""
    if (isinstance(creation_filetime, bool) or not isinstance(creation_filetime, int)
            or not 0 <= creation_filetime < 2**64):
        raise ValueError("creation_filetime must be an unsigned 64-bit integer")
    return f"windows-creation:{creation_filetime}"


def darwin_birth_id(start_seconds: int | float) -> str:
    """Format Darwin's process start seconds using the current local timezone/locale."""
    local = time.localtime(start_seconds)
    return ("ps-lstart:" + time.strftime("%a %b ", local) + f"{local.tm_mday:2d}"
            + time.strftime(" %H:%M:%S %Y", local))


@dataclass(frozen=True)
class R0Owner:
    pid: int
    process_birth_id: str
    runtime_instance_id: str
    realm_id: str
    realm_root: str

    def marker(self) -> dict[str, Any]:
        """The five fields written to runtime/instance.lock by R0."""
        return {"pid": self.pid, "process_birth_id": self.process_birth_id,
                "runtime_instance_id": self.runtime_instance_id, "realm_id": self.realm_id,
                "realm_root": self.realm_root}

    def discovery(self, *, endpoint: str, coordinator_epoch: Any,
                  protocol_version: str, schema_version: str,
                  capability_digest: str, credential_file: str,
                  advertised_at: float, worker_credential_file: str = "",
                  worker_credential_pending: bool = False, worker_actor: str = "",
                  worker_scopes: tuple[str, ...] = (),
                  door_endpoint: str | None = None,
                  door_origin: str | None = None) -> dict[str, Any]:
        """Build the bootstrap publication shape with caller-supplied metadata."""
        value = {"version": 1, "endpoint": endpoint, "pid": self.pid,
                 "process_birth_id": self.process_birth_id,
                 "runtime_instance_id": self.runtime_instance_id,
                 "active_realm": self.realm_id, "realm_root": self.realm_root,
                 "coordinator_epoch": coordinator_epoch,
                 "protocol_version": protocol_version, "schema_version": schema_version,
                 "capability_digest": capability_digest, "credential_file": credential_file,
                 "worker_credential_file": worker_credential_file,
                 "worker_credential_pending": worker_credential_pending,
                 "worker_actor": worker_actor, "worker_scopes": list(worker_scopes),
                 "advertised_at": advertised_at}
        if door_endpoint:
            if door_origin is None:
                raise ValueError("R0 door_endpoint requires door_origin")
            value.update(door_endpoint=door_endpoint, door_origin=door_origin)
        return value

    def leave_marker(self, runtime_support: Path) -> Path:
        """Write a modeled leftover marker; file existence is not proof of a held lock."""
        runtime_support.mkdir(parents=True, exist_ok=True)
        marker = runtime_support / "instance.lock"
        marker.write_text(json.dumps(self.marker()), encoding="utf-8")
        return marker


def captured_discovery(system: str) -> dict[str, Any]:
    """Return a fresh copy of the logged R0 discovery from Actions 41617035310.

    Accepted names are linux, macos/darwin, and windows/win32. Paths, PIDs and
    endpoints are historical observations and must never be used operationally.
    """
    name = {"linux": "linux", "macos": "macos", "darwin": "macos",
            "windows": "windows", "win32": "windows"}.get(system)
    if name is None:
        raise ValueError(f"unsupported platform: {system}")
    return json.loads((Path(__file__).parent / "captured" / f"{name}.json").read_text(encoding="utf-8"))
