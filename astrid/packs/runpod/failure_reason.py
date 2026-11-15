"""Closed vocabulary for controller-side refusal evidence."""
import re
from dataclasses import asdict, dataclass
from typing import TypeVar

from .failure_diagnostics import CODES, NAME_RE

STAGES = frozenset({"no_machine", "boot", "install", "model_download", "workflow_validation",
                    "execution", "output_collection", "release"})
STEPS = frozenset({"stock_wait", "create", "provider_contract", "readiness", "bootstrap",
                   "engine_install", "verify", "job", "fetch", "reset", "release"})
GATES = frozenset({"identity", "state", "image", "storage", "hardware", "region", "rate",
                   "lifetime", "inventory", "capacity", "install", "offer", "provider"})
# K1's installation checkpoint refusals, in the same validated vocabulary.
CHECKPOINT_CODES = frozenset({
    "installation_receipt_changed", "checkpoint_attestation_missing",
    "checkpoint_attestation_mismatch", "checkpoint_installation_changed", "checkpoint_job_changed",
    "checkpoint_epoch_changed", "checkpoint_invocation_changed", "checkpoint_paid_expired",
    "checkpoint_custody_changed", "checkpoint_observation_interrupted", "checkpoint_observation_failed",
    "checkpoint_observer_unavailable", "checkpoint_observer_unreaped",
})
# Status-verification outcomes that name themselves; any other status detail is
# rendered in the message and classified as status_not_installed.
_VERIFY_CODES = CHECKPOINT_CODES | frozenset({"status_no_receipt", "status_deadline_exhausted",
                                              "receipt_read_failed"})
REASON_CODES = CHECKPOINT_CODES | frozenset(CODES) | frozenset({"unclassified", "status_exit", "status_no_receipt",
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


def installation_reason(detail: str) -> Reason:
    """The one validated reason for an install verification or checkpoint refusal.

    ``detail`` is the fixed label the refusing check recorded; the free-form
    status diagnostics it may carry stay in the message, never in the code.
    """
    match = re.fullmatch(r"status_exit_(-?\d+)", detail)
    if match and -255 <= int(match.group(1)) <= 255:
        return Reason(stage="install", step="verify", gate="install", code="status_exit",
                      exit_code=int(match.group(1)))
    code = detail if detail in _VERIFY_CODES else "status_not_installed"
    return Reason(stage="install", step="verify", gate="install", code=code)


_ReasonError = TypeVar("_ReasonError", bound=BaseException)


def attach_reason(error: _ReasonError, reason: Reason) -> _ReasonError:
    """Attach a validated reason; the single boundary every refusal crosses."""
    if not isinstance(reason, Reason):
        raise ValueError("a validated reason is required")
    setattr(error, "reason", reason)
    return error
