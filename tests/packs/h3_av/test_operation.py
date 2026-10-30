from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.h3_av.actions.transform.run import (
    _operation_directory_writer_lock,
    _invoke_canonical_run,
    _invoke_stage,
)
from astrid.packs.h3_av.actions.transform.operation import OperationJournal, OperationJournalError
from astrid.sdk.results import InvocationResult


def test_operation_journal_is_durable_and_request_bound(tmp_path) -> None:
    path = tmp_path / "operation-state.json"
    journal = OperationJournal(path, request_digest="request-a")
    journal.record("run", "completed", task_id="task-a", attempt_id="attempt-a")
    resumed = OperationJournal(path, request_digest="request-a")
    assert resumed.latest("run")["task_id"] == "task-a"
    with pytest.raises(OperationJournalError, match="another request"):
        OperationJournal(path, request_digest="request-b")


def test_operation_journal_rejects_malformed_events(tmp_path) -> None:
    path = tmp_path / "operation-state.json"
    path.write_text('{"schema_version": 1, "request_digest": "x", "events": {}}', encoding="utf-8")
    with pytest.raises(OperationJournalError, match="events"):
        OperationJournal(path, request_digest="x")


def test_operation_journal_freezes_admission_digest_and_phase(tmp_path) -> None:
    path = tmp_path / "operation-state.json"
    journal = OperationJournal(path, request_digest="request-a")
    journal.freeze_admission("sha256:admission-a")
    journal.set_admission_phase("admission_started")

    resumed = OperationJournal(path, request_digest="request-a")
    assert resumed.admission_digest == "sha256:admission-a"
    assert resumed.admission_phase == "admission_started"
    with pytest.raises(OperationJournalError, match="frozen operation"):
        resumed.freeze_admission("sha256:admission-b")


def test_uncertain_canonical_admission_cannot_be_replayed(tmp_path) -> None:
    journal_path = tmp_path / "operation-state.json"
    result_path = tmp_path / "run-result.json"
    calls = []

    class Client:
        def invoke_result(self, *args, **kwargs):
            calls.append((args, kwargs))
            raise TimeoutError("response was lost")

    kwargs = {
        "inputs": {"workflow": {"digest": "sha256:workflow"}},
        "execution_request": {"target": {"kind": "runpod", "pod_id": "pod-1",
                                         "provider_account_ref": "account-1",
                                         "storage": {"network_volume_id": "volume-1"}}},
        "out": tmp_path / "run",
        "project": "project-1",
        "saved_result": result_path,
    }
    with pytest.raises(TimeoutError, match="response was lost"):
        _invoke_canonical_run(
            Client(), journal=OperationJournal(journal_path, request_digest="request-a"),
            resume=False, **kwargs,
        )

    with pytest.raises(RuntimeError, match="may have occurred"):
        _invoke_canonical_run(
            Client(), journal=OperationJournal(journal_path, request_digest="request-a"),
            resume=True, **kwargs,
        )
    assert len(calls) == 1


def test_settled_canonical_run_is_reused_with_original_identity(tmp_path) -> None:
    journal_path = tmp_path / "operation-state.json"
    result_path = tmp_path / "run-result.json"
    journal = OperationJournal(journal_path, request_digest="request-a")
    calls = []

    class Client:
        class tasks:
            @staticmethod
            def show(task_id):
                return SimpleNamespace(
                    ok=True,
                    data={
                        "task_id": task_id,
                        "run_id": "canonical-run",
                        "attempt_id": "canonical-attempt",
                        "state": "succeeded",
                        "result": {"outputs": []},
                    },
                )

            @staticmethod
            def list_managed_outputs(task_id):
                return SimpleNamespace(ok=True, data=[])

        def invoke_result(self, capability_id, **kwargs):
            calls.append(capability_id)
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                kernel_task_id="canonical-task",
                kernel_run_id="canonical-run",
                kernel_attempt_id="canonical-attempt",
            )

    kwargs = {
        "inputs": {"workflow": {"digest": "sha256:workflow"}},
        "execution_request": None,
        "out": tmp_path / "run",
        "project": "project-1",
        "saved_result": result_path,
    }
    first = _invoke_canonical_run(Client(), journal=journal, resume=False, **kwargs)
    resumed = _invoke_canonical_run(
        Client(), journal=OperationJournal(journal_path, request_digest="request-a"),
        resume=True, **kwargs,
    )
    assert first.kernel_task_id == resumed.kernel_task_id == "canonical-task"
    assert resumed.kernel_run_id == "canonical-run"
    assert resumed.kernel_attempt_id == "canonical-attempt"
    assert calls == ["vibecomfy.run"]


def test_fresh_canonical_operations_get_distinct_submission_context(tmp_path) -> None:
    contexts = []

    class Client:
        def invoke_result(self, capability_id, **kwargs):
            contexts.append(kwargs["idempotency_context"])
            ordinal = len(contexts)
            identity = (f"canonical-task-{ordinal}", f"canonical-run-{ordinal}", f"canonical-attempt-{ordinal}")
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                kernel_task_id=identity[0],
                kernel_run_id=identity[1],
                kernel_attempt_id=identity[2],
            )

    kwargs = {
        "inputs": {"workflow": {"digest": "sha256:workflow"}},
        "execution_request": None,
        "out": tmp_path / "run",
        "project": "project-1",
    }
    first_journal = OperationJournal(tmp_path / "first-state.json", request_digest="request-a")
    first = _invoke_canonical_run(
        Client(), journal=first_journal, resume=False,
        saved_result=tmp_path / "first-result.json", **kwargs,
        idempotency_context={"h3_submission_id": first_journal.submission_id},
    )
    second_journal = OperationJournal(tmp_path / "second-state.json", request_digest="request-a")
    second = _invoke_canonical_run(
        Client(), journal=second_journal, resume=False,
        saved_result=tmp_path / "second-result.json", **kwargs,
        idempotency_context={"h3_submission_id": second_journal.submission_id},
    )

    assert first.kernel_task_id != second.kernel_task_id
    assert contexts[0] != contexts[1]
    assert contexts[0]["h3_submission_id"] == first_journal.submission_id
    assert contexts[1]["h3_submission_id"] == second_journal.submission_id


