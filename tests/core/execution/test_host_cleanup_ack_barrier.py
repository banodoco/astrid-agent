"""The retained host must deliver cleanup acknowledgement before it exits."""
import json
import errno
import os
import select
import socket
import subprocess
import sys
import textwrap

import pytest

from astrid.core.execution import generic_host as host


@pytest.mark.skipif(sys.platform != 'darwin', reason='Darwin native audit-token lifecycle')
def test_native_exec_changes_token_without_changing_process_lifetime():
    """A controlled exec reproduces the stale token; imports are a separate phase.

    These child self-observations diagnose a lifecycle, not peer authentication.
    The real-peer test below independently captures the ready child in its parent.
    """
    capture = textwrap.dedent('''
        import json, os
        from astrid.core.execution import custody_broker as custody
        def snapshot(phase):
            identity = custody.default_process_identity(os.getpid())
            assert identity is not None
            token = custody.current_process_audit_token(os.getpid())
            assert custody.default_process_identity(os.getpid()) == identity
            print(json.dumps({'phase': phase, 'identity': custody._incarnation(identity, token)}), flush=True)
    ''')
    final = capture + "snapshot('after_exec')\n"
    initial = capture + textwrap.dedent('''
        import sys
        snapshot('before_host_import')
        from astrid.core.execution import generic_host
        snapshot('after_host_import')
        os.execv(sys.executable, [sys.executable, '-c', sys.argv[1]])
    ''')
    result = subprocess.run([sys.executable, '-c', initial, final],
                            capture_output=True, text=True, timeout=15)
    print(result.stdout, end='')
    assert result.returncode == 0, result.stderr
    snapshots = [json.loads(line) for line in result.stdout.splitlines()]
    assert [item['phase'] for item in snapshots] == ['before_host_import', 'after_host_import', 'after_exec']
    before, imported, executed = [item['identity'] for item in snapshots]
    assert imported == before, 'host import changed native identity'
    assert {key: before[key] for key in ('pid', 'uid', 'birth_id')} == {
        key: executed[key] for key in ('pid', 'uid', 'birth_id')}
    assert executed['audit_token_pidversion'] != before['audit_token_pidversion']
    assert executed['audit_token_sha256'] != before['audit_token_sha256']


def test_delayed_authenticated_cleanup_ack_precedes_host_exit_and_reap(tmp_path):
    client, server = socket.socketpair()
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    script = textwrap.dedent('''
        import os, socket, sys, threading
        from types import SimpleNamespace
        from astrid.core.execution import generic_host as h, custody_broker as custody

        control, ready, release = map(int, sys.argv[1:4])
        stopped = threading.Event()
        p = h.LocalExecutionPreparation(operation_id='op', channel_id='ch')
        profile = {'execution_mode': 'host_only_cpu', 'profile_digest': 'digest'}
        p._request = {'owner_epoch': 'epoch', 'profile': profile, 'custody_scope': sys.argv[4]}
        p._claim_host = SimpleNamespace(shutdown=stopped.set)
        h.process_birth_identity = lambda pid=None: 'fixture-birth'
        custody.AuthenticatedCleanupActor.private_peer = staticmethod(lambda sock: SimpleNamespace(verify=lambda: {'pid': 1}))
        class Authority:
            def __init__(self, *args): pass
            def verify_reference(self, *args, **kwargs): pass
        custody.RoleCustodyAuthority = Authority
        send = h._send_activation_frame
        def delayed(sock, reply):
            os.write(ready, b'1')
            assert os.read(release, 1) == b'1'
            send(sock, reply)
        h._send_activation_frame = delayed
        def run(lifecycles):
            lifecycles.append(p)
            p.serve_control(control)
            assert stopped.wait(10)
            return 0
        h._cli_impl = run
        sys.exit(h._cli())
    ''')
    process = subprocess.Popen([sys.executable, '-c', script, str(server.fileno()),
                                str(ready_write), str(release_read), str(tmp_path)],
                               pass_fds=(server.fileno(), ready_write, release_read))
    server.close()
    os.close(ready_write)
    os.close(release_read)
    client.settimeout(10)
    try:
        profile = {'execution_mode': 'host_only_cpu', 'profile_digest': 'digest'}
        request = {'version': host._LOCAL_PREPARATION_VERSION, 'command': 'abort_local_execution',
                   'operation_id': 'op', 'channel_id': 'ch', 'owner_epoch': 'epoch',
                   'profile_digest': 'digest', 'profile_binding_digest': host._handoff_digest(profile),
                   'custody_scope': str(tmp_path), 'custody_capabilities': {'host': {
                       'target': {'pid': process.pid, 'birth_id': 'fixture-birth'}}}}
        host._send_activation_frame(client, request)
        assert select.select([ready_read], [], [], 10)[0], 'cleanup reply was not reached'
        assert os.read(ready_read, 1) == b'1'
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.5)
        os.write(release_write, b'1')
        reply = host._read_activation_frame(client)
        assert reply['status'] == 'cleaned'
        assert reply['cleanup'] == {'host_children': 'absent'}
        assert process.wait(timeout=10) == 0
    finally:
        client.close()
        os.close(ready_read)
        os.close(release_write)
        if process.poll() is None:
            process.kill()
            process.wait()


