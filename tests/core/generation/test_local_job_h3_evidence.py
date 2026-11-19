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


def test_source_free_composition_and_verification_enforce_requested_dimensions(tmp_path):
    import subprocess
    from astrid.packs.h3_av.shared.compose import CompositionError, compose_candidate
    from astrid.packs.h3_av.shared.generation import generation_timing
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.verify import VerificationError, verify_candidate

    request = normalize_request({"version": 2, "prompt": "A scene", "duration": 1,
                                 "output": {"size": [512, 288]}, "media": []})
    preparation = prepare_request(request)

    def video(path, size, frames):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=blue:s={size}:r=24", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000", "-frames:v", str(frames),
                        "-t", str(frames / 24), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(path)], check=True, capture_output=True)
        return path

    frames = generation_timing(1)["raw_frames"]
    wrong = video(tmp_path / "wrong.mp4", "1024x576", frames)
    with pytest.raises(CompositionError, match="generated video size is 1024x576, requested 512x288"):
        compose_candidate(preparation=preparation, generated=wrong, out_dir=tmp_path / "bad")
    generated = video(tmp_path / "generated.mp4", "512x288", frames)
    composition = compose_candidate(preparation=preparation, generated=generated, out_dir=tmp_path / "good")
    candidate = Path(composition["candidate"]["path"])
    assert composition["coverage"]["candidate"]["video"]["width"] == 512
    assert composition["coverage"]["candidate"]["video"]["height"] == 288
    assert verify_candidate(preparation=preparation, composition=composition)["status"] == "verified"
    import copy
    legacy = copy.deepcopy(composition)
    del legacy["coverage"]["candidate"]["video"]["width"]
    del legacy["coverage"]["candidate"]["video"]["height"]
    verified = verify_candidate(preparation=preparation, composition=legacy)
    assert verified["coverage"]["video"]["width"] == 512
    legacy["coverage"]["candidate"]["video"]["width"] = 1024
    with pytest.raises(VerificationError, match="candidate coverage does not match"):
        verify_candidate(preparation=preparation, composition=legacy)
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", str(candidate), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True)
    assert len(decoded.stdout) == 512 * 288 * 3

    video(candidate, "1024x576", 24)
    composition["candidate"]["sha256"] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    composition["candidate"]["size"] = candidate.stat().st_size
    with pytest.raises(VerificationError, match="candidate video size is 1024x576, requested 512x288"):
        verify_candidate(preparation=preparation, composition=composition)


def test_source_backed_generated_video_must_match_canvas(tmp_path):
    import subprocess
    from astrid.packs.h3_av.shared.compose import CompositionError, compose_candidate
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.verify import verify_candidate

    def video(path, size, color):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s={size}:r=24:d=1", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=1", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                       check=True, capture_output=True)
        return path

    source = video(tmp_path / "source.mp4", "512x288", "blue")
    request = normalize_request({"version": 2, "prompt": "Edit the scene", "duration": 1,
        "output": {"size": [512, 288]}, "media": [{"id": "source", "asset": "source.mp4",
        "role": "timeline", "modality": "video", "at": {"frame": 0}, "range": [0, 1],
        "edit": [{"stream": "video", "during": [0, 1], "mask": {"full_frame": True}}]}]})
    preparation = prepare_request(request, asset_map={"source.mp4": str(source)})
    assert preparation["mask_schedule"]["branch"] == "source_backed_v2v"
    wrong = video(tmp_path / "wrong.mp4", "1024x576", "red")
    with pytest.raises(CompositionError, match="generated video size is 1024x576, requested 512x288"):
        compose_candidate(preparation=preparation, generated=wrong, source=source, out_dir=tmp_path / "bad")
    generated = video(tmp_path / "generated.mp4", "512x288", "red")
    composition = compose_candidate(preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "good")
    verification = verify_candidate(preparation=preparation, composition=composition, source=source)
    candidate = composition["candidate"]["path"]
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", candidate, "-frames:v", "1", "-f",
                              "rawvideo", "-pix_fmt", "rgb24", "-"], check=True, capture_output=True)
    assert len(decoded.stdout) == 512 * 288 * 3
    assert verification["status"] == "verified"


@pytest.mark.parametrize("source_backed", [False, True])
@pytest.mark.parametrize("media", [None, {}, {"width": 512, "height": "288"}])
def test_public_h3_dimension_checks_reject_missing_or_malformed_media(tmp_path, source_backed, media):
    import copy
    import subprocess
    from astrid.packs.h3_av.shared.compose import CompositionError, compose_candidate
    from astrid.packs.h3_av.shared.generation import generation_timing
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.verify import VerificationError, verify_candidate

    def video(path, frames):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=blue:s=512x288:r=24", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000", "-frames:v", str(frames),
                        "-t", str(frames / 24), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(path)], check=True, capture_output=True)
        return path

    request = {"version": 2, "prompt": "A scene", "duration": 1,
               "output": {"size": [512, 288]}, "media": []}
    source = None
    if source_backed:
        source = video(tmp_path / "source.mp4", 24)
        request["media"] = [{"id": "source", "asset": "source.mp4", "role": "timeline",
                             "modality": "video", "at": {"frame": 0}, "range": [0, 1],
                             "edit": [{"stream": "video", "during": [0, 1],
                                       "mask": {"full_frame": True}}]}]
    preparation = prepare_request(normalize_request(request),
                                  asset_map={"source.mp4": str(source)} if source else None)
    generated = video(tmp_path / "generated.mp4", 24 if source else generation_timing(1)["raw_frames"])
    composition = compose_candidate(preparation=preparation, generated=generated, source=source,
                                    out_dir=tmp_path / "good")
    invalid = copy.deepcopy(preparation)
    if media is None:
        invalid.pop("media")
    else:
        invalid["media"] = media
    with pytest.raises(CompositionError, match="no usable requested video dimensions"):
        compose_candidate(preparation=invalid, generated=generated, source=source, out_dir=tmp_path / "bad")
    with pytest.raises(VerificationError, match="no usable requested video dimensions"):
        verify_candidate(preparation=invalid, composition=composition, source=source)


def test_public_h3_boundaries_reject_missing_decoded_dimensions(tmp_path, monkeypatch):
    import subprocess
    from astrid.packs.h3_av.shared import compose as compose_module
    from astrid.packs.h3_av.shared.compose import CompositionError, compose_candidate
    from astrid.packs.h3_av.shared.generation import generation_timing
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.verify import VerificationError, verify_candidate

    preparation = prepare_request(normalize_request({
        "version": 2, "prompt": "A scene", "duration": 1,
        "output": {"size": [512, 288]}, "media": [],
    }))
    generated = tmp_path / "generated.mp4"
    frames = generation_timing(1)["raw_frames"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=512x288:r=24", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000", "-frames:v", str(frames),
                    "-t", str(frames / 24), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(generated)], check=True, capture_output=True)
    composition = compose_candidate(preparation=preparation, generated=generated, out_dir=tmp_path / "good")
    original_probe = compose_module._probe

    def without_dimensions(path):
        probe = original_probe(path)
        if probe:
            for stream in probe.get("streams", []):
                stream.pop("width", None)
                stream.pop("height", None)
        return probe

    monkeypatch.setattr(compose_module, "_probe", without_dimensions)
    with pytest.raises(CompositionError, match="generated video has no usable decoded dimensions"):
        compose_candidate(preparation=preparation, generated=generated, out_dir=tmp_path / "bad")
    with pytest.raises(VerificationError, match="candidate video has no usable decoded dimensions"):
        verify_candidate(preparation=preparation, composition=composition)
