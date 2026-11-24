"""One description of incomplete authored model authority.

Inspection keeps the missing-fact row, and strict provisioning raises an
EngineError with that very same row and the explicit repair. No incomplete
declaration enters the downloader or target inventory.
"""
from __future__ import annotations

from typing import Any

from .engine_contracts import ModelLockValidationError


def incomplete_model_repair(record: dict[str, Any], entry_id: str | None = None) -> dict[str, Any]:
    """The human repair and the safe, exact re-inspection step."""
    name, subdir = record["name"], record["subdir"]
    destination = (f"{subdir}/{name}" if isinstance(subdir, str) and subdir not in ("", ".")
                   and name else name)
    label = destination or "the unnamed model declaration"
    # The declaration name may itself contain angle brackets; the contract's
    # placeholder syntax must not mistake authored filenames for variables.
    label = str(label).replace("<", "\\u003c").replace(">", "\\u003e")
    return {
        "id": "complete-model-declaration", "who": "human", "scope": "read_only",
        "instruction": {
            "step": f"Obtain the verified HTTPS source, SHA-256 and exact byte size for {label}; "
                    "complete the saved workflow's model declaration before requesting installation. "
                    "Then inspect the selected entry again.",
            "then_argv": ["astrid", "generate", "align", "--entry", entry_id or "<ENTRY>", "--json"],
        },
        "placeholders": {} if entry_id else {"ENTRY": "The saved entry whose model declaration needs repair"},
    }


class IncompleteModelDeclaration(ModelLockValidationError):
    """Strict refusal preserves the same typed facts seen by inspection."""

    code = "model_lock_incomplete"

    def __init__(self, record: dict[str, Any], *, entry_id: str | None = None):
        super().__init__(
            f"Declared model {record['name'] or '?'} is unavailable: missing {', '.join(record['missing'])}"
        )
        self.details = dict(record)
        self.setup_fix = incomplete_model_repair(record, entry_id)
