"""Delivery helpers for advisory render revision mismatches.

Keep the Runtime progress wire contract unchanged. Full revision identities
remain in the verified provenance; only the progress display is bounded.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

NOTICE_PREFIX = "Pack mismatch "
NOTICE_ACK_NAME = ".astrid-render-notice-ack.json"
MAX_PROGRESS_PHASE = 128


def _display(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    # Preserve a distinguishing suffix as well as a prefix; the full value
    # is always carried in the final result's warning.
    tail = min(8, limit // 3)
    return text[:limit - tail - 3] + "..." + text[-tail:]


def revision_notice_phase(row: Mapping[str, Any]) -> str:
    """Fit both revision names into Runtime's existing 128-character phase."""
    pack = _display(row["pack_id"], 24)
    editor = " ".join(str(row["editor_revision"]).split())
    render = " ".join(str(row.get("upstream_revision", row.get("derived_from"))).split())
    start = f"{NOTICE_PREFIX}{pack}: editor "
    separator = "; render "
    budget = MAX_PROGRESS_PHASE - len(start) - len(separator)
    # Do not abbreviate either revision if both fit (including two Git SHAs).
    if len(editor) + len(render) > budget:
        editor_limit = min(len(editor), max(budget // 2, budget - len(render)))
        render_limit = budget - editor_limit
        editor, render = _display(editor, editor_limit), _display(render, render_limit)
    return start + editor + separator + render


def revision_notice_text(row: Mapping[str, Any]) -> str:
    """Full advisory warning; no comparison or provenance policy here."""
    return (
        f"Pack {row['pack_id']} revision mismatch: editor {row['editor_revision']}; "
        f"render upstream {row.get('upstream_revision', row.get('derived_from'))} "
        f"(resolved {row['resolved_revision']})"
    )


def pending_notice_phase(progress: Any) -> str | None:
    """Recognize the initial notice checkpoint, not ordinary frame progress."""
    if not isinstance(progress, Mapping) or progress.get("percent") != 10:
        return None
    phase = progress.get("phase")
    if (isinstance(phase, str) and phase.startswith(NOTICE_PREFIX)
            and len(phase) <= MAX_PROGRESS_PHASE):
        return phase
    return None


def verified_revision_warnings(
    outputs: Sequence[Mapping[str, Any]], attempt_root: Path,
) -> list[str]:
    """Project warnings from the exact already-harvested provenance bytes.

    Never use a request-provided pathname or re-read mutable render inputs.
    Recheck only the small auxiliary provenance output, not the rendered video.
    Missing or malformed optional provenance remains advisory.
    """
    warnings: list[str] = []
    for output in outputs:
        if output.get("name") != "provenance" or output.get("role") != "auxiliary":
            continue
        try:
            path = Path(output["path"])
            if (path.is_symlink() or not path.is_file()
                    or not path.resolve(strict=True).is_relative_to(attempt_root.resolve())):
                continue
            data = path.read_bytes()
            if (len(data) != output.get("size")
                    or "sha256:" + hashlib.sha256(data).hexdigest() != output.get("digest")):
                continue
            provenance = json.loads(data)
            fragments = provenance.get("backend_fragments")
            fragment = fragments.get("rendering.remotion") if isinstance(fragments, Mapping) else None
            rows = fragment.get("pack_revisions") if isinstance(fragment, Mapping) else None
            if not isinstance(rows, list):
                continue
            for row in rows:
                if (isinstance(row, Mapping) and row.get("comparison") == "mismatch"
                        and all(key in row for key in ("pack_id", "editor_revision", "resolved_revision"))):
                    warning = revision_notice_text(row)
                    if warning not in warnings:
                        warnings.append(warning)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            continue
    return warnings
