"""The recorded claim supplies pod job storage facts without another lookup."""
from __future__ import annotations

import json

import pytest

from tests.packs.runpod import test_machine
from tests.packs.runpod.test_generation_preparation import retained as retained  # noqa: F401
from tests.packs.runpod.test_remote_placement import sources as sources  # noqa: F401
from tests.packs.runpod.test_target_selection import case as case  # noqa: F401
from tests.packs.runpod.test_warm_session import (  # noqa: F401
    lease_selection_case as lease_selection_case,
)
from tests.packs.runpod.test_warm_session import (
    small_lease as small_lease,
)

machine_case = test_machine.machine_case
_claim_machine = test_machine._claim_machine

@pytest.mark.parametrize('ephemeral', [False, True])
def test_job_ships_recorded_volume_or_omits_ephemeral(machine_case, monkeypatch, ephemeral):
    c = machine_case
    _claim_machine(c)
    job = c.job()
    lease = c.machine.claim(job)
    record = lease.lease.claim
    record.update(network_volume_id=None if ephemeral else 'recorded-volume',
                  network_volume_size_gb=0 if ephemeral else 10,
                  volume_mount_path='/recorded-mount')
    # start() has no reason to query a provider for this metadata.
    monkeypatch.setattr(c.host, 'observe_liveness', lambda **kw: pytest.fail('provider observation'))
    try:
        c.machine.start(lease, job)
        body = json.loads((job.scratch / 'machine/job.json').read_text())
        if ephemeral:
            assert 'storage_volume' not in body
        else:
            assert body['storage_volume'] == {'id': 'recorded-volume', 'size_gb': 10,
                                               'mount_path': '/recorded-mount'}
    finally:
        c.machine.release(lease, False)