def test_saved_canonical_dto_cannot_override_unsettled_runtime_task(tmp_path) -> None:
    journal_path = tmp_path / "operation-state.json"
    result_path = tmp_path / "run-result.json"
    journal = OperationJournal(journal_path, request_digest="request-a")

    class FirstClient:
        def invoke_result(self, capability_id, **kwargs):
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                kernel_task_id="historical-task",
                kernel_run_id="historical-run",
                kernel_attempt_id="historical-attempt",
            )

    kwargs = {
        "inputs": {"workflow": {"digest": "sha256:workflow"}},
        "execution_request": None,
        "out": tmp_path / "run",
        "project": "project-1",
        "saved_result": result_path,
    }
    _invoke_canonical_run(FirstClient(), journal=journal, resume=False, **kwargs)

    class ResumeClient:
        class tasks:
            @staticmethod
            def show(task_id):
                return SimpleNamespace(
                    ok=True,
                    data={
                        "task_id": task_id,
                        "run_id": "historical-run",
                        "attempt_id": "historical-attempt",
                        "state": "running",
                    },
                )

            @staticmethod
            def list_managed_outputs(task_id):
                raise AssertionError("unsettled tasks must not read outputs")

        def invoke_result(self, *args, **kwargs):
            raise AssertionError("resume must not submit a replacement child")

    with pytest.raises(RuntimeError, match="refusing replay"):
        _invoke_canonical_run(
            ResumeClient(),
            journal=OperationJournal(journal_path, request_digest="request-a"),
            resume=True,
            **kwargs,
        )


def test_saved_canonical_dto_without_journal_admission_is_not_authority(tmp_path) -> None:
    result_path = tmp_path / "run-result.json"
    source_journal_path = tmp_path / "source-state.json"
    kwargs = {
        "inputs": {"workflow": {"digest": "sha256:workflow"}},
        "execution_request": None,
        "out": tmp_path / "run",
        "project": "project-1",
        "saved_result": result_path,
    }

    class SubmitClient:
        def invoke_result(self, capability_id, **kwargs):
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                kernel_task_id="historical-task",
                kernel_run_id="historical-run",
                kernel_attempt_id="historical-attempt",
            )

    _invoke_canonical_run(
        SubmitClient(),
        journal=OperationJournal(source_journal_path, request_digest="request-a"),
        resume=False,
        **kwargs,
    )

    with pytest.raises(RuntimeError, match="no matching journal admission"):
        _invoke_canonical_run(
            SubmitClient(),
            journal=OperationJournal(tmp_path / "fresh-state.json", request_digest="request-a"),
            resume=True,
            **kwargs,
        )


def test_settled_stage_result_reuse_is_bound_to_inputs(tmp_path) -> None:
    journal_path = tmp_path / "operation-state.json"
    result_path = tmp_path / "prepare-result.json"
    calls = []

    class Client:
        class tasks:
            @staticmethod
            def show(task_id):
                return SimpleNamespace(
                    ok=True,
                    data={
                        "task_id": task_id,
                        "run_id": "canonical-run",
                        "attempt_id": "canonical-attempt",
                        "state": "succeeded",
                        "result": {"outputs": []},
                    },
                )

            @staticmethod
            def list_managed_outputs(task_id):
                return SimpleNamespace(ok=True, data=[])

        def invoke_result(self, capability_id, **kwargs):
            calls.append(capability_id)
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                kernel_task_id="canonical-task",
                kernel_run_id="canonical-run",
                kernel_attempt_id="canonical-attempt",
            )

    journal = OperationJournal(journal_path, request_digest="request-a")
    initial = _invoke_stage(
        Client(), "h3_av.prepare", inputs={"request": {"digest": "a"}},
        out=tmp_path / "prepare", project="project-1", saved_result=result_path,
        resume=False, journal=journal, phase="prepare",
    )
    reused = _invoke_stage(
        Client(), "h3_av.prepare", inputs={"request": {"digest": "a"}},
        out=tmp_path / "prepare", project="project-1", saved_result=result_path,
        resume=True, journal=OperationJournal(journal_path, request_digest="request-a"),
        phase="prepare",
    )
    assert initial.capability_id == reused.capability_id == "h3_av.prepare"
    assert calls == ["h3_av.prepare"]
    with pytest.raises(RuntimeError, match="different stage inputs"):
        _invoke_stage(
            Client(), "h3_av.prepare", inputs={"request": {"digest": "changed"}},
            out=tmp_path / "prepare", project="project-1", saved_result=result_path,
            resume=True, journal=OperationJournal(journal_path, request_digest="request-a"),
            phase="prepare",
        )
    assert calls == ["h3_av.prepare"]


@pytest.mark.parametrize(
    ("phase", "capability_id"),
    [
        ("prepare", "h3_av.prepare"),
        ("compile", "h3_av.compile"),
        ("validate", "vibecomfy.validate"),
        ("run", "vibecomfy.run"),
        ("compose", "h3_av.compose"),
        ("verify", "h3_av.verify"),
        ("finalizer", "h3_av.publication_finalizer"),
    ],
)
def test_each_h3_stage_resumes_from_submission_scoped_sdk_receipt_after_lost_reply(
    tmp_path, phase: str, capability_id: str,
) -> None:
    journal_path = tmp_path / "operation-state.json"
    result_path = tmp_path / phase / "invocation-result.json"
    calls = []
    identity = (f"{phase}-task", f"{phase}-run", f"{phase}-attempt")

    class Client:
        def invoke_result(self, called_capability: str, **kwargs):
            receipt_path = Path(kwargs["recovery_path"])
            calls.append({
                "capability_id": called_capability,
                "recovery_path": receipt_path,
                "resume": kwargs["resume"],
                "read_managed_outputs": kwargs["read_managed_outputs"],
                "idempotency_context": kwargs["idempotency_context"],
            })
            if not kwargs["resume"]:
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                receipt_path.write_text("fake SDK receipt committed before reply loss")
                raise TimeoutError("admission committed; reply was lost")
            assert receipt_path.is_file()
            return InvocationResult(
                capability_id=called_capability,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                kernel_task_id=identity[0],
                kernel_run_id=identity[1],
                kernel_attempt_id=identity[2],
            )

    client = Client()

    def invoke(journal, *, resume: bool):
        context = {"h3_submission_id": journal.submission_id}
        if phase == "run":
            return _invoke_canonical_run(
                client,
                inputs={"workflow": {"digest": "sha256:workflow"}},
                execution_request=None,
                out=tmp_path / "run",
                project="project-1",
                saved_result=result_path,
                journal=journal,
                resume=resume,
                idempotency_context=context,
            )
        return _invoke_stage(
            client,
            capability_id,
            inputs={"payload": {"digest": "sha256:payload"}},
            out=tmp_path / phase,
            project="project-1",
            saved_result=result_path,
            resume=resume,
            journal=journal,
            phase=phase,
            idempotency_context=context,
        )

    first_journal = OperationJournal(journal_path, request_digest="request-a")
    submission_id = first_journal.submission_id
    with pytest.raises(TimeoutError, match="reply was lost"):
        invoke(first_journal, resume=False)
    assert not result_path.exists()

    resumed_journal = OperationJournal(journal_path, request_digest="request-a")
    assert resumed_journal.submission_id == submission_id
    result = invoke(resumed_journal, resume=True)

    assert (result.kernel_task_id, result.kernel_run_id, result.kernel_attempt_id) == identity
    assert len(calls) == 2
    assert calls[0]["recovery_path"] == calls[1]["recovery_path"]
    assert submission_id in calls[0]["recovery_path"].name
    assert calls[0]["resume"] is False and calls[1]["resume"] is True
    assert calls[0]["read_managed_outputs"] is True
    assert calls[1]["read_managed_outputs"] is True
    assert calls[0]["idempotency_context"] == calls[1]["idempotency_context"]
    assert calls[0]["idempotency_context"]["h3_submission_id"] == submission_id


