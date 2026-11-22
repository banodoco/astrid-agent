"""HM2 R12: preparation, activation retention and cleanup keep one owner.

The graph is a bounded in-memory actor. Native process, activation-channel and
role-custody acceptance remain the separate Plan 38 proofs.
"""

from __future__ import annotations

import copy
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import HostError, LocalExecutionPreparation
from tests.test_generic_host import _local_custody_request


def _grant():
    return {
        "version": generic_host._ACTIVATION_VERSION,
        "operation_id": "runtime-op", "channel_id": "private-channel",
        "executor_incarnation": "hm2-host-incarnation",
        "evidence_digest": "sha256:" + "a" * 64,
        "receipt": {"owner": "runtime-A"},
    }


def test_r12_activation_retention_snapshots_input_and_rejects_a_changed_replay():
    """R12: caller mutation and a later grant cannot rewrite accepted activation."""
    preparation = LocalExecutionPreparation(operation_id="runtime-op", channel_id="private-channel")
    grant = _grant()
    expected = copy.deepcopy(grant)
    preparation.retain_activation_grant(grant)
    grant["receipt"]["owner"] = "caller-mutated"
    assert preparation._activation_grant == expected
    preparation.retain_activation_grant(copy.deepcopy(expected))
    with pytest.raises(HostError, match="retained activation grant changed"):
        preparation.retain_activation_grant({**expected, "executor_incarnation": "replacement"})
    assert preparation._activation_grant == expected


@pytest.mark.parametrize("field,value", [
    ("version", "foreign/v1"), ("operation_id", "foreign-operation"),
    ("channel_id", "foreign-channel"), ("executor_incarnation", 7),
    ("evidence_digest", "sha256:invalid"),
])
def test_r12_activation_retention_refuses_foreign_or_malformed_identity(field, value):
    """R12: a rejected activation leaves no retained handoff grant."""
    preparation = LocalExecutionPreparation(operation_id="runtime-op", channel_id="private-channel")
    with pytest.raises(HostError, match="accepted activation grant cannot be retained"):
        preparation.retain_activation_grant({**_grant(), field: value})
    assert preparation._activation_grant is None


def test_r12_abort_during_preparation_waits_for_original_graph_then_cleans_once(tmp_path, monkeypatch):
    """R12/R13: abort cannot abandon the graph while preparation still owns it."""
    entered, release, abort_waiting = threading.Event(), threading.Event(), threading.Event()
    calls = []
    capabilities = {"engine": {"owner": "hm2-original"}}

    class Graph:
        def __init__(self, *, request):
            calls.append("constructed")

        def prepare(self):
            entered.set()
            assert release.wait(5), "test never released the original preparation"
            calls.append("prepared")
            return {"custody_capabilities": capabilities}

        def abort(self):
            calls.append("cleaned")

    request = _local_custody_request(tmp_path)
    preparation = LocalExecutionPreparation(
        operation_id="runtime-op", channel_id="private-channel", graph_factory=Graph,
    )
    wait = preparation._prepare_done.wait

    def observed_wait(timeout=None):
        abort_waiting.set()
        return wait(timeout)

    monkeypatch.setattr(preparation._prepare_done, "wait", observed_wait)
    assert preparation.known_custody_capabilities() == {}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            preparing = pool.submit(preparation.prepare_local_execution, request)
            try:
                assert entered.wait(5)
                graph = preparation.graph
                aborting = pool.submit(preparation.abort)
                assert abort_waiting.wait(5)
                assert not aborting.done()
                assert preparation.graph is graph
                assert calls == ["constructed"]
            finally:
                release.set()
            assert preparing.result(timeout=5)["status"] == "prepared"
            assert aborting.result(timeout=5) is None
        assert calls == ["constructed", "prepared", "cleaned"]
        assert preparation.graph is graph
        assert preparation._aborted is True
        assert preparation.known_custody_capabilities() == capabilities
        returned = preparation.known_custody_capabilities()
        returned["engine"] = {"owner": "caller replacement"}
        assert preparation.known_custody_capabilities() == capabilities
        preparation.abort()
        assert calls == ["constructed", "prepared", "cleaned"]
        with pytest.raises(HostError, match="has been aborted"):
            preparation.prepare_local_execution(request)
    finally:
        release.set()
        descriptor, preparation._handoff_lock_fd = preparation._handoff_lock_fd, None
        if descriptor is not None:
            generic_host.platform.unlock_file(descriptor)
            os.close(descriptor)
