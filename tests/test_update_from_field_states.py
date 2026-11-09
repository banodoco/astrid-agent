"""`install --update` from install states that exist in the field (UR-15, UR-15a).

Start states come from ``tests.fixtures.field_states``. The live Runtime is the
persistent systemctl simulation plus a health probe that answers only while
the R1 unit runs and reports the selected version, so the run receipt's
post-condition compares real state, not a branch's opinion of itself.
"""
from __future__ import annotations

import hashlib
import json
import sys

import pytest
from pathlib import Path

from runtime_protocol import platform

from astrid.installer import cli
from astrid.installer.core import Installer, current, read_json
from tests.fixtures.field_states import R0_INSTANCE, handed_off_release1_r0_live, selection, stage_release
from tests.test_boundary_handoff import case, snapshot  # noqa: F401 - pytest fixture

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="persistent Linux manager simulation")


def _cli(c, monkeypatch):
    monkeypatch.setattr(cli, "Backend", lambda *a, **kw: c.backend)
    monkeypatch.setattr(cli, "validate_manifest", lambda value: value)
    monkeypatch.setattr(cli, "_prompt_choices", lambda: pytest.fail("an existing install is never prompted"))


def _update(c, release, *extra):
    return cli.main(["--update", "--root", str(c.root), "--manifest", str(c.manifest_path(release)),
                     "--uv", sys.executable, *extra])


def _summary(out):
    return [line for line in out.splitlines() if line.strip()]


def test_handed_off_release1_update_reaches_the_target_release(case, monkeypatch, capsys):
    """The 8 Nov field failure: a finished storage move, then --update, then Release 2 is live."""
    c = handed_off_release1_r0_live(case, monkeypatch)
    _cli(c, monkeypatch)
    r0_before = snapshot(c.data)
    r0_unit = c.service.path
    r0_unit_bytes = r0_unit.read_bytes()
    code = _update(c, 2)
    out = capsys.readouterr().out
    # The release changes to the target and the live Runtime reports it.
    assert current(c.root).release == 2, out
    assert c.verified_releases == [2]
    assert code == 0, out
    assert "Astrid updated: Release 1 → Release 2." in out
    assert "Astrid is using" not in out and "handoff handled" not in out
    # The update ran against R1, never R0, whose retained data is untouched.
    selected = selection(c)
    assert selected["data_root"] == str(c.destination)
    assert selected["realm_root"] == str(c.destination / "runtime/realms/r0")
    assert selected["retained_r0_data_root"] == str(c.data)
    assert "r1_data_root" not in selected and "r1_realm_root" not in selected
    assert read_json(c.root / "install.json")["data_root"] == str(c.destination)
    assert snapshot(c.data) == r0_before
    assert read_json(c.data / "runtime/discovery.json")["runtime_instance_id"] == R0_INSTANCE
    # Normalised once: the journal is an archive, not an authority.
    assert not c.tx.journal.exists()
    assert read_json(c.tx.journal.with_name("astrid-r1-handoff.completed.json"))["phase"] == "complete"
    # The R1 service was restarted on the new version; R0's definition was never rewritten.
    assert c.state()["r1"] == "active" and not c.state()["running"] and not c.state()["enabled"]
    assert ["--user", "stop", "astrid-r1.service"] in c.state()["calls"]
    assert ["--user", "start", "astrid-r1.service"] in c.state()["calls"]
    assert r0_unit.read_bytes() == r0_unit_bytes
    # The retained R0 program stays while rollback is a decision for the owner (UR-15e).
    assert (c.root / "versions/release0").is_dir()
    receipt = read_json(platform.log_dir() / "last-run.json")
    assert receipt["format"] == "astrid-last-run/v1"
    assert (receipt["kind"], receipt["start"], receipt["still"], receipt["target"], receipt["exit_code"]) == (
        "updated", 1, 2, 2, 0)


def test_update_that_installs_nothing_fails_its_post_condition(case, monkeypatch, capsys):
    """No branch can report success while the live release is not the target."""
    c = handed_off_release1_r0_live(case, monkeypatch)
    _cli(c, monkeypatch)

    class Sink(Installer):
        def run(self, *args, **kwargs):  # returns normally having installed nothing
            return None

    monkeypatch.setattr(cli, "Installer", Sink)
    code = _update(c, 2)
    lines = _summary(capsys.readouterr().out)
    assert code == 3
    first = lines.index("Astrid did NOT update. It is still Release 1; Release 2 is available.")
    assert lines[first + 1].startswith("Why: the installer finished without installing the new release.")
    assert current(c.root).release == 1
    receipt = read_json(platform.log_dir() / "last-run.json")
    assert (receipt["kind"], receipt["reason"], receipt["still"], receipt["target"], receipt["exit_code"]) == (
        "not-updated", "installer-bug", 1, 2, 3)


