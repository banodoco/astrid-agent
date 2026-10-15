"""Fake-only durable pre-export recovery; no renderer or live workload."""
import fcntl
import os
import signal
import subprocess
import sys

import pytest

from astrid.core.execution import custody_broker as cb, process_group as groups


@pytest.fixture
def reservation(tmp_path):
    # The only live process is a disposable actor blocked on its original stdin.
    child = cb.RetainedCustodyPopen(
        [sys.executable, '-I', '-S', '-B', '-c', 'import sys; sys.stdin.buffer.readline()'],
        env={'PATH': '/usr/bin:/bin'}, stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent_pid = os.getpid()
    identities = {
        parent_pid: {'pid': parent_pid, 'uid': os.getuid(), 'birth_id': 'fake-parent'},
        child.pid: {'pid': child.pid, 'uid': os.getuid(), 'birth_id': 'fake-actor',
                    'parent_pid': parent_pid},
        999999: {'pid': 999999, 'uid': os.getuid(), 'birth_id': 'fake-stranger'},
    }
    def actor(pid):
        token = {'pid': pid, 'uid': os.getuid(), 'pidversion': 1,
                 'sha256': 'sha256:fake-' + str(pid)}
        return cb.AuthenticatedCleanupActor(lambda: token, identities.get)
    old, parent, stranger = actor(child.pid), actor(parent_pid), actor(999999)
    retained = cb.RetainedChildActor(child, old, parent=parent)
    authority = cb.RoleCustodyAuthority(tmp_path / 'scope', 'command', timeout=.2)
    authority.reserve_before_spawn(actor=old, owner_epoch='owner-1', admission_id='admission-1')
    args = dict(actor=parent, generation=1, owner_epoch='owner-1',
                expected_target={'admission_id': 'admission-1'}, transition_id='retire-1',
                dead_actor=retained)
    try:
        yield authority, child, args, old, parent, stranger, identities
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdin.close()


def die(child):
    child.kill()
    assert child.wait(timeout=5) == -signal.SIGKILL
    assert child._custody_wait_outcome is not None


def reopen(authority):
    return cb.RoleCustodyAuthority(authority.root, authority.role, timeout=.2)


def test_positive_native_parent_retirement_replays_then_acquires_generation_two(reservation):
    authority, child, args, _, parent, _, _ = reservation
    die(child)
    recovered = reopen(authority)
    before = recovered._read()
    ack = recovered.settle(**args)
    assert ack['status'] == 'retired' and ack['previous_digest'] == before['digest']
    assert recovered._read()['transitions'][-1]['evidence']['producer'] == 'retained-parent-native-wait'
    assert reopen(authority).settle(**{**args, 'dead_actor': None}) == ack
    recovered.reserve_before_spawn(actor=parent, owner_epoch='owner-2', admission_id='admission-2')
    assert recovered.reference()['generation'] == 2
    assert recovered.settle(**args) == ack
    with pytest.raises(cb.CustodyError):
        authority.begin_spawn()  # A stale original producer cannot launch after retirement.
    with pytest.raises(cb.CustodyError):
        recovered.settle(**{**args, 'transition_id': 'stale-1'})


def broker_for(authority, role, tmp_path, pipes=False):
    # Exercise the actual export methods without a broker listener or native service.
    broker = object.__new__(cb.RoleBoundCustodyBroker)
    broker.role = role
    broker.role_authority = authority
    broker.socket_path = tmp_path / 'fake-socket'
    broker.run_id = 'admission-1'
    broker.timeout = .2
    broker.child_pipe_binding = [{'fd': 12345}] if pipes else []
    return broker


def export_path(path, authority, tmp_path, monkeypatch, popen=None):
    if path.startswith('environment-'):
        return broker_for(authority, path.removeprefix('environment-'), tmp_path).child_environment(
            [sys.executable, '-c', 'pass'], start_new_session=True)
    if path.startswith('pipes-'):
        return broker_for(authority, path.removeprefix('pipes-'), tmp_path, pipes=True).child_pipe_fds
    if path == 'direct-begin':
        return authority.begin_spawn()
    broker = broker_for(authority, 'command', tmp_path)
    class Owner:
        _launch_broker = broker
        begin_launch_spawn = cb.ProcessCustody.begin_launch_spawn
        def prepare_launch(self, argv, **kwargs):
            return argv, {}  # No prior export: test the immediate Popen barrier itself.
        def cleanup_failed_launch(self, **kwargs):
            with pytest.raises(cb.CustodyError):
                authority.settle(actor=authority._unspawned_reservation.actor, generation=1,
                                 owner_epoch='owner-1', expected_target={'admission_id': 'admission-1'},
                                 transition_id='unsafe-popen-abort',
                                 unspawned=authority._unspawned_reservation)
    monkeypatch.setattr(groups, 'ProcessCustody', Owner)
    monkeypatch.setattr(groups, 'RetainedCustodyPopen', popen or (lambda *a, **k: pytest.fail('Popen escaped')))
    method = groups.popen_owned_group if path == 'creation-group' else groups.popen_owned_process
    return method([sys.executable, '-c', 'pass'], _delegate_inventory=False, env={})


PATHS = ['environment-command', 'environment-engine', 'environment-engine_listener',
         'pipes-command', 'pipes-engine', 'direct-begin', 'creation-process', 'creation-group']


@pytest.mark.parametrize('path', PATHS)
@pytest.mark.parametrize('durable', [False, True])
def test_crash_on_each_side_of_export_commit(reservation, tmp_path, monkeypatch, path, durable):
    authority, child, args, *_ = reservation
    # Each role is independently generation-bound; reuse only the fixture machinery.
    role = path.removeprefix('environment-') if path.startswith('environment-') else (
        path.removeprefix('pipes-') if path.startswith('pipes-') else 'command')
    if role != authority.role:
        authority = cb.RoleCustodyAuthority(tmp_path / role, role)
        authority.reserve_before_spawn(actor=reservation[3], owner_epoch='owner-1', admission_id='admission-1')
    write = authority._write
    def crash(record):
        assert record['transitions'][-1]['ack']['status'] == 'may-have-exported'
        if durable:
            write(record)
        raise OSError('synthetic commit interruption')
    monkeypatch.setattr(authority, '_write', crash)
    with pytest.raises(OSError):
        export_path(path, authority, tmp_path, monkeypatch)
    die(child)
    # The uncertain original instance refuses, even when disk still shows never-exported.
    with pytest.raises(cb.CustodyError):
        authority.settle(**args)
    recovered = reopen(authority)
    if durable:
        before = recovered._read()
        with pytest.raises(cb.CustodyError, match='no-spawn'):
            recovered.settle(**args)
        assert recovered._read() == before
    else:
        assert recovered.settle(**args)['status'] == 'retired'


@pytest.mark.parametrize('path', PATHS)
def test_each_successful_export_is_durably_fenced(reservation, tmp_path, monkeypatch, path):
    authority, child, args, *_ = reservation
    role = path.removeprefix('environment-') if path.startswith('environment-') else (
        path.removeprefix('pipes-') if path.startswith('pipes-') else 'command')
    if role != authority.role:
        authority = cb.RoleCustodyAuthority(tmp_path / role, role)
        authority.reserve_before_spawn(actor=reservation[3], owner_epoch='owner-1', admission_id='admission-1')
    def failed_popen(*a, **k):
        disk = reopen(authority)
        assert not disk._never_exported(disk._read())
        raise OSError('synthetic Popen failure after possible creation')
    if path.startswith('creation-'):
        with pytest.raises(OSError):
            export_path(path, authority, tmp_path, monkeypatch, failed_popen)
    else:
        export_path(path, authority, tmp_path, monkeypatch)
        export_path(path, authority, tmp_path, monkeypatch)  # idempotent repeated export
    die(child)
    with pytest.raises(cb.CustodyError, match='no-spawn'):
        reopen(authority).settle(**args)
    assert authority._read()['state'] == 'pending'  # No child row is not proof.


@pytest.mark.parametrize('case', ['live', 'missing-wait', 'external-reap', 'wrong-parent',
                                  'stale-parent', 'generation', 'owner', 'admission', 'role',
                                  'missing-marker', 'wrong-marker-generation', 'unknown-marker',
                                  'missing-state', 'no-parent-provenance', 'unknown-suffix', 'boolean-marker-generation'])
def test_recovery_refuses_unproved_or_changed_provenance(reservation, case):
    authority, child, args, old, parent, stranger, identities = reservation
    recovered = reopen(authority)
    if case == 'external-reap':
        child.kill()
        os.waitpid(child.pid, 0)
        assert child.poll() == 0  # CPython ECHILD fallback cannot create native evidence.
    elif case != 'live':
        die(child)
    if case == 'missing-wait': child._custody_wait_outcome = None
    if case == 'wrong-parent': args['actor'] = stranger
    if case == 'stale-parent': identities[os.getpid()]['birth_id'] = 'reused-parent'
    if case == 'generation': args['generation'] = 2
    if case == 'owner': args['owner_epoch'] = 'wrong-owner'
    if case == 'admission': args['expected_target'] = {'admission_id': 'wrong-admission'}
    if case == 'role': recovered = cb.RoleCustodyAuthority(authority.root, 'engine')
    if case in {'missing-marker', 'wrong-marker-generation', 'unknown-marker'}:
        record = recovered._read()
        if case == 'missing-marker': record['transitions'] = []
        if case == 'wrong-marker-generation': record['transitions'][0]['intent']['generation'] = 2
        if case == 'unknown-marker': record['transitions'][0]['ack']['status'] = 'uncertain'
        recovered._write(record)
    if case in {'unknown-suffix', 'boolean-marker-generation'}:
        record = recovered._read()
        if case == 'unknown-suffix': record['transitions'].append({'intent': {'action': 'uncertain'}})
        else: record['transitions'][0]['intent']['generation'] = True
        recovered._write(record)
    if case == 'missing-state': authority.path.unlink()
    if case == 'no-parent-provenance': args['dead_actor']._parent = None
    before = authority.path.read_bytes() if authority.path.exists() else None
    with pytest.raises((cb.CustodyError, FileNotFoundError)):
        recovered.settle(**args)
    assert (authority.path.read_bytes() if authority.path.exists() else None) == before


@pytest.mark.parametrize('export_first', [False, True])
def test_recovery_and_late_export_use_same_exclusive_lock(reservation, monkeypatch, export_first):
    authority, child, args, *_ = reservation
    recovered = reopen(authority)
    if export_first:
        authority.begin_spawn()
    die(child)
    proof = args['dead_actor']
    original = proof.prove_parent_wait
    def prove(expected, parent):
        fd = os.open(authority.root / 'cleanup.lock', os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
        with pytest.raises(cb.CustodyError):
            authority.begin_spawn()  # Cannot pass retirement while its proof holds the lock.
        return original(expected, parent)
    monkeypatch.setattr(proof, 'prove_parent_wait', prove)
    if export_first:
        with pytest.raises(cb.CustodyError): recovered.settle(**args)
    else:
        assert recovered.settle(**args)['status'] == 'retired'
        with pytest.raises(cb.CustodyError): authority.begin_spawn()


@pytest.mark.parametrize('durable', [False, True])
def test_retirement_commit_crash_and_ack_replay(reservation, monkeypatch, durable):
    authority, child, args, _, parent, *_ = reservation
    die(child)
    recovered = reopen(authority)
    write = recovered._write
    def crash(record):
        if durable: write(record)
        raise OSError('synthetic retirement commit interruption')
    monkeypatch.setattr(recovered, '_write', crash)
    with pytest.raises(OSError): recovered.settle(**args)
    after = reopen(authority)
    assert after._read()['state'] == ('retired' if durable else 'pending')
    assert after.settle(**args)['status'] == 'retired'
    after.reserve_before_spawn(actor=parent, owner_epoch='owner-2', admission_id='admission-2')
    assert after.reference()['generation'] == 2


def test_close_private_pipes_does_not_export_and_empty_descriptor_set_has_no_authority(reservation, tmp_path):
    authority, _, _, old, *_ = reservation
    read, write = os.pipe()
    broker = broker_for(authority, 'command', tmp_path)
    assert broker.child_pipe_fds == ()
    broker.child_pipe_binding = [{'fd': read}, {'fd': write}]
    broker.close_child_pipes()
    assert authority._never_exported(authority._read())
    assert authority.settle(actor=old, generation=1, owner_epoch='owner-1',
                            expected_target={'admission_id': 'admission-1'}, transition_id='local-abort',
                            unspawned=authority._unspawned_reservation)['status'] == 'retired'


CRASH_ACTOR = r'''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from astrid.core.execution import custody_broker as cb
root=Path(sys.argv[2]); durable=sys.argv[3]=='after'
authority=cb.RoleCustodyAuthority(root,'command')
actor=cb.AuthenticatedCleanupActor.current()
authority.reserve_before_spawn(actor=actor,owner_epoch='owner-1',admission_id='admission-1')
write=authority._write
def checkpoint(record):
    if durable:write(record)
    (root/'crash-checkpoint').touch()
    while True:time.sleep(.01)
authority._write=checkpoint
authority.begin_spawn()
'''


@pytest.mark.parametrize('side', ['before', 'after'])
def test_abrupt_actor_death_on_each_side_of_durable_export(tmp_path, side):
    # Real SIGKILL destroys the actor's memory. Only the still-live original
    # parent, exact protected state and retained native wait survive.
    import time
    from pathlib import Path
    source = str(Path(cb.__file__).resolve().parents[3])
    root = tmp_path / 'crashed-actor'
    child = cb.RetainedCustodyPopen(
        [sys.executable, '-I', '-S', '-B', '-c', CRASH_ACTOR, source, str(root), side],
        env={'PATH': '/usr/bin:/bin'}, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not (root / 'crash-checkpoint').exists():
            assert child.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        old = cb.AuthenticatedCleanupActor(lambda: cb.current_process_audit_token(child.pid),
                                           cb.default_process_identity)
        parent = cb.AuthenticatedCleanupActor.current()
        retained = cb.RetainedChildActor(child, old, parent=parent)
        die(child)
        recovered = cb.RoleCustodyAuthority(root, 'command')
        args = dict(actor=parent, generation=1, owner_epoch='owner-1',
                    expected_target={'admission_id': 'admission-1'},
                    transition_id='retire-1', dead_actor=retained)
        if side == 'after':
            before = recovered._read()
            with pytest.raises(cb.CustodyError, match='no-spawn'):
                recovered.settle(**args)
            assert recovered._read() == before
            with pytest.raises(cb.CustodyError):
                recovered.reserve_before_spawn(actor=parent, owner_epoch='owner-2', admission_id='next')
        else:
            assert recovered.settle(**args)['status'] == 'retired'
            recovered.reserve_before_spawn(actor=parent, owner_epoch='owner-2', admission_id='next')
            assert recovered.reference()['generation'] == 2
    finally:
        if child.poll() is None: child.kill()
        child.wait(timeout=5)


@pytest.mark.parametrize('field', ['role', 'owner_epoch', 'requester', 'target'])
def test_durable_witness_requires_exact_reservation_binding(reservation, field):
    authority, child, args, *_ = reservation
    die(child)
    record = authority._read()
    record['transitions'][0]['intent'][field] = 'foreign'
    authority._write(record)
    with pytest.raises(cb.CustodyError, match='no-spawn'):
        reopen(authority).settle(**args)


def test_actor_absence_does_not_replace_wait_and_wrong_parent_cannot_pin(reservation):
    authority, child, args, old, _, stranger, identities = reservation
    with pytest.raises(cb.CustodyError, match='direct-child parent'):
        cb.RetainedChildActor(child, old, parent=stranger)
    identities.pop(child.pid)  # No PID row, no registered workload, still no death proof.
    with pytest.raises(cb.CustodyError):
        reopen(authority).settle(**args)
    assert authority._read()['state'] == 'pending'


CREATION_RACE = r'''
import subprocess,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from astrid.core.execution import custody_broker as cb,process_group as groups
root=Path(sys.argv[2])
authority=cb.RoleCustodyAuthority(root,'command')
authority.reserve_before_spawn(actor=cb.AuthenticatedCleanupActor.current(),owner_epoch='owner-1',admission_id='admission-1')
class Owner:
    def prepare_launch(self,argv,**kwargs):return argv,{}
    def begin_launch_spawn(self):authority.begin_spawn()
class PausingPopen(cb.RetainedCustodyPopen):
    def _execute_child(self,*args,**kwargs):
        super()._execute_child(*args,**kwargs)
        # A real child now exists, but Popen.__init__ has not returned to its
        # caller. No workload handle/registration can yet have been recorded.
        cb._atomic_owner_json(root/'creation-checkpoint.json',
            {'pid':self.pid,'anchor':cb._start_anchor(cb._darwin_process_start(self.pid))})
        while True:time.sleep(.01)
groups.ProcessCustody=Owner
groups.RetainedCustodyPopen=PausingPopen
launch=groups.popen_owned_group if sys.argv[3]=='group' else groups.popen_owned_process
launch([sys.executable,'-I','-S','-B','-c','import sys;sys.stdin.buffer.read()'],
       stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
       env={'PATH':'/usr/bin:/bin'},_delegate_inventory=False)
'''


@pytest.mark.parametrize('method', ['process', 'group'])
def test_real_popen_creation_before_constructor_return_is_already_fenced(tmp_path, method):
    import json
    from pathlib import Path
    from tests.core.execution.test_native_creation_crash_windows import wait_for, cleanup_fake_identity
    root = tmp_path / 'creation-race'
    source = str(Path(cb.__file__).resolve().parents[3])
    driver = cb.RetainedCustodyPopen(
        [sys.executable, '-I', '-S', '-B', '-c', CREATION_RACE, source, str(root), method],
        env={'PATH': '/usr/bin:/bin'}, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    checkpoint = None
    try:
        path = root / 'creation-checkpoint.json'
        wait_for(path.exists)
        checkpoint = json.loads(path.read_text())
        old = cb.AuthenticatedCleanupActor(lambda: cb.current_process_audit_token(driver.pid),
                                           cb.default_process_identity)
        parent = cb.AuthenticatedCleanupActor.current()
        retained = cb.RetainedChildActor(driver, old, parent=parent)
        die(driver)
        recovered = cb.RoleCustodyAuthority(root, 'command')
        before = recovered._read()
        assert before['target'] == {'admission_id': 'admission-1'}
        assert before['transitions'][-1]['ack']['status'] == 'may-have-exported'
        with pytest.raises(cb.CustodyError, match='no-spawn'):
            recovered.settle(actor=parent, generation=1, owner_epoch='owner-1',
                             expected_target={'admission_id': 'admission-1'},
                             transition_id='unsafe-creation-retirement', dead_actor=retained)
        assert recovered._read() == before
    finally:
        if driver.poll() is None: driver.kill()
        driver.wait(timeout=5)
        if checkpoint:
            cleanup_fake_identity(checkpoint['pid'], checkpoint['anchor'])
