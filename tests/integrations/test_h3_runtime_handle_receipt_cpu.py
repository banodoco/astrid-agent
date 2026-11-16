"""Runtime receipt proof for digest-handle H3 input on the CPU RunPod harness."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from astrid.core.generation.delivery import deliver_generation
from astrid.sdk.generation import GenerationFacade

from tests.integrations.test_runpod_slot_path_cpu import (
    SlotPod,
    _drive,
    _h3_built_classes,
    _assert_ran_on_the_machine,
)
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(600)]


def _authority_context(task_data):
    """Find Runtime's admitted authority context in the public task representation."""
    found = []

    def visit(value):
        if isinstance(value, dict):
            if isinstance(value.get("authority_context"), dict):
                found.append(value["authority_context"])
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(task_data)
    return next((item for item in found if "media_handles" in item or "h3_prepared_source" in item), {})


def test_h3_digest_handle_is_admitted_with_prepared_source_receipt(
    tmp_path, monkeypatch, real_runtime, tuple_sources
):
    """A source handle crosses real SDK/Runtime admission and the fake CPU machine path."""
    # Reuse the independently qualified marker/pulse source so the admitted handle
    # names the exact bytes exercised by the build/compose CPU test.
    from tests.packs.h3_av.test_handle_build_compose import (
        _gray_markers,
        _pulse_peak,
        _source_markers_and_pulse,
    )

    source = tmp_path / "markers-pulse.mp4"
    _source_markers_and_pulse(source)
    source_digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": source_digest, "range": [0.5, 2.5]},
        "output": {"duration": 2},
        "content": {"prompt": "Keep the source outside the short generated interval."},
        "changes": {
            "video": [{"during": [0.25, 0.5], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [],
        },
        "references": [],
        "overrides": {},
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request))
    classes = set(json.loads((Path(__file__).resolve().parents[1]
                              / "fixtures/h3_cpu/object_info_h3_runpod_graph_closure.json").read_text()))
    classes |= _h3_built_classes(request_path, {source_digest: str(source)}, tmp_path)

    # The simulated provider removes the run directory during reset. Preserve
    # that run's uploaded file bytes immediately before the existing cleanup.
    retained_run: dict[str, object] = {}
    reset = SlotPod._reset

    def retain_run_before_reset(pod, directory, detached):
        for job in reversed(pod.generations):
            if job.get("entry") != "h3":
                continue
            source_video = job.get("execution", {}).get("source_video")
            if not isinstance(source_video, str):
                continue
            run_root = "/".join(source_video.split("/")[:5])
            retained_run["job"] = job
            retained_run["files"] = {
                name: bytes(data) for name, data in pod.files.items()
                if name == run_root or name.startswith(run_root + "/")
            }
            break
        return reset(pod, directory, detached)

    monkeypatch.setattr(SlotPod, "_reset", retain_run_before_reset)

    def call(facade, controls, _tmp):
        imported = controls["client"].media.import_file(
            project=controls["project"], path=source, idempotency_key="h3-marker-pulse-source"
        )
        assert imported.ok, imported.to_dict()
        imported_digest = imported.data.get("digest") or imported.data.get("media_id")
        assert imported_digest == source_digest, imported.data
        # Deliberately omit asset_map: the public digest handle must be resolved
        # through the Runtime media API and recorded at admission.
        return facade.video(entry="h3", request=str(request_path), **controls)

    with _drive(("video", "h3"), tmp_path, monkeypatch, real_runtime, tuple_sources,
                extra_classes=classes, call=call, machine_identity=True) as (
                    client, project_id, provider, result
                ):
        assert result.ok, json.dumps(result.to_dict(), default=str)[:5000]
        delivered = deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        assert delivered, "the Runtime generation produced no delivered output"
        task_result = client.tasks.show(result.task_id)
        assert task_result.ok and task_result.data["state"] == "succeeded"
        task = task_result.data
        authority = _authority_context(task)
        assert authority, json.dumps(task, default=str)[:5000]

        media_handles = authority["media_handles"]
        assert any(item.get("digest") == source_digest and item.get("handle") == source_digest
                   for item in media_handles), media_handles
        prepared = authority["h3_prepared_source"]
        assert prepared["source_asset"] == source_digest, prepared
        assert prepared["source_sha256"] == source_digest.removeprefix("sha256:"), prepared
        assert prepared["digest"].startswith("sha256:") and prepared["digest"] != source_digest, prepared
        assert prepared["request_digest"], prepared

        _assert_ran_on_the_machine(provider)
        assert provider.generations, "the CPU machine did not receive the H3 job"
        execution = provider.generations[0]["execution"]
        public_inputs = provider.generations[0]["inputs"]
        assert "request" in public_inputs, public_inputs
        assert "managed_assets" in execution, execution
        # The task receipt's prepared digest must name the bytes uploaded both as
        # the worker's source_video and as the source_video graph asset.
        uploaded = retained_run.get("files")
        assert isinstance(uploaded, dict), "fake provider cleanup snapshot is missing"
        prepared_bytes = uploaded[execution["source_video"]]
        prepared_sha256 = hashlib.sha256(prepared_bytes).hexdigest()
        assert prepared_sha256 == prepared["digest"].removeprefix("sha256:"), prepared
        prepared_path = tmp_path / "uploaded-prepared-source.mp4"
        prepared_path.write_bytes(prepared_bytes)
        original_frames = _gray_markers(source)
        prepared_frames = _gray_markers(prepared_path)
        assert len(prepared_frames) == 48
        marker = lambda frames, index: float(frames[index, 280:296, 500:524].mean())
        for output_frame, source_frame in ((0, 12), (24, 36)):
            assert abs(marker(prepared_frames, output_frame) - marker(original_frames, source_frame)) <= 8
        assert abs(_pulse_peak(prepared_path, 1.08) - 880) <= 15
        assert abs(_pulse_peak(prepared_path, 0.55) - 440) <= 15
        managed_archive = uploaded[execution["managed_assets"]]
        with zipfile.ZipFile(io.BytesIO(managed_archive)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            source_rows = [row for row in manifest["assets"] if row["binding"] == "source_video"]
            assert len(source_rows) == 1, manifest
            source_row = source_rows[0]
            assert source_row["sha256"] == prepared_sha256, source_row
            assert source_row["lineage"]["kind"] == "h3_source_conform", source_row
            assert source_row["lineage"]["source_sha256"] == source_digest, source_row
            assert source_row["lineage"]["source_range_seconds"] == ["0.5", "2.5"], source_row

        # Reconstruct the exact uploaded machine job from its preserved bytes,
        # then exercise the real worker build and after_outputs hook locally.
        job = retained_run["job"]
        assert job is provider.generations[0]
        local_inputs_root = tmp_path / "replayed-machine-inputs"
        local_inputs_root.mkdir()

        def localize(value):
            if isinstance(value, str) and value in uploaded:
                destination = local_inputs_root / Path(value).name
                destination.write_bytes(uploaded[value])
                return str(destination)
            if isinstance(value, dict):
                return {key: localize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [localize(item) for item in value]
            return value

        worker_inputs = localize(job["inputs"])
        worker_files = localize(job["execution"])
        from astrid.packs.vibecomfy.local_job import _validate_h3_prepared_handoff

        _validate_h3_prepared_handoff(worker_inputs, worker_files)
        from astrid.core.generation import entries
        import astrid.packs.vibecomfy.extensions  # noqa: F401
        entry = entries.find_entry("h3")
        build_hook = entries.entry_hooks(entry)["build"]
        conform = build_hook.__globals__["_shared"]("conform")

        def unexpected_reconform(*_args, **_kwargs):
            pytest.fail("replayed worker re-conformed a frozen prepared source")

        monkeypatch.setattr(conform, "conform_source", unexpected_reconform)
        replay_dir = tmp_path / "replayed-machine-job"
        built = entries.build_entry(entry, worker_inputs, replay_dir)
        assert built is not None
        replay_conform = built["plan"]["source_conform"]
        assert replay_conform["output"]["sha256"] == prepared_sha256
        state = json.loads((replay_dir / "h3" / "build-state.json").read_text())
        assert state["request_digest"] == prepared["request_digest"]
        assert hashlib.sha256(Path(built["assets"]["source_video"]).read_bytes()).hexdigest() == prepared_sha256

        from tests.packs.h3_av.test_handle_build_compose import _generated

        generated = tmp_path / "generated-video-only.mkv"
        _generated(generated)
        finish_job = {**entries.entry_job(entry, worker_inputs, replay_dir, hooks=entries.entry_hooks(entry)),
                      "manifest": {"models": [], "extra": {"nodes": []}}}
        engine = entries.engine_table()["comfyui"]
        finished = engine.finish(finish_job, [str(generated)])
        assert finished and Path(finished[0]).is_file()
        verification_path = replay_dir / "h3" / "verify.json"
        assert verification_path.is_file()
        verification = json.loads(verification_path.read_text())
        assert verification["preservation"]["status"] == "protected_sample_evidence"
        source_frames = _gray_markers(source)
        output_frames = _gray_markers(Path(finished[0]))
        assert len(output_frames) == 48
        marker = lambda frames, index: float(frames[index, 280:296, 500:524].mean())
        for output_frame, source_frame in ((0, 12), (24, 36)):
            assert abs(marker(output_frames, output_frame) - marker(source_frames, source_frame)) <= 8
        assert marker(output_frames, 8) > 225
        assert abs(_pulse_peak(Path(finished[0]), 1.08) - 880) <= 15
        assert abs(_pulse_peak(Path(finished[0]), 0.55) - 440) <= 15
