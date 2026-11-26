"""Copy legacy user state once, leaving the old location intact for rollback."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from astrid.core import user_state

RECEIPT_KEY = "state-imports"
MAX_RECEIPTS = 64


@dataclass(frozen=True)
class ImportSpec:
    import_id: str
    target_key: str
    source_label: str
    read_source: Callable[[], Any | None]
    to_document: Callable[[Any], dict]


@dataclass(frozen=True)
class ImportResult:
    status: str
    target_version: int
    source_digest: str | None
    note: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _existing_result(receipt: dict, digest: str) -> ImportResult:
    original = receipt["source_digest"]
    if digest != original:
        return ImportResult(
            "source_changed", receipt["target_version"], digest,
            f"Old copy was edited after the import (possibly by an older Astrid): "
            f"{original} -> {digest}",
        )
    return ImportResult("already_imported", receipt["target_version"], digest,
                        "Import already recorded; Runtime value left unchanged.")


def run_import(spec: ImportSpec, *, store: Any = None,
               now: Callable[[], datetime] = _utc_now) -> ImportResult:
    """Copy only into an absent target, then record the result in Runtime."""
    if store is None:
        try:
            store = user_state.current_store()
        except OSError:
            store = None
    if store is None:
        return ImportResult("unavailable", 0, None, "No Runtime store available.")

    receipts = user_state.read(RECEIPT_KEY, store=store).content or {}
    if spec.import_id in receipts:
        digest = user_state.content_digest(spec.read_source())
        return _existing_result(receipts[spec.import_id], digest)

    value = spec.read_source()
    if value is None:
        return ImportResult("nothing_to_import", 0, None, "No legacy value to import.")
    if len(receipts) >= MAX_RECEIPTS:
        raise user_state.UserStateError("too_large", RECEIPT_KEY)

    digest = user_state.content_digest(value)
    desired = spec.to_document(value)
    target = user_state.read(spec.target_key, store=store)
    if target.content is None:
        try:
            target = user_state.write(
                spec.target_key, desired, expected_version=0,
                idempotency_key=user_state.idempotency_key_for(spec.target_key, 0, desired),
                store=store,
            )
        except user_state.UserStateError as exc:
            if exc.code != "conflict":
                raise
            # A concurrent writer may have created the target. Runtime still wins.
            target = user_state.read(spec.target_key, store=store)
            if target.content is None:
                raise

    outcome = "imported" if target.content == desired else "kept"
    receipt = {
        "source": spec.source_label,
        "source_digest": digest,
        "target_key": spec.target_key,
        "outcome": outcome,
        "target_version": target.version,
        "at": now().astimezone(timezone.utc).isoformat(),
    }

    def change(doc: dict) -> None:
        if spec.import_id in doc:
            return
        if len(doc) >= MAX_RECEIPTS:
            raise user_state.UserStateError("too_large", RECEIPT_KEY)
        doc[spec.import_id] = receipt

    saved = user_state.update(RECEIPT_KEY, change, store=store).content
    assert saved is not None
    if saved[spec.import_id] != receipt:
        return _existing_result(saved[spec.import_id], digest)
    if outcome == "kept":
        return ImportResult("target_kept", target.version, digest,
                            "Existing Runtime value kept; old copy left intact.")
    return ImportResult("imported", target.version, digest,
                        "Import recorded; old copy left intact.")
