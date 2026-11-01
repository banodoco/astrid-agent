"""Narrow same-placement pack element edit on the existing parent-CAS publisher."""
from __future__ import annotations
import copy
import json
import math
import uuid
from types import SimpleNamespace
from collections.abc import Mapping
from typing import Any
from astrid.core.element.project_packs import resolve_admitted_element
from astrid.core.timeline.authoring_bundle import validate_authoring_candidate, publish_authoring_candidate
from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.documents import Documents


def update_parent_element(owner: Any, project: str, timeline: str, *, realm: str, clip_id: str,
                          element: dict[str,str], expected_head: str, duration: float | None=None,
                          params: dict[str,Any] | None=None, idempotency_key: str | None=None):
    key=idempotency_key or f"element-{uuid.uuid4().hex}"
    if not all(isinstance(v,str) and v for v in (project,timeline,realm,clip_id,expected_head)):
        return DomainResult.failure(ErrorObject('validation_error','Explicit project/timeline/realm/clip/expected head are required',{}),idempotency_key=key)
    if duration is not None and (isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or duration<=0):
        return DomainResult.failure(ErrorObject('validation_error','Duration must be finite and positive',{}),idempotency_key=key)
    if params is not None and (not isinstance(params,dict) or len(json.dumps(params))>16384):
        return DomainResult.failure(ErrorObject('validation_error','Params must be a bounded JSON object',{}),idempotency_key=key)
    opened=owner.open_bundle(project,timeline)
    if not opened.ok or not isinstance(opened.data,Mapping):return opened
    data=opened.data
    if data.get('head_revision_id')!=expected_head or data.get('revision_id')!=expected_head:
        return DomainResult.failure(ErrorObject('conflict','Timeline head changed; inspect before making a fresh edit',{'expected_head':expected_head,'actual_head':data.get('head_revision_id')}),idempotency_key=key)
    candidate=copy.deepcopy(data['bundle']);project_id=str(data['project_id']);timeline_id=str(data['timeline_id'])
    try:
        resolve_admitted_element(realm,project_id,element,client=SimpleNamespace(documents=Documents(owner._client)))
        parent=candidate['parent'];clips=parent.get('clips')
        if not isinstance(clips,list):raise ValueError('Canonical parent clips are unavailable')
        matches=[c for c in clips if isinstance(c,dict) and c.get('id')==clip_id]
        if len(matches)!=1:raise ValueError('Expected one existing parent effect clip')
        clip=matches[0];before=copy.deepcopy(clip)
        old_ref=clip.get('elementRef') or clip.get('element_ref')
        if not isinstance(old_ref,dict) or old_ref.get('kind')!='effect' or clip.get('clipType')!=old_ref.get('id') or not isinstance(clip.get('hold'),(int,float)) or any(k in clip for k in ('asset','file','src','source','from','to')):
            raise ValueError('Update requires an existing generated hold effect without media source trimming')
        if element.get('kind')!='effect' or element.get('id')!=old_ref.get('id'):
            raise ValueError('Update must preserve the existing effect identity')
        config=parent.get('config')
        if not isinstance(config,dict):raise ValueError('Canonical parent config is unavailable')
        # Config.clips is a legacy mirror, not a separately patched projection.
        if 'clips' in config and config['clips']!=clips:raise ValueError('Parent clip mirror is inconsistent; refusing to overwrite')
        clip['elementRef']=dict(element);clip.pop('element_ref',None)
        if duration is not None:clip['hold']=duration
        if params is not None:clip['params']={**(clip.get('params') or {}),**params}
        if 'clips' in config:config['clips']=copy.deepcopy(clips)
        legacy=(config.get('app') or {}).get('elements')
        if isinstance(legacy,dict) and old_ref.get('id') in legacy:
            remaining=[c for c in clips if c is not clip and (c.get('elementRef') or c.get('element_ref'))==old_ref]
            if not remaining:legacy.pop(old_ref['id'])
        validate_authoring_candidate(candidate)
        class Writer:
            result=None
            def publish_parent_composition(self,_project,_timeline,publication,*,idempotency_key):
                self.result=owner._typed('publish_parent_composition',_project,_timeline,publication,key=idempotency_key,idempotency_key=idempotency_key)
                if not self.result.ok:raise RuntimeError(self.result.error.message if self.result.error else 'Publication failed')
                return self.result.data
        writer=Writer()
        try:receipt=publish_authoring_candidate(candidate,writer,idempotency_key=key)
        except RuntimeError:
            if writer.result is not None:return writer.result
            raise
        publication=receipt.get('publication',{})
        new_head=publication.get('new_head') or publication.get('revision_id') or publication.get('parent_revision_id')
        if not isinstance(new_head,str) or not new_head:raise ValueError('Publication receipt omitted committed head')
        return DomainResult.success({'project_id':project_id,'timeline_id':timeline_id,'expected_head':expected_head,'new_head':new_head,'clip_id':clip_id,'element':element,'duration':clip.get('hold'),'params':clip.get('params',{}),'before':{'element':old_ref,'duration':before.get('hold'),'params':before.get('params',{})}},receipt=writer.result.receipt if writer.result else None,idempotency_key=key)
    except (ValueError,KeyError,TypeError) as exc:
        return DomainResult.failure(ErrorObject('validation_error',str(exc),{}),idempotency_key=key)
