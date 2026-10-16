"""Shared product CLI output layer (m4 plan step 24, task T26).

One exact JSON envelope renderer for the stable SDK envelope plus concise
human output, stable exit codes, and no domain semantics. Every product
family handler (plan steps 25-29) renders through this module so JSON
output, human output, and process exit codes stay aligned with the frozen
SDK envelope (``docs/contracts/platform-contract.md``) and the explicit
product registry (``astrid/core/cli/domain_product.py``).

Stable exit codes (documented in product help, :func:`print_result`):

- ``EXIT_OK`` (0) — the command succeeded (envelope ``ok`` true).
- ``EXIT_FAILURE`` (1) — the command failed with a typed SDK error
  (envelope ``ok`` false; human mode prints the error to stderr).
- ``EXIT_USAGE`` (2) — command-line usage/parse errors (argparse's
  conventional exit code; argparse raises ``SystemExit(2)``).

The JSON renderer is the **only** JSON emitter for product envelopes: it
prints exactly the five envelope keys (``ok``/``data``/``error``/
``receipt``/``idempotency_key``) in canonical form, so a product command
under ``--json`` always emits one exact envelope object.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from astrid.core.receipts.canonical import canonical_json
from astrid.sdk.contracts import DomainResult, ErrorObject

__all__ = [
    "EXIT_FAILURE",
    "EXIT_OK",
    "EXIT_USAGE",
    "DomainResult",
    "ErrorObject",
    "envelope_dict",
    "exit_code",
    "print_result",
    "render_envelope_json",
    "render_human",
]

EXIT_OK = 0
"""Command succeeded (envelope ``ok`` true)."""

EXIT_FAILURE = 1
"""Command failed with a typed SDK error (envelope ``ok`` false)."""

EXIT_USAGE = 2
"""Command-line usage/parse error (argparse's conventional exit code)."""


def envelope_dict(result: object) -> dict[str, Any]:
    """Normalize *result* to the exact five-key SDK envelope mapping.

    Accepts a :class:`DomainResult` or a plain mapping with exactly the
    five frozen envelope keys; anything else (or a mapping with extra or
    missing keys) raises ``ValueError`` so a wire-shape drift can never be
    rendered silently.
    """
    if isinstance(result, DomainResult):
        return result.as_dict()
    if isinstance(result, Mapping):
        # Validate the shape through the contract types: exactly five
        # keys, a frozen error object on failures, a read-only receipt.
        return DomainResult.from_dict(dict(result)).as_dict()
    raise ValueError(
        "product output accepts only DomainResult or a plain envelope "
        f"mapping, got {type(result).__name__}"
    )


def render_envelope_json(result: object) -> str:
    """Render *result* as the exact canonical JSON SDK envelope.

    This is the one JSON renderer for product envelopes: the output is
    always a single JSON object with exactly the five frozen keys, in
    canonical (semantic) form, so ``--json`` consumers can parse it
    deterministically.
    """
    return canonical_json(envelope_dict(result))


def _identity_summary(data: object) -> str:
    """Render useful record summaries without exposing the SDK envelope."""
    if data is None:
        return "ok"
    if isinstance(data, list):
        # Cursor-paged list results use ``[rows, cursor]``. Keep every row
        # visible; callers already bound human task/run lists at the parser.
        rows = data[0] if len(data) == 2 and isinstance(data[0], list) else data
        if not rows:
            return "0 results"
        if all(isinstance(row, Mapping) for row in rows):
            lines = [f"{len(rows)} result(s)"]
            fields = ("id", "task_id", "run_id", "capability", "capability_id", "status", "state", "created_at", "started_at", "title", "name")
            for row in rows:
                label = next((str(row[key]) for key in ("task_id", "run_id", "id") if row.get(key)), "")
                details = [f"{key}={row[key]}" for key in fields if key in row and row[key] not in (None, "") and key not in {"id", "task_id", "run_id"}]
                lines.append("  " + "  ".join(part for part in (label, *details) if part))
            return "\n".join(lines)
        return f"{len(rows)} result(s)"
    if isinstance(data, dict):
        identity = None
        for key in (
            "slug",
            "media_id",
            "timeline_id",
            "task_id",
            "run_id",
            "reference_id",
            "shot_id",
            "id",
        ):
            value = data.get(key)
            if isinstance(value, str) and value:
                identity = f"{key}: {value}"
                break
        # Selection/current project responses wrap the identity under
        # ``project`` while retaining additional preference metadata.
        nested_project = data.get("project")
        if isinstance(nested_project, Mapping):
            identity = _identity_summary(nested_project)
        lines = []
        preferred = ("status", "state", "capability", "capability_id", "created_at", "started_at", "completed_at", "duration_seconds", "project_id", "timeline_id", "revision_id", "run_id", "task_id", "local_path", "handle", "mime_type", "size_bytes", "availability")
        for key in preferred:
            value = data.get(key)
            if value is not None and not isinstance(value, (dict, list)):
                lines.append(f"{key.replace('_', ' ')}: {value}")
        for key in ("progress", "summary", "counts"):
            value = data.get(key)
            if isinstance(value, Mapping):
                lines.extend(f"{key} {name}: {item}" for name, item in value.items() if not isinstance(item, (dict, list)))
        preview = data.get("preview")
        if isinstance(preview, Mapping) and isinstance(preview.get("text"), str):
            text = preview["text"].strip()
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                text = json.dumps(parsed, ensure_ascii=False, indent=2)
            if text:
                lines.append("report:")
                lines.extend("  " + line for line in text[:1800].splitlines()[:18])
                if len(text) > 1800 or len(text.splitlines()) > 18:
                    lines.append("  … (preview truncated)")
        for key in ("events", "evidence", "children", "outputs"):
            value = data.get(key)
            if isinstance(value, list) and value:
                lines.append(f"{key}: {len(value)}")
                for row in value[:8]:
                    if isinstance(row, Mapping):
                        summary = ", ".join(f"{name}={row[name]}" for name in ("event_type", "kind", "status", "state", "task_id", "handle", "output_port") if row.get(name) is not None)
                        if summary:
                            lines.append(f"  {summary}")
                if len(value) > 8:
                    lines.append(f"  … {len(value) - 8} more")
        return "\n".join(([identity] if identity else []) + lines) if identity or lines else "ok"
    return str(data)


def render_human(result: object, *, human_renderer: Callable[[object], str] | None = None) -> str:
    """Render *result* as a concise human-readable summary.

    Success lines name the object identity and summarize useful rows/details;
    failure lines render the frozen error object as ``error <code>:
    <message>``. Human output never leaks receipt internals beyond the
    receipt id, and never prints raw data blobs.
    """
    if human_renderer is not None:
        return human_renderer(result)
    envelope = envelope_dict(result)
    if envelope["ok"]:
        line = _identity_summary(envelope["data"])
        receipt = envelope.get("receipt")
        if isinstance(receipt, Mapping) and receipt.get("receipt_id"):
            line = f"{line} (receipt {receipt['receipt_id']})"
        return line
    error = envelope.get("error")
    if isinstance(error, Mapping):
        text = f"error {error.get('code', 'unknown')}: {error.get('message', '')}"
        details = error.get("details")
        next_action = details.get("next_action") if isinstance(details, Mapping) else None
        if isinstance(next_action, str) and next_action.strip():
            text += f"\nnext action: {next_action.strip()}"
        return text
    return "error"


def exit_code(result: object) -> int:
    """Return the stable product exit code for *result*."""
    return EXIT_OK if envelope_dict(result)["ok"] else EXIT_FAILURE


def print_result(
    result: object,
    *,
    as_json: bool = False,
    stream: TextIO | None = None,
    human_renderer: Callable[[object], str] | None = None,
) -> int:
    """Print *result* and return its stable exit code.

    In JSON mode the exact envelope is printed to stdout regardless of
    outcome (scripts parse the envelope; the exit code carries the
    failure). In human mode success prints to stdout and failures print
    the ``error <code>: <message>`` line to stderr, matching the
    conventional operator experience. An optional success-only human renderer
    receives the complete result envelope; JSON output and typed failure
    rendering remain unchanged.
    """
    out = stream if stream is not None else sys.stdout
    err = sys.stderr if stream is None else stream
    if as_json:
        print(render_envelope_json(result), file=out)
        return exit_code(result)
    envelope = envelope_dict(result)
    if envelope["ok"]:
        print(render_human(result, human_renderer=human_renderer), file=out)
        return EXIT_OK
    print(render_human(result), file=err)
    return EXIT_FAILURE
