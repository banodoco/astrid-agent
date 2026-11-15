"""Synthetic-only owner state/lease/cleanup tests; no renderer/native backend."""
import copy
from dataclasses import replace
import threading

import pytest
from astrid.core.execution.remotion_frame_owner import (
    BackendHandle, CleanupUnresolved, DrainProof, FrameContext, FrameOwnerError,
    LeaseRevoked, LEASE_VERSION, PIN_KEYS, RemotionFrameOwner, UnsupportedContext,
    WaitEvidence,
)


def facts():
    return {
        'namespace': {'workspace_uuid':'workspace-synthetic', 'profile_binding_digest':'sha256:'+'a'*64,
                      'owner_epoch':'epoch1', 'custody_scope':'/fixture/runtime-custody',
                      'activation_host_pid':41, 'activation_host_birth_id':'synthetic-host-birth'},
        'roots': {'realm_root':'/fixture/realm', 'support_root':'/fixture/support',
                  'project_root':'/fixture/project', 'owner_scope_identity':'synthetic-exclusive-scope'},
        'artifact_pins': {key:{'path':'/fixture/'+key, 'sha256':'a'*64} for key in PIN_KEYS},
        'policy': {'browser_mode':'synthetic-new-headless', 'launch_cleanup_adapter':'synthetic-adapter-v1',
                   'backend_identity':'synthetic-darwin', 'descriptor_policy':'synthetic-confined',
                   'sandbox_tuple':['synthetic-only'], 'max_frames':12, 'max_resolution':2048},
        'qualification': {'test_evidence':'explicitly-synthetic-no-installed-acceptance'},
    }


class FakeBackend:
    identity='synthetic-darwin'
    def __init__(self, context):
        self.context=context; self.calls=[]; self.fail=None; self.reply_change=None
        self.reserve_entered=threading.Event(); self.reserve_continue=None
        self.exchange_entered=threading.Event(); self.exchange_continue=None
        self.proof_change=None; self.waits={}; self.handles=[]; self.count=0
    def validate_context(self, context):
        self.calls.append('validate_context')
        if context != self.context or self.fail=='context': raise UnsupportedContext('synthetic tuple mismatch')
    def reserve(self, pending):
        self.calls.append('reserve'); self.count+=1; self.reserve_entered.set()
        if self.reserve_continue is not None: assert self.reserve_continue.wait(3)
        if self.fail=='reserve-before': raise RuntimeError('ambiguous reserve')
        for kind in ('node','browser','helper'):
            handle=BackendHandle(self, object(), 'synthetic-'+kind+'-'+str(pending.generation))
            self.handles.append(handle); pending.declare(kind, handle)
        self.node=pending.obligations[0].handle
        if self.fail=='reserve-after': raise RuntimeError('ambiguous reserve after obligation')
    def start_suspended(self, pending):
        self.calls.append('start_suspended')
        if self.fail=='start': raise RuntimeError('ambiguous spawn')
        if self.fail=='handle': return BackendHandle(object(), object(), 'fake-linux-pidfd')
        return self.node
    def publish_and_resume(self, handle):
        self.calls.append('publish_and_resume')
        if self.fail=='publish': raise RuntimeError('ambiguous resume')
    def verify(self, handle):
        self.calls.append('verify')
        if self.fail=='verify' or handle not in self.handles: raise RuntimeError('incarnation unresolved')
    def exchange(self, handle, frame):
        self.calls.append('exchange'); self.exchange_entered.set()
        if self.exchange_continue is not None: assert self.exchange_continue.wait(3)
        if self.fail=='exchange': raise TimeoutError('ambiguous partial delivery')
        reply={k:frame[k] for k in ('version','generation','environment_digest','seq')}
        reply.update(status='ok', result={'synthetic_frame':'only'})
        return self.reply_change(reply) if self.reply_change else reply
    def signal(self, obligation, command):
        assert obligation.handle.backend is self
        self.calls.append(('signal',obligation.kind,command))
    def numeric_signal(self, pid):
        raise AssertionError('numeric authority is unreachable')
    def collect_exit_evidence(self, obligation):
        self.calls.append(('wait',obligation.kind))
        kind={'node':'host-reaped-node','browser':'authenticated-node-reaped-browser','helper':'authenticated-helper-wait'}[obligation.kind]
        evidence=WaitEvidence(obligation,kind,None if obligation.kind=='node' else self.node,0)
        self.waits[obligation]=evidence
        return evidence
    def prove_drain(self, pending):
        self.calls.append('prove_drain')
        proof=DrainProof(self,pending.token,pending.generation,pending.environment_digest,
                         tuple(self.waits[o] for o in pending.obligations),
                         dict(self.context.roots)['owner_scope_identity'],not pending.obligations)
        return self.proof_change(proof) if self.proof_change else proof


