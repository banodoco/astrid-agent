"""Semantic visual/video filters backed by Understanding actions."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import hashlib
import json
import mimetypes
import re
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from string import Formatter
from typing import Any

import jsonschema

from astrid import sdk

from ..artifacts import (
    load_valid_cached_sidecar,
    sidecar_hashes,
    unlink_stale_sidecar,
    write_hashed_sidecar,
)
from ..caption_providers.understanding import _materialize_video_result, _materialize_visual_result
from ....shared.interfaces import FilterResult
from ....shared.items import deterministic_id
from ._common import (
    build_filter_stats,
    increment_reason,
    pass_item,
    reject_item,
    resolve_media_path,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]

SEMANTIC_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["accept", "reason", "score", "details"],
    "properties": {
        "accept": {"type": "boolean"},
        "reason": {"type": "string"},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "details": {"type": "object"},
    },
}


class _SemanticFilterBase:
    stage_id = ""
    stage_order = 6
    provider_id = ""

    def __init__(self, *, runner: Runner = subprocess.run, repo_root: Path = PACK_ROOT, **_: Any) -> None:
        self._runner = runner
        self._repo_root = repo_root

    def apply(self, items: list[dict[str, Any]], state: dict[str, Any], config: dict[str, Any]) -> FilterResult:
        started = time.perf_counter()
        passed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        warnings: list[str] = []

        for item in items:
            try:
                decision = _validate_decision(self._decision(item, config))
            except Exception as exc:  # noqa: BLE001 - semantic failures are explicit filter rejections
                reason = f"{self.stage_id}_unavailable"
                rejected.append(reject_item(item, self.stage_id, reason=reason, extra={"error": type(exc).__name__, "feedback_hint": reason}))
                increment_reason(reasons, reason)
                continue

            feedback_hint = _feedback_hint(self.stage_id, decision["reason"])
            extra = {
                "details": dict(decision.get("details") or {}),
                "feedback_hint": feedback_hint,
                "semantic_decision": decision,
            }
            if decision["accept"] is True:
                passed.append(pass_item(item, self.stage_id, reason=str(decision["reason"]), score=float(decision["score"]), extra=extra))
                continue
            reason = _reject_reason(self.stage_id, decision["reason"])
            rejected.append(reject_item(item, self.stage_id, reason=reason, score=float(decision["score"]), extra=extra))
            increment_reason(reasons, reason)

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

    def _decision(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
        sidecar = semantic_sidecar_path(item, config, stage_id=self.stage_id, repo_root=self._repo_root)
        if _fixture_mode(config):
            fixture = _fixture_semantic_path(item, config, stage_id=self.stage_id, repo_root=self._repo_root)
            raw = json.loads(fixture.read_text(encoding="utf-8")) if fixture is not None and fixture.is_file() else _fixture_decision(item, config)
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return _extract_decision(raw)

        sidecar.parent.mkdir(parents=True, exist_ok=True)
        schema_path = _write_schema_sidecar(sidecar, self.stage_id)
        hashes = sidecar_hashes(
            prompt=_prompt(item, config),
            schema=schema_path,
            media=item,
            config=_cache_relevant_config(config, self.stage_id),
        )
        cached = load_valid_cached_sidecar(sidecar, hashes)
        if cached is not None:
            return _extract_decision(cached)
        unlink_stale_sidecar(sidecar)
        meter = config.get("child_work_meter")
        attempt_root = config.get("attempt_output_root")
        capability = f"understanding.{self.provider_id}"
        if meter is None or not isinstance(attempt_root, (str, Path)):
            raise RuntimeError(f"{capability} requires the run-scoped child meter and trusted attempt output root")
        root = Path(attempt_root).expanduser().resolve(strict=True)
        media = resolve_media_path(item, repo_root=self._repo_root, required=True, must_exist=True).resolve(strict=True)
        media_type = mimetypes.guess_type(media.name)[0]
        if not media_type or not media_type.startswith("video/"):
            raise ValueError("selected semantic filter input must have a video media type")
        inputs: dict[str, Any] = {"query": _prompt(item, config)}
        objects: list[dict[str, Any]] = []
        for port, source, content_type in (
            ("video", media, media_type),
            ("response_schema", schema_path, "application/json"),
        ):
            source = source.resolve(strict=True)
            info = source.stat()
            limit = min(meter.OBJECT_MAX_BYTES, meter.limits["max_child_bytes"])
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError("semantic filter input must be a regular file within the child byte limit")
            with source.open("rb") as stream:
                payload = stream.read(limit + 1)
            if len(payload) > limit:
                raise ValueError("semantic filter input exceeds the child byte limit")
            if port == "video" and source.is_relative_to(root):
                # Preserve a producer already registered by acquisition/captioning.
                staged = source
            else:
                directory = root
                for component in ("filter-inputs", port):
                    directory = directory / component
                    if directory.is_symlink():
                        raise ValueError("semantic filter input staging must not be a symlink")
                    directory.mkdir(exist_ok=True)
                    if not directory.resolve(strict=True).is_relative_to(root):
                        raise ValueError("semantic filter input staging escapes attempt output root")
                staged = directory / (hashlib.sha256(payload).hexdigest() + source.suffix.lower())
                if staged.is_symlink():
                    raise ValueError("semantic filter staged input must not be a symlink")
                if staged.exists():
                    if not staged.is_file() or staged.stat().st_size != len(payload):
                        raise ValueError("semantic filter staged input bytes changed")
                    with staged.open("rb") as stream:
                        if stream.read(limit + 1) != payload:
                            raise ValueError("semantic filter staged input bytes changed")
                else:
                    with staged.open("xb") as stream:
                        stream.write(payload)
            producer = {"filename": staged.relative_to(root).as_posix(), "media_type": content_type, "output_port": port}
            inputs[port] = producer
            objects.append(meter.register_local_file(staged, producer, name=port))
        if self.provider_id == "visual_understand":
            inputs["at"] = _sample_time(item)
        else:
            inputs["max_chunks"] = 1
            start, end = item.get("clip_start_s"), item.get("clip_end_s")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and float(end) > float(start):
                inputs.update(start=f"{float(start):.3f}", end=f"{float(end):.3f}")
        if len(objects) > meter.limits["max_child_inputs"] or sum(obj["size"] for obj in objects) > meter.limits["max_child_bytes"]:
            raise RuntimeError("semantic filter child input count or byte budget exceeded")
        identity = json.dumps(
            [_clip_id(item), inputs, [obj["digest"] for obj in objects]],
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        child_key = self.stage_id + "_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        _increment_budget(config, self.stage_id)
        meter.admit(child_key, capability, inputs)
        child = sdk.invoke(
            capability, kind="action", inputs=inputs, child_key=child_key,
            wait=True, timeout_seconds=600.0, poll_seconds=0.1,
        )
        materialize = _materialize_visual_result if self.provider_id == "visual_understand" else _materialize_video_result
        raw = materialize(child, attempt_root=root, meter=meter)
        decision = _extract_decision(raw)
        write_hashed_sidecar(sidecar, decision, hashes)
        return decision


class SemanticVisualFilter(_SemanticFilterBase):
    stage_id = "semantic_visual_filter"
    stage_order = 3
    provider_id = "visual_understand"


class SemanticVideoFilter(_SemanticFilterBase):
    stage_id = "semantic_video_filter"
    stage_order = 4
    provider_id = "video_understand"


def semantic_sidecar_path(item: Mapping[str, Any], config: Mapping[str, Any], *, stage_id: str, repo_root: Path = PACK_ROOT) -> Path:
    out_dir = config.get("out_dir")
    if out_dir is None:
        out_dir = resolve_media_path(item, repo_root=repo_root, required=True).parent / stage_id
    path = Path(str(out_dir)).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve() / f"{_clip_id(item)}.{stage_id}.json"


def _write_schema_sidecar(sidecar: Path, stage_id: str) -> Path:
    schema_path = sidecar.with_suffix(".schema.json")
    schema_path.write_text(json.dumps({"name": stage_id, "schema": SEMANTIC_DECISION_SCHEMA, "strict": True}, indent=2) + "\n", encoding="utf-8")
    return schema_path


def _extract_decision(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping) and {"accept", "reason", "score"}.issubset(raw.keys()):
        details = raw.get("details")
        return {
            "accept": bool(raw["accept"]),
            "reason": str(raw["reason"]),
            "score": float(raw["score"]),
            "details": dict(details) if isinstance(details, Mapping) else {},
        }
    if isinstance(raw, Mapping):
        results = raw.get("results")
        if isinstance(results, list):
            for result in results:
                if isinstance(result, Mapping) and result.get("status") == "ok":
                    return _extract_decision(result.get("answer"))
        if "answer" in raw:
            return _extract_decision(raw["answer"])
    if isinstance(raw, str):
        return _extract_decision(json.loads(raw))
    raise ValueError("semantic filter output did not contain a schema-valid decision object")


def _validate_decision(raw: Mapping[str, Any]) -> dict[str, Any]:
    decision = dict(raw)
    jsonschema.Draft7Validator(SEMANTIC_DECISION_SCHEMA).validate(decision)
    return decision


def _prompt(item: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    template = str(config.get("prompt_template") or "Decide whether this clip should remain in the training dataset.")
    values = _SafeFormatDict(
        clip_id=_clip_id(item),
        item_id=item.get("item_id", ""),
        source_id=item.get("source_id", ""),
        media_path=item.get("media_path", ""),
        duration_s=item.get("duration_s", ""),
        bucket=item.get("bucket", ""),
        feedback_hints=", ".join(_feedback_hints(config)),
    )
    field_names = [field_name for _, field_name, _, _ in Formatter().parse(template) if field_name]
    return template.format_map(values) if field_names else template


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _increment_budget(config: Mapping[str, Any], stage_id: str) -> None:
    tracker = config.get("budget_tracker")
    if tracker is not None and hasattr(tracker, "increment"):
        tracker.increment("filter.semantic_visual" if stage_id == "semantic_visual_filter" else "filter.semantic_video")


def _fixture_mode(config: Mapping[str, Any]) -> bool:
    return bool(config.get("fixture_mode") or config.get("mode") == "fixture")


def _feedback_hints(config: Mapping[str, Any]) -> list[str]:
    value = config.get("top_up_feedback_hints", config.get("feedback_hints", []))
    if isinstance(value, (str, bytes)):
        return [str(value)] if value else []
    try:
        return [str(item) for item in value if str(item)]
    except TypeError:
        return [str(value)] if value else []


def _fixture_semantic_path(item: Mapping[str, Any], config: Mapping[str, Any], *, stage_id: str, repo_root: Path) -> Path | None:
    semantic_file = item.get(f"{stage_id}_file") or item.get("semantic_file") or config.get("semantic_file")
    if isinstance(semantic_file, str):
        path = Path(semantic_file).expanduser()
        return path if path.is_absolute() else (repo_root / path).resolve()
    fixture_dir = config.get("fixture_semantic_dir") or config.get("fixture_dir")
    if isinstance(fixture_dir, str):
        path = Path(fixture_dir).expanduser()
        root = path if path.is_absolute() else (repo_root / path).resolve()
        return root / f"{_clip_id(item)}.{stage_id}.json"
    return None


def _fixture_decision(item: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    decisions = config.get("fixture_decisions")
    item_id = _clip_id(item)
    if isinstance(decisions, Mapping) and isinstance(decisions.get(item_id), Mapping):
        return _extract_decision(decisions[item_id])
    return {"accept": True, "reason": "fixture_default", "score": 1.0, "details": {"fixture": True}}


def _cache_relevant_config(config: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
    ignored = {"artifact_helpers", "budget_tracker", "child_work_meter", "attempt_output_root", "clock", "sleep", "out_dir", "fixture_mode", "mode"}
    payload = {str(key): value for key, value in config.items() if str(key) not in ignored}
    payload["stage_id"] = stage_id
    return payload


def _sample_time(item: Mapping[str, Any]) -> str:
    start = float(item.get("clip_start_s") or 0.0)
    if isinstance(item.get("duration_s"), (int, float)):
        return f"{start + max(0.0, float(item['duration_s']) / 2.0):.3f}"
    end = item.get("clip_end_s")
    if isinstance(end, (int, float)) and float(end) > start:
        return f"{start + ((float(end) - start) / 2.0):.3f}"
    return "0.000"


def _reject_reason(stage_id: str, reason: Any) -> str:
    return f"{stage_id}_{_slug(reason)}"


def _feedback_hint(stage_id: str, reason: Any) -> str:
    return f"{stage_id}:{_slug(reason)}"


def _slug(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")
    return text or "rejected"


def _clip_id(item: Mapping[str, Any]) -> str:
    for key in ("clip_id", "item_id", "source_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return deterministic_id(item.get("media_path", ""), item.get("content_hash", ""), prefix="clip")
