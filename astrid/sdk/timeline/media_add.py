"""One parent media birth using managed import and the registered SDK recipes."""
from __future__ import annotations

import copy
import hashlib
import uuid
from collections.abc import Mapping
from pathlib import Path

from ._command_state import State
from ._common import TimelineEditError
from ._recording import active
from .capture import number
from .outbox import request_bytes


def existing_selectors(transport, project, digest):
    """Return the one catalog variant already holding this digest, else None.

    Zero or several matches return None so the caller falls back to the
    idempotent managed import instead of guessing between variants.
    """
    from astrid.sdk.pagination import paged_rows

    matches = []
    for generation in paged_rows(transport.list_generations, project) or []:
        for variant in paged_rows(transport.list_variants, generation['generation_id']) or []:
            if variant.get('object_id') == digest:
                matches.append({'asset': variant['variant_id'],
                                'generation_id': generation['generation_id'],
                                'variant_id': variant['variant_id']})
    return matches[0] if len(matches) == 1 else None


def _selection(session, bundle, source):
    from astrid.core.media import MediaProbeError
    from astrid.sdk.authoring_media import _metadata
    from astrid.sdk.media_handles import resolve_media_handle
    from astrid.sdk.pagination import paged_rows
    from astrid.sdk.timeline_media_replacement import _plain, _verified

    transport, project = session.client._remote._transport, session.project_id
    if isinstance(source, Mapping):
        return _verified(transport, project, source)
    path = Path(source).expanduser()
    if path.is_file():
        try:
            metadata = _metadata(path)
        except (MediaProbeError, OSError, ValueError) as exc:
            raise TimelineEditError(f'invalid local media {str(path)!r}: {exc}; '
                                    'use a readable image, video, or audio file') from exc
        if metadata['media_type'].split('/')[0] not in ('image', 'video', 'audio'):
            raise TimelineEditError('media layers require an image, video, or audio file')
        payload = path.read_bytes()
        if 'sha256:' + hashlib.sha256(payload).hexdigest() != metadata['digest']:
            raise TimelineEditError('local media changed during import; retry after the file is saved')
        imported = _plain(transport.import_project_media(
            project, payload, media_type=metadata['media_type'], filename=metadata['filename'],
            expected_digest=metadata['digest'],
            idempotency_key='timeline-media-' + hashlib.sha256(request_bytes(metadata)).hexdigest(),
            **{key: metadata[key] for key in ('width', 'height', 'duration_seconds') if key in metadata},
        ))
        selectors = {'asset': imported['variant_id'], 'generation_id': imported['generation_id'],
                     'variant_id': imported['variant_id']}
    else:
        entry = State(bundle).registry('parent').get('assets', {}).get(source)
        if isinstance(entry, Mapping) and entry.get('generationId') and entry.get('variantId'):
            selectors = {'asset': source, 'generation_id': entry['generationId'],
                         'variant_id': entry['variantId']}
        else:
            descriptor, _ = resolve_media_handle(session.client, project, source)
            matches = []
            for generation in paged_rows(transport.list_generations, project) or []:
                for variant in paged_rows(transport.list_variants, generation['generation_id']) or []:
                    if variant.get('object_id') == descriptor['digest']:
                        matches.append({'asset': variant['variant_id'],
                                        'generation_id': generation['generation_id'],
                                        'variant_id': variant['variant_id']})
            if len(matches) != 1:
                raise TimelineEditError('media handle needs one exact catalog variant; use a local file '
                                        'or an existing timeline asset key')
            selectors = matches[0]
    pin = _plain(transport.get_project_media_selection(project, selectors))
    return _verified(transport, project, pin)


