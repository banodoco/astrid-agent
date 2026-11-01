"""Registered test recipes for staged inverse tests, independent of rendering."""
from __future__ import annotations


def install_staging_operations(monkeypatch):
    from astrid.sdk.timeline import commands

    def move(ctx, command):
        ref, args = command['select'], command['args']
        owner = ref.rsplit('/', 1)[0]
        ctx.select('move.target', owner, [ref])
        track = args.get('track', ctx.read(ref, ['track']))
        ctx.read(ref, ['at'])
        target = owner + '/track:' + track
        ctx.read(target, ['id'])
        ctx.read(target, ['kind'])
        ctx.select('collection.tracks', owner)
        ctx.bind('move.track', target)
        ctx.write(ref, ['track'], track)
        ctx.write(ref, ['at'], args['on'])

    def create(ctx, command):
        ctx.create(command['select'], 'clip', command['args']['row'])

    def properties(ctx, command):
        for change in command['args']['changes']:
            ctx.read(command['select'], change['path'])
        for change in command['args']['changes']:
            ctx.assign(command['select'], change['path'], change['state'])

    registered = commands.operations()
    for name, handler in [('fixture.move', move), ('fixture.clip-create', create),
                          ('fixture.properties', properties)]:
        registered[name] = commands.Operation({'type': 'object'}, handler, {}, name != 'fixture.clip-create')
    monkeypatch.setattr(commands, 'operations', lambda: registered)


def staged_move(ref, track, on=4):
    return {'schema_version': 1, 'op': 'fixture.move', 'version': '1', 'select': ref,
            'args': {'track': track, 'on': on}}