def test_schema_one_legacy_stage_locator_observes_shared_result_and_managed_rows(tmp_path) -> None:
    journal_path = tmp_path / "operation-state.json"
    result_path = tmp_path / "prepare-result.json"
    calls = []
    observed_task = {
        "task_id": "legacy-task",
        "run_id": "legacy-run",
        "attempt_id": "runtime-attempt",
        "state": "succeeded",
        "result": {"outputs": [{"name": "preparation"}]},
    }
    managed_outputs = [{"name": "preparation", "digest": "sha256:1", "size": 3}]

    class FirstClient:
        def invoke_result(self, capability_id, **_kwargs):
            calls.append(capability_id)
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                kernel_task_id="legacy-task",
                kernel_run_id="legacy-run",
            )

    class ResumeClient:
        class tasks:
            @staticmethod
            def show(task_id):
                assert task_id == "legacy-task"
                return SimpleNamespace(ok=True, data=observed_task)

            @staticmethod
            def list_managed_outputs(task_id):
                assert task_id == "legacy-task"
                return SimpleNamespace(ok=True, data=managed_outputs)

        def invoke_result(self, *_args, **_kwargs):
            raise AssertionError("legacy locator must observe, not admit")

    kwargs = {
        "capability_id": "h3_av.prepare",
        "inputs": {"request": {"digest": "legacy"}},
        "out": tmp_path / "prepare",
        "project": "project-1",
        "saved_result": result_path,
        "phase": "prepare",
    }
    initial_journal = OperationJournal(journal_path, request_digest="request-a")
    _invoke_stage(FirstClient(), journal=initial_journal, resume=False, **kwargs)
    result = _invoke_stage(
        ResumeClient(),
        journal=OperationJournal(journal_path, request_digest="request-a"),
        resume=True,
        **kwargs,
    )

    assert calls == ["h3_av.prepare"]
    assert result.raw_result["task"] == observed_task
    assert result.raw_result["managed_outputs"] == managed_outputs
    assert result.raw_result["state"] == "completed"
    assert result.raw_result["result"] == observed_task["result"]
    assert result.raw_result["outputs"]["artifacts"] == observed_task["result"]["outputs"]
    assert result.kernel_attempt_id == "runtime-attempt"


def test_schema_one_legacy_stage_locator_rejects_runtime_identity_mismatch(tmp_path) -> None:
    journal_path = tmp_path / "operation-state.json"
    result_path = tmp_path / "prepare-result.json"

    class FirstClient:
        def invoke_result(self, capability_id, **_kwargs):
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                kernel_task_id="legacy-task",
                kernel_run_id="legacy-run",
                kernel_attempt_id="legacy-attempt",
            )

    kwargs = {
        "capability_id": "h3_av.prepare",
        "inputs": {"request": {"digest": "legacy"}},
        "out": tmp_path / "prepare",
        "project": "project-1",
        "saved_result": result_path,
        "phase": "prepare",
    }
    _invoke_stage(
        FirstClient(),
        journal=OperationJournal(journal_path, request_digest="request-a"),
        resume=False,
        **kwargs,
    )

    class ResumeClient:
        class tasks:
            @staticmethod
            def show(task_id):
                return SimpleNamespace(
                    ok=True,
                    data={
                        "task_id": task_id,
                        "run_id": "other-run",
                        "attempt_id": "legacy-attempt",
                        "state": "succeeded",
                        "result": {"outputs": []},
                    },
                )

            @staticmethod
            def list_managed_outputs(_task_id):
                raise AssertionError("mismatched identity must stop before output readback")

        def invoke_result(self, *_args, **_kwargs):
            raise AssertionError("mismatched legacy identity must not submit")

    with pytest.raises(RuntimeError, match="unknown or unsuccessful"):
        _invoke_stage(
            ResumeClient(),
            journal=OperationJournal(journal_path, request_digest="request-a"),
            resume=True,
            **kwargs,
        )


def test_legacy_stage_dto_without_journal_identity_cannot_resume(tmp_path) -> None:
    result_path = tmp_path / "prepare-result.json"

    class Client:
        def invoke_result(self, capability_id, **_kwargs):
            return InvocationResult(
                capability_id=capability_id,
                capability_type="executor",
                native_kind="executor",
                ok=True,
                kernel_task_id="legacy-task",
                kernel_run_id="legacy-run",
                kernel_attempt_id="legacy-attempt",
            )

    kwargs = {
        "capability_id": "h3_av.prepare",
        "inputs": {"request": {"digest": "legacy"}},
        "out": tmp_path / "prepare",
        "project": "project-1",
        "saved_result": result_path,
        "phase": "prepare",
    }
    _invoke_stage(
        Client(),
        journal=OperationJournal(tmp_path / "source-state.json", request_digest="request-a"),
        resume=False,
        **kwargs,
    )

    with pytest.raises(RuntimeError, match="no matching journal admission"):
        _invoke_stage(
            Client(),
            journal=OperationJournal(tmp_path / "empty-state.json", request_digest="request-a"),
            resume=True,
            **kwargs,
        )


def test_operation_directory_writer_lock_is_nonblocking_and_scoped(tmp_path) -> None:
    with _operation_directory_writer_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already in use"):
            with _operation_directory_writer_lock(tmp_path):
                pass
    assert (tmp_path / ".operation.lock").is_file()


def test_dry_run_includes_the_final_publication_stage(tmp_path: Path) -> None:
    import json
    from PIL import Image
    from astrid.packs.h3_av.actions.transform.run import build_parser, run_transform

    request_path = tmp_path / "raw-request.json"
    asset_map_path = tmp_path / "assets.json"
    look_path = tmp_path / "look.png"
    Image.new("RGB", (16, 16), color=(32, 64, 128)).save(look_path)
    request_path.write_text(json.dumps({
        "version": 1,
        "operation": "generate",
        "source": None,
        "output": {"duration": 5},
        "content": {"prompt": "A quiet field at dawn."},
        "changes": {"video": [], "audio": []},
        "references": [{"asset": "look.png", "purpose": "appearance"}],
        "overrides": {},
    }), encoding="utf-8")
    asset_map_path.write_text(json.dumps({"look.png": str(look_path)}) + "\n", encoding="utf-8")
    result = run_transform(build_parser().parse_args([
        "--request", str(request_path), "--asset-map", str(asset_map_path),
        "--out", str(tmp_path / "operation"), "--dry-run",
    ]))
    assert result["status"] == "planned"
    assert result["stages"][-1] == "h3_av.publication_finalizer"
    assert (tmp_path / "operation" / "plan.json").is_file()
    assert not (tmp_path / "operation/result.json").exists()


