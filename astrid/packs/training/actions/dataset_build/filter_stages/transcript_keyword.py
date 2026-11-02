"""Transcript keyword filter backed by editorial.transcribe."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import hashlib
import json
import mimetypes
import re
import stat
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from astrid import sdk

from ..artifacts import (
    load_valid_cached_sidecar,
    sidecar_hashes,
    unlink_stale_sidecar,
    write_hashed_sidecar,
)
from ....shared.interfaces import FilterResult
from ....shared.items import deterministic_id
from ._common import (
    build_filter_stats,
    increment_reason,
    pass_item,
    reject_item,
    resolve_media_path,
)


TRANSCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "text": {"type": "string"},
                },
            },
        }
    },
}


class TranscriptKeywordFilter:
    def __init__(self, *, repo_root: Path = PACK_ROOT, **_: Any) -> None:
        self._repo_root = repo_root

    @property
    def stage_id(self) -> str:
        return "transcript_keyword_filter"

    @property
    def stage_order(self) -> int:
        return 2

    def apply(self, items: list[dict[str, Any]], state: dict[str, Any], config: dict[str, Any]) -> FilterResult:
        started = time.perf_counter()
        allowlist = _keywords(config.get("allowlist"))
        denylist = _keywords(config.get("denylist"))
        strict_empty = _strict_empty(config)
        passed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        warnings: list[str] = []

        for item in items:
            try:
                transcript = self._transcript(item, config)
                text = _transcript_text(transcript)
            except Exception as exc:  # noqa: BLE001 - lenient mode turns transcript failures into pass-through warnings
                if strict_empty:
                    reason = "transcript_unavailable"
                    rejected.append(reject_item(item, self.stage_id, reason=reason, extra={"error": type(exc).__name__}))
                    increment_reason(reasons, reason)
                else:
                    warning = f"transcript_unavailable:{item.get('item_id') or item.get('source_id') or 'unknown'}:{type(exc).__name__}"
                    warnings.append(warning)
                    passed.append(pass_item(item, self.stage_id, reason="transcript_unavailable", extra={"warning": warning}))
                continue

            matched_deny = _first_match(text, denylist, case_sensitive=bool(config.get("case_sensitive", False)))
            if matched_deny is not None:
                reason = "transcript_denylist_match"
                rejected.append(reject_item(item, self.stage_id, reason=reason, extra={"keyword": matched_deny}))
                increment_reason(reasons, reason)
                continue

            if not text.strip():
                if strict_empty:
                    reason = "transcript_empty"
                    rejected.append(reject_item(item, self.stage_id, reason=reason))
                    increment_reason(reasons, reason)
                else:
                    passed.append(pass_item(item, self.stage_id, reason="transcript_empty"))
                continue

            matched_allow = _first_match(text, allowlist, case_sensitive=bool(config.get("case_sensitive", False)))
            if allowlist and matched_allow is None:
                reason = "transcript_allowlist_miss"
                rejected.append(reject_item(item, self.stage_id, reason=reason))
                increment_reason(reasons, reason)
                continue

            extra = {"keyword": matched_allow} if matched_allow is not None else None
            passed.append(pass_item(item, self.stage_id, reason="", extra=extra))

        stats = build_filter_stats(
            stage_id=self.stage_id,
            stage_order=self.stage_order,
            items_in=len(items),
            items_passed=len(passed),
            items_rejected=len(rejected),
            rejection_reasons=reasons,
            warnings=warnings,
            started=started,
        )
        return FilterResult(passed=passed, rejected=rejected, stats=stats)

    def _transcript(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        sidecar = transcript_sidecar_path(item, config, repo_root=self._repo_root)
        if _fixture_mode(config):
            fixture = _fixture_transcript_path(item, config, repo_root=self._repo_root)
            raw = json.loads(fixture.read_text(encoding="utf-8")) if fixture is not None and fixture.is_file() else {"segments": []}
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return _normalize_transcript(raw)

        sidecar.parent.mkdir(parents=True, exist_ok=True)
        hashes = sidecar_hashes(
            prompt=_prompt(item, config),
            schema=TRANSCRIPT_SCHEMA,
            media=item,
            config=_cache_relevant_config(config),
        )
        cached = load_valid_cached_sidecar(sidecar, hashes)
        if cached is not None:
            return _normalize_transcript(cached)
        unlink_stale_sidecar(sidecar)
        meter = config.get("child_work_meter")
        attempt_root = config.get("attempt_output_root")
        if meter is None or not isinstance(attempt_root, (str, Path)):
            raise RuntimeError("editorial.transcribe requires the run-scoped child meter and trusted attempt output root")
        inputs, local_object = _transcribe_inputs(
            item, config, meter=meter, attempt_root=Path(attempt_root), repo_root=self._repo_root
        )
        child_key = _transcript_child_key(item, local_object["digest"])
        _increment_budget(config)
        meter.admit(child_key, "editorial.transcribe", inputs)
        child = sdk.invoke(
            "editorial.transcribe",
            kind="action",
            inputs=inputs,
            child_key=child_key,
            wait=True,
            timeout_seconds=600.0,
            poll_seconds=0.1,
        )
        raw = _materialize_transcript(child, attempt_root=Path(attempt_root), meter=meter)
        transcript = _normalize_transcript(raw)
        write_hashed_sidecar(sidecar, transcript, hashes)
        return transcript


def transcript_sidecar_path(item: Mapping[str, Any], config: Mapping[str, Any], *, repo_root: Path = PACK_ROOT) -> Path:
    out_dir = config.get("out_dir")
    if out_dir is None:
        out_dir = resolve_media_path(item, repo_root=repo_root, required=True).parent / "transcripts"
    path = Path(str(out_dir)).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve() / f"{_clip_id(item)}.transcript.json"


def _transcribe_inputs(
    item: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    meter: Any,
    attempt_root: Path,
    repo_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    media = resolve_media_path(item, repo_root=repo_root, required=True, must_exist=True)
    root = attempt_root.expanduser().resolve(strict=True)
    confined = media.resolve(strict=True)
    try:
        filename = confined.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("selected transcript clip is outside the parent attempt output root") from exc
    info = confined.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("selected transcript clip must be a regular file")
    media_type = mimetypes.guess_type(confined.name)[0]
    if not media_type:
        raise ValueError(f"selected transcript clip has no recognized media type: {confined.suffix}")
    producer = {"filename": filename, "media_type": media_type, "output_port": "audio"}

    inputs: dict[str, Any] = {"audio": producer}
    option_defaults = {
        "model": "whisper-1",
        "language": "en",
        "max_chunk_sec": 600.0,
        "no_vad_gate": False,
    }
    for name, default in option_defaults.items():
        value = config.get(name)
        if value not in (None, "") and value != default:
            inputs[name] = value
    env_file = config.get("env_file")
    if env_file not in (None, ""):
        env_path = Path(str(env_file)).expanduser()
        inputs["env_file"] = str(env_path if env_path.is_absolute() else (repo_root / env_path).resolve())
    local_object = meter.register_local_file(confined, producer, name="audio")
    return inputs, local_object


def _transcript_child_key(item: Mapping[str, Any], digest: str) -> str:
    identity = json.dumps([_clip_id(item), digest], separators=(",", ":"))
    return "transcribe_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _materialize_transcript(child: Any, *, attempt_root: Path, meter: Any) -> Any:
    if (
        getattr(child, "ok", False) is not True
        or not isinstance(getattr(child, "raw_result", None), Mapping)
        or child.raw_result.get("state") != "completed"
        or not all(
            isinstance(value, str) and value
            for value in (getattr(child, "kernel_task_id", None), getattr(child, "kernel_attempt_id", None), getattr(child, "kernel_run_id", None))
        )
    ):
        raise RuntimeError(f"editorial.transcribe child did not complete: {getattr(child, 'error', None)}")
    outputs = getattr(child, "outputs", None)
    rows = outputs.get("managed_outputs") if isinstance(outputs, Mapping) else None
    matches = [row for row in rows or [] if isinstance(row, Mapping) and row.get("output_port") == "transcript"]
    if len(matches) != 1:
        raise ValueError("editorial.transcribe must return exactly one transcript output")
    row = dict(matches[0])
    if (row.get("task_id"), row.get("attempt_id"), row.get("run_id")) != (
        child.kernel_task_id,
        child.kernel_attempt_id,
        child.kernel_run_id,
    ):
        raise ValueError("editorial.transcribe output task/attempt identity changed")
    size, digest = row.get("size"), row.get("digest")
    if (
        type(size) is not int
        or size < 0
        or not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or row.get("object_id") != digest
        or not isinstance(row.get("association_id"), str)
        or not row["association_id"]
    ):
        raise ValueError("editorial.transcribe output has an invalid size or digest")
    meter.retain(row)
    local = child.materialize_output(row.get("association_id"))
    if dict(local.output) != row:
        raise ValueError("editorial.transcribe materialized output descriptor changed")
    relative = Path(local.filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("editorial.transcribe output filename escapes attempt output root")
    root = attempt_root.expanduser().resolve(strict=True)
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("editorial.transcribe output is not a regular file under attempt output root")
    payload = path.read_bytes()
    actual = "sha256:" + hashlib.sha256(payload).hexdigest()
    if len(payload) != size or actual != digest:
        raise ValueError("editorial.transcribe output size or digest changed")
    return json.loads(payload.decode("utf-8"))


def _normalize_transcript(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        segments = raw.get("segments", [])
        return {"segments": [dict(segment) for segment in segments if isinstance(segment, Mapping)]}
    if isinstance(raw, list):
        return {"segments": [dict(segment) for segment in raw if isinstance(segment, Mapping)]}
    return {"segments": []}


def _transcript_text(transcript: Mapping[str, Any]) -> str:
    segments = transcript.get("segments")
    if not isinstance(segments, Sequence) or isinstance(segments, (str, bytes)):
        return ""
    return "\n".join(str(segment.get("text", "")).strip() for segment in segments if isinstance(segment, Mapping)).strip()


def _first_match(text: str, keywords: list[str], *, case_sensitive: bool) -> str | None:
    haystack = text if case_sensitive else text.casefold()
    for keyword in keywords:
        needle = keyword if case_sensitive else keyword.casefold()
        if needle and needle in haystack:
            return keyword
    return None


def _keywords(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value if str(item)]


def _strict_empty(config: Mapping[str, Any]) -> bool:
    if "strict_empty_transcript" in config:
        return bool(config["strict_empty_transcript"])
    return str(config.get("empty_transcript", "lenient")) == "strict"


def _fixture_mode(config: Mapping[str, Any]) -> bool:
    return bool(config.get("fixture_mode") or config.get("mode") == "fixture")


def _fixture_transcript_path(item: Mapping[str, Any], config: Mapping[str, Any], *, repo_root: Path) -> Path | None:
    transcript_file = item.get("transcript_file") or config.get("transcript_file")
    if isinstance(transcript_file, str):
        path = Path(transcript_file).expanduser()
        return path if path.is_absolute() else (repo_root / path).resolve()
    fixture_dir = config.get("fixture_transcript_dir") or config.get("fixture_dir")
    if isinstance(fixture_dir, str):
        path = Path(fixture_dir).expanduser()
        root = path if path.is_absolute() else (repo_root / path).resolve()
        return root / f"{_clip_id(item)}.transcript.json"
    return None


def _increment_budget(config: Mapping[str, Any]) -> None:
    tracker = config.get("budget_tracker")
    if tracker is not None and hasattr(tracker, "increment"):
        tracker.increment("filter.transcript.editorial.transcribe")


def _prompt(item: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    return "|".join(
        [
            "editorial.transcribe",
            str(item.get("item_id") or item.get("source_id") or ""),
            str(item.get("clip_start_s", "")),
            str(item.get("clip_end_s", "")),
            ",".join(_keywords(config.get("allowlist"))),
            ",".join(_keywords(config.get("denylist"))),
        ]
    )


def _cache_relevant_config(config: Mapping[str, Any]) -> dict[str, Any]:
    ignored = {"artifact_helpers", "budget_tracker", "child_work_meter", "attempt_output_root", "clock", "sleep", "out_dir", "fixture_mode", "mode"}
    return {str(key): value for key, value in config.items() if str(key) not in ignored}


def _clip_id(item: Mapping[str, Any]) -> str:
    for key in ("clip_id", "item_id", "source_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return deterministic_id(item.get("media_path", ""), item.get("content_hash", ""), prefix="clip")
