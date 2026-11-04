"""Synthetic checks for the existing C1 busy-refusal harness, not R0 proof."""
from types import SimpleNamespace

import pytest

from scripts import journey_version_jumps as jumps


@pytest.fixture
def busy_case(tmp_path, monkeypatch):
    root, data, evidence = tmp_path / "program", tmp_path / "data", tmp_path / "evidence"
    version = root / "versions" / "old"
    version.mkdir(parents=True)
    (data / "runtime").mkdir(parents=True)
    evidence.joinpath("busy").mkdir(parents=True)
    protected = (root / "current", version / "version.json", data / "runtime/discovery.json")
    for path in protected:
        path.write_bytes(b"original")
    evidence.joinpath("busy/update-bootstrap.log").write_text("Runtime BUSY: task-123")
    calls = []
    keys = {"task_id": "task-123"}

    def workspace(_old, _data, _evidence, action, request):
        calls.append(action)
        if action == "busy":
            return keys
        assert action == "release" and request == keys
        return {}

    def refusal(*_args):
        raise RuntimeError("bootstrap exited nonzero")

    monkeypatch.setattr(jumps, "workspace", workspace)
    monkeypatch.setattr(jumps.journey, "bootstrap", refusal)
    monkeypatch.setattr(jumps.journey, "process_birth", lambda _pid: "birth")
    monkeypatch.setattr(jumps.journey, "sentinel_alive", lambda *_: True)
    monkeypatch.setattr(jumps.journey, "assert_one_runtime", lambda _data: calls.append("one_runtime"))
    args = SimpleNamespace(app_checkout=tmp_path / "app", shell="unused")
    result = {"checks": {}}

    def run():
        jumps.busy_refusal(args, root, data, evidence, tmp_path / "manifest.json",
                           {"root": str(version)}, 123, "birth", object(), "sentinel-birth", result)

    return SimpleNamespace(run=run, calls=calls, result=result, protected=protected,
                           log=evidence / "busy/update-bootstrap.log")


def test_busy_refusal_accepts_task_specific_failure_and_releases(busy_case):
    busy_case.run()
    assert busy_case.result["checks"] == {"busy_refusal": "pass", "one_runtime": "pass",
                                         "unrelated_sentinel": "pass"}
    assert busy_case.calls == ["busy", "one_runtime", "release"]


@pytest.mark.parametrize("message", ["busy: another task", "task-123 failed"])
def test_busy_refusal_requires_both_busy_and_task_id(busy_case, message):
    busy_case.log.write_text(message)
    with pytest.raises(RuntimeError, match="task-specific guidance"):
        busy_case.run()
    assert busy_case.calls == ["busy", "release"]
    assert busy_case.result["checks"] == {}


def test_busy_refusal_rejects_success(busy_case, monkeypatch):
    monkeypatch.setattr(jumps.journey, "bootstrap", lambda *_: None)
    with pytest.raises(RuntimeError, match="updated a busy"):
        busy_case.run()
    assert busy_case.calls == ["busy", "release"]
    assert busy_case.result["checks"] == {}


def test_busy_refusal_releases_work_if_baseline_read_fails(busy_case, monkeypatch):
    busy_case.protected[0].unlink()
    monkeypatch.setattr(jumps.journey, "bootstrap", lambda *_: pytest.fail("must not attempt update"))
    with pytest.raises(FileNotFoundError):
        busy_case.run()
    assert busy_case.calls == ["busy", "release"]
    assert busy_case.result["checks"] == {}


@pytest.mark.parametrize("index", range(3), ids=["current", "version", "discovery"])
def test_busy_refusal_rejects_protected_file_changes(busy_case, monkeypatch, index):
    def mutate_then_refuse(*_args):
        busy_case.protected[index].write_bytes(b"changed")
        raise RuntimeError("bootstrap exited nonzero")

    monkeypatch.setattr(jumps.journey, "bootstrap", mutate_then_refuse)
    with pytest.raises(RuntimeError, match="mutated selection/version/discovery"):
        busy_case.run()
    assert busy_case.calls == ["busy", "release"]
    assert busy_case.result["checks"] == {}


def test_busy_refusal_rejects_replaced_owner_and_releases(busy_case, monkeypatch):
    monkeypatch.setattr(jumps.journey, "process_birth", lambda _pid: "different")
    with pytest.raises(RuntimeError, match="exited or was replaced"):
        busy_case.run()
    assert busy_case.calls == ["busy", "release"]
    assert busy_case.result["checks"] == {}


def test_busy_refusal_rejects_lost_sentinel_and_releases(busy_case, monkeypatch):
    monkeypatch.setattr(jumps.journey, "sentinel_alive", lambda *_: False)
    with pytest.raises(RuntimeError, match="disturbed the unrelated sentinel"):
        busy_case.run()
    assert busy_case.calls == ["busy", "release"]
    assert busy_case.result["checks"] == {}
