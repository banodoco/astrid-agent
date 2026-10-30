"""The retained host must deliver cleanup acknowledgement before it exits."""
import os
import select
import socket
import subprocess
import sys
import textwrap

import pytest

from astrid.core.execution import generic_host as host


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
        client.shutdown(socket.SHUT_WR)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert isinstance(preparation.control_error, BrokenPipeError)
        assert not preparation._control_cleanup_done.is_set()
    finally:
        client.close()
