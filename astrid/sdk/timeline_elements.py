"""Same-placement pack element edits through the existing durable command session."""
from __future__ import annotations

import base64
import copy
import json
import math
import re
import uuid
from types import SimpleNamespace
from typing import Any

from astrid.core.element.project_packs import (
    _json_digest,
    _read_document,
    admitted_element_snapshot_descriptor,
)
from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.documents import Documents
from astrid.sdk.exceptions import ServiceError
from astrid.sdk.remote import RemoteMedia
from astrid.sdk.timeline._command_state import ABSENT, State, equal, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.sync import CommitConflict, TimelineSession, _connection
from astrid.sdk.workspace_client import WorkspaceClientError


def _calls(clip_id, element, duration, params, clear_params, pin=None, before=None):
    target = f'parent/clip:{clip_id}'
    same = before is not None and equal(element, before.get('elementRef') or before.get('element_ref'))
    calls = [] if same else [
        {'schema_version': 1, 'op': 'clip.set-element', 'version': '1', 'select': target,
         'args': {'elementRef': copy.deepcopy(element)}}]
    if same and duration is None and not params and not clear_params:
        duration = before['hold']
    if duration is not None:
        calls.append({'schema_version': 1, 'op': 'clip.set', 'version': '1', 'select': target,
                      'args': {'values': {'hold': duration}, 'clear': []}})
    if params or clear_params:
        calls.append({'schema_version': 1, 'op': 'clip.set-params', 'version': '1', 'select': target,
                      'args': {'values': copy.deepcopy(params or {}), 'clear': list(clear_params)}})
    if pin is not None:
        for call in calls:
            if call['op'] in ('clip.set-element', 'clip.set-params'):
                call['args']['schema_source'] = copy.deepcopy(pin)
    return calls


class _SelectionReads:
    """Adapt one authenticated public history read to the native resolver port."""

    def __init__(self, owner, project, timeline):
        self.owner, self.project, self.timeline = owner, project, timeline
        self.entries = {}

    def history(self, through, *, after_publication_id=None, cursor=None, limit=100):
        if after_publication_id is not None:
            raise ValueError('Selection capture requires complete accepted history')
        result = self.owner.history(self.project, self.timeline, cursor=cursor, limit=limit)
        if not result.ok:
            raise ValueError(result.error.message)
        page = result.data
        self.entries.update((entry['publication_id'], entry) for entry in page['items'])
        return page

    def history_entry(self, publication_id):
        return self.entries[publication_id]

    def revision_closure(self, revision_id):
        for entry in self.entries.values():
            if entry['revision_id'] == revision_id:
                document = entry['receipt']['result'].get('canonical_document')
                if document is not None:
                    return document
        raise ValueError('Accepted selection revision closure is unavailable')


def _read_selected_param_inputs(owner, project, timeline, *, realm, through, select,
                                source=None, element=None):
    """Read the exact accepted PARAM declaration and its retained bytes.

    The accepted publication chain chooses the source pin.  Runtime then
    authenticates that pin and returns its portable snapshot; neither the
    current admission nor a caller-provided schema can replace it.
    """
    from astrid.sdk.timeline._selected_param_authority import resolve_selected_param_source

    if source is None:
        authority = resolve_selected_param_source(
            _SelectionReads(owner, project, timeline),
            context={'request': {'project_id': project, 'timeline_id': timeline}},
            through=through,
            select=select,
        )
    else:
        if not isinstance(source, dict) or not isinstance(element, dict):
            raise ValueError('selected PARAM retry lacks its original pin or element identity')
        authority = {'source': copy.deepcopy(source), 'element': copy.deepcopy(element)}
    pin = authority['source']
    verified = owner._client.get_project_verified_document(project, pin)
    if source is not None:
        admissions = verified.get('document', {}).get('content', {}).get('admissions', [])
        matches = [row.get('snapshot_object') for row in admissions
                   if all(row.get(key) == element.get(key)
                          for key in ('id', 'kind', 'revision'))]
        if len(matches) != 1 or not isinstance(matches[0], dict):
            raise ValueError('pack snapshot unavailable; re-admit pack')
        authority['object'] = copy.deepcopy(matches[0])
    snapshot = owner._client.get_project_verified_document_object(
        project, pin, authority['object']['object_id'])
    descriptor = admitted_element_snapshot_descriptor(
        realm, project, authority['element'], verified, snapshot)
    return authority, [(verified, {authority['object']['object_id']: snapshot})], descriptor


