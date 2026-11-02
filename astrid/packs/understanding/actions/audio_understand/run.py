#!/usr/bin/env python3
"""Query an audio-native model against source audio windows."""


from __future__ import annotations

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import AstridError
from astrid.pack_api import guard_canonical_entrypoint, run_pack_main
from astrid.packs.understanding.shared._common import emit_dry_run_preview

guard_canonical_entrypoint('understanding.audio_understand')
import argparse
import base64
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from astrid.pack_api import add_choice_arg
from astrid.pack_api import ffprobe_duration_seconds
from astrid.pack_api import CredentialsScope

API_URL = "https://api.openai.com/v1/chat/completions"
MODEL_PRESETS = {
    "fast": "gpt-audio-mini",
    "best": "gpt-audio",
}
DEFAULT_MODE = "fast"
DEFAULT_QUERY = """Listen to this audio as editorial evidence, not just words.

Return compact JSON with:
- summary: what happens in the audio
- transcript_hint: short quote/paraphrase only when speech is clear
- emotion: speaker/audience emotional state
- energy: 0-10
- confidence: 0-10
- pacing: slow/steady/fast/chaotic
- music_or_sfx: notable music, sound effects, applause, laughter, room tone, noise
- production_quality: clipping, echo, hum, intelligibility, bad cuts
- edit_value: why this moment is or is not useful in a cut
- highlight_score: 0-10
- boundary_notes: suggested clean in/out points relative to this window
- cautions: uncertainty, ambiguity, or things a transcript would miss
"""
DEFAULT_COMPARE_QUERY = """Listen to the numbered audition reel. Each candidate is introduced by a spoken number. Ignore the spoken number labels themselves; evaluate only the candidate audio that follows each label.

Compare the candidates as audio performances, not just transcript text. Return compact JSON with:
- ranking: ordered candidate numbers with one-sentence reason
- best_candidate: number and why
- emotional_intensity: per candidate, 0-10
- excitement: per candidate, 0-10
- clarity: per candidate, 0-10
- speaking_pace: per candidate, too_slow/steady/fast/too_fast
- edit_value: per candidate, how useful it is in a cut
- boundary_notes: clean in/out notes if a candidate has dead air, clipped starts, or trails off
- cautions: ambiguity or audio details a transcript alone would miss
"""


from astrid.pack_api import pack_die


def _die(message: str) -> None:
    pack_die(message, recovery_command="fix the audio_understand inputs and rerun the command")


def load_api_key(*, env_file: Path | None = None) -> str:
    return CredentialsScope.get_local("openai", env_file=env_file)


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=kwargs.pop("check", True),
        capture_output=kwargs.pop("capture_output", True),
        text=kwargs.pop("text", True),
        **kwargs,
    )


def _has_cmd(name: str) -> bool:
    return shutil.which(name) is not None


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


def _window_plan(args: argparse.Namespace, duration_sec: float, *, base_index: int = 0, source_label: str = "") -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    if args.start is not None or args.end is not None:
        start = 0.0 if args.start is None else _parse_timestamp(args.start)
        end = duration_sec if args.end is None else _parse_timestamp(args.end)
        if end <= start:
            _die("--end must be after --start")
        windows.append({"index": base_index + 1, "start": max(0.0, start), "end": min(duration_sec, end), "label": source_label or "range"})
    for seconds in _parse_times(args.at):
        half = args.window_sec / 2.0
        start = max(0.0, seconds - half)
        end = min(duration_sec, seconds + half)
        if end > start:
            windows.append({"index": base_index + len(windows) + 1, "start": start, "end": end, "label": f"{source_label} around {_format_time(seconds)}".strip()})
    if not windows:
        start = 0.0
        while start < duration_sec - 1e-6 and len(windows) < args.max_chunks:
            end = min(duration_sec, start + args.chunk_sec)
            windows.append({"index": base_index + len(windows) + 1, "start": start, "end": end, "label": source_label or "auto"})
            start = end
    if len(windows) > args.max_chunks:
        _die(f"too many audio windows: {len(windows)} > {args.max_chunks}")
    return [
        {
            **window,
            "start": round(float(window["start"]), 3),
            "end": round(float(window["end"]), 3),
            "duration": round(float(window["end"]) - float(window["start"]), 3),
        }
        for window in windows
    ]


