"""Occurrence-owned immutable UTF-8 text bindings; live heads belong to Runtime."""
from __future__ import annotations

import copy
import re

from ._command_state import ABSENT, CommandError, equal

TEXT_INTENT = {'ownership': 'occurrence'}
TEXT_MEDIA_TYPE = 'text/plain; charset=utf-8'
DERIVED_FIELDS = {'binding_id', 'project_id', 'shot_id', 'head', 'event_stream_id',
                  'created_at', 'updated_at', 'text'}
AUTHORITY_FIELDS = {'binding_id', 'head', 'event_stream_id'}


def binding_key(row):
    kind, slot = row.get('kind'), row.get('slot')
    if (kind not in ('prompt', 'voiceover_script', 'transcript')
            or (slot is not None and (kind != 'prompt' or not isinstance(slot, str)
                                     or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', slot)))):
        raise CommandError('unsupported text binding identity', kind='unsupported_shape')
    return kind + (':' + slot if slot is not None else '')


def binding_rows(state, owner):
    shot = state.occurrence(owner)['shot_id']
    rows = state.bundle['shots'][shot]['payload'].get('text_bindings', [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) or not {
            'kind', 'slot', 'media_id', 'content_hash', 'byte_size', 'mime_type'} <= row.keys() for row in rows):
        raise CommandError('text bindings require immutable descriptors', kind='unsupported_shape')
    keys = [binding_key(row) for row in rows]
    if len(keys) != len(set(keys)):
        raise CommandError('ambiguous text binding identity', kind='identity_conflict')
    return rows


def text_binding_set(ctx, command):
    owner, args = command['select'], command['args']
    if not isinstance(owner, str) or not re.fullmatch(r'occurrence:(?!.*::)[^/]+', owner):
        raise CommandError('text binding requires explicit occurrence ownership')
    ctx.read(owner, ['occurrence_id'])
    ctx.read(owner + '/shot', ['shot_id'])
    if ctx.select('text-binding.owners', owner) != [owner]:
        raise CommandError('text binding requires an exclusive occurrence', kind='unsupported_ownership')
    ref = owner + '/text-binding:' + binding_key(args['binding'])
    old = ctx.read(ref)
    authority = AUTHORITY_FIELDS & set(old or ())
    if authority and authority != AUTHORITY_FIELDS:
        raise CommandError('text binding authority is incomplete', kind='unsupported_shape', ref=ref)
    if authority and (
            not isinstance(old['binding_id'], str) or not old['binding_id']
            or type(old['head']) is not int or old['head'] < 1
            or not isinstance(old['event_stream_id'], str) or not old['event_stream_id']):
        raise CommandError('text binding authority is malformed', kind='unsupported_shape', ref=ref)
    if 'clear' in args:
        if ctx.state.get(ref, []) == ABSENT:
            raise CommandError('cannot clear an absent text binding', kind='absent_binding', ref=ref)
        ctx.select('collection.text-bindings', owner)
        ctx.clear(ref, [])
        return
    pin = args['text']
    if pin['object_id'] != pin['digest']:
        raise CommandError('text object id must equal its content digest', ref=ref)
    if not any(equal(value, {**pin, 'utf8': True}) for value in ctx.verified_media):
        raise CommandError('text binding requires exact Runtime-verified UTF-8 input', kind='dependency_changed')
    same_pin = authority == AUTHORITY_FIELDS and all(old.get(key) == pin_value for key, pin_value in (
        ('media_id', pin['object_id']), ('content_hash', pin['digest']),
        ('byte_size', pin['size']), ('mime_type', pin['media_type'])))
    if old is None:
        path = ['payload', 'text_bindings']
        if ctx.state.get(owner + '/shot', path) == ABSENT:
            ctx.read(owner + '/shot', path)
            ctx.write(owner + '/shot', path, [])
        ctx.select('collection.text-bindings.append', owner)
    ctx.dependency('text:' + pin['digest'], 'media', pin)
    row = {key: copy.deepcopy(value) for key, value in (old or {}).items()
           if key not in DERIVED_FIELDS or (same_pin and key in AUTHORITY_FIELDS)}
    row.update(args['binding'], media_id=pin['object_id'], content_hash=pin['digest'],
               byte_size=pin['size'], mime_type=pin['media_type'])
    ctx.write(ref, [], row)


def capture_text_binding_set(bundle, owner, binding, *, text=None, clear=False, verified_text=()):
    from .commands import execute

    args = {'binding': copy.deepcopy(binding)}
    if text is not None:
        args['text'] = copy.deepcopy(text)
    if clear:
        args['clear'] = True
    return execute(bundle, [{'schema_version': 1, 'op': 'text-binding.set', 'version': '1',
                             'select': owner, 'args': args, 'intent': TEXT_INTENT}],
                   verified_media=verified_text).commands[0]
