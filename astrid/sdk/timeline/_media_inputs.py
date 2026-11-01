"""Detached VerifiedMedia checks shared by deterministic media recipes."""
from __future__ import annotations

from ._command_state import CommandError, equal
from .capture import number


def _verified(ctx, pin):
    matches = [row for row in ctx.verified_media if equal(row.get('selection'), pin)]
    if not matches or any(not equal(row, matches[0]) for row in matches):
        raise CommandError('media edit requires exact Runtime-verified media input', kind='dependency_changed')
    value = matches[0]
    media_type = value.get('media_type')
    kind = media_type.split('/')[0] if isinstance(media_type, str) and '/' in media_type else None
    if kind not in ('image', 'video', 'audio') or type(value.get('size')) is not int or value['size'] <= 0:
        raise CommandError('media edit requires verified image/video/audio metadata', kind='unsupported_media')
    if kind in ('video', 'audio'):
        number(value.get('duration_seconds'), 'intrinsic duration', positive=True)
    return value, kind