def _placement_commands(bundle, pin, verified, *, on, duration, track, clip_id, values=None,
                        asset_alias=None):
    """Build the shared native track and media placement recipe."""
    kind = verified['media_type'].split('/', 1)[0]
    if kind not in ('image', 'video', 'audio'):
        raise TimelineEditError('verified media must be an image, video, or audio file')
    state_view = State(bundle)
    if track is None:
        track = 'V1'
        if kind == 'audio':
            lane = next((candidate for candidate in state_view.collection('parent', 'track')
                         if candidate.get('kind') == 'audio' and candidate.get('role', 'output') == 'output'
                         and active(candidate)), None)
            if lane is not None:
                track = lane['id']
            else:
                existing = {row.get('id') for row in state_view.collection('parent', 'track')}
                suffix = 1
                while f'A{suffix}' in existing:
                    suffix += 1
                track = f'A{suffix}'
    lane = state_view.entity('parent/track:' + track, optional=True)
    expected_lane = 'audio' if kind == 'audio' else 'visual'
    if lane is not None and (lane.get('kind') != expected_lane or lane.get('role', 'output') != 'output'):
        suggestion = 'A1' if kind == 'audio' else 'V1'
        raise TimelineEditError(f'track {track!r} is not an {expected_lane} output track; use --track {suggestion}')
    if lane is not None and not active(lane):
        suggestion = 'A1' if kind == 'audio' else 'V1'
        raise TimelineEditError(f'track {track!r} is not active; use --track {suggestion}')
    row = {'id': clip_id or 'clip-' + uuid.uuid4().hex,
           'clipType': 'audio' if kind == 'audio' else 'media', 'track': track,
           'asset': asset_alias or pin['asset'], 'at': on, 'hold': duration, **dict(values or {})}
    if kind in ('video', 'audio'):
        row.update({'from': 0, 'to': duration, 'speed': 1})
    commands = []
    if lane is None:
        commands.append({'schema_version': 1, 'op': 'track.create', 'version': '1',
                         'select': 'parent', 'args': {'row': {'id': track, 'kind': expected_lane, 'label': track}}})
    commands.append({'schema_version': 1, 'op': 'clip.add-media', 'version': '1',
                     'select': 'parent', 'args': {'row': row, 'media': pin}})
    return commands


def add_media(session, source, *, on, duration, track, clip_id, values,
              idempotency_key, dry_run, via):
    """Capture once, including IDs/import selection, then retry the original request.

    The editor's empty drop authors a parent clip, not a synthetic shot. A missing
    output track and the clip are one commit_many batch. Catalog import is the
    existing independent managed-media operation, never a timeline publication.
    """
    on, duration = number(on, 'on'), number(duration, 'duration', positive=True)
    if on < 0:
        raise TimelineEditError('on must not be negative')
    if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key.strip()):
        raise TimelineEditError('idempotency key must be nonempty')
    if track is not None and (not isinstance(track, str) or not track.strip()):
        raise TimelineEditError('media track must be a nonempty track ID')
    if {'id', 'clipType', 'track', 'asset', 'at', 'hold', 'from', 'to', 'speed'}.intersection(values or {}):
        raise TimelineEditError('media identity and timing must use the add_media arguments')
    intent = {'source': dict(source) if isinstance(source, Mapping) else str(source),
              'on': on, 'duration': duration, 'track': track,
              'clip_id': clip_id, 'values': dict(values or {})}
    with session.outbox.locked():
        state = session._state()
    previous = state.get('acknowledged', {}).get(idempotency_key)
    if previous is not None:
        return session.retry(idempotency_key, matches=lambda _: previous.get('media_input') == intent,
                             dry_run=dry_run, via=via)
    if state['pending'] or state['flight'] is not None:
        if state.get('media_input') != intent:
            raise TimelineEditError('pending intent differs; retry the original edit and key first')
        return session.flush(idempotency_key=idempotency_key, dry_run=dry_run, via=via)

    bundle = session._bundle(state)
    local_source = not isinstance(source, Mapping) and Path(source).expanduser().is_file()
    pin, verified = _selection(session, bundle, source)
    commands = _placement_commands(bundle, pin, verified, on=on, duration=duration,
                                   track=track, clip_id=clip_id, values=values)
    # Capture/validate the entire batch before persisting its input descriptor.
    from .commands import execute

    commands = execute(bundle, commands, verified_media=[verified]).commands
    imports = ([{
        'object_id': pin['object_id'],
        'generation_id': pin['generation_id'],
        'variant_id': pin['variant_id'],
        'catalog_write': True,
        'retained_on_undo': True,
    }] if local_source else [])
    with session.outbox.locked():
        if session._state() != state:
            raise TimelineEditError('outbox changed during capture; retry the original edit first')
        state['media_input'] = copy.deepcopy(intent)
        if imports:
            state['media_imports'] = copy.deepcopy(imports)
        else:
            state.pop('media_imports', None)
        session.outbox.write(state)
    return session.commit_many(commands, idempotency_key=idempotency_key, dry_run=dry_run,
                               via=via, verified_media=[verified], label='Add media')
