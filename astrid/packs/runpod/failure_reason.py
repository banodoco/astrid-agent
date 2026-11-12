"""Closed vocabulary for controller-side refusal evidence."""
from dataclasses import asdict, dataclass

from .failure_diagnostics import CODES, NAME_RE

STAGES = frozenset({"no_machine", "boot", "install", "model_download", "workflow_validation",
                    "execution", "output_collection", "release"})
STEPS = frozenset({"stock_wait", "create", "provider_contract", "readiness", "bootstrap",
                   "engine_install", "verify", "job", "fetch", "reset", "release"})
GATES = frozenset({"identity", "state", "image", "storage", "hardware", "region", "rate",
                   "lifetime", "inventory", "capacity", "install", "offer", "provider"})
REASON_CODES = frozenset(CODES) | frozenset({"unclassified", "status_exit", "status_no_receipt",
    "status_deadline_exhausted", "status_not_installed", "receipt_read_failed", "mismatch",
    "provider_transport", "provider_malformed"})


@dataclass(frozen=True)
class Reason:
    stage: str
    step: str
    code: str
    gate: str | None = None
    exit_code: int | None = None

    def __post_init__(self):
        for name, allowed in (("stage", STAGES), ("step", STEPS), ("code", REASON_CODES)):
            value = getattr(self, name)
            if not isinstance(value, str) or not NAME_RE.fullmatch(value) or value not in allowed:
                raise ValueError(f"invalid reason {name}")
        if self.gate is not None and self.gate not in GATES:
            raise ValueError("invalid reason gate")
        if self.exit_code is not None and (type(self.exit_code) is not int or not -255 <= self.exit_code <= 255):
            raise ValueError("invalid reason exit_code")

    def as_dict(self):
        return {key: value for key, value in asdict(self).items() if value is not None}