def test_failed_cleanup_ack_does_not_release_exit_barrier(monkeypatch):
    from types import SimpleNamespace
    from astrid.core.execution import custody_broker as custody

    client, server = socket.socketpair()
    preparation = host.LocalExecutionPreparation(operation_id="op", channel_id="ch")
    preparation._control_cleanup_done.clear()
    monkeypatch.setattr(custody.AuthenticatedCleanupActor, "private_peer",
                        staticmethod(lambda sock: SimpleNamespace()))
    monkeypatch.setattr(preparation, "dispatch", lambda frame: {"status": "cleaned"})
    def fail_send(sock, reply):
        raise BrokenPipeError("closed cleanup receiver")
    monkeypatch.setattr(host, "_send_activation_frame", fail_send)
    thread = preparation.serve_control(server.detach())
    try:
        encoded = b'{"command":"abort_local_execution"}'
        client.sendall(encoded + b"\n")
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError as exc:
            # The injected failed ACK can close the peer before half-close.
            # Still require the original control-error and barrier assertions.
            if exc.errno != errno.ENOTCONN:
                raise
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert isinstance(preparation.control_error, BrokenPipeError)
        assert not preparation._control_cleanup_done.is_set()
    finally:
        client.close()


def test_real_private_peer_delayed_cleanup_ack_before_exit(tmp_path):
    """Actual kernel peer and protected role designation; no authentication doubles."""
    from astrid.core.execution import custody_broker as custody

    client, server = socket.socketpair()
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    script = textwrap.dedent('''
        import os, sys, threading, traceback
        from types import SimpleNamespace
        from astrid.core.execution import generic_host as h
        control, ready, release = map(int, sys.argv[1:4])
        stopped = threading.Event()
        p = h.LocalExecutionPreparation(operation_id='op', channel_id='ch')
        p._request = {'owner_epoch': 'epoch', 'profile': {'execution_mode': 'host_only_cpu', 'profile_digest': 'digest'}, 'custody_scope': sys.argv[4]}
        p._claim_host = SimpleNamespace(shutdown=stopped.set)
        send = h._send_activation_frame
        def delayed(sock, reply):
            os.write(ready, b'1')
            assert os.read(release, 1) == b'1'
            send(sock, reply)
        h._send_activation_frame = delayed
        def run(lifecycles):
            lifecycles.append(p)
            control_thread = p.serve_control(control)
            def report_control_error():
                control_thread.join()
                if p.control_error is not None:
                    traceback.print_exception(p.control_error)
            threading.Thread(target=report_control_error, daemon=True).start()
            # The final Python has completed launcher execs, imports and setup.
            # This is readiness only; the parent still captures the kernel token.
            os.write(ready, b'R')
            assert stopped.wait(15)
            return 0
        h._cli_impl = run
        sys.exit(h._cli())
    ''')
    process = subprocess.Popen([sys.executable, '-c', script, str(server.fileno()),
                                str(ready_write), str(release_read), str(tmp_path)],
                               pass_fds=(server.fileno(), ready_write, release_read))
    server.close()
    os.close(ready_write)
    os.close(release_read)
    client.settimeout(15)
    try:
        # Popen may return in a framework Python launcher before its SETEXEC.
        # Never designate that provisional image or refresh it from peer data.
        assert select.select([ready_read], [], [], 15)[0], 'final host Python did not become ready'
        assert os.read(ready_read, 1) == b'R'
        actor = custody.AuthenticatedCleanupActor.current()
        target = custody.default_process_identity(process.pid)
        assert target is not None, 'native process identity unavailable'
        assert target['parent_pid'] == os.getpid()
        token = custody.current_process_audit_token(process.pid)
        assert custody.default_process_identity(process.pid) == target, 'ready child identity changed during capture'
        authority = custody.RoleCustodyAuthority(tmp_path, 'host')
        authority.designate_pending(actor=actor, identity=target, token=token, owner_epoch='epoch')
        reference = authority.bind_target(actor=actor, generation=1, identity=target, token=token)
        profile = {'execution_mode': 'host_only_cpu', 'profile_digest': 'digest'}
        request = {'version': host._LOCAL_PREPARATION_VERSION, 'command': 'abort_local_execution',
                   'operation_id': 'op', 'channel_id': 'ch', 'owner_epoch': 'epoch',
                   'profile_digest': 'digest', 'profile_binding_digest': host._handoff_digest(profile),
                   'custody_scope': str(tmp_path), 'custody_capabilities': {'host': reference}}
        host._send_activation_frame(client, request)
        assert select.select([ready_read], [], [], 15)[0], 'authenticated cleanup did not reach ACK'
        assert os.read(ready_read, 1) == b'1'
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.5)
        authority.verify_reference(reference, expected_actor=actor.verify(), owner_epoch='epoch')
        os.write(release_write, b'1')
        reply = host._read_activation_frame(client)
        assert reply == {key: value for key, value in {**request, 'status': 'cleaned', 'cleanup': {'host_children': 'absent'}}.items() if key != 'command'}
        assert process.wait(timeout=10) == 0
    finally:
        client.close()
        os.close(ready_read)
        os.close(release_write)
        if process.poll() is None:
            process.kill()
            process.wait()