def _extract_window(source: Path, window: dict[str, Any], out_dir: Path, *, force: bool, sample_rate: int) -> Path:
    chunks_dir = out_dir / "audio-windows"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    start_ms = int(float(window["start"]) * 1000)
    end_ms = int(float(window["end"]) * 1000)
    path = chunks_dir / f"window_{int(window['index']):03d}_{start_ms:09d}_{end_ms:09d}.wav"
    if path.exists() and not force:
        return path
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
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )
    return path


def _extract_whole_clip(source: Path, index: int, out_dir: Path, *, force: bool, sample_rate: int, max_clip_sec: float | None) -> dict[str, Any]:
    duration = ffprobe_duration_seconds(source, runner=_run)
    end = duration if max_clip_sec is None else min(duration, max_clip_sec)
    window = {
        "index": index,
        "start": 0.0,
        "end": round(end, 3),
        "duration": round(end, 3),
        "label": source.stem,
    }
    path = _extract_window(source, window, out_dir, force=force, sample_rate=sample_rate)
    return {**window, "path": str(path), "source": str(source), "source_duration_sec": round(duration, 3)}


def _make_label_audio(text: str, path: Path, *, force: bool, sample_rate: int) -> None:
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if _has_cmd("say"):
        tmp = path.with_suffix(".aiff")
        _run(["say", "-o", str(tmp), text])
        _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(tmp),
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                str(path),
            ]
        )
        tmp.unlink(missing_ok=True)
        return
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=0.35",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )


def _make_silence(path: Path, *, duration: float, force: bool, sample_rate: int) -> None:
    if path.exists() and not force:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=channel_layout=mono:sample_rate={sample_rate}",
            "-t",
            f"{duration:.3f}",
            "-c:a",
            "pcm_s16le",
            str(path),
        ]
    )


def _build_audition_reel(windows: list[dict[str, Any]], out_dir: Path, *, force: bool, sample_rate: int, gap_sec: float) -> Path:
    reel_dir = out_dir / "audition-reel"
    parts_dir = reel_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    concat_path = reel_dir / "concat.txt"
    reel_path = reel_dir / "numbered_audition_reel.wav"
    lines: list[str] = []
    for window in windows:
        index = int(window["index"])
        label = parts_dir / f"label_{index:03d}.wav"
        label_gap = parts_dir / f"label_gap_{index:03d}.wav"
        gap = parts_dir / f"gap_{index:03d}.wav"
        _make_label_audio(f"Number {index}", label, force=force, sample_rate=sample_rate)
        _make_silence(label_gap, duration=0.2, force=force, sample_rate=sample_rate)
        _make_silence(gap, duration=gap_sec, force=force, sample_rate=sample_rate)
        lines.extend([f"file '{label}'", f"file '{label_gap}'", f"file '{Path(window['path'])}'", f"file '{gap}'"])
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if reel_path.exists() and not force:
        return reel_path
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-c:a",
            "pcm_s16le",
            str(reel_path),
        ]
    )
    return reel_path


def _audio_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _message_text(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                chunks.append(item["text"])
        return "\n".join(chunks).strip()
    return ""


def _call_audio_model(
    *,
    api_key: str,
    model: str,
    query: str,
    audio_path: Path,
    max_tokens: int,
    timeout: int,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "modalities": ["text"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": query},
                    {"type": "input_audio", "input_audio": {"data": _audio_b64(audio_path), "format": "wav"}},
                ],
            }
        ],
        "max_tokens": max_tokens,
    }
    request = Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AstridError(
            f"OpenAI API error {exc.code}: {detail}",
            recovery_command="verify your OpenAI API key is valid and the model name is correct, then retry",
        ) from exc
    except URLError as exc:
        raise AstridError(
            f"Network error: {exc}",
            recovery_command="check your network connection and verify the API endpoint is reachable, then retry",
        ) from exc


