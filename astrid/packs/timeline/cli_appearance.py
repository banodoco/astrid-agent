"""Guarded appearance edits and package-backed inspection."""
from __future__ import annotations

import json
import re


def command(parsed):
    from astrid.sdk.contracts import DomainResult, ErrorObject
    from astrid.sdk.timeline._appearance import canvas, package_query
    from astrid.sdk.timeline._command_state import State
    from astrid.sdk.timeline.sync import TimelineSession
    from astrid.packs.timeline.cli_native import _commit_envelope, _render_commit_result
    from astrid.pack_api import print_result

    try:
        ref = parsed.clip or parsed.track
        kind = 'clip' if parsed.clip else 'track'
        if not re.fullmatch(rf'(?:parent|occurrence:[^/]+)/{kind}:[^/]+', ref):
            raise ValueError(f'--{kind} requires an exact parent or occurrence {kind} reference')
        values = {}
        for item in parsed.set_values:
            key, sep, raw = item.partition('=')
            if not sep or key in values:
                raise ValueError('--set requires unique FIELD=VALUE entries')
            try:
                values[key] = json.loads(raw)
            except json.JSONDecodeError:
                values[key] = raw
        allowed = ({'fit', 'x', 'y', 'width', 'height', 'cropTop', 'cropBottom', 'cropLeft', 'cropRight', 'opacity'}
                   if kind == 'clip' else {'fit', 'scale', 'opacity', 'blendMode'})
        if (set(values) | set(parsed.clear)) - allowed:
            raise ValueError('unsupported appearance property: ' + ', '.join(sorted((set(values) | set(parsed.clear)) - allowed)))
        session = TimelineSession.open(parsed.project, parsed.ref, client=parsed.client)
        if values or parsed.clear:
            operation = {'schema_version': 1, 'op': kind + '.set', 'version': '1', 'select': ref,
                         'args': {'values': values, 'clear': parsed.clear}}
            result = session.commit(operation, idempotency_key=parsed.idempotency_key,
                                    dry_run=parsed.dry_run, via='cli', merge='atomic')
            shown = _commit_envelope(result, result['idempotency_key'])
            return print_result(shown, as_json=parsed.json,
                                human_renderer=lambda value: _render_commit_result(value, dry_run=parsed.dry_run))
        state = State(session._bundle(session._state()))
        owner = ref.rsplit('/', 1)[0]
        config = state.config(owner)
        size = canvas(config, ref)
        normalized = package_query({'operation': 'normalize', 'config': {**config,
            'clips': state.collection(owner, 'clip'), 'tracks': state.collection(owner, 'track')}})
        if kind == 'track':
            row = next(row for row in normalized['config']['tracks'] if row.get('id') == ref.rsplit('/', 1)[1].split(':', 1)[1])
            answer = {'track': row, 'migration': normalized['findings']}
        else:
            clip = next(row for row in normalized['config']['clips'] if row.get('id') == ref.rsplit('/', 1)[1].split(':', 1)[1])
            track = next(row for row in normalized['config']['tracks'] if row.get('id') == clip.get('track'))
            asset = state.registry(owner).get('assets', {}).get(clip.get('asset'), {})
            resolution = asset.get('resolution', '')
            match = re.fullmatch(r'([1-9][0-9]*)[xX]([1-9][0-9]*)', str(resolution))
            if not match:
                raise ValueError(f"clip {clip.get('id')} property asset.resolution is missing or invalid; record the media resolution")
            intrinsic = {'width': int(match[1]), 'height': int(match[2])}
            answer = {'placement': package_query({'operation': 'resolve', 'input': {
                'clip': clip, 'track': track, 'canvas': size, 'intrinsic': intrinsic}}),
                'migration': normalized['findings']}
        return print_result(DomainResult.success(answer), as_json=parsed.json,
                            human_renderer=lambda value: json.dumps(value, indent=2, ensure_ascii=False))
    except Exception as exc:
        return print_result(DomainResult.failure(ErrorObject(getattr(exc, 'code', 'timeline_edit_error'), str(exc), {})),
                            as_json=parsed.json)
