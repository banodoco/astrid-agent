#!/usr/bin/env python3
"""Repair weak-mic speech using hotter pre-lift, LavaSR, and loudness mastering."""

from __future__ import annotations

from astrid.core.contracts.errors import AstridError
from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("media.speech_repair_lavasr")

import argparse
import json
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import fal_client

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.util.credentials_scope import CredentialsScope

Runner = Callable[..., subprocess.CompletedProcess[str]]

PRELIFT_FILTER = (
    "highpass=f=85,"
    "lowpass=f=11500,"
    "afftdn=nf=-30:nt=w,"
    "speechnorm=e=24:r=0.00025:l=1,"
    "acompressor=threshold=-31dB:ratio=6:attack=3:release=90:makeup=16dB,"
    "alimiter=limit=0.985"
)

LOUDNESS_FILTER = (
    "loudnorm=I=-16:TP=-1.5:LRA=9,"
    "acompressor=threshold=-22dB:ratio=2.5:attack=5:release=120:makeup=2dB,"
    "alimiter=limit=0.95"
)


def build_parser() -> argparse.ArgumentParser:
    def boolean_arg(value: str | None) -> bool:
        if value is None:
            return True
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise argparse.ArgumentTypeError(f"expected boolean value, got {value!r}")

    parser = argparse.ArgumentParser(
        prog="media.speech_repair_lavasr",
        description="Repair a weak-mic speech section with fal-ai/lava-sr.",
    )
    parser.add_argument("--input", type=Path, required=True, help="Source video file.")
    parser.add_argument("--start", type=float, required=True, help="Start time in seconds.")
    parser.add_argument("--dur", type=float, required=True, help="Duration in seconds.")
    parser.add_argument("--output", type=Path, required=True, help="Output repaired MP4.")
    parser.add_argument("--env-file", type=Path, help="Optional .env file containing FAL_KEY.")
    parser.add_argument(
        "--deepfilternet3",
        nargs="?",
        const=True,
        default=False,
        type=boolean_arg,
        help="Run fal-ai/deepfilternet3 after LavaSR before the final loudness pass.",
    )
    return parser


def _run(cmd: list[str], *, runner: Runner = subprocess.run) -> subprocess.CompletedProcess[str]:
    result = runner(cmd, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise AstridError(
            f"command failed with exit code {result.returncode}: {' '.join(cmd[:3])}",
            recovery_command="inspect ffmpeg/fal inputs and rerun media.speech_repair_lavasr",
            state_snapshot={"stderr": result.stderr[-4000:] if result.stderr else ""},
        )
    return result


def _extract_clip(src: Path, start: float, dur: float, out: Path, *, runner: Runner) -> None:
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.6f}",
            "-t",
            f"{dur:.6f}",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            str(out),
        ],
        runner=runner,
    )


def _prelift_audio(clip: Path, out_wav: Path, *, runner: Runner) -> None:
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(clip),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-af",
            PRELIFT_FILTER,
            str(out_wav),
        ],
        runner=runner,
    )


def _remux_audio(video: Path, audio: Path, out: Path, *, runner: Runner) -> None:
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(out),
        ],
        runner=runner,
    )


def _master_loudness(video: Path, out: Path, *, runner: Runner) -> None:
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-af",
            LOUDNESS_FILTER,
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(out),
        ],
        runner=runner,
    )


