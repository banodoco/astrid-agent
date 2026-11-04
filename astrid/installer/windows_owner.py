"""One conservative Windows venv redirector rule for installer and journey probes.

This module only observes/selects. Runtime discovery and its C1 fence remain
the authority to stop the selected child; the launcher is never signalled.
"""

from __future__ import annotations

import ntpath
import re
import stat
from pathlib import Path


PROCESS_QUERY = (
    "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
    "ExecutablePath,CommandLine,@{Name='CreationTime';Expression={"
    "if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString() }"
    "}} | ConvertTo-Json -Compress"
)


def runtime_command(command: str) -> bool:
    return ("-m runtime_protocol start" in command or
            (re.search(r"(?:^|\s)-c(?:\s|$)", command) is not None and
             "from astrid_runtime.cli import main" in command and
             re.search(r"main\(\s*\[\s*['\"]run['\"]", command) is not None))


def runtime_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if runtime_command(str(row.get("CommandLine") or ""))]


def _path(value: str) -> str:
    if not isinstance(value, str) or not (ntpath.isabs(value) or Path(value).is_absolute()):
        raise ValueError("Windows process executable is not absolute")
    return ntpath.normcase(ntpath.normpath(value))


def _tail(row: dict) -> str:
    # Only remove the executable token. Do not normalize/reparse Python's
    # prelude or its quoting: even a one-byte argument difference must refuse.
    match = re.fullmatch(r'\s*(?:"([^"\r\n]+)"|([^\s"]+))([ \t]+[\s\S]+)',
                         row["CommandLine"])
    if not match or _path(match[1] or match[2]) != _path(row["ExecutablePath"]):
        raise ValueError("Windows command executable differs from process image")
    return match[3]


def redirector_pair(rows: list[dict], venv: Path) -> tuple[dict, dict] | None:
    """Return (launcher, child) only for the exact approved two-process case.

    ``venv`` comes from a validated installed version, never from process argv.
    Missing, inaccessible or contradictory observations leave all matches live.
    """
    if len(rows) != 2:
        return None
    try:
        cfg = venv / "pyvenv.cfg"
        executable = venv / "Scripts" / "pythonw.exe"
        for path in {cfg, executable, *cfg.parents, *executable.parents}:
            facts = path.lstat()
            if stat.S_ISLNK(facts.st_mode) or getattr(facts, "st_file_attributes", 0) & 0x400:
                return None  # Includes Windows junctions/reparse points.
        if not cfg.is_file() or not executable.is_file():
            return None
        homes = [line.partition("=")[2].strip() for line in
                 cfg.read_text(encoding="utf-8-sig").splitlines()
                 if line.partition("=")[0].strip().casefold() == "home"]
        if len(homes) != 1:
            return None
        base = _path(ntpath.join(homes[0], "pythonw.exe"))
        launcher = _path(str(venv / "Scripts" / "pythonw.exe"))
        parents = [row for row in rows if _path(row["ExecutablePath"]) == launcher]
        children = [row for row in rows if _path(row["ExecutablePath"]) == base]
        if len(parents) != 1 or len(children) != 1 or launcher == base:
            return None
        parent, child = parents[0], children[0]
        if any(not isinstance(row.get("CreationTime"), str) or
               re.fullmatch(r"[0-9]+", row["CreationTime"]) is None for row in (parent, child)):
            return None
        if (type(parent["ProcessId"]) is not int or type(child["ProcessId"]) is not int
                or parent["ProcessId"] <= 0 or child["ProcessId"] <= 0
                or parent["ProcessId"] == child["ProcessId"]
                or type(child["ParentProcessId"]) is not int
                or child["ParentProcessId"] != parent["ProcessId"]
                or not 0 < int(parent["CreationTime"]) < int(child["CreationTime"])
                or _tail(parent) != _tail(child)):
            return None
        return parent, child
    except (OSError, UnicodeError, KeyError, TypeError, ValueError):
        return None


def owner_pids(rows: list[dict], venv: Path | None = None) -> list[int]:
    matches = runtime_rows(rows)
    pair = redirector_pair(matches, venv) if venv is not None else None
    return [pair[1]["ProcessId"]] if pair else [row["ProcessId"] for row in matches]


def pair_exited(rows: list[dict], pair: tuple[dict, dict]) -> bool:
    """Require positive absence of both captured incarnations, without signals.

    A new Runtime (including a scheduler restart) also prevents the handoff.
    Missing birth data for a reused PID cannot prove the original has exited.
    """
    if runtime_rows(rows):
        return False
    for original in pair:
        for row in rows:
            if row.get("ProcessId") == original["ProcessId"]:
                if (not row.get("CreationTime") or
                        row["CreationTime"] == original["CreationTime"]):
                    return False
    return True
