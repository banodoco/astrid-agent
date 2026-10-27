"""Boot-session identity for narrowly scoped, reboot-bound recovery proofs.

There is deliberately no wall-clock, uptime, PID, or file-time fallback. Windows
uses its kernel boot sequence, cross-checked against the documented successful
boot counter in HKLM. The shared-page x64 ABI is explicitly constrained below;
unknown layouts fail closed. The BCD loader GUID from information class 0x5A is
not a boot-session identifier and must never authorize recovery.
"""
from __future__ import annotations

import ctypes
import struct
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping


class BootIdentityUnavailable(OSError):
    """A current OS boot identity could not be proved."""


WINDOWS_BOOT_SOURCE = "windows-kernel-boot-id-v1"
_SOURCES = frozenset({WINDOWS_BOOT_SOURCE, "linux-proc-boot-id-v1", "darwin-kern-bootsessionuuid-v1"})
# Constrain the shared-page ABI to known Windows 10/11 x64 release families.
# Adding a family requires native qualification, not a permissive version range.
_WINDOWS_BUILDS = frozenset({10240, 10586, 14393, 15063, 16299, 17134, 17763,
                            18362, 18363, 19041, 19042, 19043, 19044, 19045,
                            22000, 22621, 22631, 26100, 26200})


def _canonical_guid(value: Any) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise BootIdentityUnavailable("boot identity GUID is malformed") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise BootIdentityUnavailable("boot identity GUID is invalid")
    return str(parsed)


def validate_boot_identity(value: Any) -> dict[str, Any]:
    if (not isinstance(value, Mapping) or set(value) != {"version", "source", "value"}
            or type(value.get("version")) is not int or value["version"] != 1
            or not isinstance(value.get("source"), str) or value["source"] not in _SOURCES):
        raise BootIdentityUnavailable("boot identity is missing or malformed")
    if value["source"] == WINDOWS_BOOT_SOURCE:
        identity = value["value"]
        if (not isinstance(identity, Mapping) or set(identity) != {"machine_guid", "boot_id"}
                or type(identity["boot_id"]) is not int or not 0 < identity["boot_id"] < 2**32):
            raise BootIdentityUnavailable("Windows boot counter identity is malformed")
        _canonical_guid(identity["machine_guid"])
        return {**value, "value": dict(identity)}
    _canonical_guid(value["value"])
    return dict(value)


def boot_identities_differ(prepared: Any, current: Any) -> bool:
    before, after = validate_boot_identity(prepared), validate_boot_identity(current)
    if before["source"] != after["source"]:
        raise BootIdentityUnavailable("boot identity sources differ")
    if before["source"] == WINDOWS_BOOT_SOURCE:
        if before["value"]["machine_guid"] != after["value"]["machine_guid"]:
            raise BootIdentityUnavailable("Windows machine identity changed")
        previous, now = before["value"]["boot_id"], after["value"]["boot_id"]
        if now < previous:
            raise BootIdentityUnavailable("Windows boot counter decreased or wrapped")
        return now > previous
    return before["value"] != after["value"]


def _read_windows_shared_boot_data(kernel: Any = None) -> bytes:
    """ReadProcessMemory bounds failure instead of dereferencing a native pointer."""
    try:
        if kernel is None:
            kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
            kernel.GetCurrentProcess.argtypes = []
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            kernel.ReadProcessMemory.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                 ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
            kernel.ReadProcessMemory.restype = ctypes.c_int
        data = ctypes.create_string_buffer(104)
        length = ctypes.c_size_t()
        # KUSER_SHARED_DATA's user mapping and stable prefix offsets are an ABI
        # dependency, not an exported Win32 boot-identity API. See the proof doc.
        success = kernel.ReadProcessMemory(kernel.GetCurrentProcess(), ctypes.c_void_p(0x7FFE0260),
                                           data, len(data), ctypes.byref(length))
        if not success or length.value != len(data):
            raise BootIdentityUnavailable("Windows kernel boot data read was denied or incomplete")
        return data.raw
    except (OSError, AttributeError, TypeError, ValueError) as exc:
        raise BootIdentityUnavailable("Windows kernel boot data unavailable") from exc


def _windows_kernel_boot_id(raw: bytes, version: tuple[int, int, int]) -> int:
    # Documented KUSER_SHARED_DATA fields: NtBuildNumber 0x260,
    # NativeProcessorArchitecture 0x26a, NtMajorVersion 0x26c,
    # NtMinorVersion 0x270, BootId 0x2c4. Only known x64 release ABIs qualify.
    if len(raw) != 104 or version[:2] != (10, 0) or version[2] not in _WINDOWS_BUILDS:
        raise BootIdentityUnavailable("Windows kernel boot data ABI is unsupported")
    build = struct.unpack_from("<I", raw, 0)[0] & 0xFFFF
    arch = struct.unpack_from("<H", raw, 10)[0]
    major, minor = struct.unpack_from("<II", raw, 12)
    counter = struct.unpack_from("<I", raw, 100)[0]
    if (major, minor, build) != version or arch != 9 or counter == 0:
        raise BootIdentityUnavailable("Windows kernel boot data ABI or sequence is invalid")
    return counter


def _query_windows_boot_identity(registry: Any = None) -> dict[str, Any]:
    try:
        if registry is None:
            import winreg as registry
        version = getattr(sys, "getwindowsversion")()
        native_version = (version.major, version.minor, version.build)
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise BootIdentityUnavailable("Windows boot identity requires a qualified x64 interpreter")
        before = _windows_kernel_boot_id(_read_windows_shared_boot_data(), native_version)
        access = registry.KEY_READ | registry.KEY_WOW64_64KEY
        with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, access) as key:
            machine, machine_type = registry.QueryValueEx(key, "MachineGuid")
        with registry.OpenKey(registry.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters", 0, access) as key:
            counter, counter_type = registry.QueryValueEx(key, "BootId")
        after = _windows_kernel_boot_id(_read_windows_shared_boot_data(), native_version)
        if (machine_type != registry.REG_SZ or not isinstance(machine, str)
                or counter_type != registry.REG_DWORD or type(counter) is not int
                or not 0 < counter < 2**32 or before != after or counter != before):
            raise BootIdentityUnavailable("Windows kernel and successful-boot counters disagree")
        machine = str(uuid.UUID(machine))
        _canonical_guid(machine)
        return {"machine_guid": machine, "boot_id": counter}
    except (OSError, ImportError, AttributeError, TypeError, ValueError) as exc:
        raise BootIdentityUnavailable("Windows kernel boot identity unavailable") from exc


def capture_boot_identity() -> dict[str, Any]:
    """Return a strict current boot identity, or fail closed without a fallback."""
    try:
        if sys.platform == "win32":
            value = _query_windows_boot_identity()
            if value != _query_windows_boot_identity():
                raise BootIdentityUnavailable("Windows boot identity changed during capture")
            source = WINDOWS_BOOT_SOURCE
        elif sys.platform == "linux":
            value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
            source = "linux-proc-boot-id-v1"
        elif sys.platform == "darwin":
            result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    check=True, timeout=5)
            value = str(uuid.UUID(result.stdout.strip()))
            source = "darwin-kern-bootsessionuuid-v1"
        else:
            raise BootIdentityUnavailable("OS boot identity is unsupported")
        return validate_boot_identity({"version": 1, "source": source, "value": value})
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
        raise BootIdentityUnavailable("current OS boot identity unavailable") from exc
