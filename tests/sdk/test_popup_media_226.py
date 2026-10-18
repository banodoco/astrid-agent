"""Prepared public occurrence media edits; closed wire/evaluator fixtures only."""
import copy
import json
from pathlib import Path
import pytest
from functools import lru_cache
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService, _digest
from test_timeline_commit import Port, request, call, exact_closure
from test_timeline_saved_publish import synthetic_scene_answers

pytestmark = pytest.mark.usefixtures('synthetic_scene_answers')

@lru_cache(maxsize=1)
def fixture():
    raw = json.loads((Path(__file__).parents[1] / 'fixtures/timeline_editing/tiny_publishable.json').read_text())
    # Canonical film definitions, not mutable definitions copied into chapters.
    tracks = {}
    for shot in raw['shots'].values():
        for track in shot['internal_timeline'].get('tracks', []):
            tracks.setdefault(track['id'], copy.deepcopy(track))
        shot['internal_timeline']['tracks'] = []
    raw['parent']['config']['tracks'] = list(tracks.values())
    closure = exact_closure(Checkout(raw).document())
    parent = closure['parent_revision']['payload']
    shared = copy.deepcopy(parent['occurrences'][0])
    shared.update(occurrence_id='occ-copy', placement={'start_ms': 8000})
    parent['occurrences'].append(shared)
    parent['opaque-226'] = {'preserve': [None, {'n': 1.0}]}
    closure['parent_revision']['content_digest'] = _digest(parent)
    selected = dict(realm_id='fixture', project_id='p', asset='asset-226', generation_id='generation-226', variant_id='variant-226', object_id='sha256:'+'a'*64,
        generation={'id':'generation-226'}, variant={'id':'variant-226'}, entry={'object_id':'sha256:'+'a'*64, 'media_type':'image/png'})
    selected['catalog_digest'] = _digest(selected)
    media = {key: selected[key] for key in ('asset','generation_id','variant_id','object_id','catalog_digest')}
    return closure, selected, media

def prepare(op='timeline.add'):
    closure, selected, media = copy.deepcopy(fixture())
    if op == 'timeline.add':
        command = call(op, 'occurrence:occ-copy/track:plate', media=media, at=8, duration=1, clip_id='imported-226')
        command['created'] = ['occurrence:occ-copy/clip:imported-226']
        command['expect'] = {'occurrence:occ-copy/clip:imported-226::':dict(state='absent')}
    else:
        command = call(op, 'occurrence:occ-copy/clip:a-rocket', media=media)
    body = request(command)
    shared = closure['parent_revision']['payload']['occurrences'][-1]
    shot = next(row for row in closure['shot_revisions'] if row['shot_id'] == shared['shot_id'] and row['revision_id'] == shared['shot_revision_id'])
    body['scope'].update(target='occurrence:occ-copy', content='this-occurrence', shot_id=shared['shot_id'], shot_revision_id=shared['shot_revision_id'], internal_revision_id=shot['internal_timeline_revision_id'])
    port = Port(body)
    closures = {'rev-0': copy.deepcopy(closure)}
    port.revision_closure = lambda rid: copy.deepcopy(closures[rid])
    reads = []
    def selection(value):
        reads.append(copy.deepcopy(value))
        return copy.deepcopy(selected)
    port.selected_media = selection
    accept = port.accept
    def accept_with_readback(prepared):
        response = accept(prepared)
        pub = prepared['publication']
        if pub is not None:
            rid = response['stored_result']['accepted_revision_id']
            all_shots = {row['revision_id']: copy.deepcopy(row) for row in closure['shot_revisions']}
            all_shots.update({row['revision_id']:{**copy.deepcopy(row), 'project_id':'p'} for row in pub['shot_revisions']})
            all_internals = {row['revision_id']:copy.deepcopy(row) for row in closure['internal_timeline_revisions']}
            all_internals.update({row['revision_id']:{**copy.deepcopy(row),'project_id':'p'} for row in pub['internal_timeline_revisions']})
            shots = [all_shots[key] for key in dict.fromkeys(row['shot_revision_id'] for row in pub['parent_composition']['occurrences'])]
            internals = [all_internals[key] for key in dict.fromkeys(row['internal_timeline_revision_id'] for row in shots)]
            closures[rid] = dict(parent_revision=dict(revision_id=rid, project_id='p', timeline_id='t',
                payload=copy.deepcopy(pub['parent_composition']), content_digest=_digest(pub['parent_composition'])),
                shot_revisions=shots, internal_timeline_revisions=internals)
        return response
    port.accept = accept_with_readback
    return closure, selected, body, port, reads, closures

