"""A completed claim loop cannot close the owner's private cleanup ACK."""
import socket
import threading

import pytest

from astrid.core.execution import generic_host as host


@pytest.mark.parametrize('exit_edge', ['signal', 'channel_eof'])
def test_host_only_claim_return_preserves_cleanup_reply_until_owner_exit(monkeypatch, exit_edge):
    preparation = host.LocalExecutionPreparation(operation_id='op', channel_id='ch')
    preparation._request = {'profile': {'execution_mode': 'host_only_cpu'}}
    claim_returned, release_ack, exited = (threading.Event() for _ in range(3))
    parent, child = socket.socketpair()
    parent.settimeout(2)

    def dispatch(request):
        claim_returned.set()  # Actual shutdown wakes the serving loop here.
        assert release_ack.wait(2)
        return {'status': 'cleaned', 'cleanup': {'host_children': 'absent'}}

    monkeypatch.setattr(preparation, 'dispatch', dispatch)

    def serve_claims():
        preparation.run_claim_loop(lambda: claim_returned.wait(2))
        exited.set()

    loop = threading.Thread(target=serve_claims, daemon=True)
    control = preparation.serve_control(child.detach())
    loop.start()
    try:
        host._send_activation_frame(parent, {'command': 'abort_local_execution'})
        assert claim_returned.wait(2)
        assert not exited.wait(.05), 'host exited before its cleanup ACK was sent'
        release_ack.set()
        assert host._read_activation_frame(parent) == {
            'status': 'cleaned', 'cleanup': {'host_children': 'absent'}}
        assert not exited.wait(.05), 'host must retain the original owner final-exit edge'
        if exit_edge == 'signal':
            preparation._host_only_owner_exit.set()
        else:
            parent.close()
        assert exited.wait(2)
    finally:
        release_ack.set()
        parent.close()
        preparation._host_only_owner_exit.set()
        loop.join(2)
        control.join(2)
        assert not loop.is_alive() and not control.is_alive()
