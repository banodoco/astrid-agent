"""Fake-only return evidence for H-CONFINE-WAN-CUSTODY-001.

The real launch/custody seam currently proves observed-set cleanup, not an
exhaustive history of untrusted descendants. Never enable Wan from this evidence.
"""
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

import pytest

from astrid.core.execution import generic_host as host
from astrid.core.execution import process_group as groups
from astrid.core.execution import custody_broker
from tests.core.execution.test_confinement_launch_integration import launch  # noqa: F401


class UnexpectedWorkloadHandle(AssertionError):
    """Only the reproduced inherited custody socket is expected to fail."""


class UnobservedDescendantAccepted(AssertionError):
    """Observed-set cleanup cannot discharge the Wan ownership obligation."""


WORKER = r'''
import ctypes,errno,json,os,stat,subprocess,sys,time
from pathlib import Path
inside,outside,project,role=sys.argv[1:]
project=Path(project)
def probe():
    result={'pid':os.getpid(),'ppid':os.getppid(),'sid':os.getsid(0),'fds':[]}
    for key,path in [('inside',inside),('outside',outside)]:
        try: result[key]={'value':Path(path).read_text()}
        except OSError as exc: result[key]={'errno':exc.errno}
    try:
        with open(outside,'a') as stream: stream.write('UNEXPECTED-WRITE')
        result['outside_write']={'allowed':True}
    except OSError as exc: result['outside_write']={'errno':exc.errno}
    class FDInfo(ctypes.Structure):
        _fields_=[('fd',ctypes.c_int32),('kind',ctypes.c_uint32)]
    library=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    library.proc_pidinfo.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int]
    library.proc_pidinfo.restype=ctypes.c_int
    size=library.proc_pidinfo(os.getpid(),1,0,None,0)
    if size<=0: raise RuntimeError('self FD census unavailable')
    entries=(FDInfo*(size//ctypes.sizeof(FDInfo)+128))()
    used=library.proc_pidinfo(os.getpid(),1,0,ctypes.byref(entries),ctypes.sizeof(entries))
    if used<=0 or used>=ctypes.sizeof(entries) or used%ctypes.sizeof(FDInfo):
        raise RuntimeError('self FD census incomplete')
    for entry in entries[:used//ctypes.sizeof(FDInfo)]:
        fd=entry.fd
        try:
            value=os.fstat(fd)
            result['fds'].append({'fd':fd,'device':value.st_dev,'inode':value.st_ino,'type':stat.S_IFMT(value.st_mode)})
        except OSError as exc:
            if exc.errno!=errno.EBADF: raise
    return result
if role=='descendant':
    # Same explicit inside grant/outside canaries, after an actual exec/setsid.
    value=probe()
    (project/'descendant.json').write_text(json.dumps(value))
    print('ready',flush=True)
    # Deliberately close inherited communication while continuing real work.
    for fd in range(3):
        try: os.close(fd)
        except OSError: pass
    until=time.monotonic()+15
    while time.monotonic()<until and not (project/'stop').exists():
        (project/'heartbeat').write_text(str(time.monotonic_ns()))
        time.sleep(.02)
    os._exit(0)
print(json.dumps(probe()),flush=True)
command=sys.stdin.readline().strip()
if command=='orphan':
    descendant=subprocess.Popen([sys.executable,'-I','-S','-B',__file__,inside,outside,str(project),'descendant'],
                                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                                start_new_session=True,close_fds=True)
    if descendant.stdout.readline()!=b'ready\n': raise RuntimeError('descendant startup failed')
    os._exit(0)
elif command=='exit':
    sys.exit(0)
else:
    raise RuntimeError('unexpected synthetic command')
'''


