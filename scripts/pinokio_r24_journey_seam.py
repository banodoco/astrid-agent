"""Expose the current-tuple R-24 Linux seam to the Agent journey harness.

This is deliberately a *seam record*, not an M4-6 receipt.  It invokes the
already-reviewed R-24 live hook exactly once and retains the hook's immutable
request/export bindings.  R-24 observes the real Pinokio Stop preflight's
busy, unknown, and restoration paths on Linux.  It does not bind an M4-6
backend/fully-local mode, the other three M4-6 scenarios, a platform custody
session, or R-30/R-36 authority.  None of those facts may be inferred here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from . import ci_journey_r24 as r24
from . import ci_journey_provider as provider

SCHEMA = "astrid.r25.pinokio-r24-seam.v1"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_EXPORT_NAMES = frozenset(
    {
        "junit.xml",
        "manifest.json",
        "observation-result.json",
        "observation-busy.json",
        "observation-unknown.json",
        "observation-restoration.json",
    }
)


class PinokioR24SeamError(ValueError):
    """The raw R-24 seam cannot be retained without changing its meaning."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise PinokioR24SeamError(f"{label} must be an object")
    return dict(value)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _HEX64.fullmatch(value) is None:
        raise PinokioR24SeamError(f"{label} must be a SHA-256 digest")
    return value


def _safe_output(path: Path) -> Path:
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise PinokioR24SeamError("output must be an absent absolute non-symlink path")
    parent = path.parent
    if parent.is_symlink() or not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise PinokioR24SeamError("output parent must be an absolute resolved directory")
    return path


def _captured(receipt: object) -> dict[str, Any]:
    """Retain only the existing live-hook bindings needed by this bridge."""
    row = _mapping(receipt, "R-24 live receipt")
    if row.get("schema") != "astrid.r24.journey-provider-invocation.v1":
        raise PinokioR24SeamError("R-24 live receipt schema differs")
    if row.get("status") != "captured-untrusted":
        raise PinokioR24SeamError("R-24 live hook did not capture an untrusted live observation")
    if row.get("candidate") != r24.PUBLISHED_CANDIDATE:
        raise PinokioR24SeamError("R-24 live receipt candidate differs from the published tuple")
    if not isinstance(row.get("run_id"), str) or _HEX32.fullmatch(row["run_id"]) is None:
        raise PinokioR24SeamError("R-24 live receipt run ID is invalid")
    request_sha = _digest(row.get("request_sha256"), "R-24 request SHA-256")
    if not isinstance(row.get("session"), str) or _HEX32.fullmatch(row["session"]) is None:
        raise PinokioR24SeamError("R-24 session is invalid")
    bound_provider = _mapping(row.get("provider"), "R-24 provider")
    if (
        bound_provider.get("ref") != provider.LIVE_PRODUCER_REF
        or bound_provider.get("sources") != provider.LIVE_PRODUCER_FILES
        or bound_provider.get("interface") != provider.LIVE_INTERFACE_FILES
    ):
        raise PinokioR24SeamError("R-24 provider binding differs from the approved current tuple")
    invocation = _mapping(row.get("invocation"), "R-24 invocation")
    if invocation.get("status") != "completed" or invocation.get("exit") != 0:
        raise PinokioR24SeamError("R-24 provider invocation did not complete successfully")
    admission = _mapping(row.get("admission"), "R-24 admission")
    if admission != {
        "status": "awaiting-evidence",
        "authenticated_custody": False,
        "qualification": "not-evaluated",
    }:
        raise PinokioR24SeamError("R-24 admission boundary differs")
    export = _mapping(row.get("export"), "R-24 export")
    if export.get("path") != "provider-export" or not isinstance(export.get("files"), list):
        raise PinokioR24SeamError("R-24 export binding is incomplete")
    exports: list[dict[str, object]] = []
    names = set()
    for raw in export["files"]:
        item = _mapping(raw, "R-24 export file")
        name = item.get("path")
        size = item.get("bytes")
        if not isinstance(name, str) or name in names or name not in _EXPORT_NAMES:
            raise PinokioR24SeamError("R-24 export inventory differs")
        if type(size) is not int or size < 0:
            raise PinokioR24SeamError("R-24 export byte count is invalid")
        names.add(name)
        exports.append({"path": name, "bytes": size, "sha256": _digest(item.get("sha256"), "R-24 export SHA-256")})
    if names != _EXPORT_NAMES:
        raise PinokioR24SeamError("R-24 export inventory differs")
    return {
        "run_id": row["run_id"],
        "request_sha256": request_sha,
        "session": row["session"],
        "producer_ref": provider.LIVE_PRODUCER_REF,
        "producer_sources": dict(provider.LIVE_PRODUCER_FILES),
        "interface_sources": dict(provider.LIVE_INTERFACE_FILES),
        "export": sorted(exports, key=lambda item: str(item["path"])),
    }


def execute(*, run_root: Path, candidate_file: Path, config_file: Path,
            producer_root: Path, checkouts: Mapping[str, Path], output: Path) -> dict[str, object]:
    """Run the existing Linux provider hook and write its non-M4-6 seam record.

    ``execute_live_hook`` owns all live setup and R-24 export validation.  This
    wrapper intentionally adds no provider input, process control, custody
    assertion, or interpretation of those raw observations as a slot receipt.
    """
    output = _safe_output(output)
    if set(checkouts) != {"app", "agent", "runtime", "pinokio"}:
        raise PinokioR24SeamError("exactly four candidate checkouts are required")
    receipt = r24.execute_live_hook(
        run_root=run_root,
        candidate_file=candidate_file,
        config_file=config_file,
        producer_root=producer_root,
        checkouts=dict(checkouts),
    )
    captured = _captured(receipt)
    record: dict[str, object] = {
        "schema": SCHEMA,
        "candidate": dict(r24.PUBLISHED_CANDIDATE),
        "platform": "linux",
        "r24": captured,
        "scope": {
            "observations": ["busy", "unknown", "restoration"],
            "operation": "pinokio-stop-preflight",
            "m4_6_mode_binding": "absent",
        },
        "boundary": {
            "authenticated_custody": False,
            "qualification": "not-evaluated",
            "missing": [
                "mode-specific backend or fully-local M4-6 action binding",
                "M4-6 interruption, rollback, and keep-data-uninstall observations",
                "independent R-30 section 5 observation proofs and frozen R-36 bindings",
            ],
        },
    }
    encoded = _canonical(record) + b"\n"
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise PinokioR24SeamError("unable to create seam output") from exc
    return record
