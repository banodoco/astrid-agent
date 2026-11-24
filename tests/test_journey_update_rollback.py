import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import data_fingerprint
from scripts import journey_ur_evidence as ev
from scripts import journey_ur_receipts as receipts_module
from scripts import journey_ur_stack as stack

ROOT = Path(__file__).resolve().parents[1]
SHA = {name: char * 40 for name, char in (("app", "a"), ("agent", "b"), ("runtime", "c"))}
CANDIDATE = {**SHA, "manifest_sha256": "d" * 64}


def passing_receipts():
    receipts = {name: {"status": "pass", "checks": {key: "pass" for key in checks}} for name, checks in ev.STEPS.items()}
    receipts[ev.RESULT_NAME] = {"status": "pass", "hook_present": True, "final_cleanup": "pass"}
    return receipts


def test_parse_candidate_requires_real_update():
    assert ev.parse_candidate(CANDIDATE)["agent"] == SHA["agent"]
    for bad in ({**CANDIDATE, "app": "x"}, {**CANDIDATE, "manifest_sha256": "d"},
                {**CANDIDATE, "agent": ev.R14["agent"]}, {**CANDIDATE, "runtime": ev.R14["runtime"]}, []):
        with pytest.raises(ValueError):
            ev.parse_candidate(bad)


def test_check_manifest_pins_and_release():
    r14 = {"release": 14, "agent": {"sha": ev.R14["agent"]}, "runtime": {"sha": ev.R14["runtime"]},
           "vibecomfy": {"sha": ev.R14["vibecomfy"]}}
    assert ev.check_manifest(r14, ev.R14, release=14) is r14
    with pytest.raises(ValueError):
        ev.check_manifest({**r14, "vibecomfy": {"sha": "0" * 40}}, ev.R14, release=14)
    with pytest.raises(ValueError):
        ev.check_manifest(r14, CANDIDATE)  # a Release 14 manifest is not a candidate
    r15 = {"release": 15, "agent": {"sha": SHA["agent"]}, "runtime": {"sha": SHA["runtime"]}}
    assert ev.check_manifest(r15, CANDIDATE) is r15
    with pytest.raises(ValueError):
        ev.check_manifest({**r15, "agent": {"sha": "0" * 40}}, CANDIDATE)


def test_hook_detection_reads_agent_source():
    assert ev.hook_present("x remove_history_guards y", "def remove_history_guards(self")
    assert not ev.hook_present("rollback_transaction", "def restore(self")


def test_guard_triggers_observe_the_realm_database(tmp_path):
    database = tmp_path / "realm.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE timeline_commit_history (id INTEGER PRIMARY KEY)")
    connection.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
    assert ev.guard_state(stack.guard_triggers(database)) == "absent"
    for name in ev.GUARD_TRIGGERS[:2]:
        connection.execute(f"CREATE TRIGGER {name} BEFORE UPDATE ON timeline_commit_history BEGIN SELECT 1; END")
    connection.execute("CREATE TRIGGER unrelated BEFORE UPDATE ON other BEGIN SELECT 1; END")
    connection.commit()
    assert ev.guard_state(stack.guard_triggers(database)) == "partial"
    connection.execute(f"CREATE TRIGGER {ev.GUARD_TRIGGERS[2]} BEFORE DELETE ON timeline_commit_history BEGIN SELECT 1; END")
    connection.commit()
    connection.close()
    assert ev.guard_state(stack.guard_triggers(database)) == "installed"


def stamps(**changes):
    value = {"version_id": "v14", "identity": {"release": 14, "managed_by": None}, "manifest_sha256": "m" * 64,
             "tuple": {"release": 14, "agent": SHA["agent"]}, "runtime_instance_id": "one",
             "health": {"identity": {"release": 14}, "schema_digest": "s", "protocol": "p"}}
    value.update(changes)
    return value


def test_compare_stamps_demands_exact_restoration_and_a_new_instance():
    assert ev.compare_stamps(stamps(), stamps(runtime_instance_id="two")) == []
    problems = ev.compare_stamps(stamps(), stamps(version_id="v15", runtime_instance_id="one"))
    assert len(problems) == 2
    assert ev.compare_stamps(stamps(health={}), stamps(health={}, runtime_instance_id="two"))


def clip(**fields):
    return {"clip_id": "c1", "start": [0, 1], **fields}


