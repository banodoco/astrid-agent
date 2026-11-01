import copy
import hashlib
import json
from unittest.mock import Mock
import pytest
from astrid.core.timeline.authoring_bundle import open_authoring_bundle
from astrid.sdk.contracts import DomainResult,ErrorObject
from astrid.sdk.timeline_elements import update_parent_element


def setup_owner(monkeypatch):
    monkeypatch.setattr('astrid.sdk.timeline_elements.resolve_admitted_element',lambda realm,project,ref,**kwargs:{})
    effect={'id':'effect','at':0,'track':'V1','hold':2,'clipType':'reveal','elementRef':{'id':'reveal','kind':'effect','revision':'draft-reveal'},'params':{'color':'#ff6b6b','seed':15}}
    media={'id':'media','at':0,'track':'V1','hold':10,'opaque':{'preserve':True}}
    clips=[effect,media]
    payload={'clips':copy.deepcopy(clips),'config':{'clips':copy.deepcopy(clips),'app':{'elements':{'reveal':{'source':'legacy recovery bytes'}},'other':'keep'},'bundle':{'transcriptReferences':{'associations':[{'id':'saved'}]}}},'registry':{'assets':{}},'occurrences':[]}
    digest='sha256:'+hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    bundle=open_authoring_bundle({'project_id':'project','timeline_id':'timeline','revision_id':'head','payload':payload,'content_digest':digest},shot_revisions=[],internal_timeline_revisions=[])
    owner=Mock();owner.open_bundle.return_value=DomainResult.success({'bundle':bundle,'project_id':'project','timeline_id':'timeline','head_revision_id':'head','revision_id':'head'})
    owner._typed.side_effect=lambda operation,project,timeline,publication,**kwargs:DomainResult.success({'new_head':publication['parent_revision_id']})
    ref={'id':'reveal','kind':'effect','packId':'project_owner','revision':'sha256:'+'a'*64}
    return owner,bundle,ref


def test_sameclip_exacthead_publication_preserves_closure_and_user_params(monkeypatch):
    owner,bundle,ref=setup_owner(monkeypatch)
    result=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='head',duration=6,params={'chaos':.8})
    assert result.ok, result.error
    publication=owner._typed.call_args.args[3];parent=publication['parent_composition']
    assert publication['expected_head']=='head'
    assert parent['clips'][0]['id']=='effect' and parent['clips'][0]['hold']==6
    assert parent['clips'][0]['params']=={'color':'#ff6b6b','seed':15,'chaos':.8}
    assert parent['clips'][1]==bundle['parent']['clips'][1]
    assert parent['config']['clips']==parent['clips']
    assert parent['config']['app']=={'elements':{},'other':'keep'}
    assert parent['config']['bundle']==bundle['parent']['config']['bundle']
    assert parent['registry']==bundle['parent']['registry']
    assert publication['shot_revisions']==[] and publication['internal_timeline_revisions']==[]
    assert bundle['parent']['clips'][0]['hold']==2
    assert result.data['new_head'] and result.data['clip_id']=='effect'


def test_stale_head_and_concurrent_cas_reject_without_rebase(monkeypatch):
    owner,_,ref=setup_owner(monkeypatch)
    stale=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='stale')
    assert not stale.ok;owner._typed.assert_not_called()
    owner._typed.return_value=DomainResult.failure(ErrorObject('conflict','CAS rejected',{}));owner._typed.side_effect=None
    rejected=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='head',duration=6)
    assert not rejected.ok and rejected.error.code=='conflict';assert owner._typed.call_count==1


@pytest.mark.parametrize('duration',[0,-1,float('inf'),float('nan')])
def test_bad_duration_never_publishes(monkeypatch,duration):
    owner,_,ref=setup_owner(monkeypatch)
    result=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='head',duration=duration)
    assert not result.ok;owner._typed.assert_not_called()


def test_unadmitted_owner_and_sourceclip_reject(monkeypatch):
    owner,_,ref=setup_owner(monkeypatch)
    monkeypatch.setattr('astrid.sdk.timeline_elements.resolve_admitted_element',Mock(side_effect=ValueError('not admitted')))
    result=update_parent_element(owner,'project','timeline',realm='realm',clip_id='effect',element=ref,expected_head='head')
    assert not result.ok;owner._typed.assert_not_called()
