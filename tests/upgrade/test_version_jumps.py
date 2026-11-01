"""C1 harness controls. These never install or stop a native Runtime."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from scripts import ci_journey as journey
from scripts import journey_version_jumps as jumps


def snapshot():
    return {"project": {"id": "film", "name": "Film"}, "timeline": {"edit": "keep"},
            "preferences": {"content": "fresh user change"}, "task": {"status": "succeeded"},
            "cas_sha256": "abc", "schema_version": 31, "target_schema_version": 31}


@pytest.mark.parametrize("field", ["project", "timeline", "preferences", "task", "cas_sha256"])
def test_c1_preservation_rejects_each_lost_user_content_class(field):
    before, after = snapshot(), snapshot()
    after[field] = "lost"
    with pytest.raises(RuntimeError, match=field):
        jumps.assert_preserved(before, after)


def test_c1_schema_checks_actual_candidate_target_and_allows_real_migrations():
    before, after = snapshot(), snapshot()
    before["schema_version"] = before["target_schema_version"] = 28
    jumps.assert_preserved(before, after)
    after["schema_version"] = 30
    with pytest.raises(RuntimeError, match="schema"):
        jumps.assert_preserved(before, after)


@pytest.mark.parametrize("leg", journey.VERSION_JUMP_LEGS)
def test_c1_legs_require_fresh_owned_roots_before_lifecycle(tmp_path, leg):
    run_root = tmp_path / "run"
    roots = journey.run_scope(run_root, run_root / "program", run_root / "data", leg)
    assert roots[:3] == (run_root, run_root / "program", run_root / "data")
    assert (run_root / journey.OWNER_FILE).is_file()
    with pytest.raises(ValueError, match="empty"):
        journey.run_scope(run_root, run_root / "program", run_root / "data", leg)


def test_c1_old_declaration_mutation_only_touches_selected_version(tmp_path):
    root = tmp_path / "program"
    version = root / "versions" / "old"
    version.mkdir(parents=True)
    profile = version / "profile.json"
    declaration = version / "services.json"
    journey.write_json(profile, {"service_declaration": str(declaration)})
    journey.write_json(declaration, {"services": [{"name": "chat", "env": {"keep": "yes"}}]})
    old = {"root": str(version), "profile": str(profile)}
    assert jumps.seed_old_declaration(old, root, "bare")["command"] == "astrid"
    pruned = jumps.seed_old_declaration(old, root, "pruned")
    assert not Path(pruned["command"]).exists()
    assert journey.read_json(declaration)["services"][0]["env"]["keep"] == "yes"
    outside = tmp_path / "outside.json"
    journey.write_json(outside, {"services": []})
    journey.write_json(profile, {"service_declaration": str(outside)})
    with pytest.raises(ValueError, match="outside"):
        jumps.seed_old_declaration(old, root, "bare")
    assert journey.read_json(outside) == {"services": []}


def test_c1_candidate_selection_rejects_same_tuple_and_wrong_identity(tmp_path, monkeypatch):
    before = {"agent": "old-a", "runtime": "old-r"}
    monkeypatch.setattr(journey, "selected_tuple", lambda *unused: before.copy())
    with pytest.raises(RuntimeError, match="distinct"):
        jumps.selected_candidate({}, {}, before)
    monkeypatch.setattr(journey, "selected_tuple", lambda *unused: {"agent": "new-a", "runtime": "new-r"})
    with pytest.raises(RuntimeError, match="identity"):
        jumps.selected_candidate({"release": 17}, {"identity": {"release": 16, "managed_by": None}}, before)
    assert jumps.selected_candidate({"release": 17}, {"identity": {"release": 17, "managed_by": None}}, before)["agent"] == "new-a"


def test_c1_cli_requires_declaration_variant_before_any_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "execute", lambda *_: pytest.fail("must reject CLI before execution"))
    with pytest.raises(SystemExit) as error:
        journey.main(["--leg", "update-over-old-declaration", "--app-checkout", str(tmp_path),
                      "--run-root", str(tmp_path), "--root", str(tmp_path / "program"),
                      "--data-root", str(tmp_path / "data")])
    assert error.value.code == 2


def test_c1_service_action_rejects_retained_old_action(tmp_path, monkeypatch):
    root, data = tmp_path / "program", tmp_path / "data"
    old, candidate = root / "versions/old", root / "versions/candidate"
    home, config = tmp_path / "home", tmp_path / "home/.config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(jumps.sys, "platform", "linux")
    monkeypatch.setattr(journey, "program_root", lambda: home / ".local/share/astrid")
    version = {"root": str(candidate), "identity": {"release": 17, "managed_by": None}}
    journey.write_json(home / ".local/share/astrid-install.json", {
        "program_root": str(root), "data_root": str(data), "identity": version["identity"]})
    unit = config / "systemd/user/astrid.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("ExecStart=" + str(old / "py/bin/python"))
    with pytest.raises(RuntimeError, match="action"):
        jumps.service_action(root, data, version, {"root": str(old)})
    unit.write_text("ExecStart=" + str(candidate / "py/bin/python"))
    assert jumps.service_action(root, data, version, {"root": str(old)})["identity"] == version["identity"]


def test_c1_unverified_historical_release_never_bootstraps(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    args = argparse.Namespace(leg="update-from-served", app_checkout=app, shell="sh",
                              source_override=False, agent_checkout=None, runtime_checkout=None)
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **kw: (app / "manifest.json", {}))
    monkeypatch.setattr(jumps.upgrade_release, "resolve", lambda *a, **kw: {"status": "environmental-unverified"})
    monkeypatch.setattr(journey, "bootstrap", lambda *a, **kw: pytest.fail("unverified release cannot install"))
    sentinel = argparse.Namespace(pid=123)
    with pytest.raises(RuntimeError, match="provenance"):
        jumps.execute_owned(args, tmp_path / "program", tmp_path / "data", tmp_path / "evidence", sentinel, "birth")
    receipt = journey.read_json(tmp_path / "evidence/journey-result.json")
    assert receipt["status"] == "fail"
    assert set(receipt["checks"].values()) == {"not-run"}


def test_c1_windows_containment_gap_refuses_before_historical_bootstrap(tmp_path, monkeypatch):
    args = argparse.Namespace(leg="update-from-release:pre-updater", shell="powershell.exe", source_override=False)
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    monkeypatch.setattr(journey, "bootstrap", lambda *a, **kw: pytest.fail("uncontained Windows bootstrap must not run"))
    with pytest.raises(RuntimeError, match="TST-B3"):
        jumps.execute_owned(args, tmp_path / "program", tmp_path / "data", tmp_path / "evidence",
                            argparse.Namespace(pid=123), "birth")
    assert journey.read_json(tmp_path / "evidence/journey-result.json")["status"] == "fail"


@pytest.mark.parametrize("lose_content", [False, True])
def test_c1_version_jump_uses_historical_bootstrap_then_candidate_and_checks_before_rerun(tmp_path, monkeypatch, lose_content):
    app, root, data, evidence = (tmp_path / name for name in ("app", "program", "data", "evidence"))
    app.mkdir()
    old = {"root": str(root / "versions/old"), "identity": {"release": 0, "managed_by": None}}
    candidate = {"root": str(root / "versions/new"), "identity": {"release": 17, "managed_by": None}}
    manifest = {"release": 17, "agent": {"sha": "new-a"}, "runtime": {"sha": "new-r"}}
    state, calls = {"version": old}, []
    args = argparse.Namespace(leg="update-from-release:pre-updater", app_checkout=app, shell="sh",
                              source_override=False, agent_checkout=None, runtime_checkout=None)
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **kw: (app / "manifest.json", manifest))
    def resolve(release_id, output, **kwargs):
        assert release_id == "pre-updater"
        journey.write_json(output / "install-manifest.json", {"agent": {"sha": "old-a"}, "runtime": {"sha": "old-r"}})
        return {"status": "verified", "app_manifest_commit": "old-app"}
    monkeypatch.setattr(jumps.upgrade_release, "resolve", resolve)
    def bootstrap(source, selected, shell, mode, *rest):
        calls.append(("bootstrap", source, selected, mode))
        if mode == "update":
            state["version"] = candidate
    monkeypatch.setattr(journey, "bootstrap", bootstrap)
    monkeypatch.setattr(journey, "wait_ready", lambda *_: None)
    monkeypatch.setattr(journey, "installation", lambda *a: (Path(state["version"]["root"]) / "version.json", data, state["version"]))
    monkeypatch.setattr(journey, "selected_tuple", lambda selected, version: {
        "agent": selected["agent"]["sha"], "runtime": selected["runtime"]["sha"]})
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *_: None)
    monkeypatch.setattr(journey, "healthy", lambda *_: {"pid": 123, "runtime_instance_id": state["version"]["root"]})
    monkeypatch.setattr(journey, "process_birth", lambda *_: "old-birth" if state["version"] == old else None)
    def verify(mode, version, workspace, folder, **kwargs):
        calls.append(("verify", mode))
        journey.write_json(folder / ("verify-" + mode + ".json"), {"details": {"origin.imports": {"astrid": "candidate"}}})
    monkeypatch.setattr(journey, "verify", verify)
    def task(version, folder, project):
        calls.append(("task", project))
        journey.write_json(folder / "task-settled.json", {"data": {"task_id": "sdk-task-id"}})
    monkeypatch.setattr(journey, "task", task)
    def workspace(version, data, folder, action, request):
        if action == "seed":
            assert request["task_id"] == "sdk-task-id"
            return {"project": "film"}
        result = snapshot()
        if lose_content and version == candidate:
            result["preferences"] = {"content": "stale backup"}
        return result
    monkeypatch.setattr(jumps, "workspace", workspace)
    monkeypatch.setattr(journey, "git_source", lambda *_: {"sha": "candidate-app"})
    monkeypatch.setattr(jumps, "service_action", lambda *a: {"identity": candidate["identity"]})
    monkeypatch.setattr(jumps, "rerun", lambda *a: calls.append(("rerun",)))
    monkeypatch.setattr(journey, "sentinel_alive", lambda *a: True)
    if lose_content:
        with pytest.raises(RuntimeError, match="preferences"):
            jumps.execute_owned(args, root, data, evidence, argparse.Namespace(pid=321), "sentinel-birth")
        assert ("rerun",) not in calls
        assert journey.read_json(evidence / "journey-result.json")["status"] == "fail"
    else:
        result = jumps.execute_owned(args, root, data, evidence, argparse.Namespace(pid=321), "sentinel-birth")
        assert result["status"] == "pass" and set(result["checks"].values()) == {"pass"}
        assert ("rerun",) in calls
    bootstraps = [call for call in calls if call[0] == "bootstrap"]
    assert bootstraps[0][1:] == (evidence / "initial-release", evidence / "initial-release/install-manifest.json", "install")
    assert bootstraps[1][1:] == (app, app / "manifest.json", "update")


@pytest.mark.integration
@pytest.mark.parametrize("revision", ["11443b0b5b34bd5b36725504fb083146192a759d", "959ce04fcf91351903c361d03e6b19eb0bbe8587"])
def test_c1_real_historical_and_candidate_workspace_apis(tmp_path, revision):
    """Real committed Runtime APIs; source fixture, no daemon/manager/install."""
    from tests.helpers.runtime import runtime_checkout
    repo = runtime_checkout()
    archive = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=tar", revision])
    source = tmp_path / "source"
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(source, filter="data")
    program = '''import importlib.util,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from runtime_protocol.store import RealmStore
from runtime_protocol.service import RuntimeService
spec=importlib.util.spec_from_file_location('helper',sys.argv[2]); h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
realm=Path(sys.argv[3]); RealmStore.initialize(realm).close(); s=RuntimeService(realm)
try:
    s.create_project({'slug':'film','name':'User film'},idempotency_key='film')
    lease=h.busy(s)
    assert s.store.get_task(lease['task_id'])['task']['status']=='running'
    from runtime_protocol.lifecycle import inspect_interruption_state
    report=inspect_interruption_state(realm)
    assert not report['safe'] and any(b['task_id']==lease['task_id'] for b in report['active_tasks'])
    s.store._settle_attempt(lease['task_id'],lease['lease_token'],{'outputs':[]},fence=lease['fence'],attempt_id=lease['attempt_id'])
    assert inspect_interruption_state(realm)['safe']
    keys=h.seed(s,'film','before',lease['task_id']); first=h.snapshot(s,keys)
    assert first['timeline']['content']['config']['metadata']['user_note']=='before'
    keys2=h.seed(s,'film','fresh',lease['task_id']); fresh=h.snapshot(s,keys2)
    assert first['preferences']['content']!=fresh['preferences']['content']
    assert fresh['task']['status']=='completed' and fresh['schema_version']==31
    lease2=h.busy(s); h.release(s,lease2)
    assert inspect_interruption_state(realm)['safe']
    print(json.dumps({'revision':sys.argv[4],'old':first,'fresh':fresh}))
finally: s.close()
'''
    completed = subprocess.run([sys.executable, "-I", "-c", program, str(source),
                                str(journey.ROOT / "scripts/journey_workspace.py"),
                                str(tmp_path / "realm"), revision], capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["revision"] == revision
