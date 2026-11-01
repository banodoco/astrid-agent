"""Commit/step guards and effect projection for the existing command engine."""
from __future__ import annotations

import copy
import json

from jsonschema import Draft202012Validator

from ._command_state import ABSENT, CommandError, equal, present
from ._doc_patch import Closure, identities, identity_key, normalized_path, value_at


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _effect_key(effect):
    return json.dumps(effect, sort_keys=True, separators=(',', ':'))


def validate_reads(reads):
    from .commands import _SCHEMA

    validator = Draft202012Validator({**_SCHEMA, '$ref': '#/$defs/CommitRead'})
    if not isinstance(reads, list):
        raise CommandError('reads must be an array')
    for read in reads:
        try:
            json.dumps(read, allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise CommandError('reads must contain finite JSON') from exc
        errors = list(validator.iter_errors(read))
        if errors:
            raise CommandError(errors[0].message)
        if read['ref'] == 'timeline' and read['kind'] != 'scope':
            raise CommandError('timeline is a read-only closure scope')
        if read['kind'] == 'collection' and (read['members'] != sorted(read['members'])
                or read['order'] is not None and set(read['order']) != set(read['members'])):
            raise CommandError('collection members/order must describe the same unique IDs')


def normalize_steps(request, calls):
    label = request.get('label', 'Timeline edit')
    if not _text(label):
        raise CommandError('label must be a nonempty string')
    steps = copy.deepcopy(request.get('steps', [{'id': 'edit', 'label': label, 'intent': '', 'tags': [],
                                                'command_ordinals': list(range(len(calls))), 'patch': [
                                                    copy.deepcopy(change) for call in calls if call['op'] == 'doc.patch'
                                                    for change in call['args']['changes']]}]))
    if not isinstance(steps, list) or not steps:
        raise CommandError('steps must be a nonempty array')
    seen, cursor = set(), 0
    for step in steps:
        required = {'id', 'label', 'intent', 'tags', 'command_ordinals', 'patch'}
        if not isinstance(step, dict) or not required <= step.keys() or step.keys() - required - {'reads'}:
            raise CommandError('invalid step fields')
        if not _text(step['id']) or step['id'] in seen or not _text(step['label']) or not isinstance(step['intent'], str):
            raise CommandError('invalid or duplicate step identity/label')
        seen.add(step['id'])
        tags = step['tags']
        if not isinstance(tags, list) or any(not _text(tag) for tag in tags) or len(set(tags)) != len(tags):
            raise CommandError('step tags must be unique strings')
        indices = step['command_ordinals']
        if (not isinstance(indices, list) or not indices or any(type(i) is not int for i in indices)
                or indices != list(range(cursor, cursor + len(indices))) or indices[-1] >= len(calls)):
            raise CommandError('steps must partition commands in contiguous execution order')
        cursor += len(indices)
        if not isinstance(step['patch'], list):
            raise CommandError('step patch must be an array')
        validate_reads(step.get('reads', []))
        submitted = [change for i in indices if calls[i]['op'] == 'doc.patch' for change in calls[i]['args']['changes']]
        if all(calls[i]['op'] == 'doc.patch' for i in indices):
            if 'steps' in request and not equal(step['patch'], submitted):
                raise CommandError('step patch does not equal its ordered doc.patch changes', kind='capture_mismatch')
        else:
            from .commands import _SCHEMA
            validator = Draft202012Validator({**_SCHEMA, '$ref': '#/$defs/DocPatchChange'})
            for claim in step['patch']:
                errors = list(validator.iter_errors(claim))
                if errors:
                    raise CommandError(errors[0].message)
                from ._doc_patch import validate_submitted_change
                validate_submitted_change(claim)
            if submitted and not all(change in step['patch'] for change in submitted):
                raise CommandError('step patch omits doc.patch changes', kind='capture_mismatch')
    if cursor != len(calls):
        raise CommandError('steps must include every command')
    return label, steps


def native_asset_reads(payload):
    """Retain both physical registry mirrors without resolving aliases."""
    paths = [['registry'], ['config', 'registry']
             if isinstance(payload.get('config'), dict) else ['config']]
    result = []
    for path in paths:
        node = payload
        for token in path:
            if not isinstance(node, dict) or token not in node:
                state = ABSENT.copy()
                break
            node = node[token]
        else:
            state = present(copy.deepcopy(node))
        result.append({'kind': 'field', 'ref': 'parent', 'path': path, 'state': state})
    return result


def canonical_site(closure, ref, path):
    """Normalize native recorder aliases using their verified evaluation boundary."""
    path = list(path)
    if ref == 'timeline':
        return 'timeline', []
    if ref == 'parent':
        return ref, normalized_path(ref, path)
    if ref == 'parent/source':
        return 'parent', ['config', *path]
    if ref == 'parent/registry':
        return 'parent', ['config', 'registry', *path]
    if ref.startswith(('internal:', 'internal-revision:', 'shot-revision:')):
        return closure.resolve(ref), path
    if '/collection:' in ref:
        owner, kind = ref.rsplit('/collection:', 1)
        if kind not in ('clip', 'track', 'occurrence'):
            return canonical_site(closure, owner + ('/source' if owner != 'parent' else ''), [])
        path = (['config', kind + 's'] if owner == 'parent' and kind != 'occurrence' else [kind + 's'])
        return canonical_site(closure, owner + ('/source' if owner != 'parent' else ''), path)
    owner, slash, entity = ref.partition('/')
    if owner == 'parent':
        if entity.startswith(('clip:', 'track:')):
            kind, identity = entity.split(':', 1)
            return 'parent', ['config', kind + 's', 'id:' + identity, *path]
        if entity.startswith('asset:'):
            return 'parent', ['config', 'registry', 'assets', entity[6:], *path]
    if owner.startswith('shot:'):
        if path[:1] == ['payload']:
            return closure.resolve(owner), path[1:]
        return 'timeline', []  # Native root aggregates include both shot and child.
    if owner.startswith('occurrence:'):
        occurrence = owner[len('occurrence:'):]
        if not slash:
            return 'parent', ['occurrences', 'occurrence_id:' + occurrence, *path]
        rows = closure.document['parent_revision']['payload']['occurrences']
        row = next((row for row in rows if row['occurrence_id'] == occurrence), None)
        if row is None:
            return 'parent', ['occurrences', 'occurrence_id:' + occurrence]
        shot_ref = 'shot-revision:' + row['shot_revision_id']
        shot = closure.images[shot_ref]
        internal = 'internal-revision:' + shot['internal_timeline_revision_id']
        if entity.startswith(('clip:', 'track:')):
            kind, identity = entity.split(':', 1)
            return internal, [kind + 's', 'id:' + identity, *path]
        if entity == 'source':
            return internal, path
        if entity == 'registry':
            return internal, ['registry', *path]
        if entity.startswith('asset:'):
            return internal, ['registry', 'assets', entity[6:], *path]
        if entity == 'shot':
            if path[:1] == ['payload']:
                return shot_ref, path[1:]
            return 'parent', ['occurrences', 'occurrence_id:' + occurrence, *path]
    return 'timeline', []


def _prefix(a, b):
    return a[:len(b)] == b[:len(a)]


def effect_diff(before, after):
    """Complete closure-union effects; wrappers and manifests are never targets."""
    old, new = before.images, after.images
    result = []
    # A child must exist before its owner attaches it.
    for prefix in ('internal-revision:', 'shot-revision:'):
        for ref in sorted(new.keys() - old.keys()):
            if ref.startswith(prefix):
                result.append({'op': 'set', 'ref': ref, 'path': [], 'before': ABSENT.copy(),
                               'value': copy.deepcopy(new[ref]['payload'])})

    def walk(ref, path, left, right):
        if equal(left, right):
            return
        if left != ABSENT and right != ABSENT and isinstance(left['value'], dict) and isinstance(right['value'], dict):
            a, b = left['value'], right['value']
            for key in sorted(a.keys() | b.keys()):
                walk(ref, [*path, key], present(a[key]) if key in a else ABSENT,
                     present(b[key]) if key in b else ABSENT)
            return
        key = identity_key(ref, path)
        if key and left != ABSENT and right != ABSENT and isinstance(left['value'], list) and isinstance(right['value'], list):
            a, b = left['value'], right['value']
            a_ids, b_ids = identities(a, key, ref=ref, path=path), identities(b, key, ref=ref, path=path)
            if a_ids == b_ids:
                for identity, first, second in zip(a_ids, a, b):
                    walk(ref, [*path, key + ':' + identity], present(first), present(second))
                return
        item = {'op': 'remove' if right == ABSENT else 'set', 'ref': ref, 'path': path,
                'before': copy.deepcopy(left)}
        if right != ABSENT:
            item['value'] = copy.deepcopy(right['value'])
        result.append(item)

    def logical_parent(image):
        payload = copy.deepcopy(image['payload'])
        for mirror in ('clips', 'registry'):
            if mirror not in payload:
                continue
            value = payload.pop(mirror)
            config = payload.setdefault('config', {})
            if mirror in config and not equal(present(config[mirror]), present(value)):
                other = config[mirror]
                if mirror != 'clips' or not (
                        other is None and isinstance(value, list)
                        or value is None and isinstance(other, list)):
                    raise CommandError('contradictory parent mirrors', kind='capture_mismatch',
                                       ref='parent', path=['config', mirror])
                value = other if isinstance(other, list) else value
            config[mirror] = value
        return payload

    def topology(image):
        payload = image['payload']
        config = payload.get('config', {})
        return [(key in payload, isinstance(config, dict) and key in config)
                for key in ('clips', 'registry')]

    old_parent, new_parent = logical_parent(old['parent']), logical_parent(new['parent'])
    if topology(old['parent']) != topology(new['parent']):
        # Alias paths cannot express a change in raw mirror presence. Bind the
        # complete parent image so projection preserves each storage location.
        result.append({'op': 'set', 'ref': 'parent', 'path': [],
                       'before': present(copy.deepcopy(old['parent']['payload'])),
                       'value': copy.deepcopy(new['parent']['payload'])})
    else:
        walk('parent', [], present(old_parent), present(new_parent))
    for ref in sorted(old.keys() & new.keys() - {'parent'}):
        walk(ref, [], present(old[ref]['payload']), present(new[ref]['payload']))
    for prefix in ('shot-revision:', 'internal-revision:'):
        for ref in sorted(old.keys() - new.keys()):
            if ref.startswith(prefix):
                result.append({'op': 'remove', 'ref': ref, 'path': [],
                               'before': present(old[ref]['payload'])})
    return result


def _restore_inverse_baselines(bundle, sources):
    from astrid.core.timeline.authoring_bundle import _RestoredShot

    from .commit import _verify_document, bundle_from_document

    for source in sources:
        document = source['source_mapping']['_runtime_document']
        _verify_document(document)
        if (document['project_id'], document['timeline_id']) != (bundle['project_id'], bundle['timeline_id']):
            raise CommandError('foreign inverse source', kind='history_unavailable')
        historical = bundle_from_document(document)
        for identity, shot in bundle['shots'].items():
            if (shot.get('_created') is not True or identity in bundle['source_mapping']['shots']
                    or isinstance(shot, _RestoredShot)):
                continue
            prior = historical['shots'].get(identity)
            owners = {row['occurrence_id'] for row in bundle['placements'] if row['shot_id'] == identity}
            if (prior is not None and owners
                    and owners == {row['occurrence_id'] for row in historical['placements'] if row['shot_id'] == identity}
                    and equal(shot['payload'], prior['payload'])
                    and equal(shot['internal_timeline'], prior['internal_timeline'])):
                bundle['shots'][identity] = _RestoredShot(shot, copy.deepcopy(document))


def inverse_closure(bundle, sources, *, inverse_records=()):
    """Compile inverse effects using the same authenticated immutable reuse as acceptance."""
    _restore_inverse_baselines(bundle, sources)
    return Closure(bundle, source_bundles=sources, inverse_records=inverse_records)


class CommitPatch:
    def __init__(self, request, calls, bundle, *, evaluated_head=None):
        self.active = request is not None
        self.v2 = self.active and (any(call.get('op') == 'doc.patch' for call in calls)
                                  or any(key in request for key in ('label', 'steps', 'merge', 'reads')))
        self.request = request or {}
        self.calls = calls
        allowed = {'api_version', 'project_id', 'timeline_id', 'base_revision_id', 'base_publication_id',
                   'commands', 'source', 'rationale', 'group_id', 'dry_run', 'label', 'steps', 'merge', 'reads'}
        if self.request.keys() - allowed:
            raise CommandError('unknown commit control fields')
        self.label, self.steps = normalize_steps(self.request, calls)
        merge = self.request.get('merge', 'fields')
        if merge not in ('fields', 'atomic'):
            raise CommandError('merge must be fields or atomic')
        validate_reads(self.request.get('reads', []))
        self.merge_mode = ('atomic' if any(call['op'] == 'doc.patch' for call in calls)
                           and 'reads' not in self.request else merge)
        self.head = evaluated_head or {'revision_id': bundle['base_parent']['revision_id'], 'publication_id': None}
        self.inputs, self.outputs, self.dependencies = {}, {}, {}
        self.batch_dependencies = []
        self.closures = {}
        self.records = {}
        self.boundaries = {}
        self.compiler_effects = []
        self.inverse_stages = {}
        self.inverse_effects = {}
        self.inverse_sources = {}
        self.inverse_records = {}
        self.execution_boundaries = []

    def step(self, ordinal):
        return next(step for step in self.steps if ordinal in step['command_ordinals'])

    def capture(self, closure, read, ctx, boundary):
        if read['ref'] == 'timeline':
            dependency = copy.deepcopy(read)
            bindings = [closure.binding('timeline', ref, **boundary) for ref in closure.images]
            for binding in bindings:
                binding.update(key='commit.read', policy='fixed', dependencies=[], path=[])
                ctx.record['bindings'].append(binding)
            ctx.read('timeline')
            return dependency
        expected = getattr(ctx, 'patch_replay_bindings', {}).get((ctx.record['ordinal'], 'read',
                   read['ref'], tuple(normalized_path(read['ref'], read['path'])), boundary['phase']))
        if expected is not None and read['ref'].startswith(('shot-revision:', 'internal-revision:')) and read['ref'] not in closure.images:
            physical = expected
        else:
            physical = closure.resolve(read['ref'])
        if expected is not None and physical != expected:
            raise CommandError('declared read changed physical binding', kind='binding_changed', ref=read['ref'])
        payload = closure.payload(physical)
        actual = value_at(payload, physical, read['path'])
        if read['kind'] == 'field':
            if not equal(actual, read['state']):
                raise CommandError('declared field read does not match', kind='previous_value_mismatch',
                                   ref=read['ref'], path=read['path'], base=read['state'], theirs=actual)
        elif read['kind'] == 'collection':
            ids = identities(actual.get('value'), identity_key(physical, read['path']),
                             ref=read['ref'], path=read['path'])
            if sorted(ids) != read['members'] or read['order'] is not None and ids != read['order']:
                error = CommandError('declared collection read does not match', kind='previous_value_mismatch',
                                     ref=read['ref'], path=read['path'])
                error.dependency_kind = 'collection'
                raise error
        dependency = {**copy.deepcopy(read), 'ref': physical, 'path': normalized_path(physical, read['path'])}
        binding = closure.binding(read['ref'], physical, **boundary)
        binding.update(key='commit.read', policy='fixed', dependencies=[], path=dependency['path'],
                       dependency=copy.deepcopy(dependency))
        ctx.record['bindings'].append(binding)
        # Exact captures stay separate from the normalized step dependency index.
        if read['kind'] != 'collection':
            for ref, path in closure.native_sites(physical, read['path']):
                if path or ref == 'parent':
                    ctx.read(ref, path)
        return dependency

    def before(self, ctx):
        if not self.active:
            return
        ordinal = ctx.record['ordinal']
        step = self.step(ordinal)
        boundary = {'base_revision_id': self.request.get('base_revision_id'),
                    'base_publication_id': self.request.get('base_publication_id'),
                    'evaluated_head': copy.deepcopy(self.head), 'step_id': step['id'],
                    'command_ordinal': ordinal, 'phase': 'input'}
        self.boundaries[ordinal] = boundary
        self.closures[ordinal] = Closure(copy.deepcopy(ctx.state.bundle))
        def replay_binding(expected):
            if isinstance(expected, str):
                return expected
            step_id, source, owner, owners = expected
            source = replay_binding(source)
            prior = next(step for step in self.steps if step['id'] == step_id)
            if prior['command_ordinals'][-1] >= ordinal or step_id not in self.outputs:
                raise CommandError('sequential binding precedes its source', kind='binding_changed')
            before, after = Closure(self.inputs[step_id]), Closure(self.outputs[step_id])
            record = {'bindings': []}
            self.materializations(record, before, after,
                self.boundaries[prior['command_ordinals'][-1]], prior,
                [write for index in prior['command_ordinals'] for write in self.records[index]['writes']])
            successors = {binding['successor'] for binding in record['bindings']
                          if binding['physical_ref'] == source and binding['owner'] == owner
                          and binding['owners'] == owners}
            if len(successors) != 1:
                raise CommandError('sequential image has no unique authenticated successor',
                                   kind='binding_changed', ref=source)
            return successors.pop()

        # Staged IDs name the original execution. Resolve only through a completed
        # step's actual source-bound materialization at this replay boundary.
        ctx.patch_replay_bindings = {key: replay_binding(value) if key[0] == ordinal else value
                                     for key, value in getattr(ctx, 'patch_replay_bindings', {}).items()}
        # Runtime authenticates the next input against the preceding command's observations.
        if ordinal > 0:
            previous = self.closures[ordinal - 1]
            current = self.closures[ordinal]
            for ref in sorted(current.images):
                if (ref not in previous.images
                        or previous.images[ref]['revision_id'] != current.images[ref]['revision_id']
                        or previous.images[ref]['content_digest'] != current.images[ref]['content_digest']):
                    binding = current.binding(ref, ref, **boundary)
                    binding.update(key='commit.input', policy='fixed', dependencies=[], path=[])
                    ctx.record['bindings'].append(binding)
        if ordinal == 0:
            self.batch_dependencies = [self.capture(self.closures[ordinal], read, ctx,
                                       {**boundary, 'phase': 'batch-read'})
                                       for read in self.request.get('reads', [])]
        if ordinal == step['command_ordinals'][0]:
            self.inputs[step['id']] = copy.deepcopy(ctx.state.bundle)
            self.dependencies[step['id']] = copy.deepcopy(self.batch_dependencies)
            self.dependencies[step['id']].extend(self.capture(self.closures[ordinal], read, ctx, boundary)
                                                 for read in step.get('reads', []))

    def compiler(self, ctx, prior_records):
        if not self.active:
            return
        from .native_word_anchor import PATH

        records = [*prior_records, ctx.record]
        for effect in ctx.record['derived_writes']:
            if effect['path'] not in (['at'], ['hold']) or '/clip:' not in effect['ref']:
                continue
            row = ctx.state.entity(effect['ref'])
            namespace = (row.get('app') or {}).get('astrid') or {}
            anchors = namespace.get('word_anchors', {})
            inputs = [(effect['ref'], [*PATH, key]) for key in ('on', 'until', 'for', 'word_anchors')]
            inputs.extend((effect['ref'], [key]) for key in ('speed', 'from', 'to'))
            inputs.extend([('parent', ['config', 'output']), ('parent', ['config', 'theme_overrides'])])
            for choice in anchors.values():
                source = choice['source']
                owner = source.split('/')[0]
                inputs.extend([(source, []), (owner, []), (owner + '/source', ['tracks']),
                               (owner + '/source', ['registry'])])
            contributors = []
            for record in records:
                closure = self.closures[record['ordinal']]
                reads = [canonical_site(closure, ref, path) for ref, path in inputs]
                for write in record['writes']:
                    ref, path = canonical_site(closure, write['ref'], write['path'])
                    if not equal(write['before'], write['after']) and any(
                            ref == source_ref and _prefix(path, source_path) for source_ref, source_path in reads):
                        contributors.append(record['ordinal'])
                        break
            # Unknown compiler causes retain a conservative closure dependency on
            # every possible contributing step, as required for safe partial undo.
            if not anchors or not contributors:
                contributors = list(range(len(records)))
                for step in self.steps:
                    dependency = {'kind': 'scope', 'ref': 'timeline', 'path': []}
                    if dependency not in self.dependencies[step['id']]:
                        self.dependencies[step['id']].append(dependency)
            cause = max(contributors)
            effect['cause'] = cause
            self.compiler_effects.append((cause, copy.deepcopy(effect)))
            contributing_steps = {self.step(ordinal)['id'] for ordinal in contributors}
            if len(contributing_steps) > 1:
                for step_id in contributing_steps:
                    dependency = {'kind': 'scope', 'ref': 'timeline', 'path': []}
                    if dependency not in self.dependencies[step_id]:
                        self.dependencies[step_id].append(dependency)

    def after(self, ctx, *, inverse_records=()):
        if not self.active:
            return
        ordinal = ctx.record['ordinal']
        mapping = ctx.state.bundle['source_mapping']
        if '_copy_manifest' in mapping and ctx.record['command']['op'] not in ('timeline.inverse', 'timeline.restore'):
            documents = mapping['_copy_manifest']['documents']
            for dependency in ctx.record['dependencies']:
                if dependency['kind'] in ('source', 'transcript'):
                    pin = dependency['pin']
                    if pin not in documents and any(
                            capture['pin'] == pin for capture in mapping.get('pinned_documents', [])):
                        documents.append(copy.deepcopy(pin))
        self.records[ordinal] = ctx.record
        step = self.step(ordinal)
        if hasattr(ctx, 'inverse_stages'):
            self.inverse_sources[ordinal] = ctx.inverse_sources
            self.inverse_records[ordinal] = inverse_records
            self.inverse_effects[ordinal] = getattr(ctx, 'inverse_effects', [])
            _restore_inverse_baselines(ctx.state.bundle, ctx.inverse_sources)
            stages = self.inverse_stages[ordinal] = []
            stage_has_writes = getattr(ctx, 'inverse_stage_has_writes',
                                       [True] * len(ctx.inverse_stages))
            last_effectful = next((index for index in range(len(stage_has_writes) - 1, -1, -1)
                                   if stage_has_writes[index]), len(stage_has_writes) - 1)
            # Equal historical bytes must not replace the authenticated input revision.
            before = self.closures[ordinal]
            for index, (_, bundle) in enumerate(ctx.inverse_stages):
                # Retained-input reconciliation describes the completed inverse.
                # Attach it to the last effectful stage, then keep that canonical
                # identity through any trailing stages that recorded no writes.
                completed = index >= last_effectful
                if completed:
                    bundle = ctx.state.bundle
                after = inverse_closure(bundle, ctx.inverse_sources,
                                        inverse_records=inverse_records if completed else ())
                stages.append((before, after))
                before = after
        for binding in ctx.record['bindings']:
            if binding['key'] == 'inverse.stage':
                continue
            if self.v2 and 'physical_ref' not in binding:
                physical, path = canonical_site(self.closures[ordinal], binding['ref'], [])
                if physical != 'timeline':
                    binding.update({key: value for key, value in self.closures[ordinal].binding(
                        binding['ref'], physical, **self.boundaries[ordinal]).items() if key != 'ref'})
                    binding['path'] = path
            if binding['key'].startswith('doc.patch:'):
                binding['boundary'] = {**self.boundaries[ordinal], **binding['boundary']}
        if ordinal == step['command_ordinals'][-1]:
            self.outputs[step['id']] = copy.deepcopy(ctx.state.bundle)
        if self.v2 and (len(self.calls) > 1 or ordinal in self.inverse_stages):
            self.execution_boundaries.append({
                'boundary': copy.deepcopy(self.boundaries[ordinal]),
                'input': copy.deepcopy(self.closures[ordinal].document),
                'output': inverse_closure(ctx.state.bundle, self.inverse_sources.get(ordinal, []),
                                          inverse_records=self.inverse_records.get(ordinal, ())).document,
            })

    def finish(self, plan):
        if not self.active:
            return plan
        for step in self.steps:
            before = Closure(self.inputs[step['id']])
            ordinal = step['command_ordinals'][-1]
            after = inverse_closure(self.outputs[step['id']], self.inverse_sources.get(ordinal, []),
                                    inverse_records=self.inverse_records.get(ordinal, ()))
            dependencies = self.dependencies[step['id']]
            for ordinal in step['command_ordinals']:
                closure = self.closures[ordinal]
                record = plan.records[ordinal]
                for binding in record['bindings']:
                    if not binding.get('key', '').startswith('doc.patch:'):
                        continue
                    change = binding['change']
                    item = {'kind': 'field', 'ref': binding['physical_ref'],
                            'path': binding['path'], 'state': copy.deepcopy(change['before'])}
                    if change['op'].startswith('list.'):
                        item['path'] = [*item['path'], 'id:' + change['id']]
                        shape = {'kind': 'collection', 'ref': binding['physical_ref'], 'path': binding['path'],
                                 'members': sorted(change['order_before']), 'order': change['order_before']}
                        if shape not in dependencies:
                            dependencies.append(shape)
                    if item not in dependencies:
                        dependencies.append(item)
                created_refs = {item['ref'] for item in record['created']}
                for read in record['reads']:
                    if read['ref'] in created_refs and read['state'] == ABSENT:
                        continue  # Allocation history guards absence; this is not a closure read.
                    if read['ref'] == 'parent/collection:asset' and not read['path']:
                        for item in native_asset_reads(closure.payload('parent')):
                            if item not in dependencies:
                                dependencies.append(item)
                        continue
                    ref, path = canonical_site(closure, read['ref'], read['path'])
                    if '/collection:' in read['ref']:
                        try:
                            value = value_at(closure.payload(ref), ref, path)
                            ids = identities(value.get('value'), identity_key(ref, path), ref=ref, path=path)
                            item = {'kind': 'collection', 'ref': ref, 'path': path, 'members': sorted(ids), 'order': ids}
                        except (CommandError, KeyError):
                            item = {'kind': 'scope', 'ref': ref, 'path': []}
                    elif ref == 'timeline' or not path:
                        item = {'kind': 'scope', 'ref': ref, 'path': []}
                    else:
                        item = {'kind': 'field', 'ref': ref, 'path': path, 'state': copy.deepcopy(read['state'])}
                    if item not in dependencies:
                        dependencies.append(item)
            effects = effect_diff(before, after)
            segments = [(ordinal, first, second) for ordinal in step['command_ordinals']
                        for first, second in self.inverse_stages.get(ordinal, [])]
            if segments:
                effects = []
                previous = before
                for ordinal, first, second in segments:
                    effects.extend(effect_diff(previous, first))
                    record = plan.records[ordinal]
                    stages = [binding for binding in record['bindings'] if binding['key'] == 'inverse.stage']
                    index = sum(1 for binding in stages if 'effect_range' in binding['boundary'])
                    start_effect = len(effects)
                    changes = effect_diff(first, second)
                    inherited = self.inverse_effects.get(ordinal, [])
                    if index < len(inherited) and inherited[index] is not None:
                        captured = inherited[index]
                        if sorted(map(_effect_key, changes)) != sorted(map(_effect_key, captured)):
                            raise CommandError('nested inverse effects differ from execution', kind='capture_mismatch')
                        changes = copy.deepcopy(captured)
                    effects.extend(changes)
                    if index < len(stages):
                        stages[index]['boundary']['effect_range'] = [start_effect, len(effects)]
                    previous = second
                effects.extend(effect_diff(segments[-1][2], after))
            # Byte-equal sequential edits still carry intent, history and exact inverse guards.
            if all(self.calls[i]['op'] == 'doc.patch' for i in step['command_ordinals']):
                # A whole-parent effect preserves raw mirror presence, which
                # normalized submitted paths cannot represent.
                mirror_effect = any(effect['ref'] == 'parent' and not effect['path'] for effect in effects)
                if not mirror_effect and (before.document == after.document or all(
                        change['ref'] == 'parent' for i in step['command_ordinals']
                        for change in self.calls[i]['args']['changes'])):
                    effects = [{**copy.deepcopy(binding['change']),
                                'path': normalized_path(binding['physical_ref'], binding['change']['path'])}
                               for i in step['command_ordinals']
                               for binding in plan.records[i]['bindings']
                               if binding.get('key', '').startswith('doc.patch:')]
            elif step['patch'] and any({**claim, 'path': normalized_path(claim['ref'], claim['path'])}
                                      not in effects for claim in step['patch']):
                raise CommandError('semantic step assertion differs from execution', kind='capture_mismatch')
            step['patch'] = effects
            step['dependencies'] = dependencies
        for cause, effect in self.compiler_effects:
            destination = self.step(cause)
            ref, path = canonical_site(self.closures[cause], effect['ref'], effect['path'])
            for step in self.steps:
                step['patch'][:] = [change for change in step['patch'] if not (
                    change['ref'] == ref and normalized_path(ref, change['path']) == path)]
            destination['patch'].append({'op': 'set', 'ref': ref, 'path': path,
                                         'before': copy.deepcopy(effect['before']),
                                         'value': copy.deepcopy(effect['after']['value'])})
        for step in self.steps:
            ordinal = step['command_ordinals'][-1]
            before = Closure(self.inputs[step['id']])
            after = inverse_closure(self.outputs[step['id']], self.inverse_sources.get(ordinal, []),
                                    inverse_records=self.inverse_records.get(ordinal, ()))
            record = plan.records[ordinal]
            if self.active:
                for index, effect in enumerate(step['patch']):
                    segment = next(((first, second) for source_ordinal in step['command_ordinals']
                        for stage, (first, second) in zip(
                            [binding for binding in plan.records[source_ordinal]['bindings']
                             if binding['key'] == 'inverse.stage'], self.inverse_stages.get(source_ordinal, []))
                        if stage['boundary']['effect_range'][0] <= index < stage['boundary']['effect_range'][1]),
                        (before, after))
                    for side, closure in zip(('before', 'after'), segment):
                        if effect['ref'] not in closure.images:
                            continue
                        binding = closure.binding(effect['ref'], effect['ref'],
                            **{**self.boundaries[ordinal], 'phase': 'effect', 'side': side})
                        binding.update(key='commit.effect', policy='fixed', dependencies=[],
                                       path=effect['path'], step_id=step['id'], effect_ordinal=index)
                        record['bindings'].append(binding)
            # Synthesized legacy steps need the same continuation authority as
            # explicit v2 steps when their native edits materialize child roots.
            # Input materializations describe the step's actual endpoints.
            # Transient inverse roots belong to their authenticated inverse.stage
            # effect ranges, not to the outer command's input closure.
            segments = [(first, second) for index in step['command_ordinals']
                        for first, second in self.inverse_stages.get(index, [])]
            complete_stage = (len(segments) == 1 and segments[0][0].document == before.document
                              and segments[0][1].document == after.document)
            if not self.v2 or not segments:
                for first, second in segments or [(before, after)]:
                    self.materializations(record, first, second, self.boundaries[ordinal], step,
                        [write for index in step['command_ordinals'] for write in plan.records[index]['writes']])
            elif complete_stage:
                # Only an exclusive paired turnover can become the source of
                # a later native inverse. Shared restoration stays effect-bound.
                proposed = {'bindings': []}
                self.materializations(proposed, before, after, self.boundaries[ordinal], step,
                    [write for index in step['command_ordinals'] for write in plan.records[index]['writes']])
                if all(binding['physical_ref'] not in after.images
                       and binding['successor'] not in before.images
                       and binding['edge_before'] not in after.images
                       and binding['edge_after'] not in before.images
                       and binding['owners'] == [binding['owner']]
                       and [owner + '/shot' for owner, _ in after.owners[binding['successor']]]
                           == [binding['owner']]
                       and before.images[binding['physical_ref']].get('shot_id')
                           == after.images[binding['successor']].get('shot_id')
                       for binding in proposed['bindings']):
                    record['bindings'].extend(proposed['bindings'])
        if self.merge_mode == 'atomic':
            # Effects and materializations can touch roots beyond the authored writes.
            # Build the complete batch union only after their bindings are recorded.
            touched = set()
            for record in plan.records:
                closure = self.closures[record['ordinal']]
                for item in [*record['writes'], *record['reads'], *record['derived_writes']]:
                    touched.add(canonical_site(closure, item['ref'], item['path'])[0])
                touched.update(binding['physical_ref'] for binding in record['bindings'] if 'physical_ref' in binding)
                if any(binding.get('key', '').startswith('doc.patch:') and binding['physical_ref'] == 'parent'
                       and not binding['path'] for binding in record['bindings']):
                    touched.add('timeline')
            # A fallback captures every input root, expanding the batch union for
            # all steps, including earlier inputs where removed roots still exist.
            scopes = touched if all(touched <= self.closures[step['command_ordinals'][0]].images.keys()
                                    for step in self.steps) else {'timeline'}
            for step in self.steps:
                start = step['command_ordinals'][0]
                before = Closure(self.inputs[step['id']])
                dependencies = step['dependencies']
                for ref in sorted(scopes):
                    item = {'kind': 'scope', 'ref': ref, 'path': []}
                    if item not in dependencies:
                        dependencies.append(item)
                    # Capture each scope at this step's input, never from output.
                    for physical in before.images if ref == 'timeline' else [ref]:
                        binding = before.binding(ref, physical, **self.boundaries[start])
                        binding.update(key='commit.atomic', policy='fixed', dependencies=[], path=[])
                        plan.records[start]['bindings'].append(binding)
        plan.steps, plan.label, plan.merge_mode = self.steps, self.label, self.merge_mode
        # Private execution custody stays outside the public prepared recorder.
        setattr(plan, '_sequential_execution', self.execution_boundaries)
        return plan

    @staticmethod
    def materializations(record, before, after, boundary, step, writes):
        old_rows = {row['occurrence_id']: row for row in before.document['parent_revision']['payload']['occurrences']}
        new_rows = {row['occurrence_id']: row for row in after.document['parent_revision']['payload']['occurrences']}
        for identity in old_rows.keys() & new_rows.keys():
            owner = 'occurrence:' + identity
            if any(write['ref'] == owner and (not write['path'] or write['path'] == ['shot_id'])
                   for write in writes):
                continue
            a, b = 'shot-revision:' + old_rows[identity]['shot_revision_id'], 'shot-revision:' + new_rows[identity]['shot_revision_id']
            first = 'internal-revision:' + before.images[a]['internal_timeline_revision_id']
            second = 'internal-revision:' + after.images[b]['internal_timeline_revision_id']
            for source, successor, edge_before, edge_after in ((a, b, first, second), (first, second, a, b)):
                if source != successor:
                    binding = before.binding(source, source, **boundary)
                    binding.update(key='commit.materialization', policy='fixed', dependencies=[],
                                   successor=successor, owner=owner + '/shot',
                                   edge_before=edge_before, edge_after=edge_after, step_id=step['id'])
                    record['bindings'].append(binding)


def record_sites(record, closure):
    """Authored history sites, including equal-valued writes and collection shape."""
    result, covered = [], set()
    for binding in record.get('bindings', []):
        if not binding.get('key', '').startswith('doc.patch:'):
            continue
        ref, path = binding['physical_ref'], binding['path']
        change = binding['change']
        covered.update(range(*binding['write_range']))
        if change['op'].startswith('list.'):
            result.append({'ref': ref, 'path': path, 'kind': 'shape', 'change': change})
            if change['op'] != 'list.move':
                result.append({'ref': ref, 'path': [*path, 'id:' + change['id']], 'kind': 'field',
                               'write': {'before': change['before'],
                                         'after': present(change['value']) if change['op'] == 'list.insert' else ABSENT.copy()}})
        else:
            result.append({'ref': ref, 'path': path, 'kind': 'field', 'write': {
                'before': copy.deepcopy(change['before']),
                'after': present(change['value']) if change['op'] == 'set' else ABSENT.copy()}})
    for index, write in enumerate(record['writes']):
        if index not in covered:
            ref, path = canonical_site(closure, write['ref'], write['path'])
            result.append({'ref': ref, 'path': path, 'kind': 'field', 'write': write,
                           'owner': write['ref'].split('/')[0] if write['ref'].startswith('occurrence:') else None})
    for write in record['derived_writes']:
        # Parent clip/registry mirrors share the authored site's identity.
        if write['ref'] == 'parent' and write['path'] == ['config', 'clips']:
            continue
        ref, path = canonical_site(closure, write['ref'], write['path'])
        result.append({'ref': ref, 'path': path, 'kind': 'field', 'write': write})
    return result


def _shape_changed(site, dependency):
    """A collection read guards shape, never unrelated item values."""
    path = dependency['path']
    if site['kind'] == 'shape':
        if site.get('change', {}).get('op') == 'list.move' and dependency.get('order') is None:
            return False
        return site['path'] == path or _prefix(site['path'], path) and len(site['path']) < len(path)
    if not _prefix(site['path'], path):
        return False
    if len(site['path']) > len(path):
        return len(site['path']) == len(path) + 1 and (
            site.get('write', {}).get('before') == ABSENT or site.get('write', {}).get('after') == ABSENT)
    write = site.get('write')
    if write is None or len(site['path']) < len(path):
        return True
    key = identity_key(site['ref'], path if site['ref'] != 'parent' else (
        ['clips'] if path == ['config', 'clips'] else path))
    try:
        old = identities(write['before'].get('value'), key, ref=site['ref'], path=path)
        new = identities(write['after'].get('value'), key, ref=site['ref'], path=path)
    except CommandError:
        return True
    return set(old) != set(new) or dependency.get('order') is not None and old != new


def _managed_boundaries(entry, before, after):
    """Project managed edges through authenticated step effects, including transient roots."""
    from ._doc_patch import apply_change, replace_at
    from .commit import _digest

    images = {ref: image for ref, image in before.images.items() if ref != 'parent'}
    parent = {'occurrences': copy.deepcopy(before.images['parent']['payload']['occurrences'])}
    steps = entry['records'].get('steps', [])
    if not steps:
        yield images, parent['occurrences'], {
            ref: image for ref, image in after.images.items() if ref != 'parent'
        }, after.images['parent']['payload']['occurrences'], entry['records']['commands'], None
        return
    segments = []
    for step in steps:
        stages = [binding for ordinal in step['command_ordinals']
                  for binding in entry['records']['commands'][ordinal]['bindings']
                  if binding['key'] == 'inverse.stage' and 'effect_range' in binding['boundary']]
        cursor = 0
        for binding in stages:
            start, end = binding['boundary']['effect_range']
            if start < cursor or not start <= end <= len(step['patch']):
                raise CommandError('inverse effect ranges are incomplete', kind='history_unavailable')
            if cursor < start:
                segments.append((step, step['patch'][cursor:start]))
            segments.append(({**step, '_inverse_stage': binding}, step['patch'][start:end]))
            cursor = end
        if not stages or cursor < len(step['patch']):
            segments.append((step, step['patch'][cursor:]))
    for step, effects in segments:
        output, result = copy.deepcopy(images), copy.deepcopy(parent)
        for effect in effects:
            ref, path = effect['ref'], effect['path']
            replacement = present(effect['value']) if effect['op'] == 'set' else ABSENT
            if ref == 'parent' and (not path or path[:1] == ['occurrences']):
                if path == ['occurrences'] and effect['op'] == 'list.move':
                    try:
                        result, _ = apply_change(result, effect)
                    except CommandError as exc:
                        raise CommandError('managed order input differs from history',
                                           kind='history_unavailable') from exc
                    continue
                expected = (present(effect['before']['value']['occurrences']) if not path else effect['before'])
                actual = present(result['occurrences']) if not path else value_at(result, ref, path)
                if not equal(actual, expected):
                    raise CommandError('managed edge input differs from history', kind='history_unavailable')
                if not path:
                    result['occurrences'] = copy.deepcopy(effect['value']['occurrences'])
                else:
                    replace_at(result, ref, path, replacement)
            elif ref != 'parent' and not path:
                actual = present(output[ref]['payload']) if ref in output else ABSENT
                if not equal(actual, effect['before']):
                    raise CommandError('physical root input differs from history', kind='history_unavailable')
                if replacement == ABSENT:
                    output.pop(ref, None)
                else:
                    output[ref] = {'revision_id': ref.partition(':')[2],
                                   'content_digest': _digest(effect['value']),
                                   'payload': copy.deepcopy(effect['value'])}
                    if ref.startswith('shot-revision:'):
                        output[ref]['internal_timeline_revision_id'] = effect['value']['internal_timeline_revision_id']
        records = [entry['records']['commands'][ordinal] for ordinal in step['command_ordinals']]
        yield images, parent['occurrences'], output, result['occurrences'], records, step
        images, parent = output, result
    actual = {ref: image['content_digest'] for ref, image in after.images.items() if ref != 'parent'}
    if ({ref: image['content_digest'] for ref, image in images.items()} != actual
            or parent['occurrences'] != after.images['parent']['payload']['occurrences']):
        raise CommandError('step effects do not reach the verified closure', kind='history_unavailable')


def rebase_patch(plan, entries, head_bundle, *, load_boundary):
    """Scan authenticated history before replay; final-byte equality never erases a write."""
    landing = Closure(head_bundle)
    staged_roots = {effect['ref'] for step in plan.steps for effect in step['patch']
                    if not effect['path'] and effect['op'] == 'set' and effect['before'] == ABSENT}
    staged_roots.update(item['ref'] for record in plan.records for item in record['created'])
    # A command can allocate a child that a later command in the same step
    # materializes again. The step's net effects omit that intermediate image.
    # Retain only allocated roots from the provider's actual command output;
    # a submitted ref or an equal payload is never allocation authority.
    from .commit import bundle_from_document

    for execution in getattr(plan, '_sequential_execution', []):
        record = plan.records[execution['boundary']['command_ordinal']]
        allocations = [item['ref'] for item in record['created']
                       if item['kind'] == 'shot' and item['ref'].startswith('shot:')]
        if not allocations:
            continue
        before = Closure(bundle_from_document(execution['input']))
        output = Closure(bundle_from_document(execution['output']))
        for ref in allocations:
            physical = output.resolve(ref)
            staged_roots.update({physical, 'internal-revision:'
                                 + output.images[physical]['internal_timeline_revision_id']} - before.images.keys())
    staged_predecessors = {binding['successor']: binding['physical_ref']
                          for record in plan.records for binding in record['bindings']
                          if binding.get('key') == 'commit.materialization'}
    staged_bindings = [binding for record in plan.records for binding in record['bindings']
                       if binding.get('key') == 'commit.materialization']
    transitions, sites, missing = [], [], set()
    for entry in entries:
        before_bundle, after_bundle = load_boundary(entry)
        boundary = Closure(before_bundle)
        output = Closure(after_bundle)
        records = entry['records']['commands']
        site_start = len(sites)
        for record in records:
            sites.extend(record_sites(record, boundary))
        try:
            for old_images, old_rows, new_images, new_rows, records, step in _managed_boundaries(entry, boundary, output):
                _materialization_sites(entry, old_images, old_rows, new_images, new_rows, records, step,
                                       transitions, missing, sites[site_start:], include_inverse_stages=True)
        except (KeyError, TypeError, IndexError) as exc:
            raise CommandError('incomplete materialization history', kind='history_unavailable') from exc

    def continuation(ref):
        if ref in staged_roots:
            return ref
        if ref in landing.images or ref in ('parent', 'timeline'):
            return ref
        current, visited = ref, set()
        while current not in landing.images:
            if current in visited:
                raise CommandError('cyclic materialization history', kind='history_unavailable', ref=ref)
            visited.add(current)
            candidates = {new for old, new, _ in transitions if old == current}
            if current in missing:
                raise CommandError('missing authenticated materialization history',
                                   kind='history_unavailable', ref=ref)
            if len(candidates) != 1:
                raise CommandError('bound image has no unique authenticated successor', kind='binding_changed', ref=ref)
            current = candidates.pop()
        return current

    def guarded_ref(ref):
        visited = set()
        while ref in staged_predecessors and ref not in visited:
            visited.add(ref)
            ref = staged_predecessors[ref]
        return continuation(ref)

    def replay_ref(ref, ordinal):
        sources = [binding for binding in staged_bindings if binding['successor'] == ref
                   and binding['boundary']['command_ordinal'] < ordinal]
        if not sources:
            return continuation(ref)
        binding = max(sources, key=lambda source: source['boundary']['command_ordinal'])
        source_ordinal = binding['boundary']['command_ordinal']
        return (binding['step_id'], replay_ref(binding['physical_ref'], source_ordinal),
                binding['owner'], binding['owners'])

    guards = []
    expected = {}
    for record in plan.records:
        # Generated patch bindings provide exact physical/path captures for patch operands.
        for binding in record['bindings']:
            if (binding.get('key') == 'commit.read' and 'physical_ref' in binding
                    and (binding['physical_ref'] not in staged_roots or binding['physical_ref'] in staged_predecessors)):
                if binding['ref'].startswith(('shot:', 'internal:')):
                    try:
                        landing.resolve(binding['ref'])
                    except CommandError as exc:
                        if exc.kind == 'ambiguous_ref':
                            raise
                bound = replay_ref(binding['physical_ref'], record['ordinal'])
                expected[(record['ordinal'], 'read', binding['ref'], tuple(binding['path']),
                          binding['boundary']['phase'])] = bound
            if binding.get('key', '').startswith('doc.patch:'):
                change = binding['change']
                if binding['ref'].startswith(('shot:', 'internal:')):
                    try:
                        landing.resolve(binding['ref'])
                    except CommandError as exc:
                        if exc.kind == 'ambiguous_ref':
                            raise
                ref, path = continuation(binding['physical_ref']), binding['path']
                if binding['physical_ref'] not in staged_roots or binding['physical_ref'] in staged_predecessors:
                    expected[(record['ordinal'], binding['boundary']['change_ordinal'])] = replay_ref(
                        binding['physical_ref'], record['ordinal'])
                guards.append({'kind': 'field', 'ref': ref,
                               'path': [*path, 'id:' + change['id']] if change['op'].startswith('list.') else path})
                if change['op'].startswith('list.'):
                    guards.append({'kind': 'collection', 'ref': ref, 'path': path,
                                   'members': sorted(change['order_before']), 'order': change['order_before']})
    for step in plan.steps:
        guards.extend(copy.deepcopy(step.get('dependencies', [])))
    # Captured semantic writes guard ancestors too; their historical aliases are
    # normalized at the base, before any head-dependent materialization.
    base_closure = Closure(load_boundary(entries[0])[0]) if entries else landing
    for record in plan.records:
        if record['command']['op'] != 'doc.patch':
            guards.extend({'kind': 'field', 'ref': site['ref'], 'path': site['path']}
                          for site in record_sites(record, base_closure)
                          if not (site.get('write', {}).get('before') == ABSENT
                                  and site.get('write', {}).get('ref') in {item['ref'] for item in record['created']}))
    # Collection selection changes take precedence over removed members' field guards.
    for guard in sorted(guards, key=lambda guard: guard['kind'] != 'collection'):
        guard['ref'] = guarded_ref(guard['ref'])
        for site in sites:
            their_ref = continuation(site['ref'])
            if guard['ref'] != 'timeline' and their_ref != 'timeline' and guard['ref'] != their_ref:
                continue
            if guard['ref'] == 'timeline' or their_ref == 'timeline':
                overlap = True
            elif guard['kind'] == 'collection':
                overlap = _shape_changed(site, guard)
            elif site['kind'] == 'shape':
                overlap = len(guard['path']) <= len(site['path']) and _prefix(site['path'], guard['path'])
            else:
                overlap = _prefix(site['path'], guard['path'])
            if overlap:
                kind = ('atomic_scope_changed' if plan.merge_mode == 'atomic' else
                        'selection_changed' if guard['kind'] == 'collection' else
                        'dependency_changed' if guard['kind'] == 'scope' else 'value_conflict')
                raise CommandError('intervening publication changed a guarded address', kind=kind,
                                   ref=guard['ref'], path=guard['path'])
    return expected


def _materialization_sites(entry, old_images, old_rows, new_images, new_rows, records, step,
                           transitions, missing, sites, *, include_inverse_stages=False):
    before_rows = {row['occurrence_id']: row for row in old_rows}
    after_rows = {row['occurrence_id']: row for row in new_rows}
    for identity in before_rows.keys() & after_rows.keys():
        owner = 'occurrence:' + identity
        # Native ownership replacement/fork is not continuation authority.
        rebound = any(write['ref'] == owner and (not write['path'] or write['path'] == ['shot_id'])
                      for record in records for write in record['writes'])
        if rebound:
            continue
        old_shot = 'shot-revision:' + before_rows[identity]['shot_revision_id']
        new_shot = 'shot-revision:' + after_rows[identity]['shot_revision_id']
        old_internal = 'internal-revision:' + old_images[old_shot]['internal_timeline_revision_id']
        new_internal = 'internal-revision:' + new_images[new_shot]['internal_timeline_revision_id']
        for old, new, edge_before, edge_after in (
                (old_shot, new_shot, old_internal, new_internal),
                (old_internal, new_internal, old_shot, new_shot)):
            if old != new:
                # Pin turnover proves reachability, not continuation. Require the
                # accepted source binding and both exact managed-edge boundaries.
                proven = False
                stage = step.get('_inverse_stage') if step is not None else None
                if not any(binding.get('key') == 'commit.effect' for record in records for binding in record['bindings']):
                    stage = None  # Legacy native stages retain their original input proofs.
                if stage is not None:
                    # validate_nested_history authenticates this native/effect
                    # partition against its source publication before this scan.
                    # Transient stage images are effect-bound, never outer-input
                    # materializations. Require both exact physical endpoints.
                    start, end = stage['boundary']['effect_range']
                    for ref, images, rows, side in ((old, old_images, old_rows, 'before'),
                                                    (new, new_images, new_rows, 'after')):
                        expected_owners = ['occurrence:' + row['occurrence_id'] + '/shot' for row in rows
                            if ref == 'shot-revision:' + row['shot_revision_id'] or ref == 'internal-revision:'
                            + images['shot-revision:' + row['shot_revision_id']]['internal_timeline_revision_id']]
                        pins = [binding for record in records for binding in record['bindings']
                                if binding.get('key') == 'commit.effect' and binding.get('physical_ref') == ref
                                and binding.get('path') == [] and start <= binding.get('effect_ordinal', -1) < end
                                and binding.get('boundary', {}).get('side') == side]
                        if not pins and ref in old_images and ref in new_images:
                            # A retained root can have no effect pin when the
                            # selected owner never rewrote it.  Its identity is
                            # still authenticated by the accepted inverse stage
                            # and exact source owner; payload bytes are not a
                            # successor-selection mechanism.
                            source_publications = {
                                publication
                                for record in records
                                if record.get('command', {}).get('op') == 'timeline.inverse'
                                for publication in record.get('command', {}).get('args', {}).get(
                                    'publications', [])
                            }
                            boundary = stage.get('boundary', {}) if stage is not None else {}
                            owner_id = owner.removeprefix('occurrence:')
                            before_owner = next((row for row in old_rows
                                                 if row.get('occurrence_id') == owner_id), None)
                            after_owner = next((row for row in new_rows
                                                if row.get('occurrence_id') == owner_id), None)

                            def physical_ref(row, images):
                                shot = 'shot-revision:' + row['shot_revision_id']
                                if ref.startswith('shot-revision:'):
                                    return shot
                                image = images.get(shot)
                                return ('internal-revision:' + image['internal_timeline_revision_id']
                                        if image is not None else None)

                            # The destination may be reattached to a physical
                            # root still owned by another occurrence.  Only an
                            # unchanged, uniquely ordered source owner from
                            # both accepted closures can authorize that reuse.
                            source_owners = []
                            for source_before in old_rows:
                                source_after = next((row for row in new_rows
                                                     if row.get('occurrence_id')
                                                     == source_before.get('occurrence_id')), None)
                                if (source_after is None
                                        or physical_ref(source_before, old_images) != ref
                                        or physical_ref(source_after, new_images) != ref
                                        or old_rows.index(source_before) != new_rows.index(source_after)
                                        or any(write['ref'] == 'occurrence:' + source_before['occurrence_id']
                                               and (not write['path'] or write['path'] == ['shot_id'])
                                               for record in records for write in record['writes'])):
                                    continue
                                source_owners.append((source_before, source_after))
                            from .commit import _digest

                            def valid_image(image):
                                return (isinstance(image, dict)
                                        and image.get('content_digest') == _digest(image.get('payload'))
                                        and isinstance(image.get('payload'), dict))

                            metadata = {key: value for key, value in old_images[ref].items()
                                        if key != 'payload'}
                            after_metadata = {key: value for key, value in new_images[ref].items()
                                              if key != 'payload'}
                            target_ref = None
                            if before_owner is not None and after_owner is not None:
                                target_ref = (physical_ref(before_owner, old_images)
                                              if side == 'before' else
                                              physical_ref(after_owner, new_images))
                            retained = (
                                step is not None
                                and boundary.get('publication_id') in source_publications
                                and boundary.get('step_id') == step.get('id')
                                and boundary.get('command_ordinals') == step.get('command_ordinals')
                                and before_owner is not None and after_owner is not None
                                and target_ref == ref
                                and source_owners
                                and valid_image(old_images[ref]) and valid_image(new_images[ref])
                                and metadata == after_metadata
                                and old_images[ref].get('content_digest')
                                == new_images[ref].get('content_digest')
                                and not any(effect['ref'] == ref and not effect['path']
                                            for effect in step['patch'][start:end]))
                            if retained:
                                continue
                        if not pins or any(
                                binding.get('ref') != ref or binding.get('revision_id') != images[ref]['revision_id']
                                or binding.get('content_digest') != images[ref]['content_digest']
                                or binding.get('policy') != 'fixed' or binding.get('dependencies') != []
                                or binding.get('owners') != expected_owners
                                or binding.get('step_id') != step['id']
                                or binding.get('boundary') != {**{
                                    key: entry['receipt']['result'][key]
                                    for key in ('base_revision_id', 'base_publication_id')},
                                    'evaluated_head': entry['previous'], 'step_id': step['id'],
                                    'command_ordinal': step['command_ordinals'][-1],
                                    'phase': 'effect', 'side': side}
                                for binding in pins):
                            raise CommandError('inverse stage image differs from authenticated effects',
                                               kind='history_unavailable', ref=ref)
                    proven = True
                for record in records:
                    for binding in record['bindings']:
                        if (binding.get('key') != 'commit.materialization'
                                or binding.get('physical_ref') != old
                                or binding.get('owner') != owner + '/shot'):
                            continue
                        captured = binding.get('boundary', {})
                        source = {'ref': old, 'physical_ref': old,
                                  'revision_id': old_images[old]['revision_id'],
                                  'content_digest': old_images[old]['content_digest'],
                                  'owners': ['occurrence:' + row['occurrence_id'] + '/shot' for row in old_rows
                                      if old == 'shot-revision:' + row['shot_revision_id'] or old == 'internal-revision:'
                                      + old_images['shot-revision:' + row['shot_revision_id']]['internal_timeline_revision_id']]}
                        if (any(binding.get(key) != value for key, value in source.items())
                                or binding.get('successor') != new
                                or binding.get('edge_before') != edge_before
                                or binding.get('edge_after') != edge_after
                                or captured.get('evaluated_head') != entry['previous']
                                or captured.get('phase') != 'input'
                                or captured.get('command_ordinal') != record['ordinal']
                                or captured.get('step_id') != binding.get('step_id')
                                or step is None or binding.get('step_id') != step['id']
                                or record['ordinal'] != step['command_ordinals'][-1]):
                            raise CommandError('materialization proof differs from verified edges',
                                               kind='history_unavailable', ref=old)
                        proven = True
                if not proven:
                    missing.add(old)
                    continue
                # Stage proofs are lineage for address scans, not additional
                # commit.materialization claims at the outer input boundary.
                if stage is None or include_inverse_stages:
                    transitions.append((old, new, owner))
                for site in sites:
                    if site['ref'] == old and site.get('owner') == owner:
                        site['ref'] = new


def inverse_address_resolver(entries, head_bundle, *, load_boundary, load_history=None):
    """Normalize inverse captures at their accepted boundary through R1 edge proofs."""
    from types import SimpleNamespace

    from ._command_state import logical_address
    from ._step_undo import validate_nested_history

    landing = Closure(head_bundle)
    transitions, missing, contexts, owners = [], set(), {}, {}
    for entry in entries:
        validate_nested_history(entry, load_history)
        before, after = (Closure(bundle) for bundle in load_boundary(entry))
        for old_images, old_rows, new_images, new_rows, records, step in _managed_boundaries(entry, before, after):
            _materialization_sites(entry, old_images, old_rows, new_images, new_rows, records, step,
                                   transitions, missing, [], include_inverse_stages=True)
            boundary = SimpleNamespace(images={'parent': before.images['parent'], **old_images},
                document={'parent_revision': {'payload': {'occurrences': old_rows}}})
            output = SimpleNamespace(images={'parent': after.images['parent'], **new_images},
                document={'parent_revision': {'payload': {'occurrences': new_rows}}})
            boundary.resolve = lambda ref, scope=boundary: Closure.resolve(scope, ref)
            output.resolve = lambda ref, scope=output: Closure.resolve(scope, ref)
            for record in records:
                for capture in [*record['writes'], *record['reads'], *record['derived_writes'],
                                *record['selections'], *record['bindings']]:
                    stage = step.get('_inverse_stage') if step is not None else None
                    if stage is not None and capture in record['writes']:
                        start, end = stage['boundary']['native_write_range']
                        if not any(capture is write for write in record['writes'][start:end]):
                            continue
                    if any(capture is write for write in record['writes']):
                        contexts[id(capture)] = boundary, output
                    else:
                        contexts.setdefault(id(capture), (boundary, output))
            if step is not None:
                for dependency in step.get('dependencies', []):
                    if id(dependency) in contexts:
                        continue
                    contexts[id(dependency)] = boundary, output
                    # Generated field dependencies retain the native read's
                    # owner only when that exact input site has one provenance.
                    if dependency['kind'] == 'field' and not any(
                            binding.get('key') == 'commit.read' and binding.get('dependency') == dependency
                            for record in records for binding in record['bindings']):
                        sources = {read['ref'].split('/', 1)[0] if read['ref'].startswith('occurrence:')
                                   and '/' in read['ref'] else None
                                   for record in records for read in record['reads']
                                   if canonical_site(boundary, read['ref'], read['path'])
                                   == (dependency['ref'], dependency['path'])
                                   and equal(read['state'], dependency['state'])}
                        if len(sources) == 1:
                            owners[id(dependency)] = sources.pop()

    def continuation(ref, owner):
        seen = set()
        while ref not in landing.images and ref not in ('parent', 'timeline'):
            if ref in seen:
                raise CommandError('cyclic inverse materialization history', kind='history_unavailable', ref=ref)
            seen.add(ref)
            successors = {new for old, new, source_owner in transitions
                          if old == ref and (owner is None or source_owner == owner)}
            if ref in missing:
                raise CommandError('missing authenticated materialization history', kind='history_unavailable', ref=ref)
            if not successors:
                break  # An authenticated removal keeps its immutable identity.
            if len(successors) != 1:
                raise CommandError('inverse image has ambiguous successors', kind='binding_changed', ref=ref)
            ref = successors.pop()
        return ref

    def resolve(capture, ref, path):
        boundary, output = contexts.get(id(capture), (None, None))
        if boundary is None:
            boundary = output = landing  # Earlier commands in this new batch.
        owner = owners.get(id(capture))
        if ref.startswith('occurrence:') and '/' in ref:
            owner = ref.split('/', 1)[0]
        elif capture.get('key') == 'commit.materialization':
            owner = capture['owner'].removesuffix('/shot')
        return site(boundary, output, ref, path, owner)

    def site(boundary, output, ref, path, owner):
        if ref.startswith(('internal-revision:', 'shot-revision:')):
            physical, path = ref, list(path)
        else:
            try:
                physical, path = canonical_site(boundary, ref, path)
            except CommandError as exc:
                if exc.kind != 'outside_closure':
                    raise
                physical, path = canonical_site(output, ref, path)
            except KeyError:
                physical, path = canonical_site(output, ref, path)
        if physical == 'timeline' and ref.startswith('shot:'):
            # Native aggregate roots retain their allocation identity. They
            # include authored shot and internal data, unlike a payload ref.
            return logical_address(ref, path)
        if '/collection:' in ref:
            path = [*path, ('shape',)]
        path = tuple(('id', token[3:]) if isinstance(token, str) and token.startswith('id:')
                     else ('occurrence_id', token[14:]) if isinstance(token, str)
                     and token.startswith('occurrence_id:') else token for token in path)
        return logical_address(continuation(physical, owner), path)

    return resolve
