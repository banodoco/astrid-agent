"""Occurrence-owned shot name and bounded settings edits on the raw shot payload."""
from __future__ import annotations

import copy
import re

from ._command_state import CommandError

SHOT_INTENT = {'ownership': 'occurrence'}


def shot_set(ctx, command):
    ref = command['select']
    if not isinstance(ref, str) or not re.fullmatch(r'occurrence:(?!.*::)[^/]+/shot', ref):
        raise CommandError('shot.set requires an explicit occurrence shot identity')
    owner = ref.removesuffix('/shot')
    if ctx.read(owner, ['occurrence_id']) is None:
        raise CommandError('shot owner disappeared', kind='identity_conflict', ref=owner)
    ctx.read(ref, ['shot_id'])
    shot = ctx.state.occurrence(owner)['shot_id']
    source = ctx.state.bundle['source_mapping']['shots'].get(shot)
    if source is not None:
        ctx.dependency('shot.source', 'entity', {'source': copy.deepcopy(source)})
    ctx.bind('shot.owner', owner)
    values, clear = command['args']['values'], command['args']['clear']
    settings = values.get('settings', {})
    paths = {('name',): 'name' in values, **{('settings', key): True for key in settings}}
    for entry in clear:
        path = tuple(entry.split('.'))
        if paths.get(path):
            raise CommandError('a shot field cannot be both set and cleared', ref=ref, path=path)
    # Intent stays scoped to this occurrence's own shot root, whether or not its
    # source shot is shared; sharing is resolved by the authoring bundle.
    if 'name' in values:
        ctx.write(ref, ['payload', 'name'], values['name'])
    if settings:
        ctx.object(ref, ['payload', 'settings'])
    for key in sorted(settings):
        ctx.write(ref, ['payload', 'settings', key], settings[key])
    for entry in sorted(clear):
        ctx.clear(ref, ['payload', *entry.split('.')])


def capture_shot_set(bundle, select, *, values=None, clear=()):
    """Set or clear the selected occurrence's own shot name/settings fields."""
    from .commands import execute

    args = {'values': copy.deepcopy({} if values is None else values), 'clear': list(clear)}
    return execute(bundle, [{'schema_version': 1, 'op': 'shot.set', 'version': '1',
                             'select': select, 'args': args, 'intent': dict(SHOT_INTENT)}]).commands[0]
