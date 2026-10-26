"""Shared argparse value types used by the product CLI and MCP bridge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from astrid.sdk.execution_request import ExecutionRequestError, normalize_execution_request


def json_object(value: str) -> dict[str, Any]:
    """Parse one JSON object argument."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON object: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def json_array(value: str) -> list[Any]:
    """Parse one JSON array argument."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid JSON array: {exc.msg}") from exc
    if not isinstance(parsed, list):
        raise argparse.ArgumentTypeError("must be a JSON array")
    return parsed


def execution_request(value: str) -> dict[str, Any]:
    """Parse and normalize one target-aware execution request."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid execution request: {exc.msg}") from exc
    try:
        normalized = normalize_execution_request(parsed)
    except ExecutionRequestError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if normalized is None:
        raise argparse.ArgumentTypeError("execution request must be an object")
    return normalized


def input_file(value: str) -> Path:
    """Convert a file argument the command reads to a path."""
    return Path(value)


def output_file(value: str) -> Path:
    """Convert a file argument the command writes to a path."""
    return Path(value)


def json_object_or_path(value: str) -> Any:
    """Timeline CLI input: inline JSON or a path; MCP accepts inline JSON only."""
    text = str(value)
    if text.lstrip().startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise argparse.ArgumentTypeError(f"invalid JSON: {exc.msg}") from exc
    return text
