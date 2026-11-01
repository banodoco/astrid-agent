"""Guarded payload patches over the existing lossless command recorder."""
from __future__ import annotations

import copy

from ._command_state import ABSENT, CommandError, equal, present


def identity_key(ref, path):
    """Only schema-owned collections have identity traversal; extension arrays do not."""
    path = tuple(path)
    if ref == 'parent' and path == ('occurrences',):
        return 'occurrence_id'
    if ((ref == 'parent' and path in (('clips',), ('config', 'clips'), ('config', 'tracks')))
            or (ref.startswith('internal') and path in (('clips',), ('tracks',)))):
        return 'id'
    return None


def identities(value, key, *, ref, path):
    if not key:
        raise CommandError('array has no schema-defined identity', kind='idless_list_address', ref=ref, path=path)
    if not isinstance(value, list):
        raise CommandError('collection must be an array', ref=ref, path=path)
    result = [row.get(key) if isinstance(row, dict) else None for row in value]
    if (any(not isinstance(item, str) or not item for item in result)
            or len(set(result)) != len(result)):
        raise CommandError('missing or duplicate collection identity', kind='identity_conflict', ref=ref, path=path)
    return result


def null_clip_mirror(payload):
    config = payload.get('config', {})
    return isinstance(config, dict) and (
        'clips' in payload and payload['clips'] is None and isinstance(config.get('clips'), list)
        or 'clips' in config and config['clips'] is None and isinstance(payload.get('clips'), list))


def location(root, ref, path):
    node = root
    for index, token in enumerate(path):
        if isinstance(node, list):
            key = identity_key(ref, path[:index])
            ids = identities(node, key, ref=ref, path=path[:index])
            prefix, separator, identity = token.partition(':')
            if not separator or prefix != key:
                raise CommandError('array traversal requires an identity token', kind='idless_list_address',
                                   ref=ref, path=path[:index + 1])
            if identity not in ids:
                if index == len(path) - 1:
                    return node, None
                raise CommandError('missing array ancestor', ref=ref, path=path[:index + 1])
            item = ids.index(identity)
        elif isinstance(node, dict):
            item = token
        else:
            raise CommandError('path ancestor must be an object or array', ref=ref, path=path[:index])
        if index == len(path) - 1:
            return node, item
        if isinstance(node, dict) and item not in node:
            raise CommandError('missing path ancestor', ref=ref, path=path[:index + 1])
        node = node[item]
    return None, None


def storage_path(root, ref, path):
    path = list(path)
    if ref == 'parent' and path:
        config = root.get('config', {})
        for mirror in ('clips', 'registry'):
            if path[:1] == [mirror] and mirror not in root and isinstance(config, dict) and mirror in config:
                return ['config', *path]
            if (path[:2] == ['config', mirror] and mirror in root
                    and (not isinstance(config, dict) or mirror not in config)):
                return path[1:]
    return path


def value_at(root, ref, path):
    path = storage_path(root, ref, path)
    if not path:
        return present(root)
    node, key = location(root, ref, path)
    return ABSENT.copy() if key is None or isinstance(node, dict) and key not in node else present(node[key])


def replace_at(root, ref, path, replacement):
    path = storage_path(root, ref, path)
    if not path:
        if replacement == ABSENT or not isinstance(replacement['value'], dict):
            raise CommandError('payload root must remain an object', kind='managed_closure_write', ref=ref)
        return copy.deepcopy(replacement['value'])
    node, key = location(root, ref, path)
    if key is None:
        raise CommandError('use list.insert for a new item', ref=ref, path=path)
    if isinstance(node, list):
        if replacement == ABSENT:
            raise CommandError('use list.remove for an item', ref=ref, path=path)
        identity = identity_key(ref, path[:-1])
        if not isinstance(replacement['value'], dict) or replacement['value'].get(identity) != node[key][identity]:
            raise CommandError('item identity cannot change', kind='identity_conflict', ref=ref, path=path)
    if replacement == ABSENT:
        node.pop(key, None)
    else:
        node[key] = copy.deepcopy(replacement['value'])
    return root


def normalized_path(ref, path):
    path = list(path)
    if ref == 'parent' and path[:1] == ['clips']:
        path.insert(0, 'config')
    if ref == 'parent' and path[:1] == ['registry']:
        path.insert(0, 'config')
    return path


