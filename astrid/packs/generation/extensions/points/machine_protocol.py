"""Protocol of the generation.machine point (Plan 32 T5).

A machine runs the same local generation command a local run uses
(``astrid generate <entry> --placement local``) on hardware the controller
does not own. Core keeps paid consent and the lease; a machine only does:

claim(job)                  -> lease: rent or reuse a machine for this job
start(lease, job)           -> run: ship the job directory and start the command
poll(run)                   -> {"state": running|exited|cancelled, "progress": {...}|None}
cancel(run)                 -> stop the remote job (the caller then releases)
fetch(run, destination)     -> files copied back under a digest manifest
release(lease, warm)        -> settle warm (reusable) or release the machine

A contribution may also expose ``failure_recorder(job, machine_of, timing)``
returning a callback for failure facts after release. ``machine_of()`` reads the
host's current machine record; contributions without this hook need no recorder.

``local`` and ``attached`` are built in and are not contributions.
"""

from __future__ import annotations

from typing import Any, Protocol


class Machine(Protocol):
    def claim(self, job: Any) -> Any: ...

    def start(self, lease: Any, job: Any) -> Any: ...

    def poll(self, run: Any) -> Any: ...

    def cancel(self, run: Any) -> Any: ...

    def fetch(self, run: Any, destination: Any) -> Any: ...

    def release(self, lease: Any, warm: Any) -> Any: ...
