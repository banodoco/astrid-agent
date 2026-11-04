"""Receipt persistence at the genuine normal R0 selection boundary."""
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import journey_version_jumps as jumps


@pytest.mark.parametrize("stop, expected, error", [
    ("before", "not-run", None),
    ("installation", "fail", "RuntimeError: selection unavailable"),
    ("owner", "fail", "RuntimeError: Expected exactly the selected Runtime owner 42, found [42, 43]"),
    ("after", "pass", None),
])
def test_normal_r0_selection_survives_failure_and_staging(tmp_path, monkeypatch, stop, expected, error):
    run = tmp_path / "run"
    root, data = run / "program", run / "data"
    evidence = run / "evidence" / "release0-to-release1"
    app = tmp_path / "app"
    app.mkdir()
    manifest0 = tmp_path / "release0.json"
    candidate = tmp_path / "candidate.json"
    journey.write_json(manifest0, {"release": 0})
    journey.write_json(candidate, {"manifest_sha256": "hash", "artifacts": {}})
    manifest1 = {"release": 1, "agent": {}, "runtime": {}}
    args = SimpleNamespace(job_id="direct-windows", mode="direct", leg="release0-to-release1",
                           shell="powershell.exe", app_checkout=app, candidate=candidate)
    monkeypatch.setattr(jumps, "release0_admission", lambda _: (tmp_path, manifest0, {}))
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **kw: (candidate, manifest1))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: manifest1)
    monkeypatch.setattr(journey, "git_source", lambda _: {"sha": "app"})
    monkeypatch.setattr(journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *_: None)
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *_: None)

    def wait(_):
        if stop == "before":
            raise RuntimeError("not ready")
    monkeypatch.setattr(journey, "wait_ready", wait)

    def installation(*_):
        if stop == "installation":
            raise RuntimeError("selection unavailable")
        return tmp_path / "version.json", data, {"release": 0}
    monkeypatch.setattr(journey, "installation", installation)
    monkeypatch.setattr(jumps, "installed_release", lambda _: 0)
    monkeypatch.setattr(journey, "selected_tuple", lambda *_: {})
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *_: {})

    def owner(*_):
        if stop == "owner":
            raise RuntimeError("Expected exactly the selected Runtime owner 42, found [42, 43]")
    monkeypatch.setattr(journey, "assert_release0_runtime", owner)

    def later(*_args, **_kwargs):
        # The pass is checkpointed before any later stage can fail.
        assert journey.read_json(evidence / "journey-result.json")["release0_selection"] == {
            "result": "pass", "error": None}
        raise RuntimeError("later failure")
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", later)

    with pytest.raises(RuntimeError):
        jumps.execute_release0_to_release1(args, root, data, evidence, SimpleNamespace(pid=9), "birth")
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *_: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *_: None)
    for phase in ("before-cleanup", "after-cleanup"):
        journey.snapshot_failure_evidence(evidence, run, data, phase)
        receipt = journey.read_json(run / "evidence-upload" / phase / "journey-result.json")
        assert receipt["release0_selection"] == {"result": expected, "error": error}
        assert receipt["status"] == "fail"
        assert set(receipt["checks"].values()) == {"not-run"}


@pytest.fixture
def complete_journey(tmp_path, monkeypatch):
    """A portable harness double that traverses the complete normal control flow."""
    run = tmp_path / "run"
    root, data = run / "program", run / "data"
    evidence = run / "evidence" / "release0-to-release1"
    app = tmp_path / "app"
    app.mkdir()
    manifest0, candidate = tmp_path / "r0.json", tmp_path / "candidate.json"
    journey.write_json(manifest0, {"release": 0})
    journey.write_json(candidate, {"manifest_sha256": "hash", "artifacts": {}})
    manifest1 = {"release": 1, "agent": {}, "runtime": {}}
    args = SimpleNamespace(job_id="direct-windows", mode="direct", leg="release0-to-release1",
                           shell="powershell.exe", app_checkout=app, candidate=candidate)
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old, new = {"release": 0}, {"release": 1}
    stages = []
    monkeypatch.setattr(jumps, "release0_admission", lambda _: (tmp_path, manifest0, {}))
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **kw: (candidate, manifest1))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: manifest1)
    monkeypatch.setattr(journey, "git_source", lambda _: {"sha": "app"})
    monkeypatch.setattr(journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *_: None)
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *_: None)
    monkeypatch.setattr(journey, "wait_ready", lambda *_: None)
    installations = iter([(old_path, data, old), (new_path, data, new)])
    monkeypatch.setattr(journey, "installation", lambda *_: next(installations))
    monkeypatch.setattr(jumps, "installed_release", lambda version: version["release"])
    monkeypatch.setattr(journey, "selected_tuple", lambda *_: {})
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *_: {})
    monkeypatch.setattr(journey, "assert_release0_runtime", lambda *_: stages.append("selection"))
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions",
                        lambda *a, **kw: stages.append("preconditions"))
    monkeypatch.setattr(journey, "verify", lambda *a, **kw: None)
    monkeypatch.setattr(jumps, "direct_workspace", lambda *a, **kw: {"task_id": "t"})
    monkeypatch.setattr(jumps, "task_after_update", lambda *a, **kw: "t")
    health = iter([{"pid": 42, "runtime_instance_id": "old"},
                   {"pid": 43, "runtime_instance_id": "new"}])
    monkeypatch.setattr(journey, "healthy", lambda *_: next(health))
    births = iter(["old-birth", None])
    monkeypatch.setattr(journey, "process_birth", lambda *_: next(births))
    monkeypatch.setattr(journey, "bootstrap", lambda *a, **kw: stages.append("update"))
    monkeypatch.setattr(jumps, "selected_candidate", lambda *_: {})
    monkeypatch.setattr(jumps, "assert_preserved", lambda *_: None)
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *_: stages.append("settled"))
    real_read_json = journey.read_json
    monkeypatch.setattr(journey, "read_json", lambda path: {"details": {}} if path.name == "verify-install.json"
                        else real_read_json(path))
    def execute():
        return jumps.execute_release0_to_release1(args, root, data, evidence, SimpleNamespace(pid=9), "birth")
    return SimpleNamespace(execute=execute, run=run, data=data, evidence=evidence, stages=stages)