def wait_for(predicate, *, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    pytest.fail('synthetic worker observation deadline expired')


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


@pytest.fixture(autouse=True)
def private_socket_transport(monkeypatch, tmp_path_factory):
    """Keep fake sockets under the run root despite Darwin's short sockaddr.

    Only path spelling differs; sealing, observation, signals and cleanup are
    the real implementation. Each endpoint resolves relative to its own cwd.
    """
    def create():
        directory = Path(tempfile.mkdtemp(prefix='s-', dir=tmp_path_factory.getbasetemp().parent))
        relative = Path(os.path.relpath(directory))
        selected = min((directory, relative), key=lambda p: len(os.fsencode(p)))
        assert len(os.fsencode(selected / 's')) <= custody_broker.DARWIN_UNIX_SOCKET_PATH_MAX_BYTES
        return selected

    original = custody_broker.RoleBoundCustodyBroker.child_environment

    def environment(broker, *args, **kwargs):
        result = original(broker, *args, **kwargs)
        result['ASTRID_RUNTIME_CUSTODY_SOCKET'] = os.path.relpath(broker.socket_path.absolute(), target_cwd[0])
        return result

    target_cwd = [Path.cwd()]
    monkeypatch.setattr(custody_broker, '_create_compact_socket_root', create)
    monkeypatch.setattr(custody_broker.RoleBoundCustodyBroker, 'child_environment', environment)
    return target_cwd


def spawn(launch, tmp_path, private_socket_transport):
    config, _, inside, outside = launch
    worker = tmp_path / 'fake_wan.py'
    worker.write_text(WORKER)
    config = replace(config, bootstrap_files=(*config.bootstrap_files, worker))
    plan = host._confined_launch_plan(
        [str(Path(sys.executable).resolve()), '-I', '-S', '-B', str(worker),
         str(inside), str(outside), str(config.project.project_root), 'worker'],
        policy=config, parent_environment={},
    )
    assert plan.pass_fds == ()
    private_socket_transport[0] = plan.cwd
    process = groups.popen_owned_group(list(plan.argv), **plan.subprocess_options())
    assert process.stdout is not None and process.stdin is not None
    line = process.stdout.readline()
    if not line:
        process.wait(timeout=5)
        diagnostic = process.stderr.read().decode()
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        pytest.fail('synthetic worker failed startup: ' + diagnostic)
    first = json.loads(line)
    return config, process, first


def assert_denied_work(value):
    assert value['inside'] == {'value': 'synthetic-inside'}
    assert value['outside']['errno'] in {errno.EACCES, errno.EPERM}
    assert value['outside_write']['errno'] in {errno.EACCES, errno.EPERM}


def finish_fixture(config, process, descendant=None):
    (config.project.project_root / 'stop').touch()
    if process.poll() is None:
        # Only this original retained handle authorizes root cleanup.
        groups.terminate_group(process, grace_seconds=.2)
    process.wait(timeout=5)
    if descendant is not None:
        wait_for(lambda: (row := groups._process_snapshot().get(descendant)) is None or row.status == 5)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def test_confined_exec_descendant_denials_and_host_file_fds_absent(launch, tmp_path, private_socket_transport):
    lock = tmp_path / 'fake-wan-child.lock'
    sentinel = tmp_path / 'fake-host-handle'
    lock.write_text('synthetic-lock')
    sentinel.write_text('synthetic-host')
    with lock.open('rb') as first_fd, sentinel.open('rb') as second_fd:
        for stream in (first_fd, second_fd):
            os.set_inheritable(stream.fileno(), True)
        identities = {(os.fstat(stream.fileno()).st_dev, os.fstat(stream.fileno()).st_ino)
                      for stream in (first_fd, second_fd)}
        config, process, first = spawn(launch, tmp_path, private_socket_transport)
        descendant = None
        try:
            assert_denied_work(first)
            process.stdin.write(b'orphan\n'); process.stdin.flush()
            child = wait_for(lambda: read_json(config.project.project_root / 'descendant.json'))
            descendant = child['pid']
            assert_denied_work(child)
            assert child['pid'] == child['sid'] and child['sid'] != first['sid']
            for value in (first, child):
                assert not identities.intersection((fd['device'], fd['inode']) for fd in value['fds'])
            assert (tmp_path / 'fake-credential.txt').read_text() == 'synthetic-outside-credential'
            print('WAN_CUSTODY_CONFINEMENT', json.dumps({'worker': first, 'exec_descendant': child}))
            extras = [fd for value in (first, child) for fd in value['fds'] if fd['fd'] >= 3]
            assert all(fd['type'] == stat.S_IFSOCK for fd in extras), 'unexpected new FD type'
            if extras:
                raise UnexpectedWorkloadHandle('custody socket remains in confined workload: ' + json.dumps(extras))
        finally:
            finish_fixture(config, process, descendant)


@pytest.mark.xfail(strict=True, raises=UnobservedDescendantAccepted,
                   reason='Wan unavailable: census cleanup has no complete descendant history')
def test_current_cleanup_must_not_certify_an_unobserved_session_descendant(launch, tmp_path, private_socket_transport):
    config, process, first = spawn(launch, tmp_path, private_socket_transport)
    descendant = None
    try:
        custody = process._astrid_custody
        # A legal scheduling gap: no kernel birth event/comprehensive registry
        # queues the events during this existing observation lock interval.
        with custody._lock:
            process.stdin.write(b'orphan\n'); process.stdin.flush()
            child = wait_for(lambda: read_json(config.project.project_root / 'descendant.json'))
            descendant = child['pid']
            assert child['ppid'] == process.pid and child['sid'] == descendant
            assert_denied_work(first)
            assert_denied_work(child)
            process.wait(timeout=5)
        observed = groups.observe_tree(process)
        assert descendant not in custody._handles and descendant not in observed
        before = (config.project.project_root / 'heartbeat').read_text()
        wait_for(lambda: (config.project.project_root / 'heartbeat').read_text() != before)
        error = None
        try:
            groups.verify_tree_absent(process)
        except groups.CleanupUncertainError as exc:
            error = str(exc)
        row = groups._process_snapshot().get(descendant)
        assert row is not None and row.status != 5
        result = {'worker': process.pid, 'descendant': descendant, 'descendant_ppid': row.ppid,
                  'descendant_pgid': row.pgid, 'descendant_alive': True,
                  'retained_descendant': descendant in custody._handles,
                  'verify_tree_absent_error': error,
                  'cleanup_semantics': process._astrid_cleanup_semantics}
        print('WAN_CUSTODY_RETURN_COUNTEREXAMPLE', json.dumps(result))
        if error is None:
            raise UnobservedDescendantAccepted(
                'observed-set cleanup accepted while an untracked new-session descendant was alive')
    finally:
        finish_fixture(config, process, descendant)
