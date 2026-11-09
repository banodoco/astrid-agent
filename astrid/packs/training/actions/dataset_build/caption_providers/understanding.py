"""Caption providers backed by existing Understanding actions."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import hashlib
import json
import mimetypes
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from string import Formatter
from typing import Any

from astrid import sdk

from ..artifacts import (
    load_valid_cached_sidecar,
    sidecar_hashes,
    unlink_stale_sidecar,
    write_hashed_sidecar,
)
from ....shared.interfaces import CaptionResult
from ....shared.items import deterministic_id, repo_relative_path

Runner = Callable[..., subprocess.CompletedProcess[str]]


DEFAULT_PROMPT = "Describe this training clip in one concise caption."


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class _BaseUnderstandingCaptionProvider:
    provider_id = ""
    module_name = ""
    default_model = ""

    def __init__(self, *, runner: Runner = subprocess.run, repo_root: Path = PACK_ROOT, **_: Any) -> None:
        self._runner = runner
        self._repo_root = repo_root

    def caption(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> CaptionResult:
        sidecar = caption_sidecar_path(item, config, repo_root=self._repo_root)
        if _fixture_mode(config):
            result = self._fixture_caption(item, config)
            _write_sidecar(sidecar, result)
            return result

        sidecar.parent.mkdir(parents=True, exist_ok=True)
        hashes = self._sidecar_hashes(item, config)
        cached = load_valid_cached_sidecar(sidecar, hashes)
        if cached is not None:
            return _caption_from_raw(cached, provider_id=self.provider_id, fallback_model=self._model(config))
        unlink_stale_sidecar(sidecar)
        command = self._build_command(item, config, sidecar)
        self._increment_budget(config)
        completed = self._runner(command, capture_output=True, text=True, check=True)
        raw = _load_runner_output(sidecar, completed.stdout)
        result = _caption_from_raw(raw, provider_id=self.provider_id, fallback_model=self._model(config))
        write_hashed_sidecar(sidecar, _result_to_dict(result), hashes)
        return result

    def _fixture_caption(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> CaptionResult:
        clip_id = _clip_id(item)
        prebaked = _prebaked_caption_path(item, config, clip_id, repo_root=self._repo_root)
        if prebaked is not None and prebaked.is_file():
            raw = json.loads(prebaked.read_text(encoding="utf-8"))
            return _caption_from_raw(raw, provider_id="fixture", fallback_model="fixture")
        fixture_captions = config.get("fixture_captions")
        if isinstance(fixture_captions, Mapping) and isinstance(fixture_captions.get(clip_id), str):
            text = str(fixture_captions[clip_id])
        else:
            text = f"Fixture caption for {clip_id}."
        return CaptionResult(text=text, schema_version=1, confidence=1.0, model="fixture", raw_response={"fixture": True})

    def _build_command(self, item: Mapping[str, Any], config: Mapping[str, Any], sidecar: Path) -> list[str]:
        raise NotImplementedError

    def _model(self, config: Mapping[str, Any]) -> str:
        model = config.get("model")
        return str(model) if model else self.default_model

    def _prompt(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> str:
        template = str(config.get("prompt_template") or DEFAULT_PROMPT)
        values = _SafeFormatDict(
            clip_id=_clip_id(item),
            item_id=item.get("item_id", ""),
            source_id=item.get("source_id", ""),
            source_type=item.get("source_type", ""),
            bucket=item.get("bucket", ""),
            media_path=item.get("media_path", ""),
            duration_s=item.get("duration_s", ""),
        )
        field_names = [field_name for _, field_name, _, _ in Formatter().parse(template) if field_name]
        if not field_names:
            return template
        return template.format_map(values)

    def _sidecar_hashes(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, str]:
        return sidecar_hashes(
            prompt=self._prompt(item, config),
            schema=config.get("schema_path"),
            media=item,
            config=_cache_relevant_config(config),
        )

    def _base_command(self, config: Mapping[str, Any], sidecar: Path) -> list[str]:
        command = [sys.executable, "-X", "utf8", "-m", self.module_name, "--query", ""]
        mode = config.get("mode")
        model = config.get("model")
        if mode:
            command.extend(["--mode", str(mode)])
        if model:
            command.extend(["--model", str(model)])
        env_file = config.get("env_file")
        if env_file:
            command.extend(["--env-file", str(env_file)])
        command.extend(["--out-dir", str(sidecar.parent), "--out", str(sidecar)])
        return command

    def _increment_budget(self, config: Mapping[str, Any]) -> None:
        tracker = config.get("budget_tracker")
        if tracker is not None and hasattr(tracker, "increment"):
            tracker.increment(f"caption.{self.provider_id}")


class VisualUnderstandCaptionProvider(_BaseUnderstandingCaptionProvider):
    provider_id = "visual_understand"
    default_model = "gpt-4o-mini"

    def caption(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> CaptionResult:
        if config.get("env_file") not in (None, ""):
            raise ValueError(
                "env_file is unsupported for delegated Understanding calls; configure OPENAI_API_KEY through the execution host's managed credentials."
            )
        sidecar = caption_sidecar_path(item, config, repo_root=self._repo_root)
        if _fixture_mode(config):
            result = self._fixture_caption(item, config)
            _write_sidecar(sidecar, result)
            return result

        sidecar.parent.mkdir(parents=True, exist_ok=True)
        hashes = self._sidecar_hashes(item, config)
        cached = load_valid_cached_sidecar(sidecar, hashes)
        if cached is not None:
            return _caption_from_raw(cached, provider_id=self.provider_id, fallback_model=self._model(config))
        unlink_stale_sidecar(sidecar)
        meter = config.get("child_work_meter")
        attempt_root = config.get("attempt_output_root")
        if meter is None or not isinstance(attempt_root, (str, Path)):
            raise RuntimeError("understanding.visual_understand requires the run-scoped child meter and trusted attempt output root")
        root = Path(attempt_root).expanduser().resolve(strict=True)
        inputs, objects = self._public_inputs(item, config, meter=meter, attempt_root=root)
        identity = json.dumps(
            [_clip_id(item), inputs, [obj["digest"] for obj in objects]],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        child_key = "caption_visual_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        self._increment_budget(config)
        meter.admit(child_key, "understanding.visual_understand", inputs)
        child = sdk.invoke(
            "understanding.visual_understand",
            kind="action",
            inputs=inputs,
            child_key=child_key,
            wait=True,
            timeout_seconds=600.0,
            poll_seconds=0.1,
        )
        raw = _materialize_visual_result(child, attempt_root=root, meter=meter)
        result = _caption_from_raw(raw, provider_id=self.provider_id, fallback_model=self._model(config))
        write_hashed_sidecar(sidecar, _result_to_dict(result), hashes)
        return result

    def _sidecar_hashes(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, str]:
        # Runtime authority does not change the existing caption cache identity.
        return super()._sidecar_hashes(
            item, {key: value for key, value in config.items() if key not in {"child_work_meter", "attempt_output_root"}}
        )

    def _public_inputs(
        self, item: Mapping[str, Any], config: Mapping[str, Any], *, meter: Any, attempt_root: Path
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        media = _media_path(item, repo_root=self._repo_root).resolve(strict=True)
        media_type = mimetypes.guess_type(media.name)[0]
        if not media_type or not media_type.startswith(("image/", "video/")):
            raise ValueError("selected visual caption clip must have an image or video media type")
        port = "image" if media_type.startswith("image/") else "video"
        producer = _visual_producer(media, attempt_root=attempt_root, port=port, media_type=media_type)
        objects = [meter.register_local_file(media, producer, name=port)]
        inputs: dict[str, Any] = {port: producer, "query": self._prompt(item, config)}
        if port == "video":
            inputs["at"] = _sample_time(item)
        for name in ("mode", "model"):
            if config.get(name):
                inputs[name] = str(config[name])
        if config.get("schema_path"):
            source = Path(str(config["schema_path"])).expanduser()
            if not source.is_absolute():
                source = self._repo_root / source
            source = source.resolve(strict=True)
            info = source.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > meter.OBJECT_MAX_BYTES:
                raise ValueError("visual caption schema must be a regular file within 64 MiB")
            with source.open("rb") as stream:
                payload = stream.read(meter.OBJECT_MAX_BYTES + 1)
            if len(payload) > meter.OBJECT_MAX_BYTES:
                raise ValueError("visual caption schema exceeds 64 MiB")
            # Schema data is a declared file input. Snapshot it into the
            # parent's spool; credentials use the host's managed secret route.
            directory = attempt_root
            for component in ("caption-inputs", "response_schema"):
                directory = directory / component
                if directory.is_symlink():
                    raise ValueError("visual caption schema staging must not be a symlink")
                directory.mkdir(exist_ok=True)
                if not directory.resolve(strict=True).is_relative_to(attempt_root):
                    raise ValueError("visual caption schema staging escapes attempt output root")
            staged = directory / (hashlib.sha256(payload).hexdigest() + ".json")
            if staged.is_symlink():
                raise ValueError("visual caption staged schema must not be a symlink")
            if staged.exists():
                if not staged.is_file() or staged.read_bytes() != payload:
                    raise ValueError("visual caption staged schema bytes changed")
            else:
                with staged.open("xb") as stream:
                    stream.write(payload)
            producer = _visual_producer(
                staged, attempt_root=attempt_root, port="response_schema", media_type="application/json"
            )
            inputs["response_schema"] = producer
            objects.append(meter.register_local_file(staged, producer, name="response_schema"))
        if len(objects) > meter.limits["max_child_inputs"] or sum(obj["size"] for obj in objects) > meter.limits["max_child_bytes"]:
            raise RuntimeError("visual caption child input count or byte budget exceeded")
        return inputs, objects


def _visual_producer(path: Path, *, attempt_root: Path, port: str, media_type: str) -> dict[str, str]:
    try:
        filename = path.relative_to(attempt_root).as_posix()
    except ValueError as exc:
        raise ValueError("selected visual caption input is outside the parent attempt output root") from exc
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("selected visual caption input must be a regular file")
    return {"filename": filename, "media_type": media_type, "output_port": port}


def _materialize_visual_result(child: Any, *, attempt_root: Path, meter: Any) -> Any:
    if (
        getattr(child, "ok", False) is not True
        or not isinstance(getattr(child, "raw_result", None), Mapping)
        or child.raw_result.get("state") != "completed"
        or not all(
            isinstance(value, str) and value
            for value in (getattr(child, "kernel_task_id", None), getattr(child, "kernel_attempt_id", None), getattr(child, "kernel_run_id", None))
        )
    ):
        raise RuntimeError(f"understanding.visual_understand child did not complete: {getattr(child, 'error', None)}")
    outputs = getattr(child, "outputs", None)
    rows = outputs.get("managed_outputs") if isinstance(outputs, Mapping) else None
    matches = [row for row in rows or [] if isinstance(row, Mapping) and row.get("output_port") == "result"]
    if len(matches) != 1:
        raise ValueError("understanding.visual_understand must return exactly one result output")
    row = dict(matches[0])
    if (row.get("task_id"), row.get("attempt_id"), row.get("run_id")) != (
        child.kernel_task_id, child.kernel_attempt_id, child.kernel_run_id,
    ):
        raise ValueError("understanding.visual_understand output task/attempt identity changed")
    size, digest = row.get("size"), row.get("digest")
    if (
        type(size) is not int or size < 0
        or not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or row.get("object_id") != digest
        or not isinstance(row.get("association_id"), str) or not row["association_id"]
        or row.get("media_type") != "application/json"
    ):
        raise ValueError("understanding.visual_understand result has an invalid byte identity or media type")
    meter.retain(row)
    local = child.materialize_output(row["association_id"])
    if dict(local.output) != row:
        raise ValueError("understanding.visual_understand materialized output descriptor changed")
    relative = Path(local.filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("understanding.visual_understand output filename escapes attempt output root")
    path = (attempt_root / relative).resolve(strict=True)
    if not path.is_relative_to(attempt_root) or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("understanding.visual_understand output is not a regular file under attempt output root")
    payload = path.read_bytes()
    if len(payload) != size or "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("understanding.visual_understand output size or digest changed")
    return json.loads(payload.decode("utf-8"))


class VideoUnderstandCaptionProvider(_BaseUnderstandingCaptionProvider):
    provider_id = "video_understand"
    default_model = "gemini-2.5-flash"

    def caption(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> CaptionResult:
        if config.get("env_file") not in (None, ""):
            raise ValueError(
                "env_file is unsupported for delegated Understanding calls; configure GEMINI_API_KEY through the execution host's managed credentials."
            )
        sidecar = caption_sidecar_path(item, config, repo_root=self._repo_root)
        if _fixture_mode(config):
            result = self._fixture_caption(item, config)
            _write_sidecar(sidecar, result)
            return result

        sidecar.parent.mkdir(parents=True, exist_ok=True)
        hashes = self._sidecar_hashes(item, config)
        cached = load_valid_cached_sidecar(sidecar, hashes)
        if cached is not None:
            return _caption_from_raw(cached, provider_id=self.provider_id, fallback_model=self._model(config))
        unlink_stale_sidecar(sidecar)
        meter = config.get("child_work_meter")
        attempt_root = config.get("attempt_output_root")
        if meter is None or not isinstance(attempt_root, (str, Path)):
            raise RuntimeError("understanding.video_understand requires the run-scoped child meter and trusted attempt output root")
        root = Path(attempt_root).expanduser().resolve(strict=True)
        inputs, objects = self._public_inputs(item, config, meter=meter, attempt_root=root)
        identity = json.dumps(
            [_clip_id(item), inputs, [obj["digest"] for obj in objects]],
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        child_key = "caption_video_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
        self._increment_budget(config)
        meter.admit(child_key, "understanding.video_understand", inputs)
        child = sdk.invoke(
            "understanding.video_understand",
            kind="action",
            inputs=inputs,
            child_key=child_key,
            wait=True,
            timeout_seconds=600.0,
            poll_seconds=0.1,
        )
        raw = _materialize_video_result(child, attempt_root=root, meter=meter)
        result = _caption_from_raw(raw, provider_id=self.provider_id, fallback_model=self._model(config))
        write_hashed_sidecar(sidecar, _result_to_dict(result), hashes)
        return result

    def _sidecar_hashes(self, item: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, str]:
        # Runtime authority does not change the existing caption cache identity.
        return super()._sidecar_hashes(
            item, {key: value for key, value in config.items() if key not in {"child_work_meter", "attempt_output_root"}}
        )

    def _public_inputs(
        self, item: Mapping[str, Any], config: Mapping[str, Any], *, meter: Any, attempt_root: Path
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        media = _media_path(item, repo_root=self._repo_root).resolve(strict=True)
        media_type = mimetypes.guess_type(media.name)[0]
        if not media_type or not media_type.startswith("video/"):
            raise ValueError("selected video caption clip must have a video media type")
        producer = _visual_producer(media, attempt_root=attempt_root, port="video", media_type=media_type)
        objects = [meter.register_local_file(media, producer, name="video")]
        inputs: dict[str, Any] = {"video": producer, "query": self._prompt(item, config), "max_chunks": 1}
        for name in ("mode", "model"):
            if config.get(name):
                inputs[name] = str(config[name])
        start = item.get("clip_start_s")
        end = item.get("clip_end_s")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and float(end) > float(start):
            inputs.update(start=f"{float(start):.3f}", end=f"{float(end):.3f}")
        if len(objects) > meter.limits["max_child_inputs"] or sum(obj["size"] for obj in objects) > meter.limits["max_child_bytes"]:
            raise RuntimeError("video caption child input count or byte budget exceeded")
        return inputs, objects


def _materialize_video_result(child: Any, *, attempt_root: Path, meter: Any) -> Any:
    if (
        getattr(child, "ok", False) is not True
        or not isinstance(getattr(child, "raw_result", None), Mapping)
        or child.raw_result.get("state") != "completed"
        or not all(
            isinstance(value, str) and value
            for value in (getattr(child, "kernel_task_id", None), getattr(child, "kernel_attempt_id", None), getattr(child, "kernel_run_id", None))
        )
    ):
        raise RuntimeError(f"understanding.video_understand child did not complete: {getattr(child, 'error', None)}")
    outputs = getattr(child, "outputs", None)
    rows = outputs.get("managed_outputs") if isinstance(outputs, Mapping) else None
    matches = [row for row in rows or [] if isinstance(row, Mapping) and row.get("output_port") == "result"]
    if len(matches) != 1:
        raise ValueError("understanding.video_understand must return exactly one result output")
    row = dict(matches[0])
    if (row.get("task_id"), row.get("attempt_id"), row.get("run_id")) != (
        child.kernel_task_id, child.kernel_attempt_id, child.kernel_run_id,
    ):
        raise ValueError("understanding.video_understand output task/attempt identity changed")
    size, digest = row.get("size"), row.get("digest")
    if (
        type(size) is not int or size < 0
        or not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or row.get("object_id") != digest
        or not isinstance(row.get("association_id"), str) or not row["association_id"]
        or row.get("media_type") != "application/json"
    ):
        raise ValueError("understanding.video_understand result has an invalid byte identity or media type")
    meter.retain(row)
    local = child.materialize_output(row["association_id"])
    if dict(local.output) != row:
        raise ValueError("understanding.video_understand materialized output descriptor changed")
    relative = Path(local.filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("understanding.video_understand output filename escapes attempt output root")
    path = (attempt_root / relative).resolve(strict=True)
    if not path.is_relative_to(attempt_root) or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("understanding.video_understand output is not a regular file under attempt output root")
    payload = path.read_bytes()
    if len(payload) != size or "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("understanding.video_understand output size or digest changed")
    return json.loads(payload.decode("utf-8"))


def caption_candidate(
    item: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    runner: Runner = subprocess.run,
    repo_root: Path = PACK_ROOT,
) -> tuple[CaptionResult, Path]:
    provider_id = str(config.get("provider") or "visual_understand")
    if provider_id == "visual_understand":
        provider = VisualUnderstandCaptionProvider(runner=runner, repo_root=repo_root)
    elif provider_id == "video_understand":
        provider = VideoUnderstandCaptionProvider(runner=runner, repo_root=repo_root)
    else:
        raise ValueError(f"unsupported caption provider {provider_id!r}")
    result = provider.caption(item, config)
    return result, caption_sidecar_path(item, config, repo_root=repo_root)


def caption_sidecar_path(item: Mapping[str, Any], config: Mapping[str, Any], *, repo_root: Path = PACK_ROOT) -> Path:
    out_dir = config.get("out_dir")
    if out_dir is None:
        out_dir = _media_path(item, repo_root=repo_root).parent
    path = Path(str(out_dir)).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    return path.resolve() / f"{_clip_id(item)}.caption.json"


def _write_sidecar(path: Path, result: CaptionResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_result_to_dict(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _result_to_dict(result: CaptionResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": result.text,
        "schema_version": result.schema_version,
        "confidence": result.confidence,
        "model": result.model,
    }
    if result.raw_response is not None:
        payload["raw_response"] = result.raw_response
    return payload


def _load_runner_output(sidecar: Path, stdout: str) -> dict[str, Any]:
    if sidecar.is_file():
        return json.loads(sidecar.read_text(encoding="utf-8"))
    if stdout.strip():
        return json.loads(stdout)
    return {}


def _caption_from_raw(raw: Any, *, provider_id: str, fallback_model: str) -> CaptionResult:
    if isinstance(raw, Mapping) and isinstance(raw.get("text"), str):
        return CaptionResult(
            text=str(raw["text"]),
            schema_version=int(raw.get("schema_version", 1)),
            confidence=float(raw.get("confidence", 0.0)),
            model=str(raw.get("model") or fallback_model),
            raw_response=dict(raw.get("raw_response") or raw),
        )
    answer, model = _first_answer(raw)
    text = _answer_to_text(answer)
    return CaptionResult(
        text=text,
        schema_version=1,
        confidence=0.0,
        model=model or fallback_model,
        raw_response=raw if isinstance(raw, dict) else {"provider": provider_id, "answer": answer},
    )


def _first_answer(raw: Any) -> tuple[Any, str]:
    if not isinstance(raw, Mapping):
        return raw, ""
    results = raw.get("results")
    if isinstance(results, list):
        for result in results:
            if isinstance(result, Mapping) and result.get("status") == "ok":
                return result.get("answer", ""), str(result.get("model") or "")
        for result in results:
            if isinstance(result, Mapping):
                return result.get("answer", result), str(result.get("model") or "")
    for key in ("answer", "caption", "summary", "output_text"):
        if key in raw:
            return raw[key], str(raw.get("model") or "")
    return raw, str(raw.get("model") or "")


def _answer_to_text(answer: Any) -> str:
    if isinstance(answer, str):
        return answer.strip()
    if isinstance(answer, Mapping):
        for key in ("caption", "summary", "text", "visual_read"):
            value = answer.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(answer, sort_keys=True, separators=(",", ":"))
    return str(answer).strip()


def _fixture_mode(config: Mapping[str, Any]) -> bool:
    return bool(config.get("fixture_mode") or config.get("mode") == "fixture")


def _cache_relevant_config(config: Mapping[str, Any]) -> dict[str, Any]:
    ignored = {
        "artifact_helpers",
        "budget_tracker",
        "fixture_caption_dir",
        "fixture_captions",
        "fixture_dir",
        "fixture_mode",
        "mode",
        "out_dir",
        "clock",
        "sleep",
    }
    return {str(key): value for key, value in config.items() if str(key) not in ignored}


def _prebaked_caption_path(item: Mapping[str, Any], config: Mapping[str, Any], clip_id: str, *, repo_root: Path) -> Path | None:
    caption_file = item.get("caption_file") or config.get("caption_file")
    if isinstance(caption_file, str):
        path = Path(caption_file).expanduser()
        return path if path.is_absolute() else (repo_root / path).resolve()
    fixture_dir = config.get("fixture_caption_dir") or config.get("fixture_dir")
    if isinstance(fixture_dir, str):
        path = Path(fixture_dir).expanduser()
        root = path if path.is_absolute() else (repo_root / path).resolve()
        return root / f"{clip_id}.caption.json"
    return None


def _clip_id(item: Mapping[str, Any]) -> str:
    for key in ("clip_id", "item_id", "source_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return deterministic_id(item.get("media_path", ""), item.get("content_hash", ""), prefix="clip")


def _media_path(item: Mapping[str, Any], *, repo_root: Path) -> Path:
    value = item.get("media_path")
    if not isinstance(value, str) or not value:
        raise ValueError("candidate item missing media_path")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (repo_root / path).resolve()


def _sample_time(item: Mapping[str, Any]) -> str:
    start = float(item.get("clip_start_s") or 0.0)
    if isinstance(item.get("duration_s"), (int, float)):
        return f"{max(0.0, float(item['duration_s']) / 2.0):.3f}"
    end = item.get("clip_end_s")
    if isinstance(end, (int, float)) and float(end) > start:
        return f"{start + ((float(end) - start) / 2.0):.3f}"
    return "0.000"


def sidecar_repo_path(path: Path, *, repo_root: Path = PACK_ROOT) -> str:
    return repo_relative_path(path, repo_root=repo_root)