def setup_owner():
    context=FrameContext.from_retained(**facts()); backend=FakeBackend(context)
    return RemotionFrameOwner(context,backend=backend),backend


def lease(owner, pid=71):
    return owner.acquire(task_id='task'+str(pid),attempt_id='attempt',fence='fence',
                         child_pid=pid,child_birth_id='birth'+str(pid),attempt_root='/fixture/attempt'+str(pid))


def request(pid=71):
    return {'version':LEASE_VERSION,'request_id':'req','command':'render',
            'payload':{'composition_id':'fixture','props_path':'/fixture/attempt'+str(pid)+'/props.json',
                       'output_dir':'/fixture/attempt'+str(pid)+'/out','frames':2,'resolution':512}}


def test_default_missing_or_unqualified_context_performs_no_launch():
    owner=RemotionFrameOwner()
    with pytest.raises(UnsupportedContext): lease(owner)
    assert owner.pending is None and owner.state=='idle'
    context=FrameContext.from_retained(**facts()); owner=RemotionFrameOwner(context)
    with pytest.raises(UnsupportedContext): lease(owner)
    assert owner.pending is None
    owner,backend=setup_owner(); backend.fail='context'
    with pytest.raises(UnsupportedContext): lease(owner)
    assert backend.calls==['validate_context'] and owner.pending is None


@pytest.mark.parametrize('section,key',[('namespace','owner_epoch'),('namespace','activation_host_birth_id'),('artifact_pins','browser_closure'),('policy','descriptor_policy'),('roots','owner_scope_identity')])
def test_missing_context_fact_fails_before_backend(section,key):
    data=facts(); del data[section][key]
    with pytest.raises(UnsupportedContext): FrameContext.from_retained(**data)


def test_context_is_deeply_immutable_and_mode_changes_cache_identity():
    data=facts(); context=FrameContext.from_retained(**data)
    data['artifact_pins']['browser']['sha256']='changed'; data['policy']['sandbox_tuple'].append('changed')
    assert context==FrameContext.from_retained(**facts())
    data=facts();data['policy']['browser_mode']='other-mode'
    assert FrameContext.from_retained(**data).environment_digest!=context.environment_digest
    owner,backend=setup_owner()
    with pytest.raises(AttributeError): owner.context=FrameContext.from_retained(**data)
    with pytest.raises(FrameOwnerError,match='mismatch'):
        owner.acquire(task_id='t',attempt_id='a',fence='f',child_pid=1,child_birth_id='b',attempt_root='/a',context=FrameContext.from_retained(**data))
    assert not backend.calls


@pytest.mark.parametrize('section,key,value',[('namespace','owner_epoch','other'),('namespace','activation_host_pid',42),('namespace','profile_binding_digest','other'),('policy','backend_identity','synthetic-linux'),('policy','browser_mode','other')])
def test_host_epoch_profile_backend_mode_cannot_share_owner(section,key,value):
    owner,backend=setup_owner();lease(owner)
    data=facts();data[section][key]=value
    with pytest.raises(FrameOwnerError,match='mismatch'):
        owner.acquire(task_id='t',attempt_id='a',fence='f',child_pid=1,child_birth_id='b',attempt_root='/a',context=FrameContext.from_retained(**data))
    assert backend.count==1


def test_scope_disjoint_and_no_task_qualified_escape():
    data=facts();data['roots']['support_root']='/fixture/runtime-custody'
    with pytest.raises(UnsupportedContext): FrameContext.from_retained(**data)
    data=facts();data['qualification']['qualified']=True
    with pytest.raises(UnsupportedContext): FrameContext.from_retained(**data)


def test_same_host_tasks_reuse_one_admitted_generation():
    owner,backend=setup_owner();first=lease(owner);second=lease(owner,72)
    assert first.generation==second.generation==1 and backend.count==1
    assert owner.render(first,request())['status']=='ok'
    assert owner.hello(first)['environment_digest']==owner.environment_digest
    assert backend.calls.index('reserve')<backend.calls.index('publish_and_resume')
    assert not owner.drain(idle_only=True)


