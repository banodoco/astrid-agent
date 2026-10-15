"""Bounded owner lifecycle proofs; synthetic children and owner-only state."""
from dataclasses import replace
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from astrid.core.execution import custody_broker as cb, generic_host as host, process_group as groups
from tests.core.execution.test_confinement_launch_integration import launch  # noqa: F401
from tests.core.execution.test_wan_custody_limits import private_socket_transport  # noqa: F401


@pytest.fixture
def owned(tmp_path, monkeypatch):
    identities = {101: {'pid': 101, 'uid': 501, 'birth_id': 'owner'},
                  102: {'pid': 102, 'uid': 501, 'birth_id': 'other-owner'}}
    tokens = {pid: {'pid': pid, 'uid': 501, 'pidversion': pid + 1,
                    'sha256': 'sha256:synthetic-' + str(pid), 'words': [pid] * 8} for pid in identities}
    actors = {pid: cb.AuthenticatedCleanupActor(lambda pid=pid: tokens[pid], identities.get) for pid in identities}
    child = cb.RetainedCustodyPopen([sys.executable, '-I', '-S', '-B', '-c',
                                    'import sys; sys.stdin.buffer.readline()'],
                                   env={'PATH': '/usr/bin:/bin'}, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    identities[child.pid] = {'pid': child.pid, 'uid': 501, 'parent_pid': 101, 'birth_id': 'original-child'}
    tokens[child.pid] = {'pid': child.pid, 'uid': 501, 'pidversion': 9,
                         'sha256': 'sha256:synthetic-child', 'words': [child.pid] * 8}
    monkeypatch.setattr(cb, 'audit_token_details', lambda words: {k: v for k, v in tokens[words[0]].items() if k != 'words'})
    authority = cb.RoleCustodyAuthority(tmp_path / 'scope', 'command')
    authority.designate_pending(actor=actors[101], identity=identities[child.pid], token=tokens[child.pid], owner_epoch='owner-1')
    authority.bind_target(actor=actors[101], generation=1, identity=identities[child.pid], token=tokens[child.pid])
    retained = authority.retain_child(actor=actors[101], generation=1, child=child, identity_provider=identities.get)
    args = dict(actor=actors[101], generation=1, owner_epoch='owner-1',
                expected_target=authority.reference()['target'], transition_id='terminal-1')
    try:
        yield authority, child, retained, args, actors
    finally:
        if child.poll() is None:
            child.stdin.write(b'exit\n'); child.stdin.flush()
        child.wait(timeout=5)
        child.stdin.close()


def finish(child):
    child.stdin.write(b'exit\n'); child.stdin.flush()
    assert child.wait(timeout=5) == 0


def test_settlement_replay_reconstruction_and_fresh_generation(owned):
    authority, child, retained, args, actors = owned
    finish(child)
    ack = authority.settle(**args, retained_child=retained)
    record = authority._read()
    assert record['state'] == 'settled'
    assert ack['previous_digest'] != record['digest']
    assert ack['evidence_digest'] == cb._digest_bytes(cb._canonical(record['transitions'][-1]['evidence']))
    recovered = cb.RoleCustodyAuthority(authority.root, 'command')
    assert recovered.settle(**args) == ack  # lost ACK replay requires no invented wait.
    recovered.reserve_before_spawn(actor=actors[101], owner_epoch='owner-2', admission_id='next')
    assert recovered.reference()['generation'] == 2
    assert recovered.settle(**args) == ack
    with pytest.raises(cb.CustodyError, match='already has'):
        cb.RoleCustodyAuthority(authority.root, 'command').reserve_before_spawn(
            actor=actors[101], owner_epoch='owner-3', admission_id='overlap')


@pytest.mark.parametrize('case', ['no-wait', 'live', 'actor', 'generation', 'target', 'owner', 'role'])
def test_settlement_rejects_missing_or_changed_authority(owned, case):
    authority, child, retained, args, actors = owned
    if case not in {'live', 'no-wait'}: finish(child)
    if case == 'actor': args['actor'] = actors[102]
    if case == 'generation': args['generation'] = 2
    if case == 'target': args['expected_target'] = {**args['expected_target'], 'birth_id': 'reused'}
    if case == 'owner': args['owner_epoch'] = 'foreign'
    if case == 'role': authority = cb.RoleCustodyAuthority(authority.root, 'other')
    with pytest.raises((cb.CustodyError, FileNotFoundError)):
        authority.settle(**args, retained_child=None if case == 'no-wait' else retained)


def test_conflicting_settlement_and_replay_are_refused(owned):
    authority, child, retained, args, _ = owned
    finish(child)
    authority.settle(**args, retained_child=retained)
    with pytest.raises(cb.CustodyError, match='replay conflicts'):
        authority.settle(**{**args, 'expected_target': {**args['expected_target'], 'birth_id': 'other'}})
    with pytest.raises(cb.CustodyError, match='target or owner'):
        authority.settle(**{**args, 'transition_id': 'duplicate'}, retained_child=retained)


def test_external_reap_and_popen_cached_zero_cannot_settle(owned):
    authority, child, retained, args, _ = owned
    child.stdin.write(b'exit\n'); child.stdin.flush()
    pid, status = os.waitpid(child.pid, 0)
    assert pid == child.pid and os.WIFEXITED(status)
    assert child.poll() == 0  # CPython's ECHILD cache is not positive wait evidence.
    assert child._custody_wait_outcome is None
    with pytest.raises(cb.CustodyError, match='positive native'):
        authority.settle(**args, retained_child=retained)
    assert authority._read()['state'] == 'active'


def test_unspawned_retirement_is_owner_local_and_export_fenced(tmp_path, owned):
    _, _, _, _, actors = owned
    authority = cb.RoleCustodyAuthority(tmp_path / 'unspawned', 'command')
    authority.reserve_before_spawn(actor=actors[101], owner_epoch='reservation', admission_id='never-created')
    args = dict(actor=actors[101], generation=1, owner_epoch='reservation',
                expected_target={'admission_id': 'never-created'}, transition_id='retire')
    with pytest.raises(cb.CustodyError, match='no-spawn'):
        cb.RoleCustodyAuthority(authority.root, 'command').settle(**args)
    ack = authority.settle(**args, unspawned=authority._unspawned_reservation)
    assert ack['status'] == 'retired'
    authority.reserve_before_spawn(actor=actors[101], owner_epoch='next', admission_id='may-create')
    authority.begin_spawn()
    with pytest.raises(cb.CustodyError, match='no-spawn'):
        authority.settle(actor=actors[101], generation=2, owner_epoch='next',
                         expected_target={'admission_id': 'may-create'}, transition_id='false-retire',
                         unspawned=authority._unspawned_reservation)


@pytest.mark.parametrize('cancel', [False, True])
def test_actual_restricted_broker_settles_and_reacquires(launch, private_socket_transport, tmp_path, monkeypatch, cancel):
    policy, _, _, _ = launch
    monkeypatch.chdir(tmp_path)
    scope = tmp_path / 'owned-scope'
    original = groups.ProcessCustody
    class ScopedCustody(original):
        def __init__(self):
            super().__init__(scope_root=scope, command_diagnostics=False)
    monkeypatch.setattr(groups, 'ProcessCustody', ScopedCustody)
    code = "import sys; print('ready',flush=True); sys.stdin.buffer.readline()"
    plan = host._wan_creation_control_launch_plan(
        [str(Path(sys.executable).resolve()), '-I', '-S', '-B', '-c', code], policy=policy)
    private_socket_transport[0] = plan.cwd
    for generation in (1, 2):
        process = groups.popen_owned_group(list(plan.argv), **plan.subprocess_options())
        try:
            assert process.stdout.readline() == b'ready\n'
            broker = process._astrid_custody._launch_broker
            assert broker.custody_generation == generation
            if cancel:
                groups.terminate_tree(process)
            else:
                process.communicate(input=b'exit\n', timeout=5)
            groups.verify_tree_absent(process)
            assert broker.role_authority._read()['state'] == 'settled'
            assert process._custody_wait_outcome is not None
            assert broker.settle_launch()['status'] == 'settled'
        finally:
            if process.poll() is None: groups.terminate_tree(process)
            process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr): stream.close()