class Closure:
    """Resolve one verified boundary and retain its physical-to-authoring projection."""
    def __init__(self, bundle, *, source_bundles=(), inverse_records=()):
        from .commit import document_from_bundle

        self.bundle = bundle
        self.document = document_from_bundle(
            bundle, source_bundles=source_bundles, inverse_records=inverse_records)
        self.images = {'parent': self.document['parent_revision']}
        self.images.update(('shot-revision:' + image['revision_id'], image)
                           for image in self.document['shot_revisions'])
        self.images.update(('internal-revision:' + image['revision_id'], image)
                           for image in self.document['internal_timeline_revisions'])
        shots = {row['revision_id']: row for row in self.document['shot_revisions']}
        placements = {row['occurrence_id']: row for row in bundle['placements']}
        self.owners = {}
        for row in self.document['parent_revision']['payload']['occurrences']:
            shot = shots[row['shot_revision_id']]
            owner = 'occurrence:' + row['occurrence_id']
            local = placements[row['occurrence_id']]['shot_id']
            for ref in ('shot-revision:' + shot['revision_id'],
                        'internal-revision:' + shot['internal_timeline_revision_id']):
                self.owners.setdefault(ref, []).append((owner, local))

    def resolve(self, ref):
        if ref in self.images:
            return ref
        kind, _, identity = ref.partition(':')
        field = {'shot': 'shot_id', 'internal': 'timeline_id'}.get(kind)
        prefix = {'shot': 'shot-revision:', 'internal': 'internal-revision:'}.get(kind)
        found = [physical for physical, image in self.images.items()
                 if field and physical.startswith(prefix) and image[field] == identity]
        if len(found) != 1:
            raise CommandError('root alias is ambiguous' if found else 'root is outside the verified closure',
                               kind='ambiguous_ref' if found else 'outside_closure', ref=ref)
        return found[0]

    def payload(self, physical):
        if physical == 'parent':
            payload = copy.deepcopy(self.bundle['parent'])
            original = {row['occurrence_id']: row for row in self.images['parent']['payload']['occurrences']}
            payload['occurrences'] = []
            for row in self.bundle['placements']:
                published = original[row['occurrence_id']]
                payload['occurrences'].append({**copy.deepcopy(row), 'shot_id': published['shot_id'],
                                               'shot_revision_id': published['shot_revision_id']})
            return payload
        owner, local = self.owners[physical][0]
        shot = self.bundle['shots'][local]
        if physical.startswith('internal-revision:'):
            return copy.deepcopy(shot['internal_timeline'])
        from astrid.core.timeline.authoring_bundle import _remap_known_item_references

        mapping = self.bundle['source_mapping']['shots'].get(local, {}).get('item_id_map', {})
        return _remap_known_item_references(shot['payload'], {v: k for k, v in mapping.items()})

    def binding(self, submitted, physical, **boundary):
        image = self.images[physical]
        result = {'ref': submitted, 'physical_ref': physical,
                  'revision_id': image['revision_id'], 'content_digest': image['content_digest'],
                  'boundary': boundary}
        if physical != 'parent':
            result['owners'] = [owner + '/shot' for owner, _ in self.owners[physical]]
        return result

    def native_sites(self, physical, path):
        path = list(path)
        if physical == 'parent':
            if path[:1] == ['occurrences']:
                if len(path) < 2:
                    raise CommandError('occurrence membership is provider managed', kind='managed_closure_write')
                return [('occurrence:' + path[1].partition(':')[2], path[2:])]
            paths = [path]
            parent = self.bundle['parent']
            if path[:1] == ['clips'] or path[:2] == ['config', 'clips']:
                tail = path[1:] if path[0] == 'clips' else path[2:]
                if tail and tail[0].startswith('id:'):
                    return [('parent/clip:' + tail[0][3:], tail[1:])]
                paths = ([['clips', *tail]] if 'clips' in parent and (
                    not null_clip_mirror(parent) or isinstance(parent['clips'], list)) else [])
                if (isinstance(parent.get('config'), dict) and 'clips' in parent['config']
                        and (not null_clip_mirror(parent) or isinstance(parent['config']['clips'], list))):
                    paths.append(['config', 'clips', *tail])
                if not paths:
                    paths = [path]
            elif path[:1] == ['registry'] or path[:2] == ['config', 'registry']:
                tail = path[1:] if path[0] == 'registry' else path[2:]
                paths = ([['registry', *tail]] if 'registry' in parent else [])
                if isinstance(parent.get('config'), dict) and 'registry' in parent['config']:
                    paths.append(['config', 'registry', *tail])
                if not paths:
                    paths = [path]
            elif len(path) >= 3 and path[:2] == ['config', 'tracks'] and path[2].startswith('id:'):
                return [('parent/track:' + path[2][3:], path[3:])]
            return [('parent', value) for value in paths]
        sites = []
        for owner, local in self.owners[physical]:
            if physical.startswith('internal-revision:'):
                if len(path) >= 2 and path[0] in ('clips', 'tracks') and path[1].startswith('id:'):
                    sites.append((owner + '/' + path[0][:-1] + ':' + path[1][3:], path[2:]))
                else:
                    sites.append((owner + '/source', path))
            else:
                sites.append((owner + '/shot', ['payload', *path]))
        return sites

    def install(self, ctx, physical, path, replacement):
        if physical == 'parent' and path == ['occurrences']:
            rows = replacement.get('value')
            ordered = identities(rows, 'occurrence_id', ref=physical, path=path)
            before = self.payload(physical)['occurrences']
            if not equal(sorted(before, key=lambda row: row['occurrence_id']),
                         sorted(rows, key=lambda row: row['occurrence_id'])):
                raise CommandError('occurrence order writes must preserve every verified row',
                                   kind='managed_closure_write', ref=physical, path=path)
            # Keep each exact native owner link; submitted rows supply only order.
            native = {row['occurrence_id']: row for row in ctx.state.bundle['placements']}
            ctx.assign('parent', path, present([native[identity] for identity in ordered]))
            return
        if not path:
            before = self.payload(physical)
            after = replacement['value']
            for key in sorted(before.keys() | after.keys()):
                old = present(before[key]) if key in before else ABSENT
                new = present(after[key]) if key in after else ABSENT
                if not equal(old, new):
                    self.install(ctx, physical, [key], new)
            return
        for ref, native_path in self.native_sites(physical, path):
            # Occurrence replacements keep native authoring links inside the recorder.
            value = replacement
            if physical == 'parent' and path[:1] == ['occurrences'] and not native_path and replacement != ABSENT:
                value = copy.deepcopy(replacement)
                value['value'].pop('shot_revision_id', None)
                value['value'].pop('revision_id', None)
                value['value']['shot_id'] = ctx.state.entity(ref)['shot_id']
            previous_config = ctx.state.get(ref, native_path) if physical == 'parent' and native_path == ['config'] else ABSENT
            ctx.assign(ref, native_path, value)
            if physical == 'parent' and native_path in (['clips'], ['config', 'clips']):
                # Later identity writes must follow the newly assigned collection.
                ctx.state.parent_clip_path = (('clips',) if isinstance(self.bundle['parent'].get('clips'), list)
                                              else ('config', 'clips'))
            if ref.startswith('parent/clip:'):
                # The next change must observe the recorder's synchronized mirrors.
                ctx.compile_mirrors()
            if physical == 'parent' and native_path == ['config'] and value != ABSENT:
                for mirror in ('clips', 'registry'):
                    if mirror in self.bundle['parent'] and (mirror in value['value']
                            or mirror in previous_config.get('value', {})):
                        ctx.assign('parent', [mirror], present(value['value'][mirror]) if mirror in value['value'] else ABSENT)


