"""Provisional R-24 wire contract. Parsing is not evidence authentication.

No provider is approved here, and this module does not authorize execution or
hosted admission. A source-backed provider and independently authenticated
observation custody must be agreed before the driver can consume these records.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

REQUEST_SCHEMA = "astrid.r24.provider-request.v1"
RESULT_SCHEMA = "astrid.r24.provider-result.v1"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_RUN = re.compile(r"[0-9a-f]{32}\Z")


class ProviderContractError(ValueError):
    """Malformed, mismatched, incomplete, or unsafe provider message."""


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProviderContractError(f"{label} requires exactly {sorted(fields)}")
    return value


def _match(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProviderContractError(f"invalid {label}")
    return value


def _path(value: Any) -> str:
    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\\" in value or any(p in {"", ".", ".."} for p in value.split("/"))):
        raise ProviderContractError("invalid provider source path")
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _decode(raw: str | bytes) -> Any:
    if not isinstance(raw, (str, bytes)):
        raise ProviderContractError("message must be JSON bytes or text")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProviderContractError("invalid JSON constant")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProviderContractError(f"invalid JSON: {exc}") from exc


@dataclass(frozen=True)
class ProviderRequest:
    """Immutable canonical request, created from the independently selected inputs."""

    canonical: bytes
    sha256: str

    @classmethod
    def parse(cls, raw: str | bytes) -> "ProviderRequest":
        request = _object(_decode(raw), {"schema", "candidate", "provider", "stop", "config_sha256", "run_id"}, "request")
        if request["schema"] != REQUEST_SCHEMA:
            raise ProviderContractError("unsupported request schema")
        candidate = _object(request["candidate"], {"app", "agent", "runtime", "pinokio", "manifest_sha256"}, "candidate")
        for key in ("app", "agent", "runtime", "pinokio"):
            _match(candidate[key], _SHA, f"candidate.{key}")
        _match(candidate["manifest_sha256"], _DIGEST, "manifest digest")
        provider = _object(request["provider"], {"ref", "sources"}, "provider")
        _match(provider["ref"], _SHA, "provider ref")
        sources = provider["sources"]
        if not isinstance(sources, dict) or not sources:
            raise ProviderContractError("provider sources must be a non-empty object")
        for path, digest in sources.items():
            _path(path)
            _match(digest, _DIGEST, "provider source digest")
        stop = _object(request["stop"], {"ref", "path", "sha256"}, "stop")
        if stop["ref"] != candidate["pinokio"] or stop["path"] != "pinokio/stop.js":
            raise ProviderContractError("Stop identity differs from the candidate")
        _match(stop["sha256"], _DIGEST, "Stop digest")
        _match(request["config_sha256"], _DIGEST, "config digest")
        _match(request["run_id"], _RUN, "run ID")
        canonical = _canonical(request)
        return cls(canonical, hashlib.sha256(canonical).hexdigest())


@dataclass(frozen=True)
class Observation:
    kind: str
    source: str
    evidence_sha256: str


@dataclass(frozen=True)
class ProviderResult:
    """Structurally valid claims, not authenticated observations or a receipt."""

    request_sha256: str
    observations: tuple[Observation, ...]

    @classmethod
    def parse(cls, raw: str | bytes, request: ProviderRequest) -> "ProviderResult":
        if not isinstance(request, ProviderRequest):
            raise ProviderContractError("independently selected request required")
        result = _object(_decode(raw), {"schema", "request_sha256", "observations"}, "result")
        if result["schema"] != RESULT_SCHEMA or result["request_sha256"] != request.sha256:
            raise ProviderContractError("result schema or request binding differs")
        rows = result["observations"]
        if not isinstance(rows, list) or len(rows) != 3:
            raise ProviderContractError("exactly three observations are required")
        sources = json.loads(request.canonical)["provider"]["sources"]
        observations = []
        kinds = set()
        for raw_row in rows:
            row = _object(raw_row, {"kind", "source", "evidence_sha256", "safe"}, "observation")
            kind = row["kind"]
            if not isinstance(kind, str) or kind not in {"busy", "unknown", "restoration"} or kind in kinds:
                raise ProviderContractError("missing, duplicate, or unknown observation")
            kinds.add(kind)
            if not isinstance(row["source"], str) or row["source"] not in sources:
                raise ProviderContractError("observation source is not pinned")
            if row["safe"] is not True:
                raise ProviderContractError("unsafe observation")
            digest = _match(row["evidence_sha256"], _DIGEST, "evidence digest")
            observations.append(Observation(kind, row["source"], digest))
        if kinds != {"busy", "unknown", "restoration"}:
            raise ProviderContractError("incomplete observations")
        return cls(request.sha256, tuple(observations))
