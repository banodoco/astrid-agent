import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import check_journey_evidence as gate
from scripts import windows_update_journey as update


def receipt():
    return {"outcome": "updated", "from_release": 1, "to_release": 2,
            "marker": update.MARKER, "manifest_sha256": "a" * 64,
            "old_version": "old", "current_version": "new", "exit_code": 0, "refusal": None, "task_id": "task-1",
            "task_labels": [{"name": "Astrid Runtime", "path": "\\"}], "service_names": [],
            "checks": {key: "pass" for key in ("running_before", "owned_task", "healthy", "same_labels",
                       "no_r0_handoff", "workspace", "sentinel", "prior_version", "verify_update", "task_settled")}}


def test_standin_preserves_pins_and_canonical_hash():
    source = {"release": 1, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40},
              "tools": {"python": {"version": "3.12"}}, "vibecomfy": {"sha": "c" * 40}}
    prior = copy.deepcopy(source)
    standin = update.make_standin_release(source)
    assert source == prior
    assert {k: standin[k] for k in ("agent", "runtime", "tools", "vibecomfy")} == {k: source[k] for k in ("agent", "runtime", "tools", "vibecomfy")}
    assert standin["release"] == 2 and standin["qualification_marker"] == update.MARKER
    raw = update.canonical_manifest_bytes(standin)
    assert raw == (json.dumps(standin, indent=2, sort_keys=True) + "\n").encode()
    assert len(hashlib.sha256(raw).hexdigest()) == 64


@pytest.mark.parametrize("output,expected", [("File is locked.\n", "File is locked."),
    ("File is in use.\nFile is locked.\n", None), ("Traceback\nFile is locked.\n", None),
    ("Access denied.\n", None), ("File is locked. Retry.\n", None)])
def test_refusal_must_be_one_clear_lock_sentence(output, expected):
    assert update.refusal_sentence(output) == expected


def test_gate_requires_update_and_both_outcome_contracts():
    assert gate._windows_fresh_update_errors(None, "direct-windows")
    good = receipt()
    assert not gate._windows_fresh_update_errors(good, "direct-windows")
    refused = copy.deepcopy(good)
    refused.update(outcome="refused", exit_code=1, current_version="old", refusal="File is locked.")
    refused["checks"].pop("verify_update")
    refused["checks"].pop("task_settled")
    assert not gate._windows_fresh_update_errors(refused, "direct-windows")
    for field, value in (("current_version", "new"), ("refusal", "Access denied."), ("exit_code", 0)):
        bad = copy.deepcopy(refused)
        bad[field] = value
        assert gate._windows_fresh_update_errors(bad, "direct-windows")
    for field in ("verify_update", "task_settled", "same_labels", "no_r0_handoff", "workspace", "sentinel"):
        bad = copy.deepcopy(good)
        bad["checks"].pop(field)
        assert gate._windows_fresh_update_errors(bad, "direct-windows")


def test_public_line_has_local_override_and_candidate_binding(tmp_path, monkeypatch):
    app = tmp_path / "app"
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    observed = {}
    def run(argv, **kwargs):
        observed.update(argv=argv, **kwargs)
        kwargs["stdout"].write("ok\n")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(update.subprocess, "run", run)
    code, output = update.public_update(app, "powershell.exe", manifest, tmp_path / "root", tmp_path / "data", tmp_path)
    assert code == 0 and output == "ok\n"
    assert observed["env"]["ASTRID_INSTALL_MANIFEST_URL"] == manifest.as_uri()
    assert observed["env"]["ASTRID_CI_EXPECTED_BOOTSTRAP"] == str(app / "public" / "install.ps1")
    assert "irm https://astrid.haus/install.ps1" in observed["argv"][-1]
    assert "--update" in observed["argv"][-1]


def test_update_success_and_refusal_exercise_assertions(tmp_path, monkeypatch):
    root = tmp_path / "program"
    data = tmp_path / "data"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    old = root / "versions" / "old" / "version.json"
    old.parent.mkdir(parents=True)
    old.write_text("{}")
    new = root / "versions" / "new" / "version.json"
    marker = tmp_path / "marker"
    candidate = {"release": 1, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}, "tools": {}}
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(update, "task_labels", lambda: [("\\", "Astrid Runtime")])
    monkeypatch.setattr(update, "service_names", lambda: [])
    monkeypatch.setattr(update, "assert_running_task", lambda: None)
    monkeypatch.setattr(update, "assert_no_boundary", lambda *args: None)
    monkeypatch.setattr(update.windows, "wait_for", lambda *a, **kw: None)
    calls = []
    monkeypatch.setattr(update.windows, "task", lambda *a, **kw: (calls.append("task") or "task-1"))
    current = [old]
    def verify(mode, *args, **kwargs):
        calls.append("verify-" + mode)
        path = evidence / f"verify-{mode}.json"
        path.write_text('{"passed": true}')
        return path
    journey = SimpleNamespace(installation=lambda *a: (current[0], data, {"root": str(current[0].parent),
        "identity": {"release": 1 if current[0] == old else 2, "managed_by": None}}),
        healthy=lambda *a: calls.append("healthy"), assert_direct_service_ownership=lambda *a, **kw: calls.append("owner"),
        verify=verify, wait_ready=lambda *a, **kw: calls.append("ready"), assert_content=lambda *a: calls.append("content"),
        sentinel_observation=lambda *a: {"alive": True}, read_json=lambda p: json.loads(p.read_text()))
    def success(app, shell, path, root, data, evidence):
        new.parent.mkdir()
        new.write_text("{}")
        (new.parent / "manifest.json").write_bytes(path.read_bytes())
        current[0] = new
        return 0, "ok"
    monkeypatch.setattr(update, "public_update", success)
    args = SimpleNamespace(shell="powershell.exe")
    result = update.run_update(journey, args, tmp_path, candidate, root, data, evidence, None, "birth", "project", marker, "digest")
    assert result["outcome"] == "updated" and not gate._windows_fresh_update_errors(result, "direct-windows")
    assert {"verify-observe", "verify-update", "task", "content", "ready"} <= set(calls)
    current[0] = old
    calls.clear()
    monkeypatch.setattr(update, "public_update", lambda *a: (1, "File is locked."))
    result = update.run_update(journey, args, tmp_path, candidate, root, data, evidence, None, "birth", "project", marker, "digest")
    assert result["outcome"] == "refused" and not gate._windows_fresh_update_errors(result, "direct-windows")
    assert "verify-update" not in calls and "task" not in calls
    monkeypatch.setattr(update, "public_update", lambda *a: (1, "Access denied."))
    with pytest.raises(RuntimeError, match="without a clean lock refusal"):
        update.run_update(journey, args, tmp_path, candidate, root, data, evidence, None, "birth", "project", marker, "digest")
