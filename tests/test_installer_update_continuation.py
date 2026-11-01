"""M1-9b boundaries for continuing one accepted installer operation."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_protocol import platform as runtime_platform

from astrid.installer import update_job
from astrid.installer.core import Version, read_json, write_json
from tests.test_installer_update_job import _fixture


def _descriptor(job, command):
    """Exact Linux test descriptor, including the accepted roots and argv."""
    name = f"astrid-update-{job['operation_id']}.service"
    return {
        "format": "astrid-update-platform/v1",
        "system": "linux",
        "kind": "systemd-user-transient",
        "name": name,
        "unit": name,
        "operation_id": job["operation_id"],
        "user": "123",
        "command": list(command),
        "program_root": job["program_root"],
        "data_root": job["data_root"],
        "log_path": job["log_path"],
    }


def _proof(job, *, claim_state=None, descriptor=None, state="never-entered"):
    proof = {
        "format": "astrid-update-bootstrap-proof/v1",
        "operation_id": job["operation_id"],
        "program_root": job["program_root"],
        "data_root": job["data_root"],
        "owner": "haus.astrid.runtime",
        "state": state,
        "claim_state": claim_state,
    }
    if claim_state == "claimed":
        proof["native_claim"] = {
            "operation_id": job["operation_id"],
            "program_root": job["program_root"],
            "data_root": job["data_root"],
            "owner": "haus.astrid.runtime",
            "descriptor": descriptor,
        }
    return proof


def _set_unresolved(root, job, proof_state, descriptor=None):
    marker = read_json(root / "install.json")
    saved = marker["update_job"]
    saved.update(state="recovery-needed", launch_status="unknown",
                 bootstrap_proof=_proof(job,
                                        claim_state=("claimed" if proof_state == "claimed" else "unclaimed"),
                                        descriptor=descriptor))
    if descriptor is not None:
        saved["platform"] = descriptor
    marker["update_job"] = saved
    write_json(root / "install.json", marker)


class _ContinuationAdapter:
    def __init__(self, root, *, active=False, registered=True, ack=True):
        self.root = root
        self.active = active
        self.registered = registered
        self.ack = ack
        self.events = []
        self.expected = None

    def describe(self, job, command):
        self.expected = _descriptor(job, command)
        return self.expected

    def is_registered(self, descriptor, command):
        assert descriptor == self.expected
        assert list(command) == descriptor["command"]
        _assert_lock_busy(self.root)
        self.events.append("registration")
        return self.registered

    def is_active(self, descriptor):
        assert descriptor == self.expected
        _assert_lock_busy(self.root)
        self.events.append("liveness")
        if self.active == "unreadable":
            raise OSError("native job state unavailable")
        return self.active

    def cleanup(self, descriptor):
        assert descriptor == self.expected
        _assert_lock_busy(self.root)
        self.events.append("retire")
        self.registered = False

    def launch(self, descriptor, command, log_path):
        assert descriptor == self.expected
        assert list(command) == descriptor["command"]
        assert str(log_path) == descriptor["log_path"]
        _assert_lock_busy(self.root)
        self.events.append("launch")
        if not self.ack:
            raise RuntimeError("continuation acknowledgement lost")


def _assert_lock_busy(root):
    with pytest.raises(ValueError, match="already running"):
        update_job._locked(root)


@pytest.mark.parametrize(
    ("case", "proof_state", "has_descriptor", "expected_events"),
    [
        ("accepted-without-descriptor", "never-entered", False,
         ["registration", "launch"]),
        ("accepted-without-descriptor-existing-registration", "never-entered", False,
         ["registration", "liveness", "retire", "registration", "launch"]),
        ("registered-without-ack", "never-entered", True,
         ["registration", "liveness", "retire", "registration", "launch"]),
        ("claimed-without-bootstrap", "claimed", True,
         ["registration", "liveness", "retire", "registration", "launch"]),
    ],
)
def test_explicit_update_continues_same_operation_once_under_lock(
    tmp_path, monkeypatch, case, proof_state, has_descriptor, expected_events
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    original = update_job.accept(request)
    command = update_job._expected_worker_command(Version.load(original["old_version"]), original)
    descriptor = _descriptor(original, command) if has_descriptor else None
    _set_unresolved(root, original, proof_state, descriptor)
    adapter = _ContinuationAdapter(root, registered=(case != "accepted-without-descriptor"))
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    validations = []
    validate = update_job._validate_request

    def count_admission(*args, **kwargs):
        validations.append(True)
        return validate(*args, **kwargs)

    monkeypatch.setattr(update_job, "_validate_request", count_admission)
    result = update_job.accept(request)

    assert result["operation_id"] == original["operation_id"]
    assert result["state"] == "launching"
    assert result["launch_status"] == "acknowledged"
    assert adapter.events == expected_events
    assert validations == []  # continuation never performs a second logical acceptance
    assert read_json(root / "install.json")["update_job"]["operation_id"] == original["operation_id"]
    assert read_json(root / "install.json")["update_job"]["bootstrap_proof"]["state"] == "never-entered"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing-proof", "ambiguous-proof", "claim-operation", "claim-root",
        "claim-owner", "claim-data-root", "missing-native-claim", "claim-descriptor",
        "entered-without-journal", "descriptor-operation",
        "descriptor-root", "descriptor-command", "live-owner", "unknown-owner",
        "private-inputs",
    ],
)
def test_unproved_or_mismatched_prebootstrap_evidence_refuses_and_preserves_records(
    tmp_path, monkeypatch, mutation
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    command = update_job._expected_worker_command(Version.load(job["old_version"]), job)
    descriptor = _descriptor(job, command)
    _set_unresolved(root, job, "claimed", descriptor)
    marker = read_json(root / "install.json")
    saved = marker["update_job"]
    proof = saved["bootstrap_proof"]
    if mutation == "missing-proof":
        saved.pop("bootstrap_proof")
    elif mutation == "ambiguous-proof":
        proof["state"] = "unknown"
    elif mutation == "claim-operation":
        proof["operation_id"] = "b" * 32
    elif mutation == "claim-root":
        proof["program_root"] = str(root / "foreign-program")
    elif mutation == "claim-owner":
        proof["owner"] = "other.owner"
    elif mutation == "claim-data-root":
        proof["data_root"] = str(root / "foreign-data")
    elif mutation == "missing-native-claim":
        proof.pop("native_claim")
    elif mutation == "claim-descriptor":
        proof["native_claim"]["descriptor"]["command"] = ["/bin/other-worker"]
    elif mutation == "entered-without-journal":
        proof["state"] = "entered"
    elif mutation == "descriptor-operation":
        saved["platform"]["operation_id"] = "b" * 32
    elif mutation == "descriptor-root":
        saved["platform"]["program_root"] = str(root / "foreign-program")
    elif mutation == "descriptor-command":
        saved["platform"]["command"] = ["/bin/other-worker"]
    elif mutation == "private-inputs":
        with open(request["metadata"]["bootstrap_path"], "a", encoding="utf-8") as stream:
            stream.write("# changed after acceptance\n")
    marker["update_job"] = saved
    write_json(root / "install.json", marker)
    before_marker = (root / "install.json").read_bytes()
    before_journal = ((root / "update-transaction.json").read_bytes()
                      if (root / "update-transaction.json").exists() else None)

    class Adapter(_ContinuationAdapter):
        def describe(self, value, actual_command):
            self.expected = _descriptor(value, actual_command)
            return self.expected

        def is_active(self, value):
            return False

        def is_registered(self, *_args):
            return False

        def launch(self, *_args):
            pytest.fail("invalid durable binding must never launch")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter(root))
    if mutation == "live-owner":
        monkeypatch.setattr(update_job, "_probe_identity",
                            lambda _data: ("different-instance", {"release": 7, "managed_by": None}))
    elif mutation == "unknown-owner":
        monkeypatch.setattr(update_job, "_probe_identity",
                            lambda _data: (_ for _ in ()).throw(OSError("Runtime identity unavailable")))
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert getattr(refusal.value, "reason", None) == "busy"
    assert (root / "install.json").read_bytes() == before_marker
    after_journal = ((root / "update-transaction.json").read_bytes()
                     if (root / "update-transaction.json").exists() else None)
    assert after_journal == before_journal


@pytest.mark.parametrize("liveness", [True, "unreadable"])
@pytest.mark.parametrize("proof_state,has_descriptor", [
    ("never-entered", False), ("never-entered", True), ("claimed", True),
])
def test_live_or_unknown_native_job_refuses_without_retirement_or_launch(
    tmp_path, monkeypatch, liveness, proof_state, has_descriptor
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    command = update_job._expected_worker_command(Version.load(job["old_version"]), job)
    descriptor = _descriptor(job, command) if has_descriptor else None
    _set_unresolved(root, job, proof_state, descriptor)
    before_marker = (root / "install.json").read_bytes()
    before_journal = (root / "update-transaction.json").read_bytes() if (root / "update-transaction.json").exists() else None
    adapter = _ContinuationAdapter(root, active=liveness)
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert getattr(refusal.value, "reason", None) == "busy"
    assert adapter.events == ["registration", "liveness"]
    assert (root / "install.json").read_bytes() == before_marker
    after_journal = (root / "update-transaction.json").read_bytes() if (root / "update-transaction.json").exists() else None
    assert after_journal == before_journal


def test_unrelated_journal_refuses_valid_prebootstrap_proof_and_preserves_all_evidence(
    tmp_path, monkeypatch
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    _set_unresolved(root, job, "never-entered")
    write_json(root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "d" * 32,
        "step": "stopping", "data_root": job["data_root"],
    })
    before_marker = (root / "install.json").read_bytes()
    before_journal = (root / "update-transaction.json").read_bytes()
    adapter = _ContinuationAdapter(root, registered=False)

    def no_registration_query(*_args):
        pytest.fail("an unrelated journal must block before registration inspection")

    adapter.is_registered = no_registration_query
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert getattr(refusal.value, "reason", None) == "busy"
    assert (root / "install.json").read_bytes() == before_marker
    assert (root / "update-transaction.json").read_bytes() == before_journal
    assert adapter.events == []


@pytest.mark.parametrize("mutation", [
    "worker-command-digest", "autostart-off", "active-unclaimed", "unclaimed-native-claim",
    "metadata-release", "log-outside", "log-symlink", "log-not-private",
    "missing-prior-presence", "missing-prior-digest", "transaction-id-without-journal",
    "entered-without-journal",
])
def test_new_durable_guards_refuse_a_single_invalid_prebootstrap_fact(
    tmp_path, monkeypatch, mutation
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    _set_unresolved(root, job, "never-entered")
    marker = read_json(root / "install.json")
    saved = marker["update_job"]
    proof = saved["bootstrap_proof"]
    if mutation == "worker-command-digest":
        saved["worker_command_sha256"] = "f" * 64
    elif mutation == "autostart-off":
        selection_path = update_job._selection_path()
        selection = read_json(selection_path)
        selection["preferences"] = {"autostart": False}
        write_json(selection_path, selection)
    elif mutation == "active-unclaimed":
        saved["state"] = "active"
    elif mutation == "unclaimed-native-claim":
        proof["native_claim"] = {
            "operation_id": job["operation_id"], "program_root": job["program_root"],
            "data_root": job["data_root"], "owner": "haus.astrid.runtime",
        }
    elif mutation == "metadata-release":
        saved["metadata"]["release"] = saved["target_release"] + 1
    elif mutation == "log-outside":
        saved["log_path"] = str(root.parent / "foreign-update.log")
        old = Version.load(saved["old_version"])
        saved["worker_command_sha256"] = update_job._worker_binding_digest(old, saved)
    elif mutation == "log-symlink":
        log_path = Path(saved["log_path"])
        target = root.parent / "log-target"
        target.write_text("log target\n", encoding="utf-8")
        log_path.unlink()
        log_path.symlink_to(target)
    elif mutation == "log-not-private":
        Path(saved["log_path"]).chmod(0o644)
    elif mutation == "missing-prior-presence":
        saved.pop("prior_transaction_present")
    elif mutation == "missing-prior-digest":
        saved.pop("prior_transaction_sha256")
    elif mutation == "transaction-id-without-journal":
        saved["transaction_id"] = "c" * 32
    elif mutation == "entered-without-journal":
        proof["state"] = "entered"
    marker["update_job"] = saved
    write_json(root / "install.json", marker)
    before_marker = (root / "install.json").read_bytes()

    class Adapter:
        def describe(self, value, command):
            return _descriptor(value, command)

        def is_registered(self, *_args):
            return False

        def is_active(self, *_args):
            pytest.fail("an invalid durable fact must not reach a liveness query")

        def cleanup(self, *_args, **_kwargs):
            pytest.fail("invalid durable evidence must never retire a native object")

        def launch(self, *_args, **_kwargs):
            pytest.fail("invalid durable evidence must never launch")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert getattr(refusal.value, "reason", None) == "busy"
    assert (root / "install.json").read_bytes() == before_marker
    assert not (root / "update-transaction.json").exists()


def test_failed_native_retirement_does_not_launch_or_change_durable_claim(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    command = update_job._expected_worker_command(Version.load(job["old_version"]), job)
    descriptor = _descriptor(job, command)
    _set_unresolved(root, job, "never-entered", descriptor)
    before_marker = (root / "install.json").read_bytes()
    events = []

    class Adapter(_ContinuationAdapter):
        def cleanup(self, value):
            assert value == self.expected
            _assert_lock_busy(root)
            events.append("cleanup")
            raise OSError("native cleanup unavailable")

        def launch(self, *_args):
            events.append("launch")
            pytest.fail("failed retirement must prevent replacement launch")

    adapter = Adapter(root, registered=True)
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert getattr(refusal.value, "reason", None) == "busy"
    assert events == ["cleanup"]
    assert (root / "install.json").read_bytes() == before_marker


def test_changed_windows_worker_script_refuses_before_retirement_and_preserves_bytes(
    tmp_path, monkeypatch
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(update_job, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(update_job, "_selection_path", lambda: Path(request["selection_path"]))
    job = update_job.accept(request)
    _set_unresolved(root, job, "never-entered")
    epoch, nonce = update_job._attempt_tuple(job)
    worker_script = (Path(job["metadata"]["manifest_path"]).parent
                     / f"worker-{job['operation_id']}-{epoch}-{nonce}.py")
    worker_script.write_bytes(b"# foreign or stale worker bytes\n")
    runtime_platform.owner_only(worker_script)
    before_script = worker_script.read_bytes()
    before_marker = (root / "install.json").read_bytes()
    events = []
    name = f"astrid-update-{job['operation_id']}"

    class Adapter:
        def describe(self, value, command):
            return {
                "format": "astrid-update-platform/v1", "system": "win32",
                "kind": "scheduled-task-on-demand", "name": name, "task_name": name,
                "user": "S-1-5-21-12345", "operation_id": value["operation_id"],
                "program_root": value["program_root"], "log_path": value["log_path"],
                "command": list(command),
            }

        def is_registered(self, *_args):
            events.append("registration")
            pytest.fail("changed Windows worker must be rejected before native inspection")

        def cleanup(self, *_args, **_kwargs):
            events.append("cleanup")
            pytest.fail("changed Windows worker must be rejected before retirement")

        def launch(self, *_args, **_kwargs):
            events.append("launch")
            pytest.fail("changed Windows worker must never launch")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert getattr(refusal.value, "reason", None) == "busy"
    assert events == []
    assert worker_script.read_bytes() == before_script
    assert (root / "install.json").read_bytes() == before_marker


def test_changed_windows_profile_source_before_script_handoff_invalidates_command_binding(
    tmp_path, monkeypatch
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(update_job, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(update_job, "_selection_path", lambda: Path(request["selection_path"]))
    job = update_job.accept(request)
    _set_unresolved(root, job, "never-entered")
    profile = Path(job["old_version"]["profile"])
    source_checkout = tmp_path / "new source checkout"
    source_checkout.mkdir()
    write_json(profile, {"profile": "astrid", "runtime_checkout": str(source_checkout)})
    before_marker = (root / "install.json").read_bytes()
    epoch, nonce = update_job._attempt_tuple(job)
    worker_script = (Path(job["metadata"]["manifest_path"]).parent
                     / f"worker-{job['operation_id']}-{epoch}-{nonce}.py")
    assert not worker_script.exists()
    events = []

    class Adapter:
        def describe(self, value, command):
            name = f"astrid-update-{value['operation_id']}"
            return {
                "format": "astrid-update-platform/v1", "system": "win32",
                "kind": "scheduled-task-on-demand", "name": name, "task_name": name,
                "user": "S-1-5-21-12345", "operation_id": value["operation_id"],
                "program_root": value["program_root"], "log_path": value["log_path"],
                "command": list(command),
            }

        def is_registered(self, *_args):
            events.append("registration")
            pytest.fail("changed profile source must fail before native inspection")

        def cleanup(self, *_args, **_kwargs):
            events.append("cleanup")
            pytest.fail("changed profile source must fail before retirement")

        def launch(self, *_args, **_kwargs):
            events.append("launch")
            pytest.fail("changed profile source must never launch")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert getattr(refusal.value, "reason", None) == "busy"
    assert events == []
    assert not worker_script.exists()
    assert (root / "install.json").read_bytes() == before_marker


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_lost_continuation_ack_is_recovery_needed_and_repeated_status_does_not_retry(
    tmp_path, monkeypatch, failure_type
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    _set_unresolved(root, job, "never-entered")
    adapter = _ContinuationAdapter(root)
    launch = adapter.launch

    def lose_ack(*args):
        launch(*args)
        raise failure_type("continuation acknowledgement lost")

    monkeypatch.setattr(adapter, "launch", lose_ack)
    monkeypatch.setattr(update_job, "_platform_api", lambda: adapter)
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert getattr(refusal.value, "reason", None) == "busy"
    saved = read_json(root / "install.json")["update_job"]
    assert saved["operation_id"] == job["operation_id"]
    assert saved["state"] == "recovery-needed"
    assert saved["launch_status"] == "unknown"
    assert adapter.events.count("launch") == 1
    before = (root / "install.json").read_bytes()
    for _ in range(2):
        assert update_job.read_job(root)["state"] == "recovery-needed"
    assert (root / "install.json").read_bytes() == before
    assert adapter.events.count("launch") == 1
    previous_launches = adapter.events.count("launch")
    previous_liveness_checks = adapter.events.count("liveness")
    monkeypatch.setattr(adapter, "launch", launch)
    resumed = update_job.accept(request)  # a separate explicit Update may try once again
    assert resumed["operation_id"] == job["operation_id"]
    assert resumed["state"] == "launching"
    assert resumed["launch_status"] == "acknowledged"
    assert adapter.events.count("launch") - previous_launches == 1
    assert adapter.events.count("liveness") - previous_liveness_checks == 0
    # A proved replacement now includes the post-retirement native-absence
    # barrier in addition to the two explicit reconciliation observations.
    assert adapter.events.count("registration") == 3


def test_matching_journal_precedes_claim_continuation(tmp_path, monkeypatch):
    from tests.test_installer_update_reconcile import _unfinished_journal

    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    command = update_job._expected_worker_command(Version.load(job["old_version"]), job)
    descriptor = _descriptor(job, command)
    # The matching journal is authoritative even when the claim says bootstrap
    # was entered; it must never be replayed as a fresh worker launch.
    journal = _unfinished_journal(job, operation_id="c" * 32)
    marker = read_json(root / "install.json")
    saved = marker["update_job"]
    saved.update(state="recovery-needed", launch_status="unknown", transaction_id=journal["operation_id"],
                 bootstrap_proof=_proof(job, state="entered"), platform=descriptor)
    marker["update_job"] = saved
    write_json(root / "install.json", marker)
    write_json(root / "update-transaction.json", journal)

    class Adapter(_ContinuationAdapter):
        def launch(self, *_args):
            pytest.fail("matching transaction takes precedence over continuation")

        def is_registered(self, *_args):
            pytest.fail("matching transaction does not query pre-bootstrap registration")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter(root))
    monkeypatch.setattr(update_job, "_recover_matching_transaction",
                        lambda *_args: {"state": "rolled_back", "operation_id": job["operation_id"]})
    result = update_job.accept(request)
    assert result["operation_id"] == job["operation_id"]
    assert result["state"] == "rolled_back"