@pytest.mark.parametrize('op', ['timeline.add', 'clip.swap-asset'])
def test_public_narrated_shared_media(op):
    closure, selected, body, port, reads, closures = prepare(op)
    before = copy.deepcopy(closure)
    loaded = RuntimeCommitService._load_revision(port, body, 'rev-0')
    owners = {row['occurrence_id']:row['shot_id'] for row in loaded['placements']}
    assert owners['occ-A'] != owners['occ-copy']
    assert loaded['shots'][owners['occ-copy']]['internal_timeline']['tracks'] == []
    result = RuntimeCommitService().commit(body, port)
    assert result['status'] == 'accepted', result.get('error')
    assert len(port.accepts) == 1 and len(reads) == 1
    assert closure == before
    publication = port.accepts[0]['publication']
    parent = publication['parent_composition']
    original = closure['parent_revision']['payload']
    assert parent['opaque-226'] == original['opaque-226']
    assert parent['occurrences'][:-1] == original['occurrences'][:-1]
    assert parent['occurrences'][-1]['shot_revision_id'] != original['occurrences'][-1]['shot_revision_id']

    accepted = RuntimeCommitService._load_revision(port, {**body, 'base_revision_id':result['accepted_revision_id']}, result['accepted_revision_id'])
    owner = next(row['shot_id'] for row in accepted['placements'] if row['occurrence_id']=='occ-copy')
    before_owner = owners['occ-copy']
    old = loaded['shots'][before_owner]['internal_timeline']
    new = accepted['shots'][owner]['internal_timeline']
    assert new['tracks'] == []
    old_clips = {row['id']:row for row in old['clips']}
    new_clips = {row['id']:row for row in new['clips']}
    if op == 'timeline.add':
        assert all(new_clips[key] == value for key,value in old_clips.items())
        assert new_clips['imported-226']['at'] == 0
    else:
        assert set(new_clips) == set(old_clips)
        for key,value in old_clips.items():
            if key != 'a-rocket': assert new_clips[key] == value
        assert {key:value for key,value in new_clips['a-rocket'].items() if key not in {'asset','app'}} == {key:value for key,value in old_clips['a-rocket'].items() if key not in {'asset','app'}}
        assert new_clips['a-rocket']['app']['astrid']['cut'] == old_clips['a-rocket']['app']['astrid']['cut']
    assert new['registry']['assets']['asset-226']['media_id'] == selected['object_id']
    # Inverse is an ordinary guarded command over the prepared authoring state.
    from astrid.sdk.timeline.inverse import inverse_command
    inverse = inverse_command(result['change']['commands'], loaded, result['canonical_document'],
        result['accepted_publication_id'], consequences=result['change']['consequences'])
    restored, _, _ = RuntimeCommitService._apply(result['canonical_document'], [inverse])
    # IDENTITY reservations are intentionally monotonic across undo.
    from astrid.sdk.timeline.identity import retain_cut_reservations
    expected = retain_cut_reservations(copy.deepcopy(loaded), restored)
    assert restored == expected

    # Same request identity/bytes returns the same receipt without re-selecting.
    replay = RuntimeCommitService().commit(body,port)
    assert replay == result and len(port.accepts)==1 and len(reads)==1
    changed = copy.deepcopy(body); changed['change']['commands'][0]['args']['media']['catalog_digest']='sha256:'+'c'*64
    refused = RuntimeCommitService().commit(changed,port)
    assert refused['status']=='error' and refused['error']['code']=='request_id_conflict' and len(port.accepts)==1