def run(args: argparse.Namespace) -> int:
    audio_sources = [path.expanduser() for path in (args.audio or [])]
    video_source = args.video.expanduser() if args.video else None
    if not audio_sources and video_source is None:
        _die("provide --audio or --video")
    if audio_sources and video_source is not None:
        _die("provide repeated --audio clips or one --video, not both")
    for source in audio_sources:
        if not source.is_file():
            _die(f"audio not found: {source}")
    if video_source is not None and not video_source.is_file():
        _die(f"video not found: {video_source}")
    if args.max_chunks < 1:
        _die("--max-chunks must be >= 1")
    if args.chunk_sec <= 0 or args.window_sec <= 0:
        _die("--chunk-sec and --window-sec must be > 0")
    if args.max_clip_sec < 0:
        _die("--max-clip-sec must be >= 0")
    if args.reel_gap_sec < 0:
        _die("--reel-gap-sec must be >= 0")

    out_dir = args.out_dir.expanduser()
    extracted = []
    if audio_sources:
        max_clip_sec = None if args.max_clip_sec <= 0 else args.max_clip_sec
        for source in audio_sources:
            extracted.append(
                _extract_whole_clip(
                    source,
                    len(extracted) + 1,
                    out_dir,
                    force=args.force,
                    sample_rate=args.sample_rate,
                    max_clip_sec=max_clip_sec,
                )
            )
        duration_sec = sum(float(item["duration"]) for item in extracted)
    else:
        assert video_source is not None
        duration_sec = ffprobe_duration_seconds(video_source, runner=_run)
        windows = _window_plan(args, duration_sec)
        for window in windows:
            path = _extract_window(video_source, window, out_dir, force=args.force, sample_rate=args.sample_rate)
            extracted.append({**window, "path": str(path), "source": str(video_source)})

    primary_model = args.model or MODEL_PRESETS[args.mode]
    models = [primary_model, *args.compare_model]
    use_reel = args.audition_reel == "always" or (args.audition_reel == "auto" and len(extracted) > 1)
    audio_inputs = [{"index": item["index"], "path": item["path"], "kind": "window"} for item in extracted]
    reel_path: Path | None = None
    query = args.query
    if use_reel:
        reel_path = _build_audition_reel(extracted, out_dir, force=args.force, sample_rate=args.sample_rate, gap_sec=args.reel_gap_sec)
        audio_inputs = [{"index": 1, "path": str(reel_path), "kind": "numbered_audition_reel"}]
        if query == DEFAULT_QUERY:
            query = DEFAULT_COMPARE_QUERY
        else:
            query = "This is a numbered audition reel. Ignore the spoken number labels themselves; evaluate only the candidate audio that follows each label.\n\n" + query
    preview = {
        "endpoint": API_URL,
        "models": models,
        "source": str(video_source) if video_source is not None else [str(path) for path in audio_sources],
        "source_kind": "video" if video_source is not None else "audio",
        "duration_sec": round(duration_sec, 3),
        "query": query,
        "windows": extracted,
        "audio_inputs": audio_inputs,
        "audition_reel": str(reel_path) if reel_path is not None else None,
        "philosophy": "Direct audio understanding is treated as listening evidence: tone, timing, room feel, sound design, and production quality are first-class signals, while transcript text remains a separate factual layer.",
    }
    if args.dry_run:
        return emit_dry_run_preview(preview, "understanding.audio_understand")

    api_key = load_api_key(env_file=args.env_file)
    results: list[dict[str, Any]] = []
    for model in models:
        for audio_input in audio_inputs:
            audio_path = Path(audio_input["path"])
            started = time.time()
            try:
                response = _call_audio_model(
                    api_key=api_key,
                    model=model,
                    query=query,
                    audio_path=audio_path,
                    max_tokens=args.max_tokens,
                    timeout=args.timeout,
                )
                result = {
                    "model": model,
                    "audio_input": audio_input,
                    "status": "ok",
                    "elapsed_sec": round(time.time() - started, 2),
                    "answer": _message_text(response),
                    "usage": response.get("usage"),
                    "response_id": response.get("id"),
                }
            except Exception as exc:
                result = {
                    "model": model,
                    "window_index": window["index"],
                    "status": "error",
                    "elapsed_sec": round(time.time() - started, 2),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            results.append(result)

    # --- universal result manifest -----------------------
    manifest_outputs: list[dict[str, Any]] = []
    for item in extracted:
        window_path = Path(item["path"])
        if window_path.exists():
            manifest_outputs.append({"path": str(window_path), "type": "file"})
    if reel_path is not None and Path(reel_path).exists():
        manifest_outputs.append({"path": str(reel_path), "type": "file"})
    if args.out:
        manifest_outputs.append({"path": str(args.out), "type": "file"})

    manifest_path = (args.out.parent / "manifest.json") if args.out else (out_dir / "manifest.json")
    output = {**preview, "results": results}
    output["schema_version"] = 1
    output["kind"] = "understanding.audio_understand"
    output["manifest_path"] = str(manifest_path)
    text = json.dumps(output, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")

    manifest = build_manifest(
        kind="understanding.audio_understand",
        inputs={
            "audio": [str(p) for p in audio_sources],
            "video": str(video_source) if video_source is not None else None,
            "query": query,
            "mode": args.mode,
            "model": args.model or MODEL_PRESETS[args.mode],
            "compare_model": args.compare_model,
            "at": args.at,
            "start": args.start,
            "end": args.end,
            "window_sec": args.window_sec,
            "chunk_sec": args.chunk_sec,
            "max_chunks": args.max_chunks,
            "max_clip_sec": args.max_clip_sec,
            "audition_reel": args.audition_reel,
            "reel_gap_sec": args.reel_gap_sec,
            "sample_rate": args.sample_rate,
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
        description="Ask an audio-native OpenAI model about source audio windows.",
        epilog="Use this for tone, emotion, pacing, music/SFX, production quality, audience reaction, and editorial moment value. Use transcribe.py for exact words.",
    )
    add = parser.add_argument
    add("--query", default=DEFAULT_QUERY, help="Question/instruction for the model. Defaults to an editorial audio-understanding JSON rubric.")
    add("--audio", type=Path, action="append", help="Audio clip to inspect. Repeat to build a numbered comparison audition reel.")
    add("--video", type=Path, help="Video file whose audio track should be inspected.")
    add("--at", action="append", help="Center timestamp(s), comma-separated or repeated. Supports seconds, MM:SS, HH:MM:SS.")
    add("--start", help="Optional range start timestamp.")
    add("--end", help="Optional range end timestamp.")
    add("--window-sec", type=float, default=20.0, help="Window length around each --at timestamp.")
    add("--chunk-sec", type=float, default=30.0, help="Auto chunk length when --at/--start are omitted.")
    add("--max-chunks", type=int, default=12)
    add("--max-clip-sec", type=float, default=0.0, help="For repeated --audio comparison clips, trim each source to this many seconds. 0 keeps full clips.")
    add_choice_arg(parser, "--audition-reel", values=("auto", "always", "never"), default="auto", help="Build one numbered audio reel for comparative judging. Auto enables it for multiple clips/windows.")
    add("--reel-gap-sec", type=float, default=0.45, help="Silence after each audition reel candidate.")
    add("--sample-rate", type=int, default=16000)
    add_choice_arg(parser, "--mode", values=sorted(MODEL_PRESETS), default=DEFAULT_MODE, help="fast uses gpt-audio-mini; best uses gpt-audio.")
    add("--model", help="Explicit model override.")
    add("--compare-model", action="append", default=[], help="Additional audio model to query against the same windows.")
    add("--out-dir", type=Path, default=Path("runs/audio-understanding"))
    add("--out", type=Path, help="Optional JSON result path.")
    add("--env-file", type=Path)
    add("--max-tokens", type=int, default=900)
    add("--timeout", type=int, default=180)
    add("--force", action="store_true")
    add("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        return run(build_parser().parse_args(argv))

    return run_pack_main("understanding.audio_understand", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
