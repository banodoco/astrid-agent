"""H3 evidence remains a declared task log after its job directory is removed."""
from __future__ import annotations

import hashlib
import importlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core._shared import run_logs
from astrid.core.cli.domain_tasks import build_parser
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.generation import entries, managed_preparation
from astrid.core.generation.engine_contracts import DeadlineExceeded
from astrid.core.generation.local_job import run_job
from astrid.core.generation.preflight import generation_error
from astrid.packs.runpod.failure_receipt import build_receipt
from astrid.packs.vibecomfy import local_job, production_engine
from astrid.sdk.contracts import DomainResult
from tests.core.generation.test_h3_local_job import REQUEST, _video


@pytest.mark.parametrize("verify_fails", [False, True], ids=["verified", "verification-refused"])
def test_h3_evidence_survives_cleanup_and_tasks_logs_lists_it(tmp_path, monkeypatch, capsys, verify_fails):
    source = _video(tmp_path / "source.mp4", 3, frames=57)
    generated = _video(tmp_path / "generated.mp4", 4.5)
    request = json.loads(REQUEST.read_text())
    job = tmp_path / "job.json"
    job.write_text(json.dumps({
        "schema": "astrid.machine-job.v1", "entry": "h3", "mode": "v2v", "align": "check",
        "inputs": {"request": str(REQUEST), "asset_map": {
            request["media"][0]["asset"]["id"]: str(source), "source": str(source)}},
    }))
    monkeypatch.setattr("astrid.core.generation.preflight.require_generation_target_readiness", lambda *a: None)
    monkeypatch.setattr(entries, "target_schema", lambda *a, **k: None)
    evidence = {
        "h3.preparation": "h3/preparation.json",
        "h3.plan": "h3/build-state.json",
        "h3.compilation": "h3/plan/compile/compilation.json",
        "h3.composition": "h3/compose/composition-manifest.json",
        "h3.verification": "h3/verify.json",
    }
    observed = {}
    original_bytes = {}

    def owned_action(workflow, out, **kwargs):
        # Only the GPU/owned engine is stood in for. Build, compose, verify,
        # log collection, job cleanup and the tasks logs handler are real.
        spec = json.loads(kwargs["after_outputs_job"])
        observed["job_dir"] = Path(spec["job_dir"])
        assert Path(workflow).is_relative_to(observed["job_dir"])
        engine_log = tmp_path / "engine.log"
        engine_log.write_text("engine evidence\n")
        run_logs.declare(out, run_logs.LogDecl("engine", "engine", str(engine_log)))
        if verify_fails:
            verify = importlib.import_module("_h3_av_pack.shared.verify")

            def refuse(**kwargs):
                raise verify.VerificationError("candidate digest does not match composition evidence")

            monkeypatch.setattr(verify, "verify_candidate", refuse)
        action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
        try:
            action._after_outputs(kwargs["after_outputs_job"])([generated])
        finally:
            for name, relative in evidence.items():
                path = observed["job_dir"] / relative
                if path.is_file():
                    original_bytes[name] = path.read_bytes()
        return {}

    monkeypatch.setattr(local_job, "run_owned_action", owned_action)
    out = tmp_path / "out"
    if verify_fails:
        with pytest.raises(ValueError, match="candidate digest"):
            run_job("h3", job, out)
    else:
        run_job("h3", job, out)
    assert not observed["job_dir"].exists()
    declared = {decl.name: path for decl, path in run_logs.read_declared(out)}
    assert set(evidence) <= set(declared)

    # The pod finish command recollects from declarations; then the host
    # merges those transported logs with its own. Both must retain h3.
    index = {row["name"]: row for row in run_logs.collect(out)}
    host = object.__new__(GenericPackHost)
    descriptors = host._log_outputs(out, attempt=tmp_path / "attempt")
    expected = set(evidence) - ({"h3.verification"} if verify_fails else set())
    assert set(original_bytes) == expected
    for name in expected:
        data = (out / "logs" / index[name]["file"]).read_bytes()
        assert data == original_bytes[name]
        assert index[name]["sha256"] == hashlib.sha256(data).hexdigest()
        assert declared[name].is_file()
    assert json.loads(original_bytes["h3.plan"])["plan"]["request_digest"]
    assert json.loads(original_bytes["h3.composition"])["kind"] == "h3_av_composition"
    if verify_fails:
        assert index["h3.verification"]["missing"]
    else:
        assert json.loads(original_bytes["h3.verification"])["status"] == "verified"

    class Tasks:
        def list(self, project_id, **kwargs):
            return DomainResult.success([{"task_id": "task-h3"}])

        def show(self, task_id):
            assert task_id == "task-h3"
            return DomainResult.success({"task_id": task_id, "state": "failed" if verify_fails else "succeeded",
                                         "result": {"outputs": descriptors}})

    parser = build_parser(SimpleNamespace(tasks=Tasks()))
    args = parser.parse_args(["logs", "--project", "demo", "task-h3"])
    assert args.handler(args) == 0
    listing = capsys.readouterr().out
    assert all(name in listing for name in expected)
    assert "engine" in listing
    if verify_fails:
        assert "h3.verification" not in listing


