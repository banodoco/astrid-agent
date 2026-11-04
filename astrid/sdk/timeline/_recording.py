"""Instrumented authored-state primitives. Handlers never manufacture records."""
from __future__ import annotations

import copy

from ._collections import KINDS, arrange, insertion_boundary, order, rows
from ._command_state import ABSENT, CommandError, State, equal, present, spelling
from ._common import AUDIO_KINDS


def active(row):
    return (row.get('enabled') is not False and row.get('active') is not False
            and not any(row.get(key) is True for key in ('disabled', 'hidden', 'deleted')))


def selection(state, key, owner, members=()):
    boundaries = None
    if key == "move.target":
        result = [ref for ref in members if state.entity(ref, optional=True) is not None]
    elif key == 'element.targets':
        result = order(state, owner, 'clip', check_mirrors=False)
    elif key in KINDS:
        result = ([] if owner != 'parent' and state.entity(owner, optional=True) is None
                  else order(state, owner, KINDS[key]))
        if key.endswith('.after'):
            return {"key": key, "query_version": 1, "owner": owner, "members": list(members),
                    "order": result, "boundaries": insertion_boundary(result, members),
                    "policy": "guard-membership"}
    elif key == 'text-binding.owners':
        origin = state.entity(owner + '/shot', optional=True)
        source = origin['shot_id'] if origin is not None else None
        result = ['occurrence:' + row['occurrence_id'] for row in state.bundle['placements']
                  if state.entity('occurrence:' + row['occurrence_id'] + '/shot')['shot_id'] == source]
        boundaries = ['shot:' + source] if source is not None else []
    elif key == 'placement.owners':
        origin = state.entity(owner, optional=True)
        shot = origin['shot_id'] if origin is not None else None
        result = ['occurrence:' + row['occurrence_id'] for row in state.bundle['placements']
                  if row['shot_id'] == shot]
        boundaries = ['shot:' + shot] if shot is not None else []
    elif key == "move.blockers":
        origin = state.occurrence(owner)
        lane = (origin.get('placement') or {}).get('track', origin.get('track'))
        track = state.entity('parent/track:' + lane, optional=True) if isinstance(lane, str) else None
        output = track is None or (track.get('kind') not in AUDIO_KINDS
                                   and track.get('role') not in AUDIO_KINDS and active(track))
        result = []
        for row in state.bundle['placements']:
            if output and active(row) and (row.get('placement') or {}).get('track', row.get('track')) == lane:
                result.append('occurrence:' + row['occurrence_id'])
    else:
        raise CommandError(f"unsupported recorded query {key!r}")
    return {"key": key, "query_version": 1, "owner": owner, "members": result,
            "order": list(result), "boundaries": list(result) if boundaries is None else boundaries,
            "policy": "include-concurrent" if key == "collection.text-bindings.append" else "guard-membership"}


def selection_matches(expected, actual):
    if equal(expected, actual):
        return True
    if expected["key"] != "collection.text-bindings.append" or expected["policy"] != "include-concurrent":
        return False
    # Concurrent keyed births append after the original ordered prefix. The
    # accepted record still captures the complete new input order for inverse.
    return (expected["members"] == expected["order"] == expected["boundaries"]
            and actual["order"][:len(expected["order"])] == expected["order"]
            and all(equal(value, actual[key]) for key, value in expected.items()
                    if key not in ("members", "order", "boundaries")))