def test_concurrent_start_serializes_and_keeps_published_pending():
    owner,backend=setup_owner();backend.reserve_continue=threading.Event()
    result=[];errors=[]
    def acquire(pid):
        try: result.append(lease(owner,pid))
        except Exception as exc: errors.append(exc)
    t1=threading.Thread(target=acquire,args=(71,)); t2=threading.Thread(target=acquire,args=(72,))
    t1.start();assert backend.reserve_entered.wait(2)
    assert owner.pending is not None and owner.state=='starting'
    t2.start();backend.reserve_continue.set();t1.join(3);t2.join(3)
    assert not errors and len(result)==2 and backend.count==1


@pytest.mark.parametrize('edge',['reserve-before','reserve-after','start','publish','handle','verify'])
def test_every_start_failure_retains_pending_and_prevents_replacement(edge):
    owner,backend=setup_owner();backend.fail=edge
    with pytest.raises(Exception): lease(owner)
    assert owner.pending is not None and owner.state=='fenced'
    before=backend.count;backend.fail=None
    with pytest.raises(FrameOwnerError): lease(owner,72)
    assert backend.count==before
    if edge=='reserve-before':
        backend.proof_change=lambda p:replace(p,never_spawned=False)
        with pytest.raises(CleanupUnresolved): owner.drain()
        assert owner.pending is not None
        backend.proof_change=None
    assert owner.drain()  # only exact positive synthetic drain permits successor
    assert lease(owner,72).generation==2


@pytest.mark.parametrize('field,value',[('generation',2),('environment_digest','foreign'),('seq',0),('version','foreign'),pytest.param('result','x'*65536,id='result-oversized-64KiB'),('status','unknown')])
def test_wrong_or_oversize_reply_fences_without_replacement(field,value):
    owner,backend=setup_owner();active=lease(owner)
    backend.reply_change=lambda r:{**r,field:value}
    with pytest.raises(FrameOwnerError): owner.render(active,request())
    assert owner.state=='fenced' and owner.pending is not None
    with pytest.raises(FrameOwnerError):lease(owner,72)
    assert backend.count==1


@pytest.mark.parametrize('edge',['exchange','verify'])
def test_partial_delivery_and_identity_failure_retain_uncertainty(edge):
    owner,backend=setup_owner();active=lease(owner);backend.fail=edge
    with pytest.raises(Exception):owner.render(active,request())
    assert owner.state=='fenced' and owner.first_failure is not None


@pytest.mark.parametrize('change',[lambda r:{**r,'command':'close'},lambda r:{**r,'generation':1},lambda r:{**r,'environment':{}},lambda r:{**r,'payload':{**r['payload'],'browser':'/bin/browser'}},lambda r:{**r,'payload':{**r['payload'],'props_path':'/outside/props'}},lambda r:{**r,'payload':{**r['payload'],'frames':13}}])
def test_task_lifecycle_binders_environment_and_paths_rejected(change):
    owner,backend=setup_owner();active=lease(owner)
    with pytest.raises(FrameOwnerError): owner.render(active,change(request()))
    assert 'exchange' not in backend.calls


def test_child_exit_revokes_even_descriptor_holder_and_discards_dispatched_result():
    owner,backend=setup_owner();active=lease(owner);backend.exchange_continue=threading.Event()
    errors=[]
    def render():
        try:owner.render(active,request())
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=render);thread.start();assert backend.exchange_entered.wait(2)
    owner.revoke_child(child_pid=71,child_birth_id='birth71')
    assert not owner.drain(idle_only=True)
    backend.exchange_continue.set();thread.join(3)
    assert len(errors)==1 and isinstance(errors[0],LeaseRevoked) and owner.state=='ready'
    with pytest.raises(LeaseRevoked):owner.render(active,request())
    assert owner.drain(idle_only=True)
    successor=lease(owner,72);assert successor.generation==2
    with pytest.raises(LeaseRevoked):owner.render(active,request())


def test_idle_drain_admission_race_and_handoff_invalidate_lease():
    owner,backend=setup_owner();active=lease(owner)
    assert not owner.drain(idle_only=True)
    owner.revoke_child(child_pid=71,child_birth_id='birth71')
    assert owner.drain(idle_only=True)
    new=lease(owner,72);assert new.generation==2
    assert owner.invalidate() and owner.state=='gone'
    with pytest.raises(LeaseRevoked):owner.hello(new)
    with pytest.raises(FrameOwnerError):lease(owner,73)


