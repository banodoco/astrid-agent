"""Relay Remotion CLI frame progress to the host's task progress file.

With stdout not a TTY, the Remotion CLI prints one plain line per progress
update ("Rendered 2175/4977, time remaining: 6m 3s", then "Encoded N/T").
The backend sends that stdout to a log file; this relay tails it and writes
the newest frame count to ``ASTRID_PROGRESS_PATH``. The generic host forwards
that file with every lease heartbeat as a ``task.progress`` event.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from astrid.core.rendering.revision_notice import NOTICE_ACK_NAME

PROGRESS_PATH_ENV = "ASTRID_PROGRESS_PATH"

_RENDERED = re.compile(r"Rendered (\d+)/(\d+)(?:, time remaining: ([0-9hms ]+))?")
_ENCODED = re.compile(r"Encoded (\d+)/(\d+)")
# The render executor reports "render" at 10% and "validate_output" at 90%.
# Frame capture fills 10-85% and the final encode or mux 85-90%.
_RENDER_START, _RENDER_END, _ENCODE_END = 10, 85, 90
# Normal host progress is sampled on its existing heartbeat cadence. Bound
# the advisory delivery wait; lack of a notice ACK must not refuse a render.
_NOTICE_ACK_TIMEOUT_SECONDS = 15.0


def parse_progress_line(line: str) -> dict[str, Any] | None:
    """Return host progress for one Remotion CLI progress line, if it is one."""

    match = _RENDERED.search(line)
    if match:
        done, total = int(match[1]), int(match[2])
        if total < 1:
            return None
        eta = (match[3] or "").strip()
        phase = f"rendered {done}/{total} frames" + (f" · ETA {eta}" if eta else "")
        percent = _RENDER_START + (_RENDER_END - _RENDER_START) * min(done, total) // total
        return {"phase": phase, "percent": percent, "current": done, "total": total}
    match = _ENCODED.search(line)
    if match:
        done, total = int(match[1]), int(match[2])
        if total < 1:
            return None
        percent = _RENDER_END + (_ENCODE_END - _RENDER_END) * min(done, total) // total
        return {
            "phase": f"encoded {done}/{total} frames",
            "percent": percent,
            "current": done,
            "total": total,
        }
    return None


class RemotionProgressRelay:
    """Tail a Remotion stdout log and publish its newest frame progress.

    A no-op when the host supplied no progress path (direct CLI callers).
    """

    def __init__(
        self,
        log_path: Path,
        *,
        progress_path: str | os.PathLike[str] | None = None,
        interval_seconds: float = 1.0,
        notices: list[str] | None = None,
    ) -> None:
        raw = progress_path
        self.log_path = Path(log_path)
        self.progress_path = Path(raw) if raw else None
        self.interval_seconds = interval_seconds
        self.notices = tuple(notices or ())
        self.latest: dict[str, Any] | None = None
        self._offset = 0
        self._partial = ""
        self._published: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "RemotionProgressRelay":
        if self.progress_path is not None:
            if self.notices:
                self._publish_initial_notices()
            self._thread = threading.Thread(
                target=self._run, name="astrid-remotion-progress", daemon=True
            )
            self._thread.start()
        return self

    def _publish_initial_notices(self) -> None:
        assert self.progress_path is not None
        ack_path = self.progress_path.with_name(NOTICE_ACK_NAME)
        managed = ack_path.is_file() and not ack_path.is_symlink()
        deadline = time.monotonic() + _NOTICE_ACK_TIMEOUT_SECONDS
        for notice in dict.fromkeys(self.notices):
            self._publish({"phase": "render", "percent": _RENDER_START}, notice=notice)
            if not managed:
                # A direct/legacy caller has no Runtime observer to ACK.
                break
            while time.monotonic() < deadline:
                try:
                    if (not ack_path.is_symlink()
                            and json.loads(ack_path.read_text(encoding="utf-8")).get("phase") == notice):
                        break
                except (OSError, ValueError, AttributeError):
                    pass
                time.sleep(0.05)
            else:
                # The notice is advisory. Leave it available for subsequent
                # heartbeats and do not turn a transport outage into refusal.
                print("Runtime render mismatch notice acknowledgement unavailable before drawing",
                      file=sys.stderr, flush=True)
                break

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.poll()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.poll()

    def poll(self) -> dict[str, Any] | None:
        """Read new log lines, publish the newest progress, and return it."""

        with self._lock:
            return self._poll_locked()

    def _poll_locked(self) -> dict[str, Any] | None:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(64 * 1024)
                self._offset += len(chunk)
        except OSError:
            return self.latest
        lines = (self._partial + chunk.decode("utf-8", errors="replace")).split("\n")
        self._partial = lines.pop()[-4096:]
        for line in lines:
            parsed = parse_progress_line(line)
            if parsed is not None:
                self.latest = parsed
        if self.latest is not None and self.latest != self._published:
            self._publish(self.latest)
        return self.latest

    def _publish(self, progress: dict[str, Any], *, notice: str | None = None) -> None:
        if self.progress_path is None:
            return
        temporary = self.progress_path.with_name(f".{self.progress_path.name}.remotion.tmp")
        try:
            payload = dict(progress)
            if self.notices:
                # Each notice is already bounded for the Runtime phase. Do
                # not append arbitrary frame text or another full notice.
                payload["phase"] = notice or self.notices[0]
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, self.progress_path)
        except OSError:
            # Progress is advisory; a failed write must never fail the render.
            return
        self._published = dict(progress)


__all__ = ["PROGRESS_PATH_ENV", "RemotionProgressRelay", "parse_progress_line"]
