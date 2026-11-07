"""Caption validation before manifest export."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator


from .artifacts import HASHES_KEY
from ...shared.items import utc_now_iso

CAPTION_SIDECAR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["text", "schema_version", "confidence", "model", HASHES_KEY],
    "properties": {
        "text": {"type": "string"},
        "schema_version": {"type": "integer"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "model": {"type": "string"},
        "raw_response": {"type": "object"},
        HASHES_KEY: {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {"type": "string"},
        },
    },
    "additionalProperties": True,
}


@dataclass(frozen=True)
class CaptionValidationResult:
    items: list[dict[str, Any]]
    failures: list[dict[str, str]]


def validate_accepted_captions(
    items: list[Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    now: str | None = None,
) -> CaptionValidationResult:
    """Validate accepted caption sidecars and text, returning annotated items on failure."""

    caption_config = config.get("caption") if isinstance(config.get("caption"), Mapping) else {}
    schema_path = caption_config.get("schema_path") if isinstance(caption_config, Mapping) else None
    schema = _load_caption_schema(schema_path)
    validation_rules = _validation_rules(caption_config)
    validated_at = now or utc_now_iso()
    output: list[dict[str, Any]] = []
    state_failures: list[dict[str, str]] = []

    for item in items:
        updated = copy.deepcopy(dict(item))
        if updated.get("review_status") != "accepted":
            output.append(updated)
            continue
        item_id = str(updated.get("item_id") or "")
        item_failures: list[dict[str, str]] = []
        caption = updated.get("caption") if isinstance(updated.get("caption"), Mapping) else {}
        caption_text = str(caption.get("text") or "") if isinstance(caption, Mapping) else ""
        sidecar = _load_sidecar(updated)

        if sidecar is None:
            item_failures.append(
                {
                    "code": "caption_sidecar_missing",
                    "message": "accepted caption sidecar is missing or unreadable",
                    "path": "caption_file",
                }
            )
        else:
            item_failures.extend(_validate_sidecar(sidecar))
            if schema is not None:
                raw_response = _raw_response(caption, sidecar)
                item_failures.extend(_validate_raw_response(raw_response, schema))

        item_failures.extend(_validate_text(caption_text, validation_rules))
        if item_failures:
            updated["caption_validation"] = {
                "valid": False,
                "failures": item_failures,
                "validated_at": validated_at,
            }
            if isinstance(schema_path, str) and schema_path:
                updated["caption_validation"]["schema_path"] = schema_path
            for failure in item_failures:
                state_failure = {
                    "item_id": item_id,
                    "code": failure["code"],
                    "message": failure["message"],
                }
                if failure.get("path"):
                    state_failure["path"] = failure["path"]
                state_failures.append(state_failure)
        output.append(updated)

    return CaptionValidationResult(items=output, failures=state_failures)


def _load_caption_schema(schema_path: Any) -> Mapping[str, Any] | None:
    if not isinstance(schema_path, str) or not schema_path:
        return None
    path = Path(schema_path).expanduser()
    if not path.is_absolute():
        path = (PACK_ROOT / path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError(f"caption schema must be a JSON object: {schema_path}")
    return raw


def _validation_rules(caption_config: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = caption_config.get("validation") if isinstance(caption_config.get("validation"), Mapping) else {}
    return rules if isinstance(rules, Mapping) else {}


def _load_sidecar(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = item.get("caption_file")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (PACK_ROOT / path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, Mapping) else None


def _validate_sidecar(sidecar: Mapping[str, Any]) -> list[dict[str, str]]:
    validator = Draft7Validator(CAPTION_SIDECAR_SCHEMA)
    failures: list[dict[str, str]] = []
    for error in sorted(validator.iter_errors(sidecar), key=lambda err: list(err.path)):
        failures.append(
            {
                "code": "caption_sidecar_schema_error",
                "message": error.message,
                "path": _error_path(error, fallback="caption_file"),
            }
        )
    return failures


def _raw_response(caption: Mapping[str, Any], sidecar: Mapping[str, Any]) -> Any:
    if isinstance(caption.get("raw_response"), Mapping):
        return caption["raw_response"]
    if isinstance(sidecar.get("raw_response"), Mapping):
        return sidecar["raw_response"]
    return sidecar


def _validate_raw_response(raw_response: Any, schema: Mapping[str, Any]) -> list[dict[str, str]]:
    validator = Draft7Validator(schema)
    failures: list[dict[str, str]] = []
    for error in sorted(validator.iter_errors(raw_response), key=lambda err: list(err.path)):
        failures.append(
            {
                "code": "raw_response_schema_error",
                "message": error.message,
                "path": _error_path(error, fallback="caption.raw_response"),
            }
        )
    return failures


def _validate_text(text: str, rules: Mapping[str, Any]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    if not text:
        failures.append({"code": "caption_text_empty", "message": "accepted caption text is empty", "path": "caption.text"})
        return failures

    min_length = rules.get("min_length")
    if isinstance(min_length, int) and len(text) < min_length:
        failures.append(
            {
                "code": "caption_text_too_short",
                "message": f"caption text is shorter than {min_length} characters",
                "path": "caption.text",
            }
        )
    max_length = rules.get("max_length")
    if isinstance(max_length, int) and len(text) > max_length:
        failures.append(
            {
                "code": "caption_text_too_long",
                "message": f"caption text is longer than {max_length} characters",
                "path": "caption.text",
            }
        )
    prefix = rules.get("required_prefix")
    if isinstance(prefix, str) and prefix and not text.startswith(prefix):
        failures.append(
            {
                "code": "caption_text_prefix_mismatch",
                "message": f"caption text must start with {prefix!r}",
                "path": "caption.text",
            }
        )
    pattern = rules.get("text_pattern")
    if isinstance(pattern, str) and pattern and re.search(pattern, text) is None:
        failures.append(
            {
                "code": "caption_text_pattern_mismatch",
                "message": f"caption text does not match pattern {pattern!r}",
                "path": "caption.text",
            }
        )
    required_substrings = rules.get("required_substrings")
    if isinstance(required_substrings, list):
        for substring in required_substrings:
            if isinstance(substring, str) and substring and substring not in text:
                failures.append(
                    {
                        "code": "caption_text_missing_substring",
                        "message": f"caption text must include {substring!r}",
                        "path": "caption.text",
                    }
                )
    return failures


def _json_path(path: Any) -> str:
    parts = [str(part) for part in path]
    return ".".join(parts)


def _error_path(error: Any, *, fallback: str) -> str:
    if error.validator == "required" and error.message.startswith("'"):
        return error.message.split("'", 2)[1]
    return _json_path(error.path) or fallback
