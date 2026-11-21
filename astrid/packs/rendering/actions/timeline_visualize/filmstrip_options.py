"""Public grammar for rendered continuity inspection, shared by SDK and runner."""
from __future__ import annotations

import math
import re
from typing import Any, Mapping

from .inspection_contract import inspection_options


def seconds(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError('time must be seconds or [HH:]MM:SS')
    try:
        parts = str(value).split(':')
        if len(parts) > 3:
            raise ValueError
        numbers = [float(part) for part in parts]
        if any(not math.isfinite(n) or n < 0 for n in numbers):
            raise ValueError
        if len(numbers) > 1 and any(n >= 60 for n in numbers[1:]):
            raise ValueError
        result = 0.0
        for number in numbers:
            result = result * 60 + number
    except (TypeError, ValueError):
        raise ValueError('time must be seconds or [HH:]MM:SS') from None
    if not math.isfinite(result) or result < 0:
        raise ValueError('time must be finite and non-negative')
    return result


def resolution(value: Any) -> list[int] | None:
    """Normalize an optional exact thumbnail resolution the executor consumes."""
    if value in (None, ''):
        return None
    if isinstance(value, str):
        match = re.fullmatch(r'([1-9][0-9]*)x([1-9][0-9]*)', value.strip().lower())
        if not match:
            raise ValueError('resolution must be WIDTHxHEIGHT')
        width, height = (int(part) for part in match.groups())
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        width, height = value
        if type(width) is not int or type(height) is not int:
            raise ValueError('resolution must be WIDTHxHEIGHT')
    else:
        raise ValueError('resolution must be WIDTHxHEIGHT')
    if not 16 <= width <= 4096 or not 16 <= height <= 4096:
        raise ValueError('resolution dimensions must be between 16 and 4096')
    return [width, height]


REVIEW_RESOLUTION = (480, 270)
MOTION_FRAME_BUDGET = 60
MOTION_FRAME_BUDGET_MAX = 120


def layer_names(value: Any) -> list[str]:
    """``--layer a,b`` (repeatable) as a list of names; validated against the registry by the executor."""
    if value in (None, '', []):
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    names = [part.strip().lower() for item in items for part in str(item).split(',') if part.strip()]
    for name in names:
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', name):
            raise ValueError(f'layer name {name!r} is not a short word')
    return names


def rules_value(value: Any) -> dict[str, Any] | None:
    """Project rules the client resolved (``{params, severity, disable, path}``), as JSON text or a mapping."""
    if value in (None, ''):
        return None
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError('rules must be JSON: {"params": {...}, "severity": {...}, "disable": [...]}') from None
    if not isinstance(value, Mapping):
        raise ValueError('rules must be an object')
    params = value.get('params') or {}
    severity = value.get('severity') or {}
    disable = value.get('disable') or []
    if not isinstance(params, Mapping) or not isinstance(severity, Mapping) or not isinstance(disable, list):
        raise ValueError('rules needs params (object), severity (object) and disable (list)')
    return {'params': {str(k): v for k, v in params.items() if isinstance(v, (int, float)) and not isinstance(v, bool)},
            'severity': {str(k): str(v) for k, v in severity.items()},
            'disable': [str(name) for name in disable], 'path': str(value.get('path') or '')}


def cut_numbers(value: Any) -> list[int]:
    """``--cuts 4,5,19`` as a sorted list of positive cut numbers."""
    if value in (None, '', []):
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).split(',')
    numbers = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if not text.isdigit() or int(text) < 1:
            raise ValueError(f'cuts must be positive cut numbers (got {text!r})')
        numbers.append(int(text))
    if len(numbers) > 120:
        raise ValueError('at most 120 cuts')
    return sorted(set(numbers))


