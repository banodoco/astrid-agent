#!/usr/bin/env python3

# The canonical-entrypoint guard intentionally runs before imports.
# ruff: noqa: E402

from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("rendering.render")


import argparse
import ast
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence

from astrid.core import timeline
from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.rendering.errors import RendererException
from astrid.core.rendering.contracts import RenderRequest, SCHEMA_VERSION
from astrid.core.rendering.delivery_audio import METADATA_KEY as DELIVERY_AUDIO_KEY, DeliveryAudioTarget
from astrid.core.rendering.output_policy import (
    DEFAULT_RENDER_OUTPUT_NAME,
    validate_output_basename,
)
from astrid.core.rendering.service import RenderService

# The Hype pipeline's default output file name.  The executor manifest exposes
# an ``output_name`` input defaulting to this sentinel; non-default names are
# validated as plain file names and flow through the same
# placeholder expansion and declared-output resolution as the default.
DEFAULT_OUTPUT_NAME = DEFAULT_RENDER_OUTPUT_NAME

_SERVICE: RenderService | None = None
_MAX_CLI_ERROR_CHARS = 3_500


def _report_progress(phase: str, percent: int) -> None:
    """Publish bounded phase progress for the generic Astrid host.

    The renderer is still a synchronous command, so this intentionally reports
    lifecycle checkpoints rather than pretending to know frame-level progress.
    Direct CLI callers are unaffected when the host has not supplied a path.
    """
    raw_path = os.environ.get("ASTRID_PROGRESS_PATH")
    if not raw_path:
        return
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps({"phase": phase, "percent": max(0, min(100, int(percent)))}, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _renderer_cli_error(exc: RendererException) -> str:
    """Keep structured renderer reasons actionable across the CLI boundary."""

    lines = [str(exc)]
    reasons = exc.error.details.get("reasons")
    if isinstance(reasons, list):
        for reason in reasons:
            text = str(reason).strip()
            if text:
                lines.append(f"reason: {text}")
    message = "\n".join(lines)
    if len(message) <= _MAX_CLI_ERROR_CHARS:
        return message
    return message[: _MAX_CLI_ERROR_CHARS - 30] + "\n…renderer detail truncated…"


def _default_service() -> RenderService:
    """Build (once) the backend-neutral service the facade delegates to.

    Renderer selection, invocation, validation, audio completion, finalization,
    and publication all happen inside :class:`RenderService`.
    """
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = RenderService()
    return _SERVICE


def validate_output_name(name: str) -> str:
    """Validate only portable-basename safety at the executor boundary.

    The shared RenderService policy owns the media suffix decision because it
    can inspect the timeline's alpha stamp and explicit render profile.
    """

    return validate_output_basename(name)


def _backend_config(
    *,
    project_dir: Path | None,
    composition_id: str,
    theme_path: Path | None,
    min_free_gb: float | None,
) -> dict[str, dict[str, Any]]:
    """Build explicit configuration for the canonical Remotion backend."""
    config: dict[str, dict[str, Any]] = {}
    remotion: dict[str, Any] = {}
    if project_dir is not None:
        remotion["project_dir"] = str(project_dir)
    if composition_id is not None:
        remotion["composition_id"] = composition_id
    if theme_path is not None:
        remotion["theme_path"] = str(theme_path)
    if min_free_gb is not None:
        remotion["min_free_gb"] = min_free_gb
    if remotion:
        config["rendering.remotion"] = remotion
    return config


def _parse_backend_config(value: str | None) -> dict[str, dict[str, Any]]:
    """Parse the ``--backend-config`` CLI payload (JSON or Python literal)."""
    if value is None or value == "":
        return {}
    text = str(value).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError) as exc:
            raise ValueError(
                f"--backend-config must be a JSON object keyed by qualified "
                f"backend id, got {value!r}"
            ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("--backend-config must be a JSON object keyed by qualified backend id")
    return {str(key): dict(item) for key, item in parsed.items() if item is not None}


def _parse_profile(value: str | Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """Parse the public render-profile input before request admission."""
    if value is None or value == "":
        return None
    if isinstance(value, Mapping):
        return dict(value)
    text = str(value).strip()
    # A normal profile is often a large inline JSON object.  Do not ask the
    # OS to stat that entire value as a pathname: macOS raises ENAMETOOLONG
    # before JSON parsing can happen.  Host handoffs are deliberately limited
    # to short, non-JSON path-like values.
    if len(text) < 4096 and not text.startswith(("{", "[")):
        try:
            handoff_path = Path(text).expanduser()
            if handoff_path.is_file():
                try:
                    parsed = json.loads(handoff_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ValueError("--profile host handoff is not valid JSON") from exc
                if not isinstance(parsed, Mapping):
                    raise ValueError("--profile host handoff must contain a JSON object")
                return dict(parsed)
        except OSError:
            pass
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError) as exc:
            raise ValueError("--profile must be a JSON object describing a render profile") from exc
    if isinstance(parsed, str) and len(parsed) < 4096:
        try:
            nested_path = Path(parsed).expanduser()
            if nested_path.is_file():
                parsed = json.loads(nested_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    if not isinstance(parsed, Mapping):
        raise ValueError("--profile must be a JSON object describing a render profile")
    return dict(parsed)


def _rewrite_provenance_output_path(
    output: Path,
    *,
    timeline_authority: Mapping[str, Any] | None = None,
) -> None:
    """Stamp canonical authority without rewriting output locators.

    Output bytes are published by the neutral runtime. The render pack keeps
    its attempt-local path only as ephemeral execution evidence; it never
    derives or replaces it with a local CAS/project locator.
    """
    if os.environ.get("ASTRID_INTERNAL_INVOCATION") != "1":
        return
    sidecar = Path(f"{output}.provenance.json")
    if not output.is_file() or not sidecar.is_file():
        if timeline_authority is not None:
            raise RuntimeError(
                "canonical timeline render did not produce the provenance sidecar "
                "required to stamp its pinned kernel authority"
            )
        return
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("render provenance must be a JSON object")
        if timeline_authority is not None:
            payload["canonical_timeline"] = dict(timeline_authority)
        sidecar.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        if timeline_authority is not None:
            raise RuntimeError(
                "canonical timeline render could not stamp its pinned kernel authority "
                "into provenance"
            ) from exc
        # The sidecar remains governed by the renderer's own validation; this
        # additive locator rewrite must never hide a successful render or
        # turn an otherwise useful artifact into a kernel failure.
        return


def _write_empty_asset_registry(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timeline.save_registry({"assets": {}}, path)


def _write_render_manifest(output: Path, *, timeline_path: Path, selector: str) -> None:
    """Write the universal receipt consumed by the host output harvester.

    ``rendering.render`` declares media output ports, so the host requires a
    receipt rather than guessing output identity from filenames.  The
    provenance sidecar is included when the selected backend produced it;
    lightweight/fake renderers that only produce the required video remain
    valid and are harvested as a single result.
    """
    if not output.is_file():
        # RenderService normally guarantees this invariant.  Leave the
        # missing receipt for the host to report as a missing concrete output,
        # preserving the renderer's existing error boundary for malformed
        # service results and test doubles.
        return
    outputs: list[dict[str, Any]] = [
        {
            "name": "video",
            "path": output.name,
            "type": "file",
            "artifact_type": "clip/visual",
            "role": "result",
            "is_primary": True,
        }
    ]
    provenance = Path(f"{output}.provenance.json")
    if provenance.is_file():
        outputs.append(
            {
                "name": "provenance",
                "path": provenance.name,
                "type": "file",
                "artifact_type": "metadata/provenance",
                "role": "auxiliary",
                "is_primary": False,
            }
        )
    manifest = build_manifest(
        kind="rendering.render",
        inputs={
            "timeline": str(Path(timeline_path).expanduser().resolve()),
            "selector": selector,
            "output_name": output.name,
        },
        outputs=outputs,
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(output.parent / "manifest.json", manifest)


def _previous_render_outputs_for_timeline(
    out_path: Path,
    timeline_path: Path,
) -> tuple[Path, ...]:
    """Discover prior sibling outputs; publication validates before deleting.

    Filtering happens under each candidate's publication lock using the
    committed sidecar.
    """

    out_path = out_path.resolve()
    if out_path.name != "hype.mp4":
        return ()
    run_dir = out_path.parent
    runs_dir = run_dir.parent
    if runs_dir.name != "runs" or not runs_dir.is_dir():
        return ()
    candidates: list[Path] = []
    for candidate_run_dir in runs_dir.iterdir():
        if not candidate_run_dir.is_dir() or candidate_run_dir == run_dir:
            continue
        candidates.append(candidate_run_dir / out_path.name)
    return tuple(candidates)


def _parse_bool_arg(value: str | bool | None) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"expected boolean value, got {value!r}")


def _validate_media_dependency(assets_path: Path, dependency: Path | None) -> None:
    """Check one explicit dependency without changing legacy managed media.

    The typed file has already crossed host admission. Compare its actual CAS
    identity and size, since its path can differ from the managed registry copy.
    """
    registry = json.loads(Path(assets_path).read_text(encoding="utf-8"))
    assets = registry.get("assets") if isinstance(registry, Mapping) else None
    bound = isinstance(assets, Mapping) and any(
        isinstance(asset, Mapping) and asset.get("binding") == "media_dependency"
        for asset in assets.values())
    if dependency is None and not bound:
        return
    if dependency is None:
        raise ValueError("registry media_dependency binding requires the admitted media file")
    if not isinstance(assets, Mapping) or not assets:
        raise ValueError("media_dependency requires one registry media identity")

    def digest(value: Any) -> str:
        if type(value) is not str:
            raise ValueError("invalid registry media digest")
        result = value.removeprefix("sha256:")
        if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
            raise ValueError("invalid registry media digest")
        return result

    identities: set[tuple[str, int]] = set()
    for asset in assets.values():
        if not isinstance(asset, Mapping):
            raise ValueError("invalid registry media asset")
        object_values = [asset[key] for key in ("object_id", "media_id") if key in asset]
        digests = [digest(asset[key]) for key in ("digest", "content_sha256", "sha256", "hash") if key in asset]
        if (not object_values or not digests or any(type(value) is not str or not value.startswith("sha256:")
                                                  for value in object_values)):
            raise ValueError("media_dependency requires an exact registry CAS identity")
        object_digests = [digest(value) for value in object_values]
        if len(set((*object_digests, *digests))) != 1:
            raise ValueError("conflicting registry media identity aliases")
        size = asset.get("size")
        if type(size) is not int or size < 0:
            raise ValueError("media_dependency requires an exact registry byte size")
        identities.add((object_digests[0], size))
    if len(identities) != 1:
        raise ValueError("media_dependency registry has additional or conflicting media identities")
    expected_digest, expected_size = next(iter(identities))
    source = Path(dependency).expanduser()
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.getuid()
                or before.st_size != expected_size):
            raise ValueError("media_dependency is not an admitted regular file of the registry size")
        total, sha = 0, hashlib.sha256()
        while True:
            chunk = os.read(fd, min(1048576, expected_size - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > expected_size:
                raise ValueError("media_dependency exceeds registry byte size")
            sha.update(chunk)
        after = os.fstat(fd)
        current = os.stat(source, follow_symlinks=False)
        def identity(value: os.stat_result) -> tuple[int, ...]:
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if (identity(before) != identity(after) or identity(after) != identity(current)
                or total != expected_size or sha.hexdigest() != expected_digest):
            raise ValueError("media_dependency bytes do not match the registry identity")
    finally:
        os.close(fd)


def render(
    timeline_path: Path,
    assets_path: Path,
    out_path: Path,
    *,
    selector: str = "rendering.remotion",
    project_dir: Path | None = None,
    composition_id: str = "TimelineComposition",
    theme_path: Path | None = None,
    min_free_gb: float | None = None,
    keep_previous_renders: bool = False,
    review: bool = False,
    review_context: Mapping[str, Any] | None = None,
    backend_config: Mapping[str, Mapping[str, Any]] | None = None,
    profile: Mapping[str, Any] | None = None,
    timeline_authority: Mapping[str, Any] | None = None,
    compiled_audio: Mapping[str, Any] | None = None,
    materialized_root: Path | None = None,
    materialized_objects: Mapping[str, str] | None = None,
    media_dependency: Path | None = None,
    delivery_audio: Mapping[str, Any] | None = None,
) -> Path:
    """Render through :class:`RenderService` and publish one locked pair.

    Dispatch, support, invocation, validation, audio completion, finalization,
    and publication happen in the service; this facade only adapts files and
    output naming.
    """
    _validate_media_dependency(assets_path, media_dependency)
    _report_progress("admit", 0)
    out_path = Path(out_path)
    validate_output_name(out_path.name)
    previous_outputs = (
        ()
        if keep_previous_renders
        else _previous_render_outputs_for_timeline(out_path, timeline_path)
    )
    delivery_metadata: dict[str, str] = {}
    if delivery_audio is not None:
        target = DeliveryAudioTarget.from_mapping(delivery_audio)
        delivery_metadata[DELIVERY_AUDIO_KEY] = json.dumps(target.to_dict(), sort_keys=True)
    config = _backend_config(
        project_dir=project_dir,
        composition_id=composition_id,
        theme_path=theme_path,
        min_free_gb=min_free_gb,
    )
    for key, value in (backend_config or {}).items():
        if value is None:
            continue
        existing = config.get(str(key))
        if existing is None:
            config[str(key)] = dict(value)
        else:
            # Explicit caller configuration overlays the default Remotion
            # namespace so project/theme/composition values remain intact.
            overlaid = dict(existing)
            overlaid.update({k: v for k, v in value.items() if v is not None})
            config[str(key)] = overlaid
    request = RenderRequest.from_dict(
        {
            "schema_version": SCHEMA_VERSION,
            "timeline_path": str(Path(timeline_path).expanduser().resolve()),
            "assets_registry_path": str(Path(assets_path).expanduser().resolve()),
            "output_name": out_path.name,
            "window": None,
            "audio": None,
            "profile": profile,
            "backend_config": config,
            "metadata": {**({"review": json.dumps(dict(review_context or {"shots": []}), sort_keys=True)} if review else {}), **delivery_metadata},
            "materialized_root": (
                None
                if materialized_root is None
                else str(Path(materialized_root).expanduser().resolve())
            ),
            "compiled_audio": compiled_audio,
            "timeline_authority": timeline_authority,
            "materialized_objects": dict(materialized_objects or {}),
        }
    )
    _report_progress("prepare_inputs", 5)
    _report_progress("render", 10)
    output = _default_service().render(
        request,
        selector=selector,
        out_path=out_path,
        previous_outputs=previous_outputs,
    )
    _rewrite_provenance_output_path(
        Path(output),
        timeline_authority=timeline_authority,
    )
    _report_progress("validate_output", 90)
    _write_render_manifest(Path(output), timeline_path=timeline_path, selector=selector)
    _report_progress("complete", 100)
    return output


def _delivery_audio_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    """Combine --delivery-audio JSON with the --audio-target/--true-peak shorthand."""
    if args.delivery_audio is None and args.audio_target is None and args.true_peak is None:
        return None
    target: dict[str, Any] = {}
    if args.delivery_audio is not None:
        parsed = _parse_profile(args.delivery_audio)
        if not isinstance(parsed, Mapping):
            raise ValueError("--delivery-audio must be a JSON object")
        target.update(parsed)
    if args.audio_target is not None:
        target["lufs"] = args.audio_target
    if args.true_peak is not None:
        target["true_peak"] = args.true_peak
    DeliveryAudioTarget.from_mapping(target)  # reject bad values before admission
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeline", type=Path, required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--media-dependency", type=Path)
    parser.add_argument("--materialized-root", type=Path)
    parser.add_argument("--materialized-objects", default=None)
    parser.add_argument("--compiled-audio", default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--selector",
        default=None,
        help="Qualified renderer id (rendering.remotion, rendering.ffmpeg, or rendering.threejs).",
    )
    parser.add_argument(
        "--backend-config",
        default=None,
        help="JSON object keyed by qualified backend id with per-backend configuration.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="JSON object describing the requested render profile.",
    )
    parser.add_argument(
        "--timeline-authority",
        default=None,
        help="Kernel-resolved canonical timeline authority JSON (managed ref mode only).",
    )
    parser.add_argument(
        "--output-name",
        default=None,
        help=(
            "Plain output filename (default hype.mp4). An alpha-stamped timeline may "
            "request .mov for truthful ProRes 4444 output."
        ),
    )
    # Deployment supplies the server-owned Remotion project through
    # ASTRID_REMOTION_PROJECT_DIR. Keep the CLI unset by default so the
    # backend can resolve that supported runtime route instead of pinning the
    # source checkout's development-only remotion/ directory.
    parser.add_argument("--project-dir", type=Path, default=None)
    parser.add_argument("--composition", default="TimelineComposition")
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=None,
        help="Abort before rendering unless this much free disk is available near --out.",
    )
    parser.add_argument(
        "--keep-previous-renders",
        nargs="?",
        const=True,
        default=False,
        type=_parse_bool_arg,
        help="Preserve previous sibling hype.mp4 outputs for the same timeline.",
    )
    parser.add_argument(
        "--theme",
        type=Path,
        default=None,
        help=(
            "Absolute path to one runtime-materialized theme.json document "
            "(optional; the built-in default is used when omitted)."
        ),
    )
    parser.add_argument("--review", nargs="?", const=True, default=False, type=_parse_bool_arg)
    parser.add_argument("--review-context", default=None)
    parser.add_argument("--delivery-audio", default=None, help='Opt-in audio master JSON, e.g. {"lufs": -14, "true_peak": -1}; omitted means no mastering.')
    parser.add_argument("--audio-target", type=float, default=None, help="Opt-in integrated loudness target in LUFS.")
    parser.add_argument("--true-peak", type=float, default=None, help="Opt-in true-peak ceiling in dBTP.")
    args = parser.parse_args(argv)
    try:
        if args.output_name is not None:
            validate_output_name(args.output_name)
            if Path(args.out).name != args.output_name:
                raise ValueError(
                    f"--out basename {Path(args.out).name!r} does not match "
                    f"--output-name {args.output_name!r}"
                )
        else:
            validate_output_name(Path(args.out).name)
        selector = args.selector or "rendering.remotion"
        config = _parse_backend_config(args.backend_config)
        profile = _parse_profile(args.profile)
        timeline_authority = _parse_profile(args.timeline_authority)
        compiled_audio = _parse_profile(args.compiled_audio)
        materialized_objects = _parse_profile(args.materialized_objects)
        if materialized_objects is not None and not isinstance(materialized_objects, Mapping):
            raise ValueError("--materialized-objects must be a JSON object")
        delivery_audio = _delivery_audio_from_args(args)
        if args.assets is None:
            with TemporaryDirectory(prefix="astrid-render-assets-") as tmp_text:
                assets_path = Path(tmp_text) / "hype.assets.json"
                _write_empty_asset_registry(assets_path)
                output = render(
                    args.timeline,
                    assets_path,
                    args.out,
                    selector=selector,
                    project_dir=args.project_dir,
                    composition_id=args.composition,
                    theme_path=args.theme,
                    min_free_gb=args.min_free_gb,
                    keep_previous_renders=args.keep_previous_renders,
                    review=args.review,
                    review_context=_parse_profile(args.review_context),
                    backend_config=config,
                    profile=profile,
                    timeline_authority=timeline_authority,
                    compiled_audio=compiled_audio,
                    materialized_root=args.materialized_root,
                    materialized_objects=materialized_objects,
                    media_dependency=args.media_dependency,
                    delivery_audio=delivery_audio,
                )
        else:
            output = render(
                args.timeline,
                args.assets,
                args.out,
                selector=selector,
                project_dir=args.project_dir,
                composition_id=args.composition,
                theme_path=args.theme,
                min_free_gb=args.min_free_gb,
                keep_previous_renders=args.keep_previous_renders,
                review=args.review,
                review_context=_parse_profile(args.review_context),
                backend_config=config,
                profile=profile,
                timeline_authority=timeline_authority,
                compiled_audio=compiled_audio,
                materialized_root=args.materialized_root,
                materialized_objects=materialized_objects,
                media_dependency=args.media_dependency,
                delivery_audio=delivery_audio,
            )
    except RendererException as exc:  # pragma: no cover - CLI path
        print(_renderer_cli_error(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - CLI path
        print(str(exc), file=sys.stderr)
        # The kernel's in-process capability path needs the structured
        # exception to reach its handler boundary; otherwise a support
        # rejection is flattened into a bare return code and the SDK can only
        # report "executor failed".  Preserve the traditional CLI exit code
        # for external callers while retaining actionable typed failure text
        # for project-scoped SDK invocations.
        if os.environ.get("ASTRID_INTERNAL_INVOCATION") == "1":
            structured = getattr(exc, "error", None)
            details = getattr(structured, "details", None)
            reasons = details.get("reasons") if isinstance(details, Mapping) else None
            if isinstance(reasons, (list, tuple)) and reasons:
                raise RuntimeError(
                    f"{exc}: " + "; ".join(str(reason) for reason in reasons)
                ) from exc
            raise
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
