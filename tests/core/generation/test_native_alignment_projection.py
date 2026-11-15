"""Managed receipt projection retains the public trust shape and private evidence."""
import json

import pytest

from astrid.packs.vibecomfy.local_server_engine import _project_native_attempt


@pytest.mark.parametrize('mode', ['trust', 'check'])
def test_prepared_attempt_projects_public_alignment_without_changing_preparation(tmp_path, mode):
    alignment = {'mode': mode, 'status': 'unverified' if mode == 'trust' else 'ready',
                 'verified': mode != 'trust', 'missing': [], 'fetched': []}
    prepared = {'alignment': dict(alignment), 'canonical': 'bound-evidence'}
    path = tmp_path / 'attempt.json'
    path.write_text(json.dumps({'managed_generation_result': {
        'evidence': {'producer': {'attempt': {'record': {}}}},
    }}))
    _project_native_attempt(path, tmp_path, alignment=alignment, prepared=prepared)
    projected = json.loads(path.read_text())
    expected = {key: alignment[key] for key in ('mode', 'status', 'verified')} if mode == 'trust' else alignment
    assert projected['alignment'] == expected
    assert projected['managed_generation_result']['evidence']['producer']['attempt']['record']['alignment'] == expected
    assert projected['managed_engine_preparation'] == prepared
    assert prepared['alignment'] == alignment


def test_host_projection_retains_the_same_public_trust_shape(tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import GenericPackHost
    from tests.core.execution.test_producer_evidence_transport_f06 import _materialize

    spool = _materialize(tmp_path, monkeypatch)
    alignment = {'mode': 'trust', 'status': 'unverified', 'verified': False,
                 'missing': [], 'fetched': []}
    prepared = {'alignment': dict(alignment)}
    host = GenericPackHost.__new__(GenericPackHost)
    host.boot_manifest_provenance = lambda: None
    host._prepare_producer_evidence(spool, {}, preparation=prepared)
    receipt = json.loads((spool / 'manifest.json').read_text())
    assert receipt['managed_generation_result']['evidence']['producer']['alignment'] == {
        'mode': 'trust', 'status': 'unverified', 'verified': False}
    assert prepared['alignment'] == alignment
