"""Focused tests for Windows assertion-time owner evidence and strict identity gates."""
import hashlib
import json
import shutil
import pytest
from scripts import ci_journey as journey

def _windows_census_owner(pid, parent_pid, command):
    """Build the bounded record emitted by the Windows census for a matcher hit."""
    return {
        "ProcessId": pid,
        "ParentProcessId": parent_pid,
        "CreationDate": "2026-11-02T18:51:58.6815710Z",
        "NativeIdentity": f"windows-creation:{pid}",
        "ExecutablePath": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if "powershell.exe" in command else r"C:\Astrid\python.exe",
        "Matcher": "runtime_protocol-start",
        "CommandLength": len(command),
        "CommandSha256": hashlib.sha256(command.encode()).hexdigest(),
        "SanitizedCommandLine": "-m runtime_protocol start",
    }


def test_tst_b1_windows_runtime_owner_census_is_empty_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")

    def powershell(code, name, **kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        assert name == "runtime-owner-processes.json"
        return "[]"

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    assert journey.runtime_processes(tmp_path) == []


def test_tst_b1_windows_runtime_owner_census_keeps_genuine_runtime_protocol_start(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    command = r"C:\Astrid\python.exe -X utf8 -m runtime_protocol start --support-root C:\data\runtime"
    row = _windows_census_owner(8661, 7200, command)

    def powershell(code, *unused, **kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        return json.dumps([row])

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    assert journey.runtime_processes(tmp_path) == [8661]


def test_tst_b1_windows_runtime_owner_census_excludes_its_own_powershell_probe(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    probe_command = (
        "powershell.exe -NoProfile -NonInteractive -Command "
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        + journey.WINDOWS_RUNTIME_OWNER_CENSUS
    )
    runtime_child_command = (
        r"C:\Astrid\python.exe -X utf8 -m runtime_protocol start "
        r"--support-root C:\data\runtime"
    )
    # The pre-fix census matched both: its own literal-bearing PowerShell
    # command and a genuine Runtime child.  The source guard must remove only
    # the former from the records it serializes.
    candidates = [
        (8660, 7200, probe_command),
        (8661, 8660, runtime_child_command),
    ]
    assert all(journey._runtime_owner_command(command) for _pid, _parent, command in candidates)

    def powershell(code, *unused, **kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        assert "Where-Object { $_.ProcessId -ne $PID }" in code
        return json.dumps([
            _windows_census_owner(pid, parent_pid, command)
            for pid, parent_pid, command in candidates
            if pid != 8660
        ])

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    assert journey.runtime_processes(tmp_path) == [8661]


def test_tst_b1_windows_runtime_owner_census_errors_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "healthy", lambda _data: {"pid": 8661})

    def powershell(code, *unused, **kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        raise RuntimeError("CIM enumeration unavailable")

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    evidence = tmp_path / "evidence"
    with pytest.raises(RuntimeError, match="CIM enumeration unavailable"):
        journey.assert_one_runtime(tmp_path, evidence=evidence)
    snapshot = journey.read_json(next(evidence.glob("runtime-owner-assertion-*.json")))
    assert snapshot["assertion"]["asserted_owner_pids"] == {
        "status": "unavailable", "reason": "owner census failed: RuntimeError",
    }


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_tst_b1_census_finds_installed_python_c_owner_and_foreign_duplicate(
        tmp_path, monkeypatch, platform):
    monkeypatch.setattr(journey.sys, "platform", platform)
    # The actual service prelude is long and may be rendered with newlines by ps.
    installed = ("/selected/py/bin/python -X utf8 -I -c import json,os,sys\n" +
                 "# " + "padding" * 1000 + "\n" +
                 "from astrid_runtime.cli import main\n" +
                 "sys.exit(main(['run', '--profile', 'astrid', '--data-root', '/selected']))")
    foreign = installed.replace("/selected", "/foreign")
    unrelated = "python -c from astrid_runtime.cli import main; main(['status'])"
    if platform == "win32":
        rows = [{"ProcessId": 4123, "CommandLine": installed},
                {"ProcessId": 4124, "CommandLine": foreign},
                {"ProcessId": 4125, "CommandLine": unrelated}]
        monkeypatch.setattr(journey.windows, "powershell", lambda *args, **kwargs: json.dumps(rows))
    else:
        def ps(argv, **kwargs):
            assert argv == ["ps", "-ww", "-axo", "pid=,command="]
            return f" 4123 {installed}\n 4124 {foreign}\n 4125 {unrelated}\n"
        monkeypatch.setattr(journey.subprocess, "check_output", ps)
    assert journey.runtime_processes(tmp_path) == [4123, 4124]
    monkeypatch.setattr(journey, "healthy", lambda data: {"pid": 4123})
    with pytest.raises(RuntimeError, match=r"found \[4123, 4124\]"):
        journey.assert_one_runtime(tmp_path)


def test_tst_b1_windows_duplicate_owner_error_retains_bounded_identity_evidence(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    owner = (r"C:\Astrid\pythonw.exe -X utf8 -I -c "
             "os.environ.update({'API_TOKEN':'must-not-be-retained'}); "
             "from astrid_runtime.cli import main; main(['run', '--profile', 'astrid'])")
    selected = {
        "ProcessId": 3032, "ParentProcessId": 1920,
        "CreationDate": "2026-11-02T17:18:21.973746+00:00",
        "NativeIdentity": "windows-creation:134381135019737465",
        "ExecutablePath": r"C:\Astrid\pythonw.exe", "CommandLine": owner,
    }
    duplicate = {
        "ProcessId": 2972, "ParentProcessId": 1920,
        "CreationDate": "2026-11-02T17:18:18.000000+00:00",
        "NativeIdentity": "windows-creation:134381135010000000",
        "ExecutablePath": r"C:\Astrid\pythonw.exe", "CommandLine": owner,
    }
    rows = [selected]

    def powershell(code, *unused, **kwargs):
        assert "GetProcessTimes" in code
        assert "SanitizedCommandLine" in code
        assert "CommandSha256" in code
        return json.dumps(rows)

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    monkeypatch.setattr(journey, "healthy", lambda data: {
        "pid": 3032,
        "process_birth_id": "windows-creation:134381135019737465",
        "runtime_instance_id": "instance-3032",
        "active_realm": "realm-3032",
        "coordinator_epoch": "epoch-3032",
    })
    evidence = tmp_path / "journey" / "evidence" / "release0"
    # The unchanged strict assertion passes and creates no diagnostic until it
    # actually observes the duplicate failure.
    journey.assert_one_runtime(tmp_path, evidence=evidence)
    assert not list(evidence.glob("runtime-owner-assertion-*.json"))
    rows.insert(0, duplicate)
    with pytest.raises(RuntimeError) as failure:
        journey.assert_one_runtime(tmp_path, evidence=evidence)
    message = str(failure.value)
    assert "selected Runtime owner 3032, found [2972, 3032]" in message
    assert '"matcher":"astrid_runtime-cli-run"' in message
    assert '"parent_pid":1920' in message
    assert '"creation_date":"2026-11-02T17:18:21.973746+00:00"' in message
    assert hashlib.sha256(owner.encode()).hexdigest() in message
    assert owner not in message
    assert "must-not-be-retained" not in message

    snapshot_path = next(evidence.glob("runtime-owner-assertion-*.json"))
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot = journey.read_json(snapshot_path)
    assert snapshot["capture"]["timing"] == "captured before the assertion raises"
    assert snapshot["capture"]["atomicity"]["status"] == "not-established"
    assert snapshot["assertion"]["asserted_owner_pids"] == [2972, 3032]
    assert snapshot["assertion"]["selected_pid_bound_to_asserted_owner"] == {
        "status": "observed", "record_index": 1,
    }
    assert snapshot["selected_discovery_identity"]["process_birth_id"] == {
        "status": "observed", "value": "windows-creation:134381135019737465",
    }
    owners = {row["pid"]["value"]: row for row in snapshot["asserted_owners"]}
    assert set(owners) == {2972, 3032}
    selected_snapshot = owners[3032]
    assert selected_snapshot["command_line"] == {
        "status": "observed", "value": "-c <astrid_runtime.cli main(['run', ...])>",
    }
    assert selected_snapshot["command_sha256"]["value"] == hashlib.sha256(owner.encode()).hexdigest()
    assert selected_snapshot["executable_path"] == {
        "status": "observed", "value": r"C:\Astrid\pythonw.exe",
    }
    assert selected_snapshot["creation_time"]["value"] == "2026-11-02T17:18:21.973746+00:00"
    assert selected_snapshot["native_identity"]["value"] == "windows-creation:134381135019737465"
    assert selected_snapshot["parent_child_relation"] == {
        "status": "observed", "parent_is_asserted_owner": False,
        "asserted_children": [], "shared_parent_with": [2972],
    }
    assert "must-not-be-retained" not in snapshot_path.read_text()

    # A later census may observe different rows, but cannot replace the first
    # failure-time record or its staged copies.
    rows[:] = [selected, {**duplicate, "ExecutablePath": r"C:\Other\pythonw.exe"}]
    with pytest.raises(RuntimeError):
        journey.assert_one_runtime(tmp_path, evidence=evidence)
    assert snapshot_path.read_bytes() == snapshot_bytes
    snapshots = list(evidence.glob("runtime-owner-assertion-*.json"))
    assert len(snapshots) == 2
    journey.retain_named_evidence(evidence.parent, "before-cleanup")
    staged = tmp_path / "evidence-upload" / "before-cleanup"
    assert len(list(staged.glob("runtime-service-service-owner-assertion-*.log"))) == 2
    shutil.rmtree(evidence)
    journey.retain_named_evidence(evidence.parent, "after-cleanup")
    after = staged.parent / "after-cleanup"
    assert {p.name: p.read_bytes() for p in staged.glob("*owner-assertion*.log")} == {
        p.name: p.read_bytes() for p in after.glob("*owner-assertion*.log")}



def test_tst_b1_windows_owner_failure_snapshot_marks_missing_fields_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    rows = [
        {"ProcessId": 2972, "ParentProcessId": 1920,
         "CreationDate": None, "NativeIdentity": None, "ExecutablePath": None,
         "Matcher": "astrid_runtime-cli-run", "CommandLength": None,
         "CommandSha256": None, "SanitizedCommandLine": None},
        {"ProcessId": 3032, "ParentProcessId": None,
         "CreationDate": None, "NativeIdentity": None, "ExecutablePath": None,
         "Matcher": "astrid_runtime-cli-run", "CommandLength": None,
         "CommandSha256": None, "SanitizedCommandLine": None},
    ]

    def powershell(code, *unused, **kwargs):
        assert "NativeIdentity" in code
        return json.dumps(rows)

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    monkeypatch.setattr(journey, "healthy", lambda data: {"pid": 3032})
    evidence = tmp_path / "evidence" / "release0"
    with pytest.raises(RuntimeError) as failure:
        journey.assert_one_runtime(tmp_path, evidence=evidence)
    assert "selected Runtime owner 3032, found [2972, 3032]" in str(failure.value)
    snapshot = journey.read_json(next(evidence.glob("runtime-owner-assertion-*.json")))
    selected = next(row for row in snapshot["asserted_owners"] if row["pid"]["value"] == 3032)
    assert snapshot["selected_discovery_identity"]["process_birth_id"] == {
        "status": "unavailable", "reason": "discovery process_birth_id was unavailable",
    }
    assert selected["command_sha256"] == {
        "status": "unavailable", "reason": "command-line digest was unavailable",
    }
    assert selected["executable_path"] == {
        "status": "unavailable", "reason": "Win32_Process.ExecutablePath was unavailable",
    }
    assert selected["creation_time"] == {
        "status": "unavailable", "reason": "Win32_Process.CreationDate was unavailable",
    }
    assert selected["native_identity"] == {
        "status": "unavailable", "reason": "GetProcessTimes creation identity was unavailable",
    }
    assert selected["parent_child_relation"] == {
        "status": "unavailable", "parent_is_asserted_owner": None,
        "asserted_children": [], "shared_parent_with": [],
        "reason": "parent PID was unavailable",
    }


_ARTIFACT_VENV = (r"D:\a\_temp\astrid-r1-direct-windows\program\versions\3204bb00-"
                  r"6d53a190-f042d729\py\Scripts\pythonw.exe")
_ARTIFACT_BASE = (r"D:\a\_temp\astrid-r1-direct-windows\localappdata\Astrid Installer" "\\"
                  r"bootstrap\python\cpython-3.11.13-windows-x86_64-none\pythonw.exe")
_ARTIFACT_TAIL_SHA256 = "00d7b317229dfb6c2e46d4f0a0a903af124a578f2997ccfd1c84596b36f7389e"


def _artifact_shaped_windows_owners(*, child_identity="windows-creation:134381205278019339",
                                    child_parent=6976, child_image=_ARTIFACT_BASE,
                                    action_image=_ARTIFACT_VENV,
                                    parent_tail=_ARTIFACT_TAIL_SHA256,
                                    child_tail=_ARTIFACT_TAIL_SHA256,
                                    child_arguments_match=True,
                                    action_tail=_ARTIFACT_TAIL_SHA256,
                                    parent_created="2026-11-02T19:15:27.791539+00:00",
                                    child_created="2026-11-02T19:15:27.801933+00:00",
                                    extra=()):
    """The retained 41507782325 process topology, without secret command text."""
    parent = {
        "pid": 6976,
        "parent_pid": 1880,
        "creation_date": parent_created,
        "native_identity": "windows-creation:134381205277915396",
        "executable_path": _ARTIFACT_VENV,
        "command_tail_sha256": parent_tail,
        "command_tail_argument_count": 4,
        "registered_arguments_match": True,
    }
    child = {
        "pid": 5224,
        "parent_pid": child_parent,
        "creation_date": child_created,
        "native_identity": child_identity,
        "executable_path": child_image,
        "command_tail_sha256": child_tail,
        "command_tail_argument_count": 4,
        "registered_arguments_match": child_arguments_match,
    }
    task_action = {
        "status": "observed",
        "task_path": "\\",
        "executable_path": action_image,
        "arguments_sha256": action_tail,
        "argument_count": 4,
    }
    details = [parent, child, *extra]
    return journey._RuntimeOwnerPids([row["pid"] for row in details], details, task_action)


def _artifact_shaped_discovery():
    return {
        "pid": 5224,
        "process_birth_id": "windows-creation:134381205278019339",
        "runtime_instance_id": "eac11355544e43cb87aa37c391715b25",
        "active_realm": "d924bbae-6693-4db1-9b57-cfc8651ed7b9",
        "coordinator_epoch": "eac11355544e43cb87aa37c391715b25",
    }


def test_tst_b1_windows_artifact_shaped_venv_redirector_is_one_logical_owner(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    owners = _artifact_shaped_windows_owners()
    monkeypatch.setattr(journey, "healthy", lambda _data: _artifact_shaped_discovery())
    monkeypatch.setattr(journey, "runtime_processes", lambda _data: owners)
    monkeypatch.setattr(journey, "_windows_venv_base_executable", lambda _path: _ARTIFACT_BASE)

    journey.assert_one_runtime(tmp_path, evidence=tmp_path / "evidence" / "release0")

    snapshot = journey.read_json(next((tmp_path / "evidence" / "release0").glob("runtime-owner-assertion-*.json")))
    assert snapshot["receipt_kind"] == "windows-runtime-owner-logical-owner-assertion"
    assert snapshot["assertion"]["asserted_owner_pids"] == [6976, 5224]
    assert snapshot["assertion"]["logical_owner_pids"] == [5224]
    assert snapshot["assertion"]["logical_owner_normalization"]["status"] == "collapsed"
    assert snapshot["assertion"]["logical_owner_normalization"]["selected_native_identity"] == (
        "windows-creation:134381205278019339")
    assert snapshot["assertion"]["logical_owner_normalization"]["task_action"] == {
        "executable_path": _ARTIFACT_VENV,
        "arguments_sha256": _ARTIFACT_TAIL_SHA256,
        "argument_count": 4,
    }


def test_tst_b1_windows_census_retains_raw_pair_and_redacted_task_action(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    owners = _artifact_shaped_windows_owners()
    observation = {
        "owners": [{
            "ProcessId": row["pid"], "ParentProcessId": row["parent_pid"],
            "CreationDate": row["creation_date"], "NativeIdentity": row["native_identity"],
            "ExecutablePath": row["executable_path"], "Matcher": "astrid_runtime-cli-run",
            "CommandLength": 3710, "CommandSha256": "a" * 64,
            "CommandTailSha256": row["command_tail_sha256"],
            "CommandTailArgumentCount": row["command_tail_argument_count"],
            "RegisteredArgumentsMatch": row["registered_arguments_match"],
            "SanitizedCommandLine": "-c <astrid_runtime.cli main(['run', ...])>",
        } for row in owners.details],
        "task_action": owners.task_action,
        "probe_pid": 9999,
    }

    def powershell(code, *unused, **_kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        assert "probe_pid = $PID" in code
        return json.dumps(observation)

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    retained = journey.runtime_processes(tmp_path)
    assert list(retained) == [6976, 5224]
    assert retained.task_action == owners.task_action
    assert retained.probe_pid == 9999
    assert [row["parent_pid"] for row in retained.details] == [1880, 6976]
    assert all(row["registered_arguments_match"] for row in retained.details)


@pytest.mark.parametrize("case", [
    "wrong-base-image", "changed-argv", "unreadable-pyvenv-cfg", "missing-selected-identity",
    "missing-discovery-identity", "unrelated-parent", "creation-order", "third-owner",
    "genuine-second-interpreter", "wrong-task-path", "missing-task-action",
    "wrong-action-image", "false-argument-match", "missing-parent-identity",
])
def test_tst_b1_windows_logical_owner_normalization_rejects_every_unproven_owner(
        tmp_path, monkeypatch, case):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    kwargs = {}
    if case == "wrong-base-image":
        kwargs["child_image"] = r"D:\foreign\pythonw.exe"
    elif case == "changed-argv":
        kwargs["child_tail"] = "f" * 64
        kwargs["child_arguments_match"] = False
    elif case == "missing-selected-identity":
        kwargs["child_identity"] = None
    elif case == "creation-order":
        kwargs["parent_created"] = "2026-11-02T19:15:27.901933+00:00"
    elif case == "unrelated-parent":
        kwargs["child_parent"] = 1880
    elif case == "third-owner":
        kwargs["extra"] = ({
            "pid": 7111, "parent_pid": 6976,
            "creation_date": "2026-11-02T19:15:27.802000+00:00",
            "native_identity": "windows-creation:134381205278019400",
            "executable_path": _ARTIFACT_VENV,
            "command_tail_sha256": _ARTIFACT_TAIL_SHA256,
            "command_tail_argument_count": 4,
            "registered_arguments_match": True,
        },)
    elif case == "genuine-second-interpreter":
        kwargs["extra"] = ({
            "pid": 8111, "parent_pid": 1880,
            "creation_date": "2026-11-02T19:15:27.802000+00:00",
            "native_identity": "windows-creation:134381205278019401",
            "executable_path": _ARTIFACT_BASE,
            "command_tail_sha256": _ARTIFACT_TAIL_SHA256,
            "command_tail_argument_count": 4,
            "registered_arguments_match": True,
        },)
    if case == "wrong-action-image":
        kwargs["action_image"] = r"D:\foreign\pythonw.exe"
    if case == "false-argument-match":
        kwargs["child_arguments_match"] = False
    owners = _artifact_shaped_windows_owners(**kwargs)
    if case == "wrong-task-path":
        owners.task_action["task_path"] = "\\Other\\"
    if case == "missing-task-action":
        owners.task_action = None
    if case == "missing-parent-identity":
        owners.details[0]["native_identity"] = None
    owners.probe_pid = 9999
    discovery = _artifact_shaped_discovery()
    if case == "missing-discovery-identity":
        discovery.pop("process_birth_id")
    monkeypatch.setattr(journey, "healthy", lambda _data: discovery)
    monkeypatch.setattr(journey, "runtime_processes", lambda _data: owners)
    monkeypatch.setattr(journey, "_windows_venv_base_executable",
                        lambda _path: None if case == "unreadable-pyvenv-cfg" else _ARTIFACT_BASE)

    with pytest.raises(RuntimeError, match=r"Expected exactly the selected Runtime owner 5224"):
        journey.assert_one_runtime(tmp_path, evidence=tmp_path / "evidence" / "release0")

    snapshot = journey.read_json(next((tmp_path / "evidence" / "release0").glob("runtime-owner-assertion-*.json")))
    assert snapshot["receipt_kind"] == "windows-runtime-owner-cardinality-failure"
    assert snapshot["assertion"]["asserted_owner_pids"] == list(owners)
    assert "logical_owner_pids" not in snapshot["assertion"]
    assert snapshot["normalization"]["status"] == "not-collapsed"
    assert snapshot["census_probe_pid"] == {"status": "observed", "value": 9999}
    assert snapshot["registered_task_action"] == (owners.task_action or {"status": "unavailable", "reason": "registered task action was unavailable"})


def test_tst_b1_windows_venv_cfg_requires_one_readable_absolute_home(monkeypatch):
    class Config:
        def __init__(self, text):
            self.text = text
        def is_symlink(self):
            return False
        def is_file(self):
            return True
        def read_text(self, **_kwargs):
            return self.text

    config = Config("home = D:\\base\\cpython-3.11.13\n"
                    "include-system-site-packages = false\n")
    monkeypatch.setattr(journey, "Path", lambda _path: config)
    assert journey._windows_venv_base_executable(_ARTIFACT_VENV) == r"D:\base\cpython-3.11.13\pythonw.exe"
    config.text = "home = D:\\one\nhome = D:\\two\n"
    assert journey._windows_venv_base_executable(_ARTIFACT_VENV) is None
    config.text = "home = relative\\python\n"
    assert journey._windows_venv_base_executable(_ARTIFACT_VENV) is None




def test_successful_redirector_then_independent_owner_keep_separate_staged_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    owners = _artifact_shaped_windows_owners()
    owners.probe_pid = 8888
    monkeypatch.setattr(journey, "healthy", lambda _data: _artifact_shaped_discovery())
    monkeypatch.setattr(journey, "runtime_processes", lambda _data: owners)
    monkeypatch.setattr(journey, "_windows_venv_base_executable", lambda _path: _ARTIFACT_BASE)
    root = tmp_path / "journey" / "evidence"
    evidence = root / "release0"
    journey.assert_one_runtime(tmp_path, evidence)
    owners.details[1]["parent_pid"] = 1880
    with pytest.raises(RuntimeError, match="Expected exactly the selected Runtime owner"):
        journey.assert_one_runtime(tmp_path, evidence)
    snapshots = list(evidence.glob("runtime-owner-assertion-*.json"))
    assert len(snapshots) == 2
    records = [journey.read_json(path) for path in snapshots]
    assert {record["receipt_kind"] for record in records} == {
        "windows-runtime-owner-logical-owner-assertion", "windows-runtime-owner-cardinality-failure"}
    failure = next(record for record in records if record["receipt_kind"].endswith("failure"))
    assert failure["registered_task_action"] == owners.task_action
    assert failure["normalization"]["status"] == "not-collapsed"
    assert failure["census_probe_pid"]["value"] == 8888
    journey.retain_named_evidence(root, "before-cleanup")
    staged = tmp_path / "evidence-upload" / "before-cleanup"
    files = list(staged.glob("runtime-service-service-owner-assertion-*.log"))
    assert len(files) == 2
    import re
    assert all(re.fullmatch(r"runtime-service-service-.+\.log", f.name) for f in files)
    assert {f.read_bytes() for f in files} == {f.read_bytes() for f in snapshots}
