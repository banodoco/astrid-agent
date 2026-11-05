#!/usr/bin/env python3
"""Transcribe source audio into transcript JSON, SRT, and text files with silence-aware chunking, optional diarization, and minimal CLI glue."""


from __future__ import annotations

import sys

from astrid.pack_api import AstridValueError
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.transcribe')
import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import AuditContext
from astrid.pack_api import add_choice_arg
from astrid.pack_api import AstridError
from astrid.pack_api import sha256_file
from astrid.pack_api import ffprobe_duration_seconds, require_runtime_materialized_file
from astrid.packs.editorial.shared._common import load_api_key

SILENCE_START_RE = re.compile(r"silence_start:\s*([0-9]+(?:\.[0-9]+)?)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)")
CUT_EPSILON_SEC = 0.05
HALLUCINATION_DENYLIST = {".", "Thanks for watching!", "Please subscribe!", "Thank you.", "Thank you so much.", "All right.", "Get out of here."}
PYANNOTE_PIPELINE = "pyannote/speaker-diarization-3.1"
def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=kwargs.pop("check", True),
        capture_output=kwargs.pop("capture_output", True),
        text=kwargs.pop("text", True),
        **kwargs,
    )
def srt_timestamp(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
def parse_asset_id(value: str) -> str:
    asset_id = value.strip()
    if not asset_id:
        raise argparse.ArgumentTypeError("must be a non-empty app media asset ID")
    return asset_id
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Transcribe a source audio file.")
    add = parser.add_argument
    add("--audio", type=str, required=True, help="Source audio file.")
    add("--out", type=Path, help="Output directory. Defaults to a sibling folder named after the audio stem.")
    add("--model", default="whisper-1", help="OpenAI transcription model.")
    add("--language", default="en", help="Language hint for the transcription model.")
    add("--asset-id", type=parse_asset_id, help="App media asset ID to associate with the transcript handoff.")
    add("--project", help="Runtime project for the shared media.transcript document (defaults to the task project).")
    add("--project-slug", help="Runtime supplied project for an admitted transcription task.")
    add("--cache-dir", type=Path, help="Cache directory. Defaults to <out>/cache.")
    add("--env-file", type=Path, help="Optional .env file checked before the repo-relative fallback.")
    add("--max-chunk-sec", type=float, default=600.0, help="Maximum chunk duration before a silence or hard cut.")
    add("--no-vad-gate", action="store_true", help="Disable the silent-chunk skip and denylist silence filter.")
    add_choice_arg(parser, "--diarize", values=("pyannote",), help="Optional diarization backend.")
    return parser
def resolve_dirs(audio_path: Path, out_dir: Path | None, cache_dir: Path | None) -> tuple[Path, Path]:
    out_path = out_dir.resolve() if out_dir else (audio_path.parent / audio_path.stem).resolve()
    return out_path, cache_dir.resolve() if cache_dir else out_path / "cache"
def probe_duration(media_path: Path) -> float:
    return ffprobe_duration_seconds(media_path, runner=run)
def parse_silence_windows(stderr: str, duration_sec: float) -> list[dict[str, float]]:
    windows: list[dict[str, float]] = []
    current_start: float | None = None
    for line in stderr.splitlines():
        if match := SILENCE_START_RE.search(line):
            current_start = float(match.group(1))
            continue
        if (match := SILENCE_END_RE.search(line)) and current_start is not None:
            end_sec = min(float(match.group(1)), duration_sec)
            if end_sec > current_start:
                windows.append({"start": round(current_start, 6), "end": round(end_sec, 6)})
            current_start = None
    if current_start is not None and duration_sec > current_start:
        windows.append({"start": round(current_start, 6), "end": round(duration_sec, 6)})
    return windows
def detect_silence_windows(audio_path: Path, duration_sec: float) -> list[dict[str, float]]:
    return parse_silence_windows(
        run(
            [
                "ffmpeg",
                "-i",
                str(audio_path),
                "-af",
                "silencedetect=n=-35dB:d=0.6",
                "-f",
                "null",
                "-",
            ]
        ).stderr,
        duration_sec,
    )
def silence_overlap_seconds(start_sec: float, end_sec: float, windows: list[dict[str, float]]) -> float:
    return sum(max(0.0, min(end_sec, w["end"]) - max(start_sec, w["start"])) for w in windows)
def chunk_silence_ratio(chunk: dict[str, Any], windows: list[dict[str, float]]) -> float:
    duration_sec = max(float(chunk["duration_sec"]), 0.0)
    return 1.0 if duration_sec <= 0 else silence_overlap_seconds(float(chunk["start_sec"]), float(chunk["end_sec"]), windows) / duration_sec
def segment_is_within_silence(segment: dict[str, Any], chunk_offset_sec: float, windows: list[dict[str, float]]) -> bool:
    start_sec = chunk_offset_sec + float(segment.get("start", 0.0))
    end_sec = chunk_offset_sec + float(segment.get("end", 0.0))
    duration_sec = max(0.0, end_sec - start_sec)
    if duration_sec <= 0:
        return False
    overlap = silence_overlap_seconds(start_sec, end_sec, windows)
    return overlap >= max(duration_sec - CUT_EPSILON_SEC, duration_sec * 0.95)
def filter_hallucinated_segments(segments: list[dict[str, Any]], chunk_offset_sec: float, windows: list[dict[str, float]], guard_enabled: bool) -> tuple[list[dict[str, Any]], int]:
    if not guard_enabled:
        return segments, 0
    kept, filtered = [], 0
    for segment in segments:
        if str(segment.get("text", "")).strip() in HALLUCINATION_DENYLIST and segment_is_within_silence(segment, chunk_offset_sec, windows):
            filtered += 1
            continue
        kept.append(segment)
    return kept, filtered
def choose_cut_point(start_sec: float, duration_sec: float, windows: list[dict[str, float]], max_chunk_sec: float) -> tuple[float, str]:
    hard_cut = min(start_sec + max_chunk_sec, duration_sec)
    for window in windows:
        if window["end"] > start_sec + CUT_EPSILON_SEC and not (window["start"] - CUT_EPSILON_SEC <= start_sec <= window["end"] + CUT_EPSILON_SEC) and window["start"] - CUT_EPSILON_SEC <= hard_cut <= window["end"] + CUT_EPSILON_SEC:
            return round(hard_cut, 6), "silence"
    candidates = [(w["start"] + w["end"]) / 2.0 for w in windows if start_sec + CUT_EPSILON_SEC < (w["start"] + w["end"]) / 2.0 <= hard_cut + CUT_EPSILON_SEC]
    return (round(candidates[-1], 6), "silence") if candidates else (round(hard_cut, 6), "hard")
def build_chunk_plan(duration_sec: float, windows: list[dict[str, float]], max_chunk_sec: float, suffix: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    start_sec, index = 0.0, 1
    while start_sec < duration_sec - 1e-6:
        end_sec, cut_kind = choose_cut_point(start_sec, duration_sec, windows, max_chunk_sec)
        if end_sec <= start_sec:
            end_sec, cut_kind = min(duration_sec, start_sec + max_chunk_sec), "hard"
        chunks.append({"index": index, "offset_sec": round(start_sec, 6), "start_sec": round(start_sec, 6), "end_sec": round(end_sec, 6), "duration_sec": round(end_sec - start_sec, 6), "cut_kind": cut_kind, "filename": f"chunk_{index:03d}{suffix}"})
        start_sec, index = end_sec, index + 1
    return chunks
def slice_chunks(audio_path: Path, cache_dir: Path, chunks: list[dict[str, Any]]) -> None:
    # `-vn -c:a copy` strips any video stream so Whisper only receives audio.
    # Without this, a source mp4 with video produces chunks containing both
    # streams and blows through Whisper's 25 MB per-request limit.
    chunks_dir = cache_dir / "chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    for chunk in chunks:
        chunk_path = chunks_dir / chunk["filename"]
        chunk["path"] = str(chunk_path.resolve())
        if chunk_path.is_file():
            continue
        run(["ffmpeg", "-y", "-ss", str(chunk["start_sec"]), "-to", str(chunk["end_sec"]), "-i", str(audio_path), "-vn", "-c:a", "copy", str(chunk_path)])
def prepare_chunks(audio_path: Path, cache_dir: Path, max_chunk_sec: float) -> tuple[list[dict[str, Any]], list[dict[str, float]], Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    duration_sec = probe_duration(audio_path)
    windows = detect_silence_windows(audio_path, duration_sec)
    chunks = build_chunk_plan(duration_sec, windows, max_chunk_sec, audio_path.suffix or ".audio")
    planned = [{k: v for k, v in chunk.items() if k != "path"} for chunk in chunks]
    metadata_path = cache_dir / "chunks.json"
    cached = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else None
    cached_ok = isinstance(cached, dict) and cached.get("source_audio") == str(audio_path) and cached.get("duration_sec") == round(duration_sec, 6) and cached.get("silence_windows") == windows and cached.get("chunks") == planned
    chunks_dir = cache_dir / "chunks"
    if not (cached_ok and all((chunks_dir / chunk["filename"]).is_file() for chunk in chunks)):
        slice_chunks(audio_path, cache_dir, chunks)
    else:
        for chunk in chunks:
            chunk["path"] = str((chunks_dir / chunk["filename"]).resolve())
    metadata_path.write_text(json.dumps({"source_audio": str(audio_path), "duration_sec": round(duration_sec, 6), "silence_windows": windows, "chunks": chunks}, indent=2), encoding="utf-8")
    return chunks, windows, metadata_path
def require_hf_token(diarize_mode: str | None) -> str | None:
    if diarize_mode != "pyannote":
        return None
    from astrid.pack_api import CredentialsScope

    try:
        return CredentialsScope.get_local("huggingface")
    except AstridError:
        pass
    raise SystemExit("HF_TOKEN is required when using --diarize pyannote")
def diarize_audio(audio_path: Path, cache_dir: Path, diarize_mode: str | None) -> dict[str, Any] | None:
    token = require_hf_token(diarize_mode)
    if diarize_mode != "pyannote" or token is None:
        return None
    mono_path = cache_dir / "diarize_mono.wav"
    if not mono_path.is_file():
        run(["ffmpeg", "-y", "-i", str(audio_path), "-vn", "-ac", "1", str(mono_path)])
    from pyannote.audio import Pipeline
    pipeline = Pipeline.from_pretrained(PYANNOTE_PIPELINE, use_auth_token=token)
    turns = [{"start": round(float(turn.start), 6), "end": round(float(turn.end), 6), "speaker": str(speaker)} for turn, _, speaker in pipeline(str(mono_path)).itertracks(yield_label=True) if float(turn.end) > float(turn.start)]
    return {"mono_path": mono_path, "speaker_turns": turns}
def speaker_for_segment(segment: dict[str, Any], speaker_turns: list[dict[str, Any]], offset_sec: float = 0.0) -> str | None:
    start_sec, end_sec = offset_sec + float(segment.get("start", 0.0)), offset_sec + float(segment.get("end", 0.0))
    best_speaker, best_overlap = None, 0.0
    for turn in speaker_turns:
        overlap = min(end_sec, float(turn["end"])) - max(start_sec, float(turn["start"]))
        if overlap > best_overlap:
            best_speaker, best_overlap = str(turn["speaker"]), overlap
    return best_speaker
def attach_speakers(segments: list[dict[str, Any]], speaker_turns: list[dict[str, Any]] | None, offset_sec: float = 0.0) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for segment in segments:
        item = dict(segment)
        # Keep a speaker label already supplied by the transcription source.
        # Pyannote fills only missing labels; it does not rewrite source data.
        if speaker_turns and not item.get("speaker"):
            item["speaker"] = speaker_for_segment(segment, speaker_turns, offset_sec)
        else:
            item.setdefault("speaker", None)
        annotated.append(item)
    return annotated
def normalize_segment(segment: dict[str, Any], offset_sec: float) -> dict[str, Any]:
    normalized = dict(segment)
    normalized.update({"start": round(offset_sec + float(segment.get("start", 0.0)), 6), "end": round(offset_sec + float(segment.get("end", 0.0)), 6), "text": str(segment.get("text", "")).strip()})
    words = segment.get("words")
    if isinstance(words, list):
        normalized["words"] = [
            {
                **word,
                **({"start": round(offset_sec + float(word["start"]), 6)} if "start" in word else {}),
                **({"end": round(offset_sec + float(word["end"]), 6)} if "end" in word else {}),
            }
            for word in words
        ]
    return normalized
def transcribe_chunk_payload(client: Any, chunk: dict[str, Any], windows: list[dict[str, float]], model: str, language: str, vad_gate_enabled: bool) -> tuple[dict[str, Any], dict[str, int | bool]]:
    if vad_gate_enabled and chunk_silence_ratio(chunk, windows) >= 0.95:
        return {"text": "", "segments": []}, {"skipped_silent": True, "segments_filtered": 0}
    with Path(chunk["path"]).open("rb") as handle:
        response = client.audio.transcriptions.create(model=model, file=handle, response_format="verbose_json", timestamp_granularities=["segment"], language=language)
    data = response.model_dump() if hasattr(response, "model_dump") else dict(response)
    data["segments"], filtered = filter_hallucinated_segments(list(data.get("segments") or []), float(chunk["offset_sec"]), windows, vad_gate_enabled)
    return data, {"skipped_silent": False, "segments_filtered": filtered}
def collect_transcript_segments(client: Any, chunks: list[dict[str, Any]], windows: list[dict[str, float]], model: str, language: str, vad_gate_enabled: bool, speaker_turns: list[dict[str, Any]] | None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    segments: list[dict[str, Any]] = []
    summary = {"chunks": len(chunks), "skipped_silent": 0, "segments_kept": 0, "segments_filtered": 0}
    for chunk in chunks:
        payload, stats = transcribe_chunk_payload(client, chunk, windows, model, language, vad_gate_enabled)
        summary["skipped_silent"] += int(bool(stats["skipped_silent"]))
        summary["segments_filtered"] += int(stats["segments_filtered"])
        annotated = attach_speakers(list(payload.get("segments") or []), speaker_turns, float(chunk["offset_sec"]))
        segments.extend(normalize_segment(segment, float(chunk["offset_sec"])) for segment in annotated)
    summary["segments_kept"] = len(segments)
    return segments, summary
def write_transcripts(out_dir: Path, segments: list[dict[str, Any]], audit: AuditContext | None = None, *, asset_id: str | None = None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, srt_path, txt_path = out_dir / "transcript.json", out_dir / "transcript.srt", out_dir / "transcript.txt"
    transcript_document: dict[str, Any] = {"segments": segments}
    if asset_id is not None:
        asset_id = asset_id.strip()
        if not asset_id:
            raise AstridValueError("asset_id must be a non-empty app media asset ID")
        transcript_document["assetId"] = asset_id
    json_path.write_text(json.dumps(transcript_document, indent=2), encoding="utf-8")
    srt_lines = [item for index, segment in enumerate(segments, start=1) for item in (str(index), f"{srt_timestamp(float(segment['start']))} --> {srt_timestamp(float(segment['end']))}", str(segment.get("text", "")), "")]
    srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
    txt_path.write_text("\n".join(str(segment.get("text", "")) for segment in segments if str(segment.get("text", "")).strip()), encoding="utf-8")
    if audit is not None:
        parents: list[str] = []
        transcript_id = audit.register_asset(
            kind="transcript",
            path=json_path,
            label="Transcript JSON",
            parents=parents,
            stage="transcribe",
            metadata={"segments": len(segments), "format": "json"},
        )
        audit.register_asset(
            kind="transcript_text",
            path=txt_path,
            label="Transcript text",
            parents=[transcript_id],
            stage="transcribe",
            metadata={"segments": len(segments), "format": "txt"},
        )
        audit.register_asset(
            kind="subtitle",
            path=srt_path,
            label="Transcript SRT",
            parents=[transcript_id],
            stage="transcribe",
            metadata={"segments": len(segments), "format": "srt"},
        )
        audit.register_node(
            stage="transcribe",
            label="Write transcripts",
            outputs=[transcript_id],
            metadata={"segments": len(segments)},
        )
    return {"json": json_path, "srt": srt_path, "txt": txt_path}


def whisper_transcript_content(media_id: str, segments: list[dict[str, Any]]) -> dict[str, Any]:
    """Preserve Whisper's source-time segments; use stable ordinal IDs."""
    from astrid.sdk.media_transcript import SCHEMA, WHISPER_SOURCE

    converted = []
    for index, segment in enumerate(segments):
        segment_id = f"whisper:s{index:05d}"
        words: list[dict[str, Any]] = []
        for word_index, word in enumerate(segment.get("words") or []):
            words.append({"id": f"{segment_id}:w{word_index:05d}",
                          "start": float(word["start"]), "end": float(word["end"]),
                          "text": str(word.get("text", word.get("word", "")))})
        start, end = float(segment["start"]), float(segment["end"])
        if words:
            start = min(start, *(word["start"] for word in words))
            end = max(end, *(word["end"] for word in words))
        converted.append({"id": segment_id, "start": start, "end": end,
                          "text": str(segment.get("text", "")), "words": words})
    return {"schema": SCHEMA, "media_id": media_id, "source_id": WHISPER_SOURCE, "segments": converted}


def _register_runtime_transcript(audio_path: Path, json_path: Path, project: str | None) -> None:
    if not project:
        print("note: transcript document skipped (no Runtime project supplied)", file=sys.stderr)
        return
    try:
        from astrid.sdk import AstridClient
        from astrid.sdk.media_transcript import WHISPER_SOURCE, write_transcript

        media_id = f"sha256:{sha256_file(audio_path)}"
        segments = json.loads(json_path.read_text(encoding="utf-8"))["segments"]
        content = whisper_transcript_content(media_id, segments)
        with AstridClient.open_from_launcher(start_pack_host=False) as runtime_client:
            status = write_transcript(runtime_client, project, media_id, WHISPER_SOURCE, content)
        print(f"transcript_document={status} media={media_id}")
    except Exception as exc:  # noqa: BLE001 - containment: the transcript outputs remain valid if registration fails
        print(f"note: transcript document registration failed: {exc}", file=sys.stderr)
def transcribe_to_outputs(audio_path: Path, out_dir: Path, cache_dir: Path, client: Any, model: str, language: str, max_chunk_sec: float, vad_gate_enabled: bool, diarize_mode: str | None, audit: AuditContext | None = None, *, asset_id: str | None = None) -> tuple[dict[str, Path], dict[str, int], Path]:
    chunks, windows, metadata_path = prepare_chunks(audio_path, cache_dir, max_chunk_sec)
    diarization = diarize_audio(audio_path, cache_dir, diarize_mode)
    segments, summary = collect_transcript_segments(client, chunks, windows, model, language, vad_gate_enabled, None if diarization is None else diarization["speaker_turns"])
    if audit is not None:
        audit.register_asset(
            kind="source_audio",
            path=audio_path,
            label="Transcription source audio",
            stage="transcribe",
        )
        audit.register_asset(
            kind="chunk_plan",
            path=metadata_path,
            label="Transcription chunk plan",
            stage="transcribe",
            metadata={"model": model, "language": language},
        )
    return write_transcripts(out_dir, segments, audit, asset_id=asset_id), summary, metadata_path
def _ensure_declared_chunk_plan(out_dir: Path, metadata_path: Path) -> Path:
    """Place a verified chunk-plan file at the declared output port."""
    output_root = out_dir.resolve()
    source = metadata_path.resolve()
    if not source.is_file():
        raise AstridError(f"chunk plan is not a file: {source}")

    cache_root = output_root / "cache"
    if cache_root.exists() and not cache_root.resolve().is_relative_to(output_root):
        raise AstridError(f"chunk-plan output directory escapes {output_root}: {cache_root}")
    cache_root.mkdir(parents=True, exist_ok=True)
    target = cache_root / "chunks.json"
    if target.exists() and not target.resolve().is_relative_to(output_root):
        raise AstridError(f"chunk-plan output escapes {output_root}: {target}")

    if source != target.resolve():
        shutil.copyfile(source, target)
        if source.stat().st_size != target.stat().st_size or sha256_file(source) != sha256_file(target):
            raise AstridError(f"chunk-plan copy verification failed: {source} -> {target}")
    return target

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.audio = require_runtime_materialized_file(args.audio, label="--audio")
    audio_path = args.audio.resolve()
    if not audio_path.is_file():
        raise SystemExit(f"Audio file not found: {audio_path}")
    out_dir, cache_dir = resolve_dirs(audio_path, args.out, args.cache_dir)
    from openai import OpenAI
    client = OpenAI(api_key=load_api_key(args.env_file))
    audit = AuditContext.from_env()
    paths, summary, metadata_path = transcribe_to_outputs(audio_path, out_dir, cache_dir, client, args.model, args.language, args.max_chunk_sec, not args.no_vad_gate, args.diarize, audit, asset_id=args.asset_id)
    _ensure_declared_chunk_plan(out_dir, metadata_path)
    print(" ".join([f"chunks={summary['chunks']}", f"skipped_silent={summary['skipped_silent']}", f"segments_kept={summary['segments_kept']}", f"segments_filtered={summary['segments_filtered']}", f"transcript_json={paths['json']}", f"metadata={metadata_path}"]))

    # --- universal result manifest -----------------------
    output_specs = (
        ("transcript", Path("transcript.json")),
        ("subtitle", Path("transcript.srt")),
        ("transcript_text", Path("transcript.txt")),
        ("chunk_plan", Path("cache/chunks.json")),
    )
    manifest_outputs: list[dict[str, Any]] = []
    for ordinal, (name, relative_path) in enumerate(output_specs):
        concrete_path = out_dir / relative_path
        if not concrete_path.is_file():
            raise AstridError(f"declared output is not a file: {concrete_path}")
        manifest_outputs.append(
            {
                "name": name,
                "path": relative_path.as_posix(),
                "type": "file",
                "content_hash": f"sha256:{sha256_file(concrete_path)}",
                "bytes": concrete_path.stat().st_size,
                "ordinal": ordinal,
                "role": "result",
                "is_primary": ordinal == 0,
            }
        )
    manifest_path = out_dir / "manifest.json"
    manifest = build_manifest(
        kind="transcript",
        inputs={
            "audio": str(audio_path),
            "model": args.model,
            "language": args.language,
        },
        outputs=manifest_outputs,
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    _register_runtime_transcript(audio_path, paths["json"], args.project or args.project_slug or os.environ.get("ASTRID_TASK_PROJECT") or os.environ.get("ASTRID_PROJECT_SLUG"))

    return 0
if __name__ == "__main__":
    raise SystemExit(main())