def test_compare_reads_lists_what_release_14_cannot_represent():
    read15 = {"head_content_digest": "sha256:" + "1" * 64, "parent_clips": [clip(new_field=1)]}
    same = ev.compare_reads(read15, {**read15, "parent_clips": [clip(new_field=1)]})
    assert same == {"identical": True, "unrepresentable": [], "mismatched": [], "ok": True}
    narrower = ev.compare_reads(read15, {"head_content_digest": "sha256:" + "2" * 64, "parent_clips": [clip()]})
    assert narrower["ok"] and narrower["unrepresentable"] == ["c1.new_field"] and not narrower["identical"]
    assert not ev.compare_reads(read15, {**read15, "parent_clips": [clip(start=[1, 1], new_field=1)]})["ok"]
    assert not ev.compare_reads(read15, {"head_content_digest": "x", "parent_clips": []})["ok"]
    assert not ev.compare_reads({"parent_clips": []}, {"parent_clips": []})["ok"]


def test_fingerprint_wiring_matches_data_fingerprint_and_summarises_survival(tmp_path):
    data = tmp_path / "data"
    for realm in ("one", "two"):
        (data / "runtime" / "realms" / realm).mkdir(parents=True)
        (data / "runtime" / "realms" / realm / "realm.sqlite3").write_bytes(realm.encode())
    (data / "runtime" / "realms" / "one" / "realm.sqlite3-wal").write_bytes(b"w")
    before = data_fingerprint.snap(data, stack.REALM_GLOB, data_fingerprint.DEFAULT_EXCLUDES)
    assert before["realms"] == ["one", "two"]
    (data / "runtime" / "realms" / "one" / "realm.sqlite3-wal").unlink()
    (data / "runtime" / "realms" / "two" / "realm.sqlite3").write_bytes(b"changed")
    after = data_fingerprint.snap(data, stack.REALM_GLOB, data_fingerprint.DEFAULT_EXCLUDES)
    summary = ev.fingerprint_summary(before, after)
    assert summary["ok"] and summary["files_changed"] == ["runtime/realms/two/realm.sqlite3"]
    (data / "runtime" / "realms" / "one" / "realm.sqlite3").unlink()
    lost = ev.fingerprint_summary(before, data_fingerprint.snap(data, stack.REALM_GLOB, data_fingerprint.DEFAULT_EXCLUDES))
    assert not lost["ok"] and lost["files_lost"] == ["runtime/realms/one/realm.sqlite3"]
    assert not ev.fingerprint_summary({"files": {}, "realms": []}, {"files": {}, "realms": []})["ok"]


def test_evaluate_passes_only_with_every_receipt_and_check():
    assert ev.evaluate(passing_receipts()) == []
    missing = passing_receipts()
    del missing["step3-rollback.json"]
    assert ev.evaluate(missing) == ["step3-rollback.json: receipt missing"]
    skipped = passing_receipts()
    skipped["step3-rollback.json"]["checks"]["c_guard_layer_removed"] = "skip_as_fail"
    assert any("c_guard_layer_removed" in problem for problem in ev.evaluate(skipped))
    absent = passing_receipts()
    absent[ev.RESULT_NAME]["hook_present"] = False
    assert any(ev.HOOK_MARKER in problem for problem in ev.evaluate(absent))
    unfinished = passing_receipts()
    unfinished["step1-r14-install.json"]["checks"].pop("digest_recorded")
    unfinished[ev.RESULT_NAME]["final_cleanup"] = "fail"
    assert len(ev.evaluate(unfinished)) == 2 and len(ev.evaluate({})) == len(ev.STEPS) + 1


def test_check_command_exits_nonzero_on_empty_and_zero_on_complete(tmp_path):
    script = ROOT / "scripts" / "journey_update_rollback.py"
    run = lambda: subprocess.run([sys.executable, str(script), "check", str(tmp_path)], capture_output=True, text=True)  # noqa: E731
    empty = run()
    assert empty.returncode == 1 and "step1-r14-install.json: receipt missing" in empty.stderr
    for name, body in passing_receipts().items():
        (tmp_path / name).write_text(json.dumps(body))
    assert run().returncode == 0


def test_workflow_exposes_only_the_dispatch_input_and_one_new_job():
    workflow = yaml.safe_load((ROOT / ".github/workflows/journey.yml").read_text(encoding="utf-8"))
    triggers = workflow.get("on") or workflow[True]
    assert "update_rollback_only" in triggers["workflow_dispatch"]["inputs"]
    assert "update_rollback_only" not in triggers["workflow_call"]["inputs"]
    job = workflow["jobs"]["update_rollback"]
    assert job["runs-on"] == "ubuntu-24.04" and "update_rollback" in workflow["jobs"]["gate"]["needs"]
    assert "update_rollback_only != 'true'" in workflow["jobs"]["journey"]["if"]
    steps = json.dumps(job["steps"])
    assert ev.R14["app"] in steps and ev.R14["agent"] in steps and "journey_update_rollback.py" in steps


