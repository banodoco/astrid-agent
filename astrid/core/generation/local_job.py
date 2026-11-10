"""``astrid generate <entry> --placement local --job FILE --out DIR --json`` (Plan 32 T4g).

The pod-side command of a rented machine: read the ``astrid.machine-job.v1``
file the controller wrote, resolve the entry's requirements manifest, and run
the one local run path with the job's ``align``. Progress goes to
``ASTRID_PROGRESS_PATH``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = "astrid.machine-job.v1"
ALIGN_MODES = ("align", "check", "trust")
#: Job inputs that name the shipped workflow of an entry-less (workflow-file) job.
_WORKFLOW_INPUTS = ("source", "python")


class JobFileError(ValueError):
    """The job file is unreadable or not an astrid.machine-job.v1 document."""


def read_job(path: str | Path) -> dict[str, Any]:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise JobFileError(f"job file could not be read: {path}: {exc}") from exc
    if not isinstance(body, dict) or body.get("schema") != SCHEMA:
        raise JobFileError(f"job file is not {SCHEMA}")
    if body.get("align") not in ALIGN_MODES:
        raise JobFileError(f"job align must be one of {', '.join(ALIGN_MODES)}")
    if not isinstance(body.get("inputs"), dict):
        raise JobFileError("job inputs must be an object")
    return body


def run_job(entry: str, job_path: str | Path, out: str | Path, *, mode: str | None = None,
            align: str | None = None) -> dict[str, Any]:
    """Run one job file locally; returns the run's manifest. ``align`` overrides the job's."""
    from astrid.core.generation import entries
    from astrid.core.generation.progress import write_progress
    from astrid.packs.vibecomfy.actions.run import run as run_action
    from astrid.packs.vibecomfy.local_job import run_named_entry, run_prepared_job

    write_progress("prepare")
    job = read_job(job_path)
    if align is not None and align not in ALIGN_MODES:
        raise JobFileError(f"align must be one of {', '.join(ALIGN_MODES)}")
    inputs = dict(job["inputs"])
    execution = job.get("execution")
    if execution is not None:
        if not isinstance(execution, dict):
            raise JobFileError("job execution must be an object")
        execution = dict(execution)
    task = job.get("task_identity") or "pod-job"
    attempt = job.get("attempt_identity") or "-"
    if job.get("entry") not in (None, entry) and entry != "workflow":
        raise JobFileError(f"job entry {job.get('entry')!r} differs from the requested {entry!r}")
    if execution is not None:
        bindings = job.get("bindings", json.loads(execution.get("workflow_inputs") or "{}"))
        if not isinstance(bindings, dict):
            raise JobFileError("job entry bindings must be an object")
        return run_prepared_job(entry, inputs, execution, bindings, Path(out), task=task, attempt=attempt,
                               mode=mode or job.get("mode"), align=align or job["align"])
    if entry == "workflow":
        shipped = next((inputs.pop(name) for name in _WORKFLOW_INPUTS if name in inputs), None)
        if shipped is None:
            raise JobFileError("a workflow-file job must ship its workflow")
        workflow, named = Path(str(shipped)), None
    else:
        described = entries.find_entry(entry)
        if described is None:
            raise JobFileError(f"entry {entry!r} is not available on this machine")
        workflow, named = entries.bundle_path(described), entry
    # File inputs ride the job directory; they reach the run as its own file arguments.
    files = {name: str(inputs.pop(name)) for name in ("managed_assets", "source_video") if name in inputs}
    if named is None:
        return run_action._run_and_settle(
            workflow, Path(out), task_identity=task, attempt_identity=attempt,
            workflow_inputs=json.dumps(inputs, sort_keys=True), entry=None,
            align=align or job["align"], **files)
    return run_named_entry(described, named, inputs, files, Path(out), task=task, attempt=attempt,
                           mode=mode or job.get("mode"), align=align or job["align"])
