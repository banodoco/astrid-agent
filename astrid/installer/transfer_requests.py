"""Non-authoritative request replay bookkeeping for a lifecycle transfer.

The caller must serialize access and durably persist the returned ledger in the
existing transaction before any side effect. This module does not authenticate
requests, establish a native fence, or make an unfinished request safe to retry.
It is not a wire format and is not connected to the installer transaction yet.
"""

from dataclasses import dataclass, field, replace
from hashlib import sha256


class RequestRefused(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RequestEnvelope:
    operation_id: str
    generation: str
    request_id: str
    expected_phase: str
    binding_digest: str

    def __post_init__(self):
        if any(type(value) is not str or not value for value in (
            self.operation_id, self.generation, self.request_id,
            self.expected_phase, self.binding_digest,
        )):
            raise ValueError("invalid lifecycle request envelope")


def _fingerprint(envelope: RequestEnvelope, request_bytes: bytes) -> str:
    if type(envelope) is not RequestEnvelope or type(request_bytes) is not bytes:
        raise ValueError("invalid lifecycle request")
    # Length framing is internal to this hash, not a shared transport encoding.
    values = (envelope.operation_id.encode(), envelope.generation.encode(),
              envelope.request_id.encode(), envelope.expected_phase.encode(),
              envelope.binding_digest.encode(), request_bytes)
    digest = sha256()
    for value in values:
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


@dataclass(frozen=True)
class _Record:
    request_id: str
    fingerprint: str
    result: bytes | None = field(default=None, repr=False)

    def __post_init__(self):
        if (type(self.request_id) is not str or not self.request_id
                or type(self.fingerprint) is not str or len(self.fingerprint) != 64
                or any(char not in "0123456789abcdef" for char in self.fingerprint)
                or (self.result is not None and type(self.result) is not bytes)):
            raise ValueError("invalid lifecycle request record")


@dataclass(frozen=True)
class RequestLedger:
    """Pure bookkeeping; a pending record is an uncertain outcome on recovery."""

    operation_id: str
    generation: str
    binding_digest: str
    records: tuple[_Record, ...] = ()

    def __post_init__(self):
        if any(type(value) is not str or not value for value in (
            self.operation_id, self.generation, self.binding_digest,
        )):
            raise ValueError("invalid lifecycle request binding")
        if (type(self.records) is not tuple
                or any(type(record) is not _Record for record in self.records)
                or len({record.request_id for record in self.records}) != len(self.records)):
            raise ValueError("invalid lifecycle request records")

    def begin(self, envelope: RequestEnvelope, request_bytes: bytes, *, phase: str):
        """Return (ledger, result); result is None only for a new intent.

        Persist the returned ledger before acting. An identical completed request
        returns its recorded bytes even if the current phase has since advanced.
        A pending request requires reconciliation, never a second execution.
        """
        fingerprint = _fingerprint(envelope, request_bytes)
        if (envelope.operation_id != self.operation_id
                or envelope.generation != self.generation
                or envelope.binding_digest != self.binding_digest):
            raise RequestRefused("binding_changed")
        for record in self.records:
            if record.request_id == envelope.request_id:
                if record.fingerprint != fingerprint:
                    raise RequestRefused("binding_changed")
                if record.result is None:
                    raise RequestRefused("recovery_required")
                return self, record.result
        if any(record.result is None for record in self.records):
            raise RequestRefused("recovery_required")
        if type(phase) is not str or envelope.expected_phase != phase:
            raise RequestRefused("recovery_required")
        return replace(self, records=self.records + (_Record(envelope.request_id, fingerprint),)), None

    def finish(self, envelope: RequestEnvelope, request_bytes: bytes, result: bytes):
        """Attach a result to a previously durable intent; persist before reply."""
        if type(result) is not bytes:
            raise ValueError("lifecycle result must be bytes")
        fingerprint = _fingerprint(envelope, request_bytes)
        if (envelope.operation_id != self.operation_id
                or envelope.generation != self.generation
                or envelope.binding_digest != self.binding_digest):
            raise RequestRefused("binding_changed")
        for index, record in enumerate(self.records):
            if record.request_id == envelope.request_id:
                if record.fingerprint != fingerprint:
                    raise RequestRefused("binding_changed")
                if record.result is not None:
                    if record.result != result:
                        raise RequestRefused("binding_changed")
                    return self
                records = list(self.records)
                records[index] = replace(record, result=result)
                return replace(self, records=tuple(records))
        raise RequestRefused("recovery_required")
