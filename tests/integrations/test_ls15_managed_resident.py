"""LS-15 full native preparation followed by an actually retained host job."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.core.generation import test_engine_custody
from tests.helpers.ls15_counters import Counters
from tests.packs.vibecomfy import test_ls15_resident_host

session = test_engine_custody.session
comfy = test_ls15_resident_host.comfy
resident_comfy = test_ls15_resident_host.resident_comfy

pytestmark = [pytest.mark.integration, pytest.mark.linux_native]


@pytest.fixture
def managed_resident_install(session, monkeypatch, tmp_path):
    root = session[3]
    peer = Path(__file__).resolve().parents[1] / "helpers/ls15_managed_peer.py"
    (root / "tiny_http.py").write_bytes(peer.read_bytes())
    generator = test_engine_custody.cpu_h3_install.__wrapped__(session, monkeypatch, SimpleNamespace(param="managed_native"))
    fixture = next(generator)
    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint._engines_dir", lambda: tmp_path / "roots")
    from astrid.core.generation.engine_local_endpoint import record_engine_root
    record_engine_root("comfyui", str(root))
    monkeypatch.setenv("ASTRID_ENGINE_ROOT", str(root))
    try:
        yield fixture
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


def test_managed_second_job_reuses_audited_generation_without_native_preparation(
    managed_resident_install, resident_comfy, tmp_path, monkeypatch,
):
    from astrid.core.generation.managed_preparation import ManagedEnginePreparation
    host, runtime, peer, _ = resident_comfy
    root = managed_resident_install[3]
    test_ls15_resident_host.canonical_inputs(resident_comfy, tmp_path / "authored-managed")
    preparations, native = [], []
    prepare, command = ManagedEnginePreparation.prepare, ManagedEnginePreparation._command
    def full(self, *args, **kwargs):
        preparations.append(self.attempt)
        return prepare(self, *args, **kwargs)
    def native_command(self, argv):
        native.append(tuple(argv))
        return command(self, argv)
    monkeypatch.setattr(ManagedEnginePreparation, "prepare", full)
    monkeypatch.setattr(ManagedEnginePreparation, "_command", native_command)
    counters = Counters(monkeypatch, tmp_path)
    with counters.observe():
        first = test_ls15_resident_host.run_job(resident_comfy, 1, values={"width": 16, "height": 16, "color": 0}, managed=True, align="align")
        process = host.managed_tool_session.current_adapter.process
        cold = counters.snapshot()
        requests = (root / "requests.jsonl").read_text().splitlines()
        native_before = list(native)
        second = test_ls15_resident_host.run_job(resident_comfy, 2, values={"width": 16, "height": 16, "color": 1}, managed=True, align="align")
        warm = counters.difference(cold, counters.snapshot())
    (tmp_path / "managed-warm-counters.json").write_text(json.dumps({"cold": cold, "warm": warm}, indent=2))
    assert preparations == ["ls15-attempt-1"] and native == native_before and native_before
    assert [argv[0] for argv in native_before] == ["nodes", "prepare", "doctor"]
    assert host.managed_tool_session.current_adapter.process is process
    assert first[0]["process_id"] == second[0]["process_id"] == process.pid
    assert not peer.posts, "managed jobs must use the owned endpoint"
    for scope in ("parent", "child"):
        assert not {key: warm[scope + "_counts"].get(key) for key in counters.zero_categories
                    if warm[scope + "_counts"].get(key)}, warm
    added_requests = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()[len(requests):]]
    assert ["GET", "/object_info"] not in added_requests and ["GET", "/ws"] not in added_requests
    assert second[0]["managed_readiness_reuse"]["content_bytes_verified"] > 0
    for number, result in enumerate((first, second), 1):
        report = result[0]["managed_engine_preparation"]
        assert (report["task_id"], report["attempt_id"]) == (f"ls15-task-{number}", f"ls15-attempt-{number}")
        assert report["placement"] == "managed" and report["health"]["healthy"]
    # A changed closed workflow has a different requirement/source identity.
    # The existing full preparation authority must run again, under a new
    # resident owner, before that job can submit its prompt.
    test_ls15_resident_host.canonical_inputs(resident_comfy, tmp_path / "changed-managed", color=2)
    third = test_ls15_resident_host.run_job(resident_comfy, 3, values={"color": 2}, managed=True, align="align")
    assert preparations == ["ls15-attempt-1", "ls15-attempt-3"]
    assert len(native) > len(native_before) and "managed_readiness_reuse" not in third[0]
    replacement = host.managed_tool_session.current_adapter.process
    assert replacement is not process and process.poll() is not None
    assert third[0]["managed_engine_preparation"]["task_id"] == "ls15-task-3"
    host.shutdown()
    assert replacement.poll() is not None
