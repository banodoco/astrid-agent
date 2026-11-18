"""Read and map timed transcript words to timeline clip occurrences."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def map_transcript_words(
    transcript: Mapping[str, Any], *, source_range: tuple[float, float] | None = None,
    clips: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """Return source-clock words and one independently mapped row per clip use."""
    rows: list[dict[str, Any]] = []
    for segment in transcript.get("segments", ()):
        if not isinstance(segment, Mapping):
            continue
        for word in segment.get("words", ()):
            if not isinstance(word, Mapping):
                continue
            start, end = word.get("start"), word.get("end")
            if (isinstance(start, bool) or isinstance(end, bool)
                    or not isinstance(start, (int, float)) or not isinstance(end, (int, float))):
                continue
            if source_range is not None and (end <= source_range[0] or start >= source_range[1]):
                continue
            row = {"word_id": word.get("id"), "text": word.get("text"),
                   "source_start": start, "source_end": end}
            mapped = []
            for clip in clips:
                source_in = clip.get("source_start", 0)
                source_out = clip.get("source_end")
                speed = clip.get("speed", 1)
                timeline_start = clip.get("timeline_start", 0)
                if (speed <= 0 or end <= source_in
                        or (source_out is not None and start >= source_out)):
                    continue
                mapped.append({"clip_id": clip.get("clip_id"),
                               "timeline_start": timeline_start + (start - source_in) / speed,
                               "timeline_end": timeline_start + (end - source_in) / speed})
            if clips:
                for occurrence in mapped:
                    rows.append({**row, **occurrence})
            else:
                rows.append(row)
    return rows
