#!/usr/bin/env python3
"""Bind Pinokio's installed mode file to one typed lifecycle receipt row."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any


SCHEMA = "astrid.pinokio.install-mode-binding.v1"
SLOT_MODES = {
    "pinokio-backend": "backend",
    "pinokio-fully-local": "local",
}
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_CANDIDATE_FIELDS = {"app", "agent", "runtime", "pinokio", "manifest_sha256"}


class InstallModeReceiptError(RuntimeError):
    """The installed mode cannot be bound to the declared lifecycle row."""


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise InstallModeReceiptError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_regular(path: Path) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise InstallModeReceiptError("absolute nonsymlink install-mode path required")
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024:
            raise InstallModeReceiptError("installed mode is missing or not a bounded regular file")
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise InstallModeReceiptError("installed mode is missing or not a bounded regular file") from exc


def read_receipt(path: Path, *, slot_mode: str) -> dict[str, object]:
    expected = SLOT_MODES.get(slot_mode)
    if expected is None:
        raise InstallModeReceiptError("unknown Pinokio slot mode")
    raw = _read_regular(path)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                InstallModeReceiptError("invalid JSON constant")
            ),
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise InstallModeReceiptError("installed mode is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"schema_version", "mode"}:
        raise InstallModeReceiptError("installed mode fields differ")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise InstallModeReceiptError("installed mode schema differs")
    if value.get("mode") not in {"backend", "local"}:
        raise InstallModeReceiptError("installed mode value differs")
    if value["mode"] != expected:
        raise InstallModeReceiptError(
            f"declared {slot_mode} slot conflicts with installed mode {value['mode']!r}"
        )
    return {"schema_version": 1, "mode": value["mode"]}


def build_binding(
    path: Path,
    *,
    slot_id: str,
    slot_mode: str,
    candidate: dict[str, str],
    session: str,
) -> dict[str, Any]:
    expected_prefix = slot_mode + "-"
    if not isinstance(slot_id, str) or not slot_id.startswith(expected_prefix):
        raise InstallModeReceiptError("slot id and mode differ")
    if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_FIELDS:
        raise InstallModeReceiptError("candidate fields differ")
    if any(not isinstance(candidate[name], str) or not _HEX40.fullmatch(candidate[name])
           for name in ("app", "agent", "runtime", "pinokio")):
        raise InstallModeReceiptError("candidate commit identity differs")
    if (not isinstance(candidate["manifest_sha256"], str)
            or not _HEX64.fullmatch(candidate["manifest_sha256"])):
        raise InstallModeReceiptError("candidate manifest identity differs")
    if not isinstance(session, str) or not _HEX32.fullmatch(session):
        raise InstallModeReceiptError("provider session identity differs")
    receipt = read_receipt(path, slot_mode=slot_mode)
    raw = _read_regular(path)
    return {
        "schema": SCHEMA,
        "slot_id": slot_id,
        "slot_mode": slot_mode,
        "candidate": dict(candidate),
        "provider_session": session,
        "pinokio_install_mode_receipt": receipt,
        "source": {
            "path": "app/install-mode.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    }


def project_journey_result(row: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(binding, dict):
        raise InstallModeReceiptError("journey row or mode binding is malformed")
    if binding.get("schema") != SCHEMA:
        raise InstallModeReceiptError("mode binding schema differs")
    if row.get("job_id") != binding.get("slot_id") or row.get("mode") != binding.get("slot_mode"):
        raise InstallModeReceiptError("journey slot differs from mode binding")
    if row.get("provider_session") != binding.get("provider_session"):
        raise InstallModeReceiptError("journey session differs from mode binding")
    observed = row.get("candidate_checkouts")
    candidate = binding.get("candidate")
    if not isinstance(observed, dict) or not isinstance(candidate, dict) or any(
        (observed.get(name) or {}).get("sha") != candidate.get(name)
        for name in ("app", "agent", "runtime", "pinokio")
    ):
        raise InstallModeReceiptError("journey candidate differs from mode binding")
    if "pinokio_install_mode_receipt" in row:
        raise InstallModeReceiptError("journey row already has an installed-mode receipt")
    projected = dict(row)
    projected["pinokio_install_mode_receipt"] = dict(
        binding["pinokio_install_mode_receipt"]
    )
    return projected