def beats_value(value: Any) -> dict[str, Any] | None:
    """Music beats as ``{beats, downbeats, hits}`` in cue seconds (a beats.json, already read by the client)."""
    if value in (None, ''):
        return None
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError('beats must be the JSON of a beats.json (beats, downbeats, hits)') from None
    if not isinstance(value, Mapping):
        raise ValueError('beats must be an object with beats/downbeats/hits')
    out: dict[str, Any] = {}
    for key in ('beats', 'downbeats'):
        items = value.get(key) or []
        if not isinstance(items, list) or len(items) > 5000:
            raise ValueError(f'beats.{key} must be a list of seconds')
        out[key] = [float(item) for item in items if isinstance(item, (int, float)) and not isinstance(item, bool)]
    hits = value.get('hits') or []
    if not isinstance(hits, list) or len(hits) > 2000:
        raise ValueError('beats.hits must be a list')
    out['hits'] = [{'t': float(hit['t']), 'kind': str(hit.get('kind') or 'hit')[:16]}
                   for hit in hits if isinstance(hit, Mapping) and isinstance(hit.get('t'), (int, float))]
    return out


def _render_every(value: Any) -> float:
    """Seconds between frames sampled from the rendered video (default 5, 0.5 to 60)."""
    if value in (None, ''):
        return 5.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.5 <= value <= 60:
        raise ValueError('render_every must be a number of seconds from 0.5 to 60')
    return float(value)


def title_value(value: Any) -> str | None:
    """The timeline's display name for page headers (the client reads it from the Runtime)."""
    if value in (None, ''):
        return None
    if not isinstance(value, str):
        raise ValueError('title must be text')
    text = ' '.join(value.split())
    return text[:120] or None


SHOT_PLAN_ROWS_MAX = 400


def shot_plans_value(value: Any) -> dict[str, Any] | None:
    """Client-resolved shot plan rows: ``{labels: bool, rows: [{occurrence_id, start, end, label, line}]}``.

    ``line`` marks a planned line (silence there is flagged); ``labels`` draws ``label`` on the tiles."""
    if value in (None, ''):
        return None
    if isinstance(value, str):
        import json
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError('shot_plans must be JSON: {"labels": true, "rows": [...]}') from None
    if not isinstance(value, Mapping) or not isinstance(value.get('rows', []), list):
        raise ValueError('shot_plans must be an object with a rows list')
    rows = []
    for row in value.get('rows', [])[:SHOT_PLAN_ROWS_MAX]:
        if not isinstance(row, Mapping):
            raise ValueError('each shot_plans row must be an object')
        try:
            start, end = float(row.get('start')), float(row.get('end'))
        except (TypeError, ValueError):
            raise ValueError('each shot_plans row needs numeric start and end seconds') from None
        rows.append({'occurrence_id': str(row.get('occurrence_id') or ''), 'start': start, 'end': end,
                     'label': str(row.get('label') or '')[:120], 'line': bool(row.get('line'))})
    return {'labels': bool(value.get('labels')), 'rows': rows}


