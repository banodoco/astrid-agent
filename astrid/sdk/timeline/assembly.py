"""Requester-owned reads for the registered two-child assembler admission."""
from __future__ import annotations

import copy
import hashlib
import json

from ._command_state import State, equal
from ._common import TimelineEditError
from .capture import number
from .outbox import request_bytes


def admission_body(arguments):
    """The registered assembler's completed generated createTask wire body."""
    return {key: arguments[source] for key, source in (
        ('capability_id', 'capability_id'), ('capability_digest', 'capability_digest'),
        ('schema_version', 'schema_version'), ('input_object_ids', 'input_object_ids'),
        ('project', 'project_id'), ('spec', 'spec'), ('storage_estimate', 'storage_estimate'))}


def retry_assembly(session, state, flight, children, replace_clips, digest, key, rationale):
    """Compare only original caller intent; retry never reads mutable inputs."""
    try:
        body = json.loads(flight['body'])
        capture = body['spec']['inputs']['resolved_children']
        valid = (request_bytes(body).decode() == flight['body']
                 and body['capability_id'] == 'rendering.assemble_timeline'
                 and body['project'] == session.project_id
                 and equal(capture['admitted_target'], {
                     'project_id': session.project_id, 'timeline_id': session.timeline_id,
                     'base_revision_id': state['base']['revision_id'],
                     'base_publication_id': state['base']['publication_id']})
                 and all(isinstance(child, dict) and type(child.get('version')) is int for child in children)
                 and equal([child['association'] for child in capture['children']], children)
                 and equal(capture['replace_clips'], replace_clips)
                 and equal(capture['rationale'], rationale)
                 and body['capability_digest'] == digest and flight['key'] == key)
    except (KeyError, TypeError, ValueError) as exc:
        raise TimelineEditError('invalid assembly admission flight; original intent retained') from exc
    if not valid:
        raise TimelineEditError('assembly admission is immutable; retry its original inputs and key')


def capture_assembly(session, state, child_outputs, *, replace_clips,
                     rationale='Assemble ordered child visuals'):
    """Read exact original inputs once; never select a mutable primary or head."""
    if not isinstance(rationale, str):
        raise TimelineEditError('assembly rationale must be a string')
    bundle = session._bundle(state)
    authored = State(bundle)
    replace_clips = list(replace_clips)
    if bundle['placements'] or replace_clips != [
            'parent/clip:' + row['id'] for row in authored.collection('parent', 'clip')]:
        raise TimelineEditError('assembly requires explicit original flat parent clip membership')
    child_outputs = copy.deepcopy(list(child_outputs))
    if len(child_outputs) != 2:
        raise TimelineEditError('assembly requires two ordered exact child output selectors')
    transport = session.client._remote._transport
    children = []
    for ordinal, selector in enumerate(child_outputs):
        if (not isinstance(selector, dict) or set(selector) != {'association_id', 'object_id', 'version'}
                or type(selector['version']) is not int or selector['version'] < 1):
            raise TimelineEditError('assembly requires exact association/object/version selectors')
        association = transport.get_managed_output(selector['association_id'])
        if (association['project_id'] != session.project_id
                or association['object_id'] != selector['object_id']
                or association['lifecycle']['version'] != selector['version']
                or association['durability'] != 'durable'
                or association['lifecycle']['state'] not in ('available', 'promoted')
                or association['role'] not in ('primary', 'primary_visual')
                or not association['media_type'].startswith('video/')):
            raise TimelineEditError('assembly child is not the exact eligible project visual output')
        media = transport.get_project_media_selection(session.project_id, selector)
        verified = transport.get_project_verified_media(session.project_id, media)
        if (media['object_id'] != selector['object_id'] or not equal(verified['selection'], media)
                or verified['media_type'] != association['media_type']
                or type(verified['size']) is not int or verified['size'] <= 0
                or verified['size'] != association['size']):
            raise TimelineEditError('verified media differs from the exact child association')
        number(verified.get('duration_seconds'), 'child intrinsic duration', positive=True)
        children.append({'ordinal': ordinal, 'task_id': association['task_id'],
                         'association': selector, 'media': media, 'verified_media': verified})
    target = {'project_id': session.project_id, 'timeline_id': session.timeline_id,
              'base_revision_id': state['base']['revision_id'],
              'base_publication_id': state['base']['publication_id']}
    return json.loads(json.dumps({'admitted_target': target, 'bundle': bundle,
                                  'replace_clips': replace_clips, 'children': children,
                                  'rationale': rationale}, allow_nan=False))


