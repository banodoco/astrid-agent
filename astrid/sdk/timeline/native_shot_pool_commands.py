"""Shot pool membership: an ordered list of asset aliases the shot holds without a clip.

The pool lives on the shot's internal timeline at app.astrid.pool. An absent list means
pool membership was never authored here (a migrated group's legacy poolGenerationIds stay
untouched in the occurrence provenance until they are adopted with explicit adds); an empty
list is an authored empty pool. Removing the last member leaves [] and never clears the key.

shot.pool add binds the exact media in the shot registry (as placement does) and appends its
alias; remove drops the alias and keeps the binding. A member is never also a placed clip.
Writes replace the whole list, so an inverse restores the exact order and position.
"""
from __future__ import annotations

import copy

from ._command_state import ABSENT, CommandError
from ._media_inputs import _verified
from .native_occurrence_media_commands import _registry
from .native_placement_commands import _exclusive, _explicit_occurrence

POOL_INTENT = {'ownership': 'occurrence'}
POOL = ['app', 'astrid', 'pool']


def _members(state):
    if state == ABSENT:
        return None
    members = state.get('value')
    if not isinstance(members, list) or any(not isinstance(item, str) for item in members):
        raise CommandError('shot pool must be a list of asset aliases', kind='unsupported_pool')
    return members


def shot_pool(ctx, command):
    owner = command['select']
    ctx.read(owner, ['occurrence_id'])
    ctx.read(owner + '/shot', ['shot_id'])
    ctx.state.occurrence(owner)
    ctx.bind('pool.owner', owner)
    _exclusive(ctx, owner)
    ctx.read('parent', ['config', 'pinnedShotGroups'])
    action, pin = command['args']['action'], command['args']['media']
    alias = pin['asset']
    _, kind = _verified(ctx, pin)
    source = owner + '/source'
    ctx.read(source, POOL)
    members = _members(ctx.state.get(source, POOL))
    if action == 'add':
        if members is not None and alias in members:
            raise CommandError('media is already pooled', kind='identity_collision', ref=owner)
        for ref in ctx.select('collection.clips', owner):
            for key in ('asset', 'asset_id'):
                ctx.read(ref, [key])
            row = ctx.state.entity(ref)
            if alias in (row.get('asset'), row.get('asset_id')):
                raise CommandError('media is already placed in the shot', kind='identity_conflict', ref=ref)
        # Unplace retains its binding, including opaque metadata. Reuse it rather
        # than requiring equality with a newly constructed, narrower binding.
        _registry(ctx, owner, pin, kind,
                  create=ctx.state.get(owner + '/asset:' + alias, []) == ABSENT)
        if members is None:
            ctx.object(source, POOL[:1])
            ctx.object(source, POOL[:2])
        ctx.write(source, POOL, [*(members or []), alias])
    else:
        if members is None or alias not in members:
            raise CommandError('media is not pooled', kind='identity_conflict', ref=owner)
        _registry(ctx, owner, pin, kind)
        ctx.write(source, POOL, [item for item in members if item != alias])


def capture_shot_pool(bundle, owner, action, media, *, ownership, verified_media):
    """Caller supplies the exact five-field Runtime selection; action is add or remove."""
    from .commands import execute

    _explicit_occurrence(owner, ownership)
    return execute(bundle, [{'schema_version': 1, 'op': 'shot.pool', 'version': '1', 'select': owner,
                             'args': {'action': action, 'media': copy.deepcopy(media)},
                             'intent': dict(POOL_INTENT)}], verified_media=verified_media).commands[0]
