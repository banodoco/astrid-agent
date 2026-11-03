"""Source-bound authoring forks through the production commit caller."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import (
    _copy_shot_for_placement,
    _remap_known_item_references,
    publish_authoring_candidate,
)
from astrid.sdk.timeline.authoring_commit import authoring_commit_request
from astrid.sdk.timeline.commands import CommandError, execute
from astrid.sdk.timeline.commit import bundle_from_document
from tests.helpers.trusted_seed import seed_trusted_parent
from tests.timeline.test_authoring_bundle import AUDIO, NEW, OLD, _closure, _replace_values
from tests.timeline.test_authoring_commit_media import _timeline_state
from tests.timeline.test_authoring_commit_media import realm as realm


@pytest.fixture(params=[0, 1], ids=['first', 'second'])
def shared(realm, request):  # noqa: F811
    service, writer, initial = realm
    project = initial['project_id']
    media = [service.ingest(project, value, media_type=kind, idempotency_key=key)['data']['object_id']
             for value, kind, key in ((b'old', 'image/png', 'old'),
                                      (b'voice', 'audio/wav', 'voice'),
                                      (b'new', 'image/png', 'new'))]
    parent, shots, internals = _closure()
    publication = _replace_values({'project_id': project, 'timeline_id': 'film',
        'parent_revision_id': parent['revision_id'], 'parent_composition': parent['payload'],
        'shot_revisions': shots, 'internal_timeline_revisions': internals},
        {OLD: media[0], AUDIO: media[1]})
    publication['internal_timeline_revisions'][0]['timeline_id'] = 'film'
    seed_trusted_parent(service, publication, idempotency_key='shared-seed')
    head = writer.list_timeline_commits(project, 'film')['head']
    document = service._timeline_commits()[0].revision_closure(project, 'film', head['revision_id'])
    candidate = bundle_from_document(document)
    candidate['base_parent']['publication_id'] = head['publication_id']
    local = candidate['placements'][request.param]['shot_id']
    shot = candidate['shots'][local]
    shot['payload']['items'][0]['media_id'] = media[2]
    shot['internal_timeline']['clips'][0]['asset'] = 'new-picture'
    shot['internal_timeline']['registry']['assets']['new-picture'] = {
        'media_id': media[2], 'type': 'image', 'opaque_asset': 'new'}
    return service, writer, candidate, document, request.param


def test_selected_alias_forks_and_retains_exact_sibling(shared):
    service, writer, candidate, source, selected = shared
    frozen = copy.deepcopy(candidate)
    request = authoring_commit_request(candidate, writer)
    assert candidate == frozen
    assert request == authoring_commit_request(candidate, writer)
    assert request['base_revision_id'] == source['parent_revision']['revision_id']
    assert request['base_publication_id'] == candidate['base_parent']['publication_id']
    assert request['merge'] == 'atomic' and 'reads' not in request
    fork = request['commands'][0]
    occurrence = candidate['placements'][selected]['occurrence_id']
    assert fork['op'] == 'occurrence.fork' and fork['select'] == 'occurrence:' + occurrence
    assert fork['created'] and fork['bindings'] and fork['reads']
    assert request['commands'][-1]['op'] == 'media.admit'
    assert request['steps'][1]['patch']
    with pytest.raises(CommandError, match='outside Runtime-verified inputs'):
        execute(bundle_from_document(source), request['commands'], commit_request=request)
    state = _timeline_state(service)
    preview = writer.commit_timeline(candidate['project_id'], 'film', {**request, 'dry_run': True},
                                     idempotency_key='fork-preview')
    assert preview['data']['status'] == 'preview' and preview['receipt'] is None
    assert _timeline_state(service) == state
    result = publish_authoring_candidate(candidate, writer, idempotency_key='fork-save')
    saved = result['publication']
    document = saved['canonical_document']
    assert saved['status'] == 'accepted'
    assert saved['receipt']['result']['canonical_document'] == document
    assert preview['data']['canonical_document'] == document
    assert document['parent_revision']['payload']['occurrences'][1 - selected] == source['parent_revision']['payload']['occurrences'][1 - selected]
    assert source['shot_revisions'][0] in document['shot_revisions']
    assert source['internal_timeline_revisions'][0] in document['internal_timeline_revisions']
    first = document['parent_revision']['payload']['occurrences'][selected]
    assert first['shot_id'] == fork['args']['shot_id'] != source['shot_revisions'][0]['shot_id']
    authored = next(row for row in document['shot_revisions'] if row['shot_id'] == first['shot_id'])
    local = candidate['placements'][selected]['shot_id']
    expected = copy.deepcopy(candidate['shots'][local]['payload'])
    # The registered fork remaps only item identities and declared references.
    _, remap = _copy_shot_for_placement(source['shot_revisions'][0]['payload'],
        project_id=candidate['project_id'], timeline_id='film', occurrence_id=occurrence,
        authoring_shot_id=first['shot_id'], source_shot_id=source['shot_revisions'][0]['shot_id'])
    old_map = candidate['source_mapping']['shots'][local]['item_id_map']
    expected = _remap_known_item_references(expected,
        {old_map.get(key, key): value for key, value in remap.items()})
    expected['internal_timeline_revision_id'] = authored['internal_timeline_revision_id']
    assert authored['payload'] == expected
    internal = next(row for row in document['internal_timeline_revisions']
                    if row['revision_id'] == authored['internal_timeline_revision_id'])
    assert internal['payload'] == candidate['shots'][local]['internal_timeline']
    parent = copy.deepcopy(source['parent_revision']['payload'])
    parent['occurrences'][selected] = {**parent['occurrences'][selected],
        'shot_id': first['shot_id'], 'shot_revision_id': first['shot_revision_id']}
    assert document['parent_revision']['payload'] == parent
    assert document['dependency_manifest']['media'] == saved['dependency_manifest']['media']
    history = writer.list_timeline_commits(candidate['project_id'], 'film')
    entry = next(row for row in history['items'] if row['revision_id'] == saved['new_head'])
    assert entry['records']['commands'][0]['bindings']
    assert entry['records']['commands'][-1]['dependencies']
    assert entry['records']['steps'][0]['patch']
    accepted = _timeline_state(service)
    assert publish_authoring_candidate(candidate, writer, idempotency_key='fork-save') == result
    assert _timeline_state(service) == accepted
    from runtime_protocol.errors import RuntimeErrorBase

    changed = copy.deepcopy(candidate)
    changed['parent']['opaque_parent']['later'] = True
    for key in ('fork-save', 'stale-fork-save'):
        with pytest.raises(RuntimeErrorBase):
            publish_authoring_candidate(changed, writer, idempotency_key=key)
        assert _timeline_state(service) == accepted


@pytest.mark.parametrize('damage', ['source', 'baseline', 'foreign', 'ambiguous', 'closure', 'media'])
def test_invalid_alias_source_or_input_refuses_without_writes(shared, damage):
    service, writer, candidate, source, selected = shared
    local = candidate['placements'][selected]['shot_id']
    if damage == 'source':
        candidate['source_mapping']['shots'][local]['revision_id'] = 'forged'
    elif damage == 'baseline':
        candidate['shots'][local]['base_payload']['opaque_shot'] = {'mutated': True}
    elif damage == 'foreign':
        candidate['source_mapping']['_runtime_document']['project_id'] = 'foreign'
    elif damage == 'ambiguous':
        candidate['placements'].append(copy.deepcopy(candidate['placements'][0]))
    elif damage == 'closure':
        candidate['source_mapping']['_runtime_document']['internal_timeline_revisions'] = []
    else:
        shot = candidate['shots'][local]
        shot['payload']['items'][0]['media_id'] = NEW
        shot['internal_timeline']['registry']['assets']['new-picture']['media_id'] = NEW
    before = _timeline_state(service)
    from runtime_protocol.errors import RuntimeErrorBase

    with pytest.raises((ValueError, RuntimeErrorBase)):
        publish_authoring_candidate(candidate, writer, idempotency_key='damaged-' + damage)
    assert _timeline_state(service) == before


@pytest.mark.parametrize('damage', ['payload', 'owner', 'missing'])
def test_runtime_source_response_must_match_exact_closure(shared, monkeypatch, damage):
    service, writer, candidate, source, selected = shared
    read = writer.get_project_shot_revision

    def changed(*args):
        shot = copy.deepcopy(read(*args))
        if damage == 'payload':
            shot['payload']['opaque_shot']['changed'] = True
        elif damage == 'owner':
            shot['project_id'] = 'foreign'
        else:
            shot['internal_timeline_revision_id'] = 'missing'
        return shot

    monkeypatch.setattr(writer, 'get_project_shot_revision', changed)
    before = _timeline_state(service)
    with pytest.raises((ValueError, KeyError)):
        publish_authoring_candidate(candidate, writer, idempotency_key='mutated-read-' + damage)
    assert _timeline_state(service) == before
