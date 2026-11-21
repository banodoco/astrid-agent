"""FIX-11: cleanup is terminal until the activation has a claim owner."""

import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

from astrid.core.execution import generic_host
from tests.core.execution.test_generic_host_activation import _grant


@pytest.mark.parametrize("retained", [False, True])
def test_cleanup_prevents_initial_or_replayed_activation(retained):
    preparation = generic_host.LocalExecutionPreparation(
        operation_id="operation-1", channel_id="channel-1",
    )
    grant = _grant()
    if retained:
        preparation.retain_activation_grant(grant)
    preparation.abort()

    with pytest.raises(generic_host.HostError, match="aborted"):
        preparation.retain_activation_grant(grant)

    assert preparation._aborted is True
    assert preparation._activation_grant == (grant if retained else None)
    assert preparation._claim_host is None


def test_cleanup_prevents_later_claim_owner_attachment():
    preparation = generic_host.LocalExecutionPreparation(
        operation_id="operation-1", channel_id="channel-1",
    )
    preparation.retain_activation_grant(_grant())
    preparation.abort()
    host = generic_host.GenericPackHost(pack_roots=[])
    try:
        with pytest.raises(generic_host.HostError, match="aborted"):
            preparation.attach_claim_host(host)
        assert preparation._claim_host is None
    finally:
        host.shutdown()


def test_cleanup_cannot_miss_a_concurrent_claim_owner_attachment():
    """Abort's no-host observation and final state must fence attachment."""
    preparation = generic_host.LocalExecutionPreparation(
        operation_id="operation-1", channel_id="channel-1",
    )
    cleanup_entered, release_cleanup = threading.Event(), threading.Event()
    attach_entered = threading.Event()

    class Graph:
        def abort(self):
            cleanup_entered.set()
            assert release_cleanup.wait(5), "cleanup was never released"

    preparation.graph = Graph()
    host = generic_host.GenericPackHost(pack_roots=[])

    def attach():
        attach_entered.set()
        preparation.attach_claim_host(host)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleanup = pool.submit(preparation.abort)
            try:
                assert cleanup_entered.wait(5)
                attachment = pool.submit(attach)
                assert attach_entered.wait(5)
                with pytest.raises(TimeoutError):
                    attachment.result(timeout=0.1)
            finally:
                release_cleanup.set()
            cleanup.result(timeout=5)
            with pytest.raises(generic_host.HostError, match="aborted"):
                attachment.result(timeout=5)
        assert preparation._aborted is True
        assert preparation._claim_host is None
    finally:
        release_cleanup.set()
        host.shutdown()
