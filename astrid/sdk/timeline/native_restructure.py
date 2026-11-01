"""Chapter restructuring from the provider's authenticated current closure."""
from __future__ import annotations

import copy

from ._command_state import ABSENT, CommandError, equal, present

PARAMETERS = {'type': 'object', 'properties': {}, 'additionalProperties': False}


def restructure(ctx, command):
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline_restructure import assert_equivalent, split

    bundle = ctx.state.bundle
    document = bundle.get('source_mapping', {}).get('_runtime_document')
    pin = bundle['base_parent']
    # The public pair loader retains publication provenance; Runtime checks
    # that pair against the request/history. The immutable image pins content.
    if (document is None or document['project_id'] != bundle['project_id']
            or document['timeline_id'] != bundle['timeline_id']
            or set(pin) not in ({'revision_id', 'content_digest'},
                                {'revision_id', 'content_digest', 'publication_id'})
            or ('publication_id' in pin and (not isinstance(pin['publication_id'], str)
                                            or not pin['publication_id'].strip()))
            or {key: document['parent_revision'][key] for key in ('revision_id', 'content_digest')}
            != {key: pin[key] for key in ('revision_id', 'content_digest')}):
        raise CommandError('restructure requires the authenticated current closure',
                           kind='history_unavailable')
    if command['select'] != 'parent':
        raise CommandError('restructure requires parent ownership')
    ctx.read('timeline')
    working = Checkout(copy.deepcopy(bundle))
    working._sync_film_length()
    after = split(working.document())
    assert_equivalent(working.bundle, after)
    if equal(bundle['placements'], after['placements']):
        raise CommandError('no chapter restructure is pending', kind='invalid_commands')
    # The native recorder owns membership and allocation. No caller supplies
    # child payloads, managed edges, or an alternate source document.
    for identity in sorted(set(after['shots']) - set(bundle['shots'])):
        shot = after['shots'][identity]
        ctx.create_entity('shot:' + identity, kind='shot', owner=None,
                          value={key: shot[key] for key in ('payload', 'internal_timeline')})
    for identity in sorted(set(after['shots']) & set(bundle['shots'])):
        ref = 'shot:' + identity
        value = {key: after['shots'][identity][key] for key in ('payload', 'internal_timeline')}
        if not equal(ctx.read(ref), value):
            ctx.write(ref, [], value)
    prior = {row['occurrence_id'] for row in bundle['placements']}
    retained = {row['occurrence_id'] for row in after['placements']}
    for row in list(bundle['placements']):
        ctx.read('occurrence:' + row['occurrence_id'])
        if row['occurrence_id'] not in retained:
            ctx.clear('occurrence:' + row['occurrence_id'], [])
    for row in after['placements']:
        ref = 'occurrence:' + row['occurrence_id']
        if row['occurrence_id'] in prior:
            ctx.write(ref, [], row)
        else:
            ctx.create_entity(ref, kind='occurrence', owner='parent', value=row)
    if not equal(ctx.read('parent', ['occurrences']), after['placements']):
        ctx.write('parent', ['occurrences'], after['placements'])
    parent = after['parent']
    for key in sorted(set(bundle['parent']) | set(parent), key=lambda key: (key == 'config', key)):
        ctx.read('parent', [key])
        value = present(parent[key]) if key in parent else ABSENT
        if not equal(ctx.state.get('parent', [key]), value):
            ctx.assign('parent', [key], value)
