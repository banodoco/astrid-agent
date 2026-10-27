from __future__ import annotations

import ctypes
import struct
import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from astrid.core.execution import boot_identity as boot

GUID = "e954823c-0fac-4bc0-9f84-31e1514078fd"
BEFORE = {"version": 1, "source": boot.WINDOWS_BOOT_SOURCE, "value": {"machine_guid": GUID, "boot_id": 200}}
AFTER = {**BEFORE, "value": {**BEFORE["value"], "boot_id": 201}}


def shared_data(*, build=19045, major=10, minor=0, arch=9, counter=200):
    raw = bytearray(104)
    struct.pack_into("<I", raw, 0, build)
    struct.pack_into("<HII", raw, 10, arch, major, minor)
    struct.pack_into("<I", raw, 100, counter)
    return bytes(raw)


def fake_windows(monkeypatch, *, counter=200, counter_type=4, machine=GUID, machine_type=1, raw=None):
    monkeypatch.setattr(boot, "sys", SimpleNamespace(platform="win32", getwindowsversion=lambda: SimpleNamespace(major=10, minor=0, build=19045)))
    monkeypatch.setattr(boot, "_read_windows_shared_boot_data", lambda: shared_data() if raw is None else raw)
    def open_key(hive, name, reserved, access):
        assert hive == 1 and reserved == 0 and access == (0x20019 | 0x100)
        return nullcontext(name)
    return SimpleNamespace(KEY_READ=0x20019, KEY_WOW64_64KEY=0x100, HKEY_LOCAL_MACHINE=1,
                           REG_SZ=1, REG_DWORD=4, OpenKey=open_key,
                           QueryValueEx=lambda _key, name: (machine, machine_type) if name == "MachineGuid" else (counter, counter_type))


def test_windows_boot_counter_requires_matching_kernel_and_readonly_registry(monkeypatch):
    registry = fake_windows(monkeypatch)
    assert boot._query_windows_boot_identity(registry) == BEFORE["value"]
    assert boot.boot_identities_differ(BEFORE, AFTER)
    assert not boot.boot_identities_differ(BEFORE, BEFORE)


@pytest.mark.parametrize("fault", [{"build": 28000}, {"major": 11}, {"minor": 1}, {"arch": 12}, {"counter": 0}])
def test_kernel_boot_sequence_rejects_unknown_or_mismatched_abi(fault):
    with pytest.raises(boot.BootIdentityUnavailable):
        boot._windows_kernel_boot_id(shared_data(**fault), (10, 0, 19045))


@pytest.mark.parametrize("length", [0, 4, 100, 103, 105])
def test_kernel_boot_sequence_rejects_short_or_overlong_read(length):
    with pytest.raises(boot.BootIdentityUnavailable):
        boot._windows_kernel_boot_id(bytes(length), (10, 0, 19045))


@pytest.mark.parametrize("success,length", [(False, 104), (True, 0), (True, 103)])
def test_native_memory_query_denied_or_partial_is_not_proof(success, length):
    def read(process, address, data, size, returned):
        assert process == -1 and address.value == 0x7FFE0260 and size == 104
        ctypes.cast(returned, ctypes.POINTER(ctypes.c_size_t)).contents.value = length
        return success
    kernel = SimpleNamespace(GetCurrentProcess=lambda: -1, ReadProcessMemory=read)
    with pytest.raises(boot.BootIdentityUnavailable):
        boot._read_windows_shared_boot_data(kernel)


def test_native_memory_query_returns_exact_kernel_prefix():
    raw = shared_data()
    def read(_process, _address, data, size, returned):
        data.raw = raw
        ctypes.cast(returned, ctypes.POINTER(ctypes.c_size_t)).contents.value = size
        return 1
    assert boot._read_windows_shared_boot_data(SimpleNamespace(GetCurrentProcess=lambda: -1, ReadProcessMemory=read)) == raw


