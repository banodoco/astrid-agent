"""Q6: the per-job realm reset is skipped only when the finished job left the realm clean."""
from __future__ import annotations

import json

import pytest

from astrid.packs.runpod import pod_reset


@pytest.fixture
def pod(tmp_path, monkeypatch):
    calls = {"replace": 0, "sweep": 0, "ensure": 0}
    template = tmp_path / "realm-template"
    template.mkdir()
    root = tmp_path / "realm"
    root.mkdir()

    def ensure():
        calls["ensure"] += 1
        return {"endpoint": "http://127.0.0.1:1", "realm_id": "realm-1", "credential_file": str(tmp_path / "c")}

    def replace(endpoint, token, *, template, realm_root):
        calls["replace"] += 1
        return {"state": "complete"}

    def sweep(**kwargs):
        calls["sweep"] += 1
        return []

    monkeypatch.setattr(pod_reset, "realm_root", lambda realm_id, data_root: root)
    monkeypatch.setattr(pod_reset, "replace_realm", replace)
    monkeypatch.setattr(pod_reset, "sweep", sweep)
    monkeypatch.setattr(pod_reset, "_owner_token", lambda data_root: "token")
    monkeypatch.setattr(pod_reset, "write_prepared", lambda current, **kw: {"ready": True})
    paths = dict(template=template, lock=tmp_path / "reset.lock", prepared=tmp_path / "prepared.json",
                 ready=tmp_path / "ready.json", active=tmp_path / "active-job.json", data_root=tmp_path,
                 run_dir=tmp_path / "runs" / "r", job_dir=tmp_path / "jobs" / "j")
    return calls, paths, ensure


def _rows(monkeypatch, value):
    def rows(root):
        if isinstance(value, Exception):
            raise value
        return dict(value)
    monkeypatch.setattr(pod_reset, "realm_job_rows", rows)


def test_clean_realm_skips_the_replace_but_still_sweeps_and_readies(pod, monkeypatch):
    calls, paths, ensure = pod
    _rows(monkeypatch, {"projects": 0, "tasks": 0, "runs": 0, "events": 0, "objects": 0, "cas": 0})
    steps = pod_reset.reset(ensure=ensure, **paths)
    assert steps["replace"] == {"state": "skipped", "reason": "realm_clean"}
    assert calls["replace"] == 0 and calls["sweep"] == 1 and steps["ready"] == {"ready": True}


@pytest.mark.parametrize("dirty", [{"tasks": 1}, {"cas": 1}, {"projects": 2}])
def test_any_job_row_or_stored_file_resets_as_before(pod, monkeypatch, dirty):
    calls, paths, ensure = pod
    _rows(monkeypatch, {"projects": 0, "tasks": 0, "runs": 0, "events": 0, "objects": 0, "cas": 0, **dirty})
    steps = pod_reset.reset(ensure=ensure, **paths)
    assert calls["replace"] == 1 and steps["replace"] == {"state": "complete"}


def test_a_count_error_resets_as_before(pod, monkeypatch):
    calls, paths, ensure = pod
    _rows(monkeypatch, pod_reset.ResetError("no realm store"))
    steps = pod_reset.reset(ensure=ensure, **paths)
    assert steps["realm_rows_before"] is None and calls["replace"] == 1


def test_an_empty_count_mapping_resets_as_before(pod, monkeypatch):
    calls, paths, ensure = pod
    _rows(monkeypatch, {})
    pod_reset.reset(ensure=ensure, **paths)
    assert calls["replace"] == 1


def test_the_result_line_reports_the_decision(pod, monkeypatch, capsys):
    calls, paths, ensure = pod
    _rows(monkeypatch, {"tasks": 0, "cas": 0})
    monkeypatch.setattr(pod_reset, "reset", lambda **kw: {"removed": [], "replace": {"state": "skipped"},
                                                          "realm_rows_before": {"tasks": 0, "cas": 0}})
    assert pod_reset.main(["reset"]) == 0
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["realm_reset"] == "skipped" and line["realm_rows_before"] == {"tasks": 0, "cas": 0}