@pytest.mark.parametrize('op,damage', [('timeline.add', key) for key in ['project','pin','occurrence','selection','created','duplicate','registry']] + [('clip.swap-asset', key) for key in ['project','pin','occurrence','selection','registry','source']])
def test_public_media_refusals_are_atomic(op, damage):
    closure, selected, body, port, reads, closures = prepare(op)
    if damage == 'project': body['project_id']='wrong-project'
    elif damage == 'pin': body['scope']['internal_revision_id']='wrong-pin'
    elif damage == 'occurrence': body['scope']['target']='occurrence:occ-A'
    elif damage == 'selection': body['change']['commands'][0]['args']['media']['catalog_digest']='sha256:'+'c'*64
    elif damage == 'created': body['change']['commands'][0]['created']=['occurrence:occ-A/clip:imported-226']
    elif damage == 'duplicate':
        command=body['change']['commands'][0]; command['args']['clip_id']='a-rocket'; command['created']=['occurrence:occ-copy/clip:a-rocket']
    elif damage == 'source':
        command=body['change']['commands'][0]
        command['expect']={command['select']+'::':{'state':'present','value':{'id':'a-rocket','opaque':'tampered'}}}
    else:
        raw=closures['rev-0']['internal_timeline_revisions'][0]
        raw['payload'].setdefault('registry',{}).setdefault('assets',{})['asset-226']={'media_id':'sha256:'+'f'*64,'opaque':None}
        raw['content_digest']=_digest(raw['payload'])
        body['change']['commands'][0].setdefault('expect',{})['occurrence:occ-copy/shot::internal_timeline.registry.assets.asset-226']={'state':'absent'}
    before=copy.deepcopy(closures)
    result=RuntimeCommitService().commit(body,port)
    assert result['status']=='refused' and not port.accepts, result
    assert closures==before


def test_unprepared_shared_owner_guard_is_retained():
    closure, selected, body, port, reads, closures = prepare()
    loaded=RuntimeCommitService._load_revision(port,body,'rev-0')
    for source in loaded['source_mapping']['shots'].values(): source['linked']=False
    with pytest.raises(Exception,match='Ambiguous occurrence timing identities'):
        RuntimeCommitService._scope_occurrence(body,loaded)


def test_public_media_rebases_over_unrelated_parent_content():
    closure, selected, body, port, reads, closures = prepare()
    head=copy.deepcopy(closure)
    head['parent_revision']['revision_id']='head-1'
    head['parent_revision']['payload']['unrelated-new-field']={'keep':None}
    head['parent_revision']['content_digest']=_digest(head['parent_revision']['payload'])
    closures['head-1']=head; port.current='head-1'
    result=RuntimeCommitService().commit(body,port)
    assert result['status']=='accepted' and len(port.accepts)==1, result.get('error')
    assert len(reads)==1
    assert port.accepts[0]['publication']['parent_composition']['unrelated-new-field']=={'keep':None}


def test_prepared_timing_resolves_only_its_occurrence_without_rewriting_anchors():
    from astrid.core.timeline import moments
    from astrid.sdk.timeline.model import _MomentContext
    closure, selected, body, port, reads, closures = prepare()
    loaded = RuntimeCommitService._load_revision(port, body, 'rev-0')
    tl = Checkout(loaded)
    owners = {row['occurrence_id']:row['shot_id'] for row in loaded['placements']}
    clips = [next(c for c in tl.clips() if c.shot_id==owners[oid] and c.id=='a-rocket') for oid in ['occ-A','occ-copy']]
    assert clips[1].start-clips[0].start == 8
    assert tl._cut_key(clips[0]) != tl._cut_key(clips[1])
    with pytest.raises(Exception, match='requires its occurrence/clip scope'):
        tl._cut_id('c1')
    for clip in clips:
        context=_MomentContext(tl,clip)
        words=context.words_for(moments.Moment(kind='word', text='missing word', line='s1'))
        assert words and {word.shot_id for word in words}=={clip.shot_id}
    assert loaded == RuntimeCommitService._load_revision(port, body, 'rev-0')
