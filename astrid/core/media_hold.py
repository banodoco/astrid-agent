"""Turn a project still into a silent, source-linked video."""

import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from astrid.core.media_crop import _default_key, _failure, _kind_for, _open_still
from astrid.sdk.contracts import DomainResult
from astrid.sdk.exceptions import ServiceError


def hold_media(client: Any, *, project: str, ref: str, duration: float, fps: int = 24) -> DomainResult[Any]:
    if not math.isfinite(duration) or duration <= 0 or fps <= 0:
        return _failure("validation_error", "duration and fps must be positive and finite")
    if duration * fps < 0.5:
        return _failure("validation_error", "duration is too short to encode a video frame at this fps")
    shown = client.media.show(project, ref)
    if not shown.ok:
        return shown
    source = shown.data
    filename = source.get("filename") or source.get("stored_filename") or ref
    if _kind_for(source.get("media_type"), filename) != "image":
        return _failure("unsupported_operation", "media hold requires a still image")
    source_id = source["object_id"]
    metadata = {
        "operation": "hold", "source_object_id": source_id, "source_digest": source["digest"],
        "duration": float(duration), "fps": fps, "audio": {"silent": True, "sample_rate": 48000},
    }
    key = _default_key(project, metadata)
    try:
        image, _ = _open_still(client.media.read_bytes(source_id))
        with tempfile.TemporaryDirectory(prefix="astrid-hold-") as temporary:
            still = Path(temporary) / "source.png"
            image.convert("RGB").save(still)
            output = still.with_name(f"{Path(filename).stem}-hold.mp4")
            # 4:4:4 also accommodates odd dimensions without changing the frame.
            pix_fmt = "yuv444p" if image.width % 2 or image.height % 2 else "yuv420p"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-filter_threads", "1",
                "-loop", "1", "-framerate", str(fps), "-i", str(still),
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", str(duration),
                "-c:v", "libx264", "-crf", "0", "-pix_fmt", pix_fmt, "-c:a", "alac", str(output),
            ], capture_output=True, text=True, check=True, timeout=3600)
            imported = client.media.import_file(project=project, path=output, realm="managed_local",
                                                idempotency_key=key)
        if not imported.ok:
            return imported
        media = imported.data
        related = client.media.relate(project, from_object_id=media["object_id"], to_object_id=source_id,
                                      kind="derived_from", metadata=metadata, idempotency_key=key)
        if not related.ok:
            return _failure(related.error.code, f"Hold imported but relation failed: {related.error.message}. "
                            "Repeat the command to retry.", key, imported_object_id=media["object_id"])
        return DomainResult.success({"media": media, "provenance": metadata}, idempotency_key=key)
    except ValueError as exc:
        return _failure("validation_error", str(exc), key)
    except subprocess.CalledProcessError as exc:
        return _failure("unavailable", f"ffmpeg failed: {exc.stderr.strip()[-400:]}", key)
    except (OSError, ServiceError, subprocess.TimeoutExpired) as exc:
        return _failure("unavailable", str(exc), key)
