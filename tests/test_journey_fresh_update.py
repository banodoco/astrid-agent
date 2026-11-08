import hashlib
import json

import pytest

from scripts import journey_fresh_update as update
from scripts import check_journey_evidence as gate


HOSTED = {"route": "hosted-button", "request": {"method": "POST", "path": "/api/runtime/v1/update",
          "origin": "https://astrid.haus", "content_length": 0, "authorization": False, "cookie": False,
          "status": 202, "operation_id": "a" * 32},
          "served": {"/install-manifest.json": 1, "/install": 1, "/install.ps1": 0},
          "final": {"state": "terminal", "outcome": "updated", "target_release": 2}, "intercept_reverted": True}


def test_standin_preserves_pins_and_canonical_bytes(tmp_path):
    candidate = {"release": 1, "agent": {"sha": "a"}, "runtime": {"sha": "b"},
                 "tools": {"uv": {"version": "1"}}}
    path, manifest = update.standin(candidate, {"vibecomfy": {"sha": "v"}}, tmp_path)
    assert manifest["release"] == 2
    assert all(manifest[key] == candidate[key] for key in ("agent", "runtime", "tools"))
    assert manifest["vibecomfy"] == {"sha": "v"}
    assert path.read_bytes() == (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    assert (tmp_path / "SHA256SUMS").read_text() == f"{hashlib.sha256(path.read_bytes()).hexdigest()}  install-manifest.json\n"


def transition(tmp_path):
    root = tmp_path / "program"
    old = root / "versions/old/version.json"
    new = root / "versions/new/version.json"
    for path in (old, new):
        path.parent.mkdir(parents=True)
        path.write_text("{}")
    (root / "current").write_text("new\n")
    service = {"manager": "systemd-user", "service_path": "/service/astrid.service", "foreground_fallback": False}
    boundary = {"journals": [], "r1_data_root": None, "selection_r1_data_root": None, "r1_data_exists": False}
    return [root, old, new, {"old"}, service, service.copy(), ["astrid.service"],
            ["astrid.service"], boundary, boundary.copy()]


def test_transition_requires_new_current_old_retained_same_label_and_no_boundary(tmp_path):
    args = transition(tmp_path)
    assert update.assert_transition(*args)["new_version"] == "new"
    for index, value in ((3, {"old", "new"}), (5, {"service_path": "other"}),
                         (7, ["astrid.service", "astrid-r1.service"]),
                         (9, {"journals": ["astrid-r1-handoff.json"], "r1_data_root": None, "selection_r1_data_root": None, "r1_data_exists": False})):
        changed = args.copy()
        changed[index] = value
        with pytest.raises(RuntimeError):
            update.assert_transition(*changed)
    (args[0] / "current").write_text("old")
    with pytest.raises(RuntimeError):
        update.assert_transition(*args)
    (args[0] / "current").write_text("new")
    args[1].unlink()
    with pytest.raises(RuntimeError):
        update.assert_transition(*args)


def test_gate_requires_update_and_every_assertion():
    checks = {name: "pass" for name in update.CHECKS}
    receipt = {"status": "pass", "release_from": 1, "release_to": 2, "hosted": HOSTED, "old_version": "old",
               "new_version": "new", "manifest_sha256": "a" * 64, "checks": checks,
               "service": {"service_path": "/service/astrid.service", "foreground_fallback": False},
               "service_names": ["astrid.service"]}
    assert gate.fresh_update_errors("direct-linux-fresh", None)
    assert not gate.fresh_update_errors("direct-linux-fresh", receipt)
    for key in checks:
        checks[key] = "not-run"
        assert gate.fresh_update_errors("direct-linux-fresh", receipt)
        checks[key] = "pass"
    receipt["new_version"] = "old"
    assert gate.fresh_update_errors("direct-linux-fresh", receipt)


def test_standin_follows_the_candidate_release_not_a_fixed_one(tmp_path):
    candidate = {"release": 7, "agent": {"sha": "a"}, "runtime": {"sha": "b"}, "tools": {}}
    _, manifest = update.standin(candidate, {"vibecomfy": {"sha": "v"}}, tmp_path)
    assert manifest["release"] == 8
    for bad in ({}, {"release": True}, {"release": -1}, {"release": "1"}):
        with pytest.raises(RuntimeError, match="integer release"):
            update.standin(bad, {"vibecomfy": {"sha": "v"}}, tmp_path)


@pytest.mark.parametrize("baseline", [1, 7])
def test_execute_posts_the_hosted_update_and_checks_post_update(monkeypatch, tmp_path, baseline):
    from types import SimpleNamespace

    root, old_path, new_path, versions, service, _, names, _, boundary, _ = transition(tmp_path)
    data = tmp_path / "data"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    installed = {"release": baseline, "agent": {"sha": "a"}, "runtime": {"sha": "b"},
                 "tools": {}, "vibecomfy": {"sha": "v"}}
    (old_path.parent / "manifest.json").write_text(json.dumps(installed))
    old = {"root": str(old_path.parent), "python": "/installed/python",
           "identity": {"release": baseline, "managed_by": None}}
    new = {"root": str(new_path.parent), "identity": {"release": baseline + 1, "managed_by": None}}
    # Model a new version being created by the update worker, not before it.
    import shutil
    shutil.rmtree(new_path.parent)
    calls = []
    def hosted_update(**kwargs):
        calls.append(("hosted", kwargs))
        new_path.parent.mkdir()
        new_path.write_text("{}")
        (new_path.parent / "manifest.json").write_bytes(kwargs["manifest"])
        return {"route": "hosted-button"}
    monkeypatch.setattr(update.hosted, "run_hosted_update", hosted_update)
    monkeypatch.setattr(update.journey, "bootstrap", lambda *a: pytest.fail("the journey must not run the bootstrap itself"))
    monkeypatch.setattr(update.journey, "verify", lambda mode, *a, **kw: calls.append(("verify", mode)) or evidence / "observe.json")
    monkeypatch.setattr(update.journey, "wait_ready", lambda *a, **kw: calls.append(("ready",)))
    monkeypatch.setattr(update.journey, "installation", lambda *a: (new_path, data, new))
    monkeypatch.setattr(update.journey, "selected_tuple", lambda *a: None)
    health = iter(({"runtime_instance_id": "runtime-before", "identity": {"release": baseline}},
                   {"runtime_instance_id": "runtime-after", "identity": {"release": baseline + 1}}))
    monkeypatch.setattr(update.journey, "healthy", lambda *a: next(health))
    monkeypatch.setattr(update.journey, "assert_content", lambda *a: calls.append(("content",)))
    monkeypatch.setattr(update.journey, "sentinel_alive", lambda *a: True)
    monkeypatch.setattr(update.journey, "task", lambda *a, **kw: calls.append(("task", kw["create_project"])))
    monkeypatch.setattr(update, "active_service", lambda *a: service)
    monkeypatch.setattr(update, "service_names", lambda *a: names)
    monkeypatch.setattr(update, "boundary_state", lambda *a: boundary)
    result = {}
    update.execute(SimpleNamespace(shell="sh"), root, data, evidence, tmp_path, installed,
                   old_path, old, "project", tmp_path / "marker", "digest", object(), "birth", result)
    assert result["update"]["status"] == "pass"
    assert (result["update"]["release_from"], result["update"]["release_to"]) == (baseline, baseline + 1)
    assert result["update"]["hosted"] == {"route": "hosted-button"}
    assert all(value == "pass" for value in result["update"]["checks"].values())
    names = [call[0] for call in calls]
    assert names.count("hosted") == 1
    request = calls[names.index("hosted")][1]
    assert request["python"] == "/installed/python" and request["app"] == tmp_path
    assert request["door_base"] == update.journey.HEALTH_URL and request["root"] == root
    assert json.loads(request["manifest"])["release"] == baseline + 1
    assert request["manifest"] == (evidence / "fresh-update-standin" / "install-manifest.json").read_bytes()
    assert names.index("verify") < names.index("hosted")
    assert calls.index(("verify", "update")) < calls.index(("task", False))


def test_execute_rejects_a_baseline_that_is_not_the_candidate_release(monkeypatch, tmp_path):
    from types import SimpleNamespace

    root, old_path, new_path, _, service, _, names, _, boundary, _ = transition(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (old_path.parent / "manifest.json").write_text(json.dumps({"release": 7}))
    old = {"root": str(old_path.parent), "python": "/p", "identity": {"release": 6, "managed_by": None}}
    monkeypatch.setattr(update.hosted, "run_hosted_update", lambda **kw: pytest.fail("must not post"))
    result = {}
    with pytest.raises(RuntimeError, match="not direct release 7"):
        update.execute(SimpleNamespace(shell="sh"), root, tmp_path / "data", evidence, tmp_path, {"release": 7},
                       old_path, old, "project", tmp_path / "marker", "digest", object(), "birth", result)
    assert result["update"]["status"] == "fail"


@pytest.mark.parametrize("published", [0, 1, 7])
def test_gate_reads_the_published_release_from_the_row_and_expects_exactly_one_more(published):
    hosted = {**HOSTED, "final": {**HOSTED["final"], "target_release": published + 1}}
    checks = {name: "pass" for name in update.CHECKS}
    receipt = {"status": "pass", "release_from": published, "release_to": published + 1, "hosted": hosted,
               "old_version": "old", "new_version": "new", "manifest_sha256": "a" * 64, "checks": checks,
               "service": {"service_path": "/service/astrid.service", "foreground_fallback": False},
               "service_names": ["astrid.service"]}
    assert not gate.fresh_update_errors("direct-linux-fresh", receipt)
    for bad in (published, published + 2, True):
        assert gate.fresh_update_errors("direct-linux-fresh", {**receipt, "release_to": bad})
    # The Runtime's own status must agree, the request must be the credential-free POST, and the intercept reverted.
    for change in ({"final": {**hosted["final"], "target_release": published + 2}},
                   {"request": {**hosted["request"], "authorization": True}},
                   {"served": {**hosted["served"], "/install-manifest.json": 0}},
                   {"intercept_reverted": False}):
        assert gate.fresh_update_errors("direct-linux-fresh", {**receipt, "hosted": {**hosted, **change}})
    assert gate.fresh_update_errors("direct-linux-fresh", {k: v for k, v in receipt.items() if k != "hosted"})