def managed_edges(ref, payload):
    if ref == 'parent':
        rows = payload.get('occurrences', [])
        if not isinstance(rows, list):
            return None
        return [(row.get('occurrence_id'), row.get('shot_id'), row.get('shot_revision_id'), row.get('revision_id'))
                if isinstance(row, dict) else None for row in rows]
    if ref.startswith('shot'):
        return {key: payload[key] for key in ('internal_timeline_revision_id', 'shot_id', 'revision_id') if key in payload}
    return {key: payload[key] for key in ('timeline_id', 'revision_id') if key in payload}


def validate_payload(ref, payload):
    for path in ((['clips'], ['config', 'clips'], ['config', 'tracks'], ['occurrences'])
                 if ref == 'parent' else (['clips'], ['tracks']) if ref.startswith('internal') else ()):
        try:
            value = value_at(payload, ref, path)
        except CommandError:
            continue
        if value != ABSENT:
            if (ref == 'parent' and path in (['clips'], ['config', 'clips'])
                    and value['value'] is None and null_clip_mirror(payload)):
                continue
            identities(value['value'], identity_key(ref, path), ref=ref, path=path)
    if ref == 'parent':
        config = payload.get('config', {})
        for key in ('clips', 'registry'):
            if key in payload and isinstance(config, dict) and key in config and not equal(payload[key], config[key]):
                if key == 'clips' and null_clip_mirror(payload):
                    continue
                raise CommandError('contradictory parent mirrors', kind='identity_conflict', path=[key])


