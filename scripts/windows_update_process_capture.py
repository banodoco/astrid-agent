"""Read-only, bounded observations around the direct Windows update bootstrap."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess

NAME = "windows-update-process-capture.json"
LIMIT = 4096
DOOR_PORT = 17340  # The journey's Runtime door, not a pack-host listener.
_SCRIPT = r'''$ErrorActionPreference = 'Stop'
try {
  $rows = @(Get-CimInstance Win32_Process | Select-Object -First 4097 | ForEach-Object {
    [pscustomobject]@{pid=[uint32]$_.ProcessId; ppid=[uint32]$_.ParentProcessId;
      created=if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { '' }}
  })
  try {
    $owners = @(Get-NetTCPConnection -LocalPort 17340 -State Listen -ErrorAction Stop |
      Select-Object -First 65 | ForEach-Object { [uint32]$_.OwningProcess })
    $portStatus = 'observed'
  } catch {
    if ($_.FullyQualifiedErrorId -like 'NoMatchingMSFT_NetTCPConnection*' -or
        $_.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*') { $owners = @(); $portStatus = 'observed' }
    else { $owners = @(); $portStatus = 'unavailable' }
  }
  ConvertTo-Json -InputObject @{processes=$rows; owners=$owners; port_status=$portStatus} -Depth 4 -Compress
} catch { exit 1 }
'''


_TREE_SCRIPT = r'''$ErrorActionPreference = 'Stop'
try {
  $rows = @(Get-CimInstance Win32_Process | Select-Object -First 1025 | ForEach-Object {
    [pscustomobject]@{pid=[uint32]$_.ProcessId; ppid=[uint32]$_.ParentProcessId; name=[string]$_.Name; cmd=[string]$_.CommandLine}
  })
  ConvertTo-Json -InputObject @{processes=$rows} -Depth 4 -Compress
} catch { exit 1 }
'''
TREE_LIMIT = 1024
COMMAND_LIMIT = 240


def tree_snapshot():
    """The process tree, with each process's name and a bounded command line; diagnostics only, never a guard."""
    result = {"status": "unavailable", "observed_at_utc": datetime.now(timezone.utc).isoformat(), "processes": []}
    try:
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _TREE_SCRIPT],
                                   capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode:
            return result
        rows = json.loads(completed.stdout).get("processes")
        if not isinstance(rows, list):
            return result
        result["status"] = "observed"
        result["process_limit_reached"] = len(rows) > TREE_LIMIT
        for row in rows[:TREE_LIMIT]:
            if not isinstance(row, dict) or not _pid(row.get("pid")) or type(row.get("ppid")) is not int:
                continue
            result["processes"].append({"pid": row["pid"], "ppid": row["ppid"],
                                        "name": str(row.get("name", ""))[:64],
                                        "command": str(row.get("cmd") or "")[:COMMAND_LIMIT]})
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, RecursionError):
        pass
    return result


def _pid(value):
    return type(value) is int and 0 < value <= 0xffffffff


def observe():
    """Do not retain PowerShell errors or any fields other than numeric IDs and birth times."""
    result = {"status": "unavailable", "observed_at_utc": datetime.now(timezone.utc).isoformat(),
              "processes": [], "process_limit_reached": False, "invalid_process_rows": 0,
              "door_status": "unavailable", "door_owners": [], "door_limit_reached": False}
    try:
        completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
                                   capture_output=True, text=True, timeout=15, check=False)
        if completed.returncode:
            return result
        value = json.loads(completed.stdout)
        rows, owners = value["processes"], value["owners"]
        if not isinstance(rows, list) or not isinstance(owners, list):
            return result
        result["status"] = "observed"
        result["process_limit_reached"] = len(rows) > LIMIT
        for row in rows[:LIMIT]:
            if not isinstance(row, dict) or not _pid(row.get("pid")) or type(row.get("ppid")) is not int or not 0 <= row["ppid"] <= 0xffffffff:
                result["invalid_process_rows"] += 1
                continue
            created = row.get("created")
            if not isinstance(created, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z", created):
                result["invalid_process_rows"] += 1
                continue
            result["processes"].append({"pid": row["pid"], "ppid": row["ppid"], "created": created})
        if value.get("port_status") == "observed":
            result["door_status"] = "observed"
            result["door_limit_reached"] = len(owners) > 64
            result["door_owners"] = sorted(set(pid for pid in owners[:64] if _pid(pid)))
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, KeyError, RecursionError):
        pass
    return result


def host_pid(data):
    try:
        # A marker is only a hint for the census, never authority over a process.
        with (data / "runtime" / "generic-host.ready.json").open("rb") as stream:
            value = json.loads(stream.read(8193))
        pid = value.get("pid")
        return pid if _pid(pid) else None
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        return None


def descendants(rows, roots):
    """Follow observed parent links only when the parent predates the child."""
    by_pid = {row["pid"]: row for row in rows}
    selected = {pid for pid in roots if pid in by_pid}
    while True:
        added = {row["pid"] for row in rows if row["ppid"] in selected
                 and by_pid[row["ppid"]]["created"] <= row["created"]} - selected
        if not added:
            return [by_pid[pid] for pid in sorted(selected)]
        selected.update(added)


def capture_failure(evidence, pid, before, after):
    baseline = descendants(before["processes"], [pid] if pid else [])
    live = {(row["pid"], row["created"]) for row in after["processes"]}
    surviving = [row for row in baseline if (row["pid"], row["created"]) in live]
    after_tree = descendants(after["processes"], [row["pid"] for row in surviving])
    record = {"schema_version": 1, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
              "root_marker_pid": pid, "before": {k: v for k, v in before.items() if k != "processes"},
              "after": {k: v for k, v in after.items() if k != "processes"},
              "baseline_tree": baseline, "surviving_baseline": surviving, "failure_tree": after_tree,
              "door_port": DOOR_PORT,
              "limitation": "Sequential snapshots after bootstrap failure and recovery; ancestry and listener ownership are racy, incomplete, and not custody proof. The Runtime door is not a dedicated pack-host port."}
    (evidence / NAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
