"""The fixed Release-1 installed-lifecycle receipt contract.

This is deliberately a small data-only module so the driver, evidence checker and
workflow tests share the same nine required jobs.  A mode name is an ownership
statement, not permission for a direct installer journey to fall back to an
unmanaged foreground process.
"""

from __future__ import annotations

from typing import Final

RELEASE1_MATRIX: Final = (
    {"job_id": "direct-macos", "mode": "direct", "runner": "macos-14", "shell": "sh", "os": "macos", "leg": "release0-to-release1"},
    {"job_id": "direct-linux", "mode": "direct", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux", "leg": "release0-to-release1"},
    {"job_id": "direct-windows", "mode": "direct", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows", "leg": "release0-to-release1"},
    {"job_id": "pinokio-backend-macos", "mode": "pinokio-backend", "runner": "macos-14", "shell": "sh", "os": "macos"},
    {"job_id": "pinokio-backend-linux", "mode": "pinokio-backend", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
    {"job_id": "pinokio-backend-windows", "mode": "pinokio-backend", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
    {"job_id": "pinokio-fully-local-macos", "mode": "pinokio-fully-local", "runner": "macos-14", "shell": "sh", "os": "macos"},
    {"job_id": "pinokio-fully-local-linux", "mode": "pinokio-fully-local", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
    {"job_id": "pinokio-fully-local-windows", "mode": "pinokio-fully-local", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
)

RELEASE1_JOB_IDS: Final = tuple(row["job_id"] for row in RELEASE1_MATRIX)
RELEASE1_MODES: Final = tuple(sorted({row["mode"] for row in RELEASE1_MATRIX}))


def release1_job(job_id: str) -> dict:
    """Return a copy so callers cannot mutate the checked-in matrix."""
    for row in RELEASE1_MATRIX:
        if row["job_id"] == job_id:
            return dict(row)
    raise ValueError(f"Unknown Release-1 journey job: {job_id}")


def validate_job(job_id: str, mode: str) -> dict:
    row = release1_job(job_id)
    if row["mode"] != mode:
        raise ValueError(f"Release-1 job {job_id} requires mode {row['mode']}, not {mode}")
    return row