def _admitted_element_inputs(owner, client, project, *, realm, element):
    """Select the current admitted snapshot and its exact project-pack pin."""
    if not isinstance(element, dict) or not element.get('packId'):
        raise ValueError('Element owner/revision is not admitted for this project')
    document_id, document = _read_document(client, realm, project)
    if document is None or document['content']['pack_id'] != element.get('packId'):
        document_id, document = _read_document(client, realm, project, element.get('packId'))
    if document is None or document['content']['pack_id'] != element.get('packId'):
        raise ValueError('Element owner/revision is not admitted for this project')
    admissions = [row for row in document['content']['admissions'] if all(
        row.get(k) == element.get(k) for k in ('id', 'kind', 'revision'))]
    if len(admissions) != 1 or 'snapshot_object' not in admissions[0]:
        raise ValueError('pack snapshot unavailable; re-admit pack')
    pin = {'document_id': document_id, 'version': document['version'],
           'content_digest': _json_digest(document['content'])}
    descriptor = admissions[0]['snapshot_object']
    snapshot = {**descriptor, 'bytes_base64': base64.b64encode(
        client.media.read_bytes(descriptor['object_id'])).decode('ascii')}
    verified = {'pin': pin, 'document': {**document, 'project_id': project}}
    admitted_element_snapshot_descriptor(realm, project, element, verified, snapshot)
    return pin, [(verified, {descriptor['object_id']: snapshot})]


def _matches(saved, calls):
    return len(saved) == len(calls) and all(
        prior['op'] == call['op'] and prior['select'] == call['select']
        and equal({k: v for k, v in prior['args'].items() if k != 'schema_source'}, call['args'])
        for prior, call in zip(saved, calls))


def _clip(bundle, clip_id):
    clips = bundle['parent'].get('clips')
    if not isinstance(clips, list):
        raise ValueError('Canonical parent clips are unavailable')
    matches = [clip for clip in clips if isinstance(clip, dict) and clip.get('id') == clip_id]
    if len(matches) != 1:
        raise ValueError('Expected one existing parent effect clip')
    return copy.deepcopy(matches[0])


def _placement_clip(bundle, target):
    row = State(bundle).entity(target, optional=True)
    if not isinstance(row, dict):
        raise ValueError(f'No clip found at target {target!r}')
    return copy.deepcopy(row)


def _orphan_cleanup(bundle, clip_id, element):
    before = _clip(bundle, clip_id)
    old_ref = before.get('elementRef') or before.get('element_ref')
    if not isinstance(old_ref, dict) or equal(old_ref, element):
        return [], []
    parent = bundle['parent']
    config = parent.get('config')
    app = config.get('app') if isinstance(config, dict) else None
    entries = app.get('elements') if isinstance(app, dict) else None
    if not isinstance(entries, dict) or old_ref.get('id') not in entries:
        return [], []
    clips = parent['clips']
    if any(clip['id'] != clip_id and equal(
            clip.get('elementRef') or clip.get('element_ref'), old_ref) for clip in clips):
        return [], []
    # The scan is a dependency, not authority to prune any other source entry.
    ids = [clip['id'] for clip in clips]
    reads = [{'kind': 'collection', 'ref': 'parent', 'path': ['clips'],
              'members': sorted(ids), 'order': ids}]
    reads.extend({'kind': 'field', 'ref': 'parent', 'path': ['clips', 'id:' + clip['id'], field],
                  'state': present(clip[field]) if field in clip else ABSENT.copy()}
                 for clip in clips for field in ('elementRef', 'element_ref'))
    change = {'op': 'remove', 'ref': 'parent',
              'path': ['config', 'app', 'elements', old_ref['id']],
              'before': present(entries[old_ref['id']])}
    return [{'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
             'args': {'changes': [change]}}], reads


