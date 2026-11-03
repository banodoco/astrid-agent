from __future__ import annotations

import json
import struct
import wave
from pathlib import Path
from typing import Any, Mapping

from .sources import MAX_ROWS_ARTIFACT_BYTES, load_json_rows

MAX_TYPED_ROWS = 100_000


def ensure_tone_wav(path: Path, duration_sec: float) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 48000
    n_frames = int(sample_rate * float(duration_sec))
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack("<h", 0) * n_frames)


def resolve_mapping_path(
    mapping: str | Path, *, project: str | None = None, projects_root: Path | None = None
) -> Path:
    p = Path(mapping)
    base = Path(__file__).parent / "mappings"
    cand = base / f"{str(mapping)}.yaml"
    if cand.exists():
        return cand
    cand2 = base / str(mapping)
    if cand2.exists():
        return cand2
    if p.exists() and project:
        from astrid.pack_api import require_project_owned_artifact

        return require_project_owned_artifact(project, "timeline/mapping", p, root=projects_root)
    raise ValueError(
        f"unknown mapping {str(mapping)!r}; use a built-in mapping id or a project-owned file"
    )


def parse_json_rows(raw: str | Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    parsed: Any = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(parsed, list):
        return [dict(r) if isinstance(r, Mapping) else {"value": r} for r in parsed]
    if isinstance(parsed, dict) and "rows" in parsed and isinstance(parsed["rows"], list):
        return [dict(r) if isinstance(r, Mapping) else {"value": r} for r in parsed["rows"]]
    if isinstance(parsed, dict):
        return [dict(parsed)]
    return [dict(parsed)] if isinstance(parsed, Mapping) else []


def load_admitted_rows(
    *,
    source: str,
    json_path: str | Path | None,
    json_rows: str | Any | None,
    project: str | None,
    projects_root: Path | None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load bounded rows supplied by the admitting host.

    ``runaway`` identifies the row contract; it does not grant this pack
    access to Runaway's repository or to the kernel database.  The host must
    stage either a project-owned artifact or inline admitted rows.
    """

    normalized_source = source.strip().lower()
    if normalized_source not in {"json", "runaway"}:
        raise ValueError(f"unsupported typed row source: {source!r}")
    if (json_path is None) == (json_rows is None):
        raise ValueError("provide exactly one of json_path or json_rows")

    if json_path is not None:
        if not project:
            raise ValueError("json_path requires an owning project")
        from astrid.pack_api import require_project_owned_artifact

        admitted_path = require_project_owned_artifact(
            project,
            "timeline/typed_rows",
            json_path,
            root=projects_root,
        )
        rows = load_json_rows(admitted_path)
    else:
        try:
            encoded = json.dumps(
                json_rows,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("inline rows must be finite JSON data") from exc
        if len(encoded) > MAX_ROWS_ARTIFACT_BYTES:
            raise ValueError(f"inline rows exceed {MAX_ROWS_ARTIFACT_BYTES} bytes")
        rows = parse_json_rows(json_rows)

    if len(rows) > MAX_TYPED_ROWS:
        raise ValueError(f"typed row count exceeds {MAX_TYPED_ROWS}")
    if run_id is not None:
        rows = [row for row in rows if str(row.get("run_id", "")) == run_id]
    if not rows:
        suffix = f" for run_id={run_id!r}" if run_id is not None else ""
        raise ValueError(f"admitted typed row input is empty{suffix}")
    if normalized_source == "runaway":
        ordinals: list[int] = []
        for index, row in enumerate(rows):
            ordinal = row.get("ordinal")
            start_ms = row.get("start_ms")
            duration_ms = row.get("duration_ms")
            prompt = row.get("prompt")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise ValueError(f"runaway rows[{index}].ordinal must be a non-negative integer")
            if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
                raise ValueError(f"runaway rows[{index}].start_ms must be a non-negative integer")
            if (
                isinstance(duration_ms, bool)
                or not isinstance(duration_ms, int)
                or duration_ms <= 0
            ):
                raise ValueError(f"runaway rows[{index}].duration_ms must be a positive integer")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError(f"runaway rows[{index}].prompt must be a non-empty string")
            metadata = row.get("metadata", {})
            if not isinstance(metadata, Mapping):
                raise ValueError(f"runaway rows[{index}].metadata must be an object")
            ordinals.append(ordinal)
        if len(ordinals) != len(set(ordinals)):
            raise ValueError("runaway row ordinals must be unique within one admitted view")
    return rows


def confined_output_path(out_dir: Path, value: str | Path) -> Path:
    """Resolve an output path and reject traversal outside the staging root."""

    root = out_dir.expanduser().resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.expanduser().resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"output path escapes staging directory: {candidate}") from exc
    return candidate


def portable_input_ref(value: str | Path, *, projects_root: Path) -> str:
    """Describe an admitted input without embedding a machine absolute path."""

    candidate = Path(value).expanduser()
    if candidate.exists():
        resolved = candidate.resolve()
        try:
            return resolved.relative_to(projects_root.resolve()).as_posix()
        except ValueError:
            return f"external/{resolved.name}"
    return str(value)


__all__ = [
    "MAX_TYPED_ROWS",
    "confined_output_path",
    "ensure_tone_wav",
    "load_admitted_rows",
    "parse_json_rows",
    "portable_input_ref",
    "resolve_mapping_path",
]