def test_install_line_on_handed_off_install_repairs_and_applies_choices(case, monkeypatch, capsys):
    """Choice changes after a storage move use the ordinary path (no completed-policy branch)."""
    c = handed_off_release1_r0_live(case, monkeypatch)
    _cli(c, monkeypatch)
    synced = []
    monkeypatch.setattr(c.backend, "sync_skills", lambda version: synced.append(version.release) or {
        "ok": True, "destinations": []})
    monkeypatch.setattr(c.backend, "restart_required", lambda: False)
    code = cli.main(["--root", str(c.root), "--manifest", str(c.manifest_path(1)), "--uv", sys.executable,
                     "--skill-copies", "on"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Astrid is installed and running (Release 1)." in out
    assert synced == [1] and selection(c)["choices"]["skill_copies"] is True
    assert selection(c)["data_root"] == str(c.destination)
    assert read_json(platform.log_dir() / "last-run.json")["kind"] == "repaired"


def test_rollback_after_normalisation_restores_r0_and_keeps_both_roots(case, monkeypatch, capsys):
    c = handed_off_release1_r0_live(case, monkeypatch)
    _cli(c, monkeypatch)
    from astrid.installer import boundary_handoff as handoff

    assert handoff.normalise_completed(c.backend)
    assert selection(c)["data_root"] == str(c.destination)
    r1 = snapshot(c.destination)
    assert cli.main(["--root", str(c.root), "--rollback"]) == 0
    manager = c.state()
    assert manager["enabled"] and manager["running"] and manager["r1"] in {"absent", "inactive"}
    assert current(c.root).release == 0
    assert selection(c)["data_root"] == str(c.data) and "retained_r0_data_root" not in selection(c)
    assert snapshot(c.destination) == r1


def test_install_line_clears_a_rolled_back_handoff_once_the_r1_copy_is_preserved(case, monkeypatch, capsys):
    """R2: --rollback leaves a rolled_back handoff journal, which refuses Update until the install line clears it.

    The retained R1 copy is never reused or replaced, so the install line names it first. Once the owner has
    preserved it elsewhere, the next install line must clear the journal; otherwise Update stays refused for good.
    """
    import os

    from astrid.installer import boundary_handoff as handoff
    from astrid.installer.installation import Installation

    c = handed_off_release1_r0_live(case, monkeypatch)
    _cli(c, monkeypatch)
    assert handoff.normalise_completed(c.backend)
    assert cli.main(["--root", str(c.root), "--rollback"]) == 0
    assert read_json(c.tx.journal)["phase"] == "rolled_back"
    assert Installation.resolve(c.service.home, system="linux").kind == "handoff-rolled-back"
    install = ["--root", str(c.root), "--manifest", str(c.manifest_path(1)), "--uv", sys.executable]
    capsys.readouterr()

    assert cli.main(install) != 0
    assert f"The previous R1 copy is retained at {c.destination}" in capsys.readouterr().out
    assert read_json(c.tx.journal)["phase"] == "rolled_back"

    preserved = c.destination.with_name(c.destination.name + "-preserved")
    os.replace(c.destination, preserved)
    r1 = snapshot(preserved)
    reached = []

    def fresh_handoff(self, *_args, **_kwargs):
        # The journal is gone before the fresh copy-only upgrade starts; stop the run here.
        reached.append((self.journal.exists(), Installation.resolve(c.service.home, system="linux").kind))
        raise RuntimeError("stop at the fresh handoff")

    monkeypatch.setattr(handoff.BoundaryHandoff, "run", fresh_handoff)
    cli.main(install)
    out = capsys.readouterr().out
    assert "The previous R1 copy is retained" not in out, out
    assert reached == [(False, "r0")], out
    assert snapshot(preserved) == r1


def test_rollback_after_a_post_handoff_update_refuses_without_changes(case, monkeypatch, capsys):
    c = handed_off_release1_r0_live(case, monkeypatch)
    _cli(c, monkeypatch)
    assert _update(c, 2) == 0
    r1_path = c.service.path
    c.service.path = r1_path.with_name("astrid.service")  # a fresh process starts at R0's unit path
    before = (hashlib.sha256(c.service.selection.read_bytes()).hexdigest(), json.dumps(c.state()["r1"]))
    assert cli.main(["--root", str(c.root), "--rollback"]) == 1
    assert "switching back to that copy is not offered automatically" in capsys.readouterr().out
    assert (hashlib.sha256(c.service.selection.read_bytes()).hexdigest(), json.dumps(c.state()["r1"])) == before
    assert current(c.root).release == 2


def test_resolver_reports_the_field_state(case, monkeypatch):
    from astrid.installer.installation import Installation

    c = handed_off_release1_r0_live(case, monkeypatch)
    state = Installation.resolve(c.service.home, system="linux", measure=True)
    assert (state.kind, state.phase) == ("handoff", "complete")
    assert state.active_data_root == c.destination and state.retained_r0_data_root == c.data
    assert state.selected_release == 1 and state.live_release == 1
    assert {(r.data_root, r.selected) for r in state.live_runtimes} == {(c.data, False)} | (
        {(c.destination, True)} if (c.destination / "runtime/discovery.json").exists() else set())
    assert state.free_bytes and state.astrid_bytes["retained_r0"] > 0
    from astrid.installer import boundary_handoff as handoff

    handoff.normalise_completed(c.backend)
    state = Installation.resolve(c.service.home, system="linux")
    assert state.kind == "direct" and state.active_data_root == c.destination
    assert state.service_label == "astrid-r1.service"


def test_recovery_only_not_updated_headline_is_clear_and_never_bare_did_not_finish():
    """A --update that only recovers ends not-updated with a Next line, not a bare failure.

    The Pinokio launcher maps every non-zero exit to "did not finish", so the
    run's own receipt must say what happened and what to do next.
    """
    from astrid.installer.outcome import Outcome

    outcome = Outcome("not-updated", 4, 4, 5, "recovered-previous-version", "update")
    lines = outcome.headline()
    assert lines[0] == "Astrid did NOT update. It is still Release 4; Release 5 is available."
    assert lines[1].startswith("Why: an earlier update was interrupted")
    # One Next line for this case (from next_steps), with Details as the last line.
    assert "Next: Run the same command again to finish updating." in lines
    assert lines[-2] == "Next: Run the same command again to finish updating."
    assert lines[-1].startswith("Details: ")
    assert sum(line.startswith("Next: ") for line in lines) == 1
    assert all("did not finish" not in line for line in lines)
    assert outcome.exit_code == 3


def test_exit_code_contract_read_by_bootstraps_pinokio_and_journeys():
    """The codes every reader relies on (listed in PR #319): 0, 3, 2 for consent, 1."""
    from astrid.installer.outcome import Outcome

    codes = {(kind, reason): Outcome(kind, 1, 1, 2, reason).exit_code for kind, reason in (
        ("updated", None), ("already-current", None), ("repaired", None),
        ("not-updated", "installer-bug"), ("not-updated", "declined"),
        ("not-updated", "recovered-previous-version"), ("not-updated", "consent-required"),
        ("failed", "refused"), ("rolled-back", "error"))}
    assert codes == {
        ("updated", None): 0, ("already-current", None): 0, ("repaired", None): 0,
        ("not-updated", "installer-bug"): 3, ("not-updated", "declined"): 3,
        ("not-updated", "recovered-previous-version"): 3, ("not-updated", "consent-required"): 2,
        ("failed", "refused"): 1, ("rolled-back", "error"): 1}


def _interrupted_update(c, monkeypatch):
    """A pending C2 journal whose replay restores the running version (Release 1)."""
    from astrid.installer.core import write_json

    journal = c.root / "update-transaction.json"
    write_json(journal, {"format": "astrid-install-update/v1", "operation_id": "a" * 32,
                         "transaction_kind": "migration", "step": "stopping",
                         "data_root": str(c.destination)})
    replayed = []

    def recover(self):
        replayed.append(self.data)
        write_json(journal, {**read_json(journal), "step": "rolled_back"})

    monkeypatch.setattr(Installer, "_recover_update", recover)
    return replayed


def test_plain_install_that_recovers_an_interrupted_update_is_repaired(case, monkeypatch, capsys):
    """A plain install (or Pinokio finishing an update) targets "selected version live and healthy"."""
    c = handed_off_release1_r0_live(case, monkeypatch)
    _cli(c, monkeypatch)
    replayed = _interrupted_update(c, monkeypatch)
    code = cli.main(["--root", str(c.root), "--manifest", str(c.manifest_path(2)), "--uv", sys.executable])
    out = capsys.readouterr().out
    assert replayed == [c.destination] and current(c.root).release == 1
    assert code == 0, out
    assert "Astrid is installed and running (Release 1)." in out
    assert "An earlier update was interrupted" in out
    receipt = read_json(platform.log_dir() / "last-run.json")
    assert (receipt["kind"], receipt["reason"], receipt["still"], receipt["exit_code"]) == (
        "repaired", "recovered-previous-version", 1, 0)


def test_update_that_only_recovers_an_interrupted_update_is_not_updated(case, monkeypatch, capsys):
    """--update targets the manifest release, so a recovery-only run exits 3."""
    c = handed_off_release1_r0_live(case, monkeypatch)
    _cli(c, monkeypatch)
    replayed = _interrupted_update(c, monkeypatch)
    code = _update(c, 2)
    lines = _summary(capsys.readouterr().out)
    assert replayed == [c.destination] and current(c.root).release == 1
    assert code == 3
    assert "Astrid did NOT update. It is still Release 1." in lines
    assert any(line.startswith("Why: an earlier update was interrupted") for line in lines)
    receipt = read_json(platform.log_dir() / "last-run.json")
    assert (receipt["kind"], receipt["reason"], receipt["still"], receipt["exit_code"]) == (
        "not-updated", "recovered-previous-version", 1, 3)


# --- S review of #319 ---------------------------------------------------------

def _alive(pid):
    from astrid.installer.installation import _process_alive

    return _process_alive(pid)


def test_known_r0_leftover_is_stopped_its_files_kept_and_the_update_proceeds(case, monkeypatch, capsys):
    """Hannah's R0-era Runtime, running again under R0's own label, is retired first."""
    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="label")
    _cli(c, monkeypatch)
    leftover = c.r0_process.pid
    r0_files = snapshot(c.data)
    assert _alive(leftover)
    code = _update(c, 2)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "Stopped the earlier Astrid copy that was running again; its files were kept." in out
    c.r0_process.wait(timeout=30)
    assert not _alive(leftover)
    calls = c.state()["calls"]
    assert calls.index(["--user", "disable", "--now", "astrid.service"]) < calls.index(
        ["--user", "stop", "astrid-r1.service"])
    assert snapshot(c.data) == r0_files  # files kept, including its stale discovery
    receipt = read_json(platform.log_dir() / "last-run.json")
    assert (receipt["kind"], receipt["still"], receipt["target"], receipt["exit_code"]) == ("updated", 2, 2, 0)


def test_unknown_second_runtime_is_refused_before_anything_is_stopped(case, monkeypatch, capsys):
    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="unknown")
    _cli(c, monkeypatch)
    calls = len(c.state()["calls"])
    code = _update(c, 2)
    lines = _summary(capsys.readouterr().out)
    assert code == 3
    assert "Why: two copies of Astrid are running for the same workspace." in lines
    assert _alive(c.r0_process.pid) and current(c.root).release == 1
    mutations = [call for call in c.state()["calls"][calls:]
                 if call[1] in {"stop", "start", "disable", "enable"}]
    assert mutations == []
    receipt = read_json(platform.log_dir() / "last-run.json")
    assert (receipt["kind"], receipt["reason"], receipt["exit_code"]) == ("not-updated", "other-runtime-running", 3)
    c.r0_process.kill()


