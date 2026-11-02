#!/usr/bin/env python3
"""Query a video-native Gemini model against source video windows."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('understanding.video_understand')
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import add_choice_arg
from astrid.pack_api import AstridError
from astrid.pack_api import ffprobe_duration_seconds
from astrid.core.util.llm_clients import build_gemini_client
from astrid.packs.understanding.shared._common import emit_dry_run_preview

MODEL_PRESETS = {
    "fast": "gemini-2.5-flash",
    "best": "gemini-2.5-pro",
}
DEFAULT_MODE = "fast"
DEFAULT_QUERY = """Watch this video as editorial evidence, using both picture and sound.

Return compact JSON with:
- summary: what happens in the clip
- visual_read: people, setting, framing, action, text, graphics, cuts, camera motion
- audio_read: speech delivery, music/SFX, applause/laughter, room tone, noise, sync issues
- edit_value: why this moment is or is not useful in a cut
- highlight_score: 0-10
- energy: 0-10
- pacing: slow/steady/fast/chaotic
- production_quality: visual/audio quality problems, bad cuts, focus/exposure, clipping, echo
- boundary_notes: suggested clean in/out points relative to this window
- cautions: uncertainty or details that need transcript/frame/audio follow-up
"""
RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "visual_read": {"type": "string"},
        "audio_read": {"type": "string"},
        "edit_value": {"type": "string"},
        "highlight_score": {"type": "number"},
        "energy": {"type": "number"},
        "pacing": {"type": "string"},
        "production_quality": {"type": "string"},
        "boundary_notes": {"type": "string"},
        "cautions": {"type": "string"},
    },
    "required": [
        "summary",
        "visual_read",
        "audio_read",
        "edit_value",
        "highlight_score",
        "energy",
        "pacing",
        "production_quality",
        "boundary_notes",
        "cautions",
    ],
}


from astrid.pack_api import pack_die


def _die(message: str) -> None:
    pack_die(message, recovery_command="check the input arguments and retry; use --help for usage")


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=kwargs.pop("check", True),
        capture_output=kwargs.pop("capture_output", True),
        text=kwargs.pop("text", True),
        **kwargs,
    )


def _parse_timestamp(value: str) -> float:
    raw = value.strip()
    if not raw:
        _die("empty timestamp")
    if ":" not in raw:
        return float(raw)
    parts = [float(part) for part in raw.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    _die(f"invalid timestamp: {value}")
    return 0.0


def _format_time(seconds: float) -> str:
    whole = int(seconds)
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _parse_times(values: list[str] | None) -> list[float]:
    times: list[float] = []
    for value in values or []:
        for part in value.split(","):
            if part.strip():
                times.append(_parse_timestamp(part))
    return times


def _window_plan(args: argparse.Namespace, duration_sec: float) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    if args.start is not None or args.end is not None:
        start = 0.0 if args.start is None else _parse_timestamp(args.start)
        end = duration_sec if args.end is None else _parse_timestamp(args.end)
        if end <= start:
            _die("--end must be after --start")
        windows.append({"index": 1, "start": max(0.0, start), "end": min(duration_sec, end), "label": "range"})
    for seconds in _parse_times(args.at):
        half = args.window_sec / 2.0
        start = max(0.0, seconds - half)
        end = min(duration_sec, seconds + half)
        if end > start:
            windows.append({"index": len(windows) + 1, "start": start, "end": end, "label": f"around {_format_time(seconds)}"})
    if not windows:
        start = 0.0
        while start < duration_sec - 1e-6 and len(windows) < args.max_chunks:
            end = min(duration_sec, start + args.chunk_sec)
            windows.append({"index": len(windows) + 1, "start": start, "end": end, "label": "auto"})
            start = end
    if len(windows) > args.max_chunks:
        _die(f"too many video windows: {len(windows)} > {args.max_chunks}")
    return [
        {
            **window,
            "start": round(float(window["start"]), 3),
            "end": round(float(window["end"]), 3),
            "duration": round(float(window["end"]) - float(window["start"]), 3),
        }
        for window in windows
    ]


def _extract_window(source: Path, window: dict[str, Any], out_dir: Path, *, force: bool, max_width: int) -> Path:
    clips_dir = out_dir / "video-windows"
    clips_dir.mkdir(parents=True, exist_ok=True)
    start_ms = int(float(window["start"]) * 1000)
    end_ms = int(float(window["end"]) * 1000)
    path = clips_dir / f"window_{int(window['index']):03d}_{start_ms:09d}_{end_ms:09d}.mp4"
    if path.exists() and not force:
        return path
    vf = f"scale='min({max_width},iw)':-2" if max_width > 0 else "scale=iw:ih"
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{float(window['start']):.3f}",
            "-to",
            f"{float(window['end']):.3f}",
            "-i",
            str(source),
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "26",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(path),
        ]
    )
    return path


def run(args: argparse.Namespace) -> int:
    video_source = args.video.expanduser()
    if not video_source.is_file():
        _die(f"video not found: {video_source}")
    if args.max_chunks < 1:
        _die("--max-chunks must be >= 1")
    if args.chunk_sec <= 0 or args.window_sec <= 0:
        _die("--chunk-sec and --window-sec must be > 0")
    if args.max_width < 0:
        _die("--max-width must be >= 0")

    out_dir = args.out_dir.expanduser()
    duration_sec = ffprobe_duration_seconds(video_source, runner=_run)
    windows = _window_plan(args, duration_sec)
    extracted = []
    for window in windows:
        path = _extract_window(video_source, window, out_dir, force=args.force, max_width=args.max_width)
        extracted.append({**window, "path": str(path), "source": str(video_source)})

    primary_model = args.model or MODEL_PRESETS[args.mode]
    models = [primary_model, *args.compare_model]
    preview = {
        "provider": "gemini",
        "models": models,
        "source": str(video_source),
        "source_kind": "video",
        "duration_sec": round(duration_sec, 3),
        "query": args.query,
        "windows": extracted,
        "philosophy": "Direct video understanding is treated as synchronized sight-and-sound evidence. Use visual_understand.py for cheap frame/contact-sheet reads, audio_understand.py for isolated listening judgment, and transcribe.py for exact words.",
    }
    if args.dry_run:
        return emit_dry_run_preview(preview, "understanding.video_understand")

    client = build_gemini_client(args.env_file)
    active_schema = RESPONSE_SCHEMA
    if getattr(args, "response_schema", None):
        schema_path = args.response_schema.expanduser()
        if not schema_path.is_file():
            raise AstridError(
                f"--response-schema file not found: {schema_path}",
                recovery_command="verify the --response-schema path points to an existing JSON schema file",
            )
        loaded = json.loads(schema_path.read_text(encoding="utf-8"))
        # Accept either a raw schema or {name, schema, strict?} wrapper (parallels visual_understand).
        active_schema = loaded.get("schema", loaded) if isinstance(loaded, dict) else loaded
        # Gemini's response_schema validator is strict — it rejects top-level keys
        # outside its allow-list (no $schema, $comment, additionalProperties, x_*, …).
        # Strip them to keep schemas that work cross-vendor.
        if isinstance(active_schema, dict):
            _GEMINI_TOP_KEYS = {
                "type", "properties", "required", "items", "enum", "description",
                "nullable", "format", "minimum", "maximum", "minItems", "maxItems",
                "minLength", "maxLength", "pattern", "anyOf", "oneOf", "allOf",
            }
            active_schema = {k: v for k, v in active_schema.items() if k in _GEMINI_TOP_KEYS}

    results: list[dict[str, Any]] = []
    for model in models:
        for window in extracted:
            video_path = Path(window["path"])
            prompt = (
                f"{args.query}\n\n"
                f"Clip label: {window['label']}. "
                f"Window index {window['index']} covers source-relative {window['start']}s to {window['end']}s. "
                "When giving boundary notes, describe offsets relative to this clip/window, not absolute source timestamps."
            )
            print(f"querying={model} window={window['index']} video={video_path}", file=sys.stderr)
            started = time.time()
            try:
                response = client.describe_video(
                    model=model,
                    video_path=video_path,
                    prompt=prompt,
                    response_schema=active_schema,
                )
                result = {
                    "model": model,
                    "window": window,
                    "status": "ok",
                    "elapsed_sec": round(time.time() - started, 2),
                    "answer": response,
                }
            except Exception as exc:
                result = {
                    "model": model,
                    "window": window,
                    "status": "error",
                    "elapsed_sec": round(time.time() - started, 2),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)

    output = {**preview, "results": results}

    # --- universal result manifest -----------------------
    manifest_path = ((args.out.parent / "manifest.json") if args.out else (out_dir / "manifest.json")).resolve()
    output["schema_version"] = 1
    output["kind"] = "understanding.video_understand"
    output["manifest_path"] = str(manifest_path)
    text = json.dumps(output, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote={args.out}", file=sys.stderr)

    # The receipt root is normally the host-assigned ``{out}`` directory.
    # Standalone custom paths remain writable, but files outside that spool
    # are deliberately not claimed as harvestable outputs.
    staging_root = out_dir.expanduser().resolve()
    receipt_path = manifest_path.expanduser().resolve()
    receipt_root = receipt_path.parent
    manifest_outputs: list[dict[str, Any]] = []
    declared_paths: set[Path] = set()

    def add_file_output(
        name: str,
        path: Path,
        *,
        artifact_type: str,
        role: str = "auxiliary",
        is_primary: bool = False,
    ) -> None:
        candidate = path.expanduser()
        if not candidate.is_file():
            return
        resolved = candidate.resolve()
        if resolved == receipt_path or resolved in declared_paths:
            return
        try:
            resolved.relative_to(staging_root)
            relative = resolved.relative_to(receipt_root).as_posix()
        except ValueError:
            return
        manifest_outputs.append(
            {
                "name": name,
                "output_port": name,
                "path": relative,
                "type": "file",
                "artifact_type": artifact_type,
                "ordinal": len(manifest_outputs),
                "role": role,
                "is_primary": is_primary,
            }
        )
        declared_paths.add(resolved)

    if args.out:
        add_file_output(
            "result",
            args.out,
            artifact_type="understanding/video",
            role="result",
            is_primary=True,
        )
    for window in extracted:
        add_file_output(
            "windows",
            Path(window["path"]),
            artifact_type="understanding/video-windows",
            role="result",
        )

    manifest = build_manifest(
        kind="understanding.video_understand",
        inputs={
            "video": str(video_source),
            "query": args.query,
            "mode": args.mode,
            "model": args.model or MODEL_PRESETS[args.mode],
            "compare_model": args.compare_model,
            "at": args.at,
            "start": args.start,
            "end": args.end,
            "window_sec": args.window_sec,
            "chunk_sec": args.chunk_sec,
            "max_chunks": args.max_chunks,
            "max_width": args.max_width,
            "out_dir": str(out_dir),
        },
        outputs=manifest_outputs,
        created=datetime.now(timezone.utc).isoformat(),
        schema_version=1,
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    print(text)
    return 0 if all(result["status"] == "ok" for result in results) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ask a video-native Gemini model about source video windows.",
        epilog="Use this when the editorial question depends on synchronized picture and sound. Use visual_understand.py for cheap frame sheets and audio_understand.py for isolated delivery/sound judgment.",
    )
    add = parser.add_argument
    add("--query", default=DEFAULT_QUERY, help="Question/instruction for the model. Defaults to an editorial video-understanding JSON rubric.")
    add("--video", type=Path, required=True, help="Video file to inspect.")
    add("--at", action="append", help="Center timestamp(s), comma-separated or repeated. Supports seconds, MM:SS, HH:MM:SS.")
    add("--start", help="Optional range start timestamp.")
    add("--end", help="Optional range end timestamp.")
    add("--window-sec", type=float, default=20.0, help="Window length around each --at timestamp.")
    add("--chunk-sec", type=float, default=30.0, help="Auto chunk length when --at/--start are omitted.")
    add("--max-chunks", type=int, default=8)
    add("--max-width", type=int, default=960, help="Downscale extracted clips to this width before upload. 0 keeps source width.")
    add_choice_arg(parser, "--mode", values=sorted(MODEL_PRESETS), default=DEFAULT_MODE, help="fast uses Gemini Flash; best uses Gemini Pro.")
    add("--model", help="Explicit Gemini model override.")
    add("--compare-model", action="append", default=[], help="Additional Gemini model to query against the same windows.")
    add("--out-dir", type=Path, default=Path("runs/video-understanding"))
    add("--out", type=Path, help="Optional JSON result path.")
    add("--response-schema", type=Path,
        help="Optional path to a JSON schema file. When provided, replaces the default editorial RESPONSE_SCHEMA — the model is constrained to emit JSON matching this schema (Gemini response_schema). File may be a raw schema or {name, schema, strict?} (parallels visual_understand --response-schema).")
    add("--env-file", type=Path)
    add("--timeout", type=int, default=300, help="Reserved for parity with other understanding tools.")
    add("--force", action="store_true")
    add("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
