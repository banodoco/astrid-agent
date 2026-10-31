#!/usr/bin/env python3
"""Collect provisional, fail-closed native M4-6 platform observations.

The disposable executor is intentionally not imported or promoted. An approved
hosted driver must create the real product-owned service definition and execute
the four scenarios through :class:`NativeSession`.

Serialized observations contain structural hashes, never authentication. Hosted
publication therefore requires a separately registered B/platform-owned verifier;
this adapter has no default verifier and cannot authenticate itself. Injected
collectors remain structural fixtures and are never submitted for hosted admission.
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Final, Mapping, Protocol, Sequence

if __package__:
    from . import journey_failure_modes as failure
else:
    try:
        import journey_failure_modes as failure
    except ModuleNotFoundError:
        from scripts import journey_failure_modes as failure


SERVICE_LABEL: Final = "haus.astrid.runtime"
SYSTEMD_UNIT: Final = "astrid.service"
WINDOWS_TASK_NAME: Final = "Astrid Runtime"
WINDOWS_TASK_PATH: Final = "\\"
DATA_IDENTITY_SCHEMA: Final = "astrid-native-data-identity-v1"
DURABLE_JOURNAL_SCHEMA: Final = "astrid-runtime-operation-journal-v1"
HOSTED_SCOPE: Final = "hosted-native-service"
FIXTURE_SCOPE: Final = "platform-fixture; not hosted OS evidence"
MANAGERS: Final = {
    "macos": "launchd",
    "linux": "systemd-user",
    "windows": "task-scheduler",
}
SYNTHETIC_IMAGE = re.compile(r"(?:local|fixture|fake|synthetic|test)", re.IGNORECASE)
WINDOWS_PROCESS_BIRTH_ID = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{7}Z"
)
REQUIRED_NATIVE_SLOT_IDS: Final = (
    "direct-macos",
    "direct-linux",
    "direct-windows",
    "pinokio-backend-macos",
    "pinokio-backend-linux",
    "pinokio-backend-windows",
    "pinokio-fully-local-macos",
    "pinokio-fully-local-linux",
    "pinokio-fully-local-windows",
)
INSTANCE_IDENTITY_FIELDS: Final = (
    "state",
    "manager_instance_id",
    "pid",
    "process_birth_id",
    "runtime_instance_id",
    "executable_path",
    "executable_sha256",
    "data_root",
    "data_identity_sha256",
    "workspace_identity",
    "semantic_data_sha256",
    "data_manifest_path",
    "data_manifest_sha256",
    "data_root_identity_sha256",
)
DATA_IDENTITY_FIELDS: Final = INSTANCE_IDENTITY_FIELDS[7:]


def _verified_release0_fixture(path: Path) -> tuple[Path, Mapping[str, Any], dict[str, Any]]:
    """Lazy import keeps the standalone ``--help`` path dependency-free."""
    if __package__:
        from .journey_version_jumps import verified_release0_fixture
    else:
        try:
            from journey_version_jumps import verified_release0_fixture
        except ModuleNotFoundError:
            from scripts.journey_version_jumps import verified_release0_fixture
    return verified_release0_fixture(path)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_windows_process_birth_id(value: object) -> bool:
    if not isinstance(value, str) or WINDOWS_PROCESS_BIRTH_ID.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return True


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _instance_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {name: value.get(name) for name in INSTANCE_IDENTITY_FIELDS}


def _data_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    return {name: value.get(name) for name in DATA_IDENTITY_FIELDS}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _xml_descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element.iter() if _local_name(child.tag) == name]


def _xml_text(parent: ET.Element, name: str, *, required: bool = True) -> str:
    children = _xml_children(parent, name)
    if len(children) > 1:
        raise RuntimeError(f"Scheduled Task XML has multiple {name} values")
    if not children:
        if required:
            raise RuntimeError(f"Scheduled Task XML requires exactly one {name}")
        return ""
    return (children[0].text or "").strip()


def _task_xml_semantics(xml: str) -> dict[str, str]:
    """Return required Task Scheduler semantics, ignoring formatting/namespaces."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise RuntimeError("Scheduled Task XML is malformed") from exc
    if _local_name(root.tag) != "Task":
        raise RuntimeError("Scheduled Task XML root is not Task")
    actions_nodes = _xml_descendants(root, "Actions")
    principals_nodes = _xml_descendants(root, "Principals")
    settings_nodes = _xml_descendants(root, "Settings")
    if len(actions_nodes) != 1 or len(principals_nodes) != 1 or len(settings_nodes) != 1:
        raise RuntimeError("Scheduled Task XML action/principal/settings container is ambiguous")
    action_children = list(actions_nodes[0])
    exec_nodes = _xml_children(actions_nodes[0], "Exec")
    principal_nodes = _xml_children(principals_nodes[0], "Principal")
    if (
        len(action_children) != 1
        or _local_name(action_children[0].tag) != "Exec"
        or len(exec_nodes) != 1
        or len(principal_nodes) != 1
    ):
        raise RuntimeError("Scheduled Task XML requires one action and principal")
    execute = _xml_text(exec_nodes[0], "Command")
    arguments = _xml_text(exec_nodes[0], "Arguments", required=False)
    working = _xml_text(exec_nodes[0], "WorkingDirectory", required=False)
    principal = _xml_text(principal_nodes[0], "UserId")
    multiple_instances = _xml_text(settings_nodes[0], "MultipleInstancesPolicy")
    disallow_battery = _xml_text(settings_nodes[0], "DisallowStartIfOnBatteries")
    stop_on_battery = _xml_text(settings_nodes[0], "StopIfGoingOnBatteries")
    if not execute or not principal:
        raise RuntimeError("Scheduled Task XML action/principal is empty")
    return {
        "execute": execute,
        "arguments": arguments,
        "working_directory": working,
        "principal": principal,
        "multiple_instances_policy": multiple_instances,
        "disallow_start_on_batteries": disallow_battery.lower(),
        "stop_on_batteries": stop_on_battery.lower(),
    }


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str], *, timeout: int = 30) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(self, argv: Sequence[str], *, timeout: int = 30) -> CommandResult:
        try:
            completed = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"native command could not run: {argv[0]}") from exc
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class ProcessProbe(Protocol):
    def observe(
        self, pid: int, *, os_name: str, expected_executable: Path
    ) -> Mapping[str, Any]: ...

    def incarnation_absent(self, pid: int, birth_id: str, *, os_name: str) -> bool: ...

    def owned_action_processes(
        self, action: Sequence[str], *, os_name: str
    ) -> Sequence[Mapping[str, Any]]: ...


def _read_bounded_json(
    path: Path, allowed_root: Path, *, label: str
) -> tuple[dict[str, Any], bytes]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} is absent or unsafe")
    canonical = path.resolve(strict=True)
    if canonical != path.absolute() or not canonical.is_relative_to(allowed_root):
        raise RuntimeError(f"{label} is outside the run-owned root")
    data = canonical.read_bytes()
    if not data or len(data) > 64 * 1024:
        raise RuntimeError(f"{label} size is invalid")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} must be an object")
    return value, data


class DataProbe:
    """Adapter-owned reader for the product's precise data-identity manifest."""

    def __init__(self, allowed_root: Path) -> None:
        self.allowed_root = allowed_root.resolve(strict=True)

    def observe(self, discovery: Mapping[str, Any]) -> dict[str, Any]:
        try:
            root = Path(discovery["data_root"])
            manifest_path = Path(discovery["data_manifest_path"])
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Runtime data manifest discovery is incomplete") from exc
        if (
            not root.is_absolute()
            or root.is_symlink()
            or not root.is_dir()
            or root.resolve(strict=True) != root.absolute()
            or not root.resolve(strict=True).is_relative_to(self.allowed_root)
            or manifest_path.parent != root
            or manifest_path.name != "native-data-identity.json"
        ):
            raise RuntimeError("Runtime data root/manifest is noncanonical or unsafe")
        manifest, raw = _read_bounded_json(
            manifest_path, self.allowed_root, label="Runtime data identity manifest"
        )
        if set(manifest) != {"schema", "workspace_identity", "entities"}:
            raise RuntimeError("Runtime data identity manifest fields differ")
        workspace = manifest.get("workspace_identity")
        entities = _mapping(manifest.get("entities"))
        if (
            manifest.get("schema") != DATA_IDENTITY_SCHEMA
            or not _nonempty(workspace)
            or set(entities) != set(failure.DATA_ENTITIES)
            or any(not _digest(entities.get(name)) for name in failure.DATA_ENTITIES)
        ):
            raise RuntimeError("Runtime data identity manifest semantics differ")
        entity_rows = {
            name: {
                "before_sha256": entities[name],
                "after_sha256": entities[name],
            }
            for name in failure.DATA_ENTITIES
        }
        data_identity = _sha256_bytes(_canonical(dict(entities)))
        semantic = {
            "before_sha256": data_identity,
            "after_sha256": data_identity,
            "identical": True,
            "entities": entity_rows,
        }
        observed = {
            "data_root": str(root.resolve(strict=True)),
            "data_identity_sha256": data_identity,
            "workspace_identity": workspace,
            "semantic_data_sha256": _sha256_bytes(_canonical(semantic)),
            "data_manifest_path": str(manifest_path.resolve(strict=True)),
            "data_manifest_sha256": _sha256_bytes(raw),
            "data_root_identity_sha256": _sha256_bytes(
                _canonical(
                    {
                        "data_root": str(root.resolve(strict=True)),
                        "manifest_sha256": _sha256_bytes(raw),
                    }
                )
            ),
        }
        if any(discovery.get(name) != value for name, value in observed.items()):
            raise RuntimeError("Runtime discovery differs from adapter-observed data identity")
        return {**observed, "semantic_data": semantic}


class DurableJournalProbe:
    """Adapter-owned bounded reader for the durable update journal."""

    def __init__(self, allowed_root: Path) -> None:
        self.allowed_root = allowed_root.resolve(strict=True)

    def observe(self, discovery: Mapping[str, Any]) -> dict[str, str]:
        try:
            path = Path(discovery["durable_journal_path"])
        except (KeyError, TypeError) as exc:
            raise RuntimeError("external controller durable journal path is absent") from exc
        journal, raw = _read_bounded_json(
            path, self.allowed_root, label="external controller durable journal"
        )
        if set(journal) != {"schema", "operation_id", "phase"}:
            raise RuntimeError("external controller durable journal fields differ")
        observed = {
            "runtime_operation_id": journal.get("operation_id"),
            "durable_phase": journal.get("phase"),
            "durable_journal_path": str(path.resolve(strict=True)),
            "durable_journal_sha256": _sha256_bytes(raw),
        }
        if (
            journal.get("schema") != DURABLE_JOURNAL_SCHEMA
            or not _nonempty(observed["runtime_operation_id"])
            or not _nonempty(observed["durable_phase"])
            or any(discovery.get(name) != value for name, value in observed.items())
        ):
            raise RuntimeError("external controller discovery differs from durable journal")
        return observed