@pytest.mark.parametrize("fault", ["partial-write", "replace"])
def test_failed_checkpoint_preserves_receipt_and_later_exception(
        complete_journey, monkeypatch, capsys, fault):
    ctx = complete_journey
    receipt = ctx.evidence / "journey-result.json"
    later_error = RuntimeError("later failure")
    real_dump, real_replace = jumps.json.dump, jumps.os.replace
    def partial_dump(value, stream, **kwargs):
        assert journey.read_json(receipt)["release0_selection"]["result"] == "not-run"
        stream.write('{"partial":')
        raise OSError("checkpoint " + "x" * 500)
    def failed_replace(source, destination):
        assert destination == receipt
        assert journey.read_json(receipt)["release0_selection"]["result"] == "not-run"
        raise OSError("replace failed")
    if fault == "partial-write":
        monkeypatch.setattr(jumps.json, "dump", partial_dump)
    else:
        monkeypatch.setattr(jumps.os, "replace", failed_replace)
    def later(*_args, **_kwargs):
        assert journey.read_json(receipt)["release0_selection"]["result"] == "not-run"
        ctx.stages.append("later reached")
        raise later_error
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", later)
    with pytest.raises(RuntimeError) as caught:
        ctx.execute()
    assert caught.value is later_error
    assert ctx.stages == ["selection", "later reached"]
    recorded = journey.read_json(receipt)
    assert recorded["release0_selection"] == {"result": "pass", "error": None}
    assert recorded["status"] == "fail" and recorded["error"] == "RuntimeError: later failure"
    assert not list(ctx.evidence.glob(".journey-result-*.tmp"))
    diagnostic = capsys.readouterr().err
    assert "Release-0 selection checkpoint failed: OSError:" in diagnostic
    assert len(diagnostic) < 400
    monkeypatch.setattr(jumps.json, "dump", real_dump)
    monkeypatch.setattr(jumps.os, "replace", real_replace)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *_: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *_: None)
    for phase in ("before-cleanup", "after-cleanup"):
        journey.snapshot_failure_evidence(ctx.evidence, ctx.run, ctx.data, phase)
        staged = journey.read_json(ctx.run / "evidence-upload" / phase / "journey-result.json")
        assert staged["release0_selection"] == {"result": "pass", "error": None}
        assert staged["error"] == "RuntimeError: later failure"


@pytest.mark.parametrize("checkpoint_fails", [False, True])
def test_full_journey_completion(complete_journey, monkeypatch, checkpoint_fails):
    ctx = complete_journey
    if checkpoint_fails:
        monkeypatch.setattr(jumps.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))
    def preconditions(*_args, **_kwargs):
        previous = journey.read_json(ctx.evidence / "journey-result.json")
        assert previous["release0_selection"]["result"] == ("not-run" if checkpoint_fails else "pass")
        ctx.stages.append("preconditions")
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", preconditions)
    result = ctx.execute()
    assert ctx.stages == ["selection", "preconditions", "update", "settled"]
    assert result["status"] == "pass"
    assert all(value == "pass" for value in result["checks"].values())
    assert result["release0_selection"] == {"result": "pass", "error": None}
    assert journey.read_json(ctx.evidence / "journey-result.json") == result
    assert not list(ctx.evidence.glob(".journey-result-*.tmp"))


def test_selection_exception_identity_and_receipt(complete_journey, monkeypatch):
    ctx = complete_journey
    original = RuntimeError("owner failure")
    def fail(*_):
        raise original
    monkeypatch.setattr(journey, "assert_release0_runtime", fail)
    with pytest.raises(RuntimeError) as caught:
        ctx.execute()
    assert caught.value is original
    recorded = journey.read_json(ctx.evidence / "journey-result.json")
    assert recorded["release0_selection"] == {"result": "fail", "error": "RuntimeError: owner failure"}
    assert recorded["status"] == "fail"
    assert ctx.stages == []


def test_checkpoint_diagnostic_failure_is_contained(complete_journey, monkeypatch):
    ctx = complete_journey
    class BrokenStderr:
        def write(self, _value):
            raise OSError("stderr unavailable")
        def flush(self):
            raise OSError("stderr unavailable")
    monkeypatch.setattr(jumps.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))
    monkeypatch.setattr(jumps.sys, "stderr", BrokenStderr())
    assert ctx.execute()["status"] == "pass"
    assert "settled" in ctx.stages