class Recording:
    def __init__(self, bundle, command, ordinal, *, closures=None, verified_media=(),
                 schema_inputs=(), realm_id=None, verified_producer=None):
        self.state = State(bundle, closures=closures)
        self.verified_media = copy.deepcopy(verified_media)
        self.verified_producer = copy.deepcopy(verified_producer)
        self.schema_inputs, self.realm_id = schema_inputs, realm_id
        self.input_state = None
        self.command_input = State(copy.deepcopy(bundle), closures=closures)
        parent = bundle['parent']
        config = parent.get('config')
        self.mirrored_clips = (isinstance(parent.get('clips'), list)
                               and isinstance(config, dict) and isinstance(config.get('clips'), list)
                               and equal(parent['clips'], config['clips']))
        self.record = {"ordinal": ordinal, "command": copy.deepcopy(command), "writes": [],
                       "reads": [], "selections": [], "bindings": [], "created": [],
                       "dependencies": [], "derived_writes": []}

    def capture_input(self):
        """Keep inverse claims on its immutable head, separate from reverse stages."""
        self.input_state = State(copy.deepcopy(self.state.bundle), closures=self.state.closures)

    def read(self, ref, path=()):
        value = self.state.get(ref, path)
        captured = self.input_state.get(ref, path) if self.input_state is not None else value
        read = {"ref": ref, "path": list(path), "state": captured}
        if not any(equal(read, prior) for prior in self.record['reads']):
            self.record['reads'].append(read)
        return copy.deepcopy(value.get('value'))

    def select(self, key, owner, members=()):
        value = selection(self.input_state or self.state, key, owner, members)
        if value not in self.record['selections']:
            if key.startswith('collection.'):
                self.read(owner + '/collection:' + KINDS[key])
                if owner != 'parent':
                    self.read(owner, ['occurrence_id'])
                    self.read(owner + '/shot', ['shot_id'])
                    self.bind('collection.owner:' + key, owner)
            self.record['selections'].append(value)
        return value['members']

    def bind(self, key, ref, *, dependencies=()):
        self.record['bindings'].append({"key": key, "ref": ref, "policy": "fixed",
                                        "dependencies": list(dependencies)})

    def dependency(self, key, kind, pin):
        self.record['dependencies'].append({"key": key, "kind": kind,
                                            "pin": copy.deepcopy(pin), "policy": "exact"})

    def assign(self, ref, path, after):
        if after['state'] == 'absent':
            for end in range(1, len(path)):
                ancestor = self.state.get(ref, path[:end])
                if ancestor == ABSENT or ancestor == present(None):
                    # A missing leaf must not erase its ancestor's tagged presence.
                    self.read(ref, path[:end])
                    break
        before = self.state.get(ref, path)
        self.record['writes'].append({"ref": ref, "path": list(path),
                                       "before": before, "after": copy.deepcopy(after)})
        self.state.set(ref, path, after)
        if ref == 'parent' and list(path) == ['config']:
            parent = self.state.bundle['parent']
            config = parent.get('config')
            self.mirrored_clips = (isinstance(parent.get('clips'), list)
                                   and isinstance(config, dict) and isinstance(config.get('clips'), list)
                                   and equal(parent['clips'], config['clips']))

    def write(self, ref, path, value):
        self.assign(ref, path, present(value))

    def clear(self, ref, path):
        self.assign(ref, path, ABSENT)

    def object(self, ref, path):
        value = self.state.get(ref, path)
        if value['state'] == 'present' and isinstance(value['value'], dict):
            return
        self.read(ref, path)
        if value['state'] == 'present' and value['value'] is not None:
            raise CommandError("intent container must be object or null", ref=ref, path=path)
        self.write(ref, path, {})

    def create(self, owner, kind, row, *, restoring=False, after=None):
        rows(self.state, owner, kind, check_mirrors=not restoring)
        if not restoring and any(s['owner'] == owner and s['key'] == 'collection.' + kind + 's.after'
               for s in self.record['selections']):
            raise CommandError('create-after requires a single scoped creation')
        ordered = None
        if after is not None:
            if restoring or kind not in ('clip', 'track') or any(
                    item['owner'] == owner and item['kind'] == kind for item in self.record['created']):
                raise CommandError('create-after requires a single scoped creation')
            self.select('collection.' + kind + 's.after', owner, [after])
            ordered = order(self.state, owner, kind)
        if owner != 'parent':
            self.read(owner, ['occurrence_id'])
            self.read(owner + '/shot', ['shot_id'])
            self.bind('create.owner', owner)
        if not restoring and after is None:
            self.select('track.create' if kind == 'track' else 'collection.' + kind + 's', owner)
        ref = owner + '/' + kind + ':' + row['id']
        if self.state.get(ref, []) != ABSENT:
            raise CommandError("created identity is occupied", kind="identity_collision", ref=ref)
        self.read(ref)
        self.write(ref, [], row)
        self.record['created'].append({"ref": ref, "kind": kind, "owner": owner})
        if ordered is not None:
            ordered.insert(ordered.index(after) + 1, ref)
            arrange(self.state, owner, kind, ordered)
        return ref

    def create_entity(self, ref, *, kind, owner, value, restoring=False):
        from ._command_state import root_identity

        root = root_identity(ref)
        if (root is None or root[0] != kind
                or owner != (None if kind == 'shot' else 'parent')):
            raise CommandError('invalid created root scope', ref=ref)
        self.state.validate_root(ref, value)
        document = self.state.bundle['source_mapping'].get('_runtime_document', {})
        occupied = (any(row['shot_id'] == root[1] for row in document.get('shot_revisions', []))
                    if kind == 'shot' else any(row['occurrence_id'] == root[1] for row in
                        document.get('parent_revision', {}).get('payload', {}).get('occurrences', [])))
        if not document:
            mapping = self.state.bundle['source_mapping']
            occupied = (any(row['shot_id'] == root[1] for row in mapping['shots'].values())
                        if kind == 'shot' else root[1] in mapping['placements'])
        if (occupied and not restoring) or self.state.get(ref, []) != ABSENT or any(
                item['ref'] == ref for item in self.record['created']):
            raise CommandError('created identity is occupied', kind='identity_collision', ref=ref)
        self.read(ref)
        self.write(ref, [], value)
        if kind == 'shot' and root[1] not in self.state.bundle['source_mapping']['shots']:
            self.state.bundle['shots'][root[1]]['_created'] = True
        self.record['created'].append({'ref': ref, 'kind': kind, 'owner': owner})
        return ref

    def create_asset(self, owner, identity, value):
        ref = owner + '/asset:' + identity
        if owner != 'parent':
            self.read(owner, ['occurrence_id'])
            self.read(owner + '/shot', ['shot_id'])
        if self.state.get(ref, []) != ABSENT:
            raise CommandError('created identity is occupied', kind='identity_collision', ref=ref)
        self.read(ref)
        self.write(ref, [], value)
        self.record['created'].append({'ref': ref, 'kind': 'asset', 'owner': owner})
        return ref

    def compile_inputs(self):
        # Consumers validate the complete native input, including recorded
        # parent mirrors, before deriving timing from it.
        self.compile_mirrors()
        # The compiler retains this immutable input closure. These pins do not
        # imply that the move evaluated an anchor or any document settings.
        mapping = self.state.bundle.get('source_mapping', {})
        retained = mapping.get('_runtime_document', {}).get('dependency_manifest', {}).get('documents', [])
        from astrid.core.timeline.authoring_bundle import _digest
        for capture in mapping.get('pinned_documents', []):
            pin = capture['pin']
            # An inverse may reopen an exact pin from an accepted removed clip.
            if pin in retained or self.record['command']['op'] == 'timeline.inverse':
                kind = 'transcript' if capture['document']['kind'] == 'media.transcript' else 'source'
                self.dependency('compile.document:' + _digest(pin), kind, pin)

    def compile_mirrors(self):
        parent = self.state.bundle['parent']
        if self.mirrored_clips:
            before = self.state.get('parent', ['config', 'clips'])
            after = present(parent['clips'])
            if not equal(before, after):
                self.record['derived_writes'].append({
                    "ref": "parent", "path": ['config', 'clips'], "before": before,
                    "after": after, "cause": self.record['ordinal']})
                self.state.set('parent', ['config', 'clips'], after)

    def finish(self, *, capture=False):
        command = self.record['command']
        if capture:
            # Public replay guards describe the input; the record retains actual
            # observations made between the deterministic recipe's own writes.
            command['reads'], command['selections'] = [], []
            for read in self.record['reads']:
                claim = {**read, 'state': self.command_input.get(read['ref'], read['path'])}
                if not any(equal(claim, prior) for prior in command['reads']):
                    command['reads'].append(claim)
            for selected in self.record['selections']:
                selection_state = self.command_input
                if (command.get('op') == 'timeline.inverse'
                        and selected['key'] == 'track.create'
                        and any(selection_state.entity(ref, optional=True) is None
                                for ref in selected['members'])):
                    selection_state = self.state
                claim = selection(selection_state, selected['key'], selected['owner'], selected['members'])
                if claim not in command['selections']:
                    command['selections'].append(claim)
            command['bindings'] = {b['key']: b['ref'] for b in self.record['bindings']
                                   if '/clip:' in b['ref'] or '/track:' in b['ref']
                                   or (b['ref'].startswith('occurrence:') and '/' not in b['ref'])}
            command['created'] = [item['ref'] for item in self.record['created']]
            command['expect'], command['authorship'] = {}, {}
            for write in self.record['writes']:
                # Whole-closure guards use the schema's general read refs. The
                # legacy authored-property TargetKey cannot spell this scope.
                if write['ref'] == 'timeline':
                    continue
                key = write['ref'] + '::' + spelling(write['path'])
                before = self.command_input.get(write['ref'], write['path'])
                command['expect'].setdefault(key, copy.deepcopy(before))
                command['authorship'][key] = 'direct'
        return self.record


def guard(state, ref, path, expected, *, kind='dependency_changed'):
    actual = state.get(ref, path)
    if not state.matches(ref, path, expected):
        raise CommandError("original authored guard changed", kind=kind, ref=ref, path=path,
                           base=expected, theirs=actual)