def allocations(closure, physical, before, after):
    created = []
    for path, kind in ((['config', 'tracks'], 'track'), (['clips'], 'clip')) if physical == 'parent' else (
            ((['tracks'], 'track'), (['clips'], 'clip')) if physical.startswith('internal') else ()):
        old, new = value_at(before, physical, path), value_at(after, physical, path)
        if physical == 'parent' and path == ['clips'] and (
                old == ABSENT and new == ABSENT
                or null_clip_mirror(before) and before.get('clips') is None):
            old, new = value_at(before, physical, ['config', 'clips']), value_at(after, physical, ['config', 'clips'])
        previous = [] if old == ABSENT else identities(old['value'], 'id', ref=physical, path=path)
        current = [] if new == ABSENT else identities(new['value'], 'id', ref=physical, path=path)
        owners = ['parent'] if physical == 'parent' else [owner for owner, _ in closure.owners[physical]]
        created.extend({'ref': owner + '/' + kind + ':' + identity, 'kind': kind, 'owner': owner}
                       for identity in current if identity not in previous for owner in owners)
    # Asset keys are identities even though the registry is an object rather than an identity list.
    if physical == 'parent' or physical.startswith('internal'):
        registry_path = ['registry']
        old, new = value_at(before, physical, registry_path), value_at(after, physical, registry_path)
        old_value, new_value = old.get('value'), new.get('value')
        old_assets = old_value.get('assets', {}) if isinstance(old_value, dict) else {}
        new_assets = new_value.get('assets', {}) if isinstance(new_value, dict) else {}
        if isinstance(old_assets, dict) and isinstance(new_assets, dict):
            owners = ['parent'] if physical == 'parent' else [owner for owner, _ in closure.owners[physical]]
            created.extend({'ref': owner + '/asset:' + identity, 'kind': 'asset', 'owner': owner}
                           for identity in new_assets if identity not in old_assets for owner in owners)
    return created


def verify_media(ctx, ref, before, after):
    def media(payload):
        scopes = [payload]
        if ref == 'parent' and isinstance(payload.get('config'), dict):
            scopes.append(payload['config'])
        nodes = []
        for scope in scopes:
            registry = scope.get('registry', {})
            assets = registry.get('assets', {}) if isinstance(registry, dict) else {}
            if isinstance(assets, dict):
                nodes.extend(assets.values())
            rows = scope.get('items' if ref.startswith('shot') else 'clips', [])
            if rows is None and ref == 'parent' and null_clip_mirror(payload):
                continue
            nodes.extend(rows)
        return {value for node in nodes if isinstance(node, dict)
                for key in ('media_id', 'object_id') if isinstance(value := node.get(key), str)}
    source = ctx.state.bundle['source_mapping']['_runtime_document']
    allowed = {pin['media_id'] for pin in source['dependency_manifest']['media']}
    allowed.update(row.get('selection', {}).get('object_id') for row in ctx.verified_media)
    allowed.update(row['object_id'] for row in ctx.verified_media
                   if row.get('pin') == {'media_id': row.get('object_id'),
                                         'content_digest': row.get('object_id')}
                   and isinstance(row.get('object_id'), str))
    if media(after) - media(before) - allowed:
        raise CommandError('patch references media outside Runtime-verified inputs', kind='dependency_changed', ref=ref)


def validate_submitted_change(change):
    ref, path, op = change['ref'], change['path'], change['op']
    if (not path and (op != 'set' or change['before'] == ABSENT)
            or ref.startswith('shot') and path[:1] == ['internal_timeline_revision_id']
            or ref == 'parent' and path[:1] == ['occurrences'] and (len(path) < 2 and op != 'list.move'
                or len(path) == 2 and op == 'remove'
                or len(path) > 2 and path[2] in ('occurrence_id', 'shot_id', 'shot_revision_id', 'revision_id'))):
        raise CommandError('closure membership and edges require semantic commands',
                           kind='managed_closure_write', ref=ref, path=path)
    if op == 'set' and change['before']['state'] == 'present':
        before, after = change['before']['value'], change['value']
        if not path and isinstance(before, dict) and isinstance(after, dict):
            if not equal(managed_edges(ref, before), managed_edges(ref, after)):
                raise CommandError('replacement changes provider-managed edges', kind='managed_closure_write', ref=ref)
        if ref == 'parent' and len(path) == 2 and path[0] == 'occurrences' and isinstance(before, dict) and isinstance(after, dict):
            keys = ('occurrence_id', 'shot_id', 'shot_revision_id', 'revision_id')
            if any(before.get(key) != after.get(key) for key in keys):
                raise CommandError('replacement changes provider-managed edges', kind='managed_closure_write', ref=ref, path=path)


