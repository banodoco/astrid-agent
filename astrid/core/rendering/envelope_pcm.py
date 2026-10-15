"""Attempt-owned PCM for the existing Remotion external encoder asset path."""

from __future__ import annotations

import json
import math
import subprocess

from astrid.core.rendering.compiled_audio import audio_host, digest
from astrid.core.timeline.audio_envelope import (
    AudioEnvelopeError,
    compile_legacy,
    ffmpeg_expression,
)


def requires_envelope_pcm(config):
    fps = config.get("output", {}).get("fps", config.get("fps", 30))
    return any(
        audio_host(config, clip) and compile_legacy(clip, fps) is not None
        for clip in config.get("clips", [])
    )


def prepare_envelope_pcm(config, materializer, server):
    """Bake gain only. Remotion retains its single source trim/rate application.

    Each clip gets independent full-source float PCM, including repeated assets.
    The asset materializer's disposable stage owns cleanup on success/failure.
    """
    fps = config.get("output", {}).get("fps", config.get("fps", 30))
    for clip in config.get("clips", []):
        if not audio_host(config, clip):
            continue
        envelope = compile_legacy(clip, fps)
        if envelope is None:
            continue
        asset = materializer.assets.get(clip.get("asset"))
        if server is None or asset is None or asset.local_path is None:
            raise AudioEnvelopeError(
                f"{clip.get('id')}: encoder requires attempt-local PCM asset",
                duck=bool(envelope.get("duck")),
            )
        # Identical to computeMediaTrim's frame rounding and sanitized rate.
        source_from = max(0, math.floor(float(clip.get("from", 0)) * fps + 0.5)) / fps
        rate = float(clip.get("speed", 1))
        if not math.isfinite(rate) or rate <= 0:
            raise AudioEnvelopeError("invalid encoder sample clock")
        signature = digest(
            {"envelope": envelope, "source_from": source_from, "playback_rate": rate}
        )
        # stage is always disposable. serving_root may be an admitted object root;
        # callers must stage sources (reuse_materialized_paths=False) for PCM.
        destination = materializer.staging_dir / (
            "envelope-" + digest({"id": clip["id"], "signature": signature}) + ".wav"
        )
        if materializer.serving_root != materializer.staging_dir:
            raise AudioEnvelopeError("encoder PCM requires disposable asset staging")
        expression = ffmpeg_expression(envelope, f"((t-{source_from:.17g})/{rate:.17g})")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-i",
                    str(asset.local_path),
                    "-vn",
                    "-af",
                    f"aeval=exprs='val(ch)*({expression})':c=same",
                    "-c:a",
                    "pcm_f32le",
                    str(destination),
                ],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            destination.unlink(missing_ok=True)
            raise AudioEnvelopeError(
                f"{clip.get('id')}: encoder PCM preparation failed: {exc}",
                duck=bool(envelope.get("duck")),
            ) from exc
        clip["audio"] = {
            "envelope": envelope,
            "render_source": {
                "file": server.local_url(destination),
                "envelope_key": json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                "source_from": source_from,
                "playback_rate": rate,
            },
        }
    return config
