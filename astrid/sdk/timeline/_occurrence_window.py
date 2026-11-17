"""SDK side of the single `source_offset` rule (see astrid.core.timeline.occurrence_window)."""
from __future__ import annotations

from astrid.core.timeline.occurrence_window import source_offset_ms

from ._command_state import CommandError


def occurrence_offset_ms(row, *, content_extent_ms=None):
    """The occurrence's validated window origin in ms; raises CommandError otherwise."""
    duration = row.get('duration_ms')
    duration = duration if isinstance(duration, (int, float)) and not isinstance(duration, bool) else None
    try:
        return source_offset_ms(row.get('source_offset'), duration_ms=duration,
                                content_extent_ms=content_extent_ms)
    except ValueError as exc:
        kind = 'occurrence_boundary' if 'content' in str(exc) else 'unsupported_map'
        raise CommandError(str(exc), kind=kind, ref='occurrence:' + str(row.get('occurrence_id'))) from exc


def content_extent_ms(state, owner):
    """End of the shot's active internal content in shot-local ms (0 without content). Reads only."""
    from ._recording import active
    from .capture import _duration, number

    end = 0.0
    for clip in state.collection(owner, 'clip'):
        if not active(clip):
            continue
        try:
            start = clip['at'] if 'at' in clip else number(clip.get('at_ms'), 'clip.at_ms') / 1000
            end = max(end, (number(start, 'clip.at') + _duration(clip)) * 1000)
        except (ValueError, TypeError) as exc:
            if isinstance(exc, CommandError):
                raise
            raise CommandError(str(exc), kind='unsupported_timing', ref=owner) from exc
    return end