def apply_change(root, change, *, physical=None):
    """Apply one guarded payload change; closure authority stays with the caller."""
    ref, path, op = change['ref'], change['path'], change['op']
    physical = physical or ref
    validate_submitted_change(change)
    old_root = copy.deepcopy(root)
    if op.startswith('list.'):
        state = value_at(root, physical, path)
        rows = state.get('value')
        key = identity_key(physical, path)
        ordered = identities(rows, key, ref=ref, path=path)
        if ordered != change['order_before']:
            error = CommandError('original collection order changed', kind='previous_value_mismatch', ref=ref, path=path)
            error.dependency_kind = 'collection'
            raise error
        identity = change['id']
        index = ordered.index(identity) if identity in ordered else None
        before = ABSENT.copy() if index is None else present(rows[index])
    else:
        before = value_at(root, physical, path)
    if not equal(before, change['before']) or op in ('remove', 'list.remove', 'list.move') and before == ABSENT:
        raise CommandError('patch previous value does not match', kind='previous_value_mismatch',
                           ref=ref, path=path, base=change['before'], theirs=before)
    if op.startswith('list.'):
        if op == 'list.insert':
            if before != ABSENT:
                raise CommandError('insert identity is occupied', kind='identity_collision', ref=ref, path=path)
            item = copy.deepcopy(change['value'])
            if not isinstance(item, dict) or item.get(key) != identity:
                raise CommandError('insert value has a different identity', kind='identity_conflict', ref=ref, path=path)
        else:
            item = rows.pop(index)
        if op != 'list.remove':
            remaining = identities(rows, key, ref=ref, path=path)
            anchor = change['after_id']
            if anchor == identity or anchor is not None and anchor not in remaining:
                raise CommandError('invalid list anchor', ref=ref, path=path)
            rows.insert(0 if anchor is None else remaining.index(anchor) + 1, item)
        after = present(rows)
        root = replace_at(root, physical, path, after)
    else:
        if path and path[-1] in ('id', 'occurrence_id'):
            if (len(path) > 1 and identity_key(physical, path[:-2]) == path[-1]
                    and path[-2].startswith(path[-1] + ':')):
                raise CommandError('item identity cannot change', kind='identity_conflict', ref=ref, path=path)
        after = present(change['value']) if op == 'set' else ABSENT.copy()
        root = replace_at(root, physical, path, after)
    # Mirrors share one guarded authored value, including explicit absence.
    if physical == 'parent':
        for mirror in ('clips', 'registry'):
            config = root.get('config', {})
            old_config = old_root.get('config', {})
            if mirror == 'clips' and null_clip_mirror(old_root) and path != ['config']:
                continue
            if (path == ['config'] and op == 'set' and mirror in old_root
                    and (mirror in old_config or mirror in config)):
                if mirror in config:
                    root[mirror] = copy.deepcopy(config[mirror])
                else:
                    root.pop(mirror, None)
            elif mirror in old_root and mirror in old_config:
                if path[:1] == [mirror]:
                    if mirror in root:
                        config[mirror] = copy.deepcopy(root[mirror])
                    else:
                        config.pop(mirror, None)
                elif path[:2] == ['config', mirror]:
                    if mirror in config:
                        root[mirror] = copy.deepcopy(config[mirror])
                    else:
                        root.pop(mirror, None)
    old_edges, new_edges = managed_edges(physical, old_root), managed_edges(physical, root)
    if physical == 'parent' and path == ['occurrences'] and op == 'list.move':
        old_edges = sorted(old_edges, key=lambda row: row[0])
        new_edges = sorted(new_edges, key=lambda row: row[0])
    if not equal(old_edges, new_edges):
        raise CommandError('replacement changes provider-managed edges', kind='managed_closure_write', ref=ref, path=path)
    validate_payload(physical, root)
    return root, before


def item_scoped_removal(closure, changes, physical, path):
    # Mixed structural replacements retain their aggregate order-restoration proof.
    path = normalized_path(physical, path)
    for change in changes:
        if change['op'].startswith('list.') and any(
                '/' in identity or '::' in identity for identity in change['order_before']):
            return False  # Literal patch identities need not be native entity refs.
        other = normalized_path(physical, change['path'])
        if change['op'] == 'list.remove' or path[:len(other)] != other:
            continue
        try:
            if closure.resolve(change['ref']) == physical:
                return False
        except CommandError:
            return False
    return True