def test_terminal_cannot_reactivate_and_same_authority_resets_generation(owned):
    authority, child, retained, args, actors = owned
    finish(child)
    authority.settle(**args, retained_child=retained)
    with pytest.raises(cb.CustodyError, match='terminal custody'):
        authority.bind_target(actor=actors[101], generation=1, identity={}, token={})
    authority._ready_base = {'old': 'readiness'}
    authority._live_start_anchor = {'old': 'anchor'}
    authority._ready_committed = True
    authority.reserve_before_spawn(actor=actors[101], owner_epoch='next', admission_id='next-child')
    assert authority.reference()['generation'] == 2
    assert authority._ready_base is authority._live_start_anchor is None
    assert not authority._ready_committed


def test_actual_broker_export_fences_unspawned_retirement(launch, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Short relative socket spelling; all artifacts stay below this task's fake root.
    def compact():
        import tempfile
        return Path(tempfile.mkdtemp(prefix='s-', dir=tmp_path)).relative_to(tmp_path)
    monkeypatch.setattr(cb, '_create_compact_socket_root', compact)
    before = cb.RoleBoundCustodyBroker(role='command', identity_provider=cb.default_process_identity,
                                       ledger_root=tmp_path / 'before-ledger', timeout=0.2)
    before.abort_before_spawn()
    assert before.role_authority._read()['state'] == 'retired'
    exported = cb.RoleBoundCustodyBroker(role='command', identity_provider=cb.default_process_identity,
                                         ledger_root=tmp_path / 'after-ledger', timeout=0.2)
    try:
        exported.child_environment([sys.executable, '-c', 'pass'], start_new_session=True)
        with pytest.raises(cb.CustodyError, match='no-spawn'):
            exported.abort_before_spawn()
        assert exported.role_authority._read()['state'] == 'pending'
    finally:
        exported.listener.close()
        exported._thread.join(timeout=2)
        exported.socket_path.unlink(missing_ok=True)
        try:
            exported.socket_root.rmdir()
        except FileNotFoundError:
            pass  # The broker thread may already have removed its own socket root.
        exported._release_authority_scope()


@pytest.mark.parametrize('durable', [False, True])
def test_settlement_commit_failure_and_lost_ack_preserve_exact_state(owned, monkeypatch, durable):
    authority, child, retained, args, actors = owned
    finish(child)
    write = authority._write
    def fail(record):
        if durable:
            write(record)
        raise OSError('synthetic settlement commit interruption')
    monkeypatch.setattr(authority, '_write', fail)
    with pytest.raises(OSError):
        authority.settle(**args, retained_child=retained)
    recovered = cb.RoleCustodyAuthority(authority.root, 'command')
    assert recovered._read()['state'] == ('settled' if durable else 'active')
    if durable:
        ack = recovered.settle(**args)
        assert ack['status'] == 'settled'
        recovered.reserve_before_spawn(actor=actors[101], owner_epoch='next', admission_id='next')
        assert recovered.reference()['generation'] == 2
    else:
        with pytest.raises(cb.CustodyError, match='positive retained-child'):
            recovered.settle(**args)
        with pytest.raises(cb.CustodyError, match='already has'):
            recovered.reserve_before_spawn(actor=actors[101], owner_epoch='next', admission_id='next')
