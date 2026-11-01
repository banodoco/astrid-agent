"""Worker-produced evidence at the claim/bootstrap interruption boundaries."""
from __future__ import annotations

import pytest

from astrid.installer import update_job
from astrid.installer.core import Version, read_json, write_json
from tests.test_installer_update_continuation import _ContinuationAdapter
from tests.test_installer_update_job import _fixture
from tests.test_installer_update_reconcile import _unfinished_journal


def test_initial_native_launch_excludes_a_competing_reconciliation(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    accepted = update_job.accept(request)

    class Adapter(_ContinuationAdapter):
        def launch(self, descriptor, command, log_path):
            super().launch(descriptor, command, log_path)
            with pytest.raises(ValueError) as refusal:
                update_job.accept(request)
            assert refusal.value.reason == "busy"

    adapter = Adapter(root)
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    result = update_job.launch_job(root, accepted["operation_id"])
    assert result["launch_status"] == "acknowledged"
    assert result["operation_id"] == accepted["operation_id"]
    assert adapter.events == ["launch"]


def test_real_worker_claim_survives_interruption_and_can_continue(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    original = update_job.accept(request)
    adapter = _ContinuationAdapter(root)
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    # The explicit continuation supplies the previously missing descriptor.
    acknowledged = update_job.accept(request)
    assert acknowledged["launch_status"] == "acknowledged"
    validate = update_job._validate_worker_binding

    def interrupted(*_args):
        proof = read_json(root / "install.json")["update_job"]["bootstrap_proof"]
        assert proof["state"] == "never-entered"
        assert proof["claim_state"] == "claimed"
        assert proof["native_claim"]["descriptor"] == acknowledged["platform"]
        raise SystemExit("worker vanished after durable claim")

    monkeypatch.setattr(update_job, "_validate_worker_binding", interrupted)
    with pytest.raises(SystemExit):
        update_job.run_job(root, original["operation_id"])
    claimed = read_json(root / "install.json")["update_job"]
    assert claimed["state"] == "active"
    monkeypatch.setattr(update_job, "_validate_worker_binding", validate)
    # Positive inactivity is supplied by the same native adapter. No manual
    # proof fabrication: this is the claim produced by run_job itself.
    result = update_job.accept(request)
    assert result["operation_id"] == original["operation_id"]
    assert result["bootstrap_proof"]["claim_state"] == "unclaimed"
    assert result["bootstrap_proof"].get("native_claim") is None
    assert adapter.events.count("launch") == 2  # Two explicit requests.


def test_worker_marks_entry_durably_before_releasing_installer_lock(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    accepted = update_job.accept(request)
    adapter = _ContinuationAdapter(root)
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    update_job.accept(request)
    unlock = update_job._unlock
    observed = []

    def inspect_unlock(stream):
        saved = read_json(root / "install.json")["update_job"]
        observed.append(saved["bootstrap_proof"]["state"])
        assert saved["bootstrap_proof"]["state"] == "entered"
        unlock(stream)

    monkeypatch.setattr(update_job, "_unlock", inspect_unlock)

    def bootstrap(*_args, **_kwargs):
        assert observed == ["entered"]
        stream = update_job._locked(root)
        unlock(stream)
        raise SystemExit("bootstrap acknowledgement vanished")

    monkeypatch.setattr(update_job.subprocess, "run", bootstrap)
    with pytest.raises(SystemExit):
        update_job.run_job(root, accepted["operation_id"])
    monkeypatch.setattr(update_job, "_unlock", unlock)
    before = (root / "install.json").read_bytes()
    launches = adapter.events.count("launch")
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert refusal.value.reason == "busy"
    assert adapter.events.count("launch") == launches
    assert (root / "install.json").read_bytes() == before


def test_entered_proof_with_missing_matching_transaction_never_resumes(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    accepted = update_job.accept(request)
    marker = read_json(root / "install.json")
    marker["update_job"]["bootstrap_proof"]["state"] = "entered"
    marker["update_job"]["transaction_id"] = "c" * 32
    write_json(root / "install.json", marker)
    adapter = _ContinuationAdapter(root)
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert refusal.value.reason == "busy"
    assert "launch" not in adapter.events
    assert read_json(root / "install.json")["update_job"]["operation_id"] == accepted["operation_id"]


def test_legacy_matching_transaction_keeps_m1_9a_reconciliation_path(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    adapter = _ContinuationAdapter(root)
    descriptor = adapter.describe(job, update_job._expected_worker_command(Version.load(job["old_version"]), job))
    journal = _unfinished_journal(job)
    marker = read_json(root / "install.json")
    saved = marker["update_job"]
    for field in ("worker_command_sha256", "bootstrap_proof", "prior_transaction_present",
                  "prior_transaction_sha256"):
        saved.pop(field)
    saved.update(state="recovery-needed", transaction_id=journal["operation_id"], platform=descriptor)
    write_json(root / "install.json", marker)
    write_json(root / "update-transaction.json", journal)
    calls = []

    def recover(selected_root, prior, transaction):
        with pytest.raises(ValueError):
            update_job._locked(selected_root)
        calls.append(transaction["operation_id"])
        return {**prior, "state": "rolled_back"}

    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    monkeypatch.setattr(update_job, "_recover_matching_transaction", recover)
    result = update_job.accept(request)
    assert result["operation_id"] == job["operation_id"]
    assert result["state"] == "rolled_back"
    assert calls == [journal["operation_id"]]
    assert adapter.events == ["liveness"]


@pytest.mark.parametrize("mutation", [None, "missing-presence", "false-presence", "changed-journal"])
def test_preexisting_terminal_journal_requires_exact_accepted_snapshot(tmp_path, monkeypatch, mutation):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    prior = {"operation_id": "d" * 32, "step": "complete"}
    journal_path = root / "update-transaction.json"
    write_json(journal_path, prior)
    accepted = update_job.accept(request)
    marker = read_json(root / "install.json")
    marker["update_job"]["state"] = "recovery-needed"
    if mutation == "missing-presence":
        marker["update_job"].pop("prior_transaction_present")
    elif mutation == "false-presence":
        marker["update_job"]["prior_transaction_present"] = False
    elif mutation == "changed-journal":
        write_json(journal_path, {**prior, "outcome": "changed-after-acceptance"})
    write_json(root / "install.json", marker)
    before_marker = (root / "install.json").read_bytes()
    before_journal = journal_path.read_bytes()
    adapter = _ContinuationAdapter(root, registered=False)
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    if mutation:
        with pytest.raises(ValueError) as refusal:
            update_job.accept(request)
        assert refusal.value.reason == "busy"
        assert (root / "install.json").read_bytes() == before_marker
        assert adapter.events == []
    else:
        result = update_job.accept(request)
        assert result["operation_id"] == accepted["operation_id"]
        assert result["launch_status"] == "acknowledged"
        assert adapter.events.count("launch") == 1
    assert journal_path.read_bytes() == before_journal
