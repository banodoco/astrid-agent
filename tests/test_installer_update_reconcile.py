"""Explicit Update reconciliation boundaries for accepted installer jobs."""
from __future__ import annotations

import io
import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from astrid.installer import update_job
from astrid.installer.core import Version, read_json, write_json
from tests.test_installer_update_job import _fixture


def _descriptor(job, command=None):
    name = f"astrid-update-{job['operation_id']}.service"
    return {
        "format": "astrid-update-platform/v1",
        "system": "linux",
        "kind": "systemd-user-transient",
        "name": name,
        "unit": name,
        "operation_id": job["operation_id"],
        "user": "123",
        "command": list(command or update_job._expected_worker_command(
            Version.load(job["old_version"]), job)),
        "program_root": job["program_root"],
        "data_root": job["data_root"],
        "log_path": job["log_path"],
    }


def _unfinished_journal(job, *, step="stopping", operation_id="c" * 32):
    candidate_root = Path(job["program_root"]) / "versions" / "candidate-8"
    candidate_root.mkdir(parents=True, exist_ok=True)
    candidate = Version(candidate_root, candidate_root / "python", candidate_root / "profile.json",
                        {}, release=job["target_release"])
    journal = {
        "format": "astrid-install-update/v1",
        "operation_id": operation_id,
        "transaction_kind": "migration",
        "data_root": job["data_root"],
        "step": step,
        "old": job["old_version"],
        "candidate": candidate.document(),
        "realm": {"realm_id": "realm-1", "realm_root": str(Path(job["data_root"]) / "realms/realm-1")},
        "update_job": {"operation_id": job["operation_id"]},
    }
    return journal


def _mark_unresolved(root, job, journal, descriptor=None):
    marker = read_json(root / "install.json")
    saved = marker["update_job"]
    saved.update(state="recovery-needed", transaction_id=journal["operation_id"],
                 launch_status="unknown")
    if descriptor is not None:
        saved["platform"] = descriptor
    marker["update_job"] = saved
    write_json(root / "install.json", marker)
    write_json(root / "update-transaction.json", journal)


def _assert_lock_busy(root):
    with pytest.raises(ValueError, match="already running"):
        update_job._locked(root)


def _write_owner_records(data):
    from astrid_runtime.paths import RuntimePaths

    paths = RuntimePaths.current(data_root=data)
    paths.runtime_support.mkdir(parents=True, exist_ok=True)
    common = {
        "pid": 123,
        "process_birth_id": "birth-123",
        "runtime_instance_id": "runtime-instance",
        "realm_root": str(Path(data) / "realms/realm-1"),
    }
    write_json(paths.discovery_path, {**common, "active_realm": "realm-1",
                                     "endpoint": "http://127.0.0.1:12345"})
    write_json(paths.instance_lock_path, {**common, "realm_id": "realm-1"})
    return paths


def _install_recovery_effects(monkeypatch, resulting_step):
    """Keep the transaction helper real while faking only backend side effects."""
    from contextlib import nullcontext

    class Backend:
        def __init__(self, *_args, **_kwargs):
            self.service = type("Service", (), {"lock": lambda _self: nullcontext()})()

        def preflight_stop(self, _data_root):
            return None

    def recover_update(installer):
        saved = read_json(installer.root / "update-transaction.json")
        saved["step"] = resulting_step
        write_json(installer.root / "update-transaction.json", saved)

    monkeypatch.setattr("astrid.installer.backend.Backend", Backend)
    monkeypatch.setattr("astrid.installer.core.Installer._recover_update", recover_update)


