"""Protocol of the generation.engine point (Plan 32 T2a).

requirements(entry, inputs) -> {engine, base, models[name, subdir, url, sha256, size], extra}
align(manifest, mode)       -> mode is "align" | "check" | "trust"
execute(job)                -> files written under job.out
cancel(job)
probe(endpoint)             -> optional
"""

from __future__ import annotations

from typing import Any, Protocol


class Engine(Protocol):
    __optional__ = ("probe",)

    def requirements(self, entry: Any, inputs: Any) -> dict[str, Any]: ...

    def align(self, manifest: Any, mode: str) -> Any: ...

    def execute(self, job: Any) -> Any: ...

    def cancel(self, job: Any) -> Any: ...

    def probe(self, endpoint: Any) -> Any: ...