@pytest.mark.parametrize('edge',['eof','pid_absent','group_absent','browser_close','wrong_node','wrong_ticket','stale_generation','missing_browser','unaccounted_helper'])
def test_transport_absence_close_or_wrong_wait_subject_never_proves_drain(edge):
    owner,backend=setup_owner();active=lease(owner)
    def change(proof):
        if edge in {'eof','pid_absent','group_absent','browser_close'}:return edge
        if edge=='wrong_ticket':return replace(proof,ticket_token=object())
        if edge=='stale_generation':return replace(proof,generation=0)
        if edge=='missing_browser':return replace(proof,waits=tuple(w for w in proof.waits if w.obligation.kind!='browser'))
        if edge=='unaccounted_helper':return replace(proof,waits=tuple(w for w in proof.waits if w.obligation.kind!='helper'))
        return replace(proof,waits=tuple(replace(w,reaper=BackendHandle(backend,object(),'wrong-node-incarnation')) if w.obligation.kind=='browser' else w for w in proof.waits))
    backend.proof_change=change
    with pytest.raises(CleanupUnresolved):owner.drain()
    assert owner.pending is not None and owner.state=='fenced'
    with pytest.raises(FrameOwnerError):lease(owner,72)
    assert backend.count==1


def test_cleanup_keeps_first_failure_and_browser_wait_precedes_node_signal():
    owner,backend=setup_owner();active=lease(owner);backend.fail='exchange'
    with pytest.raises(TimeoutError):owner.render(active,request())
    first=owner.first_failure;backend.fail=None;backend.proof_change=lambda p:'EOF'
    with pytest.raises(CleanupUnresolved):owner.drain()
    assert owner.first_failure is first and owner.cleanup_evidence
    backend.proof_change=None;assert owner.drain()
    assert backend.calls.index(('wait','browser'))<backend.calls.index(('signal','node','TERM'))
    assert owner.first_failure is first


def test_bad_pin_and_constructed_digest_are_unsupported():
    data=facts();data['artifact_pins']['node']['sha256']='version-is-not-a-hash'
    with pytest.raises(UnsupportedContext):FrameContext.from_retained(**data)
    context=FrameContext.from_retained(**facts())
    with pytest.raises(UnsupportedContext):replace(context,environment_digest='task-selected')
    with pytest.raises(UnsupportedContext):replace(context,roots=list(context.roots))


@pytest.mark.parametrize('field',['generation','seq'])
def test_boolean_reply_binder_cannot_equal_integer(field):
    owner,backend=setup_owner();active=lease(owner)
    backend.reply_change=lambda r:{**r,field:True}
    with pytest.raises(FrameOwnerError):owner.render(active,request())
    assert owner.state=='fenced'


def test_lost_browser_wait_cannot_be_recovered_by_later_node_exit_proof():
    owner,backend=setup_owner();lease(owner)
    original=backend.collect_exit_evidence
    def missing_browser(obligation):
        value=original(obligation)
        return None if obligation.kind=='browser' else value
    backend.collect_exit_evidence=missing_browser
    with pytest.raises(CleanupUnresolved):owner.drain()
    assert owner.pending is not None and owner.state=='fenced'


def test_backend_substitution_and_invalidated_startup_retain_pending():
    owner,backend=setup_owner();backend.identity='synthetic-linux'
    with pytest.raises(UnsupportedContext):lease(owner)
    assert not backend.calls and owner.pending is None
    owner,backend=setup_owner();backend.reserve_continue=threading.Event();errors=[]
    def acquire():
        try:lease(owner)
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=acquire);thread.start();assert backend.reserve_entered.wait(2)
    with pytest.raises(CleanupUnresolved):owner.invalidate()
    assert owner.pending is not None
    backend.reserve_continue.set();thread.join(3)
    assert errors and owner.state=='fenced'
    assert owner.drain() and owner.state=='gone'


def test_queued_revoked_task_drops_and_node_exchanges_are_serial():
    owner,backend=setup_owner();first=lease(owner);second=lease(owner,72)
    backend.exchange_continue=threading.Event();errors=[]
    def render(active, pid):
        try:owner.render(active,request(pid))
        except Exception as exc:errors.append(exc)
    t1=threading.Thread(target=render,args=(first,71));t1.start();assert backend.exchange_entered.wait(2)
    t2=threading.Thread(target=render,args=(second,72));t2.start()
    owner.revoke_child(child_pid=72,child_birth_id='birth72')
    backend.exchange_continue.set();t1.join(3);t2.join(3)
    assert len(errors)==1 and isinstance(errors[0],LeaseRevoked)
    assert backend.calls.count('exchange')==1 and owner.state=='ready'