def test_real_live_probe_reads_runtime_health_identity(case, monkeypatch, capsys):
    """The real probe path (no stub): Runtime health identity served by a stand-in owner."""
    import http.server
    import threading

    from astrid.installer import installation
    from astrid_runtime.runtime_boundary import WIRE_PROTOCOL

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    _cli(c, monkeypatch)

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if self.path != "/v1/health" or c.state()["r1"] != "active":
                self.send_error(503)
                return
            body = json.dumps({"protocol": WIRE_PROTOCOL, "status": "ok", "runtime_instance_id": "r1-live",
                               "identity": {"release": c.started_release, "managed_by": None}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        discovery = c.destination / "runtime" / "discovery.json"
        import os

        discovery.write_text(json.dumps({
            "endpoint": f"http://127.0.0.1:{server.server_port}", "runtime_instance_id": "r1-live",
            "pid": os.getpid(), "process_birth_id": platform.process_identity(os.getpid())["birth_id"]}))
        monkeypatch.setattr(installation, "probe_live", installation.probe_runtime_identity)
        assert installation.probe_live(c.destination) == {"release": 1, "managed_by": None, "instance_id": "r1-live"}
        code = _update(c, 2)
        out = capsys.readouterr().out
    finally:
        server.shutdown()
        server.server_close()
    assert code == 0, out
    assert "Astrid updated: Release 1 → Release 2." in out
    receipt = read_json(platform.log_dir() / "last-run.json")
    assert (receipt["kind"], receipt["start"], receipt["still"], receipt["exit_code"]) == ("updated", 1, 2, 0)


def _launcher_admits(c, version):
    """Run the selected version's R1 launcher checks (everything before it starts the Runtime)."""
    import subprocess

    script = (version / "bin" / "runtime.py").read_text(encoding="utf-8")
    checks = script[:script.index("settings_path =")] + "print('ADMITTED')\n"
    result = subprocess.run([sys.executable, "-I", "-c", checks, "run", "--profile", "astrid",
                             "--data-root", str(c.destination)], capture_output=True, text=True, timeout=60)
    return result.returncode == 0 and result.stdout.strip() == "ADMITTED", result.stdout + result.stderr


def test_launcher_admits_r1_when_normalisation_was_cut_between_marker_and_selection(case, monkeypatch):
    from astrid.installer import boundary_handoff as handoff, boundary_registration as registration

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    version = c.root / "versions/release1"
    publish = registration._publish

    def cut_after_marker(path, content, **options):
        publish(path, content, **options)
        if path == c.root / "install.json":
            raise KeyboardInterrupt("cut after the marker was published")

    monkeypatch.setattr(registration, "_publish", cut_after_marker)
    with pytest.raises(KeyboardInterrupt):
        handoff.normalise_completed(c.backend)
    assert "retained_r0_data_root" in read_json(c.root / "install.json")
    assert "r1_data_root" in selection(c)  # mixed: marker normalised, selection not yet
    admitted, output = _launcher_admits(c, version)
    assert admitted, output
    monkeypatch.setattr(registration, "_publish", publish)
    assert handoff.normalise_completed(c.backend)  # the next run finishes it
    admitted, output = _launcher_admits(c, version)
    assert admitted, output


def test_restore_archive_cut_between_link_and_unlink_is_finished_by_the_next_run(case, monkeypatch, capsys):
    import os

    from astrid.installer import boundary_handoff as handoff

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    _cli(c, monkeypatch)
    assert handoff.normalise_completed(c.backend)
    archive = handoff.archive_path(c.service)
    os.link(archive, c.tx.journal)  # restore_archive linked the journal, then was cut
    assert cli.main(["--root", str(c.root), "--rollback"]) == 0
    assert not archive.exists() and current(c.root).release == 0
    assert selection(c)["data_root"] == str(c.data)


def test_a_failing_run_after_a_success_leaves_no_stale_receipt(case, monkeypatch, capsys):
    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    _cli(c, monkeypatch)
    assert _update(c, 2) == 0
    record = platform.log_dir() / "last-run.json"
    first = read_json(record)
    assert first["kind"] == "updated" and first["operation_id"] and first["started_at"]

    def broken(*_args, **_kwargs):
        raise OSError("the installer could not start")

    monkeypatch.setattr(cli, "Backend", broken)
    assert cli.main(["--update", "--root", str(c.root), "--manifest", str(c.manifest_path(3))]) == 1
    assert not record.exists() or read_json(record)["operation_id"] != first["operation_id"]


def test_a_finished_local_update_journal_reads_as_direct(case, monkeypatch):
    from astrid.installer import boundary_handoff as handoff
    from astrid.installer.core import write_json
    from astrid.installer.installation import Installation

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    assert handoff.normalise_completed(c.backend)
    write_json(c.root / "update-transaction.json", {"format": "astrid-install-update/v1", "step": "complete",
                                                     "transaction_context": {"owner": "local-update"}})
    assert Installation.resolve(c.service.home, system="linux").kind == "direct"


def test_local_update_operates_the_r1_label_after_normalisation(case, monkeypatch):
    from astrid.installer import boundary_handoff as handoff, local_update

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    assert handoff.normalise_completed(c.backend)
    r0_unit = c.service.path
    before = r0_unit.read_bytes()
    backend = local_update._backend(c.root)
    assert backend.service.handed_off and backend.service.path.name == "astrid-r1.service"
    backend.service.publish_launch_admission(current(c.root), c.destination)  # the local update's first step
    assert r0_unit.read_bytes() == before


def test_retained_versions_keep_every_version_when_the_move_cannot_be_read(case, monkeypatch):
    from astrid.installer import boundary_handoff as handoff
    from astrid.installer.core import write_json
    from astrid.installer.installation import retained_versions

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    assert handoff.normalise_completed(c.backend)
    assert retained_versions(c.service, c.root) == {c.root / "versions/release0"}
    write_json(c.service.selection, {**selection(c), "retained_r0_data_root": "/elsewhere"})
    kept = retained_versions(c.service, c.root)
    assert c.root / "versions/release0" in kept and c.root / "versions/release1" in kept


def test_update_whose_runtime_was_not_restarted_reports_runtime_not_running(case, monkeypatch, capsys):
    """The selection advances to the target, but the live Runtime still runs the old release."""
    from astrid.installer.core import switch

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    _cli(c, monkeypatch)

    class SwitchOnly(Installer):
        def run(self, mode, manifest, **_kwargs):
            switch(self.root, c.backend.stage(manifest, self.root, self.data))

    monkeypatch.setattr(cli, "Installer", SwitchOnly)
    code = _update(c, 2)
    lines = _summary(capsys.readouterr().out)
    assert code == 3 and current(c.root).release == 2
    assert "Astrid did NOT update. It is still Release 1; Release 2 is available." in lines
    receipt = read_json(platform.log_dir() / "last-run.json")
    assert (receipt["kind"], receipt["reason"]) == ("not-updated", "runtime-not-running")


def test_handle_refuses_an_unnormalised_complete_journal_without_changes(case, monkeypatch):
    from astrid.installer import boundary_handoff as handoff

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    before = (c.tx.journal.read_bytes(), c.service.selection.read_bytes(), (c.root / "install.json").read_bytes())
    with pytest.raises(ValueError, match="must be finished by the install line first"):
        handoff.handle(c.root, c.data, c.backend)
    assert (c.tx.journal.read_bytes(), c.service.selection.read_bytes(),
            (c.root / "install.json").read_bytes()) == before


def test_normalisation_replaces_the_release1_launcher_before_rewriting_records(case, monkeypatch):
    from astrid.installer import boundary_handoff as handoff, boundary_registration as registration
    from tests.fixtures.release1_launcher import release1_launcher_script

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    version = current(c.root)
    script = version.root / "bin/runtime.py"
    assert script.read_text(encoding="utf-8") == release1_launcher_script(version)
    admitted, output = _launcher_admits(c, version.root)
    assert admitted, output  # Release 1's launcher admits the handoff's records
    order = []
    publish = registration._publish

    def recorded(path, content, **options):
        order.append(Path(path).name)
        return publish(path, content, **options)

    monkeypatch.setattr(registration, "_publish", recorded)
    assert handoff.normalise_completed(c.backend)
    assert order[:2] == ["runtime.py", "runtime"] and order.index("runtime.py") < order.index("install.json")
    assert script.read_text(encoding="utf-8") == registration.launcher_script(version)
    admitted, output = _launcher_admits(c, version.root)
    assert admitted, output  # the R1 service still starts on the normalised records


def test_normalisation_that_cannot_replace_the_launcher_changes_nothing(case, monkeypatch):
    from astrid.installer import boundary_handoff as handoff, boundary_registration as registration

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    version = current(c.root)
    records = (c.root / "install.json").read_bytes(), c.service.selection.read_bytes()
    monkeypatch.setattr(registration, "stage_runtime_launcher", lambda *a, **k: None)
    with pytest.raises(ValueError, match="launcher could not be replaced"):
        handoff.normalise_completed(c.backend)
    assert ((c.root / "install.json").read_bytes(), c.service.selection.read_bytes()) == records
    assert c.tx.journal.exists()
    admitted, output = _launcher_admits(c, version.root)
    assert admitted, output  # Release 1 keeps starting on the untouched records


def test_release1_launcher_refuses_normalised_records(case, monkeypatch):
    """Why the order matters: the fixture's Release 1 launcher detects a records-first normalisation."""
    from astrid.installer import boundary_handoff as handoff

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    version = current(c.root)
    value = read_json(c.tx.journal)
    for path, is_selection in ((c.root / "install.json", False), (c.service.selection, True)):
        path.write_text(json.dumps(handoff._normalise_record(read_json(path), value, selection=is_selection)))
    admitted, _output = _launcher_admits(c, version.root)
    assert not admitted


def test_every_non_zero_outcome_ends_with_next_and_details():
    """A Release 1 Runtime cannot show the receipt in the App: the terminal must say what to do."""
    from astrid.installer.outcome import REASONS, Outcome

    log = "/home/hannah/Library/Logs/Astrid Runtime/install.log"
    for kind in ("not-updated", "failed", "rolled-back"):
        for reason in (*REASONS, None):
            outcome = Outcome(kind, 1, 1, 2, reason, "update", log)
            lines = outcome.headline()
            assert outcome.exit_code != 0, (kind, reason)
            assert lines[-2].startswith("Next: ") and lines[-1] == f"Details: {log}", (kind, reason, lines)
            assert sum(line.startswith("Next: ") for line in lines) == 1
        rolled = Outcome("rolled-back", 1, 1, 2, "error", "update", log).headline()
        assert rolled[-3].startswith("Run the same command again")
    console = Outcome("failed", None, None, 2, "error").headline()
    assert console[-1] == "Details: shown above in this window (the installer log was not used)."
    for kind in ("updated", "already-current", "repaired"):
        assert not any(line.startswith("Next: ")
                       for line in Outcome(kind, 2, 1, 2, None, "update", log).headline())


SUPPORT = "Next: open astrid.haus > Settings > Updates > Download diagnostics, and send it to support."
# Owner rule (Hannah): every failure names its own next action; only a bug or an
# unexplained error sends the person to support.
EXPECTED_NEXT = {
    "self-update-too-old": "Next: run the install command once; after that Update now works.",
    "disk-space": "Next: free up at least the amount named above, then run the same command again.",
    "busy": "Next: quit the other Astrid window (or let the other installer finish), "
            "then run the same command again.",
    "needs-repair": "Next: run the install command without --update to repair this installation, "
                    "then update again.",
    "other-runtime-running": "Next: stop the other Astrid, then run the same command again.",
    "windows-upgrade-unsupported": "Next: this Windows version can't be upgraded this way. "
                                   "Open astrid.haus > Settings > Updates > Download diagnostics "
                                   "to see what is installed.",
    "autostart-off": "Next: run the update command from a terminal. It updates Astrid and starts it, "
                     "and your choice stays off.",
    "unsupported-service-manager": "Next: run the update command from a terminal instead.",
    "acknowledgement-unknown": "Next: wait a minute, then run the same update command again. It checks the "
                               "earlier attempt before starting a new one.",
    "legacy-update-record": "Next: run the update command from a terminal to start a fresh update.",
    "pinokio-managed": "Next: use the Update button in Pinokio.",
    "source-checkout": "Next: update the source checkout, then run the same command again.",
    "consent-required": "Next: run the install command again and accept the setup choices, "
                        "or answer the prompts.",
    "declined": "Next: nothing to do. To install later, run the install command again "
                "and accept the setup choices.",
    "recovered-previous-version": "Next: Run the same command again to finish updating.",
    "runtime-not-running": "Next: run the same command again so Astrid restarts on the new release.",
    "refused": "Next: fix what the message above names, then run the same command again.",
    "error": SUPPORT,
    "installer-bug": SUPPORT,
}


@pytest.mark.parametrize("reason", sorted(EXPECTED_NEXT))
def test_each_reason_has_its_own_next_step(reason):
    """Plan 29 R11 card 1: the Next line is the reason's own action; support only for bugs and errors."""
    from astrid.installer.outcome import REASONS, Outcome

    assert set(REASONS) == set(EXPECTED_NEXT)  # every reason in the enum is covered, none invented
    log = "/tmp/install.log"
    for kind in ("not-updated", "failed", "rolled-back"):
        lines = Outcome(kind, 1, 1, 2, reason, "update", log).headline()
        assert lines[-2:] == [EXPECTED_NEXT[reason], f"Details: {log}"], (kind, lines)
        mentions_support = any("to support" in line for line in lines)
        assert mentions_support == (reason in {"installer-bug", "error"}), (kind, lines)


def _local_update(c, monkeypatch, capsys, release):
    """Run ``python -m astrid.installer.local_update`` in-process from the field state."""
    import io

    from astrid.installer import local_update
    from tests.test_installer_local_update import _app

    hooks = []

    class Hooks:
        def __init__(self, app, _stdin, _stdout):
            self.app = app

        def call(self, name, version, _realm, **_options):
            hooks.append((name, version.release if version else None))

    manifest = {**json.loads(c.manifest_path(release).read_text()),
                "agent": {"sha": "b" * 40}, "runtime": {"sha": "c" * 40}}
    app = {**_app(c.root.parent), "release": release, "manifest": manifest}
    identities = []
    monkeypatch.setattr(local_update, "Backend", lambda *a, **kw: c.backend)
    monkeypatch.setattr(local_update, "validate_manifest", lambda value: value)
    monkeypatch.setattr(local_update, "HookClient", Hooks)
    monkeypatch.setattr(local_update, "_verify_selected_identity",
                        lambda _backend, version, data, *_a: identities.append((version.release, data)))
    monkeypatch.setattr(c.backend, "stage", stage_release(c, release))
    c.migrated = []

    def backup(_version, data, _realm):
        c.migrated.append(("backup", data))
        (c.root / "backups/local").mkdir(parents=True)
        return {"root": str(c.root / "backups/local"), "kind": "runtime"}

    monkeypatch.setattr(c.backend, "backup", backup)
    monkeypatch.setattr(c.backend, "upgrade", lambda _version, data: c.migrated.append(("upgrade", data)))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"action": "update", "app": app,
                                                              "manifest": manifest}) + "\n"))
    code = local_update.main(["--root", str(c.root)])
    reply = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    return code, reply, hooks, identities


