"""Worker checks for a controller-conformed H3 source handoff."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from astrid.core.generation import entries
from astrid.core.generation.engine_facts import comfyui_facts
from astrid.core.generation.local_job import run_job
from astrid.packs.vibecomfy.local_job import JobInputError, _validate_h3_prepared_handoff
from tests.core.generation.test_h3_local_job import REQUEST, _video


@pytest.fixture
def frozen_source(tmp_path: Path) -> dict[str, object]:
    from astrid.packs.h3_av.shared import engine_facts
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.request import normalize_request

    engine_facts.install(comfyui_facts())
    source = _video(tmp_path / "source.mp4", 3)
    document = json.loads(REQUEST.read_text(encoding="utf-8"))
    document["media"][0]["asset"]["id"] = "source"
    request = normalize_request(document)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(document), encoding="utf-8")
    prepared = prepare_request(
        request, asset_map={"source": str(source)}, out_dir=tmp_path / "controller-build",
    )
    conform = prepared["source_conform"]
    source_path = Path(prepared["assets"][0]["path"])
    prepared_sha256 = conform["output"]["sha256"]
    envelope = {
        "version": 1,
        "request_digest": request.digest,
        "asset": "source",
        "managed_source": {
            "digest": "sha256:" + prepared_sha256,
            "sha256": prepared_sha256,
            "bytes": source_path.stat().st_size,
            "filename": source_path.name,
        },
        "conform": conform,
    }
    envelope_path = tmp_path / "prepared-source.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    return {
        "source": source,
        "request": request,
        "request_path": request_path,
        "source_path": source_path,
        "conform": conform,
        "envelope": envelope,
        "envelope_path": envelope_path,
    }


def _worker_inputs(frozen_source: dict[str, object], *, include_handoff: bool = True) -> tuple[dict, dict]:
    source_path = frozen_source["source_path"]
    asset_map = {"source": str(source_path)}
    if include_handoff:
        asset_map["__astrid_h3_prepared__"] = str(frozen_source["envelope_path"])
    return (
        {"request": str(frozen_source["request_path"]), "asset_map": asset_map},
        {"source_video": str(source_path)},
    )


def test_h3_worker_refuses_prepared_source_without_handoff(frozen_source: dict[str, object]) -> None:
    inputs, files = _worker_inputs(frozen_source, include_handoff=False)
    with pytest.raises(JobInputError, match="requires a frozen prepared-source handoff"):
        _validate_h3_prepared_handoff(inputs, files)


def test_h3_worker_refuses_direct_source_job_without_handoff(
    frozen_source: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import astrid.packs.vibecomfy.extensions  # noqa: F401  (register H3)

    source = frozen_source["source"]
    request_path = frozen_source["request_path"]
    job_path = tmp_path / "direct-job.json"
    job_path.write_text(json.dumps({
        "schema": "astrid.machine-job.v1",
        "entry": "h3",
        "mode": "v2v",
        "align": "check",
        "inputs": {
            "request": str(request_path),
            "asset_map": {"source": str(source)},
            "source_video": str(source),
        },
    }), encoding="utf-8")
    monkeypatch.setattr("astrid.core.generation.preflight.require_generation_target_readiness", lambda *_a, **_k: None)
    monkeypatch.setattr(entries, "target_schema", lambda *_a, **_k: None)
    monkeypatch.setattr(entries, "build_entry", lambda *_a, **_k: pytest.fail("worker built without prepared-source handoff"))
    with pytest.raises(JobInputError, match="requires a frozen prepared-source handoff"):
        run_job("h3", job_path, tmp_path / "out")


def test_h3_worker_refuses_prepared_source_digest_mismatch(
    frozen_source: dict[str, object],
) -> None:
    inputs, files = _worker_inputs(frozen_source)
    Path(frozen_source["source_path"]).write_bytes(b"tampered prepared source")
    with pytest.raises(JobInputError, match="prepared source digest mismatch"):
        _validate_h3_prepared_handoff(inputs, files)


def test_h3_worker_refuses_managed_source_descriptor_mismatch(
    frozen_source: dict[str, object],
) -> None:
    inputs, files = _worker_inputs(frozen_source)
    envelope = dict(frozen_source["envelope"])
    envelope["managed_source"] = {**envelope["managed_source"], "sha256": "0" * 64}
    Path(frozen_source["envelope_path"]).write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(JobInputError, match="managed-source descriptor digest mismatch"):
        _validate_h3_prepared_handoff(inputs, files)


def test_h3_worker_trusts_sealed_measurement_without_probing_again(
    frozen_source: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.packs.h3_av.shared import engine_facts, prepare

    run = subprocess.run
    calls = []
    source_path = Path(frozen_source["source_path"])
    expected_command = [shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
                        str(source_path)]
    expected_kwargs = {"check": True, "capture_output": True, "text": True, "timeout": 30}

    def geometry_probe(*args, **kwargs):
        calls.append((args, kwargs))
        assert args == (expected_command,)
        assert kwargs == expected_kwargs
        return run(*args, **kwargs)

    def unexpected_measurement(*_args, **_kwargs):
        pytest.fail("worker repeated decoded audio/video rate measurement")

    monkeypatch.setattr(subprocess, "run", geometry_probe)
    monkeypatch.setattr(prepare, "_check_source_av_rate", unexpected_measurement)
    monkeypatch.setattr(engine_facts, "facts", lambda: SimpleNamespace(measure_source_av=unexpected_measurement))
    request = frozen_source["request"]
    source = request.value["media"][0]
    canvas = request.value["canvas"]
    result = prepare._validate_frozen_source(request, source, source_path, frozen_source["envelope"],
                                             canvas=(canvas["width"], canvas["height"]))
    assert calls == [((expected_command,), expected_kwargs)]
    assert result["decoded_av"] == frozen_source["conform"]["decoded_av"]


def test_controller_checks_conformed_source_rate_once(
    frozen_source: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from astrid.packs.h3_av.shared import conform, prepare

    calls = []
    check = prepare._check_source_av_rate

    def counted(asset, path, *, expected_size=None):
        calls.append((Path(path), expected_size))
        return check(asset, path, expected_size=expected_size)

    monkeypatch.setattr(prepare, "_check_source_av_rate", counted)
    monkeypatch.setattr(conform, "_check_source_av_rate", counted)
    prepare.prepare_request(frozen_source["request"], asset_map={"source": str(frozen_source["source"])},
                            out_dir=tmp_path / "second-controller-build")
    assert len(calls) == 1
    assert calls[0][0].name == "source.mp4"
    assert calls[0][1] == (1024, 576)


def test_h3_worker_build_consumes_frozen_source_without_reconforming(
    frozen_source: dict[str, object], monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import astrid.packs.vibecomfy.extensions  # noqa: F401  (register H3)
    from astrid.packs.h3_av.shared import engine_facts

    engine_facts.install(comfyui_facts())
    inputs, _files = _worker_inputs(frozen_source)
    entry = entries.find_entry("h3")
    build_hook = entries.entry_hooks(entry)["build"]
    # The contribution loader deliberately imports its pack under a private
    # namespace; patch the same module instance its worker build will use.
    conform = build_hook.__globals__["_shared"]("conform")

    def unexpected_reconform(*_args, **_kwargs):
        pytest.fail("worker attempted to conform an already prepared source")

    monkeypatch.setattr(conform, "conform_source", unexpected_reconform)
    built = entries.build_entry(entry, inputs, tmp_path / "worker-job")
    assert built is not None
    worker_conform = built["plan"]["source_conform"]
    expected_conform = frozen_source["conform"]
    assert {key: worker_conform[key] for key in expected_conform} == expected_conform
    measurement = worker_conform["decoded_av"]
    assert measurement["video_frames"] == expected_conform["output"]["frames"]
    assert measurement["audio_samples"] > 0
    assert measurement["audio_sample_rate"] == 32000
    assert measurement["fractional_mismatch"] <= 0.005
    assert built["assets"]["source_video"].read_bytes() == Path(frozen_source["source_path"]).read_bytes()
    state = json.loads((tmp_path / "worker-job" / "h3" / "build-state.json").read_text(encoding="utf-8"))
    assert state["request_digest"] == frozen_source["request"].digest