class LiveProcessProbe:
    """Exact process probes; Runtime discovery remains a separate source."""

    def __init__(self, runner: CommandRunner, powershell: Path | None) -> None:
        self.runner = runner
        self.powershell = powershell

    @staticmethod
    def _linux_identity(pid: int) -> tuple[str, Path]:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2 :].split()
        return fields[19], Path(f"/proc/{pid}/exe").resolve(strict=True)

    @staticmethod
    def _mac_executable(pid: int) -> Path:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        buffer = ctypes.create_string_buffer(4096)
        length = library.proc_pidpath(pid, buffer, len(buffer))
        if length <= 0:
            raise OSError(ctypes.get_errno(), "proc_pidpath failed")
        return Path(os.fsdecode(buffer.value)).resolve(strict=True)

    def _windows_rows(self, script: str) -> list[dict[str, Any]]:
        if self.powershell is None:
            raise RuntimeError("validated Windows PowerShell path is unavailable")
        result = self.runner.run(
            [str(self.powershell), "-NoProfile", "-NonInteractive", "-Command", script]
        )
        if result.returncode:
            raise RuntimeError("Windows process observation failed")
        if not result.stdout.strip():
            raise RuntimeError("Windows process observation is empty")

        def object_without_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            row: dict[str, Any] = {}
            for name, value in pairs:
                if name in row:
                    raise ValueError(f"duplicate field: {name}")
                row[name] = value
            return row

        def reject_json_constant(value: str) -> None:
            raise ValueError(f"invalid JSON constant: {value}")

        try:
            value = json.loads(
                result.stdout,
                object_pairs_hook=object_without_duplicate_fields,
                parse_constant=reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("Windows process observation is malformed") from exc
        if not isinstance(value, list):
            raise RuntimeError("Windows process observation is ambiguous")

        required = {"ProcessId", "CreationDate", "ExecutablePath"}
        rows: list[dict[str, Any]] = []
        by_pid: dict[int, tuple[str, str]] = {}
        identities: set[tuple[int, str, str]] = set()
        for provider_row in value:
            if not isinstance(provider_row, dict) or set(provider_row) != required:
                raise RuntimeError("Windows process observation row fields differ")
            pid = provider_row["ProcessId"]
            birth = provider_row["CreationDate"]
            raw_executable = provider_row["ExecutablePath"]
            if type(pid) is not int or pid <= 0 or pid > 0xFFFFFFFF:
                raise RuntimeError("Windows process observation PID is invalid")
            if not _valid_windows_process_birth_id(birth):
                raise RuntimeError("Windows process observation birth identity is invalid")
            if (
                not _nonempty(raw_executable)
                or raw_executable != raw_executable.strip()
                or not Path(raw_executable).is_absolute()
            ):
                raise RuntimeError("Windows process observation executable path is invalid")
            try:
                executable = Path(raw_executable).resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise RuntimeError(
                    "Windows process observation executable path cannot be resolved"
                ) from exc
            if not executable.is_file():
                raise RuntimeError("Windows process observation executable is not a file")
            identity = (pid, birth, str(executable))
            if identity in identities:
                raise RuntimeError("Windows process observation contains a duplicate row")
            prior = by_pid.get(pid)
            if prior is not None and prior != identity[1:]:
                raise RuntimeError("Windows process observation has inconsistent PID rows")
            identities.add(identity)
            by_pid[pid] = identity[1:]
            rows.append(
                {
                    "pid": pid,
                    "process_birth_id": birth,
                    "executable_path": str(executable),
                }
            )
        return rows

    @staticmethod
    def _windows_json(script: str) -> str:
        """Force PowerShell to serialize the provider result as one JSON array."""
        return (
            "$ErrorActionPreference='Stop';"
            f"$rows=@({script});"
            "ConvertTo-Json -InputObject $rows -Compress"
        )

    @staticmethod
    def _windows_process_projection() -> str:
        return (
            "Select-Object ProcessId,"
            "@{Name='CreationDate';Expression={"
            "$_.CreationDate.ToUniversalTime().ToString('o')}},"
            "ExecutablePath"
        )

    def observe(self, pid: int, *, os_name: str, expected_executable: Path) -> Mapping[str, Any]:
        if (
            type(pid) is not int
            or pid <= 0
            or (os_name == "windows" and pid > 0xFFFFFFFF)
        ):
            raise RuntimeError("positive native Runtime PID is required")
        try:
            expected = expected_executable.resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise RuntimeError("expected Runtime executable cannot be resolved") from exc
        if not expected.is_file():
            raise RuntimeError("expected Runtime executable is not a file")
        try:
            if os_name == "linux":
                birth, executable = self._linux_identity(pid)
            elif os_name == "macos":
                executable = self._mac_executable(pid)
                result = self.runner.run(["ps", "-p", str(pid), "-o", "lstart="], timeout=10)
                if result.returncode or not result.stdout.strip():
                    raise RuntimeError("macOS process birth identity is unavailable")
                birth = result.stdout.strip()
            elif os_name == "windows":
                rows = self._windows_rows(
                    self._windows_json(
                        f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" | '
                        f"{self._windows_process_projection()}"
                    )
                )
                if len(rows) != 1:
                    raise RuntimeError(
                        "Windows Runtime process observation requires exactly one row"
                    )
                if rows[0]["pid"] != pid:
                    raise RuntimeError("Windows Runtime process PID differs")
                birth = rows[0]["process_birth_id"]
                executable = Path(rows[0]["executable_path"])
            else:
                raise RuntimeError(f"unsupported process platform: {os_name}")
        except (OSError, IndexError) as exc:
            raise RuntimeError("Runtime process identity is unavailable") from exc
        if executable != expected or not birth:
            raise RuntimeError("Runtime executable/birth identity differs")
        return {
            "pid": pid,
            "process_birth_id": birth,
            "executable_path": str(expected),
            "executable_sha256": _sha256_file(expected),
        }

    def incarnation_absent(self, pid: int, birth_id: str, *, os_name: str) -> bool:
        birth_is_invalid = (
            not _valid_windows_process_birth_id(birth_id)
            if os_name == "windows"
            else (
                not _nonempty(birth_id)
                or birth_id != birth_id.strip()
                or any(ord(character) < 0x20 for character in birth_id)
            )
        )
        if (
            type(pid) is not int
            or pid <= 0
            or (os_name == "windows" and pid > 0xFFFFFFFF)
            or birth_is_invalid
        ):
            raise RuntimeError("positive native process PID/birth identity is required")
        if os_name == "linux":
            try:
                current, _path = self._linux_identity(pid)
            except (FileNotFoundError, ProcessLookupError):
                return True
            except (OSError, IndexError) as exc:
                raise RuntimeError("Linux process absence is not observable") from exc
            return current != birth_id
        if os_name == "macos":
            result = self.runner.run(["ps", "-p", str(pid), "-o", "lstart="], timeout=10)
            if result.returncode == 1:
                return True
            if result.returncode or not result.stdout.strip():
                raise RuntimeError("macOS process absence is not observable")
            return result.stdout.strip() != birth_id
        if os_name != "windows":
            raise RuntimeError(f"unsupported process platform: {os_name}")
        rows = self._windows_rows(
            self._windows_json(
                f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" | '
                f"{self._windows_process_projection()}"
            )
        )
        if not rows:
            return True
        if len(rows) != 1:
            raise RuntimeError(
                "Windows process absence observation requires at most one row"
            )
        if rows[0]["pid"] != pid:
            raise RuntimeError("Windows process absence PID differs")
        return rows[0]["process_birth_id"] != birth_id

    def owned_action_processes(
        self, action: Sequence[str], *, os_name: str
    ) -> Sequence[Mapping[str, Any]]:
        executable = Path(action[0]).resolve(strict=True)
        rows: list[Mapping[str, Any]] = []
        if os_name == "linux":
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    birth, path = self._linux_identity(int(entry.name))
                except (FileNotFoundError, ProcessLookupError, IndexError):
                    continue
                except OSError as exc:
                    raise RuntimeError("Linux owned-process enumeration was denied") from exc
                if path == executable:
                    rows.append({"pid": int(entry.name), "process_birth_id": birth})
            return rows
        if os_name == "macos":
            library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            count = library.proc_listallpids(None, 0)
            values = (ctypes.c_int * max(count, 1))()
            actual = library.proc_listallpids(values, ctypes.sizeof(values))
            for pid in values[:actual]:
                try:
                    if self._mac_executable(pid) == executable:
                        result = self.runner.run(
                            ["ps", "-p", str(pid), "-o", "lstart="], timeout=10
                        )
                        if result.returncode == 1:
                            continue
                        if result.returncode or not result.stdout.strip():
                            raise RuntimeError("macOS owned-process birth query was denied")
                        birth = result.stdout.strip()
                        rows.append({"pid": pid, "process_birth_id": birth})
                except OSError as exc:
                    if exc.errno in {errno.ESRCH, errno.ENOENT}:
                        continue
                    raise RuntimeError("macOS owned-process enumeration was denied") from exc
            return rows
        executable_name = executable.name.replace("'", "''")
        observed = self._windows_rows(
            self._windows_json(
                "Get-CimInstance Win32_Process | "
                "Where-Object {(($null -ne $_.ExecutablePath -and "
                "$_.ExecutablePath -ne '') -or "
                f"$_.Name -eq '{executable_name}')}} | "
                f"{self._windows_process_projection()}"
            )
        )
        # Path equality follows the target filesystem: case-insensitive on hosted
        # Windows, while POSIX fixture paths retain their native case semantics.
        return [
            row for row in observed if Path(row["executable_path"]) == executable
        ]


@dataclass(frozen=True)
class ServiceSpec:
    """Exact product-created definition and action."""

    definition_path: Path
    definition_sha256: str
    action: tuple[str, ...]
    principal: str
    definition_owner: str
    working_directory: str = ""

    def __post_init__(self) -> None:
        if not _digest(self.definition_sha256):
            raise ValueError("service definition SHA-256 is required")
        if not self.action or not all(_nonempty(value) for value in self.action):
            raise ValueError("exact non-empty service action is required")
        if not _nonempty(self.principal):
            raise ValueError("exact authenticated principal is required")
        if self.definition_owner != "product-installer":
            raise ValueError("service definition must be owned by the product installer")


class JourneyDriver(Protocol):
    def service_spec(self, context: Mapping[str, Any]) -> ServiceSpec: ...

    def run(self, context: Mapping[str, Any], session: "NativeSession") -> Mapping[str, Any]: ...


class HostedAdmissionCapability(Protocol):
    """Opaque, one-use capability implemented by the external platform owner."""

    def consume(self, observation_sha256: str) -> bool: ...


class HostedAdmissionVerifier(Protocol):
    """B/platform-owned trust boundary; never implemented by this adapter."""

    def verify_live_observation(
        self, observation: Mapping[str, Any]
    ) -> HostedAdmissionCapability: ...


JOURNEY_DRIVER_FACTORY: Callable[[Mapping[str, Any]], JourneyDriver] | None = None
HOSTED_ADMISSION_VERIFIER: HostedAdmissionVerifier | None = None


def register_journey_driver(factory: Callable[[Mapping[str, Any]], JourneyDriver]) -> None:
    global JOURNEY_DRIVER_FACTORY
    if JOURNEY_DRIVER_FACTORY is not None:
        raise RuntimeError("native journey driver is already registered")
    JOURNEY_DRIVER_FACTORY = factory


def register_hosted_admission_verifier(verifier: HostedAdmissionVerifier) -> None:
    """Register the single external trust provider during B/platform integration."""
    global HOSTED_ADMISSION_VERIFIER
    if HOSTED_ADMISSION_VERIFIER is not None:
        raise RuntimeError("hosted admission verifier is already registered")
    if not callable(getattr(verifier, "verify_live_observation", None)):
        raise TypeError("hosted admission verifier does not implement the protocol")
    HOSTED_ADMISSION_VERIFIER = verifier


def _parse_assignments(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    return fields


def _launchd_arguments(output: str) -> list[str]:
    match = re.search(r"^\s*arguments\s*=\s*\{\s*$([\s\S]*?)^\s*\}\s*$", output, re.MULTILINE)
    if match is None:
        return []
    indexed: list[tuple[int, str]] = []
    for line in match.group(1).splitlines():
        item = re.match(r"^\s*(\d+)\s*=\s*(.*?)\s*$", line)
        if item is not None:
            indexed.append((int(item.group(1)), item.group(2)))
    indexed.sort()
    if [index for index, _value in indexed] != list(range(len(indexed))):
        return []
    return [value for _index, value in indexed]


def _systemd_argv(value: str) -> list[str]:
    match = re.search(r"\bargv\[\]=(.*?)(?:\s+;\s+\w+=)", value)
    if match is None:
        return []
    try:
        return shlex.split(match.group(1), posix=True)
    except ValueError:
        return []


class NativeOwner:
    """Native manager authority with exact revalidation before mutation."""

    def __init__(
        self,
        *,
        os_name: str,
        canonical_definition_path: Path | None,
        runner: CommandRunner,
        process_probe: ProcessProbe,
        data_probe: DataProbe,
        durable_journal_probe: DurableJournalProbe,
        powershell: Path | None,
    ) -> None:
        self.os_name = os_name
        self.manager = MANAGERS[os_name]
        self.definition_path = canonical_definition_path
        self.runner = runner
        self.process_probe = process_probe
        self.data_probe = data_probe
        self.durable_journal_probe = durable_journal_probe
        self.powershell = powershell
        self.raw: list[dict[str, Any]] = []
        self.spec: ServiceSpec | None = None
        self.principal = ""
        self.target = ""
        self.manager_version = ""
        self.manager_definition_sha256 = ""
        self.source_xml_sha256 = ""
        self.exported_xml_sha256 = ""
        self.task_semantic_sha256 = ""

    def _run(
        self,
        label: str,
        argv: Sequence[str],
        *,
        allowed: tuple[int, ...] = (0,),
        timeout: int = 30,
    ) -> CommandResult:
        result = self.runner.run(argv, timeout=timeout)
        self.raw.append(
            {
                "label": label,
                "command": Path(str(argv[0])).name,
                "argc": len(argv),
                "argv_sha256": _sha256_bytes(_canonical(list(argv))),
                "returncode": result.returncode,
                "stdout_bytes": len(result.stdout.encode()),
                "stdout_sha256": _sha256_bytes(result.stdout.encode()),
                "stderr_bytes": len(result.stderr.encode()),
                "stderr_sha256": _sha256_bytes(result.stderr.encode()),
            }
        )
        if result.returncode not in allowed:
            raise RuntimeError(f"native {label} failed with exit {result.returncode}")
        return result

    def _safe_parsed(self, values: Mapping[str, Any]) -> None:
        self.raw[-1]["parsed"] = copy.deepcopy(dict(values))

    def _ps(self, label: str, script: str) -> CommandResult:
        if self.powershell is None:
            raise RuntimeError("validated Windows PowerShell 5.1 is unavailable")
        return self._run(
            label,
            [str(self.powershell), "-NoProfile", "-NonInteractive", "-Command", script],
        )

    def probe(self) -> dict[str, Any]:
        if self.os_name in {"macos", "linux"}:
            uid = self._run("principal-uid", ["id", "-u"]).stdout.strip()
            user = self._run("principal-name", ["id", "-un"]).stdout.strip()
            if not uid.isdigit() or not user:
                raise RuntimeError("POSIX principal identity is unavailable")
            self.principal = user
            if self.os_name == "macos":
                result = self._run("launchd-version", ["launchctl", "version"])
                self.manager_version = result.stdout.strip().splitlines()[0]
                self._run(
                    "launchd-gui-capability",
                    ["launchctl", "asuser", uid, "/usr/bin/true"],
                )
                self.target = f"gui/{uid}/{SERVICE_LABEL}"
                self._safe_parsed({"gui_uid": int(uid), "available": True})
            else:
                result = self._run("systemd-version", ["systemctl", "--user", "--version"])
                self.manager_version = result.stdout.strip().splitlines()[0]
                match = re.search(r"\bsystemd\s+(\d+)\b", self.manager_version)
                if match is None or int(match.group(1)) < 254:
                    raise RuntimeError("systemd --user 254 or newer is required")
                session = self._run(
                    "systemd-session-capability",
                    ["systemctl", "--user", "is-system-running"],
                    allowed=(0, 1),
                )
                state = session.stdout.strip()
                if state not in {"running", "degraded", "starting"}:
                    raise RuntimeError("systemd user session is unavailable")
                self._safe_parsed({"state": state})
                self.target = SYSTEMD_UNIT
        else:
            result = self._ps(
                "task-scheduler-capability",
                "$ErrorActionPreference='Stop';$c=Get-Command Get-ScheduledTask;"
                "$s=New-Object -ComObject 'Schedule.Service';$s.Connect();"
                "$id=[System.Security.Principal.WindowsIdentity]::GetCurrent();"
                "[pscustomobject]@{PSVersion=$PSVersionTable.PSVersion.ToString();"
                "Module=$c.Source;SID=$id.User.Value;Com=$true} | ConvertTo-Json -Compress",
            )
            try:
                row = json.loads(result.stdout)
                version = str(row["PSVersion"])
                self.principal = str(row["SID"])
                module = str(row["Module"])
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Task Scheduler capability is malformed") from exc
            if (
                not version.startswith("5.1")
                or module != "ScheduledTasks"
                or row.get("Com") is not True
            ):
                raise RuntimeError("PowerShell 5.1 ScheduledTasks/COM capability is required")
            self.manager_version = f"PowerShell {version} / {module}"
            self.target = WINDOWS_TASK_PATH + WINDOWS_TASK_NAME
            self._safe_parsed({"version": version, "module": module, "sid": self.principal})
        if not self.manager_version:
            raise RuntimeError("native manager version is unavailable")
        return {
            "manager": self.manager,
            "manager_version": self.manager_version,
            "principal": self.principal,
            "target": self.target,
        }

    def _windows_query(self) -> Mapping[str, Any] | None:
        result = self._ps(
            "task-scheduler-query",
            "$ErrorActionPreference='Stop';"
            f"$tasks=@(Get-ScheduledTask | Where-Object {{$_.TaskName -eq '{WINDOWS_TASK_NAME}' "
            f"-and $_.TaskPath -eq '{WINDOWS_TASK_PATH}'}});"
            "$svc=New-Object -ComObject 'Schedule.Service';$svc.Connect();"
            "$folder=$svc.GetFolder('\\');$com=$null;"
            f"try {{$com=$folder.GetTask('{WINDOWS_TASK_NAME}')}} "
            "catch [System.Runtime.InteropServices.COMException] {"
            "if ($_.Exception.HResult -ne -2147024894) {throw}};"
            "if ($tasks.Count -eq 0 -and $null -eq $com) {"
            "[pscustomobject]@{TaskCount=0;ComPresent=$false} | ConvertTo-Json -Compress;exit 0};"
            "$actions=@($tasks | ForEach-Object {$_.Actions});"
            "$instances=@();if ($null -ne $com) {$instances=@($com.GetInstances(0) | ForEach-Object {"
            "[pscustomobject]@{InstanceGuid=$_.InstanceGuid;EnginePID=$_.EnginePID}})};"
            "$xml=if ($tasks.Count -eq 1) {Export-ScheduledTask -TaskName $tasks[0].TaskName "
            "-TaskPath $tasks[0].TaskPath} else {''};"
            "[pscustomobject]@{TaskCount=$tasks.Count;ComPresent=($null -ne $com);"
            "ActionCount=$actions.Count;Execute=if($actions.Count -eq 1){$actions[0].Execute}else{''};"
            "Arguments=if($actions.Count -eq 1){$actions[0].Arguments}else{''};"
            "WorkingDirectory=if($actions.Count -eq 1){$actions[0].WorkingDirectory}else{''};"
            "Principal=if($tasks.Count -eq 1){$tasks[0].Principal.UserId}else{''};"
            "State=if($tasks.Count -eq 1){$tasks[0].State.ToString()}else{''};"
            "ExportedXml=$xml;Instances=$instances} | ConvertTo-Json -Depth 5 -Compress",
        )
        try:
            row = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Task Scheduler query is malformed") from exc
        if not isinstance(row, dict):
            raise RuntimeError("Task Scheduler query is ambiguous")
        if row.get("TaskCount") == 0 and row.get("ComPresent") is False:
            self._safe_parsed({"task_count": 0, "com_present": False})
            return None
        instances = row.get("Instances", [])
        if isinstance(instances, dict):
            instances = [instances]
        if not isinstance(instances, list) or any(
            not isinstance(item, dict)
            or set(item) != {"InstanceGuid", "EnginePID"}
            for item in instances
        ):
            raise RuntimeError("Task Scheduler instance list is ambiguous")
        safe_instances = [
            {"manager_instance_id": item.get("InstanceGuid"), "pid": item.get("EnginePID")}
            for item in instances
        ]
        exported_xml = str(row.get("ExportedXml", ""))
        semantics = _task_xml_semantics(exported_xml)
        parsed = {
            "task_count": row.get("TaskCount"),
            "com_present": row.get("ComPresent"),
            "action_count": row.get("ActionCount"),
            "principal": row.get("Principal"),
            "state": row.get("State"),
            "instances": safe_instances,
            "exported_xml_sha256": _sha256_bytes(exported_xml.encode()),
            "exported_semantic_sha256": _sha256_bytes(_canonical(semantics)),
        }
        self._safe_parsed(parsed)
        return {
            **parsed,
            "definition_path": str(self.definition_path),
            "action_executable": row.get("Execute"),
            "action_arguments": row.get("Arguments", ""),
            "working_directory": row.get("WorkingDirectory", ""),
            "xml_semantics": semantics,
        }

    def _query(self) -> Mapping[str, Any] | None:
        if self.os_name == "macos":
            result = self._run("launchd-query", ["launchctl", "print", self.target], allowed=(0, 3))
            if result.returncode:
                text = (result.stdout + result.stderr).lower()
                if "could not find service" not in text and "not found" not in text:
                    raise RuntimeError("launchd absence is ambiguous")
                self._safe_parsed({"present": False})
                return None
            fields = _parse_assignments(result.stdout)
            row = {
                "definition_path": fields.get("path"),
                "action": _launchd_arguments(result.stdout),
                "action_executable": fields.get("program"),
                "state": fields.get("state"),
                "pid": int(fields["pid"]) if fields.get("pid", "").isdigit() else None,
            }
            self._safe_parsed(
                {
                    "present": True,
                    "definition_path_sha256": _sha256_bytes(str(row["definition_path"]).encode()),
                    "action_sha256": _sha256_bytes(_canonical(row["action"])),
                    "state": row["state"],
                    "pid": row["pid"],
                }
            )
            return row
        if self.os_name == "linux":
            result = self._run(
                "systemd-query",
                [
                    "systemctl",
                    "--user",
                    "show",
                    SYSTEMD_UNIT,
                    "--property=Id,Names,LoadState,ActiveState,SubState,FragmentPath,MainPID,InvocationID,NeedDaemonReload,ExecStart",
                ],
                allowed=(0, 4),
            )
            fields = _parse_assignments(result.stdout)
            if fields.get("LoadState") == "not-found":
                self._safe_parsed({"present": False, "load_state": "not-found"})
                return None
            if result.returncode or not fields.get("LoadState"):
                raise RuntimeError("systemd absence is ambiguous")
            row = {
                "id": fields.get("Id"),
                "names": fields.get("Names", "").split(),
                "load_state": fields.get("LoadState"),
                "state": fields.get("ActiveState"),
                "substate": fields.get("SubState"),
                "definition_path": fields.get("FragmentPath"),
                "pid": int(fields["MainPID"]) if fields.get("MainPID", "").isdigit() else None,
                "manager_instance_id": fields.get("InvocationID"),
                "need_reload": fields.get("NeedDaemonReload"),
                "action": _systemd_argv(fields.get("ExecStart", "")),
            }
            self._safe_parsed(
                {
                    key: value
                    for key, value in row.items()
                    if key not in {"action", "definition_path"}
                }
                | {
                    "definition_path_sha256": _sha256_bytes(str(row["definition_path"]).encode()),
                    "action_sha256": _sha256_bytes(_canonical(row["action"])),
                }
            )
            return row
        return self._windows_query()

    def assert_absent(self) -> None:
        if self._query() is not None:
            raise RuntimeError("pre-existing native service target is registered")

    def _validate_spec(self, spec: ServiceSpec) -> None:
        path = spec.definition_path
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise RuntimeError("product-owned canonical definition is missing or unsafe")
        resolved = path.resolve(strict=True)
        if resolved != path.absolute():
            raise RuntimeError("product-owned canonical definition traverses a symlink")
        if self.definition_path is None:
            if self.os_name != "windows":
                raise RuntimeError("product-canonical definition path is unavailable")
        elif (
            path.absolute() != self.definition_path.absolute()
            or resolved != self.definition_path.absolute()
        ):
            raise RuntimeError("definition path is not product-canonical")
        if _sha256_file(path) != spec.definition_sha256:
            raise RuntimeError("definition bytes changed before registration")
        if spec.principal != self.principal:
            raise RuntimeError("service principal differs from authenticated principal")
        executable = Path(spec.action[0])
        if (
            not executable.is_absolute()
            or executable.is_symlink()
            or not executable.is_file()
            or executable.resolve(strict=True) != executable.absolute()
        ):
            raise RuntimeError("service action is not a real nonsymlink executable")
        if self.os_name == "windows":
            xml = path.read_text(encoding="utf-8")
            semantics = _task_xml_semantics(xml)
            expected = {
                "execute": spec.action[0],
                "arguments": subprocess.list2cmdline(list(spec.action[1:])),
                "working_directory": spec.working_directory,
                "principal": spec.principal,
                "multiple_instances_policy": "IgnoreNew",
                "disallow_start_on_batteries": "false",
                "stop_on_batteries": "false",
            }
            if semantics != expected:
                raise RuntimeError("source Scheduled Task XML semantics are foreign")
            self.source_xml_sha256 = spec.definition_sha256
            self.task_semantic_sha256 = _sha256_bytes(_canonical(semantics))

    def register_start(self, spec: ServiceSpec) -> dict[str, Any]:
        self._validate_spec(spec)
        if self.definition_path is None:
            self.definition_path = spec.definition_path.resolve(strict=True)
        if self.spec is not None:
            raise RuntimeError("adapter already owns a registration")
        self.assert_absent()
        self.spec = spec
        mutated = False
        try:
            if self.os_name == "macos":
                uid = self.target.split("/", 2)[1]
                mutated = True
                self._run(
                    "launchd-bootstrap",
                    ["launchctl", "bootstrap", f"gui/{uid}", str(spec.definition_path)],
                )
                self._run("launchd-start", ["launchctl", "kickstart", self.target])
            elif self.os_name == "linux":
                self._run("systemd-reload", ["systemctl", "--user", "daemon-reload"])
                mutated = True
                self._run("systemd-enable", ["systemctl", "--user", "enable", SYSTEMD_UNIT])
                self._run("systemd-start", ["systemctl", "--user", "start", SYSTEMD_UNIT])
            else:
                escaped = str(spec.definition_path).replace("'", "''")
                mutated = True
                self._ps(
                    "task-scheduler-register",
                    "$ErrorActionPreference='Stop';"
                    f"$xml=Get-Content -Raw -LiteralPath '{escaped}';"
                    f"Register-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}' -TaskPath '\\' -Xml $xml | Out-Null;"
                    f"Start-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}' -TaskPath '\\'",
                )
            query = self._query()
            if query is None:
                raise RuntimeError("native target disappeared after registration")
            if self.os_name == "windows":
                self.exported_xml_sha256 = str(query.get("exported_xml_sha256", ""))
                self.manager_definition_sha256 = self.exported_xml_sha256
            else:
                self.manager_definition_sha256 = spec.definition_sha256
            self._validate_query(query, require_running=True)
            return self.registration()
        except Exception as exc:
            if mutated:
                raise RuntimeError(
                    "native registration mutated before validation; cleanup refused; "
                    f"residue may remain: {exc}"
                ) from exc
            raise

    def registration(self) -> dict[str, Any]:
        if self.spec is None:
            raise RuntimeError("native registration is unavailable")
        registration = {
            "target": self.target,
            "definition_path": str(self.spec.definition_path),
            "definition_sha256": self.spec.definition_sha256,
            "manager_definition_sha256": self.manager_definition_sha256
            or self.spec.definition_sha256,
            "action": list(self.spec.action),
            "action_sha256": _sha256_bytes(_canonical(list(self.spec.action))),
            "principal": self.principal,
            "definition_owner": self.spec.definition_owner,
            "working_directory": self.spec.working_directory,
        }
        if self.os_name == "windows":
            registration.update(
                {
                    "source_xml_sha256": self.source_xml_sha256,
                    "exported_xml_sha256": self.exported_xml_sha256,
                    "task_semantic_sha256": self.task_semantic_sha256,
                    "task_semantics": {
                        "execute": self.spec.action[0],
                        "arguments": subprocess.list2cmdline(list(self.spec.action[1:])),
                        "working_directory": self.spec.working_directory,
                        "principal": self.spec.principal,
                        "multiple_instances_policy": "IgnoreNew",
                        "disallow_start_on_batteries": "false",
                        "stop_on_batteries": "false",
                    },
                }
            )
        return registration

    def _validate_query(self, query: Mapping[str, Any], *, require_running: bool) -> None:
        if self.spec is None:
            raise RuntimeError("native registration authority is absent")
        if query.get("definition_path") != str(self.definition_path):
            raise RuntimeError("native definition path is foreign")
        if self.os_name == "macos":
            if query.get("action_executable") != self.spec.action[0] or query.get("action") != list(
                self.spec.action
            ):
                raise RuntimeError("launchd action is foreign")
            if require_running and (
                query.get("state") != "running"
                or type(query.get("pid")) is not int
                or query["pid"] <= 0
            ):
                raise RuntimeError("launchd running PID is unavailable")
        elif self.os_name == "linux":
            if (
                query.get("id") != SYSTEMD_UNIT
                or query.get("names") != [SYSTEMD_UNIT]
                or query.get("load_state") != "loaded"
                or query.get("need_reload") != "no"
                or query.get("action") != list(self.spec.action)
            ):
                raise RuntimeError("systemd unit/action identity is foreign or stale")
            if require_running and (
                query.get("state") != "active"
                or query.get("substate") != "running"
                or type(query.get("pid")) is not int
                or query["pid"] <= 0
                or not _nonempty(query.get("manager_instance_id"))
            ):
                raise RuntimeError("systemd running instance identity is unavailable")
        else:
            expected_arguments = subprocess.list2cmdline(list(self.spec.action[1:]))
            instances = query.get("instances")
            if (
                query.get("task_count") != 1
                or query.get("com_present") is not True
                or query.get("action_count") != 1
                or query.get("action_executable") != self.spec.action[0]
                or query.get("action_arguments", "") != expected_arguments
                or query.get("working_directory", "") != self.spec.working_directory
                or query.get("principal") != self.principal
                or query.get("exported_semantic_sha256") != self.task_semantic_sha256
                or query.get("xml_semantics", {}).get("execute") != self.spec.action[0]
                or query.get("xml_semantics", {}).get("arguments") != expected_arguments
                or query.get("xml_semantics", {}).get("working_directory")
                != self.spec.working_directory
                or query.get("xml_semantics", {}).get("principal") != self.principal
            ):
                raise RuntimeError("Scheduled Task action/principal/XML identity is foreign")
            if require_running and (
                str(query.get("state", "")).lower() != "running"
                or not isinstance(instances, list)
                or len(instances) != 1
                or not _nonempty(_mapping(instances[0]).get("manager_instance_id"))
                or type(_mapping(instances[0]).get("pid")) is not int
                or _mapping(instances[0])["pid"] <= 0
            ):
                raise RuntimeError("exactly one running Scheduled Task COM instance is required")

    def observe(self, discovery: Mapping[str, Any]) -> dict[str, Any]:
        query = self._query()
        if query is None:
            raise RuntimeError("owned native target disappeared")
        self._validate_query(query, require_running=True)
        try:
            pid = discovery["pid"]
            birth = discovery["process_birth_id"]
            runtime_id = discovery["runtime_instance_id"]
            executable = Path(discovery["executable_path"])
            executable_digest = discovery["executable_sha256"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Runtime discovery identity is incomplete") from exc
        if type(pid) is not int or pid <= 0 or not _nonempty(birth) or not _nonempty(runtime_id):
            raise RuntimeError("Runtime discovery PID/birth/instance identity is incomplete")
        data = self.data_probe.observe(discovery)
        if self.os_name == "windows":
            manager_row = _mapping(query["instances"][0])
            manager_pid = manager_row.get("pid")
            manager_instance_id = manager_row.get("manager_instance_id")
        else:
            manager_pid = query.get("pid")
            manager_instance_id = query.get("manager_instance_id") or f"{self.target}:{manager_pid}"
        if type(manager_pid) is not int or manager_pid <= 0 or manager_pid != pid:
            raise RuntimeError("positive native manager PID differs from Runtime discovery")
        process = self.process_probe.observe(
            pid, os_name=self.os_name, expected_executable=executable
        )
        if process.get("process_birth_id") != birth:
            raise RuntimeError("process birth differs from Runtime discovery")
        if process.get("executable_sha256") != executable_digest:
            raise RuntimeError("process executable digest differs from Runtime discovery")
        return {
            "state": "running",
            "manager_instance_id": manager_instance_id,
            "pid": pid,
            "process_birth_id": birth,
            "runtime_instance_id": runtime_id,
            "executable_path": str(executable.resolve(strict=True)),
            "executable_sha256": executable_digest,
            **{name: data[name] for name in INSTANCE_IDENTITY_FIELDS[7:]},
        }

    @staticmethod
    def _same_instance(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        return all(left.get(name) == right.get(name) for name in INSTANCE_IDENTITY_FIELDS)

    def revalidate(
        self, expected: Mapping[str, Any], discovery: Mapping[str, Any]
    ) -> dict[str, Any]:
        if (
            self.spec is None
            or not self.definition_path.is_file()
            or self.definition_path.is_symlink()
        ):
            raise RuntimeError("native definition is absent or unsafe before mutation")
        if _sha256_file(self.definition_path) != self.spec.definition_sha256:
            raise RuntimeError("native definition changed before mutation")
        current = self.observe(discovery)
        if not self._same_instance(current, expected):
            raise RuntimeError("native Runtime instance changed before mutation")
        return current

    def restart(self, expected: Mapping[str, Any], discovery: Mapping[str, Any]) -> None:
        self.revalidate(expected, discovery)
        if self.os_name == "macos":
            self._run("launchd-restart", ["launchctl", "kickstart", "-k", self.target])
        elif self.os_name == "linux":
            self._run("systemd-restart", ["systemctl", "--user", "restart", SYSTEMD_UNIT])
        else:
            self._ps(
                "task-scheduler-restart",
                f"Stop-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}' -TaskPath '\\';"
                f"Start-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}' -TaskPath '\\'",
            )

    def terminate_owned_runtime(
        self, expected: Mapping[str, Any], discovery: Mapping[str, Any]
    ) -> None:
        current = self.revalidate(expected, discovery)
        if self.os_name == "windows":
            self._ps(
                "owned-runtime-termination",
                f"$ErrorActionPreference='Stop';Stop-Process -Id {current['pid']} -Force",
            )
        else:
            self._run("owned-runtime-termination", ["kill", "-TERM", str(current["pid"])])

    def observe_external_controller(self, discovery: Mapping[str, Any]) -> dict[str, Any]:
        try:
            pid = discovery["pid"]
            birth = discovery["process_birth_id"]
            executable = Path(discovery["executable_path"])
            executable_sha256 = discovery["executable_sha256"]
            controller_instance_id = discovery["controller_instance_id"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("external controller discovery is incomplete") from exc
        durable = self.durable_journal_probe.observe(discovery)
        if (
            type(pid) is not int
            or pid <= 0
            or not _nonempty(birth)
            or not _nonempty(controller_instance_id)
            or not _digest(executable_sha256)
        ):
            raise RuntimeError("external controller identity/durable proof is incomplete")
        observed = self.process_probe.observe(
            pid,
            os_name=self.os_name,
            expected_executable=executable,
        )
        if (
            observed.get("process_birth_id") != birth
            or observed.get("executable_sha256") != executable_sha256
        ):
            raise RuntimeError("external controller process identity differs")
        return {
            "pid": pid,
            "process_birth_id": birth,
            "controller_instance_id": controller_instance_id,
            "executable_path": str(executable.resolve(strict=True)),
            "executable_sha256": executable_sha256,
            **durable,
        }

    def observe_absent(self, authorized: Mapping[str, Any]) -> dict[str, Any]:
        query = self._query()
        definition_absent = not self.definition_path.exists()
        incarnations = authorized.get("observed_incarnations")
        actions = authorized.get("observed_actions")
        if not isinstance(incarnations, list) or not incarnations:
            raise RuntimeError("authorized native incarnation set is absent")
        if not isinstance(actions, list) or not actions:
            raise RuntimeError("authorized native action set is absent")
        if any(
            type(row) is not dict
            or set(row) != {"pid", "process_birth_id"}
            or type(row.get("pid")) is not int
            or row["pid"] <= 0
            or (self.os_name == "windows" and row["pid"] > 0xFFFFFFFF)
            or (
                not _valid_windows_process_birth_id(row.get("process_birth_id"))
                if self.os_name == "windows"
                else not _nonempty(row.get("process_birth_id"))
            )
            for row in incarnations
        ):
            raise RuntimeError("authorized native incarnation record is malformed")
        incarnation_identities = [
            (row["pid"], row["process_birth_id"]) for row in incarnations
        ]
        if len(set(incarnation_identities)) != len(incarnation_identities):
            raise RuntimeError("authorized native incarnation record is duplicated")
        all_incarnations_absent = all(
            self.process_probe.incarnation_absent(
                row["pid"],
                row["process_birth_id"],
                os_name=self.os_name,
            )
            for row in incarnations
        )
        remaining = [
            process
            for action in actions
            for process in self.process_probe.owned_action_processes(
                tuple(str(item) for item in action), os_name=self.os_name
            )
        ]
        post_data = self.data_probe.observe(_mapping(authorized.get("data_identity")))
        post_data_identity = _data_identity(post_data)
        data_unchanged = post_data_identity == authorized.get("data_identity")
        if (
            query is not None
            or not definition_absent
            or not all_incarnations_absent
            or remaining
            or not data_unchanged
        ):
            raise RuntimeError("native target/definition/owned processes are not observably absent")
        return {
            "definition_absent": True,
            "manager_target_absent": True,
            "owned_processes_after": len(remaining),
            "all_authorized_incarnations_absent": True,
            "observed_action_count": len(actions),
            "observed_incarnation_count": len(incarnations),
            "post_data_identity": post_data_identity,
            "post_data_identity_sha256": _sha256_bytes(_canonical(post_data_identity)),
            "data_identity_unchanged": True,
            "observed": True,
        }

    def remove_cleanup(
        self,
        expected: Mapping[str, Any],
        discovery: Mapping[str, Any],
        authorized: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.revalidate(expected, discovery)
        if self.os_name == "macos":
            self._run("launchd-cleanup-remove", ["launchctl", "bootout", self.target])
        elif self.os_name == "linux":
            self._run(
                "systemd-cleanup-remove",
                ["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT],
            )
        else:
            self._ps(
                "task-scheduler-cleanup-remove",
                f"Stop-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}' -TaskPath '\\' -ErrorAction SilentlyContinue;"
                f"Unregister-ScheduledTask -TaskName '{WINDOWS_TASK_NAME}' -TaskPath '\\' -Confirm:$false",
            )
        self.definition_path.unlink()
        if self.os_name == "linux":
            self._run("systemd-cleanup-reload", ["systemctl", "--user", "daemon-reload"])
        absent = self.observe_absent(authorized)
        return {**absent, "performed_by": "adapter-cleanup", "requested": True}


class NativeSession:
    def __init__(self, owner: NativeOwner) -> None:
        self.owner = owner
        self.instances: list[dict[str, Any]] = []
        self.removal: Mapping[str, Any] | None = None
        self.controller_interruption: Mapping[str, Any] | None = None
        self._removal_authorization: dict[str, Any] | None = None
        self._controller_authorizations: dict[str, dict[str, Any]] = {}

    def _removal_scope(self, current: Mapping[str, Any]) -> dict[str, Any]:
        registration = self.owner.registration()
        registered_action = tuple(str(item) for item in registration["action"])
        suffix = registered_action[1:]
        if not self.instances or any(
            type(row) is not dict
            or row.get("state") != "running"
            or type(row.get("pid")) is not int
            or row["pid"] <= 0
            or (self.owner.os_name == "windows" and row["pid"] > 0xFFFFFFFF)
            or not _nonempty(row.get("executable_path"))
            or (
                not _valid_windows_process_birth_id(row.get("process_birth_id"))
                if self.owner.os_name == "windows"
                else not _nonempty(row.get("process_birth_id"))
            )
            for row in self.instances
        ):
            raise RuntimeError("complete observed native removal scope is unavailable")
        actions = {
            (row["executable_path"], *suffix)
            for row in self.instances
        }
        actions.add(registered_action)
        incarnations = {
            (row["pid"], row["process_birth_id"])
            for row in self.instances
        }
        if not actions or not incarnations:
            raise RuntimeError("complete observed native removal scope is unavailable")
        observed_actions = [list(action) for action in sorted(actions)]
        observed_incarnations = [
            {"pid": pid, "process_birth_id": birth} for pid, birth in sorted(incarnations)
        ]
        data_identity = _data_identity(current)
        return {
            "observed_actions": observed_actions,
            "observed_actions_sha256": _sha256_bytes(_canonical(observed_actions)),
            "observed_incarnations": observed_incarnations,
            "observed_incarnations_sha256": _sha256_bytes(_canonical(observed_incarnations)),
            "data_identity": data_identity,
            "data_identity_sha256": _sha256_bytes(_canonical(data_identity)),
        }

    def observe(self, scenario: str, phase: str, discovery: Mapping[str, Any]) -> dict[str, Any]:
        allowed = scenario in failure.SCENARIOS or (
            scenario.startswith("stop_safety.")
            and scenario.split(".", 1)[1] in failure.STOP_OUTCOMES
        )
        if not allowed or phase not in {"before", "after", "cleanup"}:
            raise ValueError("unknown native scenario/phase")
        instance = self.owner.observe(discovery)
        if scenario == "interruption_recovery" and phase == "after":
            before = next(
                (
                    row
                    for row in self.instances
                    if row.get("scenario") == scenario and row.get("phase") == "before"
                ),
                None,
            )
            if (
                before is None
                or before.get("runtime_instance_id") == instance.get("runtime_instance_id")
                or not self.owner.process_probe.incarnation_absent(
                    before["pid"], before["process_birth_id"], os_name=self.owner.os_name
                )
            ):
                raise RuntimeError("prior Runtime incarnation remains after interruption")
        self.instances.append({"scenario": scenario, "phase": phase, **instance})
        return dict(instance)

    def terminate_owned_runtime(
        self, expected: Mapping[str, Any], discovery: Mapping[str, Any]
    ) -> None:
        """Runtime termination only; not controller-interruption evidence."""
        self.owner.terminate_owned_runtime(expected, discovery)

    def restart(self, expected: Mapping[str, Any], discovery: Mapping[str, Any]) -> None:
        self.owner.restart(expected, discovery)

    def _before_external_controller(
        self,
        role: str,
        discovery: Mapping[str, Any],
    ) -> str:
        if role not in {"interrupted", "recovery"}:
            raise ValueError("unknown external controller role")
        current = self.owner.observe_external_controller(discovery)
        revalidated = self.owner.observe_external_controller(discovery)
        if current != revalidated:
            raise RuntimeError("external controller changed before interruption authority")
        if role == "recovery":
            interrupted = self._controller_authorizations.get("interrupted-complete")
            if interrupted is None:
                raise RuntimeError("interrupted controller absence is not yet observed")
            prior = interrupted["controller"]
            if (
                revalidated["runtime_operation_id"] != prior["runtime_operation_id"]
                or revalidated["durable_phase"] != prior["durable_phase"]
                or revalidated["durable_journal_path"] != prior["durable_journal_path"]
                or revalidated["durable_journal_sha256"] != prior["durable_journal_sha256"]
            ):
                raise RuntimeError("recovery controller durable operation binding differs")
            if (
                revalidated["pid"] == prior["pid"]
                and revalidated["process_birth_id"] == prior["process_birth_id"]
            ) or revalidated["controller_instance_id"] == prior["controller_instance_id"]:
                raise RuntimeError("recovery controller is not a distinct process")
        token = secrets.token_urlsafe(32)
        self._controller_authorizations[role] = {
            "token_sha256": _sha256_bytes(token.encode()),
            "controller": revalidated,
            "controller_sha256": _sha256_bytes(_canonical(revalidated)),
        }
        return token

    def before_external_controller_interruption(self, discovery: Mapping[str, Any]) -> str:
        """Authorize driver interruption of one exact durable controller process."""
        return self._before_external_controller("interrupted", discovery)

    def observe_external_controller_interrupted(self, authorization: str) -> None:
        self._complete_external_controller("interrupted", authorization)

    def before_external_recovery_controller(self, discovery: Mapping[str, Any]) -> str:
        return self._before_external_controller("recovery", discovery)

    def observe_external_recovery_controller_complete(self, authorization: str) -> None:
        self._complete_external_controller("recovery", authorization)

    def _complete_external_controller(self, role: str, authorization: str) -> None:
        auth = self._controller_authorizations.get(role)
        if auth is None or _sha256_bytes(authorization.encode()) != auth["token_sha256"]:
            raise RuntimeError("external controller authorization is absent or foreign")
        controller = auth["controller"]
        if not self.owner.process_probe.incarnation_absent(
            controller["pid"],
            controller["process_birth_id"],
            os_name=self.owner.os_name,
        ):
            raise RuntimeError("exact external controller incarnation remains")
        complete = {
            "controller": controller,
            "controller_sha256": auth["controller_sha256"],
            "authorization_sha256": auth["token_sha256"],
            "exact_birth_absent": True,
        }
        self._controller_authorizations[f"{role}-complete"] = complete
        if role == "recovery":
            interrupted = self._controller_authorizations["interrupted-complete"]
            self.controller_interruption = {
                "kind": "external-controller-process",
                "runtime_operation_id": controller["runtime_operation_id"],
                "durable_phase": controller["durable_phase"],
                "durable_journal_path": controller["durable_journal_path"],
                "durable_journal_sha256": controller["durable_journal_sha256"],
                "interrupted_controller": interrupted,
                "recovery_controller": complete,
                "local_baseexception": False,
            }

    def before_product_removal(
        self, expected: Mapping[str, Any], discovery: Mapping[str, Any]
    ) -> str:
        current = self.owner.revalidate(expected, discovery)
        before = next(
            (
                row
                for row in reversed(self.instances)
                if row.get("scenario") == "keep_data_uninstall" and row.get("phase") == "before"
            ),
            None,
        )
        if before is None or not self.owner._same_instance(before, current):
            raise RuntimeError("removal authority differs from keep-data before identity")
        scope = self._removal_scope(current)
        token = secrets.token_urlsafe(32)
        self._removal_authorization = {
            "token_sha256": _sha256_bytes(token.encode()),
            "identity": _instance_identity(current),
            "identity_sha256": _sha256_bytes(_canonical(_instance_identity(current))),
            "registration_sha256": _sha256_bytes(_canonical(self.owner.registration())),
            "authorization_raw_index": len(self.owner.raw) - 1,
            **scope,
        }
        return token

    def observe_product_removal(self, authorization: str) -> Mapping[str, Any]:
        auth = self._removal_authorization
        if auth is None or _sha256_bytes(authorization.encode()) != auth["token_sha256"]:
            raise RuntimeError("product-removal authorization is absent or foreign")
        if self.removal is not None:
            raise RuntimeError("native removal is already recorded")
        absence = self.owner.observe_absent(auth)
        self.removal = {
            **absence,
            "requested": True,
            "owned_target_revalidated": True,
            "performed_by": "product-uninstall",
            "authorization_sha256": auth["token_sha256"],
            "authorized_identity_sha256": auth["identity_sha256"],
            "authorized_registration_sha256": auth["registration_sha256"],
            "authorized_actions_sha256": auth["observed_actions_sha256"],
            "authorized_incarnations_sha256": auth["observed_incarnations_sha256"],
            "authorized_data_identity_sha256": auth["data_identity_sha256"],
            "authorization_raw_index": auth["authorization_raw_index"],
            "absence_raw_index": len(self.owner.raw) - 1,
        }
        self.instances.append(
            {
                "scenario": "keep_data_uninstall",
                "phase": "cleanup",
                "state": "absent",
                "manager_instance_id": None,
                "pid": None,
                "process_birth_id": None,
                "runtime_instance_id": None,
                "executable_path": None,
                "executable_sha256": None,
                **absence["post_data_identity"],
            }
        )
        return dict(self.removal)

    def safe_finalize(self) -> Mapping[str, Any]:
        if self.removal is not None:
            return self.removal
        current = next(
            (row for row in reversed(self.instances) if row.get("state") == "running"), None
        )
        if current is None:
            raise RuntimeError("no exact current instance is available for safe cleanup")
        return self.owner.remove_cleanup(current, current, self._removal_scope(current))


def _platform_name() -> str:
    return {"darwin": "macos", "linux": "linux", "win32": "windows"}.get(sys.platform, "")


def _powershell_path(environment: Mapping[str, str], *, fixture_path: Path | None) -> Path | None:
    if fixture_path is not None:
        candidate = fixture_path
    else:
        system_root = environment.get("SystemRoot")
        if not _nonempty(system_root):
            return None
        candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
        raise RuntimeError("absolute nonsymlink Windows PowerShell 5.1 is required")
    resolved = candidate.resolve(strict=True)
    if resolved != candidate.absolute():
        raise RuntimeError("Windows PowerShell path traverses a symlink")
    return resolved


def _canonical_definition_path(os_name: str, environment: Mapping[str, str]) -> Path | None:
    if os_name == "macos":
        home = environment.get("HOME")
        if not _nonempty(home) or not Path(home).is_absolute():
            raise ValueError("absolute HOME is required for canonical LaunchAgent")
        return Path(home) / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    if os_name == "linux":
        config = environment.get("XDG_CONFIG_HOME")
        if _nonempty(config):
            root = Path(config)
        else:
            home = environment.get("HOME")
            if not _nonempty(home):
                raise ValueError("HOME or XDG_CONFIG_HOME is required")
            root = Path(home) / ".config"
        if not root.is_absolute():
            raise ValueError("absolute XDG configuration path is required")
        return root / "systemd" / "user" / SYSTEMD_UNIT
    # Task Scheduler has no canonical on-disk XML location. The approved product
    # driver must return the exact installer-owned XML path in ServiceSpec.
    return None


def _work_root(work_root: Path, environment: Mapping[str, str]) -> Path:
    runner_temp = environment.get("RUNNER_TEMP")
    if not _nonempty(runner_temp):
        raise ValueError("RUNNER_TEMP is required")
    temp = Path(runner_temp).resolve(strict=True)
    root = work_root.resolve()
    if root == temp or not root.is_relative_to(temp):
        raise ValueError("work root must be a unique child of resolved RUNNER_TEMP")
    if root.exists():
        raise ValueError("work root must not already exist")
    root.mkdir(parents=True)
    return root


def _host_authentication(
    environment: Mapping[str, str],
    contract: Mapping[str, Any],
    runner: CommandRunner,
    *,
    fixture_mode: bool,
    powershell: Path | None,
) -> tuple[dict[str, Any], str]:
    expected_os, expected_runner_os, expected_image = failure.RUNNER_PROVENANCE[contract["runner"]]
    required = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_OS": expected_runner_os,
        "ImageOS": expected_image,
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_SERVER_URL": "https://github.com",
    }
    for name, expected in required.items():
        if environment.get(name) != expected:
            raise RuntimeError(f"hosted runner provenance {name} does not match")
    for name in (
        "ImageVersion",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
        "GITHUB_REPOSITORY",
    ):
        if not _nonempty(environment.get(name)):
            raise RuntimeError(f"hosted runner provenance {name} is absent")
    if SYNTHETIC_IMAGE.search(environment["ImageVersion"]):
        raise RuntimeError("hosted runner ImageVersion is synthetic")
    if expected_os == "macos":
        result = runner.run(["sw_vers", "-buildVersion"], timeout=10)
    elif expected_os == "linux":
        result = runner.run(["uname", "-r"], timeout=10)
    else:
        if powershell is None:
            raise RuntimeError("validated Windows PowerShell is unavailable")
        result = runner.run(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-CimInstance Win32_OperatingSystem).BuildNumber",
            ],
            timeout=10,
        )
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("live OS build is unavailable")
    build = result.stdout.strip().splitlines()[0]
    return (
        {
            "provider": "github-actions",
            **required,
            "ImageVersion": environment["ImageVersion"],
            "GITHUB_RUN_ID": environment["GITHUB_RUN_ID"],
            "GITHUB_RUN_ATTEMPT": environment["GITHUB_RUN_ATTEMPT"],
            "GITHUB_JOB": environment["GITHUB_JOB"],
            "GITHUB_REPOSITORY": environment["GITHUB_REPOSITORY"],
            "os_build": build,
            "shell": contract["shell"],
            "probe_match": True,
            "authenticated": False,
            "requires_admission_context": True,
        },
        f"{expected_runner_os}-{build}",
    )


def _checkout_heads(
    candidate: Mapping[str, str], repos: Mapping[str, Path], runner: CommandRunner
) -> dict[str, dict[str, str]]:
    observed: dict[str, dict[str, str]] = {}
    for name in ("app", "agent", "runtime", "pinokio"):
        repo = repos[name]
        if not repo.is_dir() or repo.is_symlink():
            raise ValueError(f"candidate {name} checkout is unsafe")
        result = runner.run(["git", "-C", str(repo), "rev-parse", "HEAD"])
        head = result.stdout.strip()
        if result.returncode or head != candidate[name]:
            raise ValueError(f"candidate {name} checkout differs from frozen candidate")
        observed[name] = {"sha": head, "path": str(repo.resolve(strict=True))}
    return observed


def _semantic_for_instance(scenarios: Mapping[str, Any], scenario: str) -> Mapping[str, Any]:
    if scenario.startswith("stop_safety."):
        outcome = scenario.split(".", 1)[1]
        case = _mapping(_mapping(_mapping(scenarios.get("stop_safety")).get("cases")).get(outcome))
        return _mapping(case.get("semantic_data"))
    return _mapping(_mapping(scenarios.get(scenario)).get("semantic_data"))


def _controller_errors(observation: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    scenarios = _mapping(observation.get("scenarios"))
    interruption = _mapping(scenarios.get("interruption_recovery"))
    native = _mapping(observation.get("native_service"))
    controller = _mapping(native.get("controller_interruption"))
    if _mapping(interruption.get("native_controller_interruption")) != controller:
        errors.append("interruption_recovery: scenario/controller observation differs")
    if (
        controller.get("kind") != "external-controller-process"
        or controller.get("runtime_operation_id") != interruption.get("operation_id")
        or controller.get("durable_phase") != interruption.get("phase")
        or not _nonempty(controller.get("durable_journal_path"))
        or not _digest(controller.get("durable_journal_sha256"))
        or controller.get("local_baseexception") is not False
    ):
        errors.append("interruption_recovery: durable external controller proof required")
    rows: list[Mapping[str, Any]] = []
    for role in ("interrupted_controller", "recovery_controller"):
        completed = _mapping(controller.get(role))
        row = _mapping(completed.get("controller"))
        rows.append(row)
        if (
            completed.get("exact_birth_absent") is not True
            or completed.get("controller_sha256") != _sha256_bytes(_canonical(row))
            or not _digest(completed.get("authorization_sha256"))
        ):
            errors.append(f"interruption_recovery.{role}: observed absence binding required")
        if (
            type(row.get("pid")) is not int
            or row.get("pid", 0) <= 0
            or not _nonempty(row.get("process_birth_id"))
            or not _nonempty(row.get("controller_instance_id"))
            or not _nonempty(row.get("executable_path"))
            or not _digest(row.get("executable_sha256"))
            or row.get("runtime_operation_id") != controller.get("runtime_operation_id")
            or row.get("durable_phase") != controller.get("durable_phase")
            or row.get("durable_journal_path") != controller.get("durable_journal_path")
            or row.get("durable_journal_sha256") != controller.get("durable_journal_sha256")
        ):
            errors.append(
                f"interruption_recovery.{role}: exact process/operation identity required"
            )
    if len(rows) == 2 and (
        (
            rows[0].get("pid") == rows[1].get("pid")
            and rows[0].get("process_birth_id") == rows[1].get("process_birth_id")
        )
        or rows[0].get("controller_instance_id") == rows[1].get("controller_instance_id")
    ):
        errors.append("interruption_recovery: recovery controller is not a distinct process")
    return errors


def _scenario_instance_errors(observation: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    scenarios = _mapping(observation.get("scenarios"))
    native = _mapping(observation.get("native_service"))
    rows = native.get("instances")
    if not isinstance(rows, list):
        return ["native_service.instances: list is required"]
    by_key: dict[tuple[str, str], Mapping[str, Any]] = {}
    data_roots: set[str] = set()
    workspaces: set[str] = set()
    data_manifests: set[tuple[str, str, str]] = set()
    os_name = observation.get("os")
    target = native.get("target")
    for index, value in enumerate(rows):
        row = _mapping(value)
        key = (str(row.get("scenario", "")), str(row.get("phase", "")))
        if key in by_key:
            errors.append(f"native_service.instances[{index}]: duplicate scenario/phase")
        by_key[key] = row
        if row.get("state") == "running":
            if type(row.get("pid")) is not int or row.get("pid", 0) <= 0:
                errors.append(f"native_service.instances[{index}].pid: positive PID required")
            for name in (
                "manager_instance_id",
                "process_birth_id",
                "runtime_instance_id",
                "executable_path",
                "data_root",
                "workspace_identity",
                "data_manifest_path",
            ):
                if not _nonempty(row.get(name)):
                    errors.append(f"native_service.instances[{index}].{name}: required")
            for name in (
                "executable_sha256",
                "data_identity_sha256",
                "semantic_data_sha256",
                "data_manifest_sha256",
                "data_root_identity_sha256",
            ):
                if not _digest(row.get(name)):
                    errors.append(f"native_service.instances[{index}].{name}: digest required")
            manager_id = row.get("manager_instance_id")
            if os_name == "macos" and manager_id != f"{target}:{row.get('pid')}":
                errors.append(f"native_service.instances[{index}]: launchd identity differs")
            if os_name == "linux" and re.fullmatch(r"[0-9a-fA-F]{32}", str(manager_id)) is None:
                errors.append(f"native_service.instances[{index}]: systemd InvocationID required")
            if (
                os_name == "windows"
                and re.fullmatch(
                    r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}",
                    str(manager_id),
                )
                is None
            ):
                errors.append(f"native_service.instances[{index}]: Task InstanceGuid required")
            semantic = _semantic_for_instance(scenarios, key[0])
            if row.get("data_identity_sha256") != semantic.get("after_sha256") or row.get(
                "semantic_data_sha256"
            ) != _sha256_bytes(_canonical(semantic)):
                errors.append(f"native_service.instances[{index}]: scenario data identity differs")
            data_roots.add(str(row.get("data_root")))
            workspaces.add(str(row.get("workspace_identity")))
            data_manifests.add(
                (
                    str(row.get("data_manifest_path")),
                    str(row.get("data_manifest_sha256")),
                    str(row.get("data_root_identity_sha256")),
                )
            )
        elif row.get("state") == "absent":
            if any(row.get(name) is not None for name in INSTANCE_IDENTITY_FIELDS[1:7]):
                errors.append(
                    f"native_service.instances[{index}]: absent row retains process authority"
                )
            for name in DATA_IDENTITY_FIELDS:
                if not _nonempty(row.get(name)):
                    errors.append(
                        f"native_service.instances[{index}].{name}: post-uninstall data proof required"
                    )
        else:
            errors.append(f"native_service.instances[{index}].state: running/absent required")
    required = (
        ("interruption_recovery", "before"),
        ("interruption_recovery", "after"),
        ("failed_candidate_rollback", "after"),
        ("keep_data_uninstall", "before"),
        ("keep_data_uninstall", "cleanup"),
        ("stop_safety.refused", "before"),
        ("stop_safety.refused", "after"),
        ("stop_safety.unknown", "before"),
        ("stop_safety.unknown", "after"),
    )
    if set(by_key) != set(required):
        errors.append("native_service.instances: exact nine scenario/phase observations required")
    if len(data_roots) != 1 or len(workspaces) != 1 or len(data_manifests) != 1:
        errors.append("native_service.instances: data root/workspace/manifest identity changed")
    keep_before = by_key.get(("keep_data_uninstall", "before"), {})
    keep_cleanup = by_key.get(("keep_data_uninstall", "cleanup"), {})
    if keep_before and keep_cleanup and _data_identity(keep_before) != _data_identity(keep_cleanup):
        errors.append("native keep-data post-uninstall identity differs")
    before = by_key.get(("interruption_recovery", "before"), {})
    after = by_key.get(("interruption_recovery", "after"), {})
    if (
        before
        and after
        and (
            (
                before.get("pid") == after.get("pid")
                and before.get("process_birth_id") == after.get("process_birth_id")
            )
            or before.get("runtime_instance_id") == after.get("runtime_instance_id")
        )
    ):
        errors.append("native interruption did not produce a new Runtime instance")
    bindings = {
        ("interruption_recovery", "after"): _mapping(
            _mapping(scenarios.get("interruption_recovery")).get("terminal_identity")
        ),
        ("failed_candidate_rollback", "after"): _mapping(
            _mapping(
                _mapping(scenarios.get("failed_candidate_rollback")).get("executable_identity")
            ).get("after")
        ),
    }
    for key, identity in bindings.items():
        instance = by_key.get(key)
        if instance and (
            instance.get("executable_path") != identity.get("executable_path")
            or instance.get("executable_sha256") != identity.get("executable_sha256")
        ):
            errors.append(f"native {key[0]} executable binding differs")
    if bindings[("failed_candidate_rollback", "after")].get("selected_release") != "release0":
        errors.append("native rollback is not bound to release-0")
    cases = _mapping(_mapping(scenarios.get("stop_safety")).get("cases"))
    for outcome in failure.STOP_OUTCOMES:
        case = _mapping(cases.get(outcome))
        mutation = _mapping(case.get("mutation_inventory"))
        if mutation.get("unchanged") is not True or mutation.get("before") != mutation.get("after"):
            errors.append(f"native stop {outcome} mutated update bytes")
        pair = _mapping(case.get("executable_identity"))
        left = by_key.get((f"stop_safety.{outcome}", "before"), {})
        right = by_key.get((f"stop_safety.{outcome}", "after"), {})
        for phase, instance in (("before", left), ("after", right)):
            identity = _mapping(pair.get(phase))
            if instance and (
                instance.get("executable_path") != identity.get("executable_path")
                or instance.get("executable_sha256") != identity.get("executable_sha256")
            ):
                errors.append(f"native stop {outcome} {phase} identity differs")
        if (
            left
            and right
            and any(left.get(name) != right.get(name) for name in INSTANCE_IDENTITY_FIELDS)
        ):
            errors.append(f"native stop {outcome} changed native instance/data identity")
    errors.extend(_controller_errors(observation))
    return errors


def _exact_keys(
    value: object, allowed: set[str], prefix: str, errors: list[str]
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        errors.append(f"{prefix}: exact object required")
        return {}
    row = _mapping(value)
    unknown = set(row) - allowed
    if unknown:
        errors.append(f"{prefix}: unknown fields {sorted(unknown)}")
    return row


def _typed_fields(
    row: Mapping[str, Any],
    schema: Mapping[str, tuple[type, ...]],
    prefix: str,
    errors: list[str],
) -> None:
    """Reject container/type smuggling in every explicitly admitted leaf."""
    for name, expected in schema.items():
        if name in row and type(row[name]) not in expected:
            expected_names = "/".join(kind.__name__ for kind in expected)
            errors.append(f"{prefix}.{name}: exact {expected_names} leaf required")


def _string_fields(row: Mapping[str, Any], names: set[str], prefix: str, errors: list[str]) -> None:
    _typed_fields(row, {name: (str,) for name in names}, prefix, errors)


def _semantic_closed_world(value: object, prefix: str, errors: list[str]) -> None:
    row = _exact_keys(
        value, {"before_sha256", "after_sha256", "identical", "entities"}, prefix, errors
    )
    _string_fields(row, {"before_sha256", "after_sha256"}, prefix, errors)
    _typed_fields(row, {"identical": (bool,)}, prefix, errors)
    entities = _exact_keys(
        row.get("entities"), set(failure.DATA_ENTITIES), f"{prefix}.entities", errors
    )
    for name in failure.DATA_ENTITIES:
        entity = _exact_keys(
            entities.get(name),
            {"before_sha256", "after_sha256"},
            f"{prefix}.entities.{name}",
            errors,
        )
        _string_fields(
            entity,
            {"before_sha256", "after_sha256"},
            f"{prefix}.entities.{name}",
            errors,
        )


def _identity_closed_world(value: object, prefix: str, errors: list[str]) -> None:
    row = _exact_keys(
        value,
        {
            "selected_release",
            "executable_path",
            "executable_sha256",
            "version_manifest_sha256",
            "process_identity_sha256",
            "source_refs",
            "running",
            "health",
        },
        prefix,
        errors,
    )
    expected_ref_keys = (
        set(failure.RELEASE0_REFS)
        if row.get("selected_release") == "release0"
        else {"agent", "runtime"}
    )
    refs = _exact_keys(row.get("source_refs"), expected_ref_keys, f"{prefix}.source_refs", errors)
    _string_fields(
        row,
        {
            "selected_release",
            "executable_path",
            "executable_sha256",
            "version_manifest_sha256",
            "process_identity_sha256",
            "health",
        },
        prefix,
        errors,
    )
    _typed_fields(row, {"running": (bool,)}, prefix, errors)
    _string_fields(refs, expected_ref_keys, f"{prefix}.source_refs", errors)


def _identity_pair_closed_world(value: object, prefix: str, errors: list[str]) -> None:
    row = _exact_keys(value, {"before", "after", "match"}, prefix, errors)
    _typed_fields(row, {"match": (bool,)}, prefix, errors)
    _identity_closed_world(row.get("before"), f"{prefix}.before", errors)
    _identity_closed_world(row.get("after"), f"{prefix}.after", errors)


def _scenario_closed_world_errors(observation: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    scenarios = _exact_keys(
        observation.get("scenarios"), set(failure.SCENARIOS), "scenarios", errors
    )
    interruption = _exact_keys(
        scenarios.get("interruption_recovery"),
        {
            "status",
            "operation_id",
            "phase",
            "injected",
            "first_request",
            "recovery_request",
            "terminal_state",
            "duplicate_operation",
            "recovery_verified",
            "terminal_identity",
            "semantic_data",
            "native_controller_interruption",
        },
        "scenarios.interruption_recovery",
        errors,
    )
    _string_fields(
        interruption,
        {"status", "operation_id", "phase", "terminal_state"},
        "scenarios.interruption_recovery",
        errors,
    )
    _typed_fields(
        interruption,
        {name: (bool,) for name in ("injected", "duplicate_operation", "recovery_verified")},
        "scenarios.interruption_recovery",
        errors,
    )
    first_request = _exact_keys(
        interruption.get("first_request"),
        {"interrupted", "journal_state"},
        "scenarios.interruption_recovery.first_request",
        errors,
    )
    _typed_fields(
        first_request,
        {"interrupted": (bool,), "journal_state": (str,)},
        "scenarios.interruption_recovery.first_request",
        errors,
    )
    recovery_request = _exact_keys(
        interruption.get("recovery_request"),
        {"explicit", "operation_id"},
        "scenarios.interruption_recovery.recovery_request",
        errors,
    )
    _typed_fields(
        recovery_request,
        {"explicit": (bool,), "operation_id": (str,)},
        "scenarios.interruption_recovery.recovery_request",
        errors,
    )
    _identity_closed_world(
        interruption.get("terminal_identity"),
        "scenarios.interruption_recovery.terminal_identity",
        errors,
    )
    _semantic_closed_world(
        interruption.get("semantic_data"),
        "scenarios.interruption_recovery.semantic_data",
        errors,
    )
    rollback = _exact_keys(
        scenarios.get("failed_candidate_rollback"),
        {
            "status",
            "failure_injected",
            "failure_phase",
            "candidate_started",
            "candidate_activated",
            "rollback_selected",
            "journal_terminal_state",
            "real_release0_started",
            "real_release0_identity_match",
            "data_intact",
            "executable_identity",
            "semantic_data",
        },
        "scenarios.failed_candidate_rollback",
        errors,
    )
    _string_fields(
        rollback,
        {"status", "failure_phase", "journal_terminal_state"},
        "scenarios.failed_candidate_rollback",
        errors,
    )
    _typed_fields(
        rollback,
        {
            name: (bool,)
            for name in (
                "failure_injected",
                "candidate_started",
                "candidate_activated",
                "rollback_selected",
                "real_release0_started",
                "real_release0_identity_match",
                "data_intact",
            )
        },
        "scenarios.failed_candidate_rollback",
        errors,
    )
    _identity_pair_closed_world(
        rollback.get("executable_identity"),
        "scenarios.failed_candidate_rollback.executable_identity",
        errors,
    )
    _semantic_closed_world(
        rollback.get("semantic_data"),
        "scenarios.failed_candidate_rollback.semantic_data",
        errors,
    )
    uninstall = _exact_keys(
        scenarios.get("keep_data_uninstall"),
        {
            "status",
            "requested",
            "keep_data",
            "exit_code",
            "program_root_removed",
            "selection_removed",
            "native_service_removed",
            "data_root_exists",
            "workspace_and_cas_present",
            "runtime_processes_after",
            "unrelated_sentinel_survived",
            "semantic_data",
        },
        "scenarios.keep_data_uninstall",
        errors,
    )
    _string_fields(uninstall, {"status"}, "scenarios.keep_data_uninstall", errors)
    _typed_fields(
        uninstall,
        {
            **{
                name: (bool,)
                for name in (
                    "requested",
                    "keep_data",
                    "program_root_removed",
                    "selection_removed",
                    "native_service_removed",
                    "data_root_exists",
                    "workspace_and_cas_present",
                    "unrelated_sentinel_survived",
                )
            },
            "exit_code": (int,),
            "runtime_processes_after": (int,),
        },
        "scenarios.keep_data_uninstall",
        errors,
    )
    _semantic_closed_world(
        uninstall.get("semantic_data"), "scenarios.keep_data_uninstall.semantic_data", errors
    )
    stop = _exact_keys(
        scenarios.get("stop_safety"), {"status", "cases"}, "scenarios.stop_safety", errors
    )
    _string_fields(stop, {"status"}, "scenarios.stop_safety", errors)
    cases = _exact_keys(
        stop.get("cases"), set(failure.STOP_OUTCOMES), "scenarios.stop_safety.cases", errors
    )
    for outcome in failure.STOP_OUTCOMES:
        prefix = f"scenarios.stop_safety.cases.{outcome}"
        case = _exact_keys(
            cases.get(outcome),
            {
                "outcome",
                "reason",
                "update_admitted",
                "candidate_downloaded_or_written",
                "selection_changed",
                "journal_changed_to_update",
                "candidate_activated",
                "data_changed",
                "automatic_retry",
                "executable_identity",
                "semantic_data",
                "mutation_inventory",
            },
            prefix,
            errors,
        )
        _string_fields(case, {"outcome", "reason"}, prefix, errors)
        _typed_fields(
            case,
            {
                name: (bool,)
                for name in (
                    "update_admitted",
                    "candidate_downloaded_or_written",
                    "selection_changed",
                    "journal_changed_to_update",
                    "candidate_activated",
                    "data_changed",
                    "automatic_retry",
                )
            },
            prefix,
            errors,
        )
        _identity_pair_closed_world(
            case.get("executable_identity"), f"{prefix}.executable_identity", errors
        )
        _semantic_closed_world(case.get("semantic_data"), f"{prefix}.semantic_data", errors)
        inventory = _exact_keys(
            case.get("mutation_inventory"),
            {"before", "after", "unchanged"},
            f"{prefix}.mutation_inventory",
            errors,
        )
        _typed_fields(
            inventory,
            {"before": (str,), "after": (str,), "unchanged": (bool,)},
            f"{prefix}.mutation_inventory",
            errors,
        )
    return errors


def _adapter_closed_world_errors(observation: Mapping[str, Any]) -> list[str]:
    errors = _scenario_closed_world_errors(observation)
    observation_fields = {
        "slot_id",
        "job_id",
        "mode",
        "os",
        "runner_image",
        "runner_provenance",
        "os_build",
        "shell",
        "source_override_test_mode",
        "candidate_checkouts",
        "manifest_sha256",
        "release0_fixture",
        "scenarios",
        "execution_scope",
        "hosted_authentication",
        "collector_provenance",
        "collector_structural_sha256",
        "owner",
        "native_service",
        "evidence_files",
    }
    receipt_fields = {
        "schema_version",
        "receipt_kind",
        "receipt_state",
        "checks",
        "status",
        "verification",
    }
    top_allowed = observation_fields | receipt_fields
    _exact_keys(observation, top_allowed, "observation", errors)
    if set(observation) & receipt_fields:
        if set(observation) != top_allowed:
            errors.append("observation: exact recorded receipt fields required")
        _typed_fields(
            observation,
            {
                "schema_version": (int,),
                "receipt_kind": (str,),
                "receipt_state": (str,),
                "status": (str,),
            },
            "observation",
            errors,
        )
        if observation.get("schema_version") != failure.SCHEMA_VERSION:
            errors.append("schema_version: exact failure receipt version required")
        if observation.get("receipt_kind") != failure.RECEIPT_KIND:
            errors.append("receipt_kind: exact failure receipt kind required")
        if observation.get("receipt_state") != "recorded":
            errors.append("receipt_state: recorded required")
        status = observation.get("status")
        if type(status) is not str or status not in {"pass", "fail"}:
            errors.append("status: pass/fail string required")
        checks = _exact_keys(observation.get("checks"), set(failure.SCENARIOS), "checks", errors)
        if set(checks) != set(failure.SCENARIOS):
            errors.append("checks: exact four failure scenario keys required")
        for scenario in failure.SCENARIOS:
            value = checks.get(scenario)
            if type(value) is not str or value not in {"pass", "fail"}:
                errors.append(f"checks.{scenario}: pass/fail string required")
        verification = _exact_keys(
            observation.get("verification"),
            {"state", "verifier", "failed_assertions"},
            "verification",
            errors,
        )
        if set(verification) != {"state", "verifier", "failed_assertions"}:
            errors.append("verification: exact receipt verification fields required")
        _typed_fields(
            verification,
            {"state": (str,), "verifier": (str,), "failed_assertions": (list,)},
            "verification",
            errors,
        )
        verification_state = verification.get("state")
        if type(verification_state) is not str or verification_state not in {
            "verified-pass",
            "verified-fail",
        }:
            errors.append("verification.state: verified-pass/verified-fail string required")
        expected_verifier = f"{failure.RECEIPT_KIND}/v{failure.SCHEMA_VERSION}"
        if verification.get("verifier") != expected_verifier:
            errors.append("verification.verifier: exact failure receipt verifier required")
        failed_assertions = verification.get("failed_assertions")
        if not isinstance(failed_assertions, list) or any(
            type(item) is not str for item in failed_assertions
        ):
            errors.append("verification.failed_assertions: exact string list required")
    _string_fields(
        observation,
        {
            "slot_id",
            "job_id",
            "mode",
            "os",
            "runner_image",
            "os_build",
            "shell",
            "manifest_sha256",
            "execution_scope",
            "collector_structural_sha256",
        },
        "observation",
        errors,
    )
    _typed_fields(observation, {"source_override_test_mode": (bool,)}, "observation", errors)
    runner = _exact_keys(
        observation.get("runner_provenance"),
        {"RUNNER_OS", "ImageOS", "ImageVersion"},
        "runner_provenance",
        errors,
    )
    _string_fields(runner, {"RUNNER_OS", "ImageOS", "ImageVersion"}, "runner_provenance", errors)
    checkouts = _exact_keys(
        observation.get("candidate_checkouts"),
        {"app", "agent", "runtime", "pinokio"},
        "candidate_checkouts",
        errors,
    )
    for name in ("app", "agent", "runtime", "pinokio"):
        checkout = _exact_keys(
            checkouts.get(name), {"sha", "path"}, f"candidate_checkouts.{name}", errors
        )
        _string_fields(checkout, {"sha", "path"}, f"candidate_checkouts.{name}", errors)
    collector = _exact_keys(
        observation.get("collector_provenance"),
        {
            "kind",
            "hosted_admissible",
            "environment",
            "command_runner",
            "process_probe",
            "data_probe",
            "durable_journal_probe",
            "journey_driver",
        },
        "collector_provenance",
        errors,
    )
    _string_fields(
        collector,
        {
            "kind",
            "environment",
            "command_runner",
            "process_probe",
            "data_probe",
            "durable_journal_probe",
            "journey_driver",
        },
        "collector_provenance",
        errors,
    )
    _typed_fields(collector, {"hosted_admissible": (bool,)}, "collector_provenance", errors)
    owner = _exact_keys(
        observation.get("owner"),
        {"slot_id", "mode", "managed_by", "foreground_fallback", "checkout_sha256"},
        "owner",
        errors,
    )
    _string_fields(owner, {"slot_id", "mode", "managed_by", "checkout_sha256"}, "owner", errors)
    _typed_fields(owner, {"foreground_fallback": (bool,)}, "owner", errors)
    hosted_keys = {
        "provider",
        "CI",
        "GITHUB_ACTIONS",
        "RUNNER_ENVIRONMENT",
        "GITHUB_SERVER_URL",
        "RUNNER_OS",
        "ImageOS",
        "ImageVersion",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
        "GITHUB_REPOSITORY",
        "os_build",
        "shell",
        "probe_match",
        "authenticated",
        "requires_admission_context",
    }
    hosted = _exact_keys(
        observation.get("hosted_authentication"), hosted_keys, "hosted_authentication", errors
    )
    _string_fields(
        hosted,
        hosted_keys - {"probe_match", "authenticated", "requires_admission_context"},
        "hosted_authentication",
        errors,
    )
    _typed_fields(
        hosted,
        {
            "probe_match": (bool,),
            "authenticated": (bool,),
            "requires_admission_context": (bool,),
        },
        "hosted_authentication",
        errors,
    )
    native = _exact_keys(
        observation.get("native_service"),
        {
            "manager",
            "manager_version",
            "principal",
            "target",
            "capability",
            "registration",
            "instances",
            "controller_interruption",
            "removal",
            "raw_observations",
            "raw_observations_sha256",
        },
        "native_service",
        errors,
    )
    registration_keys = {
        "target",
        "definition_path",
        "definition_sha256",
        "manager_definition_sha256",
        "action",
        "action_sha256",
        "principal",
        "definition_owner",
        "working_directory",
    }
    if observation.get("os") == "windows":
        registration_keys |= {
            "source_xml_sha256",
            "exported_xml_sha256",
            "task_semantic_sha256",
            "task_semantics",
        }
    registration = _exact_keys(
        native.get("registration"), registration_keys, "native_service.registration", errors
    )
    _string_fields(
        native,
        {
            "manager",
            "manager_version",
            "principal",
            "target",
            "capability",
            "raw_observations_sha256",
        },
        "native_service",
        errors,
    )
    _string_fields(
        registration,
        registration_keys - {"action", "task_semantics"},
        "native_service.registration",
        errors,
    )
    action = registration.get("action")
    if "action" in registration and (
        not isinstance(action, list) or any(type(item) is not str for item in action)
    ):
        errors.append("native_service.registration.action: exact string list required")
    if observation.get("os") == "windows":
        semantics = _exact_keys(
            registration.get("task_semantics"),
            {
                "execute",
                "arguments",
                "working_directory",
                "principal",
                "multiple_instances_policy",
                "disallow_start_on_batteries",
                "stop_on_batteries",
            },
            "native_service.registration.task_semantics",
            errors,
        )
        _string_fields(
            semantics,
            {
                "execute",
                "arguments",
                "working_directory",
                "principal",
                "multiple_instances_policy",
                "disallow_start_on_batteries",
                "stop_on_batteries",
            },
            "native_service.registration.task_semantics",
            errors,
        )
    instances = native.get("instances")
    if isinstance(instances, list):
        for index, row in enumerate(instances):
            instance = _exact_keys(
                row,
                {"scenario", "phase", *INSTANCE_IDENTITY_FIELDS},
                f"native_service.instances[{index}]",
                errors,
            )
            _string_fields(
                instance,
                {"scenario", "phase", *set(DATA_IDENTITY_FIELDS), "state"},
                f"native_service.instances[{index}]",
                errors,
            )
            _typed_fields(
                instance,
                {
                    "pid": (int, type(None)),
                    **{
                        name: (str, type(None))
                        for name in INSTANCE_IDENTITY_FIELDS[1:7]
                        if name != "pid"
                    },
                },
                f"native_service.instances[{index}]",
                errors,
            )
    removal_keys = {
        "definition_absent",
        "manager_target_absent",
        "owned_processes_after",
        "all_authorized_incarnations_absent",
        "observed_action_count",
        "observed_incarnation_count",
        "post_data_identity",
        "post_data_identity_sha256",
        "data_identity_unchanged",
        "observed",
        "requested",
        "owned_target_revalidated",
        "performed_by",
        "authorization_sha256",
        "authorized_identity_sha256",
        "authorized_registration_sha256",
        "authorized_actions_sha256",
        "authorized_incarnations_sha256",
        "authorized_data_identity_sha256",
        "authorization_raw_index",
        "absence_raw_index",
    }
    removal = _exact_keys(native.get("removal"), removal_keys, "native_service.removal", errors)
    _typed_fields(
        removal,
        {
            **{
                name: (bool,)
                for name in (
                    "definition_absent",
                    "manager_target_absent",
                    "all_authorized_incarnations_absent",
                    "data_identity_unchanged",
                    "observed",
                    "requested",
                    "owned_target_revalidated",
                )
            },
            **{
                name: (int,)
                for name in (
                    "owned_processes_after",
                    "observed_action_count",
                    "observed_incarnation_count",
                    "authorization_raw_index",
                    "absence_raw_index",
                )
            },
        },
        "native_service.removal",
        errors,
    )
    _string_fields(
        removal,
        removal_keys
        - {
            "post_data_identity",
            "definition_absent",
            "manager_target_absent",
            "owned_processes_after",
            "all_authorized_incarnations_absent",
            "observed_action_count",
            "observed_incarnation_count",
            "data_identity_unchanged",
            "observed",
            "requested",
            "owned_target_revalidated",
            "authorization_raw_index",
            "absence_raw_index",
        },
        "native_service.removal",
        errors,
    )
    post_identity = _exact_keys(
        removal.get("post_data_identity"),
        set(DATA_IDENTITY_FIELDS),
        "native_service.removal.post_data_identity",
        errors,
    )
    _string_fields(
        post_identity,
        set(DATA_IDENTITY_FIELDS),
        "native_service.removal.post_data_identity",
        errors,
    )
    controller = _exact_keys(
        native.get("controller_interruption"),
        {
            "kind",
            "runtime_operation_id",
            "durable_phase",
            "durable_journal_path",
            "durable_journal_sha256",
            "interrupted_controller",
            "recovery_controller",
            "local_baseexception",
        },
        "native_service.controller_interruption",
        errors,
    )
    _string_fields(
        controller,
        {
            "kind",
            "runtime_operation_id",
            "durable_phase",
            "durable_journal_path",
            "durable_journal_sha256",
        },
        "native_service.controller_interruption",
        errors,
    )
    _typed_fields(
        controller,
        {"local_baseexception": (bool,)},
        "native_service.controller_interruption",
        errors,
    )
    for role in ("interrupted_controller", "recovery_controller"):
        complete = _exact_keys(
            controller.get(role),
            {
                "controller",
                "controller_sha256",
                "authorization_sha256",
                "exact_birth_absent",
            },
            f"native_service.controller_interruption.{role}",
            errors,
        )
        controller_identity = _exact_keys(
            complete.get("controller"),
            {
                "pid",
                "process_birth_id",
                "controller_instance_id",
                "executable_path",
                "executable_sha256",
                "runtime_operation_id",
                "durable_phase",
                "durable_journal_path",
                "durable_journal_sha256",
            },
            f"native_service.controller_interruption.{role}.controller",
            errors,
        )
        _string_fields(
            complete,
            {"controller_sha256", "authorization_sha256"},
            f"native_service.controller_interruption.{role}",
            errors,
        )
        _typed_fields(
            complete,
            {"exact_birth_absent": (bool,)},
            f"native_service.controller_interruption.{role}",
            errors,
        )
        _string_fields(
            controller_identity,
            {
                "process_birth_id",
                "controller_instance_id",
                "executable_path",
                "executable_sha256",
                "runtime_operation_id",
                "durable_phase",
                "durable_journal_path",
                "durable_journal_sha256",
            },
            f"native_service.controller_interruption.{role}.controller",
            errors,
        )
        _typed_fields(
            controller_identity,
            {"pid": (int,)},
            f"native_service.controller_interruption.{role}.controller",
            errors,
        )
    evidence = observation.get("evidence_files")
    if isinstance(evidence, list):
        for index, item in enumerate(evidence):
            entry = _exact_keys(
                item,
                {
                    "path",
                    "sha256",
                    "kind",
                    "scenario",
                    "collector_kind",
                    "collector_structural_sha256",
                    "execution_scope",
                },
                f"evidence_files[{index}]",
                errors,
            )
            _string_fields(
                entry,
                {
                    "path",
                    "sha256",
                    "kind",
                    "scenario",
                    "collector_kind",
                    "collector_structural_sha256",
                    "execution_scope",
                },
                f"evidence_files[{index}]",
                errors,
            )
    return errors


def _native_structural_errors(observation: Mapping[str, Any]) -> list[str]:
    errors = _adapter_closed_world_errors(observation)
    if tuple(failure.REQUIRED_SLOT_IDS) != REQUIRED_NATIVE_SLOT_IDS:
        errors.append("slot contract: exact nine slot tuple/order differs")
    slot_id = observation.get("slot_id")
    if slot_id not in REQUIRED_NATIVE_SLOT_IDS:
        return ["slot_id: unknown M4-6 native slot"]
    contract = failure.SLOT_CONTRACTS.get(slot_id)
    if contract is None:
        return ["slot_id: missing M4-6 slot contract"]
    os_name, runner_os, image_os = failure.RUNNER_PROVENANCE[contract["runner"]]
    for name, expected in (
        ("job_id", slot_id),
        ("mode", contract["mode"]),
        ("os", os_name),
        ("runner_image", contract["runner"]),
        ("shell", contract["shell"]),
    ):
        if observation.get(name) != expected:
            errors.append(f"{name}: slot contract differs")
    if observation.get("source_override_test_mode") is not False:
        errors.append("source_override_test_mode: false required")
    if contract["mode"] != "direct":
        errors.append("owner.policy: NEEDS approved Pinokio custody policy")
    runner_provenance = _mapping(observation.get("runner_provenance"))
    expected_runner_provenance = {
        "RUNNER_OS": runner_os,
        "ImageOS": image_os,
        "ImageVersion": _mapping(observation.get("hosted_authentication")).get("ImageVersion"),
    }
    if runner_provenance != expected_runner_provenance:
        errors.append("runner_provenance: exact hosted runner binding differs")
    manifest_sha256 = observation.get("manifest_sha256")
    scenarios = _mapping(observation.get("scenarios"))
    candidate_identity = _mapping(
        _mapping(scenarios.get("interruption_recovery")).get("terminal_identity")
    )
    if (
        not _digest(manifest_sha256)
        or candidate_identity.get("selected_release") != "candidate"
        or candidate_identity.get("version_manifest_sha256") != manifest_sha256
    ):
        errors.append("manifest_sha256: candidate scenario binding differs")
    release0 = _mapping(observation.get("release0_fixture"))
    if set(release0) != {
        "fixture_sums_sha256",
        "manifest_sha256",
        "provenance_sha256",
        "bootstrap_sha256",
        "sources",
        "launcher_refs",
    }:
        errors.append("release0_fixture: exact frozen receipt fields required")
    errors.extend(failure._release0_fixture_errors(release0))
    collector = _mapping(observation.get("collector_provenance"))
    collector_sha = observation.get("collector_structural_sha256")
    if collector_sha != _sha256_bytes(_canonical(collector)):
        errors.append("collector_structural_sha256: structural collector binding differs")
    fixture_collector = collector.get("kind") == "injected-platform-fixture"
    live_collector = collector.get("kind") == "live-platform"
    if not fixture_collector and not live_collector:
        errors.append("collector_provenance.kind: known collector kind required")
    if fixture_collector and (
        collector.get("hosted_admissible") is not False
        or observation.get("execution_scope") != FIXTURE_SCOPE
    ):
        errors.append("collector_provenance: injected collector scope differs")
    if live_collector and observation.get("execution_scope") != HOSTED_SCOPE:
        errors.append("execution_scope: live collector requires hosted-native-service")
    hosted = _mapping(observation.get("hosted_authentication"))
    exact_hosted = {
        "provider": "github-actions",
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "GITHUB_SERVER_URL": "https://github.com",
        "RUNNER_OS": runner_os,
        "ImageOS": image_os,
        "shell": contract["shell"],
    }
    for name, expected in exact_hosted.items():
        if hosted.get(name) != expected:
            errors.append(f"hosted_authentication.{name}: exact provenance differs")
    for name in (
        "ImageVersion",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
        "GITHUB_REPOSITORY",
        "os_build",
    ):
        if not _nonempty(hosted.get(name)):
            errors.append(f"hosted_authentication.{name}: required")
    if SYNTHETIC_IMAGE.search(str(hosted.get("ImageVersion", ""))):
        errors.append("hosted_authentication.ImageVersion: synthetic value forbidden")
    if (
        hosted.get("authenticated") is not False
        or hosted.get("requires_admission_context") is not True
        or hosted.get("probe_match") is not True
    ):
        errors.append("hosted_authentication: serialized provenance is structural only")
    if observation.get("os_build") != f"{runner_os}-{hosted.get('os_build', '')}":
        errors.append("os_build: live build binding differs")
    checkouts = _mapping(observation.get("candidate_checkouts"))
    checkout_values: dict[str, str] = {}
    for name in ("app", "agent", "runtime", "pinokio"):
        row = _mapping(checkouts.get(name))
        sha = row.get("sha")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
            errors.append(f"candidate_checkouts.{name}.sha: exact commit required")
        else:
            checkout_values[name] = sha
        if not _nonempty(row.get("path")):
            errors.append(f"candidate_checkouts.{name}.path: required")
    owner = _mapping(observation.get("owner"))
    expected_checkout_sha = _sha256_bytes(_canonical(checkout_values))
    for name, expected in (
        ("slot_id", slot_id),
        ("mode", contract["mode"]),
        ("managed_by", "native-service"),
        ("foreground_fallback", False),
        ("checkout_sha256", expected_checkout_sha),
    ):
        if owner.get(name) != expected:
            errors.append(f"owner.{name}: exact owner binding differs")
    native = _mapping(observation.get("native_service"))
    if native.get("manager") != MANAGERS[os_name]:
        errors.append("native_service.manager: platform manager differs")
    if native.get("capability") != "pass" or not _nonempty(native.get("manager_version")):
        errors.append("native_service.capability/manager_version: live capability required")
    registration = _mapping(native.get("registration"))
    if native.get("principal") != registration.get("principal") or not _nonempty(
        registration.get("principal")
    ):
        errors.append("native_service.principal: registration principal differs")
    expected_target = {
        "linux": SYSTEMD_UNIT,
        "windows": WINDOWS_TASK_PATH + WINDOWS_TASK_NAME,
    }.get(os_name)
    target = native.get("target")
    if os_name == "macos":
        if re.fullmatch(rf"gui/\d+/{re.escape(SERVICE_LABEL)}", str(target)) is None:
            errors.append("native_service.target: canonical launchd target required")
    elif target != expected_target:
        errors.append("native_service.target: canonical manager target differs")
    if registration.get("target") != target:
        errors.append("native_service.registration.target: manager target differs")
    action = registration.get("action")
    if (
        not isinstance(action, list)
        or not action
        or not all(_nonempty(item) for item in action)
        or registration.get("action_sha256") != _sha256_bytes(_canonical(action))
        or registration.get("definition_owner") != "product-installer"
        or not _digest(registration.get("definition_sha256"))
    ):
        errors.append("native_service.registration: exact definition/action binding required")
    definition_path = str(registration.get("definition_path", ""))
    if not (Path(definition_path).is_absolute() or PureWindowsPath(definition_path).is_absolute()):
        errors.append("native_service.registration.definition_path: absolute path required")
    if os_name == "macos" and not definition_path.endswith(
        f"/Library/LaunchAgents/{SERVICE_LABEL}.plist"
    ):
        errors.append("native_service.registration.definition_path: canonical LaunchAgent required")
    if os_name == "linux" and not definition_path.endswith(f"/systemd/user/{SYSTEMD_UNIT}"):
        errors.append("native_service.registration.definition_path: canonical user unit required")
    if os_name == "windows":
        semantics = _mapping(registration.get("task_semantics"))
        if (
            semantics.get("execute") != (action[0] if isinstance(action, list) and action else None)
            or semantics.get("arguments")
            != subprocess.list2cmdline(action[1:] if isinstance(action, list) else [])
            or semantics.get("working_directory") != registration.get("working_directory", "")
            or semantics.get("principal") != registration.get("principal")
            or semantics.get("multiple_instances_policy") != "IgnoreNew"
            or semantics.get("disallow_start_on_batteries") != "false"
            or semantics.get("stop_on_batteries") != "false"
            or registration.get("task_semantic_sha256") != _sha256_bytes(_canonical(semantics))
            or registration.get("source_xml_sha256") != registration.get("definition_sha256")
            or not _digest(registration.get("exported_xml_sha256"))
            or registration.get("manager_definition_sha256")
            != registration.get("exported_xml_sha256")
        ):
            errors.append("native_service.registration: Scheduled Task semantic XML differs")
    elif registration.get("manager_definition_sha256") != registration.get("definition_sha256"):
        errors.append("native_service.registration: manager definition digest differs")
    rows = native.get("instances")
    keep_before: Mapping[str, Any] = {}
    if isinstance(rows, list):
        keep_before = next(
            (
                _mapping(row)
                for row in rows
                if _mapping(row).get("scenario") == "keep_data_uninstall"
                and _mapping(row).get("phase") == "before"
            ),
            {},
        )
    removal = _mapping(native.get("removal"))
    registered_action = registration.get("action")
    action_suffix = (
        tuple(str(item) for item in registered_action[1:])
        if isinstance(registered_action, list)
        else ()
    )
    expected_actions = {
        (str(_mapping(row).get("executable_path")), *action_suffix)
        for row in rows or []
        if _mapping(row).get("state") == "running"
        and _nonempty(_mapping(row).get("executable_path"))
    }
    if isinstance(registered_action, list) and registered_action:
        expected_actions.add(tuple(str(item) for item in registered_action))
    expected_action_rows = [list(action) for action in sorted(expected_actions)]
    expected_incarnations = sorted(
        {
            (_mapping(row)["pid"], _mapping(row)["process_birth_id"])
            for row in rows or []
            if _mapping(row).get("state") == "running"
            and type(_mapping(row).get("pid")) is int
            and _nonempty(_mapping(row).get("process_birth_id"))
        }
    )
    expected_incarnation_rows = [
        {"pid": pid, "process_birth_id": birth} for pid, birth in expected_incarnations
    ]
    expected_data_identity = _data_identity(keep_before)
    for name in (
        "requested",
        "owned_target_revalidated",
        "definition_absent",
        "manager_target_absent",
        "all_authorized_incarnations_absent",
        "data_identity_unchanged",
        "observed",
    ):
        if removal.get(name) is not True:
            errors.append(f"native_service.removal.{name}: true required")
    if (
        removal.get("owned_processes_after") != 0
        or removal.get("performed_by") != "product-uninstall"
        or not _digest(removal.get("authorization_sha256"))
        or removal.get("authorized_registration_sha256") != _sha256_bytes(_canonical(registration))
        or removal.get("authorized_identity_sha256")
        != _sha256_bytes(_canonical(_instance_identity(keep_before)))
        or removal.get("authorized_actions_sha256")
        != _sha256_bytes(_canonical(expected_action_rows))
        or removal.get("authorized_incarnations_sha256")
        != _sha256_bytes(_canonical(expected_incarnation_rows))
        or removal.get("authorized_data_identity_sha256")
        != _sha256_bytes(_canonical(expected_data_identity))
        or removal.get("observed_action_count") != len(expected_action_rows)
        or removal.get("observed_incarnation_count") != len(expected_incarnation_rows)
        or _mapping(removal.get("post_data_identity")) != expected_data_identity
        or removal.get("post_data_identity_sha256")
        != _sha256_bytes(_canonical(expected_data_identity))
    ):
        errors.append("native_service.removal: authorization/absence binding differs")
    raw = native.get("raw_observations")
    if not isinstance(raw, list) or not raw:
        errors.append("native_service.raw_observations: sanitized metadata required")
        raw = []
    if native.get("raw_observations_sha256") != _sha256_bytes(_canonical(raw)):
        errors.append("native_service.raw_observations_sha256: metadata binding differs")
    required_labels = {"owned-runtime-termination"}
    if os_name == "macos":
        required_labels |= {
            "principal-uid",
            "principal-name",
            "launchd-version",
            "launchd-gui-capability",
            "launchd-query",
            "launchd-bootstrap",
            "launchd-start",
        }
    elif os_name == "linux":
        required_labels |= {
            "principal-uid",
            "principal-name",
            "systemd-version",
            "systemd-session-capability",
            "systemd-query",
            "systemd-reload",
            "systemd-enable",
            "systemd-start",
        }
    else:
        required_labels |= {
            "task-scheduler-capability",
            "task-scheduler-query",
            "task-scheduler-register",
        }
    labels: set[str] = set()
    raw_contract: dict[str, tuple[str, int, set[int]]] = {}
    if os_name == "macos":
        raw_contract = {
            "principal-uid": ("id", 2, {0}),
            "principal-name": ("id", 2, {0}),
            "launchd-version": ("launchctl", 2, {0}),
            "launchd-gui-capability": ("launchctl", 4, {0}),
            "launchd-query": ("launchctl", 3, {0, 3}),
            "launchd-bootstrap": ("launchctl", 4, {0}),
            "launchd-start": ("launchctl", 3, {0}),
            "launchd-restart": ("launchctl", 4, {0}),
            "launchd-cleanup-remove": ("launchctl", 3, {0}),
            "owned-runtime-termination": ("kill", 3, {0}),
        }
    elif os_name == "linux":
        raw_contract = {
            "principal-uid": ("id", 2, {0}),
            "principal-name": ("id", 2, {0}),
            "systemd-version": ("systemctl", 3, {0}),
            "systemd-session-capability": ("systemctl", 3, {0, 1}),
            "systemd-query": ("systemctl", 5, {0, 4}),
            "systemd-reload": ("systemctl", 3, {0}),
            "systemd-enable": ("systemctl", 4, {0}),
            "systemd-start": ("systemctl", 4, {0}),
            "systemd-restart": ("systemctl", 4, {0}),
            "systemd-cleanup-remove": ("systemctl", 5, {0}),
            "systemd-cleanup-reload": ("systemctl", 3, {0}),
            "owned-runtime-termination": ("kill", 3, {0}),
        }
    else:
        raw_contract = {
            label: ("powershell.exe", 5, {0})
            for label in (
                "task-scheduler-capability",
                "task-scheduler-query",
                "task-scheduler-register",
                "task-scheduler-restart",
                "task-scheduler-cleanup-remove",
                "owned-runtime-termination",
            )
        }
    for index, value in enumerate(raw):
        row = _mapping(value)
        label = row.get("label")
        labels.add(str(label or ""))
        if "stdout" in row or "stderr" in row or "argv" in row:
            errors.append(f"native_service.raw_observations[{index}]: unsanitized data")
        allowed_keys = {
            "label",
            "command",
            "argc",
            "argv_sha256",
            "returncode",
            "stdout_bytes",
            "stdout_sha256",
            "stderr_bytes",
            "stderr_sha256",
            "parsed",
        }
        if not set(row).issubset(allowed_keys):
            errors.append(f"native_service.raw_observations[{index}]: unknown metadata field")
        command = row.get("command")
        expected_command, expected_argc, expected_returncodes = raw_contract.get(
            str(label), ("", -1, set())
        )
        if (
            not _nonempty(label)
            or not _nonempty(command)
            or Path(str(command)).name != command
            or command != expected_command
            or type(row.get("argc")) is not int
            or row.get("argc") != expected_argc
            or type(row.get("returncode")) is not int
            or row.get("returncode") not in expected_returncodes
            or type(row.get("stdout_bytes")) is not int
            or row.get("stdout_bytes", -1) < 0
            or type(row.get("stderr_bytes")) is not int
            or row.get("stderr_bytes", -1) < 0
            or (row.get("stdout_bytes") == 0) != (row.get("stdout_sha256") == _sha256_bytes(b""))
            or (row.get("stderr_bytes") == 0) != (row.get("stderr_sha256") == _sha256_bytes(b""))
            or ("parsed" in row and not isinstance(row.get("parsed"), Mapping))
        ):
            errors.append(f"native_service.raw_observations[{index}]: result metadata differs")
        parsed = row.get("parsed")
        if parsed is not None:
            parsed_row = _mapping(parsed)
            parsed_allowed: set[str] = set()
            if label == "launchd-gui-capability":
                parsed_allowed = {"gui_uid", "available"}
            elif label == "systemd-session-capability":
                parsed_allowed = {"state"}
            elif label == "task-scheduler-capability":
                parsed_allowed = {"version", "module", "sid"}
            elif label == "launchd-query":
                parsed_allowed = (
                    {"present"}
                    if parsed_row.get("present") is False
                    else {"present", "definition_path_sha256", "action_sha256", "state", "pid"}
                )
            elif label == "systemd-query":
                parsed_allowed = (
                    {"present", "load_state"}
                    if parsed_row.get("present") is False
                    else {
                        "id",
                        "names",
                        "load_state",
                        "state",
                        "substate",
                        "pid",
                        "manager_instance_id",
                        "need_reload",
                        "definition_path_sha256",
                        "action_sha256",
                    }
                )
            elif label == "task-scheduler-query":
                parsed_allowed = (
                    {"task_count", "com_present"}
                    if parsed_row.get("task_count") == 0
                    else {
                        "task_count",
                        "com_present",
                        "action_count",
                        "principal",
                        "state",
                        "instances",
                        "exported_xml_sha256",
                        "exported_semantic_sha256",
                    }
                )
            if not parsed_allowed:
                errors.append(
                    f"native_service.raw_observations[{index}].parsed: forbidden for operation"
                )
            else:
                _exact_keys(
                    parsed_row,
                    parsed_allowed,
                    f"native_service.raw_observations[{index}].parsed",
                    errors,
                )
                parsed_prefix = f"native_service.raw_observations[{index}].parsed"
                parsed_schema: dict[str, tuple[type, ...]] = {}
                if label == "launchd-gui-capability":
                    parsed_schema = {"gui_uid": (int,), "available": (bool,)}
                elif label == "systemd-session-capability":
                    parsed_schema = {"state": (str,)}
                elif label == "task-scheduler-capability":
                    parsed_schema = {
                        "version": (str,),
                        "module": (str,),
                        "sid": (str,),
                    }
                elif label == "launchd-query":
                    parsed_schema = {
                        "present": (bool,),
                        "definition_path_sha256": (str,),
                        "action_sha256": (str,),
                        "state": (str,),
                        "pid": (int,),
                    }
                elif label == "systemd-query":
                    parsed_schema = {
                        "present": (bool,),
                        "id": (str,),
                        "load_state": (str,),
                        "state": (str,),
                        "substate": (str,),
                        "pid": (int,),
                        "manager_instance_id": (str,),
                        "need_reload": (str,),
                        "definition_path_sha256": (str,),
                        "action_sha256": (str,),
                    }
                elif label == "task-scheduler-query":
                    parsed_schema = {
                        "task_count": (int,),
                        "com_present": (bool,),
                        "action_count": (int,),
                        "principal": (str,),
                        "state": (str,),
                        "exported_xml_sha256": (str,),
                        "exported_semantic_sha256": (str,),
                    }
                _typed_fields(parsed_row, parsed_schema, parsed_prefix, errors)
                parsed_instances = parsed_row.get("instances")
                if isinstance(parsed_instances, list):
                    for parsed_index, instance in enumerate(parsed_instances):
                        instance_row = _exact_keys(
                            instance,
                            {"manager_instance_id", "pid"},
                            f"native_service.raw_observations[{index}].parsed.instances[{parsed_index}]",
                            errors,
                        )
                        _typed_fields(
                            instance_row,
                            {"manager_instance_id": (str,), "pid": (int,)},
                            f"native_service.raw_observations[{index}].parsed.instances[{parsed_index}]",
                            errors,
                        )
                elif "instances" in parsed_row:
                    errors.append(f"{parsed_prefix}.instances: exact list required")
                for parsed_name, parsed_value in parsed_row.items():
                    if parsed_name == "instances":
                        continue
                    if parsed_name == "names":
                        if not isinstance(parsed_value, list) or any(
                            not isinstance(item, str) for item in parsed_value
                        ):
                            errors.append(
                                f"native_service.raw_observations[{index}].parsed.names: strings required"
                            )
                    elif isinstance(parsed_value, (Mapping, list)):
                        errors.append(
                            f"native_service.raw_observations[{index}].parsed.{parsed_name}: nested data forbidden"
                        )
        for name in ("argv_sha256", "stdout_sha256", "stderr_sha256"):
            if not _digest(row.get(name)):
                errors.append(f"native_service.raw_observations[{index}].{name}: digest required")
    if not required_labels.issubset(labels):
        errors.append("native_service.raw_observations: required manager operations absent")
    auth_index = removal.get("authorization_raw_index")
    absence_index = removal.get("absence_raw_index")
    if (
        type(auth_index) is not int
        or type(absence_index) is not int
        or auth_index < 0
        or absence_index != auth_index + 1
        or absence_index >= len(raw)
    ):
        errors.append("native_service.removal: raw authorization/absence indexes differ")
    else:
        query_label = {
            "macos": "launchd-query",
            "linux": "systemd-query",
            "windows": "task-scheduler-query",
        }[os_name]
        if (
            _mapping(raw[auth_index]).get("label") != query_label
            or _mapping(raw[absence_index]).get("label") != query_label
        ):
            errors.append("native_service.removal: manager query operations differ")
    evidence = observation.get("evidence_files")
    if not isinstance(evidence, list) or len(evidence) != len(failure.SCENARIOS):
        errors.append("evidence_files: exact scenario evidence entries required")
    else:
        for index, scenario in enumerate(failure.SCENARIOS):
            item = _mapping(evidence[index])
            if (
                item.get("path") != f"evidence/{scenario}.json"
                or item.get("scenario") != scenario
                or item.get("kind") != "machine-observation"
                or not _digest(item.get("sha256"))
                or item.get("collector_kind") != collector.get("kind")
                or item.get("collector_structural_sha256") != collector_sha
                or item.get("execution_scope") != observation.get("execution_scope")
            ):
                errors.append(f"evidence_files[{index}]: collector/scope/scenario binding differs")
    errors.extend(_scenario_instance_errors(observation))
    return errors


def native_observation_errors(observation: Mapping[str, Any]) -> list[str]:
    """Validate serialized structure; serialized data can never admit itself."""
    errors = _native_structural_errors(observation)
    errors.append("hosted_admission_verifier: NEEDS B/platform-owned external authentication")
    return errors


def _evidence_bundle(
    observation: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    collector = _mapping(observation.get("collector_provenance"))
    collector_sha = observation.get("collector_structural_sha256")
    native = _mapping(observation.get("native_service"))
    files: list[dict[str, Any]] = []
    payloads: dict[str, bytes] = {}
    scenario_fields = {
        "interruption_recovery": (
            "status",
            "operation_id",
            "phase",
            "injected",
            "first_request",
            "recovery_request",
            "terminal_state",
            "duplicate_operation",
            "recovery_verified",
            "terminal_identity",
            "semantic_data",
            "native_controller_interruption",
        ),
        "failed_candidate_rollback": (
            "status",
            "failure_injected",
            "failure_phase",
            "candidate_started",
            "candidate_activated",
            "rollback_selected",
            "journal_terminal_state",
            "real_release0_started",
            "real_release0_identity_match",
            "data_intact",
            "executable_identity",
            "semantic_data",
        ),
        "keep_data_uninstall": (
            "status",
            "requested",
            "keep_data",
            "exit_code",
            "program_root_removed",
            "selection_removed",
            "native_service_removed",
            "data_root_exists",
            "workspace_and_cas_present",
            "runtime_processes_after",
            "unrelated_sentinel_survived",
            "semantic_data",
        ),
        "stop_safety": ("status", "cases"),
    }
    for scenario in failure.SCENARIOS:
        relative = f"evidence/{scenario}.json"
        scenario_row = _mapping(_mapping(observation.get("scenarios")).get(scenario))
        payload = {
            "execution_scope": observation.get("execution_scope"),
            "slot_id": observation.get("slot_id"),
            "scenario": scenario,
            "collector_provenance": collector,
            "collector_structural_sha256": collector_sha,
            "observation": {
                name: copy.deepcopy(scenario_row[name])
                for name in scenario_fields[scenario]
                if name in scenario_row
            },
            "native_instances": [
                row
                for row in native.get("instances", [])
                if isinstance(row, Mapping)
                and str(row.get("scenario", "")).split(".", 1)[0] == scenario
            ],
            "sanitized_native_manager_observations": native.get("raw_observations", []),
        }
        data = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
        payloads[relative] = data
        files.append(
            {
                "path": relative,
                "sha256": _sha256_bytes(data),
                "kind": "machine-observation",
                "scenario": scenario,
                "collector_kind": collector.get("kind"),
                "collector_structural_sha256": collector_sha,
                "execution_scope": observation.get("execution_scope"),
            }
        )
    return files, payloads


def _execute_structural_collection(
    slot_id: str,
    candidate: Mapping[str, str],
    fixture: Path,
    work_root: Path,
    *,
    app_repo: Path,
    agent_repo: Path,
    runtime_repo: Path,
    pinokio_repo: Path,
    environment: Mapping[str, str] | None = None,
    _runner: CommandRunner | None = None,
    _process_probe: ProcessProbe | None = None,
    _journey_driver: JourneyDriver | None = None,
    _fixture_powershell: Path | None = None,
    _allow_platform_fixture: bool = False,
) -> dict[str, Any]:
    contract = failure.SLOT_CONTRACTS.get(slot_id)
    if contract is None:
        raise ValueError(f"unknown failure-mode slot: {slot_id}")
    if failure.candidate_errors(candidate):
        raise ValueError("candidate is not a fully pinned tuple")
    if contract["mode"] != "direct":
        raise RuntimeError(f"{slot_id}: NEEDS B/platform approval for Pinokio custody")
    driver = _journey_driver
    if driver is None and JOURNEY_DRIVER_FACTORY is None:
        raise RuntimeError("NEEDS approved shared hosted journey-driver registration")
    fixture_mode = any(
        value is not None
        for value in (
            environment,
            _runner,
            _process_probe,
            _journey_driver,
            _fixture_powershell,
        )
    )
    if fixture_mode and not _allow_platform_fixture:
        raise RuntimeError("injected collectors are inadmissible at hosted entry point")
    env = dict(os.environ if environment is None else environment)
    runner = _runner or SubprocessCommandRunner()
    os_name, runner_os, image_os = failure.RUNNER_PROVENANCE[contract["runner"]]
    if _platform_name() != os_name and not fixture_mode:
        raise RuntimeError("live operating system differs from slot")
    powershell = (
        _powershell_path(env, fixture_path=_fixture_powershell) if os_name == "windows" else None
    )
    process_probe = _process_probe or LiveProcessProbe(runner, powershell)
    collector = {
        "kind": "injected-platform-fixture" if fixture_mode else "live-platform",
        "hosted_admissible": not fixture_mode,
        "environment": "injected-mapping" if environment is not None else "live-process",
        "command_runner": type(runner).__name__,
        "process_probe": type(process_probe).__name__,
        "data_probe": "DataProbe",
        "durable_journal_probe": "DurableJournalProbe",
        "journey_driver": (
            "registered-shared-driver" if _journey_driver is None else "injected-platform-fixture"
        ),
    }
    collector_sha = _sha256_bytes(_canonical(collector))
    auth, os_build = _host_authentication(
        env, contract, runner, fixture_mode=fixture_mode, powershell=powershell
    )
    root = _work_root(Path(work_root), env)
    canonical_definition = _canonical_definition_path(os_name, env)
    repos = {
        "app": Path(app_repo),
        "agent": Path(agent_repo),
        "runtime": Path(runtime_repo),
        "pinokio": Path(pinokio_repo),
    }
    checkouts = _checkout_heads(candidate, repos, runner)
    fixture_path, _manifest, fixture_receipt = _verified_release0_fixture(Path(fixture))
    fixture_receipt = {**fixture_receipt, "launcher_refs": dict(failure.RELEASE0_LAUNCHERS)}
    owner = NativeOwner(
        os_name=os_name,
        canonical_definition_path=canonical_definition,
        runner=runner,
        process_probe=process_probe,
        data_probe=DataProbe(root),
        durable_journal_probe=DurableJournalProbe(root),
        powershell=powershell,
    )
    capability = owner.probe()
    owner.assert_absent()
    context: dict[str, Any] = {
        "slot_id": slot_id,
        "contract": dict(contract),
        "candidate": dict(candidate),
        "fixture": fixture_path,
        "work_root": root,
        "repos": repos,
        "candidate_checkouts": checkouts,
        "canonical_definition_path": canonical_definition,
        "definition_path_policy": (
            "product-driver-owned-task-xml"
            if os_name == "windows"
            else "platform-canonical-product-definition"
        ),
        "principal": owner.principal,
    }
    if driver is None:
        assert JOURNEY_DRIVER_FACTORY is not None
        driver = JOURNEY_DRIVER_FACTORY(context)
    spec = driver.service_spec(context)
    registration = owner.register_start(spec)
    session = NativeSession(owner)
    try:
        values = dict(driver.run(context, session))
        if set(values) != set(failure.SCENARIOS):
            raise RuntimeError("journey driver did not return exact four scenarios")
        if session.controller_interruption is None:
            raise RuntimeError("journey driver did not observe external controller interruption")
        values["interruption_recovery"] = dict(values["interruption_recovery"])
        values["interruption_recovery"]["native_controller_interruption"] = copy.deepcopy(
            session.controller_interruption
        )
        if session.removal is None:
            raise RuntimeError("journey driver did not authorize and observe product removal")
        owner_checkout_sha = _sha256_bytes(
            _canonical({name: candidate[name] for name in ("app", "agent", "runtime", "pinokio")})
        )
        observation: dict[str, Any] = {
            "slot_id": slot_id,
            "job_id": slot_id,
            "mode": contract["mode"],
            "os": os_name,
            "runner_image": contract["runner"],
            "runner_provenance": {
                "RUNNER_OS": runner_os,
                "ImageOS": image_os,
                "ImageVersion": auth["ImageVersion"],
            },
            "os_build": os_build,
            "shell": contract["shell"],
            "source_override_test_mode": False,
            "candidate_checkouts": checkouts,
            "manifest_sha256": candidate["manifest_sha256"],
            "release0_fixture": fixture_receipt,
            "scenarios": values,
            "execution_scope": FIXTURE_SCOPE if fixture_mode else HOSTED_SCOPE,
            "hosted_authentication": auth,
            "collector_provenance": collector,
            "collector_structural_sha256": collector_sha,
            "owner": {
                "slot_id": slot_id,
                "mode": contract["mode"],
                "managed_by": "native-service",
                "foreground_fallback": False,
                "checkout_sha256": owner_checkout_sha,
            },
            "native_service": {
                **capability,
                "capability": "pass",
                "registration": registration,
                "instances": session.instances,
                "controller_interruption": copy.deepcopy(session.controller_interruption),
                "removal": dict(session.removal),
                "raw_observations": owner.raw,
                "raw_observations_sha256": _sha256_bytes(_canonical(owner.raw)),
            },
        }
        closed_world_errors = _adapter_closed_world_errors(observation)
        if closed_world_errors:
            raise RuntimeError("; ".join(closed_world_errors))
        evidence_files, _evidence_payloads = _evidence_bundle(observation)
        observation["evidence_files"] = evidence_files
        base_errors = failure.evaluate_observation(slot_id, candidate, observation)
        native_errors = _native_structural_errors(observation)
        if base_errors or native_errors:
            raise RuntimeError("; ".join([*base_errors, *native_errors]))
        if not fixture_mode:
            verifier = HOSTED_ADMISSION_VERIFIER
            if verifier is None:
                raise RuntimeError(
                    "hosted_admission_verifier: NEEDS B/platform-owned external authentication"
                )
            try:
                capability = verifier.verify_live_observation(observation)
                consume = getattr(capability, "consume", None)
                digest = _sha256_bytes(_canonical(observation))
                if not callable(consume) or consume(digest) is not True:
                    raise RuntimeError("hosted_admission_verifier: external capability was denied")
            except RuntimeError:
                raise
            except Exception as verifier_exc:  # noqa: BLE001 - external verifier fails closed
                raise RuntimeError(
                    "hosted_admission_verifier: external authentication failed "
                    f"({type(verifier_exc).__name__})"
                ) from verifier_exc
        return observation
    except BaseException as exc:
        try:
            cleanup = session.safe_finalize()
        except (OSError, RuntimeError, ValueError) as cleanup_exc:
            raise RuntimeError(
                f"native journey failed ({type(exc).__name__}); cleanup refused; "
                f"residue may remain: {cleanup_exc}"
            ) from exc
        raise RuntimeError(
            f"native journey failed ({type(exc).__name__}); exact cleanup observed: "
            f"{cleanup.get('performed_by')}"
        ) from exc


execute = _execute_structural_collection


def _publish_atomic(
    observation: Mapping[str, Any],
    receipt: Mapping[str, Any],
    output: Path,
) -> None:
    evidence_files, evidence_payloads = _evidence_bundle(observation)
    if evidence_files != observation.get("evidence_files"):
        raise RuntimeError("validated evidence metadata changed before publication")
    publication_root = output.parent
    publication_parent = publication_root.parent
    publication_parent.mkdir(parents=True, exist_ok=True)
    if publication_root.exists():
        raise ValueError("output publication directory already exists")
    staged = Path(
        tempfile.mkdtemp(
            prefix=f".{publication_root.name}.native-publication-stage-",
            dir=publication_parent,
        )
    )
    try:
        for relative, data in evidence_payloads.items():
            path = staged / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        failure.write_json(staged / output.name, receipt)
        os.replace(staged, publication_root)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot-id", required=True, choices=failure.REQUIRED_SLOT_IDS)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--fixture", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--app-repo", required=True, type=Path)
    parser.add_argument("--agent-repo", required=True, type=Path)
    parser.add_argument("--runtime-repo", required=True, type=Path)
    parser.add_argument("--pinokio-repo", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        candidate = failure.read_json(args.candidate)
        observation = execute(
            args.slot_id,
            candidate,
            args.fixture,
            args.work_root,
            app_repo=args.app_repo,
            agent_repo=args.agent_repo,
            runtime_repo=args.runtime_repo,
            pinokio_repo=args.pinokio_repo,
        )
        receipt = failure.create_receipt(args.slot_id, candidate, observation)
        errors = [
            *_native_structural_errors(receipt),
            *failure.validate_receipt(receipt, candidate, args.slot_id),
        ]
        if errors:
            raise RuntimeError("; ".join(errors))
        _publish_atomic(observation, receipt, args.output)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Native failure-mode execution: FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"Native observation recorded ({args.slot_id}); provisional and not admitted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