def test_local_update_normalises_a_finished_handoff_and_updates_r1(case, monkeypatch, capsys):
    """UR-15l-1 (a1): the fully local Update now never runs against R0's data."""
    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    r0_before = snapshot(c.data)
    r0_unit = c.service.path
    r0_unit_bytes = r0_unit.read_bytes()
    code, reply, hooks, identities = _local_update(c, monkeypatch, capsys, 2)
    assert code == 0, reply
    assert reply["result"]["status"] == "updated" and reply["result"]["release"] == 2
    assert current(c.root).release == 2 and c.verified_releases == [2]
    assert identities == [(2, c.destination)]
    assert c.migrated and all(data == c.destination for _step, data in c.migrated), c.migrated
    assert ("start-editor", 2) in hooks
    # Normalised once, as the installer does: R1 is the data root, R0 provenance only.
    selected = selection(c)
    assert selected["data_root"] == str(c.destination)
    assert selected["realm_root"] == str(c.destination / "runtime/realms/r0")
    assert selected["retained_r0_data_root"] == str(c.data)
    assert read_json(c.root / "install.json")["data_root"] == str(c.destination)
    assert not c.tx.journal.exists()
    assert read_json(c.tx.journal.with_name("astrid-r1-handoff.completed.json"))["phase"] == "complete"
    assert read_json(c.root / "update-transaction.json")["data_root"] == str(c.destination)
    # R0's retained data and its login definition were never written.
    assert snapshot(c.data) == r0_before
    assert r0_unit.read_bytes() == r0_unit_bytes and c.service.path.name == "astrid-r1.service"
    assert ["--user", "start", "astrid-r1.service"] in c.state()["calls"]


