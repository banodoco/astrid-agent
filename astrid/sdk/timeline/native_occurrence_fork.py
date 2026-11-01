"""Fork one existing occurrence's child through native allocation and recording."""
from __future__ import annotations

from ._command_state import CommandError, root_identity
from .native_occurrence_duplicate import OCCURRENCE_INTENT, copy_child

FORK_PARAMETERS = {
    'type': 'object', 'required': ['shot_id'],
    'properties': {'shot_id': {'type': 'string', 'pattern': r'^(?!.*::)[^/]+$'}},
    'additionalProperties': False,
}


def occurrence_fork(ctx, command):
    owner = command['select']
    root = root_identity(owner)
    if root is None or root[0] != 'occurrence':
        raise CommandError('fork requires one explicit occurrence', kind='unsupported_ownership')
    if ctx.read('parent', ['config', 'pinnedShotGroups']) not in (None, []):
        raise CommandError('legacy groups require recorded migration', kind='migration_needed')
    row = ctx.read(owner)
    if not isinstance(row, dict):
        raise CommandError('fork source disappeared', kind='identity_conflict', ref=owner)
    source_shot = ctx.read(owner + '/shot', ['shot_id'])
    ctx.select('collection.clips', owner)
    ctx.select('collection.tracks', owner)
    internal = ctx.read(owner + '/source')
    payload = ctx.read(owner + '/shot', ['payload'])
    shot_id = command['args']['shot_id']
    if shot_id == source_shot:
        raise CommandError('fork requires a distinct reserved shot identity',
                           kind='identity_collision', ref=owner)
    ctx.bind('fork.source', owner + '/source')
    copy_child(ctx, owner, row['occurrence_id'], shot_id, source_shot, payload, internal)
    ctx.bind('fork.owner', owner)
    ctx.write(owner, ['shot_id'], shot_id)


def capture_occurrence_fork(bundle, owner, shot_id):
    """Reserve the destination once and capture against the exact original bundle."""
    from .commands import execute

    return execute(bundle, [{'schema_version': 1, 'op': 'occurrence.fork', 'version': '1',
                             'select': owner, 'args': {'shot_id': shot_id},
                             'intent': OCCURRENCE_INTENT}]).commands[0]
