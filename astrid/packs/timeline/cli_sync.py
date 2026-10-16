"""Status watch mode: resolve scope once, then one compact read per observation."""
from __future__ import annotations

import json
import time

from astrid.core.cli.domain_output import DomainResult, envelope_dict
from astrid.sdk.timeline import store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.revisions import RuntimeRevisions
from astrid.sdk.timeline.sync import status_fields


def sync_lines(fields):
    def value(key, state):
        return str(fields[key]) if fields[state] == "known" else fields[state]
    return [
        f"checkout base: {value('base_revision', 'base_state')}",
        f"observed head: {fields['observed_head'] or 'absent'} (publication {fields['observed_publication_id'] or 'unknown'})",
        f"accepted revision: {value('accepted_revision', 'accepted_state')}",
        f"unimported external revisions: {value('unimported_external_revisions', 'external_state')}",
        f"conflicts: {value('conflicts', 'conflict_state')}",
        *[f"choice: {choice}" for choice in fields['choices']],
        *([f"sync: {fields['sync_error']}"] if fields['sync_error'] else []),
    ]


def watch_status(parsed):
    service = parsed.client.timelines
    if not callable(getattr(service, 'head_canonical', None) or getattr(service, 'head', None)):
        raise TimelineEditError("status --watch requires the compact Runtime head API")
    resolve = getattr(service, 'resolve_scope', None)
    if callable(resolve):
        scope = resolve(parsed.project, parsed.ref)
        if not scope.ok:
            raise TimelineEditError(f"could not resolve timeline scope: {scope.error}")
        project, timeline = scope.data['project_id'], scope.data['timeline_id']
    else:
        project, timeline, _head = store.resolve_ids(parsed.project, parsed.ref, client=parsed.client)
    name = parsed.draft or store.current_draft(project, timeline)
    path = store.draft_path(project, timeline, name)
    revisions = RuntimeRevisions(service)
    deadline = time.monotonic() + parsed.timeout_seconds
    try:
        while True:
            head = revisions.head_state(project, timeline)
            checkout = Checkout.load(path) if path.is_file() else None
            fields = status_fields(checkout, head, compact=True)
            data = dict(timeline_id=timeline, current_head=head['revision_id'], draft=name,
                        working_copy='present' if checkout is not None else 'absent', **fields)
            if getattr(parsed, 'text', False):
                print('\n'.join(sync_lines(fields)), flush=True)
            else:
                print(json.dumps(envelope_dict(DomainResult.success(data)), sort_keys=True), flush=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 0
            time.sleep(min(parsed.poll_seconds, remaining))
            if time.monotonic() >= deadline:
                return 0
    except KeyboardInterrupt:
        return 130
