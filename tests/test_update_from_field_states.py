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
from runtime_protocol import platform

from astrid.installer import cli
from astrid.installer.core import Installer, current, read_json
from tests.fixtures.field_states import R0_INSTANCE, handed_off_release1_r0_live, selection
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