def _volumedetect(path: Path, *, runner: Runner) -> dict[str, float | None]:
    result = runner(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "volumedetect",
            "-f",
            "null",
            "-",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    text = result.stderr or ""
    mean = re.search(r"mean_volume: ([\-0-9.]+) dB", text)
    maxv = re.search(r"max_volume: ([\-0-9.]+) dB", text)
    return {
        "mean_db": float(mean.group(1)) if mean else None,
        "max_db": float(maxv.group(1)) if maxv else None,
    }


def _run_lavasr(input_wav: Path, out_wav: Path, response_json: Path, env_file: Path | None) -> None:
    api_key = CredentialsScope.get_local("fal", env_file=env_file)
    audio_url = fal_client.upload_file(input_wav)
    result = fal_client.subscribe(
        "fal-ai/lava-sr",
        arguments={"audio_url": audio_url, "audio_format": "wav", "bitrate": "192k"},
        with_logs=True,
    )
    response_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    audio_obj = result.get("audio") or result.get("audio_file") or {}
    out_url = audio_obj.get("url") if isinstance(audio_obj, dict) else None
    if not out_url:
        raise AstridError(
            "fal-ai/lava-sr response did not include an audio URL",
            recovery_command="inspect the FAL response JSON and rerun",
            state_snapshot={"response": result},
        )
    urllib.request.urlretrieve(out_url, out_wav)


def _run_deepfilternet3(input_audio: Path, out_audio: Path, response_json: Path, env_file: Path | None) -> None:
    api_key = CredentialsScope.get_local("fal", env_file=env_file)
    audio_url = fal_client.upload_file(input_audio)
    result = fal_client.subscribe(
        "fal-ai/deepfilternet3",
        arguments={"audio_url": audio_url},
        with_logs=True,
    )
    response_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    audio_obj = result.get("audio_file") or result.get("audio") or {}
    out_url = audio_obj.get("url") if isinstance(audio_obj, dict) else None
    if not out_url:
        raise AstridError(
            "fal-ai/deepfilternet3 response did not include an audio URL",
            recovery_command="inspect the FAL response JSON and rerun",
            state_snapshot={"response": result},
        )
    urllib.request.urlretrieve(out_url, out_audio)


def main(argv: list[str] | None = None, *, runner: Runner = subprocess.run) -> int:
    def _main() -> int:
        args = build_parser().parse_args(argv)
        src = args.input.expanduser().resolve()
        out = args.output.expanduser().resolve()
        env_file = args.env_file.expanduser().resolve() if args.env_file else None

        if not src.is_file():
            raise AstridError(
                f"input file not found: {src}",
                recovery_command="check --input and rerun",
            )
        if args.start < 0 or args.dur <= 0:
            raise AstridError(
                f"invalid time range: start={args.start}, dur={args.dur}",
                recovery_command="use start >= 0 and dur > 0",
            )

        out.parent.mkdir(parents=True, exist_ok=True)
        stem = out.stem
        clip = out.parent / f"{stem}.source-clip.mp4"
        prelift_wav = out.parent / f"{stem}.prelift-hotter-16k.wav"
        lavasr_wav = out.parent / f"{stem}.lavasr.wav"
        lavasr_video = out.parent / f"{stem}.lavasr.mp4"
        response_json = out.parent / f"{stem}.lavasr.response.json"
        deepfilter_audio = out.parent / f"{stem}.deepfilternet3.mp3"
        deepfilter_video = out.parent / f"{stem}.deepfilternet3.mp4"
        deepfilter_response_json = out.parent / f"{stem}.deepfilternet3.response.json"

        _extract_clip(src, args.start, args.dur, clip, runner=runner)
        before_metrics = _volumedetect(clip, runner=runner)
        _prelift_audio(clip, prelift_wav, runner=runner)
        _run_lavasr(prelift_wav, lavasr_wav, response_json, env_file)
        repair_audio = lavasr_wav
        repair_video = lavasr_video
        _remux_audio(clip, repair_audio, repair_video, runner=runner)
        if args.deepfilternet3:
            _run_deepfilternet3(lavasr_wav, deepfilter_audio, deepfilter_response_json, env_file)
            repair_audio = deepfilter_audio
            repair_video = deepfilter_video
            _remux_audio(clip, repair_audio, repair_video, runner=runner)
        _master_loudness(repair_video, out, runner=runner)
        after_metrics = _volumedetect(out, runner=runner)

        manifest = build_manifest(
            kind="speech_repair_lavasr",
            inputs={
                "input": str(src),
                "start": args.start,
                "dur": args.dur,
                "env_file": str(env_file) if env_file else None,
                "deepfilternet3": bool(args.deepfilternet3),
            },
            outputs=[
                {"path": out.name, "type": "file"},
                {"path": clip.name, "type": "intermediate"},
                {"path": prelift_wav.name, "type": "intermediate"},
                {"path": lavasr_wav.name, "type": "intermediate"},
                {"path": lavasr_video.name, "type": "intermediate"},
                {"path": response_json.name, "type": "metadata"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
            recipe={
                "prelift_filter": PRELIFT_FILTER,
                "fal_model": "fal-ai/lava-sr",
                "***": "fal-ai/deepfilternet3" if args.deepfilternet3 else None,
                "loudness_filter": LOUDNESS_FILTER,
            },
            metrics={
                "source_clip": before_metrics,
                "output": after_metrics,
            },
        )
        if args.deepfilternet3:
            manifest["outputs"].extend(
                [
                    {"path": deepfilter_audio.name, "type": "intermediate"},
                    {"path": deepfilter_video.name, "type": "intermediate"},
                    {"path": deepfilter_response_json.name, "type": "metadata"},
                ]
            )
        write_manifest(out.parent / "manifest.json", manifest)
        print(out)
        return 0

    return run_pack_main("media.speech_repair_lavasr", _main, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
