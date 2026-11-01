"""Recorded App defaults on the Runtime's pristine empty publication."""
from __future__ import annotations

from ._command_state import ABSENT, CommandError, equal


def initialize(ctx, command):
    bundle = ctx.state.bundle
    parent = ctx.read('parent')
    config = parent.get('config') if isinstance(parent, dict) else None
    pristine_parent = (
        isinstance(config, dict)
        and isinstance(config.get('tracks'), list)
        and equal(parent, {
            'config': {'clips': [], 'tracks': config['tracks']},
            'registry': {},
            'clips': [],
        })
    )
    if (ctx.record['ordinal'] != 0
            or not pristine_parent
            or bundle['placements'] or bundle['shots']):
        raise CommandError('initialization requires the pristine empty timeline', kind='not_pristine')
    for path in (['config', 'output'], ['name'], ['slug'], ['registry', 'assets']):
        ctx.read('parent', path)
        if ctx.state.get('parent', path) != ABSENT:
            raise CommandError('initialization field is already authored', kind='not_pristine')
    args = command['args']
    ctx.write('parent', ['config', 'output'], args['output'])
    ctx.write('parent', ['name'], args['name'])
    ctx.write('parent', ['slug'], args['slug'])
    ctx.write('parent', ['registry', 'assets'], {})


def capture_initialize(bundle, *, name, slug, output):
    """Capture timeline metadata on Runtime's canonical bootstrap tracks.

    Publication-history authority stays in the provider; persist the captured
    command and its original base pair before sending it.
    """
    from .commands import execute

    command = {'schema_version': 1, 'op': 'timeline.init', 'version': '1',
               'select': 'parent', 'args': {'name': name, 'slug': slug, 'output': output}}
    return execute(bundle, [command]).commands[0]
