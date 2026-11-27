"""Read-only public workflow requirements preview (P90 WS9).

The exact-plan producer is VibeComfy WS2. This draft establishes the Agent
boundary while its pinned callable/JSON contract is awaiting RS1. It must
not land until that bridge and the real producer fixtures are wired.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .preflight import GenerationReadinessError, readiness_envelope


@dataclass(frozen=True)
class PreviewSource:
    path: Path
    entry: dict[str, Any] | None
    approvals: list[dict[str, Any]]


def resolve_source(value: str) -> PreviewSource:
    """Resolve FILE or actual generation ENTRY without importing/publishing it."""
    from .entries import find_entry
    from .node_approvals import entry_approval_directory, read_entry_approvals

    path = Path(value).expanduser()
    if path.exists():
        return PreviewSource(path.resolve(), None, [])
    entry = find_entry(value)
    if not entry or not entry.get("bundle"):
        raise ValueError(f"{value!r} is neither a workflow path nor an entry with a workflow bundle")
    path = Path(entry["bundle"]).expanduser()
    if not path.exists():
        raise ValueError(f"the selected entry {value!r} has no readable workflow bundle")
    directory = entry_approval_directory(entry)
    approvals = read_entry_approvals(directory) if directory is not None else []
    return PreviewSource(path.resolve(), entry, approvals)


def read_grant_status(kind: str, subject: str, digest: str) -> dict[str, Any]:
    """Observe the current actor's protected P35 ledger over the existing GET.

    Identity and normal status shape mirror RuntimeService.grant_status. No
    local fallback and no generic app-state PUT. A failed read is not absence
    and cannot confer authority. WS2/RS1 must supply the actual grant subject
    and digest; this helper never derives them from a guessed repo name.
    """
    from astrid.core import user_state

    if kind not in ("pack", "model", "node"):
        raise ValueError("invalid grant kind")
    if not isinstance(subject, str) or not subject or len(subject) > 96:
        raise ValueError("invalid grant subject")
    if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise ValueError("invalid grant digest")
    encoded = json.dumps([kind, subject, digest], sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    grant_id = kind + "-" + hashlib.sha256(encoded).hexdigest()[:24]

    def result(state, generation=None):
        return {"grant_id": grant_id, "state": state,
                "authorized": state == "approved", "generation": generation}

    try:
        document = user_state.read("grants." + kind)
    except user_state.UserStateError as exc:
        return result("unavailable" if exc.code == "unavailable" else "unknown")
    except (OSError, KeyError, TypeError, ValueError):
        return result("unknown")
    content = document.content
    if content is None:
        return result("absent", 0)
    if (not isinstance(content, dict) or content.get("schema") != "astrid.grants.v1"
            or content.get("kind") != kind or not isinstance(content.get("grants"), dict)):
        return result("unknown")
    grants = content["grants"]
    if grant_id not in grants:
        return result("absent", 0)
    row = grants[grant_id]
    if (not isinstance(row, dict) or row.get("subject") != subject or row.get("digest") != digest
            or row.get("state") not in ("approved", "revoked")
            or type(row.get("generation")) is not int or row["generation"] < 1):
        return result("unknown")
    return result(row["state"], row["generation"])


def preview_envelope(plan, *, entry, target, issues=(), fixes=()):
    """Transport one complete producer plan, never an Agent-created digest.

    The caller maps the actual WS2/RS1 gap/action fields to readiness issues
    and P56 fixes. Do not derive readiness from a guessed status field.
    """
    from astrid.core.contracts.errors import redact_credential_data

    if (not isinstance(plan, dict) or not {"requirements", "gaps", "bytes", "digest"} <= plan.keys()
            or not isinstance(plan["requirements"], list) or not isinstance(plan["gaps"], list)):
        raise ValueError("requirements producer returned an invalid plan")
    # The digest must describe the displayed plan. Do not display a redacted
    # approximation as the exact approval object, and do not bypass the
    # common credential filter. RS1 checks the actual public JSON here.
    if redact_credential_data(plan) != plan:
        raise ValueError("requirements producer returned non-public plan data")
    if any(fix.get("who") not in ("agent", "human") for fix in fixes):
        raise ValueError("each requirements fix must name who acts")
    envelope = readiness_envelope(entry=entry, target=target, issues=issues,
                                  fixes=fixes, plan=plan)
    if envelope["status"] == "blocked":
        raise GenerationReadinessError(envelope)
    return envelope


def main(args, *, output=None) -> int:
    """Public preview dispatch; refuse on the accepted pre-WS2 pin.

    A draft bridge must never substitute a synthetic plan or advertise apply.
    Integrate the actual producer signature, target observation, P56 consent
    and typed actions here once WS2's immutable candidate and RS1 are supplied.
    """
    from .preflight import generation_error
    output = sys.stdout if output is None else output
    try:
        from vibecomfy.requirements import plan as canonical_plan
    except ImportError as exc:
        raise generation_error(
            ValueError("The canonical workflow requirements producer is not installed."),
            code="missing_dependency",
            details={"field": "requirements", "producer": "vibecomfy.requirements.plan"},
        ) from exc
    if not callable(canonical_plan):
        raise generation_error(ValueError("The installed requirements producer is invalid."),
                               code="missing_dependency")
    # Deliberate draft-only gate: the accepted pin has no such callable. A new
    # unreviewed package must not accidentally become the approval authority.
    raise generation_error(
        ValueError("The requirements producer contract has not been integrated and reviewed."),
        code="requirements_contract_pending",
        details={"field": "requirements", "producer": "vibecomfy.requirements.plan"},
    )
