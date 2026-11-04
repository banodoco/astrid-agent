"""Exact process identity at the harness-only R0 pair admission boundary."""

import base64
import ctypes
import hashlib
import json
import os
import xml.etree.ElementTree as ET
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey


@pytest.fixture
def captured_r0():
    fixture = journey.ROOT / "tests/fixtures/windows-r0-owner-41769985291"
    provenance = json.loads((fixture / "provenance.json").read_text())
    captures = {}
    for name, facts in provenance["files"].items():
        raw = base64.b64decode((fixture / (name + ".b64")).read_bytes())
        assert len(raw) == facts["bytes"]
        assert hashlib.sha256(raw).hexdigest() == facts["sha256"]
        captures[name] = raw
    rows = json.loads(captures["runtime-owner-selection.json"])
    context = json.loads(captures["runtime-owner-selection-context.json"])
    paths = {item["path"]: item for item in context["paths"].values()}

    class CapturedPath(PureWindowsPath):
        # Replay every original Windows path and recorded filesystem predicate
        # without remapping executable paths or requiring a Windows filesystem.
        def stat(self):
            return SimpleNamespace(st_file_attributes=paths[str(self)]["file_attributes"])

        def is_symlink(self):
            return paths[str(self)]["is_symlink"]

        def is_file(self):
            return paths[str(self)]["is_file"]

        def read_text(self, *, encoding):
            assert encoding == "utf-8"
            return "\n".join(paths[str(self)]["home_lines"])

    version = CapturedPath(context["version_path"])
    discovery = context["discovery"]
    # These are supplied native observations, NOT native facts retained in the
    # artifact. Exercise every possible parent sub-microsecond digit below.
    native = {7636: 134382382606305690, 1492: 134382382606407668}
    return version, rows, discovery, native, context, captures


@pytest.mark.parametrize("parent_digit", range(10))
def test_real_r0_capture_is_blocked_only_by_lossy_birth_comparison(captured_r0, parent_digit):
    version, rows, discovery, native, _, captures = captured_r0
    owners = [r for r in rows if journey._runtime_owner_command(str(r.get("CommandLine") or ""))]
    assert [r["ProcessId"] for r in owners] == [7636, 1492]
    parent, child = owners
    assert child["ParentProcessId"] == parent["ProcessId"]
    assert 0 < int(parent["Birth"]) < int(child["Birth"])
    exact_child = int(discovery["process_birth_id"].split(":")[1])
    assert exact_child - int(child["Birth"]) == 8
    assert exact_child // 10 * 10 == int(child["Birth"])
    # The actual hosted R0 task supplies the entire, unmodified multiline tail.
    # The PowerShell text capture is UTF-8 while retaining the task export's
    # UTF-16 declaration. Decode the capture before parsing the XML content.
    action = ET.fromstring(captures["unit-file-before-cleanup.txt"].decode("utf-8")).find(
        ".//{*}Actions/{*}Exec"
    )
    assert parent["ExecutablePath"] == action.find("{*}Command").text
    assert (
        journey._windows_command_parts(parent["CommandLine"])[1]
        == " " + action.find("{*}Arguments").text
    )
    assert (
        journey._windows_command_parts(child["CommandLine"])[1]
        == journey._windows_command_parts(parent["CommandLine"])[1]
    )
    native[7636] += parent_digit
    assert journey._windows_r0_pair(rows, discovery, version, native)
    # Replaying only the retained CIM observations cannot establish identity.
    native[1492] = int(child["Birth"])
    assert not journey._windows_r0_pair(rows, discovery, version, native)


@pytest.mark.parametrize(
    "change",
    [
        "child_reuse_same_microsecond",
        "child_reuse_previous_tick",
        "child_reuse_next_microsecond",
        "parent_reuse",
        "missing_child_native",
        "missing_parent_native",
        "stale_child_census",
        "stale_parent_census",
        "extra_owner",
        "independent_owner",
        "wrong_image",
        "wrong_tail",
        "symlink",
        "reparse",
        "duplicate_home",
    ],
)
def test_real_r0_capture_still_fails_closed(captured_r0, change):
    version, rows, discovery, native, context, _ = captured_r0
    parent, child = [r for r in rows if r.get("ProcessId") in (7636, 1492)]
    if change == "child_reuse_same_microsecond":
        native[1492] += 1
    elif change == "child_reuse_previous_tick":
        native[1492] -= 1
    elif change == "child_reuse_next_microsecond":
        native[1492] += 2
    elif change == "parent_reuse":
        native[7636] = native[1492] + 1
    elif change == "missing_child_native":
        native[1492] = None
    elif change == "missing_parent_native":
        native[7636] = None
    elif change == "stale_child_census":
        child["Birth"] = str(int(child["Birth"]) - 10)
    elif change == "stale_parent_census":
        parent["Birth"] = str(int(parent["Birth"]) - 10)
    elif change == "extra_owner":
        rows.append(dict(child, ProcessId=1234))
    elif change == "independent_owner":
        child["ParentProcessId"] = 1234
    elif change == "wrong_image":
        child["ExecutablePath"] = r"C:\other\pythonw.exe"
    elif change == "wrong_tail":
        child["CommandLine"] += " "
    elif change == "symlink":
        context["paths"]["venv"]["is_symlink"] = True
    elif change == "reparse":
        context["paths"]["version_dir"]["file_attributes"] |= 0x400
    elif change == "duplicate_home":
        context["paths"]["config"]["home_lines"] *= 2
    assert not journey._windows_r0_pair(rows, discovery, version, native)