def test_job_level_env_uses_no_runner_context():
    # GitHub rejects the whole workflow file ("workflow file issue", a 0-job run) when job-level env uses `runner`.
    workflow = yaml.safe_load((ROOT / ".github/workflows/journey.yml").read_text(encoding="utf-8"))
    env = workflow["jobs"]["update_rollback"]["env"]
    assert not [k for k, v in env.items() if "runner." in str(v)]


def run_config(tmp_path, **overrides):
    """The workflow's own ``config`` step, run as test_keepgoing_workflow.py runs it."""
    import os
    import textwrap
    workflow = (ROOT / ".github/workflows/journey.yml").read_text(encoding="utf-8")
    code = workflow.split("      - id: config\n", 1)[1].split("          python3 - <<'PY'\n", 1)[1].split("\n          PY\n", 1)[0]
    (tmp_path / "driver").symlink_to(ROOT, target_is_directory=True)
    env = {**os.environ, "GITHUB_OUTPUT": str(tmp_path / "output"), "DRIVER_AGENT_REF": "b" * 40,
           "CANDIDATE": json.dumps({**SHA, "pinokio": "e" * 40, "manifest_sha256": "d" * 64}),
           "WORKFLOW_REPOSITORY": "banodoco/astrid-agent", "OLD_LAUNCHER_REF": "", "STOPPED_PATH_AGENT_REF": "",
           "M4_4A_EXECUTOR_CONFIGS": '{"linux":{},"macos":{},"windows":{}}', "DIAGNOSTIC_KEEP_GOING": "false",
           "DIAGNOSTIC_LEG": "release9-then-install", "DIRECT_ONLY": "false", "JOURNEY_ROW": "",
           "M4_4A_BACKEND_ONLY": "false", "M4_5_ENABLED": "false", "M4_5_ONLY": "false", "M4_5_BOUNDARY": "",
           "M4_5_BOUNDARY_ARGS": "[]", "M4_4B_ENABLED": "false", "M4_6_ENABLED": "false", "M4_6_CANDIDATE": "",
           **overrides}
    done = subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=tmp_path, env=env, capture_output=True, text=True)
    output = tmp_path / "output"
    return done, dict(line.split("=", 1) for line in output.read_text().splitlines()) if output.exists() else {}


def test_config_accepts_the_update_rollback_dispatch_without_release1_launcher_inputs(tmp_path, tmp_path_factory):
    # `row` defaults to "direct" on a dispatch, so that default must not count as another leg selection.
    done, outputs = run_config(tmp_path, UPDATE_ROLLBACK_ONLY="true", JOURNEY_ROW="direct")
    assert done.returncode == 0, done.stderr
    assert outputs["update_rollback_only"] == "true" and outputs["m4_5_only"] == "false"
    plain, _ = run_config(tmp_path_factory.mktemp("plain"))
    assert plain.returncode != 0  # without the new input the old launcher inputs are still required


def test_config_refuses_to_combine_update_rollback_with_another_leg(tmp_path):
    done, _ = run_config(tmp_path, UPDATE_ROLLBACK_ONLY="true", JOURNEY_ROW="direct-linux")
    assert done.returncode != 0 and "cannot be combined" in done.stderr


def test_receipt_writer_and_loader_round_trip(tmp_path):
    body = receipts_module.receipt(tmp_path, "step4-resave-reupdate.json", {"e_r14_edit_saved": "pass"}, note="x")
    assert body["status"] == "fail" and body["checks"]["e_reupdate_succeeded"] == "not-run" and body["note"] == "x"
    assert receipts_module.load_receipts(tmp_path)["step4-resave-reupdate.json"] == body
    assert receipts_module.report_problems(tmp_path)  # three other steps and the result are missing


def test_entry_point_imports_every_module_when_run_as_a_script():
    script = ROOT / "scripts" / "journey_update_rollback.py"
    done = subprocess.run([sys.executable, str(script), "run", "--help"], capture_output=True, text=True)
    assert done.returncode == 0 and "--candidate-agent-checkout" in done.stdout


def test_each_module_stays_small():
    for name in ("journey_update_rollback", "journey_ur_evidence", "journey_ur_receipts", "journey_ur_stack",
                 "journey_ur_steps"):
        assert len((ROOT / "scripts" / f"{name}.py").read_text(encoding="utf-8").splitlines()) <= 250, name
