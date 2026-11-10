"""Drive one generation job on a ``generation.machine`` contribution (Plan 32 T5).

Core owns the order and the money rule; the machine owns its provider:

    claim -> start -> poll ... (progress copied into the attempt) -> fetch -> settle/release

A cancelled local run calls the machine's ``cancel`` to stop the remote job.
Every path ends with ``release``; the machine verifies whether it can settle idle.
"""

from __future__ import annotations

import time
import inspect
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
                   on_claim: Callable[[Any], None] | None = None) -> Mapping[str, Any]:
    """Claim, start, poll, fetch into ``destination``, then settle or release.

    ``on_claim(lease)`` records which machine runs the job as soon as it is claimed.
    """
    # Existing machine kinds keep their original claim contract. A machine
    # with a capacity wait can opt into the same host cancellation callback.
    claim_parameters = inspect.signature(machine.claim).parameters
    lease = (machine.claim(job, cancelled=cancelled) if "cancelled" in claim_parameters
             else machine.claim(job))
    success = False
    failure = None
    stage = "start"
    try:
        if on_claim is not None:
            on_claim(lease)
        if cancelled():
            raise MachineRunCancelled("generation cancelled before the remote job started")
        run = machine.start(lease, job)
        while True:
            stage = "poll"
            if cancelled():
                stage = "cancel"
                machine.cancel(run)
                raise MachineRunCancelled("generation cancelled; the remote job was stopped")
            status = machine.poll(run)
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
        if "failure" in release_parameters:
            details = {"failure": failure}
            if "stage" in release_parameters:
                details["stage"] = stage
            machine.release(lease, success, **details)
        else:
            machine.release(lease, success)


__all__ = ["WORKFLOW_FILE_ENTRY", "BUILT_IN", "FILE_INPUTS", "MachineJob", "MachineRunCancelled", "MachineRunError",
           "resolve_machine", "run_on_machine"]
