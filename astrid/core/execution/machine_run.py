"""Drive one generation job on a ``generation.machine`` contribution (Plan 32 T5).

Core owns the order and the money rule; the machine owns its provider:

    claim -> start -> poll ... (progress copied into the attempt) -> fetch -> settle/release

A cancelled local run calls the machine's ``cancel`` to stop the remote job.
Every path ends with ``release``; the machine verifies whether it can settle idle.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from astrid.core.contracts.errors import AstridError

POINT = "generation.machine"
#: Built-in placements that are not machine contributions.
BUILT_IN = frozenset({"local", "attached"})
#: The id ``entries.describe_workflow`` gives a workflow-file job: not an entry. The job's
#: ``entry`` is None and the pod runs the shipped bundle (``astrid generate workflow``).
WORKFLOW_FILE_ENTRY = "workflow"
#: Inputs that name local files; they ship inside the job directory.
FILE_INPUTS = frozenset({"python", "companion", "source", "managed_assets", "source_video", "entry_inputs"})


class MachineRunError(AstridError):
    """The remote job failed; the machine decides safe retention under its lease."""


class MachineRunCancelled(MachineRunError):
    """The local run was cancelled; the remote job was stopped."""


@dataclass(frozen=True)
class MachineJob:
    """One generation job, as the machine ships it."""

    entry: str | None  # None: run the shipped workflow bundle
    mode: str | None
    policy: Mapping[str, Any]
    inputs: Mapping[str, Any]
    invocation_id: str
    task_id: str
    attempt_id: str
    scratch: Path
    progress_path: Path
    align: str = "align"
    files: frozenset[str] = field(default=FILE_INPUTS)
    #: Optional ``PhaseTiming`` collector; the machine stamps its phases into it.
    timing: Any = None


def resolve_machine(kind: str, registry: Any | None = None) -> Any:
    """The ``generation.machine`` contribution for ``kind`` (its unconfigured entry)."""
    from astrid.core.extensions.registry import ExtensionRegistry

    if kind in BUILT_IN:
        raise MachineRunError(f"{kind} is a built-in placement, not a machine contribution")
    registry = registry or ExtensionRegistry().load()
    contribution = registry.resolve(POINT, kind)
    return registry.entry(contribution)


def run_on_machine(machine: Any, job: MachineJob, *, destination: Path,
                   cancelled: Callable[[], bool], poll_seconds: float = 15.0,
                   sleep: Callable[[float], None] = time.sleep,
                   on_claim: Callable[[Any], None] | None = None,
                   on_failure: Callable[[Mapping[str, Any]], None] | None = None) -> Mapping[str, Any]:
    """Claim, start, poll, fetch into ``destination``, then settle or release.

    ``on_claim(lease)`` records which machine runs the job as soon as it is claimed.
    ``on_failure(facts)`` runs once after release on any failure (the claim included)
    with ``failure``, ``stage``, ``lease`` and ``run`` (None if it never existed).
    It can never change or mask the job's own error.
    """
    # Existing machine kinds keep their original claim contract. A machine
    # with a capacity wait can opt into the same host cancellation callback.
    claim_parameters = inspect.signature(machine.claim).parameters
    run = None
    try:
        lease = (machine.claim(job, cancelled=cancelled) if "cancelled" in claim_parameters
                 else machine.claim(job))
    except BaseException as exc:
        _report_failure(on_failure, exc, "claim", None, None)
        raise
    success = False
    failure = None
    stage = "start"
    try:
        if on_claim is not None:
            on_claim(lease)
        if cancelled():
            raise MachineRunCancelled("generation cancelled before the remote job started")
        start_parameters = inspect.signature(machine.start).parameters
        run = (machine.start(lease, job, cancelled=cancelled) if "cancelled" in start_parameters
               else machine.start(lease, job))
        while True:
            stage = "poll"
            if cancelled():
                stage = "cancel"
                machine.cancel(run)
                raise MachineRunCancelled("generation cancelled; the remote job was stopped")
            status = machine.poll(run)
            if cancelled():
                stage = "cancel"
                machine.cancel(run)
                raise MachineRunCancelled("generation cancelled; the remote job was stopped")
            state = status.get("state")
            if state == "exited":
                break
            if state in {"cancelled", "missing"}:
                raise MachineRunError(f"remote job is {state}")
            sleep(poll_seconds)
        stage = "fetch"
        fetched = machine.fetch(run, destination)
        success = True
        return fetched
    except BaseException as exc:
        failure = exc
        raise
    finally:
        release_parameters = inspect.signature(machine.release).parameters
        try:
            if "failure" in release_parameters:
                details = {"failure": failure}
                if "stage" in release_parameters:
                    details["stage"] = stage
                machine.release(lease, success, **details)
            else:
                machine.release(lease, success)
        except BaseException as release_error:
            # A job that passed but could not be released is a release-stage failure.
            if failure is None:
                _report_failure(on_failure, release_error, "release", lease, run)
            else:
                _report_failure(on_failure, failure, stage, lease, run, release_failed=True)
            raise
        if failure is not None:
            _report_failure(on_failure, failure, stage, lease, run)


def _report_failure(on_failure: Callable[[Mapping[str, Any]], None] | None, failure: BaseException, stage: str,
                    lease: Any, run: Any, *, release_failed: bool = False) -> None:
    if on_failure is None:
        return
    try:
        on_failure({"failure": failure, "stage": stage, "lease": lease, "run": run,
                    "release_failed": release_failed})
    except Exception as exc:  # noqa: BLE001 - failure reporting must not mask the job error
        import sys
        print(f"failure receipt not written ({type(exc).__name__})", file=sys.stderr)


__all__ = ["WORKFLOW_FILE_ENTRY", "BUILT_IN", "FILE_INPUTS", "MachineJob", "MachineRunCancelled", "MachineRunError",
           "resolve_machine", "run_on_machine"]
