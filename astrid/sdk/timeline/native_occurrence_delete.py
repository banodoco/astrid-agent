"""Remove one explicit canonical placement, retaining its shared source content."""
from __future__ import annotations

from ._command_state import CommandError, root_identity

OCCURRENCE_INTENT = {'ownership': 'occurrence'}


def occurrence_delete(ctx, command):
    owner = command['select']
    if root_identity(owner) is None or not owner.startswith('occurrence:'):
        raise CommandError('delete requires one explicit occurrence', kind='unsupported_ownership')
    if ctx.read('parent', ['config', 'pinnedShotGroups']) not in (None, []):
        raise CommandError('legacy groups require recorded migration', kind='migration_needed')
    if not isinstance(ctx.read(owner), dict):
        raise CommandError('delete target disappeared', kind='identity_conflict', ref=owner)
    ctx.read(owner + '/shot', ['shot_id'])
    ctx.select('collection.clips', owner)
    ctx.select('collection.tracks', owner)
    ctx.read(owner + '/source')
    ctx.read(owner + '/shot', ['payload'])
    ctx.bind('delete.owner', owner)
    ctx.clear(owner, [])


def capture_occurrence_delete(bundle, owner):
    """Capture the raw canonical owner on the original authored bundle."""
    from .commands import execute

    return execute(bundle, [{'schema_version': 1, 'op': 'occurrence.delete', 'version': '1',
                             'select': owner, 'args': {}, 'intent': OCCURRENCE_INTENT}]).commands[0]
