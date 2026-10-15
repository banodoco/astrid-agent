"""Shared CLI adapter for V3 contributed read-only presentations."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from astrid.core.cli.domain_output import print_result
from astrid.core.pack.views import (
    TimelineReadModels,
    UnsupportedView,
    discover_views,
    read_view,
    render_view_text,
    resolve_view,
)
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.timeline._common import TimelineEditError


def contributed_view(parsed, identity=None):
    as_json = bool(getattr(parsed, 'json', False))
    try:
        if getattr(parsed, 'list_views', False):
            views = [view.to_json() for view in discover_views()]
            if as_json:
                return print_result(DomainResult.success(dict(views=views)), as_json=True)
            for view in views:
                d = view['descriptor']
                print(f"{view['identity']} · {d['label']} · {d['description']}")
            return 0
        view = resolve_view(identity)
        timelines = getattr(parsed.client, 'timelines', None)
        opener = getattr(timelines, 'open_bundle', None)
        if not callable(opener):
            raise UnsupportedView('unsupported view host: exact-revision bundle reads unavailable')
        project = getattr(parsed, 'project', None)
        timeline = getattr(parsed, 'ref', None) or getattr(parsed, 'timeline_slug', None) or getattr(parsed, 'timeline_ref', None)
        requested = getattr(parsed, 'revision_id', None)
        opened = opener(project, timeline, revision_id=requested)
        if not opened.ok:
            return print_result(opened, as_json=as_json)
        data = opened.data
        if not isinstance(data, Mapping) or not all(isinstance(data.get(k), str) and data[k]
                    for k in ('project_id', 'timeline_id', 'revision_id')):
            raise TimelineEditError('view host omitted project/timeline/revision identity')
        if requested is not None and requested != data['revision_id']:
            raise TimelineEditError('view host returned a different revision')
        bundle = data.get('bundle')
        if not isinstance(bundle, Mapping) or any(bundle.get(k) != data[k] for k in ('project_id', 'timeline_id')) \
                or (bundle.get('base_parent') or {}).get('revision_id') != data['revision_id']:
            raise TimelineEditError('view host bundle identity mismatch')
        # Resolve aliases at the established Runtime boundary; only its returned
        # canonical IDs reach the producer's second, exact-revision read.
        expected_digest = hashlib.sha256(json.dumps(dict(bundle), sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        result = read_view(view, project=data['project_id'], timeline=data['timeline_id'],
                           revision_id=data['revision_id'], host=TimelineReadModels(parsed.client))
        if result["data"]["content_digest"] != expected_digest:
            raise TimelineEditError("view host changed the content of the pinned revision")
        if as_json:
            return print_result(DomainResult.success(result), as_json=True)
        print(render_view_text(result))
        return 0
    except (UnsupportedView, TimelineEditError, ValueError) as exc:
        return print_result(DomainResult.failure(ErrorObject(
            getattr(exc, 'code', 'validation_error'), str(exc), {})), as_json=as_json)