@pytest.mark.parametrize("bucket", [1000, 2**53 // 10 * 10, 134382382606407660, 134382382609999990])
@pytest.mark.parametrize("digit", range(10))
def test_exact_identity_at_precision_boundaries(captured_r0, bucket, digit):
    version, rows, discovery, _, _, _ = captured_r0
    parent, child = [r for r in rows if r.get("ProcessId") in (7636, 1492)]
    parent["Birth"], child["Birth"] = str(bucket - 100), str(bucket)
    native = {7636: bucket - 100, 1492: bucket + digit}
    discovery["process_birth_id"] = f"windows-creation:{native[1492]}"
    assert journey._windows_r0_pair(rows, discovery, version, native)
    # +/-1 includes both same-bucket collisions and boundary crossings.
    for offset in (-1, 1):
        changed = native.copy()
        changed[1492] += offset
        assert not journey._windows_r0_pair(rows, discovery, version, changed)


@pytest.mark.parametrize("failure", [None, "reuse", "unavailable", "access_denied"])
def test_native_observation_is_used_and_retained(captured_r0, tmp_path, monkeypatch, failure):
    version, rows, discovery, native, _, captures = captured_r0
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "healthy", lambda _: discovery)
    calls = []

    def native_read(pid):
        calls.append(pid)
        if pid == 1492:
            if failure == "reuse":
                return native[pid] + 1
            if failure == "unavailable":
                return None
            if failure == "access_denied":
                raise OSError("access denied")
        return native[pid]

    monkeypatch.setattr(journey, "_windows_creation_time", native_read)

    def census(command, filename, **kwargs):
        raw = captures["runtime-owner-selection.json"]
        (journey.windows.EVIDENCE / filename).write_bytes(raw)
        return raw.decode("utf-8")

    monkeypatch.setattr(journey.windows, "powershell", census)
    if failure:
        with pytest.raises(RuntimeError, match=r"found \[7636, 1492\]"):
            journey.assert_release0_runtime(tmp_path, version, tmp_path)
    else:
        journey.assert_release0_runtime(tmp_path, version, tmp_path)
    assert calls == [7636, 1492]
    assert (tmp_path / "runtime-owner-selection.json").read_bytes() == captures[
        "runtime-owner-selection.json"
    ]
    context = journey.read_json(tmp_path / "runtime-owner-selection-context.json")
    observed = context["native_creation_filetimes"]
    assert observed["7636"] == str(native[7636])
    assert observed["1492"] == (
        None
        if failure in ("unavailable", "access_denied")
        else str(native[1492] + (failure == "reuse"))
    )


@pytest.mark.parametrize("failure", [None, "open", "query", "exception"])
def test_native_filetime_reader_uses_query_only_handle_and_closes_it(monkeypatch, failure):
    calls = []
    created = 134382382606407668

    class Call:
        def __init__(self, fn):
            self.fn = fn

        def __call__(self, *args):
            return self.fn(*args)

    def open_process(access, inherit, pid):
        assert (access, inherit, pid) == (0x1000, False, 1492)
        calls.append("open")
        return 0 if failure == "open" else 73

    def get_times(handle, creation, *_):
        assert handle == 73
        calls.append("query")
        if failure == "exception":
            raise OSError("query failed")
        creation._obj.dwHighDateTime = created >> 32
        creation._obj.dwLowDateTime = created & 0xFFFFFFFF
        return failure != "query"

    def close(handle):
        assert handle == 73
        calls.append("close")
        return True

    kernel = SimpleNamespace(
        OpenProcess=Call(open_process), GetProcessTimes=Call(get_times), CloseHandle=Call(close)
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    if failure == "exception":
        with pytest.raises(OSError, match="query failed"):
            journey._windows_creation_time(1492)
    else:
        assert journey._windows_creation_time(1492) == (None if failure else created)
    assert calls == (["open"] if failure == "open" else ["open", "query", "close"])


@pytest.mark.windows_native
def test_native_reader_matches_current_process_kernel_filetime():
    # Run this focused suite on Windows to exercise the actual kernel API.
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    times = [wintypes.FILETIME() for _ in range(4)]
    assert kernel.GetProcessTimes(
        kernel.GetCurrentProcess(), *[ctypes.byref(value) for value in times]
    )
    expected = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    assert expected > 0
    assert journey._windows_creation_time(os.getpid()) == expected
