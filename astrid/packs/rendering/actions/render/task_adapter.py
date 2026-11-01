"""Pack-owned task adapter for the real timeline MP4 renderer.

The bridge's ``render_export`` task is admitted with a frozen timeline
snapshot.  This adapter turns that snapshot back into the two concrete input
files expected by the canonical ``rendering.render`` executor, invokes the
existing renderer, and returns one universal result manifest.  It deliberately
does not synthesize a placeholder video or fall back to the visualization
renderer: missing inputs and renderer failures remain typed task failures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import tempfile
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from astrid.core.env_vars import ASTRID_REMOTION_PROJECT_DIR
from astrid.core.execution.executor.registry import load_default_registry
from astrid.core.execution.executor.runner import ExecutorRunRequest, run_executor

PACK_ID = "rendering.render"
FAMILY = "render_export"
# The paired/local worker owns backend selection.  Ordinary media timelines
# use the canonical FFmpeg renderer; timelines containing non-media elements
# require the server-owned Remotion runtime.  This is fixed policy, never a
# task/browser parameter.
FFMPEG_SELECTOR = "rendering.ffmpeg"
REMOTION_SELECTOR = "rendering.remotion"
DEFAULT_OUTPUT_NAME = "render.mp4"
_MAX_DEADLINE_SECONDS = 60 * 60
_FORBIDDEN_CALLER_PARAMS = frozenset({"engine", "backend", "backend_config", "project_dir"})


class RenderExportRefused(RuntimeError):
    """Raised when an admitted render cannot be executed safely."""


@dataclass(frozen=True, slots=True)
class RenderExportExecutionContext:
    """Cooperative execution controls shared by local and HTTP workers.

    The renderer remains a synchronous library call, so cancellation and the
    deadline are checked at every adapter boundary and progress is emitted at
    bounded phase transitions.  A transport worker can use ``progress`` to
    heartbeat the live attempt without coupling this pack to HTTP.
    """

    deadline_at: float
    cancelled: Callable[[], bool] = lambda: False
    progress: Callable[[Mapping[str, Any]], None] = lambda _payload: None

    @classmethod
    def bounded(
        cls,
        *,
        deadline_seconds: float = 30 * 60,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> "RenderExportExecutionContext":
        if not isinstance(deadline_seconds, (int, float)) or isinstance(deadline_seconds, bool):
            raise RenderExportRefused("render_export deadline_seconds must be numeric")
        if deadline_seconds <= 0 or deadline_seconds > _MAX_DEADLINE_SECONDS:
            raise RenderExportRefused(
                f"render_export deadline_seconds must be in (0, {_MAX_DEADLINE_SECONDS}]"
            )
        return cls(
            deadline_at=time.monotonic() + float(deadline_seconds),
            cancelled=cancelled or (lambda: False),
            progress=progress or (lambda _payload: None),
        )

    def check(self, phase: str) -> None:
        if self.cancelled():
            raise RenderExportRefused(f"render_export cancelled during {phase}")
        if time.monotonic() >= self.deadline_at:
            raise RenderExportRefused(f"render_export deadline exceeded during {phase}")

    def report(self, phase: str, percent: int) -> None:
        self.check(phase)
        self.progress({"phase": phase, "percent": max(0, min(100, int(percent)))})


def _require_string(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RenderExportRefused(f"render_export {field} must be a non-empty string")
    return value.strip()


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _digest_value(value: object) -> str:
    if not isinstance(value, str):
        raise RenderExportRefused("render_export asset digest must be a string")
    digest = value.removeprefix("sha256:")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise RenderExportRefused("render_export asset digest is invalid")
    return digest


def _asset_extension(asset: Mapping[str, Any]) -> str:
    mime = asset.get("type") or asset.get("mime_type")
    guessed = mimetypes.guess_extension(str(mime)) if isinstance(mime, str) else None
    return guessed or ".bin"


def _stage_managed_registry(
    *,
    registry: Mapping[str, Any],
    materialized_objects: Mapping[str, str | Path | bytes],
    materialized_root: Path,
    inputs_dir: Path,
    context: RenderExportExecutionContext,
) -> Mapping[str, Any]:
    assets = registry.get("assets")
    if not isinstance(assets, Mapping):
        raise RenderExportRefused("render_export timeline registry assets are invalid")
    staged_assets = inputs_dir / "assets"
    staged_assets.mkdir(mode=0o700, exist_ok=True)
    rewritten: dict[str, Any] = {}
    for ordinal, (asset_key, raw_asset) in enumerate(assets.items()):
        context.report("stage_asset", min(25, 5 + ordinal))
        if not isinstance(asset_key, str) or not asset_key:
            raise RenderExportRefused("render_export asset key must be non-empty")
        if not isinstance(raw_asset, Mapping):
            raise RenderExportRefused(f"render_export asset {asset_key!r} is invalid")
        if any(key in raw_asset for key in ("file", "path", "url", "uri", "locator", "realm")):
            raise RenderExportRefused(
                f"render_export asset {asset_key!r} contains an unmanaged locator"
            )
        media_id = _require_string(raw_asset.get("media_id"), f"asset {asset_key} media_id")
        expected = _digest_value(raw_asset.get("content_sha256"))
        candidates = (media_id, expected, f"sha256:{expected}")
        value = next((materialized_objects[candidate] for candidate in candidates if candidate in materialized_objects), None)
        if value is None:
            raise RenderExportRefused(
                f"render_export runtime object {media_id!r} has no host materialization"
            )
        destination = staged_assets / f"{ordinal:04d}{_asset_extension(raw_asset)}"
        if isinstance(value, bytes):
            payload = value
            destination.write_bytes(payload)
            size = len(payload)
        else:
            source = Path(value).expanduser()
            if source.is_symlink():
                raise RenderExportRefused(f"render_export object {media_id!r} is a symlink")
            try:
                source = source.resolve(strict=True)
                source.relative_to(materialized_root.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise RenderExportRefused(
                    f"render_export object {media_id!r} is outside the host materialization root"
                ) from exc
            if not source.is_file():
                raise RenderExportRefused(f"render_export object {media_id!r} is not a regular file")
            shutil.copyfile(source, destination)
            size = source.stat().st_size
        staged_digest, staged_size = _digest_file(destination)
        if staged_digest != expected or staged_size != size:
            raise RenderExportRefused(
                f"render_export staged media {media_id!r} failed verification"
            )
        entry = {
            key: value
            for key, value in raw_asset.items()
            if key
            not in {
                "file",
                "url",
                "uri",
                "locator",
                "realm",
                # These are queue/managed-media provenance fields, not
                # renderer registry fields; FFmpeg/Remotion reject unknown
                # asset keys by design.
                "media_id",
                "content_sha256",
            }
        }
        entry.update(
            {
                "file": str(destination),
                # Preserve the runtime identity on the derived registry.  The
                # renderer consumes the private file, while support probing
                # still needs the object/digest pair to verify that this file
                # came from the host handoff rather than a caller locator.
                "media_id": media_id,
                "content_sha256": expected,
            }
        )
        rewritten[asset_key] = entry
    result = dict(registry)
    result["assets"] = rewritten
    return result


@contextmanager
def _scoped_env(key: str, value: str) -> Iterator[None]:
    previous = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _decode_spec(task: Any) -> tuple[str, Mapping[str, Any], Mapping[str, Any], dict[str, Any]]:
    spec = getattr(task, "spec", None)
    if not isinstance(spec, Mapping):
        raise RenderExportRefused("render_export task spec must be an object")
    if spec.get("family") not in (None, FAMILY):
        raise RenderExportRefused("render_export task spec family is incompatible")
    project_slug = _require_string(spec.get("project_slug"), "project_slug")
    snapshot = spec.get("timeline_snapshot")
    if not isinstance(snapshot, Mapping):
        raise RenderExportRefused("render_export task is missing its timeline snapshot")
    config = snapshot.get("config")
    registry = snapshot.get("registry")
    if not isinstance(config, Mapping):
        raise RenderExportRefused("render_export timeline snapshot config is invalid")
    if not isinstance(registry, Mapping) or not isinstance(registry.get("assets"), Mapping):
        raise RenderExportRefused("render_export timeline snapshot registry is invalid")
    params = spec.get("params")
    if not isinstance(params, Mapping):
        raise RenderExportRefused("render_export task params must be an object")
    return project_slug, config, registry, dict(params)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


class RenderExportTaskAdapter:
    """Execute one admitted render-export task through ``rendering.render``."""

    def execute(
        self,
        *,
        task: Any,
        staging_dir: Path,
        context: RenderExportExecutionContext | None = None,
    ) -> Mapping[str, Any]:
        context = context or RenderExportExecutionContext.bounded()
        context.report("admit", 0)
        project_slug, config, registry, params = _decode_spec(task)
        from astrid.core.rendering.remotion_runtime import (
            remotion_runtime_status,
            timeline_requires_remotion,
        )

        requires_remotion = timeline_requires_remotion(config)
        remotion_status = (
            remotion_runtime_status(require_explicit_project=True)
            if requires_remotion
            else None
        )
        if requires_remotion and not remotion_status.available:
            raise RenderExportRefused(
                "server-owned Remotion runtime unavailable: "
                + (remotion_status.reason or "unknown reason")
            )
        staging_dir = Path(staging_dir).resolve()
        staging_dir.mkdir(parents=True, exist_ok=True)
        inputs_dir = staging_dir / "render-inputs"
        inputs_dir.mkdir(mode=0o700, exist_ok=True)
        timeline_path = inputs_dir / "timeline.json"
        assets_path = inputs_dir / "assets.json"
        context.report("prepare_inputs", 5)
        forbidden = sorted(_FORBIDDEN_CALLER_PARAMS.intersection(params))
        if forbidden:
            raise RenderExportRefused(
                "render_export server-owned parameter(s) are not accepted: " + ", ".join(forbidden)
            )
        raw_materialized = params.get("materialized_objects")
        if not isinstance(raw_materialized, Mapping):
            raise RenderExportRefused(
                "render_export requires the host materialized_objects handoff"
            )
        timeline_ref = params.get("timeline_ref")
        if not isinstance(timeline_ref, str) or not timeline_ref.strip():
            raise RenderExportRefused(
                "render_export requires the canonical timeline_ref"
            )
        expected_version = params.get("expected_version")
        if expected_version is not None and (
            isinstance(expected_version, bool) or not isinstance(expected_version, int)
        ):
            raise RenderExportRefused(
                "render_export expected_version must be an integer"
            )
        materialized_root = (staging_dir / "managed-objects").resolve()
        if not materialized_root.is_dir():
            raise RenderExportRefused(
                "render_export host materialization root is missing"
            )
        staged_registry = _stage_managed_registry(
            registry=registry,
            materialized_objects=dict(raw_materialized),
            materialized_root=materialized_root,
            inputs_dir=inputs_dir,
            context=context,
        )
        _write_json(timeline_path, config)
        _write_json(assets_path, staged_registry)

        # The canonical executor consumes only attempt-local artifacts. Keep
        # the externally-visible staging pack unchanged, but provide
        # short-lived writable copies inside this same assigned attempt.
        owned_inputs_dir = Path(tempfile.mkdtemp(prefix=".render-inputs-", dir=staging_dir))
        try:
            owned_timeline_path = owned_inputs_dir / "timeline.json"
            owned_assets_path = owned_inputs_dir / "assets.json"
            shutil.copy2(timeline_path, owned_timeline_path)
            owned_assets = owned_inputs_dir / "assets"
            owned_assets.mkdir(mode=0o700)
            owned_registry = json.loads(assets_path.read_text(encoding="utf-8"))
            for asset in owned_registry.get("assets", {}).values():
                source = Path(asset["file"])
                destination = owned_assets / source.name
                # The renderer receives a writable private copy, never a hard
                # link to managed media or the durable staging evidence.
                shutil.copyfile(source, destination)
                asset["file"] = str(destination)
            _write_json(owned_assets_path, owned_registry)

            # The server owns renderer selection; a caller cannot downgrade or
            # inject backend configuration.  FFmpeg receives no Remotion
            # namespace or environment, so an unset Remotion project can never
            # become the accidental default for an ordinary media timeline.
            if "output_name" in params:
                raise RenderExportRefused("render_export uses output_filename")
            output_name = params.get("output_filename", DEFAULT_OUTPUT_NAME)
            if (
                not isinstance(output_name, str)
                or Path(output_name).name != output_name
                or not output_name.endswith(".mp4")
            ):
                raise RenderExportRefused(
                    "render_export output_filename must be a plain .mp4 filename"
                )
            output_path = staging_dir / output_name
            selector = REMOTION_SELECTOR if requires_remotion else FFMPEG_SELECTOR
            renderer_inputs: dict[str, Any] = {
                "timeline": str(owned_timeline_path),
                # The canonical executor manifest requires the immutable
                # runtime identity even though this task adapter has already
                # received the pinned snapshot.  Keep the identity on the
                # executor request so admission and provenance cannot silently
                # fall back to file-only mode.
                "timeline_ref": timeline_ref,
                "assets_registry": str(owned_assets_path),
                "output_name": output_name,
                "selector": selector,
                "keep_previous_renders": True,
                # Carry the host-owned handoff through the canonical executor
                # request.  The derived registry's file fields are valid only
                # when the renderer can verify them against this exact
                # attempt root; without it support probing would (correctly)
                # reject the private paths as retired caller locators.
                # The renderer receives a second, writable copy under the
                # attempt-owned input directory.  Its derived registry paths
                # must therefore be checked against that copy's root (the
                # original host root remains immutable evidence).
                "materialized_root": str(owned_inputs_dir),
                "materialized_objects": dict(raw_materialized),
            }
            if expected_version is not None:
                renderer_inputs["expected_version"] = expected_version
            render_env = nullcontext()
            if requires_remotion:
                assert remotion_status is not None and remotion_status.project_dir is not None
                renderer_inputs["backend_config"] = {
                    REMOTION_SELECTOR: {
                        # This is deployment configuration, never a task field.
                        "project_dir": str(remotion_status.project_dir),
                    }
                }
                render_env = _scoped_env(
                    ASTRID_REMOTION_PROJECT_DIR,
                    str(remotion_status.project_dir),
                )

            # Route through the canonical executor registry so this pack adapter
            # never imports the facade runtime directly. The executor manifest is
            # the ownership boundary for dispatch; this adapter supplies only the
            # immutable, server-owned inputs admitted above.
            context.report("render", 10)
            with _scoped_env("ASTRID_RENDER_INHERIT_PROCESS_GROUP", "1"), render_env:
                result = run_executor(
                    ExecutorRunRequest(
                        executor_id=PACK_ID,
                        out=staging_dir,
                        project=project_slug,
                        inputs=renderer_inputs,
                        project_was_auto_resolved=True,
                        invocation="render-export-task",
                    ),
                    load_default_registry(),
                )
            if not result.ok:
                detail = result.error.message if result.error is not None else result.payload
                raise RenderExportRefused(f"render_export executor failed: {detail}")
            # ``run_executor`` returns harvested output descriptors as a
            # sequence when the universal result receipt is present.  Keep
            # compatibility with older in-process doubles that returned a
            # mapping, but use the adapter's declared output path by default
            # so receipt harvesting never changes the render identity.
            rendered = output_path
            result_outputs = getattr(result, "outputs", ())
            if isinstance(result_outputs, Mapping):
                rendered = result_outputs.get("video", output_path)
            elif isinstance(result_outputs, (list, tuple)):
                for descriptor in result_outputs:
                    if not isinstance(descriptor, Mapping) or descriptor.get("name") != "video":
                        continue
                    candidate = descriptor.get("path")
                    if candidate:
                        rendered = candidate
                    break
        except RenderExportRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - typed by the task boundary
            raise RenderExportRefused(f"render_export renderer failed: {exc}") from exc
        finally:
            shutil.rmtree(owned_inputs_dir, ignore_errors=True)

        context.report("validate_output", 90)
        rendered_path = Path(rendered).resolve()
        if rendered_path != output_path.resolve() or not rendered_path.is_file():
            raise RenderExportRefused("render_export renderer did not produce its declared MP4")
        if rendered_path.stat().st_size <= 0:
            raise RenderExportRefused("render_export renderer produced an empty MP4")
        with rendered_path.open("rb") as handle:
            header = handle.read(8)
        if len(header) < 8 or header[4:8] != b"ftyp":
            raise RenderExportRefused(
                "render_export renderer produced bytes without an MP4 ftyp box"
            )

        digest, byte_size = _digest_file(rendered_path)
        context.report("complete", 100)
        return {
            "schema_version": 1,
            "kind": PACK_ID,
            "inputs": {
                "family": FAMILY,
                "project_slug": project_slug,
                "timeline_ref": params.get("timeline_ref"),
                "expected_version": params.get("expected_version"),
                "semantic_role": "render",
                "selector": selector,
            },
            "outputs": [
                {
                    "path": output_name,
                    "content_hash": f"sha256:{digest}",
                    "bytes": byte_size,
                    "ordinal": 0,
                    "is_primary": True,
                    "role": "result",
                    "label": "render",
                }
            ],
            "created": str(getattr(task, "created_at", "") or PACK_ID),
            "warnings": [],
        }


def execute_render_export_task(
    *,
    task: Any,
    staging_dir: Path,
    deadline_seconds: float = 30 * 60,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[Mapping[str, Any]], None] | None = None,
) -> Mapping[str, Any]:
    """Bounded callable harness entrypoint for a local bridge worker."""
    context = RenderExportExecutionContext.bounded(
        deadline_seconds=deadline_seconds,
        cancelled=cancelled,
        progress=progress,
    )
    return RenderExportTaskAdapter().execute(
        task=task, staging_dir=staging_dir, context=context
    )


def task_handler_for_capability(
    capability_id: str,
) -> RenderExportTaskAdapter:
    """Resolve the transport-neutral handler registered by this pack."""
    if capability_id != PACK_ID:
        raise RenderExportRefused(f"no render-export handler is registered for {capability_id!r}")
    return RenderExportTaskAdapter()


__all__ = [
    "RenderExportExecutionContext",
    "RenderExportRefused",
    "RenderExportTaskAdapter",
    "execute_render_export_task",
    "task_handler_for_capability",
]


def main(argv: list[str] | None = None) -> int:
    """Narrow child-process entrypoint used by transport workers.

    The parent owns fencing and termination; this command only transforms a
    frozen task JSON into files under the caller-provided staging directory.
    It never accepts renderer selection or a project checkout path.
    """
    parser = argparse.ArgumentParser(prog="astrid-render-export-child")
    parser.add_argument("--task-json", type=Path, required=True)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--deadline-seconds", type=float, default=30 * 60)
    args = parser.parse_args(argv)
    from types import SimpleNamespace

    task_data = json.loads(args.task_json.read_text(encoding="utf-8"))
    if not isinstance(task_data, Mapping):
        raise RenderExportRefused("render_export child task JSON must be an object")
    task = SimpleNamespace(
        id=str(task_data.get("id", "render-export-child")),
        created_at=str(task_data.get("created_at", "")),
        spec=task_data.get("spec"),
    )
    manifest = execute_render_export_task(
        task=task,
        staging_dir=args.staging_dir,
        deadline_seconds=args.deadline_seconds,
    )
    (args.staging_dir / "manifest.json").write_text(
        json.dumps(dict(manifest), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by worker subprocess
    raise SystemExit(main())