@pytest.mark.parametrize("fault", [
    {"counter": 199}, {"counter": 201}, {"counter": 0}, {"counter": True}, {"counter": 2**32},
    {"counter_type": 1}, {"machine_type": 4}, {"machine": "invalid"}, {"machine": "00000000-0000-0000-0000-000000000000"},
])
def test_successful_boot_registry_metadata_must_match_native_kernel(monkeypatch, fault):
    registry = fake_windows(monkeypatch, **fault)
    with pytest.raises(boot.BootIdentityUnavailable):
        boot._query_windows_boot_identity(registry)


def test_registry_access_denied_has_no_fallback(monkeypatch):
    registry = fake_windows(monkeypatch)
    def denied(*_args):
        raise PermissionError("HKLM access denied")
    registry.OpenKey = denied
    with pytest.raises(boot.BootIdentityUnavailable):
        boot._query_windows_boot_identity(registry)


@pytest.mark.parametrize("count", [1, 199])
def test_reset_decrease_and_wrap_never_prove_new_boot(count):
    with pytest.raises(boot.BootIdentityUnavailable, match="decreased or wrapped"):
        boot.boot_identities_differ(BEFORE, {**AFTER, "value": {**AFTER["value"], "boot_id": count}})


def test_counter_maximum_to_one_is_not_a_new_boot_proof():
    maximum = {**BEFORE, "value": {**BEFORE["value"], "boot_id": 2**32 - 1}}
    with pytest.raises(boot.BootIdentityUnavailable, match="decreased or wrapped"):
        boot.boot_identities_differ(maximum, {**AFTER, "value": {**AFTER["value"], "boot_id": 1}})


def test_clock_changes_cannot_turn_same_boot_into_permission(monkeypatch):
    import time
    registry = fake_windows(monkeypatch)
    def forbidden():
        pytest.fail("boot proof must not consult a clock")
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", forbidden)
        patch.setattr(time, "monotonic", forbidden)
        captured = {**BEFORE, "value": boot._query_windows_boot_identity(registry)}
        assert not boot.boot_identities_differ(BEFORE, captured)


def test_machine_identity_must_remain_exact():
    with pytest.raises(boot.BootIdentityUnavailable, match="machine identity"):
        boot.boot_identities_differ(BEFORE, {**AFTER, "value": {**AFTER["value"], "machine_guid": "618947b2-d244-47ab-8dd2-7b86b8b7c101"}})


@pytest.mark.parametrize("value", [None, {}, {**BEFORE, "version": True}, {**BEFORE, "source": "clock"},
    {**BEFORE, "value": GUID}, {**BEFORE, "value": {"machine_guid": GUID, "boot_id": True}},
    {**BEFORE, "source": "windows-native-boot-guid-v1", "value": GUID},
])
def test_invalid_identity_or_loader_guid_cannot_prove_reboot(value):
    with pytest.raises(boot.BootIdentityUnavailable):
        boot.boot_identities_differ(value, AFTER)


def test_boot_identity_sources_are_not_interchangeable():
    with pytest.raises(boot.BootIdentityUnavailable):
        boot.boot_identities_differ({"version": 1, "source": "linux-proc-boot-id-v1", "value": GUID}, AFTER)


def test_windows_capture_rejects_unstable_kernel_identifier(monkeypatch):
    fake_windows(monkeypatch)
    values = iter([BEFORE["value"], AFTER["value"]])
    monkeypatch.setattr(boot, "_query_windows_boot_identity", lambda: next(values))
    with pytest.raises(boot.BootIdentityUnavailable, match="unavailable"):
        boot.capture_boot_identity()


@pytest.mark.windows_native
def test_windows_native_boot_identity_is_stable_in_child_process():
    """Unavailable native proof is a failing qualification, never an allowed skip."""
    import json
    import subprocess
    identity = boot.capture_boot_identity()
    child = subprocess.run([sys.executable, "-c", "import json; from astrid.core.execution.boot_identity import capture_boot_identity; print(json.dumps(capture_boot_identity()))"],
                           capture_output=True, text=True, check=True, timeout=10)
    assert identity == json.loads(child.stdout)
    assert identity["source"] == boot.WINDOWS_BOOT_SOURCE
