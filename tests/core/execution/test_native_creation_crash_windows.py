"""Preserved real-crash evidence-loss cases, not recovery certification.

Use the existing reservation/registration/suspended-release primitives verbatim.
A passing test means the lost launch/wait producer cannot settle a generation.
Normal retained-owner settlement is exercised separately; it cannot reconstruct
these missing results after supervisor death.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from astrid.core.execution import custody_broker as cb, generic_host as host
from tests.core.execution.test_confinement_launch_integration import launch  # noqa: F401


DRIVER = r'''
import json,os,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from astrid.core.execution import custody_broker as cb
settings=json.loads(Path(sys.argv[2]).read_text())
root=Path(settings['root']);root.mkdir(mode=0o700)
plan=settings['plan']; stage=settings['stage']; child=None
# IPC spelling only. All registration/authentication/SETEXEC remain real.
def compact():
    return Path(tempfile.mkdtemp(prefix='cs-',dir=Path.cwd())).relative_to(Path.cwd())
cb._create_compact_socket_root=compact

def pause(name,broker=None):
    if name!=stage:return
    record={'stage':name,'supervisor_pid':os.getpid(),
            'child_pid':child.pid if child is not None else None}
    if broker is not None:
        record.update(socket=str(broker.socket_path.absolute()),
                      registration=broker.registration,release_sent=broker._release_sent)
    cb._atomic_owner_json(Path(settings['checkpoint']),record)
    while True:time.sleep(.02)

original_persist=cb.RoleBoundCustodyBroker._persist
original_commit=cb.RoleCustodyAuthority._commit
def persist(self,event):
    original_persist(self,event)
    pause(event,self)
def commit(self,record,**kwargs):
    original_commit(self,record,**kwargs)
    if any(item.get('ack',{}).get('status')=='resume-intent-durable'
           for item in record['transitions']):pause('release-intent',broker)
cb.RoleBoundCustodyBroker._persist=persist
cb.RoleCustodyAuthority._commit=commit
broker=cb.RoleBoundCustodyBroker(role='command',identity_provider=cb.default_process_identity,
    ledger_root=root/'ledger',authority_scope_root=root,authority_journal=root/'authority.jsonl',timeout=2)
pause('reservation',broker)
env={'PATH':'/usr/bin:/bin','HOME':plan['cwd'],'TMPDIR':plan['cwd'],
     'PYTHONDONTWRITEBYTECODE':'1','PYTHONNOUSERSITE':'1','LC_ALL':'C',
     **broker.child_environment(plan['argv'],start_new_session=True)}
env['ASTRID_RUNTIME_CUSTODY_SOCKET']=os.path.relpath(broker.socket_path.absolute(),plan['cwd'])
child=subprocess.Popen(cb.custody_wrapper_argv(sys.executable),cwd=plan['cwd'],env=env,
    close_fds=True,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
broker.close_child_pipes()
broker.wait_until_sealed()
pause('registered-suspended',broker)
broker.release_suspended_launch(expected_pid=child.pid,process=child)
Path(settings['stop']).touch()
out,err=child.communicate(input=b'exit\n',timeout=5)
if child.returncode!=0:raise RuntimeError('synthetic workload failed')
pause('settlement-after-wait',broker)
raise RuntimeError('expected crash checkpoint was not reached')
'''


def wait_for(function,seconds=7):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=function()
        if value:return value
        time.sleep(.01)
    pytest.fail('fake crash checkpoint deadline expired')


def read(path):
    try:return json.loads(path.read_text())
    except (FileNotFoundError,json.JSONDecodeError):return None


def cleanup_fake_identity(pid, expected_anchor):
    """Test-owned orphan cleanup only; never an engine settlement certificate."""
    if pid is None:return 'never-launched'
    try:
        before=cb._darwin_process_start(pid)
    except cb.DarwinProcessExitedError:return 'absent-or-exited-nonexecuting'
    if expected_anchor is None or cb._start_anchor(before)!=expected_anchor:
        return 'original-identity-not-live-no-signal'
    # Capture only this fixture's exact live process; no numeric signal fallback.
    token=cb.current_process_audit_token(pid)
    after=cb._darwin_process_start(pid)
    assert expected_anchor==cb._start_anchor(after)
    cb.signal_audit_token(token['words'],signal.SIGKILL)
    def stopped():
        try:return cb._start_anchor(cb._darwin_process_start(pid))!=expected_anchor
        except cb.DarwinProcessExitedError:return True
    wait_for(stopped)
    return 'fixture-exact-token-kill-nonexecuting'


@pytest.mark.parametrize('stage',[
    'reservation','registration_pre_exec','registered-suspended',
    'release-intent','launch_release_sent','settlement-after-wait',
])
def test_supervisor_crash_retains_unrecoverable_generation(launch,tmp_path,stage):
    policy,*_=launch
    marker=policy.project.project_root/'workload-released'
    stop=policy.project.project_root/'stop'
    code=(f'from pathlib import Path;import time;Path({str(marker)!r}).touch();print("ready",flush=True)\n'
          f'while not Path({str(stop)!r}).exists():time.sleep(.01)\n')
    plan=host._wan_creation_control_launch_plan([str(Path(sys.executable).resolve()),'-I','-S','-B','-c',code],policy=policy)
    checkpoint=tmp_path/'checkpoint.json'; root=tmp_path/'protected-owner'
    settings={'root':str(root),'checkpoint':str(checkpoint),'stage':stage,'stop':str(stop),
              'plan':{'argv':plan.argv,'cwd':str(plan.cwd)}}
    settings_path=tmp_path/'settings.json';settings_path.write_text(json.dumps(settings))
    source=str(Path(host.__file__).resolve().parents[3])
    env={'PATH':'/usr/bin:/bin','HOME':str(tmp_path),'TMPDIR':str(tmp_path),'PYTHONDONTWRITEBYTECODE':'1'}
    driver=subprocess.Popen([sys.executable,'-I','-S','-B','-c',DRIVER,source,str(settings_path)],
        cwd=Path.cwd(),env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    snapshot=None;pid=None;anchor=None;cleanup='not-needed'
    try:
        def ready():
            if driver.poll() is not None:
                pytest.fail('fake supervisor exited before checkpoint')
            return read(checkpoint)
        snapshot=wait_for(ready)
        record=json.loads((root/'designation-command.json').read_text())
        (tmp_path/'original-designation.json').write_text(json.dumps(record,sort_keys=True))
        pid=record['target'].get('pid') or snapshot['child_pid']
        if pid is not None:
            try:anchor=cb._start_anchor(cb._darwin_process_start(pid))
            except cb.DarwinProcessExitedError:pass
        actor=cb.AuthenticatedCleanupActor(lambda:cb.current_process_audit_token(driver.pid),cb.default_process_identity)
        retained=cb.RetainedChildActor(driver,actor)
        if stage in {'launch_release_sent','settlement-after-wait'}:wait_for(marker.exists)
        else:assert not marker.exists()
        # Actual abrupt death closes locks/pipes and destroys live custody state.
        driver.kill();driver.wait(timeout=5)
        assert driver.returncode==-signal.SIGKILL
        successor=cb.AuthenticatedCleanupActor.current()
        authority=cb.RoleCustodyAuthority(root,'command',timeout=.3)
        errors={}
        with pytest.raises(cb.CustodyError) as refusal:
            authority.reserve_before_spawn(actor=successor,owner_epoch='successor',admission_id='successor')
        errors['fresh_generation']=str(refusal.value)
        with pytest.raises(cb.CustodyError) as refusal:
            authority.transfer(actor=successor,next_actor=successor,generation=1,
                transition_id='nonchild-recovery',owner_epoch='successor')
        errors['nonchild_recovery']=str(refusal.value)
        # Even a surviving original parent has only bounded existing recovery.
        try:
            authority.transfer(actor=successor,next_actor=successor,generation=1,
                transition_id='retained-parent-recovery',owner_epoch='successor',dead_actor=retained)
            errors['retained_parent_transfer']='succeeded; no retained live start anchor or exit waiter restored'
        except cb.CustodyError as exc:errors['retained_parent_transfer']=str(exc)
        assert authority._live_start_anchor is None and authority._retained_record is None
        if record['state']=='active' and stage!='settlement-after-wait':
            reference=authority.reference()
            try:
                authority.signal(actor=successor,generation=reference['generation'],
                    expected_target=reference['target'],signum=signal.SIGKILL,
                    identity_provider=cb.default_process_identity)
                errors['recovered_signal']='exact stored token accepted'
            except cb.CustodyError as exc:
                errors['recovered_signal']={'type':type(exc).__name__,'message':str(exc)}
            if stage=='launch_release_sent':
                assert isinstance(errors['recovered_signal'],dict)
                assert cb.default_process_identity(pid) is not None
        with pytest.raises(cb.CustodyError) as refusal:
            authority.reserve_before_spawn(actor=successor,owner_epoch='fresh',admission_id='fresh')
        errors['fresh_after_recovery_attempt']=str(refusal.value)
        cleanup=cleanup_fake_identity(pid,anchor)
        # Test-only cleanup cannot produce the lost launch owner's wait result.
        with pytest.raises(cb.CustodyError):
            authority.reserve_before_spawn(actor=successor,owner_epoch='after-fixture-cleanup',
                                           admission_id='after-fixture-cleanup')
        final=authority._read()
        assert final['state'] in {'pending','active'}
        assert not any(item.get('ack',{}).get('status') in {'settled','retired'}
                       for item in final['transitions'])
        (tmp_path/'crash-result.json').write_text(json.dumps({
            'stage':stage,'designation_state':final['state'],
            'fresh_generation_refused':True,'fixture_cleanup_does_not_settle':True,
            'workload_released':marker.exists(),'cleanup':cleanup,
            'missing_producer':('launch-owner no-spawn result' if stage=='reservation'
                                else 'retained direct-child wait result'),
            'recovered_signal':('refused' if isinstance(errors.get('recovered_signal'),dict)
                                else 'accepted' if 'recovered_signal' in errors else 'not-attempted'),
        },sort_keys=True))
    finally:
        if driver.poll() is None:driver.kill()
        driver.wait(timeout=5)
        if pid is not None and cleanup=='not-needed':cleanup_fake_identity(pid,anchor)
        for stream in (driver.stdout,driver.stderr):stream.close()
        # Host-owned synthetic sockets are not evidence. Preserve checkpoint/ledger.
        if snapshot and snapshot.get('socket'):
            socket=Path(snapshot['socket']);socket.unlink(missing_ok=True)
            try:socket.parent.rmdir()
            except FileNotFoundError:pass
