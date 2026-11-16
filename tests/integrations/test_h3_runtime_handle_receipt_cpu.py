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
        prepared_bytes = provider.files[execution["source_video"]]
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
        with zipfile.ZipFile(io.BytesIO(provider.files[execution["managed_assets"]])) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            source_rows = [row for row in manifest["assets"] if row["binding"] == "source_video"]
            assert len(source_rows) == 1, manifest
            source_row = source_rows[0]
            assert source_row["sha256"] == prepared_sha256, source_row
            assert source_row["lineage"]["kind"] == "h3_source_conform", source_row
            assert source_row["lineage"]["source_sha256"] == source_digest, source_row
            assert source_row["lineage"]["source_range_seconds"] == ["0.5", "2.5"], source_row
