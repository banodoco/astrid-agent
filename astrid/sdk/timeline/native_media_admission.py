"""Explicit media inputs for guarded document patches, captured by Runtime."""
from __future__ import annotations

import copy

from ._command_state import CommandError, equal
from ._preexisting import split_findings

PIN = {
    'type': 'object', 'additionalProperties': False,
    'required': ['media_id', 'content_digest'],
    'properties': {key: {'type': 'string', 'pattern': '^sha256:[0-9a-f]{64}$'}
                   for key in ('media_id', 'content_digest')},
}
SELECTION = {
    'type': 'object', 'additionalProperties': False,
    'required': ['asset', 'generation_id', 'variant_id', 'object_id', 'catalog_digest'],
    'properties': {key: {'type': 'string', 'minLength': 1} for key in
                   ('asset', 'generation_id', 'variant_id', 'object_id', 'catalog_digest')},
}
PARAMETERS = {
    'type': 'object', 'additionalProperties': False, 'required': ['inputs'],
    'properties': {'inputs': {'type': 'array', 'minItems': 1, 'uniqueItems': True,
        'items': {'type': 'object', 'additionalProperties': False, 'required': ['pin'],
                  'properties': {'pin': PIN, 'selection': SELECTION}}}},
}


def _registry_entries(bundle):
    """Yield ``((scope, asset), entry)`` for entries naming a catalog association."""
    parent = bundle['parent']
    scopes = [('parent', parent), ('config', parent.get('config', {})),
              *((shot_id, shot['internal_timeline']) for shot_id, shot in bundle['shots'].items())]
    for scope_key, scope in scopes:
        for asset, entry in scope.get('registry', {}).get('assets', {}).items():
            if isinstance(entry, dict) and ('generationId' in entry or 'variantId' in entry):
                yield (scope_key, asset), entry


def registry_selections(bundle):
    """Complete catalog associations in authored registries, without digest lookup.

    Incomplete entries are not associations; ``check_registry_associations``
    judges them, so reading any bundle (including an unchanged base) never raises.
    """
    for (_, asset), entry in _registry_entries(bundle):
        if entry.get('generationId') and entry.get('variantId'):
            yield {'asset': asset, 'generation_id': entry['generationId'],
                   'variant_id': entry['variantId'], 'object_id': entry.get('media_id')}


def check_registry_associations(candidate, base):
    """Refuse an incomplete association the candidate introduces or changes.

    An entry already incomplete and identical in ``base`` is pre-existing and
    stays editable (#387 pattern).
    """
    def incomplete(bundle):
        return {key: (entry.get('generationId'), entry.get('variantId'), entry.get('media_id'))
                for key, entry in _registry_entries(bundle)
                if not (entry.get('generationId') and entry.get('variantId'))}

    introduced, _ = split_findings(incomplete(candidate), incomplete(base))
    if introduced:
        raise CommandError('catalog association requires both generation and variant')


def capture_media_admissions(port, calls):
    """Called only by the provider with the authenticated request-private port."""
    from runtime_protocol.errors import ConflictError, NotFoundError, ValidationError

    def capture(reader, value, path):
        try:
            return reader(copy.deepcopy(value))
        except (ConflictError, NotFoundError, ValidationError) as exc:
            raise CommandError(str(exc), kind='dependency_changed' if isinstance(exc, ConflictError)
                               else 'invalid_media_selection', path=path) from exc

    verified = []
    for ordinal, call in enumerate(calls):
        if call['op'] != 'media.admit':
            continue
        for index, value in enumerate(call['args']['inputs']):
            pin = value['pin']
            path = ['commands', str(ordinal), 'args', 'inputs', str(index)]
            raw = capture(port.raw_media_info, pin, [*path, 'pin'])
            if raw.get('object_id') != pin['media_id'] or pin['content_digest'] != pin['media_id']:
                raise CommandError('Runtime raw media differs from the exact pin', kind='dependency_changed')
            verified.append({**raw, 'pin': copy.deepcopy(pin)})
            if 'selection' in value:
                selected = capture(port.selected_media, value['selection'], [*path, 'selection'])
                if (selected.get('selection') != value['selection']
                        or value['selection']['object_id'] != pin['media_id']):
                    raise CommandError('Runtime catalog selection differs from the media pin', kind='dependency_changed')
                verified.append(selected)
    return verified


def admit(ctx, command):
    if command['select'] is not None:
        raise CommandError('media admission has no document selection')
    for value in command['args']['inputs']:
        pin = value['pin']
        matches = [row for row in ctx.verified_media if equal(row.get('pin'), pin)]
        if (pin['media_id'] != pin['content_digest'] or not matches
                or any(not equal(row, matches[0]) for row in matches)
                or matches[0].get('object_id') != pin['media_id']):
            raise CommandError('media admission requires exact Runtime input', kind='dependency_changed')
        if not any(equal(row['pin'], pin) for row in ctx.record['dependencies']):
            ctx.dependency('media.input:' + pin['media_id'], 'media', pin)
        if 'selection' in value:
            selected = value['selection']
            if (selected['object_id'] != pin['media_id']
                    or not any(equal(row.get('selection'), selected) for row in ctx.verified_media)):
                raise CommandError('media admission requires exact Runtime catalog input', kind='dependency_changed')
            ctx.dependency('media.selection:' + selected['catalog_digest'], 'media', selected)


def validate_admissions(bundle, calls):
    """Every admitted object and association must be consumed by the final edit."""
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate

    inputs = [value for call in calls if call['op'] == 'media.admit' for value in call['args']['inputs']]
    if not inputs:
        return
    media = compile_authoring_candidate(bundle).publication['dependency_manifest']['media']
    from .commit import bundle_from_document

    original = bundle_from_document(bundle['source_mapping']['_runtime_document'])
    check_registry_associations(bundle, original)
    associations = list(registry_selections(bundle))
    old_associations = list(registry_selections(original))
    selected_inputs = [{key: val for key, val in value['selection'].items() if key != 'catalog_digest'}
                       for value in inputs if 'selection' in value]
    if any(row not in old_associations and row not in selected_inputs for row in associations):
        raise CommandError('new catalog association requires exact Runtime admission', kind='dependency_changed')
    for value in inputs:
        if value['pin'] not in media:
            raise CommandError('admitted media is not referenced by the candidate', kind='dependency_changed')
        if 'selection' in value:
            selected = {key: val for key, val in value['selection'].items() if key != 'catalog_digest'}
            if selected not in associations:
                raise CommandError('admitted catalog association is not referenced by the candidate', kind='dependency_changed')
