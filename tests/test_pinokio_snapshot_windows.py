from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from scripts import pinokio_snapshot_windows as collector


TASK = r"\Astrid Runtime"
SERVICE = "AstridRuntime"


def _payload() -> dict[str, object]:
    return {
        "processes": [
            {
                "pid": 412,
                "parent_pid": 88,
                "command_line": r'"C:\Program Files\Astrid\runtime.exe" serve',
                "creation_time_utc": "2026-11-02T18:14:13.1234567Z",
                "executable_path": r"C:\Program Files\Astrid\runtime.exe",
            },
            {
                "pid": 88,
                "parent_pid": 4,
                "command_line": None,
                "creation_time_utc": "2026-11-02T18:14:12Z",
                "executable_path": None,
            },
        ],
        "scheduled_tasks": [
            {
                "target": TASK,
                "status": "present",
                "state": "running",
                "enabled": True,
                "actions": [
                    {
                        "type": 0,
                        "execute": r"C:\Program Files\Astrid\runtime.exe",
                        "arguments": "",
                        "working_directory": None,
                    }
                ],
            }
        ],
        "services": [
            {
                "target": SERVICE,
                "status": "present",
                "state": "Running",
                "start_type": "Auto",
                "process_id": 412,
            }
        ],
    }


def _run(payload: object, *, returncode: int = 0):
    encoded = payload if isinstance(payload, str) else json.dumps(payload)

    def runner(argv, **kwargs):
        assert argv[0] == collector.POWERSHELL
        assert argv[1:7] == [
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
        ]
        assert "Get-CimInstance -ClassName Win32_Process" in argv[7]
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        assert json.loads(kwargs["input"])["scheduled_task_paths"] == [TASK]
        return subprocess.CompletedProcess(argv, returncode, stdout=encoded, stderr="failure")

    return runner


def _collect(payload: object | None = None, **kwargs):
    return collector.collect_windows_snapshot(
        scheduled_task_paths=[TASK],
        service_names=[SERVICE],
        run=_run(_payload() if payload is None else payload),
        **kwargs,
    )


def test_present_observation_uses_fixed_powershell_and_has_canonical_digest() -> None:
    result = _collect()

    assert result["schema"] == "astrid.r25.snapshot.windows.v1"
    assert result["source"] == {
        "profile": "windows-cim-task-service-snapshot-v1",
        "platform": "windows",
        "runner": "windows-2022",
        "shell": "powershell.exe",
        "executable": collector.POWERSHELL,
        "process_interface": "Get-CimInstance Win32_Process",
        "task_interface": "Task Scheduler COM",
        "service_interface": "Get-CimInstance Win32_Service",
        "continuity": "bounded-snapshot",
    }
    assert [row["pid"] for row in result["processes"]] == [88, 412]
    assert result["scheduled_tasks"][0]["status"] == "present"
    assert result["services"][0]["start_type"] == "Auto"
    without_digest = dict(result)
    digest = without_digest.pop("structural_sha256")
    canonical = json.dumps(
        without_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    assert digest == hashlib.sha256(canonical).hexdigest()
    json.dumps(result, allow_nan=False)


def test_absent_and_collection_error_targets_remain_distinct_observations() -> None:
    payload = _payload()
    payload["scheduled_tasks"] = [{"target": TASK, "status": "absent"}]
    payload["services"] = [
        {
            "target": SERVICE,
            "status": "collection-error",
            "error": {
                "type": "Microsoft.Management.Infrastructure.CimException",
                "hresult": -2146233087,
                "fully_qualified_error_id": "HRESULT 0x80041003",
                "category": "PermissionDenied",
            },
        }
    ]

    result = _collect(payload)

    assert result["scheduled_tasks"] == [{"target": TASK, "status": "absent"}]
    assert result["services"][0]["status"] == "collection-error"
    assert result["services"][0]["error"]["category"] == "PermissionDenied"


def test_duplicate_process_pid_is_refused() -> None:
    payload = _payload()
    payload["processes"][1]["pid"] = payload["processes"][0]["pid"]
    with pytest.raises(collector.WindowsSnapshotError, match="duplicate PID"):
        _collect(payload)


def test_duplicate_output_target_is_refused() -> None:
    payload = _payload()
    payload["scheduled_tasks"] = [payload["scheduled_tasks"][0], payload["scheduled_tasks"][0]]
    with pytest.raises(collector.WindowsSnapshotError, match="do not match requested targets"):
        _collect(payload)


@pytest.mark.parametrize(
    "targets, message",
    [
        ({"scheduled_task_paths": [TASK, TASK.lower()]}, "duplicate scheduled-task target"),
        ({"service_names": [SERVICE, SERVICE.lower()]}, "duplicate service target"),
        ({"scheduled_task_paths": [r"relative\task"]}, "unsafe scheduled-task target"),
        ({"service_names": [r"Astrid\Runtime"]}, "unsafe service target"),
    ],
)
def test_duplicate_or_unsafe_caller_targets_are_refused_before_execution(targets, message) -> None:
    called = False

    def runner(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("must not execute")

    arguments = {"scheduled_task_paths": [TASK], "service_names": [SERVICE], **targets}
    with pytest.raises(collector.WindowsSnapshotError, match=message):
        collector.collect_windows_snapshot(run=runner, **arguments)
    assert called is False


@pytest.mark.parametrize("malformed", ["not-json", '{"processes":[],"processes":[]}'])
def test_malformed_or_duplicate_key_cim_json_is_refused(malformed: str) -> None:
    with pytest.raises(collector.WindowsSnapshotError, match="malformed JSON|duplicate JSON key"):
        _collect(malformed)


def test_missing_creation_time_is_refused() -> None:
    payload = _payload()
    payload["processes"][0]["creation_time_utc"] = None
    with pytest.raises(collector.WindowsSnapshotError, match="creation_time_utc"):
        _collect(payload)


def test_process_and_output_capacity_are_refused() -> None:
    with pytest.raises(collector.WindowsSnapshotError, match="process capacity"):
        _collect(max_processes=1)
    with pytest.raises(collector.WindowsSnapshotError, match="output capacity"):
        _collect(max_output_bytes=10)


def test_command_error_is_refused_without_interpreting_stdout() -> None:
    with pytest.raises(collector.WindowsSnapshotError, match="failed with exit 5"):
        collector.collect_windows_snapshot(
            scheduled_task_paths=[TASK],
            service_names=[SERVICE],
            run=_run(_payload(), returncode=5),
        )