def _result(session, data, receipt, before, clip_id, expected_head, key):
    clip = _clip({'parent': data['canonical_document']['parent_revision']['payload']}, clip_id)
    return DomainResult.success({
        'project_id': session.project_id, 'timeline_id': session.timeline_id,
        'expected_head': expected_head, 'new_head': data['accepted_revision_id'],
        'clip_id': clip_id, 'element': clip.get('elementRef') or clip.get('element_ref'), 'duration': clip.get('hold'),
        'params': clip.get('params', {}), 'before': {
            'element': before.get('elementRef') or before.get('element_ref'),
            'duration': before.get('hold'), 'params': before.get('params', {})},
    }, receipt=CommandReceipt.from_dict(receipt), idempotency_key=key)


def _step(calls):
    """Describe one semantic element edit for the v2 commit envelope.

    Runtime fills native effects; the explicit orphan removal remains part of
    the same guarded edit and its inverse.
    """
    return [{
        'id': 'element-update',
        'label': 'Update element',
        'intent': 'Update the selected element',
        'tags': ['element'],
        'command_ordinals': list(range(len(calls))),
        'patch': [copy.deepcopy(change) for call in calls if call['op'] == 'doc.patch'
                  for change in call['args']['changes']],
    }]


def update_parent_element(owner: Any, project: str, timeline: str, *, realm: str, clip_id: str,
                          element: dict[str, str], expected_head: str, duration: float | None = None,
                          params: dict[str, Any] | None = None, idempotency_key: str | None = None,
                          session: TimelineSession | None = None, clear_params=()):
    """Update one existing effect, recovering an exact flight before reading admission.

    ``session`` optionally selects the caller's existing outbox. Otherwise the
    normal realm/actor/project/timeline outbox is used. ``params`` merges literal
    values; ``clear_params`` explicitly removes named properties.
    """
    key = idempotency_key
    try:
        if not all(isinstance(v, str) and v for v in (project, timeline, realm, clip_id, expected_head)):
            raise ValueError('Explicit project/timeline/realm/clip/expected head are required')
        if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (int, float))
                                     or not math.isfinite(duration) or duration <= 0):
            raise ValueError('Duration must be finite and positive')
        if params is not None and (not isinstance(params, dict)
                                   or len(json.dumps(params, allow_nan=False)) > 16384):
            raise ValueError('Params must be a bounded JSON object')
        if (not isinstance(clear_params, (list, tuple))
                or any(not isinstance(name, str) or not name for name in clear_params)
                or len(set(clear_params)) != len(clear_params)
                or set(clear_params).intersection(params or {})):
            raise ValueError('Clear params must be unique names separate from supplied values')
        client = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner,
                                 documents=Documents(owner._client), media=RemoteMedia(owner._client))
        if _connection(client)['realm_id'] != realm:
            raise ValueError('Project pack realm differs from authenticated Runtime')
        if session is None:
            session = TimelineSession.open(project, timeline, client=client)
        else:
            from astrid.sdk.timeline.store import resolve_ids

            project_id, timeline_id, _ = resolve_ids(project, timeline, client=client)
            if (session.project_id, session.timeline_id, session.identity) != (
                    project_id, timeline_id, _connection(client)):
                raise ValueError('Session belongs to a different realm, actor, project or timeline')
        calls = _calls(clip_id, element, duration, params, clear_params)
        with session.outbox.locked():
            state = session._state()
            key = key or (state['flight']['key'] if state['flight'] else f'element-{uuid.uuid4().hex}')
        # Acknowledged identities belong to Runtime history, never a client cache.
        # Return its original result without recapturing against a newer head/pin.
        cursor = None
        while state['flight'] is None and idempotency_key is not None:
            history = owner.history(session.project_id, session.timeline_id, cursor=cursor, limit=100)
            if not history.ok:
                return history
            for entry in history.data['items']:
                receipt = entry['receipt']
                if receipt['idempotency_key'] == key and entry['publisher'] == session.identity['actor_id']:
                    data = receipt['result']
                    if (receipt['command_kind'] != 'timeline.commit'
                            or data['base_revision_id'] != expected_head):
                        raise ValueError('Acknowledged request identity has different original intent')
                    opened = owner.open_bundle(session.project_id, session.timeline_id, revision_id=expected_head)
                    if not opened.ok:
                        return opened
                    before = _clip(opened.data['bundle'], clip_id)
                    cleanup, _ = _orphan_cleanup(opened.data['bundle'], clip_id, element)
                    alternatives = [calls, _calls(
                        clip_id, element, duration, params, clear_params, before=before)]
                    if not any(_matches(data['commands'], candidate)
                               or _matches(data['commands'], candidate + cleanup)
                               for candidate in alternatives):
                        raise ValueError('Acknowledged request identity has different original intent')
                    return _result(session, data, receipt, before,
                                   clip_id, expected_head, key)
            cursor = history.data['next_cursor']
            if cursor is None:
                break
        if state['base']['revision_id'] != expected_head:
            return DomainResult.failure(ErrorObject(
                'conflict', 'Timeline head changed; inspect before making a fresh edit',
                {'expected_head': expected_head, 'actual_head': state['base']['revision_id']}),
                idempotency_key=key)
        opened = owner.open_bundle(session.project_id, session.timeline_id, revision_id=expected_head)
        if not opened.ok:
            return opened
        before = _clip(opened.data['bundle'], clip_id)
        fresh_calls = _calls(clip_id, element, duration, params, clear_params, before=before)
        cleanup, cleanup_reads = _orphan_cleanup(opened.data['bundle'], clip_id, element)

        def capture(state):
            if state['base']['revision_id'] != expected_head:
                raise ValueError('Session base changed before element capture')
            current = owner.history(session.project_id, session.timeline_id, limit=1)
            if not current.ok:
                raise ValueError(current.error.message)
            if current.data['head']['revision_id'] != expected_head:
                raise CommitConflict({'code': 'conflict', 'message': 'Timeline head changed before element capture',
                                      'details': {'expected_head': expected_head,
                                                  'actual_head': current.data['head']['revision_id']}})
            old_ref = before.get('elementRef') or before.get('element_ref')
            if (not isinstance(old_ref, dict) or old_ref.get('kind') != 'effect'
                    or before.get('clipType') != old_ref.get('id')
                    or isinstance(before.get('hold'), bool) or not isinstance(before.get('hold'), (int, float))
                    or any(k in before for k in ('asset', 'file', 'src', 'source', 'from', 'to'))):
                raise ValueError('Update requires an existing generated hold effect without media source trimming')
            if (not isinstance(element, dict) or element.get('kind') != 'effect'
                    or element.get('id') != old_ref.get('id')
                    or old_ref.get('packId') is not None and old_ref['packId'] != element.get('packId')):
                raise ValueError('Update must preserve the existing effect identity and pack owner')
            if all(call['op'] == 'clip.set' for call in fresh_calls):
                return execute(opened.data['bundle'], fresh_calls, realm_id=realm).commands
            if all(call['op'] != 'clip.set-element' for call in fresh_calls):
                authority, inputs, _ = _read_selected_param_inputs(
                    owner, session.project_id, session.timeline_id, realm=realm,
                    through=state['base'], select=f'parent/clip:{clip_id}')
                pin = authority['source']
                return execute(opened.data['bundle'], _calls(
                    clip_id, element, duration, params, clear_params, pin, before),
                    schema_inputs=inputs, realm_id=realm).commands
            document_id, document = _read_document(client, realm, session.project_id)
            if document is None or document['content']['pack_id'] != element.get('packId'):
                document_id, document = _read_document(client, realm, session.project_id, element.get('packId'))
            if document is None or document['content']['pack_id'] != element.get('packId'):
                raise ValueError('Element owner/revision is not admitted for this project')
            admissions = [row for row in document['content']['admissions'] if all(
                row.get(k) == element.get(k) for k in ('id', 'kind', 'revision'))]
            if len(admissions) != 1 or 'snapshot_object' not in admissions[0]:
                raise ValueError('pack snapshot unavailable; re-admit pack')
            pin = {'document_id': document_id, 'version': document['version'],
                   'content_digest': _json_digest(document['content'])}
            descriptor = admissions[0]['snapshot_object']
            snapshot = {**descriptor, 'bytes_base64': base64.b64encode(
                client.media.read_bytes(descriptor['object_id'])).decode('ascii')}
            inputs = [({'pin': pin, 'document': document}, {descriptor['object_id']: snapshot})]
            return execute(opened.data['bundle'], _calls(
                clip_id, element, duration, params, clear_params, pin) + cleanup,
                schema_inputs=inputs, realm_id=realm).commands

        # A persisted flight is immutable: recognize its old batch shape and
        # retry it verbatim.  New batches use the v2 request envelope with one
        # semantic step, while the existing command executor remains the sole
        # source of element validation, retained inputs and compiler mirrors.
        with session.outbox.locked():
            state = session._state()

        def matches(pending):
            return any(_matches(pending, candidate) or _matches(pending, candidate + cleanup)
                       for candidate in (calls, fresh_calls))

        if state['pending'] or state['flight'] is not None:
            session._stage(capture, matches)
            committed = session.flush(idempotency_key=key)
        else:
            captured = capture(state)
            committed = session.submit(
                captured,
                label='Update element',
                steps=_step(captured),
                merge='fields',
                reads=cleanup_reads if cleanup else None,
                idempotency_key=key,
                expected_state=state,
                base=state['base'],
                evaluation={'kind': 'label', 'count': 0},
            )
        return _result(session, committed['data'], committed['receipt'], before, clip_id, expected_head, key)
    except CommitConflict as exc:
        failure = exc.failure
        return DomainResult.failure(ErrorObject(failure['code'], failure['message'], failure['details']),
                                    idempotency_key=key or f'element-{uuid.uuid4().hex}')
    except WorkspaceClientError as exc:
        return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details),
                                    idempotency_key=key or f'element-{uuid.uuid4().hex}')
    except ServiceError as exc:
        return DomainResult.failure(exc.to_error_object(),
                                    idempotency_key=key or f'element-{uuid.uuid4().hex}')
    except (ValueError, KeyError, TypeError) as exc:
        return DomainResult.failure(ErrorObject('validation_error', str(exc), {}),
                                    idempotency_key=key or f'element-{uuid.uuid4().hex}')


