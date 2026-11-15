"""Provider-neutral machine observations."""

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


class FieldNotObserved(LookupError):
    """A consumer needed a fact this provider response shape does not carry."""


@dataclass(frozen=True)
class MachineRecord:
    provider: str
    machine_id: str
    state: Literal["starting", "running", "stopped", "gone"]
    name: str | None = None
    image: str | None = None
    storage_id: str | None = None
    storage_mount: str | None = None
    gpu_count: int | None = None
    gpu_label: str | None = None
    region: str | None = None
    hourly_usd: float | None = None
    # Fields this response shape actually carries. A field outside this set is
    # NOT observed, which is different from "observed and empty": a GraphQL pod
    # record that omits its attachment says nothing about storage.
    observed: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not isinstance(self.machine_id, str) or not self.machine_id.strip():
            raise ValueError("machine record requires a machine id")

    def require(self, name: str) -> Any:
        """Return an observed field (possibly None, meaning observed as empty); refuse an unobserved one."""
        if name not in self.observed:
            raise FieldNotObserved(name)
        return getattr(self, name)


class MachineProvider(Protocol):
    kind: str

    def read(self, machine_id: str) -> MachineRecord | None: ...

    def find(self, request_name: str) -> list[MachineRecord]: ...
