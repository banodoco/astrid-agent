import json
import subprocess
import time

import pytest

from scripts import ci_journey as journey
from scripts import ci_journey_turnover as observer


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def read(evidence, action, phase):
    return json.loads((evidence / observer._name(action, phase)).read_text())


def call(evidence, data, phase, reference=None, action="seed"):
    return observer.observe_turnover(action=action, phase=phase, evidence=evidence, data=data, reference=reference)


def test_turnover_phases_exact_id_and_staging(tmp_path):
    data = tmp_path / "run" / "data"
    evidence = tmp_path / "run" / "evidence" / "leg"
    baseline = evidence / "release0-api-seed"
    support = data / "runtime"
    put(support / "generic-host.json", {"runtime_service_launch_id": "a" * 32, "secret": "private-registration"})
    put(support / "pack-host-job-cleanup.json", {"receipts": [{"runtime_service_launch_id": "b" * 32,
        "pid": 123, "job_drained": True, "token": "private-receipt"}]})
    ref = call(baseline, data, "before-stop")
    assert ref == "a" * 32
    assert read(baseline, "seed", "owner-absent")["status"] == "not-reached"
    before = read(baseline, "seed", "before-stop")
    assert before["receipt"]["comparison"] == "no-matching-id-observed"
    put(support / "generic-host.json", {"runtime_service_launch_id": "c" * 32})
    put(support / "pack-host-job-cleanup.json", {"receipts": [{"runtime_service_launch_id": "a" * 32,
        "job_drained": True}]})
    call(baseline, data, "owner-absent", ref)
    call(baseline, data, "after-start-request", ref)
    absent = read(baseline, "seed", "owner-absent")
    assert absent["status"] == "observed"
    assert absent["registration"]["launch_id"] == "c" * 32
    assert absent["reference"]["launch_id"] == "a" * 32
    assert absent["receipt"]["comparison"] == "matching-id-observed"
    assert read(baseline, "seed", "after-start-request")["phase"] == "after-start-request"
    assert "not authentication" in absent["meaning"]
    assert all("private-" not in p.read_text() for p in baseline.iterdir())
    retained = journey.retain_named_evidence(evidence, "before-cleanup")
    assert all(observer._name("seed", p) in retained for p in observer.PHASES)


def test_turnover_unavailable_absent_invalid_and_no_pid_fallback(tmp_path):
    data, evidence = tmp_path / "data", tmp_path / "evidence"
    support = data / "runtime"
    put(support / "generic-host.json", {"pid": 123, "runtime_service_launch_id": "not-a-launch"})
    put(support / "pack-host-job-cleanup.json", {"receipts": [{"pid": 123, "job_drained": True}]})
    assert call(evidence, data, "before-stop") is None
    result = read(evidence, "seed", "before-stop")
    assert result["reference"]["status"] == "unavailable"
    assert result["receipt"]["comparison"] == "unavailable"
    assert result["receipt"]["entries"][0]["matches_reference"] is None
    (support / "pack-host-job-cleanup.json").unlink()
    call(evidence, data, "owner-absent", "a" * 32)
    result = read(evidence, "seed", "owner-absent")
    assert result["receipt"]["status"] == "absent"
    assert result["receipt"]["comparison"] == "unavailable"
    (support / "generic-host.json").write_bytes(b"bad")
    call(evidence, data, "after-start-request", "a" * 32)
    assert read(evidence, "seed", "after-start-request")["registration"]["status"] == "malformed"


def test_turnover_bounds_and_redaction(tmp_path):
    data, evidence = tmp_path / "data", tmp_path / "evidence"
    support = data / "runtime"
    put(support / "generic-host.json", {"runtime_service_launch_id": "a" * 32})
    put(support / "pack-host-job-cleanup.json", {"receipts": [{"runtime_service_launch_id": "b" * 32,
        "private": "credential"} for _ in range(observer.ENTRY_LIMIT + 1)]})
    ref = call(evidence, data, "before-stop")
    result = read(evidence, "seed", "before-stop")
    assert result["receipt"]["entries_truncated"]
    assert result["receipt"]["comparison"] == "incomplete"
    assert len(result["receipt"]["entries"]) == observer.ENTRY_LIMIT
    assert (evidence / observer._name("seed", "before-stop")).stat().st_size < 16 * 1024
    assert "credential" not in (evidence / observer._name("seed", "before-stop")).read_text()
    (support / "pack-host-job-cleanup.json").write_bytes(b"x" * (observer.LIMIT + 1))
    call(evidence, data, "owner-absent", ref)
    assert read(evidence, "seed", "owner-absent")["receipt"]["status"] == "oversized"
    assert read(evidence, "seed", "owner-absent")["receipt"]["comparison"] == "unavailable"


def test_turnover_failure_isolation_and_error_record(tmp_path, monkeypatch):
    original = observer._run
    calls = []
    def fail_once(request):
        calls.append(request)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("worker", observer.TIMEOUT)
        return original(request)
    monkeypatch.setattr(observer, "_run", fail_once)
    assert call(tmp_path / "evidence", tmp_path / "data", "owner-absent", "a" * 32) is None
    assert read(tmp_path / "evidence", "seed", "owner-absent")["status"] == "error"
    assert calls[1]["error"] == "observer-failed"
    monkeypatch.setattr(observer, "_run", lambda request: (_ for _ in ()).throw(OSError("private")))
    assert call(tmp_path / "evidence", tmp_path / "data", "after-start-request", "a" * 32) is None
    assert observer.observe_turnover(action="../../bad", phase="before-stop", evidence=tmp_path,
                                     data=tmp_path, reference=None) is None


def test_turnover_real_worker_timeout_is_bounded(tmp_path, monkeypatch):
    slow = tmp_path / "slow_worker.py"
    slow.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
    monkeypatch.setattr(observer, "__file__", str(slow))
    monkeypatch.setattr(observer, "TIMEOUT", 0.15)
    start = time.monotonic()
    assert call(tmp_path / "evidence", tmp_path / "data", "before-stop") is None
    assert time.monotonic() - start < 3


def test_turnover_unsafe_leaf_is_unavailable(tmp_path):
    data, evidence = tmp_path / "data", tmp_path / "evidence"
    support = data / "runtime"
    support.mkdir(parents=True)
    (support / "generic-host.json").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text('{"runtime_service_launch_id": "' + "a" * 32 + '"}')
    assert call(evidence, data, "before-stop") is None
    result = read(evidence, "seed", "before-stop")
    assert result["registration"]["status"] == "unsafe"
    assert result["reference"]["status"] == "unavailable"