def read_assembly_proposal(session, task_id, attempt_id):
    """Read and hash Runtime's exact completed managed proposal bytes."""
    from astrid.core.contracts.runtime_orchestration import is_native_assembler_spec

    from .commands import _VALIDATOR
    from .commit import _digest

    if not all(isinstance(value, str) and value.strip() for value in (task_id, attempt_id)):
        raise TimelineEditError('assembly requires an exact task and attempt')
    transport = session.client._remote._transport
    task = transport.get_task(task_id)
    try:
        spec = task['spec']['spec']
        if (task['task_id'] != task_id or task['project_id'] != session.project_id
                or task['state'] != 'succeeded' or task['attempt_id'] != attempt_id
                or task['capability_id'] != 'rendering.assemble_timeline'
                or not is_native_assembler_spec(spec)):
            raise ValueError('not the completed registered attempt')
        target = spec['runtime_dependencies']['publication']['admitted_target']
        if (target['project_id'], target['timeline_id']) != (session.project_id, session.timeline_id):
            raise ValueError('foreign admitted target')
        output, = [row for row in task['result']['outputs'] if row.get('name') == 'authoring_proposal']
        associations, cursor = transport.list_managed_outputs(task_id)
        if cursor is not None:
            raise ValueError('incomplete managed output read')
        association, = [row for row in associations
                        if row['attempt_id'] == attempt_id and row['output_port'] == 'authoring_proposal']
        if (output.get('output_port', output['name']) != 'authoring_proposal'
                or output['kind'] != 'object' or output['media_type'] != 'application/json'
                or association['task_id'] != task_id or association['project_id'] != session.project_id
                or association['run_id'] != task['run_id']
                or association['object_id'] != output['digest']
                or type(output['size']) is not int or output['size'] <= 0
                or association['size'] != output['size']
                or association['media_type'] != output['media_type']
                or association['role'] != output.get('role', 'output')
                or any(association[key] != output.get(key, default) for key, default in
                       (('group_key', 'default'), ('variant_key', str(output.get('ordinal', 0))),
                        ('ordinal', 0)))
                or association['durability'] != 'durable'
                or association['lifecycle']['state'] not in ('available', 'promoted')):
            raise ValueError('proposal association changed')
        provenance = association['provenance']
        producer = {'task_id': task_id, 'attempt_id': attempt_id,
                    'executor_id': provenance['executor_id'], 'fence': task['lease_fence'],
                    'runtime_epoch': task['runtime_epoch']}
        if (any(type(producer[key]) is not int or producer[key] <= 0 for key in ('fence', 'runtime_epoch'))
                or any(not equal(provenance[key], value) for key, value in producer.items())
                or provenance['capability_id'] != task['capability_id']):
            raise ValueError('proposal attempt provenance changed')
        raw = transport.get_object(association['object_id'])['data']
        if (not isinstance(raw, bytes) or len(raw) != output['size']
                or 'sha256:' + hashlib.sha256(raw).hexdigest() != association['object_id']):
            raise ValueError('proposal object bytes changed')

        def pairs(items):
            value = {}
            for key, item in items:
                if key in value:
                    raise ValueError('duplicate proposal JSON key')
                value[key] = item
            return value

        proposal = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs)
        errors = list(_VALIDATOR.evolve(schema={'$ref': '#/$defs/SettledProducerProposal'}).iter_errors(proposal))
        if errors:
            raise ValueError(errors[0].message)
        if (any(type(proposal['producer'][key]) is not int for key in ('fence', 'runtime_epoch'))
                or not equal(proposal['producer'], producer)
                or not equal(proposal['admitted_target'], target)
                or proposal['proposal_digest'] != _digest({key: value for key, value in proposal.items()
                                                           if key != 'proposal_digest'})):
            raise ValueError('proposal differs from its original admission or digest')
        return proposal
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise TimelineEditError('settled assembly proposal is unavailable or changed') from exc


def commit_assembly(session, proposal, *, idempotency_key, label, steps=None,
                    merge="fields", reads=None, group_id=None, dry_run=False):
    """Commit the exact settled proposal under its original requesting actor.

    Requeueing a completed batch deliberately retains its original base and body:
    Runtime can replay acceptance after a lost render-admission acknowledgement.
    """
    from .producer import build_producer_batch

    # Runtime authenticates producer.guard, including the original managed output.
    # Do not reread mutable task/output lifecycle before replaying an accepted key.
    batch = build_producer_batch(proposal)
    if (batch['project_id'], batch['timeline_id']) != (session.project_id, session.timeline_id):
        raise TimelineEditError('proposal belongs to a different project or timeline')
    if not isinstance(label, str) or not label.strip() or merge not in ('fields', 'atomic'):
        raise TimelineEditError('assembly commit requires a label and fields or atomic merge')
    if steps is None:
        steps = [{'id': 'assembly', 'label': label, 'intent': batch['rationale'],
                  'tags': [], 'command_ordinals': list(range(len(batch['commands']))),
                  'patch': []}]
    options = {'label': label, 'steps': copy.deepcopy(steps), 'merge': merge}
    if reads is not None:
        options['reads'] = copy.deepcopy(reads)
    if group_id is not None:
        options['group_id'] = group_id
    base = {'revision_id': batch['base_revision_id'],
            'publication_id': batch['base_publication_id']}
    with session.outbox.locked():
        state = session._state()
        if state['pending'] or state['flight'] is not None:
            if (not equal(state['pending'], batch['commands']) or state['base'] != base
                    or not equal(state.get('commit_options'), options)
                    or state.get('rationale') != batch['rationale']):
                raise TimelineEditError('retry the original assembly commit before another publication')
        else:
            state.update(base=base, pending=batch['commands'], rationale=batch['rationale'],
                         commit_options=options)
            session.outbox.write(state)
    return session.flush(idempotency_key=idempotency_key, dry_run=dry_run)
