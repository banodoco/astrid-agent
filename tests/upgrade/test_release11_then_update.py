"""Focused controls for the release11-then-update leg.

The leg starts from the served Release 11 tuple, installed plainly from its pinned
App commit, then takes the candidate's public ``install --update`` to Release 12.
These tests drive the leg body with fakes and pin its four assertions: the installed
release is 12, the candidate's run receipt says updated, exactly one Runtime is live,
and the seeded user data survives.
"""
import argparse
import json
from pathlib import Path

import pytest

from scripts import ci_journey as journey
from scripts import journey_matrix
from scripts import journey_version_jumps as jumps

LEG = "release11-then-update"
ROW_IDS = ("direct-linux-r11-update", "direct-macos-r11-update")
RELEASE11_RELEASE = 11
RELEASE12_RELEASE = 12


def _leg(monkeypatch, tmp_path, *, candidate_release=12, update_exit=0, update_installs=True,
         receipt_kind="updated", live=None, preserved=True):
    app, candidate_path, root = (tmp_path / name for name in ("app", "candidate.json", "program"))
    data = tmp_path / "data"
    app.mkdir()
    candidate_path.write_text("{}", encoding="utf-8")
    manifest = {"release": candidate_release, "agent": {"sha": "agent-r12"}, "runtime": {"sha": "runtime-r12"}}
    versions = {"r11": {"root": str(root / "versions/r11")}, "r12": {"root": str(root / "versions/r12")}}
    state = {"current": "r11"}
    bootstraps = []
    args = argparse.Namespace(job_id="direct-linux-r11-update", mode="direct", leg=LEG,
                              shell="sh", candidate_checkouts={}, service_manager_prerequisite={},
                              app_checkout=app, candidate=candidate_path)
    release = {"r11": RELEASE11_RELEASE, "r12": RELEASE12_RELEASE}

    def installation(*_args):
        version = versions[state["current"]]
        return Path(version["root"]) / "version.json", data, version

    def plain_install(source, manifest_path, shell, evidence, root_path, data_path, agent=None):
        bootstraps.append((source, "install"))
        state["current"] = "r11"

    def candidate_update(*_args):
        if update_installs:
            state["current"] = "r12"
        return {"command": "install --update", "exit_code": update_exit, "log": "update.log", "last_lines": []}

    monkeypatch.setattr(jumps, "release11_admission", lambda *_: (
        tmp_path / "release11-source", tmp_path / "r11.json", {"app_commit": jumps.RELEASE11_APP_COMMIT}))
    monkeypatch.setattr(jumps.journey, "selected_manifest", lambda *_a, **_k: (app / "manifest.json", manifest))
    monkeypatch.setattr(jumps.journey, "read_json", lambda path: {"manifest_sha256": "candidate", "artifacts": {}}
                        if Path(path) == candidate_path else {})
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: manifest)
    monkeypatch.setattr(jumps.journey, "git_source", lambda *_: {"sha": "app-r12"})
    monkeypatch.setattr(jumps.journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release11", plain_install)
    monkeypatch.setattr(jumps, "candidate_update", candidate_update)
    monkeypatch.setattr(jumps, "run_receipt", lambda _data: {
        "format": "astrid-last-run/v1", "kind": receipt_kind, "exit_code": 0 if receipt_kind == "updated" else 3,
        "start": 11, "still": 12 if receipt_kind == "updated" else 11, "target": 12})
    monkeypatch.setattr(jumps, "live_runtime_owners", lambda _data: (
        live if live is not None else [{"data_root": str(data), "pid": 1, "runtime_instance_id": "new"}]))
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(jumps, "selected_data_root", lambda _root, selected, **_k: selected)
    monkeypatch.setattr(jumps.journey, "wait_ready", lambda *_: None)
    monkeypatch.setattr(jumps.journey, "installation", installation)
    monkeypatch.setattr(jumps, "installed_release", lambda version: next(
        release[name] for name, value in versions.items() if value is version))
    monkeypatch.setattr(jumps.journey, "selected_tuple", lambda *_: {"agent": "old", "runtime": "old"})
    monkeypatch.setattr(jumps.journey, "assert_direct_service_ownership",
                        lambda *_a, **_k: {"manager": "systemd-user", "foreground_fallback": False,
                                           "phase": "release11-or-candidate"})
    monkeypatch.setattr(jumps.journey, "verify", lambda *_a, **_k: None)
    monkeypatch.setattr(jumps, "live_workspace", lambda *_args, **_kwargs: (
        {"project": "film"} if _args[3] == "seed" else {
            "project": "film", "timeline": "kept", "preferences": "kept", "task": "settled", "cas_sha256": "abc"}))
    monkeypatch.setattr(jumps.journey, "healthy", lambda *_: {
        "runtime_instance_id": "new" if state["current"] == "r12" else "old",
        "identity": {"release": release[state["current"]]}})
    monkeypatch.setattr(jumps, "selected_candidate", lambda *_: {"agent": "agent-r12", "runtime": "runtime-r12"})
    monkeypatch.setattr(jumps, "add_journey_pack_and_restart_legacy_host", lambda *_a: ["chiptune"])
    monkeypatch.setattr(jumps, "task_after_update", lambda *_args, **_kwargs: "task-id")

    def assert_seeded_data(before, after):
        if not preserved:
            raise RuntimeError("seeded workspace task did not survive the update")

    monkeypatch.setattr(jumps, "assert_preserved", assert_seeded_data)
    monkeypatch.setattr(jumps, "semantic_digest", lambda *_: "same-content")

    def run():
        return jumps.execute_release11_then_update(args, root, data, tmp_path / "evidence",
                                                   argparse.Namespace(pid=123), "birth")
    return run, bootstraps, tmp_path / "evidence" / "journey-result.json"


def test_release11_identity_is_the_served_tuple_at_the_pinned_app_commit():
    assert jumps.RELEASE11_LEG == LEG
    assert jumps.RELEASE11_APP_COMMIT == "57e200b45615a59181920192697a932400d985fc"
    assert jumps.RELEASE11_RELEASE == RELEASE11_RELEASE
    assert jumps.RELEASE11_IDENTITY == {
        "agent": "a105de3a4852cb61b9647dfe2c920118bd29f272",
        "runtime": "4f5477cb0f5b03d28bec991a1fe34edcd8fc309f",
        "schema_digest": "sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c",
    }


def test_release11_pin_is_a_named_entry_in_the_release_table():
    releases = json.loads((Path(__file__).parent / "releases.json").read_text(encoding="utf-8"))["releases"]
    row = next(row for row in releases if row.get("id") == "release11")
    assert row == {"id": "release11", "source": "app-commit", "app_commit": jumps.RELEASE11_APP_COMMIT,
                   "reason": row["reason"]}


def test_release11_leg_rows_are_direct_linux_and_macos_and_outside_the_strict_matrix():
    rows = {row["job_id"]: row for row in journey_matrix.RELEASE11_UPDATE_MATRIX}
    assert set(rows) == set(ROW_IDS)
    assert rows["direct-linux-r11-update"] == {
        "job_id": "direct-linux-r11-update", "mode": "direct", "runner": "ubuntu-24.04",
        "shell": "sh", "os": "linux", "leg": LEG}
    assert rows["direct-macos-r11-update"] == {
        "job_id": "direct-macos-r11-update", "mode": "direct", "runner": "macos-15",
        "shell": "sh", "os": "macos", "leg": LEG}
    # The strict five-row Release-1 contract keeps its own legs and job ids.
    assert set(journey_matrix.RELEASE1_JOB_IDS).isdisjoint(ROW_IDS)
    assert journey_matrix.validate_job("direct-linux-r11-update", "direct")["leg"] == LEG
    assert journey_matrix.validate_job("direct-macos-r11-update", "direct")["leg"] == LEG


def test_release11_leg_is_selected_on_linux_and_macos_and_not_admitted_on_windows(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(jumps, "execute_release11_then_update",
                        lambda *args: seen.append(args) or "dispatched")
    for platform in ("linux", "darwin"):
        monkeypatch.setattr(jumps.sys, "platform", platform)
        assert jumps.execute_owned(argparse.Namespace(leg=LEG), tmp_path / "program", tmp_path / "data",
                                   tmp_path / "evidence", argparse.Namespace(pid=1), "birth") == "dispatched"
    assert len(seen) == 2
    monkeypatch.setattr(jumps.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="narrowed refusal"):
        jumps.execute_owned(argparse.Namespace(leg=LEG), tmp_path / "program", tmp_path / "data",
                            tmp_path / "evidence", argparse.Namespace(pid=1), "birth")


def test_release11_install_is_the_plain_install_and_never_the_update_command(monkeypatch, tmp_path):
    calls = []

    def fake_bootstrap(source, manifest, shell, mode, evidence, root, data, **kwargs):
        calls.append((mode, kwargs))

    monkeypatch.setattr(jumps.journey, "bootstrap", fake_bootstrap)
    jumps.bootstrap_release11(tmp_path / "source", tmp_path / "r11.json", "sh", tmp_path / "evidence",
                              tmp_path / "program", tmp_path / "data", agent=tmp_path / "agent")
    assert calls == [("install", {"agent": tmp_path / "agent"})]


def test_leg_passes_only_when_the_update_installs_release_twelve_and_every_check_holds(monkeypatch, tmp_path):
    run, bootstraps, _ = _leg(monkeypatch, tmp_path)
    result = run()
    assert result["status"] == "pass"
    assert result["scenario"] == LEG
    assert bootstraps == [(tmp_path / "release11-source", "install")]
    assert result["update"]["exit_code"] == 0
    assert result["release_transition"] == {"from": 11, "to": 12, "source": "candidate-install-update"}
    assert result["health_transition"]["candidate_live_release"] == 12
    assert all(value == "pass" for value in result["checks"].values()), result["checks"]


def test_only_the_release11_baseline_adds_the_pack_and_restarts_its_host_before_its_task(monkeypatch, tmp_path):
    run, _, _ = _leg(monkeypatch, tmp_path)
    order = []
    monkeypatch.setattr(jumps, "add_journey_pack_and_restart_legacy_host",
                        lambda _root, _data, version, _evidence: order.append(("legacy-pack", version["root"])) or ["chiptune"])
    monkeypatch.setattr(jumps, "task_after_update",
                        lambda version, *_args, existing_pack_gate=True: order.append(
                            ("task", version["root"], existing_pack_gate)) or "task-id")
    assert run()["status"] == "pass"
    r11 = str(tmp_path / "program" / "versions/r11")
    # R11 has no single-worker hot refresh: add + restart once, before R11's baseline task only.
    # That baseline task skips the existing-pack gate; every later task keeps it.
    assert order[:2] == [("legacy-pack", r11), ("task", r11, False)]
    assert all(entry[2] is True for entry in order[2:] if entry[0] == "task")
    assert [entry for entry in order if entry[0] == "legacy-pack"] == [("legacy-pack", r11)]


@pytest.mark.parametrize("manager", ["systemd-user", "launchd"])
def test_legacy_baseline_adds_the_pack_then_restarts_its_owned_host(monkeypatch, tmp_path, manager):
    events = []
    owners = iter([[4242], []])
    monkeypatch.setattr(jumps.journey, "install_journey_packs",
                        lambda run_cli, **kwargs: events.append(("add", sorted(kwargs))) or ["chiptune"])
    monkeypatch.setattr(jumps.journey, "assert_direct_service_ownership",
                        lambda *_a, **_k: events.append("owned") or {"manager": manager, "service_path": "/la.plist"})
    monkeypatch.setattr(jumps.journey, "run", lambda argv, log, **_k: events.append(tuple(argv[:3])) or "")
    monkeypatch.setattr(jumps.journey, "runtime_processes", lambda *_a: next(owners))
    monkeypatch.setattr(jumps.journey, "wait_ready", lambda *_a: events.append("ready"))
    monkeypatch.setattr(jumps, "selected_data_root", lambda _root, selected, **_k: selected)
    monkeypatch.setattr(jumps.time, "sleep", lambda *_a: None)
    assert jumps.add_journey_pack_and_restart_legacy_host(
        tmp_path / "program", tmp_path / "data", {"root": "/v/r11"}, tmp_path / "ev") == ["chiptune"]
    stop, start = ((("systemctl", "--user", "stop"), ("systemctl", "--user", "start")) if manager == "systemd-user"
                   else (("launchctl", "bootout", f"gui/{jumps.os.getuid()}/haus.astrid.runtime"),
                         ("launchctl", "bootstrap", f"gui/{jumps.os.getuid()}")))
    # Add first (no hot-refresh hooks: this is the legacy path), then a full owned-host restart.
    assert events == [("add", ["routes_packs"]), "owned", stop, start, "ready", "owned"]


def test_legacy_baseline_restarts_nothing_when_the_pack_is_already_there(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(jumps.journey, "install_journey_packs", lambda run_cli, **kwargs: [])
    monkeypatch.setattr(jumps.journey, "run", lambda *a, **k: events.append("run"))
    assert jumps.add_journey_pack_and_restart_legacy_host(
        tmp_path / "program", tmp_path / "data", {"root": "/v/r11"}, tmp_path / "ev") == []
    assert events == []


def test_update_that_exits_zero_and_installs_nothing_fails(monkeypatch, tmp_path):
    run, _, receipt = _leg(monkeypatch, tmp_path, update_installs=False)
    with pytest.raises(RuntimeError, match="exited 0 but installed nothing: the selected release is still 11, "
                                           "not the candidate release 12"):
        run()
    recorded = json.loads(receipt.read_text())
    assert recorded["status"] == "fail"
    assert recorded["checks"]["update_exit"] == "pass"
    assert recorded["checks"]["release"] == "not-run"


def test_update_that_exits_nonzero_fails_on_the_exit_code(monkeypatch, tmp_path):
    run, _, receipt = _leg(monkeypatch, tmp_path, update_exit=3, update_installs=False)
    with pytest.raises(RuntimeError, match="install --update exited 3"):
        run()
    assert json.loads(receipt.read_text())["checks"]["update_exit"] == "not-run"


def test_run_receipt_must_say_updated_to_release_twelve(monkeypatch, tmp_path):
    run, _, receipt = _leg(monkeypatch, tmp_path, receipt_kind="not-updated")
    with pytest.raises(RuntimeError, match="run receipt is not updated to release 12"):
        run()
    recorded = json.loads(receipt.read_text())
    assert recorded["checks"]["run_receipt"] == "not-run"
    assert recorded["checks"]["release"] == "not-run"


def test_exactly_one_live_runtime_is_required_after_the_update(monkeypatch, tmp_path):
    data = tmp_path / "data"
    run, _, receipt = _leg(monkeypatch, tmp_path, live=[
        {"data_root": str(data), "pid": 1, "runtime_instance_id": "release11-era"},
        {"data_root": str(data) + "-r1", "pid": 2, "runtime_instance_id": "new"}])
    with pytest.raises(RuntimeError, match="Exactly one live Runtime"):
        run()
    assert json.loads(receipt.read_text())["checks"]["one_runtime"] == "not-run"


def test_seeded_user_data_must_survive_the_update(monkeypatch, tmp_path):
    run, _, receipt = _leg(monkeypatch, tmp_path, preserved=False)
    with pytest.raises(RuntimeError, match="seeded workspace task did not survive"):
        run()
    recorded = json.loads(receipt.read_text())
    assert recorded["checks"]["semantic_data"] == "not-run"
    assert recorded["checks"]["task_settled"] == "not-run"


def test_candidate_must_be_release_twelve(monkeypatch, tmp_path):
    run, bootstraps, receipt = _leg(monkeypatch, tmp_path, candidate_release=11)
    with pytest.raises(RuntimeError, match="requires the candidate to be Release 12"):
        run()
    assert bootstraps == []
    assert json.loads(receipt.read_text())["status"] == "fail"


@pytest.mark.parametrize(("changes", "message"), [
    ({"mode": "pinokio-backend"}, "pinned Release-11"),
    ({"job_id": None}, "pinned Release-11"),
    ({"candidate": None}, "pinned Release-11"),
    ({"release11_app_checkout": None}, "pinned Release-11"),
    ({"allow_release11_to_candidate": False}, "pinned Release-11"),
    ({"source_override": True}, "source override"),
])
def test_release11_admission_is_narrow(tmp_path, changes, message):
    values = {"mode": "direct", "job_id": "direct-linux-r11-update", "candidate": tmp_path / "candidate.json",
              "allow_release11_to_candidate": True, "release11_app_checkout": tmp_path,
              "source_override": False, **changes}
    with pytest.raises(ValueError, match=message):
        jumps.release11_admission(argparse.Namespace(**values), tmp_path / "source")


def test_release11_source_must_resolve_to_the_pinned_identity(monkeypatch, tmp_path):
    def resolved(identity, commit=jumps.RELEASE11_APP_COMMIT):
        return lambda *_args, **_kwargs: {
            "status": "verified", "app_manifest_commit": commit, "identity": identity,
            "manifest_sha256": "a" * 64, "bootstrap": [], "local_candidate_used": False,
        }
    monkeypatch.setattr(jumps.upgrade_release, "resolve", resolved(jumps.RELEASE11_IDENTITY, commit="b" * 40))
    with pytest.raises(RuntimeError, match="pinned App commit"):
        jumps.verified_release11_source(tmp_path, tmp_path / "source")
    monkeypatch.setattr(jumps.upgrade_release, "resolve",
                        resolved({**jumps.RELEASE11_IDENTITY, "agent": "e" * 40}))
    with pytest.raises(RuntimeError, match="identity differs"):
        jumps.verified_release11_source(tmp_path, tmp_path / "source")


@pytest.mark.parametrize("drop", ["--release11-app-checkout", "--allow-release11-to-candidate"])
def test_driver_requires_the_release11_admission_flags(tmp_path, monkeypatch, drop):
    monkeypatch.setattr(journey, "execute", lambda *unused: pytest.fail("must reject before execution"))
    monkeypatch.setattr(journey.sys, "platform", "linux")
    argv = ["--leg", LEG, "--mode", "direct", "--job-id", "direct-linux-r11-update",
            "--app-checkout", str(tmp_path), "--run-root", str(tmp_path / "run"),
            "--root", str(tmp_path / "run" / "program"), "--data-root", str(tmp_path / "run" / "data"),
            "--candidate", str(tmp_path / "candidate.json"),
            "--candidate-agent-checkout", str(tmp_path / "agent"),
            "--candidate-runtime-checkout", str(tmp_path / "runtime"),
            "--pinokio-checkout", str(tmp_path / "pinokio"), "--shell", "sh",
            "--release11-app-checkout", str(tmp_path / "release11"), "--allow-release11-to-candidate"]
    index = argv.index(drop)
    del argv[index:index + (1 if drop.startswith("--allow") else 2)]
    with pytest.raises(SystemExit) as failure:
        journey.main(argv)
    assert failure.value.code == 2


def test_driver_hands_the_release11_leg_to_execution_on_its_row(tmp_path, monkeypatch):
    recorded = []
    monkeypatch.setattr(journey, "execute", lambda args: recorded.append(args.leg) or {"status": "pass"})
    monkeypatch.setattr(journey.sys, "platform", "linux")
    argv = ["--leg", LEG, "--mode", "direct", "--job-id", "direct-linux-r11-update",
            "--app-checkout", str(tmp_path), "--run-root", str(tmp_path / "run"),
            "--root", str(tmp_path / "run" / "program"), "--data-root", str(tmp_path / "run" / "data"),
            "--candidate", str(tmp_path / "candidate.json"),
            "--candidate-agent-checkout", str(tmp_path / "agent"),
            "--candidate-runtime-checkout", str(tmp_path / "runtime"),
            "--pinokio-checkout", str(tmp_path / "pinokio"), "--shell", "sh",
            "--release11-app-checkout", str(tmp_path / "release11"), "--allow-release11-to-candidate"]
    assert journey.main(argv) == 0
    assert recorded == [LEG]