def test_accept_holds_installer_lock_through_checks_and_new_admission(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    original = update_job._validate_request
    observed = []

    def validate_while_locked(value, selected_root):
        _assert_lock_busy(selected_root)
        observed.append("admission")
        return original(value, selected_root)

    monkeypatch.setattr(update_job, "_validate_request", validate_while_locked)
    accepted = update_job.accept(request)
    assert accepted["state"] == "accepted"
    assert observed == ["admission"]


def test_reconcile_runs_under_the_installer_lock_before_new_input_preparation(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    observed = []
    original = update_job._reconcile_existing

    def reconcile_while_locked(selected_root, marker, value):
        _assert_lock_busy(selected_root)
        observed.append(value["operation_id"])
        return original(selected_root, marker, value)

    monkeypatch.setattr(update_job, "_reconcile_existing", reconcile_while_locked)
    monkeypatch.setattr(update_job, "fetch_update_inputs",
                        lambda *_args, **_kwargs: pytest.fail("P1 must not prepare inputs"))

    assert update_job.reconcile(request) == {"disposition": "prepare-new"}
    assert observed == [request["operation_id"]]
    assert read_json(root / "install.json").get("update_job") is None


def test_matching_unfinished_recovery_returns_i3_and_ends_this_update_request(tmp_path, monkeypatch):
    root, data, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    descriptor = _descriptor(previous)
    journal = _unfinished_journal(previous)
    _mark_unresolved(root, previous, journal, descriptor)
    _write_owner_records(data)
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary
    monkeypatch.setattr(LocalRuntimeBoundary, "process_birth_identity", lambda _pid: None)

    class Adapter:
        def is_active(self, value):
            assert value == descriptor
            _assert_lock_busy(root)
            return False

        def describe(self, job, command):
            assert job["operation_id"] == previous["operation_id"]
            return {**descriptor, "command": command}

    recovery_calls = []

    _install_recovery_effects(monkeypatch, "rolled_back")
    original_recover = update_job._recover_matching_transaction

    def recover(selected_root, job, saved_journal):
        _assert_lock_busy(selected_root)
        recovery_calls.append(saved_journal["operation_id"])
        return original_recover(selected_root, job, saved_journal)

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    monkeypatch.setattr(update_job, "_recover_matching_transaction", recover)

    def ready(selected_root, _data_root, expected_version, expected_identity, _realm):
        _assert_lock_busy(selected_root)
        return (Version.load(expected_version), "restored-instance", expected_identity)

    monkeypatch.setattr(update_job, "_verify_selected_ready", ready)

    result = update_job.accept(dict(request, operation_id="b" * 32,
                                    job_name="astrid-update-" + "b" * 32))
    assert result["operation_id"] == previous["operation_id"]
    assert result["state"] == "rolled_back"
    assert result["message"] == "Recovered your previous version — run Update again"
    assert recovery_calls == [journal["operation_id"]]
    assert read_json(root / "install.json")["update_job"]["operation_id"] == previous["operation_id"]


@pytest.mark.parametrize("active_result", [True, "unreadable"])
def test_live_or_unknown_native_liveness_refuses_busy_and_preserves_evidence(
    tmp_path, monkeypatch, active_result
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    descriptor = _descriptor(previous)
    journal = _unfinished_journal(previous)
    _mark_unresolved(root, previous, journal, descriptor)
    before_marker = (root / "install.json").read_bytes()
    before_journal = (root / "update-transaction.json").read_bytes()

    class Adapter:
        def describe(self, *_args):
            return descriptor

        def is_active(self, _value):
            if active_result == "unreadable":
                raise OSError("native status unavailable")
            return active_result

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    monkeypatch.setattr(update_job, "_recover_matching_transaction",
                        lambda *_args: pytest.fail("uncertain liveness must not recover"))
    with pytest.raises(ValueError) as refusal:
        update_job.accept(dict(request, operation_id="b" * 32,
                               job_name="astrid-update-" + "b" * 32))
    assert getattr(refusal.value, "reason", None) == "busy"
    assert (root / "install.json").read_bytes() == before_marker
    assert (root / "update-transaction.json").read_bytes() == before_journal


@pytest.mark.parametrize("mismatch", [
    "binding", "transaction", "data-root", "descriptor-command", "program-root", "owner",
])
def test_mismatched_recovery_evidence_refuses_without_replacing_job_or_journal(
    tmp_path, monkeypatch, mismatch
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    descriptor = _descriptor(previous)
    journal = _unfinished_journal(previous)
    if mismatch == "binding":
        journal["update_job"]["operation_id"] = "d" * 32
    elif mismatch == "transaction":
        journal["operation_id"] = "d" * 32
        previous["transaction_id"] = "c" * 32
    elif mismatch == "data-root":
        journal["data_root"] = str(root / "other-data")
    elif mismatch == "program-root":
        previous["program_root"] = str(root / "other-program")
    _mark_unresolved(root, previous, journal, descriptor)
    if mismatch == "transaction":
        marker = read_json(root / "install.json")
        marker["update_job"]["transaction_id"] = "c" * 32
        write_json(root / "install.json", marker)
    elif mismatch == "program-root":
        marker = read_json(root / "install.json")
        marker["update_job"]["program_root"] = str(root / "other-program")
        write_json(root / "install.json", marker)
    elif mismatch == "owner":
        marker = read_json(root / "install.json")
        marker["owner"] = "foreign-owner"
        write_json(root / "install.json", marker)
    before_marker = (root / "install.json").read_bytes()
    before_journal = (root / "update-transaction.json").read_bytes()

    class Adapter:
        def describe(self, job, command):
            expected = _descriptor(job, command)
            if mismatch == "descriptor-command":
                expected["command"] = ["/bin/other-worker"]
            return expected

        def is_active(self, _value):
            if mismatch == "descriptor-command":
                pytest.fail("a command mismatch must be rejected before liveness query")
            return False

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    monkeypatch.setattr(update_job, "_recover_matching_transaction",
                        lambda *_args: pytest.fail("mismatched evidence must not recover"))
    with pytest.raises(ValueError) as refusal:
        update_job.accept(dict(request, operation_id="b" * 32,
                               job_name="astrid-update-" + "b" * 32))
    assert getattr(refusal.value, "reason", None) == "busy"
    assert (root / "install.json").read_bytes() == before_marker
    assert (root / "update-transaction.json").read_bytes() == before_journal


def test_recovered_before_stop_terminal_record_is_verified_then_new_update_is_admitted(
    tmp_path, monkeypatch
):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    descriptor = _descriptor(previous)
    journal = _unfinished_journal(previous, step="complete")
    journal["outcome"] = "recovered_before_stop"
    _mark_unresolved(root, previous, journal, descriptor)

    class Adapter:
        def describe(self, job, command):
            return {**descriptor, "command": command}

        def is_active(self, _value):
            return False

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    ready = []

    def verify(selected_root, data_root, version, identity, realm):
        _assert_lock_busy(selected_root)
        ready.append((version, identity, realm))
        return (Version.load(version), previous["old_instance_id"], identity)

    monkeypatch.setattr(update_job, "_verify_selected_ready", verify)
    second = update_job.accept(dict(request, operation_id="b" * 32,
                                    job_name="astrid-update-" + "b" * 32))
    marker = read_json(root / "install.json")
    assert second["operation_id"] == "b" * 32
    assert second["state"] == "accepted"
    assert marker["update_job_history"][-1]["operation_id"] == previous["operation_id"]
    assert ready and ready[0][0] == previous["old_version"]
    assert ready[0][1] == previous["old_identity"]


def test_unreadable_journal_and_prior_transaction_are_busy_and_preserved(tmp_path, monkeypatch):
    for unreadable in (True, False):
        case = tmp_path / str(unreadable)
        case.mkdir()
        root, _, _, request = _fixture(case, monkeypatch)
        previous = update_job.accept(request)
        descriptor = _descriptor(previous)
        journal = _unfinished_journal(previous, operation_id="d" * 32)
        # The journal operation predates acceptance; its ID is recorded on the job,
        # but it is not a transaction claimed by this operation.
        _mark_unresolved(root, previous, journal, descriptor)
        marker = read_json(root / "install.json")
        marker["update_job"]["prior_transaction_id"] = journal["operation_id"]
        write_json(root / "install.json", marker)
        if unreadable:
            (root / "update-transaction.json").write_text("{ truncated\n")
        before_marker = (root / "install.json").read_bytes()
        before_journal = (root / "update-transaction.json").read_bytes()

        class Adapter:
            def describe(self, job, command):
                return {**descriptor, "command": command}

            def is_active(self, _value):
                return False

        monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
        with pytest.raises(ValueError) as refusal:
            update_job.accept(dict(request, operation_id="b" * 32,
                                   job_name="astrid-update-" + "b" * 32))
        assert getattr(refusal.value, "reason", None) == "busy"
        assert (root / "install.json").read_bytes() == before_marker
        assert (root / "update-transaction.json").read_bytes() == before_journal


def test_unready_selected_runtime_preserves_terminal_recovery_evidence(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    descriptor = _descriptor(previous)
    journal = _unfinished_journal(previous, step="rolled_back")
    _mark_unresolved(root, previous, journal, descriptor)
    before_marker = (root / "install.json").read_bytes()
    before_journal = (root / "update-transaction.json").read_bytes()

    class Adapter:
        def describe(self, job, command):
            return {**descriptor, "command": command}

        def is_active(self, _value):
            return False

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    monkeypatch.setattr(update_job, "_verify_selected_ready",
                        lambda *_args: (_ for _ in ()).throw(ValueError("wrong Runtime identity")))
    with pytest.raises(ValueError) as refusal:
        update_job.accept(dict(request, operation_id="b" * 32,
                               job_name="astrid-update-" + "b" * 32))
    assert getattr(refusal.value, "reason", None) == "busy"
    assert (root / "install.json").read_bytes() == before_marker
    assert (root / "update-transaction.json").read_bytes() == before_journal


@pytest.mark.parametrize("failure", ["wrong-release", "missing-instance", "unknown-owner", "unready"])
def test_real_selected_readiness_verifier_rejects_identity_and_health_failures(
    tmp_path, monkeypatch, failure
):
    root, data, old, _request = _fixture(tmp_path, monkeypatch)

    class Backend:
        def __init__(self, *_args, **_kwargs):
            pass

        def workspace(self, selected, selected_data):
            assert selected.document() == old.document()
            assert selected_data == data
            return {"realm_id": "realm-1"}

        def verify(self, *_args):
            if failure == "unready":
                raise ValueError("Runtime health is unavailable")

    monkeypatch.setattr("astrid.installer.backend.Backend", Backend)
    if failure == "wrong-release":
        monkeypatch.setattr(update_job, "_probe_startup_identity",
                            lambda *_args: ("instance-7", {"release": 8, "managed_by": None}))
    elif failure == "missing-instance":
        monkeypatch.setattr(update_job, "_probe_startup_identity",
                            lambda *_args: ("", {"release": 7, "managed_by": None}))
    elif failure == "unknown-owner":
        monkeypatch.setattr(update_job, "_probe_startup_identity",
                            lambda *_args: (_ for _ in ()).throw(ValueError("identity query failed")))
    else:
        monkeypatch.setattr(update_job, "_probe_startup_identity",
                            lambda *_args: ("instance-7", {"release": 7, "managed_by": None}))

    with pytest.raises(ValueError):
        update_job._verify_selected_ready(root, data, old.document(), {"release": 7, "managed_by": None})


def test_real_selected_readiness_verifier_rejects_wrong_selected_version(tmp_path, monkeypatch):
    root, data, old, _request = _fixture(tmp_path, monkeypatch)

    class Backend:
        def __init__(self, *_args, **_kwargs):
            pytest.fail("wrong selected image must fail before Runtime readiness calls")

    monkeypatch.setattr("astrid.installer.backend.Backend", Backend)
    wrong = {**old.document(), "identity": {"release": 99, "managed_by": None}}
    with pytest.raises(ValueError, match="unexpected version"):
        update_job._verify_selected_ready(root, data, wrong, {"release": 99, "managed_by": None})


def test_helper_serializes_busy_refusal_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    marker = read_json(root / "install.json")
    marker["update_job"].update(state="recovery-needed", launch_status="unknown")
    write_json(root / "install.json", marker)
    payload = {**request, "action": "accept", "operation_id": "b" * 32,
               "job_name": "astrid-update-" + "b" * 32}
    monkeypatch.setattr(update_job.sys, "stdin", io.StringIO(json.dumps(payload)))
    with pytest.raises(SystemExit) as outcome:
        update_job.main()
    assert outcome.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "busy"
    assert result["state"] == "recovery-needed"
    assert read_json(root / "install.json")["update_job"]["operation_id"] == previous["operation_id"]


def test_successful_empty_systemd_state_is_unknown_liveness(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    from astrid.installer import update_job_platform

    descriptor = update_job_platform.describe(job, ["python", "worker"], system="linux")
    monkeypatch.setattr(update_job_platform, "_run",
                        lambda args: CompletedProcess(args, 0, stdout="", stderr=""))
    with pytest.raises(RuntimeError, match="Unknown systemd update job state"):
        update_job_platform.is_active(descriptor, system="linux")


@pytest.mark.parametrize("identity_matches", [False, True])
def test_live_runtime_must_match_transaction_before_recovery(tmp_path, monkeypatch, identity_matches):
    from contextlib import nullcontext

    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    root, data, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    descriptor = _descriptor(previous)
    journal = _unfinished_journal(previous)
    _mark_unresolved(root, previous, journal, descriptor)
    paths = _write_owner_records(data)
    for path in (paths.discovery_path, paths.instance_lock_path):
        owner_record = read_json(path)
        owner_record["runtime_instance_id"] = "live-instance"
        write_json(path, owner_record)
    before_marker = (root / "install.json").read_bytes()
    before_journal = (root / "update-transaction.json").read_bytes()
    recovered = []

    class Adapter:
        def describe(self, job, command):
            return {**descriptor, "command": command}

        def is_active(self, _value):
            return False

    class Backend:
        def __init__(self, *_args, **_kwargs):
            self.service = type("Service", (), {"lock": lambda _self: nullcontext()})()

        def preflight_stop(self, _data_root):
            return None

    def recover_update(_installer):
        recovered.append(True)
        saved = read_json(root / "update-transaction.json")
        saved["step"] = "rolled_back"
        write_json(root / "update-transaction.json", saved)

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    monkeypatch.setattr("astrid.installer.backend.Backend", Backend)
    monkeypatch.setattr("astrid.installer.core.Installer._recover_update", recover_update)
    monkeypatch.setattr(LocalRuntimeBoundary, "process_birth_identity", lambda _pid: "birth-123")
    monkeypatch.setattr(LocalRuntimeBoundary, "validate_owner", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(update_job, "_probe_identity",
                        lambda *_args: ("live-instance", {"release": 7 if identity_matches else 8,
                                                          "managed_by": None}))
    monkeypatch.setattr(update_job, "_verify_selected_ready",
                        lambda *args: (Version.load(args[2]), "restored-instance", args[3]))

    if identity_matches:
        result = update_job.accept(dict(request, operation_id="b" * 32,
                                        job_name="astrid-update-" + "b" * 32))
        assert result["state"] == "rolled_back"
        assert recovered == [True]
    else:
        with pytest.raises(ValueError) as refusal:
            update_job.accept(dict(request, operation_id="b" * 32,
                                   job_name="astrid-update-" + "b" * 32))
        assert getattr(refusal.value, "reason", None) == "busy"
        assert recovered == []
        assert (root / "install.json").read_bytes() == before_marker
        assert (root / "update-transaction.json").read_bytes() == before_journal


def test_terminal_rollback_can_admit_one_newer_operation_on_later_update(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    descriptor = _descriptor(previous)
    journal = _unfinished_journal(previous, step="rolled_back")
    _mark_unresolved(root, previous, journal, descriptor)

    class Adapter:
        def describe(self, job, command):
            return {**descriptor, "command": command}

        def is_active(self, _value):
            _assert_lock_busy(root)
            return False

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    verified = []

    def ready(selected_root, data_root, expected_version, expected_identity, expected_realm):
        _assert_lock_busy(selected_root)
        verified.append((expected_version, expected_identity, expected_realm))
        return (Version.load(expected_version), "restored-instance", expected_identity)

    monkeypatch.setattr(update_job, "_verify_selected_ready", ready)
    second = update_job.accept(dict(request, operation_id="b" * 32,
                                    job_name="astrid-update-" + "b" * 32))
    marker = read_json(root / "install.json")
    assert second["operation_id"] == "b" * 32
    assert second["state"] == "accepted"
    assert marker["update_job"]["operation_id"] == "b" * 32
    assert marker["update_job_history"][-1]["operation_id"] == previous["operation_id"]
    assert verified and verified[0][1] == previous["old_identity"]


@pytest.mark.parametrize("prepared", [True, False], ids=["same-release", "no-target"])
def test_confirmed_terminal_result_with_no_newer_target_returns_without_admission(tmp_path, monkeypatch, prepared):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    descriptor = _descriptor(previous)
    journal = _unfinished_journal(previous, step="complete")
    candidate = Version.load(journal["candidate"])
    candidate.root.mkdir(parents=True, exist_ok=True)
    candidate.profile.write_text("{}\n")
    candidate.python.write_text("python placeholder\n")
    write_json(candidate.root / "version.json", candidate.document())
    (root / "current").write_text(candidate.root.name + "\n")
    _mark_unresolved(root, previous, journal, descriptor)

    class Adapter:
        def describe(self, job, command):
            return {**descriptor, "command": command}

        def is_active(self, _value):
            return False

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())
    verified = []
    monkeypatch.setattr(update_job, "_verify_selected_ready",
                        lambda *args: (verified.append(args)
                                       or (Version.load(args[2]), "updated-instance", args[3])))
    result = update_job.accept(dict(request, operation_id="b" * 32,
                                    job_name="astrid-update-" + "b" * 32,
                                    metadata=request["metadata"] if prepared else None))
    assert result["operation_id"] == previous["operation_id"]
    assert result["state"] == "updated"
    assert len(verified) == 1
    assert verified[0][2] == candidate.document()
    assert verified[0][3] == {"release": 8, "managed_by": None}
    assert read_json(root / "install.json")["update_job"]["operation_id"] == previous["operation_id"]


def test_recovery_result_cannot_be_mistaken_for_new_operation_launch(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    descriptor = _descriptor(previous)
    journal = _unfinished_journal(previous)
    _mark_unresolved(root, previous, journal, descriptor)
    class Adapter:
        def describe(self, job, command):
            return {**descriptor, "command": command}

        def is_active(self, _value):
            return False

        def launch(self, *_args, **_kwargs):
            pytest.fail("recovery-only result must not launch a second operation")

    monkeypatch.setattr(update_job, "_platform_api", lambda: Adapter())

    def recover(selected_root, job, saved_journal):
        saved_journal["step"] = "rolled_back"
        write_json(selected_root / "update-transaction.json", saved_journal)
        return update_job._set_reconciled_job(
            selected_root, job, "rolled_back", "Recovered your previous version — run Update again")

    monkeypatch.setattr(update_job, "_recover_matching_transaction", recover)
    monkeypatch.setattr(update_job, "_verify_selected_ready",
                        lambda *args: (Version.load(args[2]), "restored-instance", args[3]))
    result = update_job.accept(dict(request, operation_id="b" * 32,
                                    job_name="astrid-update-" + "b" * 32))
    assert result["state"] == "rolled_back"
    with pytest.raises(ValueError, match="no longer current"):
        update_job.launch_job(root, "b" * 32)


def test_read_job_remains_observational_for_unresolved_operation(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    job = update_job.accept(request)
    journal = _unfinished_journal(job)
    _mark_unresolved(root, job, journal, _descriptor(job))
    before_marker = (root / "install.json").read_bytes()
    before_journal = (root / "update-transaction.json").read_bytes()
    for _ in range(2):
        assert update_job.read_job(root)["state"] == "recovery-needed"
    assert (root / "install.json").read_bytes() == before_marker
    assert (root / "update-transaction.json").read_bytes() == before_journal


@pytest.mark.parametrize("prior", ["unreadable", {"format": update_job.JOB_FORMAT, "state": "unknown"},
                                  {"format": update_job.JOB_FORMAT, "state": []}])
def test_malformed_prior_operation_cannot_be_overwritten_by_new_admission(tmp_path, monkeypatch, prior):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    marker = read_json(root / "install.json")
    marker["update_job"] = prior
    write_json(root / "install.json", marker)
    before = (root / "install.json").read_bytes()
    monkeypatch.setattr(update_job, "_validate_request", lambda *_args: pytest.fail("no new admission"))
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert refusal.value.reason == "busy"
    assert (root / "install.json").read_bytes() == before


def test_confirmed_operation_id_cannot_be_accepted_again(tmp_path, monkeypatch):
    root, _, _, request = _fixture(tmp_path, monkeypatch)
    previous = update_job.accept(request)
    marker = read_json(root / "install.json")
    marker["update_job"]["state"] = "rolled_back"
    write_json(root / "install.json", marker)
    before = (root / "install.json").read_bytes()
    with pytest.raises(ValueError) as refusal:
        update_job.accept(request)
    assert refusal.value.reason == "busy"
    assert (root / "install.json").read_bytes() == before
    assert update_job.read_job(root)["operation_id"] == previous["operation_id"]
