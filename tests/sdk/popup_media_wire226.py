"""Closed process wire for the App popup tests; no server or Runtime access."""
import contextlib
import copy
import io
import json
import sys
from pathlib import Path
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import RuntimeCommitService, _digest
from astrid.core.timeline import scene_answers
from test_popup_media_226 import fixture
from test_timeline_commit import Port

class Client:
    def query_scene(self, *, revision_digest, scene, frames):
        return [dict(frame=f, coverage='COVERED', content='CONTENT', findings=[], layers=[]) for f in frames]
scene_answers._client=lambda:Client()
Checkout._native_outline_integrity=lambda *a:None

def envelopes(closure):
    for shot in closure['shot_revisions']:
        internal=next(r for r in closure['internal_timeline_revisions'] if r['revision_id']==shot['internal_timeline_revision_id'])
        shot['internal_timeline_id']=internal['timeline_id']
    return closure

def run():
    statefile=Path(sys.argv[1])
    if sys.argv[2]=='init':
        closure,selected,media=copy.deepcopy(fixture())
        state=dict(closures={'rev-0':envelopes(closure)}, selected=selected, media=media, current='rev-0', saved=None, requests=[])
    else:
        state=json.loads(statefile.read_text());body=json.load(sys.stdin)
        port=Port(body);port.current=state['current'];port.saved=state['saved']
        port.revision_closure=lambda rid:copy.deepcopy(state['closures'][rid])
        port.selected_media=lambda request:copy.deepcopy(state['selected'])
        result=RuntimeCommitService().commit(body,port)
        state['requests'].append(body);state['result']=result;state['saved']=port.saved
        if port.accepts and port.accepts[-1]['publication']:
            pub=port.accepts[-1]['publication'];base=state['closures'][body['base_revision_id']]
            shots={r['revision_id']:copy.deepcopy(r) for r in base['shot_revisions']};shots.update({r['revision_id']:{**r,'project_id':'p'} for r in pub['shot_revisions']})
            internals={r['revision_id']:copy.deepcopy(r) for r in base['internal_timeline_revisions']};internals.update({r['revision_id']:{**r,'project_id':'p'} for r in pub['internal_timeline_revisions']})
            sr=[shots[k] for k in dict.fromkeys(o['shot_revision_id'] for o in pub['parent_composition']['occurrences'])]
            ir=[internals[k] for k in dict.fromkeys(s['internal_timeline_revision_id'] for s in sr)]
            rid=result['accepted_revision_id'];state['current']=rid
            state['closures'][rid]=envelopes(dict(parent_revision=dict(project_id='p',timeline_id='t',revision_id=rid,payload=pub['parent_composition'],content_digest=_digest(pub['parent_composition'])),shot_revisions=sr,internal_timeline_revisions=ir))
    statefile.write_text(json.dumps(state,sort_keys=True))
    return state
with contextlib.redirect_stdout(io.StringIO()):
    state=run()
print(json.dumps(state))