def patch(ctx, command):
    closure = Closure(ctx.state.bundle)
    ctx.patch_closure = closure
    generated = []
    for ordinal, change in enumerate(command['args']['changes']):
        ref, path, op = change['ref'], change['path'], change['op']
        validate_submitted_change(change)
        expected = getattr(ctx, 'patch_replay_bindings', {}).get((ctx.record['ordinal'], ordinal))
        if expected is not None and ref.startswith(('shot-revision:', 'internal-revision:')) and ref not in closure.images:
            physical = expected
        else:
            physical = closure.resolve(ref)
        if expected is not None and physical != expected:
            raise CommandError('root alias changed its physical binding', kind='binding_changed', ref=ref, path=path)
        root = closure.payload(physical)
        old_root = copy.deepcopy(root)
        root, before = apply_change(root, change, physical=physical)
        verify_media(ctx, physical, old_root, root)
        created = allocations(closure, physical, old_root, root)
        ctx.record['created'].extend(created)
        # Previous operands are sequential observations, not refreshed command claims.
        if physical == 'parent' and path == ['occurrences'] and op == 'list.move':
            ctx.read('occurrence:' + change['id'])
        elif not op.startswith('list.'):
            for native_ref, native_path in closure.native_sites(physical, path):
                if native_path or native_ref == 'parent':
                    ctx.read(native_ref, native_path)
        start = len(ctx.record['writes'])
        if (op == 'list.remove' and identity_key(physical, path) == 'id'
                and item_scoped_removal(closure, command['args']['changes'], physical, path)):
            # Guard the complete removed row and collection shape separately;
            # surviving rows are not operands of a member removal.
            for native_ref, native_path in closure.native_sites(physical, [*path, 'id:' + change['id']]):
                owner, entity = native_ref.rsplit('/', 1)
                key = 'collection.' + entity.split(':', 1)[0] + 's'
                if not any(selected['key'] == key and selected['owner'] == owner
                           for selected in ctx.record['selections']):
                    ctx.select(key, owner)
                ctx.read(native_ref, native_path)
                ctx.clear(native_ref, native_path)
            ctx.compile_mirrors()
        else:
            closure.install(ctx, physical, path, value_at(root, physical, path))
        binding = closure.binding(ref, physical, command_ordinal=ctx.record['ordinal'], change_ordinal=ordinal,
                                  phase='input')
        # Bindings remain provider facts in the existing recorder; never command claims.
        actual_change = {**copy.deepcopy(change), 'before': copy.deepcopy(before), 'ref': physical}
        binding.update(key='doc.patch:' + str(ordinal), policy='fixed', dependencies=[],
                       path=normalized_path(physical, path), change=actual_change,
                       write_range=[start, len(ctx.record['writes'])])
        ctx.record['bindings'].append(binding)
        if physical != 'parent':
            from ._doc_patch_materialize import remember
            remember(closure, physical)
        generated.extend(item['ref'] for item in created)
    if generated != command.get('created', []):
        raise CommandError('doc.patch must declare every created identity', kind='capture_mismatch')


def inverse_changes(changes):
    """Reverse authoritative sequential effects; root/edge effects stay provider-only."""
    result = []
    for change in reversed(changes):
        inverse = {key: copy.deepcopy(change[key]) for key in ('ref', 'path')}
        op, before = change['op'], change['before']
        if op.startswith('list.'):
            identity = change['id']
            order = list(change['order_before'])
            previous = order[order.index(identity) - 1] if identity in order and order.index(identity) else None
            if op != 'list.insert':
                order.remove(identity)
            if op != 'list.remove':
                anchor = change['after_id']
                order.insert(0 if anchor is None else order.index(anchor) + 1, identity)
            inverse.update(id=identity, order_before=order)
            if op == 'list.insert':
                inverse.update(op='list.remove', before=present(change['value']))
            elif op == 'list.remove':
                inverse.update(op='list.insert', before=ABSENT.copy(), value=copy.deepcopy(before['value']),
                               after_id=previous)
            else:
                inverse.update(op='list.move', before=copy.deepcopy(before), after_id=previous)
        else:
            inverse.update(op='remove' if before == ABSENT else 'set',
                           before=present(change['value']) if op == 'set' else ABSENT.copy())
            if before != ABSENT:
                inverse['value'] = copy.deepcopy(before['value'])
        result.append(inverse)
    return result
