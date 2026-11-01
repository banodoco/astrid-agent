"""F04 typed preparation through real R02 and bootstrap/install with fakes."""
from __future__ import annotations

import json
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from astrid.core.generation.interfaces import RunPodPreparationRequest
from astrid.packs.runpod.generation import RunPodGenerationPreparer
from astrid.packs.runpod.prepared_task import PreparedWorkerError, PreparedWorkerSelection, _claim_target
from astrid.packs.runpod.remote_placement import Claim
from tests.packs.runpod.test_remote_placement import FakePod, _provider, _wrapper, sources as sources
from tests.packs.runpod.test_target_selection import case as case


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("generation preparation test attempted network access")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def generation(case, sources, tmp_path):
    case.now = datetime.now(timezone.utc)
    case.grant = replace(case.grant, deadline=(case.now + timedelta(hours=1)).isoformat())
    events = []
    provider = _provider(sources)
    placement = _wrapper(provider, sources)
    pod = FakePod({}, provider.marker)
    source, workflow = tmp_path / "source.json", tmp_path / "workflow.py"
    source.write_text('{"nodes": {}}')
    workflow.write_text("# normalized fake workflow")
    request = RunPodPreparationRequest(
        {"engine": "comfyui", "id": "image/fake", "kind": "image"}, source, workflow,
        {"seed": 17}, case.grant.consent, case.grant.max_usd, case.grant.deadline,
    )
    def validate(request):
        events.append("validate")
    def connect(claim):
        events.append("connect")
        return Claim(claim["pod_id"], pod, dict(claim))
    def select(claim):
        events.append("select-worker")
        return PreparedWorkerSelection(case.owner.path, tmp_path / "profile", tmp_path / "staging",
                                       tmp_path / "journal", _claim_target(claim), {}, {})
    preparer = RunPodGenerationPreparer(
        owner=case.owner, placement=placement, validate=validate, connect_claim=connect,
        select_worker=select, consent=case.grant,
    )
    return SimpleNamespace(case=case, preparer=preparer, request=request, events=events,
                           provider=provider, pod=pod)


@pytest.mark.parametrize("entry", [
    {"id": "image/fake", "kind": "image"}, {"id": "video/fake", "kind": "video"},
    {"kind": "workflow"}, {"id": "h3_av", "kind": "video"},
])
def test_ordinary_entry_and_raw_workflow_prepare_exact_claim_without_transform(generation, entry):
    g = generation
    prepared = g.preparer.prepare(replace(g.request, entry={"engine": "comfyui", **entry}))
    assert g.events == ["validate", "connect", "select-worker"]
    assert g.case.creates == 1
    assert g.provider.claims == g.provider.releases == 0
    assert g.pod.installed
    assert all("h3_av.transform" not in command for command in g.pod.job_log)
    request = prepared.execution_request(None)
    assert request["remote_activation_required"] is True
    assert request["target"] == prepared.selection.target
    assert request["target"]["mounts"][0]["target"] == "/workspace"
    saved = json.loads(g.case.owner.path.read_text())
    assert saved["ordinary_preparation"] == prepared.receipt
    assert prepared.receipt["claim_operation_id"] == saved["operation_id"]
    assert prepared.receipt["state"] == "prepared"
    assert prepared.inputs == {"seed": 17}
    assert "inputs" not in prepared.receipt  # CAS remains Runtime's responsibility.


@pytest.mark.parametrize("change", [
    {"entry": {"engine": "wan"}}, {"max_usd": 99}, {"consent": ()},
])
def test_unsupported_engine_or_undisclosed_limits_refuse_before_claim(generation, change):
    g = generation
    with pytest.raises(PreparedWorkerError):
        g.preparer.prepare(replace(g.request, **change))
    assert g.case.creates == 0
    assert not g.case.owner.path.exists()


def test_source_validation_failure_happens_before_claim_or_offer(generation):
    g = generation
    def invalid(request):
        raise ValueError("invalid raw workflow input")
    g.preparer.validate = invalid
    g.case.owner.observe_offer = lambda: pytest.fail("invalid source reached offer observation")
    with pytest.raises(ValueError, match="invalid raw workflow"):
        g.preparer.prepare(g.request)
    assert g.case.creates == 0


def test_ambiguous_install_retains_cleanup_and_cannot_replay(generation):
    g = generation
    g.pod.fail_jobs.add("engines install")
    with pytest.raises(Exception, match="engine install"):
        g.preparer.prepare(g.request)
    saved = json.loads(g.case.owner.path.read_text())
    assert saved["state"] == "cleanup_pending"
    assert saved["ordinary_preparation"]["state"] == "unresolved"
    calls = list(g.pod.job_log)
    with pytest.raises(PreparedWorkerError, match="unresolved"):
        g.preparer.prepare(g.request)
    assert g.case.creates == 1
    assert g.pod.job_log == calls


def test_preparation_expiry_never_admits_worker(generation):
    from datetime import timedelta
    g = generation
    original = g.preparer.placement.prepare_claimed
    def expires(*args, **kwargs):
        result = original(*args, **kwargs)
        g.case.now += timedelta(hours=2)
        return result
    g.preparer.placement.prepare_claimed = expires
    with pytest.raises(PreparedWorkerError, match="paid lifetime"):
        g.preparer.prepare(g.request)
    assert "select-worker" not in g.events
    assert json.loads(g.case.owner.path.read_text())["state"] == "cleanup_pending"


def test_bound_client_hands_normalized_inputs_to_ordinary_prepared_boundary(generation):
    g = generation
    prepared = g.preparer.prepare(g.request)
    calls = []
    selection = replace(prepared.selection, composition_factory=lambda **kwargs: calls.append(kwargs) or object())
    prepared = replace(prepared, selection=selection)
    client = object()
    bound = prepared.bind_client(client, workflow_path=g.request.workflow,
                                 workflow_bundle_path=g.request.workflow.parent)
    assert bound._client is client
    assert calls[0]["workflow_inputs"] == {"seed": 17}
    with pytest.raises(PreparedWorkerError, match="inputs changed"):
        prepared.bind_client(client, workflow_path=g.request.workflow,
                             workflow_bundle_path=g.request.workflow.parent, workflow_inputs={"seed": 18})


def test_missing_sdk_cancel_bridge_retains_custody_and_reports_stop_unknown(generation):
    admission = SimpleNamespace(task_id="task-exact")
    assert generation.preparer.cancel(admission=admission, reason="user") == {
        "task_id": "task-exact", "state": "cancellation_unresolved",
        "custody_retained": True, "observed_stop": False,
    }