def filmstrip_options(values: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only public controls; never accept an unbounded sampling job."""
    # Component/target grammar is shared with SDK admission. Keep the
    # normalized fields alongside the resolved contract so the executor and
    # the public CLI describe the same filmstrip request.
    shared = inspection_options(values)
    view = values.get('view') or 'filmstrip'
    if view not in {'filmstrip', 'contact', 'motion'}:
        raise ValueError('view must be filmstrip (paired drill-down), contact (one overview page) '
                         'or motion (one cut: frames, onion skins, curves, sync)')
    contact = view == 'contact'
    motion = view == 'motion'
    every, frames = values.get('every'), values.get('every_frames')
    preset = values.get('preset') or None
    if motion:
        from .motion.window import WINDOW_PRESETS

        windows = [name for name in ('cut', 'range', 'at', 'frame') if values.get(name) not in (None, '')]
        if not windows:
            raise ValueError('choose a window: --cut N, --range A..B, --at T (centred) or --frame N; '
                             'presets: --preset scan|motion|beat|frame|cut')
        if len(windows) > 1:
            raise ValueError(f'choose one window, not {" and ".join("--" + w for w in windows)}')
        if preset is not None and preset not in WINDOW_PRESETS:
            raise ValueError(f'preset {preset!r} is not a window preset; window presets: {", ".join(WINDOW_PRESETS)}')
        if values.get('sample') not in (None, '', 'motion'):
            raise ValueError('--sample belongs to the filmstrip view; window views sample by preset (--every to override)')
    # An overview samples one frame per cut unless a density was asked for.
    default_sample = 'interval' if (every is not None or frames is not None) else ('cuts' if contact else 'interval')
    sample = 'motion' if motion else (values.get('sample') or default_sample)
    if sample not in {'interval', 'clips', 'shots', 'cuts', 'motion'}:
        raise ValueError('sample must be interval, clips, shots, or cuts')
    if every is not None and frames is not None:
        raise ValueError('choose every seconds or every_frames, not both')
    if frames is not None and (type(frames) is not int or frames < 1):
        raise ValueError('every_frames must be a positive integer')
    if every is not None and (isinstance(every, bool) or not isinstance(every, (float, int))
                              or not math.isfinite(every) or every <= 0):
        raise ValueError('every must be a finite positive number of seconds')
    if sample not in ('interval', 'motion') and (every is not None or frames is not None):
        raise ValueError('every/every_frames apply only to sample=interval')
    # Keep the distinction between an omitted density (which requests the
    # bounded adaptive overview) and an explicit interval request.  Applying
    # the 0.5s default before planning used to make ``--every 0.5`` look like
    # the overview and then injected unrelated boundary/beat frames.
    explicit_interval = every is not None or frames is not None
    include_cuts = values.get('include_cuts', False)
    if include_cuts is None:
        include_cuts = False
    if not isinstance(include_cuts, bool):
        raise ValueError('include_cuts must be a boolean')
    if include_cuts and sample != 'interval':
        raise ValueError('include_cuts applies only to sample=interval')
    normalized_every = every if every is not None else (None if frames else 0.5)
    density = ({'mode': 'every_frames', 'value': frames}
               if frames is not None else {'mode': 'every_seconds', 'value': normalized_every})
    result: dict[str, Any] = {'view': view, 'sample': sample, 'every': normalized_every,
                              'every_frames': frames, 'density': density,
                              'explicit_interval': explicit_interval,
                              'include_cuts': include_cuts,
                              'max_frames': 2000,
                              'include_media': bool(values.get('include_media', False))}
    components = list(shared['components']['resolved'])
    if contact or motion:
        # The overview and the motion sheet are output plus text; input lanes
        # belong to the paired view.
        components = [name for name in components if name != 'inputs']
    result.update({
        'components': components,
        'component_request': shared['components'],
        'track_ids': shared['tracks'],
        'detail': shared['detail'],
        'input_window': shared['window'],
        # Occurrence is the canonical placement selector. Keep it distinct
        # from shot/clip aliases so repeated shots remain addressable.
        'occurrence': shared['occurrence'],
        # Render review (contact view with a render): seconds between sampled frames.
        'render_every': _render_every(values.get('render_every')),
    })
    # Keep omission distinguishable from an explicit page-size override. The
    # paired renderer uses that distinction to make the normal input+output
    # view one row wide while still allowing a caller to request denser pages.
    result['page_size_explicit'] = values.get('page_size') is not None
    columns_default, columns_max = (10, 12) if contact else ((8, 16) if motion else (5, 8))
    for name, default, maximum in [('columns', columns_default, columns_max), ('page_size', 50, 100)]:
        n = values.get(name, default)
        if n is None:
            n = default
        if type(n) is not int or not 1 <= n <= maximum:
            raise ValueError(f'{name} must be an integer between 1 and {maximum}')
        result[name] = n
    window, at, frame = values.get('range'), values.get('at'), values.get('frame')
    if frame is not None:
        if type(frame) is not int or frame < 0:
            raise ValueError('frame must be a non-negative integer')
        if window is not None or at is not None:
            raise ValueError('choose frame, range, or at, not more than one')
    if window is not None and at is not None:
        raise ValueError('choose range or at, not both')
    if window is not None:
        if isinstance(window, str):
            window = window.split('..')
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            raise ValueError('range must be START..END')
        start, end = map(seconds, window)
        if end <= start:
            raise ValueError('range end must follow its start')
        result['range'] = [start, end]
    if at is not None:
        result['at'] = seconds(at)
    result['frame'] = frame
    result.setdefault('range', None)
    result.setdefault('at', None)
    # Review scale by default for the views that capture many frames: a
    # full-canvas contact sheet or motion sheet costs disk and capture time
    # and can exceed the settlement size limit, and 480x270 is what the page shows.
    result['resolution'] = resolution(values.get('resolution')) or (list(REVIEW_RESOLUTION) if contact or motion else None)
    if motion:
        from .motion.window import preset_settings

        name = preset or ('cut' if values.get('cut') not in (None, '') else 'scan')
        settings = preset_settings(name)
        result['preset'] = name
        result['cut'] = str(values.get('cut')).strip() if values.get('cut') not in (None, '') else None
        result['size_explicit'] = values.get('resolution') not in (None, '')
        if not result['size_explicit']:
            result['resolution'] = list(settings['size'])
        result['columns_explicit'] = values.get('columns') is not None
        if not result['columns_explicit']:
            result['columns'] = settings['columns']
        result['window_s'] = values.get('window_s')
        budget = values.get('frame_budget', None)
        if budget in (None, ''):
            budget = settings['budget']
        if type(budget) is not int or not 1 <= budget <= MOTION_FRAME_BUDGET_MAX:
            raise ValueError(f'frame_budget must be an integer between 1 and {MOTION_FRAME_BUDGET_MAX}')
        result['frame_budget'] = budget
        if values.get('layers') in (None, '', []) and settings.get('layers'):
            values = {**values, 'layers': list(settings['layers'])}
    preview = values.get('preview') or False
    if not isinstance(preview, bool):
        raise ValueError('preview must be a boolean')
    if preview and not motion:
        raise ValueError('--preview applies to --view motion (an animated GIF of that cut for humans)')
    result['preview'] = preview
    result['layers'] = layer_names(values.get('layers'))
    # one layer to outline over the frames (a clip id, from `--highlight c41.tool-16`); implies bounds
    highlight = values.get('highlight')
    result['highlight'] = str(highlight) if highlight not in (None, '') else None
    if result['highlight'] and 'bounds' not in (result['layers'] or ()):
        result['layers'] = tuple(result['layers'] or ()) + ('bounds',)
    result['rules'] = rules_value(values.get('rules'))
    result['cuts'] = cut_numbers(values.get('cuts'))
    if result['cuts'] and not contact:
        raise ValueError('--cuts applies to --view contact (tiles for only those cut numbers)')
    result['beats'] = beats_value(values.get('beats'))
    result['title'] = title_value(values.get('title'))
    result['shot_plans'] = shot_plans_value(values.get('shot_plans'))
    result['request'] = {
        'range': result['range'],
        'at': result['at'],
        'density': density,
        'resolution': result['resolution'],
    }
    if result['frame'] is not None:
        result['request']['frame'] = result['frame']
    if result['occurrence'] is not None:
        result['request']['occurrence'] = result['occurrence']
    context = values.get('context', 3.0)
    result['context'] = seconds(3.0 if context is None else context)
    if at is not None and result['context'] <= 0:
        raise ValueError('context must be positive when focusing a timestamp')
    for name in ('clip', 'shot', 'asset'):
        if values.get(name) not in (None, ''):
            if not isinstance(values[name], str):
                raise ValueError(f'{name} must be an identifier')
            result[name] = values[name]
    return result