def _untouched(c):
    return (c.tx.journal.read_bytes(), c.service.selection.read_bytes(),
            (c.root / "install.json").read_bytes(), (c.root / "current").read_bytes(),
            sorted(path.name for path in (c.root / "versions").iterdir()),
            (c.root / "update-transaction.json").exists(), snapshot(c.data), snapshot(c.destination))


def test_local_update_refuses_when_the_handoff_cannot_be_normalised(case, monkeypatch, capsys):
    from astrid.installer import boundary_registration as registration

    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    monkeypatch.setattr(registration, "stage_runtime_launcher", lambda *a, **k: None)
    before = _untouched(c)
    code, reply, hooks, identities = _local_update(c, monkeypatch, capsys, 2)
    assert code == 3, reply
    assert reply["code"] == "needs-repair" and reply["step"] == "normalise-handoff"
    assert "Run the install line once" in reply["error"]
    assert hooks == [] and identities == [] and c.staged == [] and c.migrated == []
    assert _untouched(c) == before


def test_local_update_refuses_an_unfinished_handoff_without_changes(case, monkeypatch, capsys):
    c = handed_off_release1_r0_live(case, monkeypatch, r0_runtime="none")
    journal = read_json(c.tx.journal)
    c.tx.journal.write_text(json.dumps({**journal, "phase": "switching"}))
    before = _untouched(c)
    code, reply, hooks, _identities = _local_update(c, monkeypatch, capsys, 2)
    assert code == 3, reply
    assert reply["code"] == "needs-repair" and "storage move" in reply["error"]
    assert hooks == [] and c.staged == []
    assert _untouched(c) == before