def _transform_args(tmp_path: Path, *extra: str, target=None):
    import json
    from astrid.packs.h3_av.actions.transform.run import build_parser
    args = build_parser().parse_args([
        "--request", str(tmp_path / "raw-request.json"), "--asset-map", str(tmp_path / "assets.json"),
        "--out", str(tmp_path / "operation"), *extra,
    ])
    if target is not None:
        args.execution_request = tmp_path / "execution-request.json"
        args.execution_request.write_text(json.dumps({"target": target}), encoding="utf-8")
    return args


@pytest.mark.parametrize("with_diagnostic", [True, False])
def test_validation_failure_persists_only_bounded_lease_evidence(tmp_path, monkeypatch, with_diagnostic):
    import json
    from astrid.core.contracts.errors import AstridError
    from astrid.packs.h3_av.actions.transform import run as transform

    secret = "PRIVATE_WORKFLOW_INPUT_SENTINEL"
    diagnostic = {
        "schema_version": 1, "category": "heartbeat_exception",
        "observed_at_utc": "2026-10-30T18:44:22+00:00",
        "last_successful_heartbeat_at_utc": "2026-10-30T18:43:00+00:00",
        "last_successful_heartbeat_expiry": "2026-10-30T18:45:00+00:00",
        "observed": {"state": "running", "attempt_id": "attempt-1", "fence": "3",
                     "lease_expires_at": "2026-10-30T18:45:00+00:00", "input": secret},
        "exception_type": "RuntimeFault", "runtime_code": "runtime_unavailable",
        "request_id": "request-1", "runtime_message": secret,
        "workflow_inputs": {"prompt": secret},
    }
    expected = {key: value for key, value in diagnostic.items()
                if key not in {"runtime_message", "workflow_inputs"}}
    expected["observed"] = {key: value for key, value in diagnostic["observed"].items() if key != "input"}
    settled = {"error": {"code": "lease_lost", "message": "lease lost", "diagnostic": diagnostic,
                         "request": secret}, "workflow_inputs": secret} if with_diagnostic else {"payload": {}, "outputs": []}
    sdk_error = {"code": "task_failed", "message": "lease lost" if with_diagnostic else "render task failed",
                 "details": {"state": "failed", "result": settled}}
    class Client:
        def invoke_result(self, capability_id, **kwargs):
            return InvocationResult(capability_id=capability_id, capability_type="executor", native_kind="executor",
                                    ok=False, error=sdk_error, raw_result={"task": {"state": "failed", "result": settled}})

    def invoke_validation(args, *, terminal):
        terminal["enabled"] = True
        terminal["journal"] = OperationJournal(args.out / "operation-state.json", request_digest="request-a")
        return transform._invoke_stage(
            Client(), "vibecomfy.validate", inputs={"workflow_inputs": secret}, out=args.out / "03-validate",
            project=None, saved_result=args.out / "03-validate" / "invocation-result.json", resume=False,
            journal=terminal["journal"], phase="validate",
        )

    monkeypatch.setattr(transform, "_run_transform_unlocked", invoke_validation)
    args = _transform_args(tmp_path)
    with pytest.raises(AstridError) as caught:
        transform.run_transform(args)
    receipt_text = (args.out / "result.json").read_text()
    receipt = json.loads(receipt_text)
    assert receipt["status"] == "failed" and receipt["ok"] is False
    assert receipt["error"]["message"] == str(caught.value)
    assert secret not in receipt_text
    if with_diagnostic:
        assert receipt["lease_diagnostic"] == expected
    else:
        assert "lease_diagnostic" not in receipt
        assert str(caught.value) == f"vibecomfy.validate failed: {sdk_error}"


def test_lease_diagnostic_rejects_malformed_or_unbounded_values():
    from astrid.packs.h3_av.actions.transform.run import _bounded_lease_diagnostic

    assert _bounded_lease_diagnostic({"schema_version": True, "category": "lease_expired"}) is None
    assert _bounded_lease_diagnostic({"schema_version": 1, "category": "unknown"}) is None
    assert _bounded_lease_diagnostic({
        "schema_version": 1, "category": "lease_expired",
        "observed_at_utc": "2026-10-30T18:44:22+01:00", "request_id": "x" * 97,
        "runtime_code": "payload=secret", "exception_type": "A/secret",
        "observed": {"state": "running\nprivate", "fence": {"input": "private"},
                     "attempt_id": "/private/path", "lease_expires_at": "invalid"},
    }) == {"schema_version": 1, "category": "lease_expired"}


@pytest.mark.parametrize("different_request", [False, True])
def test_rejected_journal_admission_preserves_existing_terminal_result(tmp_path, monkeypatch, different_request):
    import json
    from contextlib import nullcontext
    from astrid.core.contracts.errors import AstridError
    from astrid.packs.h3_av.actions.transform import run as transform
    from astrid.packs.h3_av.shared.request import normalize_request

    args = _transform_args(tmp_path)
    request = normalize_request({
        "version": 1, "operation": "generate", "output": {"duration": 1},
        "content": {"prompt": "scene"}, "changes": {"video": [], "audio": []},
        "references": [{"asset": "image.png", "purpose": "appearance"}], "overrides": {},
    })
    args.request.write_text(json.dumps(request.value))
    journal = OperationJournal(args.out / "operation-state.json", request_digest="other" if different_request else request.digest)
    journal.record("operation", "candidate_verified")
    prior_journal = journal.path.read_bytes()
    result_path = args.out / "result.json"
    result_path.write_bytes(b'{"prior": "terminal receipt"}\n')
    monkeypatch.setattr(transform, "_resolve_local_engine", lambda: object())
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", lambda **kwargs: nullcontext(object()))
    with pytest.raises(AstridError, match="another request|already has history"):
        transform.run_transform(args)
    assert result_path.read_bytes() == b'{"prior": "terminal receipt"}\n'
    assert journal.path.read_bytes() == prior_journal


def test_placement_flag_defaults_to_unset_and_accepts_local_runpod(tmp_path: Path) -> None:
    assert _transform_args(tmp_path).placement is None
    assert _transform_args(tmp_path, "--placement", "local").placement == "local"
    assert _transform_args(tmp_path, "--placement", "runpod").placement == "runpod"
    with pytest.raises(SystemExit):
        _transform_args(tmp_path, "--placement", "mars")


