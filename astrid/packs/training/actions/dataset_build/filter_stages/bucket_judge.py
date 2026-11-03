"""Generic bucket-judge filter gate."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import hashlib
import json
import mimetypes
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
from ..budget import BudgetTracker
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

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["accept", "bucket", "reason", "score"],
    "properties": {
        "accept": {"type": "boolean"},
        "bucket": {"type": ["string", "null"]},
        "reason": {"type": "string"},
        "score": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


class BucketJudgeGate:
    def __init__(self, *, runner: Runner = subprocess.run, repo_root: Path = PACK_ROOT, **_: Any) -> None:
        self._runner = runner
        self._repo_root = repo_root

    @property
    def stage_id(self) -> str:
        return "bucket_judge_filter"

    @property
    def stage_order(self) -> int:
        return 1

    def apply(self, items: list[dict[str, Any]], state: dict[str, Any], config: dict[str, Any]) -> FilterResult:
        started = time.perf_counter()
        gate_config = _gate_config(config)
        if not gate_config.get("enabled", False):
            return _disabled_result(self.stage_id, self.stage_order, items, started)

        allowed_buckets = _allowed_buckets(config, state, gate_config)
        passed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        warnings: list[str] = []
        for item in items:
            raw = self._judge_item(item, gate_config, allowed_buckets)
            decision = _validate_decision(raw)
            updated = dict(item)
            updated["judge_result"] = decision
            reason = str(decision["reason"])
            score = float(decision["score"])
            bucket = decision["bucket"]
            if decision["accept"] is True and isinstance(bucket, str) and bucket in allowed_buckets:
                updated["bucket"] = bucket
                updated = pass_item(updated, self.stage_id, reason=reason, score=score)
                passed.append(updated)
                continue
            if decision["accept"] is True:
                reason = f"invalid_bucket:{bucket}"
                warnings.append(reason)
            increment_reason(reasons, reason)
            updated = reject_item(updated, self.stage_id, reason=reason, score=score)
            rejected.append(updated)

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

    def _judge_item(self, item: Mapping[str, Any], config: Mapping[str, Any], allowed_buckets: list[str]) -> dict[str, Any]:
        sidecar = judge_sidecar_path(item, config, repo_root=self._repo_root)
        if _fixture_mode(config):
            fixture = _fixture_judge_path(item, config, repo_root=self._repo_root)
            if fixture is not None and fixture.is_file():
                raw = json.loads(fixture.read_text(encoding="utf-8"))
            else:
                raw = _deterministic_fixture_decision(allowed_buckets)
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return raw

        sidecar.parent.mkdir(parents=True, exist_ok=True)
        schema_path = _write_schema_sidecar(sidecar)
        hashes = sidecar_hashes(
            prompt=_prompt(item, config),
            schema=schema_path,
            media=item,
            config=_cache_relevant_config(config, allowed_buckets),
        )
        cached = load_valid_cached_sidecar(sidecar, hashes)
        if cached is not None:
            return _extract_decision(cached)
        unlink_stale_sidecar(sidecar)
        provider = str(config.get("provider") or "visual_understand")
        if provider not in {"visual_understand", "video_understand"}:
            raise ValueError(f"unsupported bucket judge provider {provider!r}")
        capability = f"understanding.{provider}"
        meter = config.get("child_work_meter")
        attempt_root = config.get("attempt_output_root")
        if meter is None or not isinstance(attempt_root, (str, Path)):
            raise RuntimeError(f"{capability} requires the run-scoped child meter and trusted attempt output root")
        root = Path(attempt_root).expanduser().resolve(strict=True)
        media = _media_path(item, repo_root=self._repo_root).resolve(strict=True)
        media_type = mimetypes.guess_type(media.name)[0]
        if not media_type or not media_type.startswith("video/"):
            raise ValueError("selected bucket judge input must have a video media type")
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
                raise ValueError("bucket judge input must be a regular file within the child byte limit")
            with source.open("rb") as stream:
                payload = stream.read(limit + 1)
            if len(payload) > limit:
                raise ValueError("bucket judge input exceeds the child byte limit")
            if port == "video" and source.is_relative_to(root):
                # Preserve a producer already registered by acquisition/captioning.
                staged = source
            else:
                directory = root
                for component in ("filter-inputs", port):
                    directory = directory / component
                    if directory.is_symlink():
                        raise ValueError("bucket judge input staging must not be a symlink")
                    directory.mkdir(exist_ok=True)
                    if not directory.resolve(strict=True).is_relative_to(root):
                        raise ValueError("bucket judge input staging escapes attempt output root")
                staged = directory / (hashlib.sha256(payload).hexdigest() + source.suffix.lower())
                if staged.is_symlink():
                    raise ValueError("bucket judge staged input must not be a symlink")
                if staged.exists():
                    if not staged.is_file() or staged.stat().st_size != len(payload):
                        raise ValueError("bucket judge staged input bytes changed")
                    with staged.open("rb") as stream:
                        if stream.read(limit + 1) != payload:
                            raise ValueError("bucket judge staged input bytes changed")
                else:
                    with staged.open("xb") as stream:
                        stream.write(payload)
            producer = {"filename": staged.relative_to(root).as_posix(), "media_type": content_type, "output_port": port}
            inputs[port] = producer
            objects.append(meter.register_local_file(staged, producer, name=port))
        if provider == "visual_understand":
            inputs["at"] = _sample_time(item)
        else:
            inputs["max_chunks"] = 1
            start, end = item.get("clip_start_s"), item.get("clip_end_s")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and float(end) > float(start):
                inputs.update(start=f"{float(start):.3f}", end=f"{float(end):.3f}")
        if len(objects) > meter.limits["max_child_inputs"] or sum(obj["size"] for obj in objects) > meter.limits["max_child_bytes"]:
            raise RuntimeError("bucket judge child input count or byte budget exceeded")
        identity = json.dumps(
            [provider, _clip_id(item), inputs, [obj["digest"] for obj in objects]],
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        child_key = "bucket_judge_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        _increment_budget(config)
        meter.admit(child_key, capability, inputs)
        child = sdk.invoke(
            capability, kind="action", inputs=inputs, child_key=child_key,
            wait=True, timeout_seconds=600.0, poll_seconds=0.1,
        )
        materialize = _materialize_visual_result if provider == "visual_understand" else _materialize_video_result
        raw = materialize(child, attempt_root=root, meter=meter)
        decision = _extract_decision(raw)
        write_hashed_sidecar(sidecar, decision, hashes)
        return decision


def judge_sidecar_path(item: Mapping[str, Any], config: Mapping[str, Any], *, repo_root: Path = PACK_ROOT) -> Path:
    out_dir = config.get("out_dir")
    if out_dir is None:
        out_dir = _media_path(item, repo_root=repo_root).parent / "judges"
    path = Path(str(out_dir)).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve() / f"{_clip_id(item)}.judge.json"


def _gate_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if "bucket_judge" in config and isinstance(config["bucket_judge"], Mapping):
        gate_config = dict(config["bucket_judge"])
        for key in ("budget_tracker", "budgets", "clock", "sleep", "artifact_helpers", "fixture_mode", "mode"):
            if key in config and key not in gate_config:
                gate_config[key] = config[key]
    else:
        extensions = config.get("extensions")
        if isinstance(extensions, Mapping) and isinstance(extensions.get("bucket_judge"), Mapping):
            gate_config = dict(extensions["bucket_judge"])
        else:
            gate_config = dict(config)
    # The two authority values always come from the parent run services.
    # Other nested projection behavior intentionally remains unchanged.
    for key in ("child_work_meter", "attempt_output_root"):
        gate_config[key] = config.get(key)
    return gate_config


def _disabled_result(stage_id: str, stage_order: int, items: list[dict[str, Any]], started: float) -> FilterResult:
    passed = [pass_item(item, stage_id, reason="disabled", score=1.0) for item in items]
    stats = build_filter_stats(
        stage_id=stage_id,
        stage_order=stage_order,
        items_in=len(items),
        items_passed=len(passed),
        items_rejected=0,
        warnings=["bucket_judge disabled"],
        started=started,
    )
    return FilterResult(passed=passed, rejected=[], stats=stats)


def _allowed_buckets(config: Mapping[str, Any], state: Mapping[str, Any], gate_config: Mapping[str, Any]) -> list[str]:
    buckets = gate_config.get("buckets")
    if isinstance(buckets, Mapping):
        return [str(key) for key in buckets]
    if isinstance(buckets, list):
        return [str(value) for value in buckets]
    config_buckets = config.get("buckets")
    if isinstance(config_buckets, Mapping):
        return [str(key) for key in config_buckets]
    state_buckets = state.get("buckets")
    if isinstance(state_buckets, Mapping):
        return [str(key) for key in state_buckets]
    return []


def _validate_decision(raw: Mapping[str, Any]) -> dict[str, Any]:
    decision = dict(raw)
    jsonschema.Draft7Validator(JUDGE_SCHEMA).validate(decision)
    return decision


def _prompt(item: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    template = str(config.get("prompt_template") or "Classify this clip for the configured training buckets.")
    buckets = config.get("buckets")
    if isinstance(buckets, Mapping):
        bucket_names = ", ".join(str(key) for key in buckets)
    elif isinstance(buckets, list):
        bucket_names = ", ".join(str(value) for value in buckets)
    else:
        bucket_names = ""
    values = _SafeFormatDict(
        clip_id=_clip_id(item),
        source_id=item.get("source_id", ""),
        media_path=item.get("media_path", ""),
        duration_s=item.get("duration_s", ""),
        buckets=bucket_names,
    )
    field_names = [field_name for _, field_name, _, _ in Formatter().parse(template) if field_name]
    if not field_names:
        return template
    return template.format_map(values)


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _write_schema_sidecar(sidecar: Path) -> Path:
    schema_path = sidecar.with_suffix(".schema.json")
    schema_path.write_text(json.dumps({"name": "bucket_judge", "schema": JUDGE_SCHEMA, "strict": True}, indent=2) + "\n", encoding="utf-8")
    return schema_path


def _extract_decision(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping) and {"accept", "bucket", "reason", "score"}.issubset(raw.keys()):
        return {
            "accept": bool(raw["accept"]),
            "bucket": raw["bucket"],
            "reason": str(raw["reason"]),
            "score": float(raw["score"]),
        }
    if isinstance(raw, Mapping):
        results = raw.get("results")
        if isinstance(results, list):
            for result in results:
                if isinstance(result, Mapping) and result.get("status") == "ok":
                    return _extract_decision(result.get("answer"))
        answer = raw.get("answer")
        if answer is not None:
            return _extract_decision(answer)
    if isinstance(raw, str):
        return _extract_decision(json.loads(raw))
    raise ValueError("bucket judge output did not contain a schema-valid decision object")


def _increment_budget(config: Mapping[str, Any]) -> None:
    tracker = config.get("budget_tracker")
    provider = str(config.get("provider") or "visual_understand")
    if tracker is not None and hasattr(tracker, "increment"):
        tracker.increment(f"bucket_judge.{provider}")
        return
    budgets = config.get("budgets")
    if isinstance(budgets, Mapping):
        BudgetTracker.from_config({"budgets": budgets}).increment(f"bucket_judge.{provider}")


def _fixture_mode(config: Mapping[str, Any]) -> bool:
    return bool(config.get("fixture_mode") or config.get("mode") == "fixture")


def _cache_relevant_config(config: Mapping[str, Any], allowed_buckets: list[str]) -> dict[str, Any]:
    ignored = {"artifact_helpers", "budget_tracker", "child_work_meter", "attempt_output_root", "clock", "sleep", "out_dir", "fixture_mode", "mode"}
    payload = {str(key): value for key, value in config.items() if str(key) not in ignored}
    payload["allowed_buckets"] = list(allowed_buckets)
    return payload


def _fixture_judge_path(item: Mapping[str, Any], config: Mapping[str, Any], *, repo_root: Path) -> Path | None:
    judge_file = item.get("judge_file") or config.get("judge_file")
    if isinstance(judge_file, str):
        path = Path(judge_file).expanduser()
        return path if path.is_absolute() else (repo_root / path).resolve()
    fixture_dir = config.get("fixture_judge_dir") or config.get("fixture_dir")
    if isinstance(fixture_dir, str):
        path = Path(fixture_dir).expanduser()
        root = path if path.is_absolute() else (repo_root / path).resolve()
        return root / f"{_clip_id(item)}.judge.json"
    return None


def _deterministic_fixture_decision(allowed_buckets: list[str]) -> dict[str, Any]:
    return {
        "accept": bool(allowed_buckets),
        "bucket": allowed_buckets[0] if allowed_buckets else None,
        "reason": "fixture_default",
        "score": 1.0 if allowed_buckets else 0.0,
    }


def _clip_id(item: Mapping[str, Any]) -> str:
    for key in ("clip_id", "item_id", "source_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return deterministic_id(item.get("media_path", ""), item.get("content_hash", ""), prefix="clip")


def _media_path(item: Mapping[str, Any], *, repo_root: Path) -> Path:
    resolved = resolve_media_path(item, repo_root=repo_root, required=True)
    if resolved is None:
        raise ValueError("item missing media_path")
    return resolved


def _sample_time(item: Mapping[str, Any]) -> str:
    if isinstance(item.get("duration_s"), (int, float)):
        return f"{max(0.0, float(item['duration_s']) / 2.0):.3f}"
    return "0.000"
