"""Real Runtime H3 admission for qualified output handles and project refusals."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.core.generation.delivery import deliver_generation
from tests.integrations.test_h3_runtime_handle_receipt_cpu import _authority_context
from tests.integrations.test_runpod_slot_path_cpu import (
    SlotPod,
    _drive,
    _h3_built_classes,
    _assert_ran_on_the_machine,
)
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(600)]


def _av_source(path: Path) -> Path:
    import subprocess

    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1024x576:rate=24:duration=3",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
    ], check=True, timeout=120)
    return path


def _request(path: Path, handle: str) -> Path:
    document = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": handle, "range": [0.5, 2.5]},
        "output": {"duration": 2},
        "content": {"prompt": "Preserve the selected source interval."},
        "changes": {"video": [{"during": [0.25, 0.5], "area": {"full_frame": True},
                                "action": "generate"}], "audio": []},
        "references": [],
        "overrides": {},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _h3_classes(request_path: Path, source: Path, handle: str, tmp_path: Path) -> set[str]:
    closure_path = (Path(__file__).resolve().parents[1]
                    / "fixtures/h3_cpu/object_info_h3_runpod_graph_closure.json")
    classes = set(json.loads(closure_path.read_text(encoding="utf-8")))
    classes |= _h3_built_classes(request_path, {handle: str(source)}, tmp_path)
    return classes


def test_qualified_prior_run_handle_admits_h3_without_asset_map(
    tmp_path, monkeypatch, real_runtime, tuple_sources,
):
    source = _av_source(tmp_path / "prior-generated-source.mp4")
    source_bytes = source.read_bytes()
    source_digest = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    placeholder = "run:prior-run/primary#0"
    request_path = _request(tmp_path / "h3-request.json", placeholder)
    classes = _h3_classes(request_path, source, placeholder, tmp_path)

    # Give the first, real Runtime-backed generation a deterministic audiovisual
    # output. The output is still created and indexed by the real fake-provider
    # machine path, so the following H3 request resolves an actual prior task.
    def output_for(_pod, job):
        if job.get("mode") == "i2v":
            return "prior-source.mp4", source_bytes, "video/mp4"
        return ("h3-output.mp4", source_bytes, "video/mp4")

    monkeypatch.setattr(SlotPod, "output_for", output_for)
    observed: dict[str, object] = {}

    def call(facade, controls, tmp):
        from PIL import Image

        image = tmp / "start.png"
        Image.new("RGB", (64, 64), (20, 40, 220)).save(image)
        prior = facade.video(mode="i2v", prompt="A static blue test frame", image_ref=str(image), **controls)
        assert prior.ok, json.dumps(prior.to_dict(), default=str)[:4000]
        from astrid.sdk.media_handles import _data, _rows, output_handle

        output_result = controls["client"].tasks.list_managed_outputs(prior.task_id)
        prior_outputs = _rows(_data(output_result, f"outputs of task {prior.task_id}"))
        output = next((row for row in prior_outputs if row.get("role") != "thumbnail"), None)
        assert output is not None, f"prior task has no non-thumbnail managed output: {prior_outputs!r}"
        output_digest = str(output.get("digest") or output.get("object_id") or "")
        if output_digest and not output_digest.startswith("sha256:"):
            output_digest = "sha256:" + output_digest
        assert output_digest == source_digest, output
        handle = output_handle(output)
        assert handle and handle.startswith("run:")
        request_path.write_text(json.dumps({
            **json.loads(request_path.read_text(encoding="utf-8")),
            "source": {"asset": handle, "range": [0.5, 2.5]},
        }), encoding="utf-8")
        observed["handle"] = handle
        observed["prior_run_id"] = output["run_id"]
        observed["prior_task_id"] = prior.task_id
        observed["prior_output"] = {
            "run_id": output["run_id"], "task_id": output["task_id"],
            "output_port": output["output_port"], "ordinal": int(output.get("ordinal") or 0),
        }
        # No asset_map is supplied: Runtime resolves this run handle in-project.
        observed["caller_supplied_asset_map"] = False
        result = facade.video(entry="h3", request=str(request_path), **controls)
        return result

    with _drive(("video", "i2v"), tmp_path, monkeypatch, real_runtime, tuple_sources,
                extra_classes=classes, call=call, machine_identity=True) as (
                    client, project_id, provider, result
                ):
        assert result.ok, json.dumps(result.to_dict(), default=str)[:5000]
        assert deliver_generation(result, client=client, out=tmp_path / "delivered", project=project_id)
        task_result = client.tasks.show(result.task_id)
        assert task_result.ok and task_result.data["state"] == "succeeded"
        authority = _authority_context(task_result.data)
        assert authority, json.dumps(task_result.data, default=str)[:5000]
        row = next(item for item in authority["media_handles"]
                   if item.get("handle") == observed["handle"])
        assert row["digest"] == source_digest, row
        assert row["output"] == observed["prior_output"], row
        prepared = authority["h3_prepared_source"]
        assert prepared["source_asset"] == observed["handle"], prepared
        assert prepared["source_sha256"] == source_digest.removeprefix("sha256:"), prepared
        assert observed["caller_supplied_asset_map"] is False
        _assert_ran_on_the_machine(provider)
        assert any(job.get("entry") == "h3" for job in provider.generations)


def test_existing_digest_from_another_project_is_refused_before_h3_task(
    tmp_path, monkeypatch, real_runtime, tuple_sources,
):
    source = _av_source(tmp_path / "foreign-project-source.mp4")
    source_digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request_path = _request(tmp_path / "foreign-request.json", source_digest)
    classes = _h3_classes(request_path, source, source_digest, tmp_path)

    def call(facade, controls, _tmp):
        client = controls["client"]
        imported = client.media.import_file(
            project=controls["project"], path=source, idempotency_key="foreign-project-media",
        )
        assert imported.ok, imported.to_dict()
        assert (imported.data.get("digest") or imported.data.get("media_id")) == source_digest
        foreign = client.projects.create(name="Different project", slug="different-project",
                                         idempotency_key="different-project")
        assert foreign.ok, foreign.to_dict()
        from astrid.sdk.media_handles import MediaHandleError

        with pytest.raises(MediaHandleError, match="no media in project") as caught:
            facade.video(entry="h3", request=str(request_path), project=foreign.data["project_id"],
                         client=client, placement=controls["placement"], wait=True)
        return {"refused": True, "error": str(caught.value)}

    with _drive(("video", "h3"), tmp_path, monkeypatch, real_runtime, tuple_sources,
                extra_classes=classes, call=call, machine_identity=True) as (
                    _client, _project_id, provider, result
                ):
        assert result["refused"] is True
        assert "no media in project" in result["error"]
        assert not provider.generations, "project-scoped handle refusal occurred after machine task admission"