def test_preparation_timeout_says_rerun_to_resume_and_still_drains(tmp_path, monkeypatch):
    workflow = tmp_path / "workflow.py"
    workflow.write_text("pass")
    workflow.with_suffix(".vibe.json").write_text("{}")
    workflow.with_name("source.json").write_text("{}")
    events = []
    timed_out = DeadlineExceeded("model download deadline exceeded", step_id="models",
                                 retryable=True, recovery_handle="download-prefix")

    class Preparation:
        root = str(tmp_path / "engine")
        recipe = SimpleNamespace(engine="comfyui")
        fail_preparation = True

        def __init__(self, **kwargs):
            pass

        def prepare(self, *args, **kwargs):
            events.append("prepare")
            if self.fail_preparation:
                raise timed_out
            return {"endpoint": "http://127.0.0.1:8188"}

        def close(self, **kwargs):
            events.append("close")

    monkeypatch.setattr(managed_preparation, "ManagedEnginePreparation", Preparation)
    monkeypatch.setattr(production_engine, "load_workflow_path", lambda *a: SimpleNamespace(resolved=object()))
    monkeypatch.setattr(production_engine, "_canonical_bundle_value", lambda value: SimpleNamespace(workflow=object()))
    monkeypatch.setattr(production_engine, "loaded_workflow_execution_identity", lambda value: "identity")
    action = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")

    def execute(*args, **kwargs):
        events.append("execute")
        raise DeadlineExceeded("execution deadline exceeded")

    monkeypatch.setattr(action, "_run_and_settle", execute)
    with pytest.raises(DeadlineExceeded) as caught:
        local_job.run_owned_action(workflow, tmp_path / "out", task_identity="task", attempt_identity="attempt",
                                   align="align")
    error = caught.value
    assert "rerun to retry preparation; completed model files are kept and verified; interrupted models download again from the start" in generation_error(error).to_envelope()["issues"][0]["message"]
    assert error is timed_out
    assert error.code == "deadline_exceeded" and error.retryable
    assert error.step_id == "models" and error.recovery_handle == "download-prefix"
    assert events == ["prepare", "close"]
    assert not list(tmp_path.glob("astrid-pod-preparation-*"))

    # A sampling/execution timeout must retain its own diagnosis.
    Preparation.fail_preparation = False
    monkeypatch.setattr(managed_preparation, "preparation_scope", lambda report: nullcontext())
    with pytest.raises(DeadlineExceeded, match="^execution deadline exceeded$"):
        local_job.run_owned_action(workflow, tmp_path / "out-execute", task_identity="task", attempt_identity="attempt",
                                   align="align")
    assert events == ["prepare", "close", "prepare", "execute", "close"]


def test_cold_download_timeout_receipt_tells_user_to_rerun():
    """The controller's fixed refusal must retain the advice from pod preparation."""
    lease = SimpleNamespace(failure_diagnostic={"code": "deadline_exceeded", "category": "deadline"},
                            failure_exit_code=2)
    run = SimpleNamespace(progress=[{"phase": "align"}])
    facts = {"failure": RuntimeError("private pod message"), "stage": "fetch", "lease": lease, "run": run}
    receipt = build_receipt(facts, job=SimpleNamespace(task_id="task-h3"), machine={}, timing=None)
    assert receipt["stage"] == "model_download"
    assert "rerun to retry preparation; completed model files are kept and verified; interrupted models download again from the start" in receipt["message"]
    assert "private pod message" not in receipt["message"]
    run.progress = [{"phase": "run"}]
    receipt = build_receipt(facts, job=SimpleNamespace(task_id="task-h3"), machine={}, timing=None)
    assert receipt["stage"] == "execution"
    assert "rerun to retry preparation; completed model files are kept and verified; interrupted models download again from the start" not in receipt["message"]