def test_local_placement_fails_fast_when_engine_not_installed(tmp_path: Path, monkeypatch) -> None:
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.engine_local_endpoint import EngineNotInstalled
    from astrid.packs.h3_av.actions.transform import run as transform
    def not_installed(*args, **kwargs):
        raise EngineNotInstalled("vibecomfy-h3", "no install receipt")
    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.local_engine_endpoint", not_installed)
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", lambda **kwargs: pytest.fail("connected before engine check"))
    for extra in ((), ("--placement", "local")):
        with pytest.raises(AstridError) as caught:
            transform.run_transform(_transform_args(tmp_path, *extra))
        assert caught.value.recovery_command == "astrid engines install vibecomfy-h3"
        import json
        receipt = json.loads((tmp_path / "operation/result.json").read_text())
        assert receipt["status"] == "failed" and receipt["ok"] is False
        assert receipt["capability_id"] == "h3_av.transform"
        assert receipt["outputs"] == {"artifacts": []}
        assert receipt["error"]["message"] == str(caught.value)


def test_local_placement_reports_endpoint_ownership(tmp_path: Path, monkeypatch) -> None:
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.engine_contracts import EndpointOwnershipError
    from astrid.packs.h3_av.actions.transform import run as transform

    def unowned(*args, **kwargs):
        raise EndpointOwnershipError("engine port is occupied by an unowned service")

    monkeypatch.setattr("astrid.core.generation.engine_local_endpoint.local_engine_endpoint", unowned)
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", lambda **kwargs: pytest.fail("connected before engine check"))
    with pytest.raises(AstridError) as caught:
        transform.run_transform(_transform_args(tmp_path, "--placement", "local"))
    assert caught.value.code == "endpoint_ownership"
    assert "unowned" in str(caught.value)
    assert not caught.value.recovery_command
    import json
    receipt = json.loads((tmp_path / "operation/result.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["error"]["code"] == "endpoint_ownership"


def test_runpod_placement_without_prepared_worker_uses_the_remote_wrapper(tmp_path: Path, monkeypatch) -> None:
    from astrid.packs.h3_av.actions.transform import run as transform
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", lambda **kwargs: pytest.fail("connected"))
    monkeypatch.setattr(transform, "_resolve_local_engine", lambda: pytest.fail("local engine resolved"))
    monkeypatch.setattr(transform, "_run_remote_placement", lambda args: {"placement": "runpod"})
    assert transform.run_transform(_transform_args(tmp_path, "--placement", "runpod")) == {"placement": "runpod"}


@pytest.mark.parametrize("target", [{"kind": "runpod"}, {"kind": "runpod", "pod_id": "pod"}])
def test_runpod_target_requires_runpod_placement(tmp_path: Path, monkeypatch, target) -> None:
    from astrid.core.contracts.errors import AstridError
    from astrid.packs.h3_av.actions.transform import run as transform
    monkeypatch.setattr(transform, "_resolve_local_engine", lambda: pytest.fail("local engine resolved"))
    args = _transform_args(tmp_path, "--placement", "local", target=target)
    with pytest.raises(AstridError, match="--placement runpod"):
        transform.run_transform(args)


@pytest.mark.parametrize("dry_run", [False, True])
def test_transform_rejects_unknown_target_kind(tmp_path: Path, dry_run) -> None:
    from astrid.core.contracts.errors import AstridError
    from astrid.packs.h3_av.actions.transform.run import run_transform
    extra = ("--prepared-worker", str(tmp_path / "must-not-be-read.json")) + (("--dry-run",) if dry_run else ())
    with pytest.raises(AstridError, match="target must be default or runpod"):
        run_transform(_transform_args(tmp_path, *extra, target={"kind": "local"}))


def test_dry_run_reports_the_resolved_placement(tmp_path: Path) -> None:
    import json
    from PIL import Image
    from astrid.packs.h3_av.actions.transform.run import run_transform

    request_path = tmp_path / "raw-request.json"
    asset_map_path = tmp_path / "assets.json"
    look_path = tmp_path / "look.png"
    Image.new("RGB", (16, 16), color=(32, 64, 128)).save(look_path)
    request_path.write_text(json.dumps({
        "version": 1,
        "operation": "generate",
        "source": None,
        "output": {"duration": 5},
        "content": {"prompt": "A quiet field at dawn."},
        "changes": {"video": [], "audio": []},
        "references": [{"asset": "look.png", "purpose": "appearance"}],
        "overrides": {},
    }), encoding="utf-8")
    asset_map_path.write_text(json.dumps({"look.png": str(look_path)}) + "\n", encoding="utf-8")
    assert run_transform(_transform_args(tmp_path, "--dry-run"))["placement"] == "local"
    assert (tmp_path / "operation" / "plan.json").is_file()
    legacy = run_transform(_transform_args(tmp_path, "--dry-run", "--prepared-worker", str(tmp_path / "p.json")))
    assert legacy["placement"] == "runpod"
    assert (tmp_path / "operation" / "plan.json").is_file()


def test_local_placement_passes_endpoint_inputs_and_records_receipt(tmp_path: Path, monkeypatch) -> None:
    """The local branch hands comfy_url/comfy_output_dir to the run and placement=local."""
    from types import SimpleNamespace
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.engine_contracts import HealthEvidence, LocalAttestation
    from astrid.packs.h3_av.actions.transform import run as transform
    health = HealthEvidence("healthy", "2026-10-28T00:00:00Z", "ab" * 32, True)
    attestation = LocalAttestation("local-engine.v1", "/r/.astrid-engine/install.json", "cd" * 32, health, "ab" * 32)
    engine = SimpleNamespace(endpoint=SimpleNamespace(url="http://127.0.0.1:8188", attestation=attestation),
                             comfy_output_dir="/r/ComfyUI/output")
    monkeypatch.setattr(transform, "_resolve_local_engine", lambda: engine)
    class Stop(Exception):
        pass
    def open_client(**kwargs):
        raise Stop
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", open_client)
    with pytest.raises(Stop):
        transform.run_transform(_transform_args(tmp_path))

    from astrid.packs.h3_av.actions.transform.receipt import build_final_receipt
    from dataclasses import asdict
    receipt = build_final_receipt(request_digest="d", task_succeeded=None, candidate_verified=None,
                                  placement="local", local_attestation=asdict(attestation))
    assert receipt["placement"] == "local"
    assert receipt["local_attestation"]["install_receipt_sha256"] == "cd" * 32
    assert receipt["local_attestation"]["object_info_sha256"] == "ab" * 32
    assert "placement" not in build_final_receipt(request_digest="d", task_succeeded=None, candidate_verified=None)


def test_prepared_worker_selection_is_exclusive_and_declared(tmp_path: Path) -> None:
    from astrid.packs.h3_av.actions.transform.run import build_parser
    base = ["--request", str(tmp_path / "raw.json"), "--asset-map", str(tmp_path / "assets.json"), "--out", str(tmp_path / "operation")]
    selected = build_parser().parse_args(base + ["--prepared-worker", str(tmp_path / "prepared.json")])
    assert selected.prepared_worker == tmp_path / "prepared.json"
    with pytest.raises(SystemExit):
        build_parser().parse_args(base + ["--prepared-worker", str(tmp_path / "prepared.json"), "--worker-qualification", str(tmp_path / "qualified.json")])


def test_prepared_workflow_bundle_pins_relocated_members_and_rejects_drift(tmp_path: Path) -> None:
    import hashlib
    from astrid.packs.h3_av.actions.transform.run import _materialize_prepared_worker_workflow_bundle
    filenames = {"python": "workflow.py", "companion": "workflow.vibe.json", "source": "source.json"}
    source = tmp_path / "verified-inputs"
    source.mkdir()
    paths = {}
    for port, name in filenames.items():
        paths[port] = source / name
        paths[port].write_bytes(("sealed " + name).encode())
    compilation = {"workflow": {name: {"sha256": hashlib.sha256(paths[port].read_bytes()).hexdigest()} for port, name in filenames.items()}}
    destination = tmp_path / "operation/prepared-workflow-bundle"
    selected = _materialize_prepared_worker_workflow_bundle(paths, compilation, destination)
    assert selected == destination / "workflow.py"
    assert {name: (destination / name).read_bytes() for name in filenames.values()} == {name: paths[port].read_bytes() for port, name in filenames.items()}
    assert _materialize_prepared_worker_workflow_bundle(paths, compilation, destination) == selected
    selected.write_bytes(b"changed after selection")
    with pytest.raises(RuntimeError, match="differs from the frozen compilation"):
        _materialize_prepared_worker_workflow_bundle(paths, compilation, destination)


@pytest.mark.parametrize("invalid", ["incomplete", "changed", "symlink"])
def test_prepared_workflow_bundle_rejects_unverified_input_before_copy(tmp_path: Path, invalid: str) -> None:
    import hashlib
    from astrid.packs.h3_av.actions.transform.run import _materialize_prepared_worker_workflow_bundle
    paths = {}
    compilation = {"workflow": {}}
    for port, name in {"python": "workflow.py", "companion": "workflow.vibe.json", "source": "source.json"}.items():
        paths[port] = tmp_path / name
        paths[port].write_bytes(name.encode())
        compilation["workflow"][name] = {"sha256": hashlib.sha256(name.encode()).hexdigest()}
    if invalid == "incomplete":
        paths.pop("source")
    elif invalid == "changed":
        paths["python"].write_bytes(b"unreviewed bytes")
    else:
        target = tmp_path / "aliased.py"
        target.write_bytes(b"workflow.py")
        paths["python"].unlink()
        paths["python"].symlink_to(target)
    destination = tmp_path / "copied"
    with pytest.raises(RuntimeError, match="complete canonical|changed after verification|not a regular file"):
        _materialize_prepared_worker_workflow_bundle(paths, compilation, destination)
    assert not destination.exists()


# These tests exercise the local transform's client context: the first receipt
# must already exist when the ephemeral client starts its cleanup.
def _h3_failure_client_fixture(tmp_path, monkeypatch, *, diagnostic, cleanup_fails=False,
                               fail_first_result_write=False, details_only=False, comfy_diagnostic=None):
    import json
    from astrid.packs.h3_av.actions.transform import run as transform

    sentinels = ("PROMPT_SENTINEL", "SECRET_SENTINEL", "/private/MEDIA_SENTINEL.mov",
                 "WORKFLOW_INPUT_SENTINEL", "RAW_EXCEPTION_SENTINEL")
    failure = {"code": "lease_lost", "message": sentinels[4],
               "diagnostic": diagnostic, "inputs": sentinels[3], "path": sentinels[2],
               "source": {"verified": True, "identity": sentinels[1], "sha256": "f" * 64}}
    if diagnostic is None:
        del failure["diagnostic"]
    settled = {"error": failure, "prompt": sentinels[0], "secret": sentinels[1]}
    result = InvocationResult(
        capability_id="vibecomfy.validate", capability_type="executor", native_kind="executor",
        ok=False, kernel_task_id="child-task-1", kernel_run_id="child-run-1",
        kernel_attempt_id="child-attempt-1",
        error={"code": "task_failed", "message": "failed", "details": {"result": settled}},
        raw_result={} if details_only else {"task": {"state": "failed", "result": settled}},
    )
    observed = {"before_cleanup": None, "writes": 0}
    args = _transform_args(tmp_path)

    class Client:
        def invoke_result(self, *args, **kwargs):
            return result

    class ClientContext:
        def __enter__(self):
            return Client()

        def __exit__(self, exc_type, exc, tb):
            path = args.out / "result.json"
            observed["before_cleanup"] = json.loads(path.read_text()) if path.exists() else None
            if cleanup_fails:
                raise RuntimeError("RAW_EXCEPTION_SENTINEL during cleanup")
            return False

    monkeypatch.setattr(transform, "_resolve_local_engine", lambda: object())
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", lambda **kwargs: ClientContext())
    monkeypatch.setattr(transform, "load_request_document", lambda path: {})
    monkeypatch.setattr(transform, "normalize_request", lambda request: SimpleNamespace(digest="sha256:request", value={}))
    monkeypatch.setattr(transform, "_json_mapping", lambda path: {})

    def prepare(client, *, journal, **kwargs):
        return transform._invoke_stage(
            client, "vibecomfy.validate", inputs={"prompt": sentinels[0], "path": sentinels[2]},
            out=args.out / "03-validate", project=None,
            saved_result=args.out / "03-validate" / "invocation-result.json",
            resume=False, journal=journal, phase="validate",
        )

    monkeypatch.setattr(transform, "_prepare_transform_input", prepare)
    if comfy_diagnostic is not None:
        original_failure = transform._validation_failure

        def failure(*args, **kwargs):
            error = original_failure(*args, **kwargs)
            error.comfy_diagnostic = comfy_diagnostic
            return error

        monkeypatch.setattr(transform, "_validation_failure", failure)
    original_write = transform.write_json_atomic

    def write(path, value):
        if Path(path) == args.out / "result.json":
            observed["writes"] += 1
            if fail_first_result_write and observed["writes"] == 1:
                raise OSError("SECRET_SENTINEL evidence write rejected")
        return original_write(path, value)

    monkeypatch.setattr(transform, "write_json_atomic", write)
    return transform, args, observed, sentinels


@pytest.mark.parametrize("details_only", [False, True])
def test_nested_failure_persists_bounded_evidence_before_cleanup(tmp_path, monkeypatch, details_only):
    import json
    from astrid.core.contracts.errors import AstridError

    diagnostic = {
        "schema_version": 1, "category": "heartbeat_exception",
        "observed_at_utc": "2026-10-30T18:44:22+00:00",
        "observed": {"state": "running", "attempt_id": "child-attempt-1", "fence": "3",
                     "inputs": "WORKFLOW_INPUT_SENTINEL"},
        "runtime_message": "RAW_EXCEPTION_SENTINEL",
    }
    transform, args, observed, sentinels = _h3_failure_client_fixture(
        tmp_path, monkeypatch, diagnostic=diagnostic, details_only=details_only,
    )
    with pytest.raises(AstridError) as caught:
        transform.run_transform(args)
    before = observed["before_cleanup"]
    assert before is not None
    receipt = json.loads((args.out / "result.json").read_text())
    envelope = receipt["failure_envelope"]
    assert before["failure_envelope"]["cleanup"]["client_context_exit"] == "pending"
    assert before["failure_envelope"]["persistence"]["before_cleanup"] == "succeeded"
    assert receipt["status"] == "failed" and receipt["ok"] is False
    assert receipt["error"]["code"] == (caught.value.code or "executor_failed")
    assert receipt["outputs"] == {"artifacts": []}
    assert envelope["schema_version"] == 1
    assert envelope["category"] == "child_failure"
    assert envelope["stage"] == "validate"
    assert envelope["observed_at_utc"]
    assert envelope["child"] == {"task_id": "child-task-1", "run_id": "child-run-1",
                                 "attempt_id": "child-attempt-1"}
    assert envelope["source"] == {"identity": "unavailable", "sha256": "unavailable"}
    assert envelope["cause"]["category"] == "heartbeat_exception"
    assert envelope["cause"]["lease_diagnostic"]["observed"]["fence"] == "3"
    assert envelope["cleanup"]["client_context_exit"] == "succeeded"
    assert envelope["cleanup"]["status"] == "unavailable"
    for sentinel in sentinels:
        assert sentinel not in json.dumps(envelope)
        assert sentinel not in json.dumps(receipt)


@pytest.mark.parametrize("diagnostic", [None, {"schema_version": 1, "category": "bad-category", "secret": "SECRET_SENTINEL"}])
def test_nested_failure_with_missing_or_malformed_diagnostic_is_unclassified(tmp_path, monkeypatch, diagnostic):
    import json
    from astrid.core.contracts.errors import AstridError

    transform, args, observed, sentinels = _h3_failure_client_fixture(
        tmp_path, monkeypatch, diagnostic=diagnostic,
    )
    with pytest.raises(AstridError):
        transform.run_transform(args)
    envelope = json.loads((args.out / "result.json").read_text())["failure_envelope"]
    assert envelope["cause"] == {"category": "unclassified", "lease_diagnostic": "unavailable",
                                 "execution_diagnostic": "unavailable"}
    assert envelope["source"] == {"identity": "unavailable", "sha256": "unavailable"}
    assert envelope["child"]["task_id"] == "child-task-1"
    assert observed["before_cleanup"] is not None
    for sentinel in sentinels:
        assert sentinel not in json.dumps(envelope)


def test_cleanup_failure_does_not_replace_nested_failure(tmp_path, monkeypatch):
    import json
    from astrid.core.contracts.errors import AstridError

    transform, args, observed, sentinels = _h3_failure_client_fixture(
        tmp_path, monkeypatch, diagnostic={"schema_version": 1, "category": "lease_expired"},
        cleanup_fails=True,
    )
    with pytest.raises(AstridError) as caught:
        transform.run_transform(args)
    receipt = json.loads((args.out / "result.json").read_text())
    assert "vibecomfy.validate failed" in str(caught.value)
    assert receipt["error"]["code"] == (caught.value.code or "executor_failed")
    assert receipt["failure_envelope"]["cleanup"]["client_context_exit"] == "failed"
    assert receipt["failure_envelope"]["persistence"]["before_cleanup"] == "succeeded"
    assert observed["before_cleanup"]["failure_envelope"]["cleanup"]["client_context_exit"] == "pending"
    assert all(sentinel not in json.dumps(receipt["failure_envelope"]) for sentinel in sentinels)


def test_evidence_write_rejection_is_separate_from_original_failure(tmp_path, monkeypatch):
    import json
    from astrid.core.contracts.errors import AstridError

    transform, args, observed, sentinels = _h3_failure_client_fixture(
        tmp_path, monkeypatch, diagnostic={"schema_version": 1, "category": "lease_expired"},
        fail_first_result_write=True,
    )
    with pytest.raises(AstridError) as caught:
        transform.run_transform(args)
    receipt = json.loads((args.out / "result.json").read_text())
    assert "vibecomfy.validate failed" in str(caught.value)
    assert receipt["error"]["code"] == (caught.value.code or "executor_failed")
    assert observed["before_cleanup"] is None
    assert receipt["failure_envelope"]["persistence"]["before_cleanup"] == "failed"
    assert receipt["failure_envelope"]["cleanup"]["client_context_exit"] == "succeeded"
    assert all(sentinel not in json.dumps(receipt["failure_envelope"]) for sentinel in sentinels)


@pytest.mark.parametrize("cleanup_fails,fail_first_write", [(False, False), (True, False), (False, True)])
def test_comfy_diagnostic_is_bounded_and_persisted_before_cleanup(
    tmp_path, monkeypatch, cleanup_fails, fail_first_write,
):
    import json
    from astrid.core.contracts.errors import AstridError

    diagnostic = {
        "schema_version": 1, "code": "comfy_execution_error", "stage": "execution",
        "node_id": "123", "node_class": "VHS_LoadVideoFFmpeg", "exception_type": "ValueError",
        "expected": 56, "observed": 48.0,
        "correlation": "01234567-89ab-cdef-0123-456789abcdef",
        "message": "RAW_EXCEPTION_SENTINEL" * 1000,
        "source_identity": "SECRET_SENTINEL", "current_inputs": "WORKFLOW_INPUT_SENTINEL",
        "unknown": {"prompt": "PROMPT_SENTINEL", "path": "/private/MEDIA_SENTINEL.mov"},
    }
    transform, args, observed, sentinels = _h3_failure_client_fixture(
        tmp_path, monkeypatch, diagnostic={"schema_version": 1, "category": "lease_expired"},
        comfy_diagnostic=diagnostic, cleanup_fails=cleanup_fails, fail_first_result_write=fail_first_write,
    )
    with pytest.raises(AstridError) as caught:
        transform.run_transform(args)
    receipt = json.loads((args.out / "result.json").read_text())
    envelope = receipt["failure_envelope"]
    execution = envelope["cause"]["execution_diagnostic"]
    assert execution == {
        "schema_version": 1, "code": "comfy_execution_error", "stage": "execution",
        "node_id": "123", "node_class": "VHS_LoadVideoFFmpeg", "exception_type": "ValueError",
        "message": "unavailable", "expected": 56, "observed": 48.0,
        "correlation": "01234567-89ab-cdef-0123-456789abcdef", "source_identity": "unavailable",
    }
    assert envelope["cause"]["lease_diagnostic"]["category"] == "lease_expired"
    assert envelope["cause"]["category"] == "lease_expired"
    assert receipt["status"] == "failed" and receipt["ok"] is False
    assert receipt["error"]["code"] == (caught.value.code or "executor_failed")
    assert "vibecomfy.validate failed" in str(caught.value)
    assert envelope["persistence"]["before_cleanup"] == ("failed" if fail_first_write else "succeeded")
    assert envelope["cleanup"]["client_context_exit"] == ("failed" if cleanup_fails else "succeeded")
    if not fail_first_write:
        before = observed["before_cleanup"]["failure_envelope"]
        assert before["cause"]["execution_diagnostic"] == execution
        assert before["cleanup"]["client_context_exit"] == "pending"
        assert all(sentinel not in json.dumps(before) for sentinel in sentinels)
    else:
        assert observed["before_cleanup"] is None
    assert all(sentinel not in json.dumps(envelope) for sentinel in sentinels)


def test_comfy_diagnostic_rejects_unsafe_and_malformed_values(tmp_path, monkeypatch):
    import json
    from astrid.core.contracts.errors import AstridError

    diagnostic = {
        "schema_version": 1, "code": "SECRET_SENTINEL", "stage": "PROMPT_SENTINEL",
        "node_id": "9" * 100, "node_class": "SECRET_SENTINEL", "exception_type": "SECRET_SENTINEL",
        "expected": float("nan"), "observed": float("inf"), "message": "/private/MEDIA_SENTINEL.mov",
        "correlation": "WORKFLOW_INPUT_SENTINEL", "source_identity": "SECRET_SENTINEL",
    }
    transform, args, observed, sentinels = _h3_failure_client_fixture(
        tmp_path, monkeypatch, diagnostic=None, comfy_diagnostic=diagnostic,
    )
    with pytest.raises(AstridError):
        transform.run_transform(args)
    before = observed["before_cleanup"]["failure_envelope"]
    execution = before["cause"]["execution_diagnostic"]
    assert execution == {"schema_version": 1, **{key: "unavailable" for key in (
        "code", "stage", "node_id", "node_class", "exception_type", "message", "expected", "observed",
        "correlation", "source_identity",
    )}}
    assert all(sentinel not in json.dumps(before) for sentinel in sentinels)
    for invalid in (None, [], {"schema_version": True}, {"schema_version": 2}):
        error = AstridError("failed")
        error.comfy_diagnostic = invalid
        assert transform._bounded_execution_diagnostic(error) == "unavailable"
    for invalid in (True, "56", 2**53, -2**53, float("nan"), float("inf")):
        error = AstridError("failed")
        error.comfy_diagnostic = {"schema_version": 1, "expected": invalid, "observed": invalid}
        result = transform._bounded_execution_diagnostic(error)
        assert result["expected"] == result["observed"] == "unavailable"


def test_comfy_diagnostic_extraction_failure_preserves_original_failure(tmp_path, monkeypatch):
    import json
    from collections.abc import Mapping
    from astrid.core.contracts.errors import AstridError

    class BrokenMapping(Mapping):
        def __getitem__(self, key):
            raise RuntimeError("SECRET_SENTINEL")
        def __iter__(self):
            return iter(())
        def __len__(self):
            return 0

    transform, args, observed, sentinels = _h3_failure_client_fixture(
        tmp_path, monkeypatch, diagnostic=None, comfy_diagnostic=BrokenMapping(),
    )
    with pytest.raises(AstridError, match="vibecomfy.validate failed"):
        transform.run_transform(args)
    before = observed["before_cleanup"]["failure_envelope"]
    assert before["cause"]["execution_diagnostic"] == "unavailable"
    assert before["persistence"]["before_cleanup"] == "succeeded"
    after = json.loads((args.out / "result.json").read_text())["failure_envelope"]
    assert after["cleanup"]["client_context_exit"] == "succeeded"
    assert all(sentinel not in json.dumps(after) for sentinel in sentinels)


def test_pre_execution_failure_has_no_invented_child_identity(tmp_path, monkeypatch):
    import json
    from astrid.core.contracts.errors import AstridError
    from astrid.packs.h3_av.actions.transform import run as transform

    def fail():
        raise AstridError("engine unavailable", code="engine_unavailable")

    monkeypatch.setattr(transform, "_resolve_local_engine", fail)
    args = _transform_args(tmp_path)
    with pytest.raises(AstridError) as caught:
        transform.run_transform(args)
    receipt = json.loads((args.out / "result.json").read_text())
    envelope = receipt["failure_envelope"]
    assert caught.value.code == receipt["error"]["code"] == "engine_unavailable"
    assert receipt["status"] == "failed"
    assert envelope["category"] == "pre_execution_failure"
    assert envelope["stage"] == "unavailable"
    assert envelope["child"] == {"task_id": "unavailable", "run_id": "unavailable", "attempt_id": "unavailable"}
    assert envelope["source"] == {"identity": "unavailable", "sha256": "unavailable"}
    assert envelope["cause"] == {"category": "unclassified", "lease_diagnostic": "unavailable",
                                 "execution_diagnostic": "unavailable"}
    assert envelope["cleanup"]["status"] == "unavailable"


def test_success_terminal_result_keeps_existing_shape(tmp_path, monkeypatch):
    import json
    from astrid.packs.h3_av.actions.transform import run as transform

    result = {"status": "completed", "result_id": "result-1"}

    def succeed(args, *, terminal):
        terminal.update(enabled=True, request_digest="sha256:request", artifacts=[])
        return result

    monkeypatch.setattr(transform, "_run_transform_unlocked", succeed)
    args = _transform_args(tmp_path)
    assert transform.run_transform(args) == result
    receipt = json.loads((args.out / "result.json").read_text())
    assert receipt["status"] == "succeeded" and receipt["ok"] is True
    assert receipt["outputs"] == {"artifacts": []}
    assert "failure_envelope" not in receipt
    assert "lease_diagnostic" not in receipt