def place_element(owner: Any, project: str, timeline: str, *, realm: str, target: str,
                  element: dict[str, str], expected_head: str,
                  idempotency_key: str | None = None,
                  session: TimelineSession | None = None):
    """Place an admitted project-pack element on an occurrence shot clip.

    First placement supports effect elements on ``am-discord`` clips. If the
    target already has an element ref, the native command keeps the same id,
    kind, and pack owner as the existing element-update route.
    """
    key = idempotency_key
    try:
        if not all(isinstance(v, str) and v for v in (project, timeline, realm, target, expected_head)):
            raise ValueError('Explicit project/timeline/realm/target/expected head are required')
        if not re.fullmatch(r'occurrence:[^/]+/clip:[^/]+', target) or '::' in target:
            raise ValueError('Target must be an occurrence shot clip: occurrence:<occurrence-id>/clip:<clip-id>')
        if (not isinstance(element, dict)
                or set(element) != {'packId', 'id', 'kind', 'revision'}
                or any(not isinstance(element[name], str) or not element[name]
                       for name in element)):
            raise ValueError('Element must be an owner-qualified admitted reference')
        client = SimpleNamespace(_remote=SimpleNamespace(_transport=owner._client), timelines=owner,
                                 documents=Documents(owner._client), media=RemoteMedia(owner._client))
        if _connection(client)['realm_id'] != realm:
            raise ValueError('Project pack realm differs from authenticated Runtime')
        if session is None:
            session = TimelineSession.open(project, timeline, client=client)
        else:
            from astrid.sdk.timeline.store import resolve_ids

            project_id, timeline_id, _ = resolve_ids(project, timeline, client=client)
            if (session.project_id, session.timeline_id, session.identity) != (
                    project_id, timeline_id, _connection(client)):
                raise ValueError('Session belongs to a different realm, actor, project or timeline')
        calls = [{'schema_version': 1, 'op': 'clip.set-element', 'version': '1',
                  'select': target, 'args': {'elementRef': copy.deepcopy(element)}}]
        with session.outbox.locked():
            state = session._state()
            key = key or (state['flight']['key'] if state['flight'] else f'element-place-{uuid.uuid4().hex}')

        cursor = None
        while state['flight'] is None and idempotency_key is not None:
            history = owner.history(session.project_id, session.timeline_id, cursor=cursor, limit=100)
            if not history.ok:
                return history
            for entry in history.data['items']:
                receipt = entry['receipt']
                if receipt['idempotency_key'] == key and entry['publisher'] == session.identity['actor_id']:
                    data = receipt['result']
                    if (receipt['command_kind'] != 'timeline.commit'
                            or data['base_revision_id'] != expected_head):
                        raise ValueError('Acknowledged request identity has different original intent')
                    opened = owner.open_bundle(session.project_id, session.timeline_id, revision_id=expected_head)
                    if not opened.ok:
                        return opened
                    before = _placement_clip(opened.data['bundle'], target)
                    if ('elementRef' not in before and 'element_ref' not in before
                            and element['kind'] != 'effect'):
                        raise ValueError('First placement supports effect elements only')
                    if not _matches(data['commands'], calls):
                        raise ValueError('Acknowledged request identity has different original intent')
                    return _placement_result(session, data, receipt, before, target, expected_head, key)
            cursor = history.data['next_cursor']
            if cursor is None:
                break
        if state['base']['revision_id'] != expected_head:
            return DomainResult.failure(ErrorObject(
                'conflict', 'Timeline head changed; inspect before making a fresh edit',
                {'expected_head': expected_head, 'actual_head': state['base']['revision_id']}),
                idempotency_key=key)
        opened = owner.open_bundle(session.project_id, session.timeline_id, revision_id=expected_head)
        if not opened.ok:
            return opened
        before = _placement_clip(opened.data['bundle'], target)
        if ('elementRef' not in before and 'element_ref' not in before
                and element['kind'] != 'effect'):
            raise ValueError('First placement supports effect elements only')

        def capture(state):
            if state['base']['revision_id'] != expected_head:
                raise ValueError('Session base changed before element placement capture')
            current = owner.history(session.project_id, session.timeline_id, limit=1)
            if not current.ok:
                raise ValueError(current.error.message)
            if current.data['head']['revision_id'] != expected_head:
                raise CommitConflict({'code': 'conflict', 'message': 'Timeline head changed before element placement capture',
                                      'details': {'expected_head': expected_head,
                                                  'actual_head': current.data['head']['revision_id']}})
            pin, inputs = _admitted_element_inputs(
                owner, client, session.project_id, realm=realm, element=element)
            return execute(opened.data['bundle'], [
                {**calls[0], 'args': {**calls[0]['args'], 'schema_source': pin}}],
                schema_inputs=inputs, realm_id=realm).commands

        with session.outbox.locked():
            state = session._state()

        def matches(pending):
            return _matches(pending, calls)

        if state['pending'] or state['flight'] is not None:
            session._stage(capture, matches)
            committed = session.flush(idempotency_key=key)
        else:
            captured = capture(state)
            committed = session.submit(
                captured,
                label='Place element',
                steps=[{
                    'id': 'element-placement',
                    'label': 'Place element',
                    'intent': 'Place the selected admitted element',
                    'tags': ['element'],
                    'command_ordinals': list(range(len(captured))),
                    'patch': [],
                }],
                merge='fields',
                idempotency_key=key,
                expected_state=state,
                base=state['base'],
                evaluation={'kind': 'label', 'count': 0},
            )
        return _placement_result(session, committed['data'], committed['receipt'], before,
                                 target, expected_head, key)
    except CommitConflict as exc:
        failure = exc.failure
        return DomainResult.failure(ErrorObject(failure['code'], failure['message'], failure['details']),
                                    idempotency_key=key or f'element-place-{uuid.uuid4().hex}')
    except WorkspaceClientError as exc:
        return DomainResult.failure(ErrorObject(exc.code, exc.message, exc.details),
                                    idempotency_key=key or f'element-place-{uuid.uuid4().hex}')
    except ServiceError as exc:
        return DomainResult.failure(exc.to_error_object(),
                                    idempotency_key=key or f'element-place-{uuid.uuid4().hex}')
    except (ValueError, KeyError, TypeError) as exc:
        return DomainResult.failure(ErrorObject('validation_error', str(exc), {}),
                                    idempotency_key=key or f'element-place-{uuid.uuid4().hex}')


def _placement_result(session, data, receipt, before, target, expected_head, key):
    return DomainResult.success({
        'project_id': session.project_id,
        'timeline_id': session.timeline_id,
        'expected_head': expected_head,
        'new_head': data['accepted_revision_id'],
        'target': target,
        'element': copy.deepcopy(data['commands'][-1]['args']['elementRef']),
        'before': {'element': before.get('elementRef') or before.get('element_ref')},
    }, receipt=CommandReceipt.from_dict(receipt), idempotency_key=key)
