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
        "execution_request": {"target": {"pod_id": "pod-1"}},
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
    from astrid.packs.h3_av.actions.transform.run import build_parser, run_transform
    result = run_transform(build_parser().parse_args([
        "--request", str(tmp_path / "raw-request.json"), "--asset-map", str(tmp_path / "assets.json"),
        "--out", str(tmp_path / "operation"), "--dry-run",
    ]))
    assert result["status"] == "planned"
    assert result["stages"][-1] == "h3_av.publication_finalizer"


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
