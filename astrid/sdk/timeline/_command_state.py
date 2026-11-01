"""Canonical addresses and tagged authored state for the command recorder."""
from __future__ import annotations

import copy
import json
import re

from ._common import TimelineEditError

ABSENT = {"state": "absent"}


class CommandError(TimelineEditError):
    def __init__(self, message, *, kind="invalid_edit_command", ref="parent", path=(),
                 base=None, theirs=None, ours=None):
        super().__init__(message)
        self.kind, self.ref, self.path = kind, ref, list(path)
        self.base = copy.deepcopy(base or ABSENT)
        self.theirs = copy.deepcopy(theirs or ABSENT)
        self.ours = copy.deepcopy(ours or ABSENT)

    def to_json(self):
        return {"kind": self.kind, "ref": self.ref, "path": self.path,
                "base": self.base, "theirs": self.theirs, "ours": self.ours, "choices": []}


def present(value):
    return {"state": "present", "value": copy.deepcopy(value)}


def equal(a, b):
    """JSON equality, with numeric equivalence but no bool/number coercion."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(equal(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(equal(x, y) for x, y in zip(a, b))
    return a == b


def tokens(path):
    """Parse the legacy property spelling once; quoted punctuation is data."""
    if not isinstance(path, str):
        raise CommandError("property path must be a string")
    result, pos = [], 0
    while pos < len(path):
        if path[pos] == '[':
            if path[pos + 1:pos + 2] == '"':
                try:
                    value, end = json.JSONDecoder().raw_decode(path, pos + 1)
                except ValueError as exc:
                    raise CommandError("invalid quoted property token") from exc
                if end >= len(path) or path[end] != ']':
                    raise CommandError("missing property bracket")
                pos = end + 1
            else:
                match = re.match(r'\[(0|[1-9][0-9]*)\]', path[pos:])
                if not match:
                    raise CommandError("invalid numeric property token")
                value, pos = match[1], pos + match.end()
        else:
            match = re.match(r'[A-Za-z_][\w-]*', path[pos:])
            if not match:
                raise CommandError("invalid property token")
            value, pos = match[0], pos + match.end()
        result.append(value)
        if pos < len(path) and path[pos] == '.':
            pos += 1
            if pos == len(path) or path[pos] == '[':
                raise CommandError("invalid property separator")
        elif pos < len(path) and path[pos] != '[':
            raise CommandError("invalid property separator")
    return tuple(result)


def spelling(path):
    return ''.join((('.' if i else '') + token if re.fullmatch(r'[A-Za-z_][\w-]*', token)
                    else '[' + json.dumps(token, ensure_ascii=False) + ']')
                   for i, token in enumerate(path))


def address(key, resolved):
    # The entity delimiter is outside any quoted JSON property atom.
    quoted, escaped, split = False, False, []
    for i, char in enumerate(key):
        if escaped:
            escaped = False
        elif quoted and char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and key[i:i + 2] == '::':
            split.append(i)
    if len(split) > 1:
        raise CommandError("ambiguous entity/property delimiter")
    if split:
        i = split[0]
        return key[:i], tokens(key[i + 2:])
    if len(resolved) != 1:
        raise CommandError("bare property guard needs exactly one target")
    return resolved[0], tokens(key)


def logical_address(ref, path):
    path = tuple(path)
    if '/text-binding:' in ref:
        owner, identity = ref.rsplit('/text-binding:', 1)
        return owner, ('payload', 'text_bindings', ('text-binding', identity), *path)
    if ref == 'parent/registry':
        ref, path = 'parent', ('registry', *path)
    if ref == 'parent' and path[:1] == ('registry',):
        path = ('config', *path)
    if '/asset:' in ref:
        owner, identity = ref.rsplit('/asset:', 1)
        return owner, ('config' if owner == 'parent' else 'source', 'registry', 'assets', identity, *path)
    if '/collection:' in ref:
        owner, kind = ref.rsplit('/collection:', 1)
        from ._collections import container
        root, prefix = container(owner, kind)
        return root, (*prefix, ('shape',), *path)
    if '/clip:' in ref or '/track:' in ref:
        owner, entity = ref.rsplit('/', 1)
        kind, identity = entity.split(':', 1)
        prefix = ('config' if owner == 'parent' else 'source', kind + 's')
        return owner, (*prefix, ('id', identity), *path)
    if ref.endswith('/source'):
        owner = ref.removesuffix('/source')
        return owner, ('config' if owner == 'parent' else 'source', *path)
    if ref.endswith('/shot') and path[:1] == ('shot_id',):
        return ref.removesuffix('/shot'), path
    if ref == 'parent' and path[:1] == ('clips',):
        path = ('config', *path)
    return ref, path


def overlaps(a, b):
    a, b = logical_address(*a), logical_address(*b)
    return (a == ("timeline", ()) or b == ("timeline", ())
            or (a[0] == b[0] and a[1][:len(b[1])] == b[1][:len(a[1])]))


def one(rows, key, identity, *, optional=False):
    if not isinstance(rows, list):
        raise CommandError("entity collection must be an array")
    matches = [row for row in rows if isinstance(row, dict) and row.get(key) == identity]
    if len(matches) != 1:
        if optional and not matches:
            return None
        raise CommandError(f"missing or ambiguous identity {identity!r}", kind="identity_conflict")
    return matches[0]


def root_identity(ref):
    match = re.fullmatch(r'(shot|occurrence):((?!.*::)[^/]+)', ref) if isinstance(ref, str) else None
    return match.groups() if match and match[2].strip() else None


class State:
    def __init__(self, bundle, *, closures=None):
        self.bundle = bundle
        self.closures = closures or {}
        parent = bundle['parent']
        config = parent.get('config')
        primary = parent.get('clips')
        mirror = config.get('clips') if isinstance(config, dict) else None
        self.parent_clip_path = ('clips',) if isinstance(primary, list) and (
            primary or not isinstance(mirror, list) or not mirror) else ('config', 'clips')

    def occurrence(self, owner):
        return one(self.bundle["placements"], "occurrence_id", owner.removeprefix("occurrence:"))

    def config(self, owner):
        if owner == "parent":
            return self.bundle["parent"].get("config", {})
        if not owner.startswith('occurrence:') or '/' in owner:
            raise CommandError('collection requires an exact owner', kind='identity_conflict', ref=owner)
        row = self.occurrence(owner)
        return self.bundle["shots"][row["shot_id"]]["internal_timeline"]

    def collection(self, owner, kind):
        config = self.config(owner)
        if owner == "parent" and kind == "clip":
            parent = self.bundle["parent"]
            return (parent.get('clips', []) if self.parent_clip_path == ('clips',)
                    else config.get('clips', []))
        return config.get(kind + "s", [])

    def registry(self, owner):
        if owner != 'parent':
            return self.config(owner).get('registry', {})
        from astrid.core.timeline.parent_registry import (
            ParentRegistryError,
            effective_parent_registry,
        )
        parent = self.bundle['parent']
        try:
            registry = effective_parent_registry(parent)
        except ParentRegistryError as exc:
            raise CommandError(str(exc)) from exc
        if any(isinstance(obj, dict) and 'registry' in obj and not isinstance(obj['registry'], dict)
               for obj in (parent, parent.get('config', {}))):
            raise CommandError('media edit requires an authored asset registry')
        return registry

    def entity(self, ref, *, optional=False):
        if optional and ref.startswith('occurrence:') and '/' in ref:
            owner = ref.split('/', 1)[0]
            if self.entity(owner, optional=True) is None:
                return None
        if '/text-binding:' in ref:
            from .native_text_binding import binding_key, binding_rows
            owner, identity = ref.rsplit('/text-binding:', 1)
            rows = binding_rows(self, owner)
            return next((row for row in rows if binding_key(row) == identity), None)
        if '/asset:' in ref:
            owner, identity = ref.rsplit('/asset:', 1)
            assets = self.registry(owner).get('assets', {})
            if not isinstance(assets, dict):
                raise CommandError('asset collection must be an object', ref=ref)
            if identity not in assets and not optional:
                raise CommandError('missing asset identity', kind='identity_conflict', ref=ref)
            if identity in assets and not isinstance(assets[identity], dict):
                raise CommandError('asset binding must be an object', ref=ref)
            return assets.get(identity)
        if '/clip:' in ref or '/track:' in ref:
            owner, entity = ref.rsplit('/', 1)
            kind, identity = entity.split(':', 1)
            return one(self.collection(owner, kind), "id", identity, optional=optional)
        if ref == "parent":
            return self.bundle["parent"]
        root = root_identity(ref)
        if root is not None:
            kind, identity = root
            if kind == 'occurrence':
                return one(self.bundle['placements'], 'occurrence_id', identity, optional=optional)
            shot = self.bundle['shots'].get(identity)
            if shot is None:
                if optional:
                    return None
                raise CommandError('missing shot root', kind='identity_conflict', ref=ref)
            return {key: copy.deepcopy(shot[key]) for key in ('payload', 'internal_timeline')}
        if ref.endswith('/registry'):
            owner = ref.removesuffix('/registry')
            return self.registry(owner)
        if ref.endswith('/source'):
            return self.config(ref.removesuffix('/source'))
        if ref.endswith('/shot'):
            owner = ref.removesuffix('/shot')
            row = self.occurrence(owner)
            if (row['shot_id'] not in self.bundle['source_mapping']['shots']
                    and self.bundle['shots'][row['shot_id']].get('_created') is True):
                return {'shot_id': row['shot_id']}
            source = self.bundle['source_mapping']['shots'].get(row['shot_id'])
            if source is None:
                raise CommandError('shot identity requires its source or recorded creation',
                                   kind='history_unavailable', ref=ref)
            source = source['shot_id']
            from astrid.core.timeline.authoring_bundle import _authoring_shot_id
            identity = _authoring_shot_id(
                self.bundle['project_id'], self.bundle['timeline_id'], row['occurrence_id'],
                source, shared=True)
            if row['shot_id'] not in (source, identity):
                raise CommandError('occurrence authoring owner changed', kind='identity_conflict',
                                   ref=owner, path=['shot_id'])
            return {"shot_id": source}
        raise CommandError(f"unsupported entity reference {ref!r}")

    def validate_root(self, ref, value):
        root = root_identity(ref)
        if root is None or not isinstance(value, dict):
            raise CommandError('invalid authored root', ref=ref)
        kind, identity = root
        if kind == 'shot':
            if (set(value) != {'payload', 'internal_timeline'}
                    or not all(isinstance(child, dict) for child in value.values())):
                raise CommandError('shot root requires payload and internal_timeline objects', ref=ref)
        elif (value.get('occurrence_id') != identity
              or not isinstance(value.get('shot_id'), str)
              or root_identity('shot:' + value['shot_id']) is None
              or any(key in value for key in ('revision_id', 'shot_revision_id'))):
            raise CommandError('invalid authored occurrence identity or revision', ref=ref)
        elif value['shot_id'] not in self.bundle['shots']:
            raise CommandError('occurrence references missing shot', kind='identity_conflict', ref=ref)

    def _copy_placement_baseline(self, identity, value):
        from astrid.core.timeline.authoring_bundle import (
            UnsupportedAuthoringEditError,
            _copied_shot_baseline,
            _CopiedShot,
            _validate_placement_capabilities,
        )

        shot = self.bundle['shots'][value['shot_id']]
        if (type(shot) is not _CopiedShot or shot.get('_created') is not True
                or shot.source.get('occurrence_id') != identity
                or shot.source.get('source_occurrence_id') == identity):
            return
        _copied_shot_baseline(self.bundle, shot, value['shot_id'])
        source = self.entity('occurrence:' + shot.source['source_occurrence_id'])
        identities = {'occurrence_id', 'shot_id', 'stable_deep_link', 'output_identity'}
        if not equal({key: item for key, item in value.items() if key not in identities},
                     {key: item for key, item in source.items() if key not in identities}):
            raise CommandError('duplicate placement differs from its source', kind='identity_conflict')
        try:
            _validate_placement_capabilities(value, path='duplicate placement')
        except UnsupportedAuthoringEditError:
            # Only an exact native copy inherits legacy projection metadata.
            # Later edits still pass the compiler's normal changed-field guards.
            self.bundle['base_placements'][identity] = copy.deepcopy(value)

    def shot_item_map(self, ref):
        """Use the existing verified input to authenticate placement-local items."""
        from .commit import bundle_from_document

        owner = ref.removesuffix('/shot')
        shot = self.occurrence(owner)['shot_id']
        document = self.bundle['source_mapping'].get('_runtime_document')
        if document is None:
            raise CommandError('shot item source is unavailable', kind='history_unavailable', ref=ref)
        from astrid.core.timeline.authoring_bundle import _copied_shot_baseline, _CopiedShot

        current = self.bundle['shots'][shot]
        if (type(current) is _CopiedShot and current.get('_created') is True
                and shot not in self.bundle['source_mapping']['shots']):
            identity = owner.removeprefix('occurrence:')
            if (current.source.get('source_occurrence_id') != identity
                    or current.source.get('occurrence_id') != identity):
                raise CommandError('fork item owner changed', kind='identity_conflict', ref=ref)
            # Native fork provenance authenticates the exact original closure
            # and allocation. Its copied item IDs are already physical IDs.
            payload, _ = _copied_shot_baseline(self.bundle, current, shot)
            mapping = {row['item_id']: row['item_id'] for row in payload.get('items', [])}
            for value in (payload, current['payload']):
                self._item_ids(value.get('items', []), set(mapping), ref)
            return mapping
        baseline = bundle_from_document(document)
        source = self.bundle['source_mapping']['shots'].get(shot)
        if isinstance(source, dict) and shot == source.get('shot_id'):
            # Shared placement reopens the exact physical image, without adding
            # its newly created owner to the immutable batch-input document.
            image = one(document['shot_revisions'], 'revision_id', source.get('revision_id'))
            internal = one(document['internal_timeline_revisions'], 'revision_id',
                           image['internal_timeline_revision_id'])
            expected = {'shot_id': image['shot_id'], 'revision_id': image['revision_id'],
                        'content_digest': image['content_digest'],
                        'internal_timeline_id': internal['timeline_id'],
                        'internal_timeline_revision_id': internal['revision_id'],
                        'internal_timeline_content_digest': internal['content_digest'],
                        'item_id_map': {}}
            original = one(baseline['placements'], 'occurrence_id',
                           owner.removeprefix('occurrence:'), optional=True)
            if (shot != image['shot_id'] or not equal(source, expected)
                    or original is not None and not equal(
                        {**baseline['source_mapping']['shots'][original['shot_id']], 'item_id_map': {}},
                        expected)):
                raise CommandError('shot item source changed', kind='identity_conflict', ref=ref)
            # Physical item IDs need no placement correspondence. Guarded payload
            # replacement may change membership; never extend an alias map for it.
            for payload in (image['payload'], current['payload']):
                self._item_ids(payload.get('items', []), None, ref)
            return {row['item_id']: row['item_id'] for row in current['payload'].get('items', [])}
        original = one(baseline['placements'], 'occurrence_id', owner.removeprefix('occurrence:'))
        expected = baseline['source_mapping']['shots'].get(original['shot_id'])
        if shot != original['shot_id'] or source is None or not equal(source, expected):
            raise CommandError('shot item source changed', kind='identity_conflict', ref=ref)
        mapping = source.get('item_id_map')
        if (not isinstance(mapping, dict)
                or any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
                       for key, value in mapping.items())
                or len(set(mapping.values())) != len(mapping)):
            raise CommandError('shot item map is ambiguous', kind='identity_conflict', ref=ref)
        # Main leaves an un-copied shot's authenticated map empty. Its exact
        # verified item identities are already local; absence of a map is not this case.
        if not mapping and shot == source['shot_id']:
            rows = baseline['shots'][shot]['payload'].get('items', [])
            mapping = {row['item_id']: row['item_id'] for row in rows}
        for payload in (baseline['shots'][shot]['payload'], self.bundle['shots'][shot]['payload']):
            self._item_ids(payload.get('items', []), set(mapping.values()), ref)
        return mapping

    @staticmethod
    def _item_ids(rows, expected, ref):
        if not isinstance(rows, list):
            raise CommandError('shot items must be an array', kind='identity_conflict', ref=ref)
        ids = [row.get('item_id') if isinstance(row, dict) else None for row in rows]
        if (any(not isinstance(identity, str) or not identity for identity in ids)
                or len(set(ids)) != len(ids) or expected is not None and set(ids) != expected):
            raise CommandError('shot item correspondence changed', kind='identity_conflict', ref=ref)

    def project_shot_items(self, ref, path, state):
        """Project item-bearing aggregates without changing stored item identities."""
        if state['state'] != 'present' or tuple(path[:1]) != ('payload',):
            return state
        from astrid.core.timeline.authoring_bundle import _remap_known_item_references

        mapping = self.shot_item_map(ref)
        shot = self.occurrence(ref.removesuffix('/shot'))['shot_id']
        source = self.bundle['source_mapping']['shots'].get(shot, {})
        expected = None if shot == source.get('shot_id') else set(mapping)
        value = state['value']
        if len(path) == 1 and isinstance(value, dict) and 'items' in value:
            self._item_ids(value['items'], expected, ref)
            return present(_remap_known_item_references(value, mapping))
        if list(path) == ['payload', 'items']:
            self._item_ids(value, expected, ref)
            return present(_remap_known_item_references({'items': value}, mapping)['items'])
        if len(path) == 3 and path[2].startswith('item_id:'):
            identity = path[2].split(':', 1)[1]
            if not isinstance(value, dict) or value.get('item_id') != identity or identity not in mapping:
                raise CommandError('shot item replacement changed identity', kind='identity_conflict', ref=ref)
            return present(_remap_known_item_references({'items': [value]}, mapping)['items'][0])
        if len(path) == 4 and path[3] in ('item_id', 'id'):
            identity = path[2].split(':', 1)[1]
            if value != identity or identity not in mapping:
                raise CommandError('shot item identity cannot change', kind='identity_conflict', ref=ref)
            return present(mapping[identity])
        return state

    def _shot_item_path(self, ref, path):
        if (not ref.endswith('/shot') or tuple(path[:2]) != ('payload', 'items')
                or len(path) < 3 or not path[2].startswith('item_id:')):
            return path
        mapping = self.shot_item_map(ref)
        identity = path[2].split(':', 1)[1]
        if identity not in mapping:
            raise CommandError('unknown published item identity', kind='identity_conflict', ref=ref, path=path)
        shot = self.occurrence(ref.removesuffix('/shot'))['shot_id']
        rows = self.bundle['shots'][shot]['payload']['items']
        row = one(rows, 'item_id', mapping[identity])
        return [*path[:2], str(rows.index(row)), *path[3:]]

    def get(self, ref, path):
        if ref == 'parent' and list(path) == ['occurrences']:
            return present(copy.deepcopy(self.bundle['placements']))
        if '/collection:' in ref:
            from ._collections import shape
            owner, kind = ref.rsplit('/collection:', 1)
            if path:
                raise CommandError('collection shape requires the whole guard', ref=ref)
            if owner != 'parent' and self.entity(owner, optional=True) is None:
                return copy.deepcopy(ABSENT)
            return present(shape(self, owner, kind))
        if ref == "timeline":
            if path:
                raise CommandError("timeline replacement requires the whole closure")
            from .commit import document_from_bundle
            image = document_from_bundle(self.bundle)['parent_revision']
            return present({key: image[key] for key in ('revision_id', 'content_digest')})
        value = self.entity(ref, optional=True)
        if value is None:
            return copy.deepcopy(ABSENT)
        if ref.endswith('/shot') and tuple(path[:1]) == ('payload',):
            # Authored content in published identities, never the materialized alias.
            from astrid.core.timeline.authoring_bundle import _remap_known_item_references
            owner = ref.removesuffix('/shot')
            shot = self.occurrence(owner)['shot_id']
            mapping = self.bundle['source_mapping']['shots'].get(shot, {}).get('item_id_map', {})
            if (tuple(path[:2]) == ('payload', 'items')
                    or list(path) == ['payload'] and self.bundle['shots'][shot]['payload'].get('items')):
                mapping = self.shot_item_map(ref)
            value = {'payload': _remap_known_item_references(self.bundle['shots'][shot]['payload'],
                                                           {v: k for k, v in mapping.items()})}
        path = self._shot_item_path(ref, path)
        for key in path:
            if isinstance(value, dict) and key in value:
                value = value[key]
            elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
                value = value[int(key)]
            else:
                return copy.deepcopy(ABSENT)
        return present(value)

    def matches(self, ref, path, expected):
        actual = self.get(ref, path)
        if equal(actual, expected):
            return True
        if (root_identity(ref) is not None and ref.startswith('occurrence:')
                and expected['state'] == actual['state'] == 'present'):
            if not path and isinstance(expected['value'], dict):
                before = copy.deepcopy(expected['value'])
                if not self.matches(ref, ['shot_id'], present(before.get('shot_id'))):
                    return False
                before['shot_id'] = actual['value']['shot_id']
                return equal(before, actual['value'])
            if list(path) == ['shot_id']:
                from astrid.core.timeline.authoring_bundle import _authoring_shot_id
                source = self.entity(ref + '/shot')['shot_id']
                materialized = _authoring_shot_id(self.bundle['project_id'], self.bundle['timeline_id'],
                                                ref.removeprefix('occurrence:'), source, shared=True)
                return expected['value'] in (source, materialized) and actual['value'] in (source, materialized)
        if (root_identity(ref) is not None and ref.startswith('shot:') and not path
                and expected['state'] == actual['state'] == 'present'):
            # The verified source link is compiler output, not authored root data.
            source = self.bundle['source_mapping']['shots'].get(ref[5:], {})
            link = actual['value']['payload'].get('internal_timeline_revision_id')
            if link is not None and link == source.get('internal_timeline_revision_id'):
                before, after = copy.deepcopy(expected['value']), copy.deepcopy(actual['value'])
                before['payload'].pop('internal_timeline_revision_id', None)
                after['payload'].pop('internal_timeline_revision_id', None)
                return equal(before, after)
        if (ref.endswith('/shot') and list(path) == ['shot_id']
                and expected['state'] == actual['state'] == 'present'
                and isinstance(expected.get('value'), str)):
            from astrid.core.timeline.authoring_bundle import _authoring_shot_id
            # The only admitted source change is this occurrence's exact main
            # materialization of the original source, never a prefix heuristic.
            occurrence = self.occurrence(ref.removesuffix('/shot'))
            def materialized(source):
                return _authoring_shot_id(self.bundle['project_id'], self.bundle['timeline_id'],
                                          occurrence['occurrence_id'], source, shared=True)
            return (actual['value'] == materialized(expected['value'])
                    or expected['value'] == occurrence['shot_id'] == materialized(actual['value']))
        return False

    def set(self, ref, path, state):
        if ref == 'parent' and list(path) == ['occurrences']:
            before = self.bundle['placements']
            after = state.get('value')
            if (state['state'] != 'present' or not isinstance(after, list)
                    or len(after) != len(before)
                    or not equal(sorted(after, key=lambda row: row['occurrence_id']),
                                 sorted(before, key=lambda row: row['occurrence_id']))):
                raise CommandError('occurrence order writes must preserve every authored row',
                                   kind='identity_conflict', ref=ref, path=path)
            self.bundle['placements'] = copy.deepcopy(after)
            return
        if '/text-binding:' in ref:
            from .native_text_binding import binding_key, binding_rows
            if path:
                raise CommandError('text bindings are written whole', ref=ref)
            owner, identity = ref.rsplit('/text-binding:', 1)
            rows = binding_rows(self, owner)
            existing = self.entity(ref, optional=True)
            if state['state'] == 'present' and (not isinstance(state['value'], dict)
                                               or binding_key(state['value']) != identity):
                raise CommandError('text binding row differs from its key', ref=ref)
            if existing is not None:
                index = rows.index(existing)
                if state['state'] == 'absent':
                    rows.pop(index)
                else:
                    rows[index] = copy.deepcopy(state['value'])
            elif state['state'] == 'present':
                shot = self.occurrence(owner)['shot_id']
                self.bundle['shots'][shot]['payload'].setdefault('text_bindings', rows)
                rows.append(copy.deepcopy(state['value']))
            return
        if ref == 'parent' and tuple(path) in (('registry', 'assets'), ('config', 'registry', 'assets')):
            parent = self.bundle['parent']
            config = self.config('parent')
            if 'registry' in parent and 'registry' in config:
                self.registry('parent')
                # These are aliases of one guarded container, just like asset
                # leaves. One recorded creation/removal must reach both mirrors.
                for registry in (parent['registry'], config['registry']):
                    if state['state'] == 'absent':
                        registry.pop('assets', None)
                    else:
                        registry['assets'] = copy.deepcopy(state['value'])
                return
        if '/asset:' in ref:
            owner, identity = ref.rsplit('/asset:', 1)
            self.registry(owner)
            if not path and state['state'] == 'present' and not isinstance(state['value'], dict):
                raise CommandError('asset binding must be an object', ref=ref)
            prefixes = ([['registry']] if 'registry' in self.bundle['parent'] else []) if owner == 'parent' else []
            if owner != 'parent' or 'registry' in self.config(owner):
                prefixes.append(['config', 'registry'] if owner == 'parent' else ['registry'])
            if not prefixes:
                raise CommandError('asset write requires an authored registry', ref=ref)
            for prefix in prefixes:
                self.set('parent' if owner == 'parent' else owner + '/source',
                         [*prefix, 'assets', identity, *path], state)
            return
        if ref == "timeline":
            pin = state.get('value', {})
            target = self.closures.get(pin.get('revision_id'))
            if path or state.get('state') != 'present' or target is None:
                raise CommandError("timeline replacement requires a verified historical closure",
                                   kind='history_unavailable', ref=ref)
            image = target['source_mapping']['_runtime_document']['parent_revision']
            if pin != {key: image[key] for key in ('revision_id', 'content_digest')}:
                raise CommandError("historical closure pin mismatch", ref=ref)
            replacement = copy.deepcopy(target)
            self.bundle.clear()
            self.bundle.update(replacement)
            return
        root = root_identity(ref)
        if root is not None and (not path or root[0] == 'shot'):
            kind, identity = root
            if path:
                raise CommandError('shot writes require the complete authored aggregate', ref=ref, path=path)
            if state['state'] == 'present':
                self.validate_root(ref, state['value'])
            if kind == 'occurrence':
                existing = self.entity(ref, optional=True)
                rows = self.bundle['placements']
                if existing is not None:
                    index = rows.index(existing)
                    if state['state'] == 'absent':
                        rows.pop(index)
                    else:
                        rows[index] = copy.deepcopy(state['value'])
                elif state['state'] == 'present':
                    rows.append(copy.deepcopy(state['value']))
                    self._copy_placement_baseline(identity, state['value'])
            elif state['state'] == 'absent':
                if any(row['shot_id'] == identity for row in self.bundle['placements']):
                    raise CommandError('shot has surviving occurrences', kind='surviving_dependents', ref=ref)
                self.bundle['shots'].pop(identity, None)
            else:
                shot = self.bundle['shots'].get(identity)
                if shot is None:
                    # Reopening the exact input recovers compiler baselines for
                    # an existing root restored after removal, never latest data.
                    source = self.bundle['source_mapping'].get('_runtime_document')
                    if identity in self.bundle['source_mapping']['shots']:
                        if source is None:
                            raise CommandError('restoring a source shot requires its exact input closure',
                                               kind='history_unavailable', ref=ref)
                        from .commit import bundle_from_document
                        shot = bundle_from_document(source)['shots'].get(identity)
                    if shot is None:
                        shot = {'shot_id': identity}
                self.bundle['shots'][identity] = {**shot, **copy.deepcopy(state['value'])}
            return
        if root is None and ref.startswith(('shot:', 'occurrence:')) and '/' not in ref:
            raise CommandError('invalid authored root reference', ref=ref)
        if not path:
            owner, entity = ref.rsplit('/', 1)
            kind, identity = entity.split(':', 1)
            rows = self.collection(owner, kind)
            existing = self.entity(ref, optional=True)
            if state['state'] == 'present' and (not isinstance(state['value'], dict)
                                               or state['value'].get('id') != identity):
                raise CommandError('row identity differs from its scoped reference',
                                   kind='identity_conflict', ref=ref)
            if existing is not None:
                index = rows.index(existing)
                if state["state"] == "absent":
                    rows.pop(index)
                else:
                    rows[index] = copy.deepcopy(state["value"])
            elif state["state"] == "present":
                rows.append(copy.deepcopy(state["value"]))
            return
        value = self.entity(ref)
        if ref.endswith('/shot') and tuple(path[:1]) == ('payload',):
            # Only the occurrence's own shot root; display IDs never select it.
            shot = self.occurrence(ref.removesuffix('/shot'))['shot_id']
            if (tuple(path[:2]) == ('payload', 'items') or len(path) == 1):
                state = self.project_shot_items(ref, path, state)
            path = self._shot_item_path(ref, path)
            if len(path) == 1:
                if state['state'] != 'present' or not isinstance(state['value'], dict):
                    raise CommandError('shot payload requires an object', ref=ref, path=path)
                self.bundle['shots'][shot]['payload'] = copy.deepcopy(state['value'])
                return
            value, path = self.bundle['shots'][shot]['payload'], tuple(path[1:])
        for key in path[:-1]:
            if (state['state'] == 'absent' and isinstance(value, dict)
                    and (key not in value or value[key] is None)):
                return
            if isinstance(value, dict):
                if key not in value or not isinstance(value[key], (dict, list)):
                    raise CommandError("write requires recorded container creation", ref=ref, path=path)
                value = value[key]
            elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
                value = value[int(key)]
            else:
                raise CommandError("write requires recorded container creation", ref=ref, path=path)
        if not isinstance(value, (dict, list)):
            raise CommandError("property parent must be an object", ref=ref, path=path)
        if isinstance(value, dict):
            if state["state"] == "absent":
                value.pop(path[-1], None)
            else:
                value[path[-1]] = copy.deepcopy(state["value"])
        elif path[-1].isdecimal() and int(path[-1]) < len(value):
            if state["state"] == "absent":
                raise CommandError("list members cannot be cleared", ref=ref, path=path)
            value[int(path[-1])] = copy.deepcopy(state["value"])
        else:
            raise CommandError("list member is outside the recorded collection", ref=ref, path=path)