def test_owner_only_close_before_authority_waits_does_not_count_as_drain():
    owner,backend=setup_owner();lease(owner);frames=[]
    exchange=backend.exchange
    def capture(handle,frame):frames.append(copy.deepcopy(frame));return exchange(handle,frame)
    backend.exchange=capture;assert owner.drain()
    assert [f['command'] for f in frames]==['close']
    assert frames[0]['generation']==1 and frames[0]['environment_digest']==owner.environment_digest
    assert backend.calls.index('exchange')<backend.calls.index(('signal','browser','TERM'))


def test_boolean_drain_generation_and_unknown_close_remain_fenced_until_exact_waits():
    owner,backend=setup_owner();lease(owner)
    backend.proof_change=lambda proof:replace(proof,generation=True)
    with pytest.raises(CleanupUnresolved):owner.drain()
    assert owner.pending is not None
    backend.proof_change=None;backend.fail='exchange'
    assert owner.drain()  # exact waits resolve cleanup despite ambiguous close
    assert owner.first_failure is not None


def test_successful_frame_requires_original_browser_obligation():
    owner, backend = setup_owner()
    def node_only(pending):
        backend.calls.append('reserve'); backend.count += 1
        handle = BackendHandle(backend, object(), 'synthetic-node-' + str(pending.generation))
        backend.handles.append(handle); pending.declare('node', handle)
        backend.node = handle
    backend.reserve = node_only
    admitted = lease(owner)  # Lazy Node startup itself allocates no browser.
    with pytest.raises(FrameOwnerError, match='original browser obligation'):
        owner.render(admitted, request())
    assert 'publish_and_resume' in backend.calls
    assert owner.state == 'fenced' and owner.pending is not None


@pytest.mark.parametrize('mutation', ['remove', 'add', 'reorder'])
def test_cleanup_callback_cannot_drop_or_rewrite_original_obligations(mutation):
    owner, backend = setup_owner(); lease(owner)
    pending = owner.pending
    originals = tuple(pending.obligations)
    first_signal = backend.signal
    changed = False
    extra = None
    def mutate(obligation, command):
        nonlocal changed, extra
        first_signal(obligation, command)
        if not changed:
            changed = True
            if mutation == 'remove': pending.obligations[:] = [pending.obligations[0]]
            elif mutation == 'reorder': pending.obligations.reverse()
            else:
                handle = BackendHandle(backend, object(), 'synthetic-unaccounted-helper')
                extra = pending.declare('helper', handle)
    backend.signal = mutate
    with pytest.raises(CleanupUnresolved): owner.drain()
    assert owner.pending is pending and owner.state == 'fenced'
    assert owner._coverage_error is not None
    assert all(any(row is original for row in pending.obligations) for original in originals)
    if extra is not None: assert any(row is extra for row in pending.obligations)
    # Cleanup visits all ORIGINAL obligations even if the callback prunes the
    # mutable producer list. Positive waits cannot repair lost creation history.
    signalled = [row[1] for row in backend.calls if isinstance(row, tuple) and row[0] == 'signal']
    assert signalled == ['browser', 'helper', 'node']
    with pytest.raises(FrameOwnerError, match='unavailable'): lease(owner, 72)


def test_cleanup_retains_declarations_pruned_before_drain():
    owner, backend = setup_owner(); lease(owner)
    pending = owner.pending
    originals = tuple(pending.obligations)
    pending.obligations[:] = [originals[0]]
    with pytest.raises(CleanupUnresolved): owner.drain()
    assert owner.pending is pending and owner.state == 'fenced'
    assert owner._coverage_error is not None
    assert pending.obligations == list(originals)
    signalled = [row[1] for row in backend.calls if isinstance(row, tuple) and row[0] == 'signal']
    assert signalled == ['browser', 'helper', 'node']
    with pytest.raises(FrameOwnerError, match='unavailable'): lease(owner, 72)


def test_successful_frame_rejects_pruned_declarations_during_exchange():
    owner, backend = setup_owner(); admitted = lease(owner)
    originals = tuple(owner.pending.obligations)
    exchange = backend.exchange
    def prune(handle, frame):
        reply = exchange(handle, frame)
        owner.pending.obligations[:] = [originals[0]]
        return reply
    backend.exchange = prune
    with pytest.raises(FrameOwnerError, match='original browser obligation'):
        owner.render(admitted, request())
    assert owner.state == 'fenced' and owner.pending._declared == originals
