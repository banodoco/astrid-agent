"""Remote H3 entry inputs carry the controller's immutable prepared source."""
from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from astrid.core.generation import entries
from astrid.core.generation.engine_facts import comfyui_facts
from astrid.core.generation.job_inputs import read_archive, write_archive
from astrid.packs.h3_av.shared.handoff import _h3_remote_handoff
from astrid.packs.h3_av.shared import engine_facts
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.vibecomfy.local_job import _validate_h3_prepared_handoff


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True, text=True, timeout=60)


def _source(path: Path) -> None:
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x480:rate=24000/1001:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", str(path))


def _controller_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    publish_failure: str | None = None,
    events: list[str] | None = None,
) -> tuple[dict, dict, bytes, bytes, dict]:
    import astrid.packs.vibecomfy.extensions  # noqa: F401  (register H3)

    events = events if events is not None else []
    engine_facts.install(comfyui_facts())
    source = tmp_path / "original.mp4"
    _source(source)
    document = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source", "range": [0.5, 2.0], "fit": "fit"},
        "output": {"duration": 1.5},
        "content": {"prompt": "Keep the selected source interval."},
        "changes": {
            "video": [{"during": [0, 1.5], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [],
        },
        "references": [],
        "overrides": {"seed": 7, "steps": 8},
    }
    request = normalize_request(document)
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(document), encoding="utf-8")
    entry = entries.find_entry("h3")
    assert entry is not None
    build_hook = entries.entry_hooks(entry)["build"]
    compile_module = build_hook.__globals__["_shared"]("compile")
    real_compile = compile_module.compile_preparation
    managed_path = tmp_path / "managed" / "source.mp4"
    descriptor_record: dict[str, object] = {}

    def publish_source(path: Path, expected: str) -> tuple[Path, dict[str, object]]:
        events.append("publish")
        data = Path(path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        managed_path.write_bytes(data if publish_failure != "reopened_bytes" else b"reopened mismatch")
        digest = hashlib.sha256(data).hexdigest()
        descriptor = {"digest": "sha256:" + digest, "sha256": digest,
                      "bytes": len(data), "filename": Path(path).name}
        if publish_failure == "descriptor":
            descriptor["digest"] = "sha256:" + "0" * 64
        descriptor_record.update(descriptor)
        events.append("reopen")
        return managed_path, descriptor

    def observed_compile(preparation, *args, **kwargs):
        events.append("compile")
        source_asset = next(row for row in preparation["assets"] if row["asset"] == "source")
        if events.count("compile") == 1:
            assert Path(source_asset["path"]).resolve() == managed_path.resolve()
        assert hashlib.sha256(Path(source_asset["path"]).read_bytes()).hexdigest() == preparation[
            "source_conform"]["output"]["sha256"]
        return real_compile(preparation, *args, **kwargs)

    monkeypatch.setattr(compile_module, "compile_preparation", observed_compile)
    job_dir = tmp_path / "job"
    if publish_failure:
        with pytest.raises(ValueError, match=(
                "managed H3 source descriptor digest mismatch" if publish_failure == "descriptor"
                else "reopened managed H3 source digest mismatch")):
            entries.build_entry(
                entry, {"request": str(request_path), "asset_map": {"source": str(source)}},
                job_dir, publish_source=publish_source,
            )
        assert events == ["publish", "reopen"]
        assert descriptor_record
        return ({}, document, b"", b"", {"events": events})

    built = entries.build_entry(
        entry, {"request": str(request_path), "asset_map": {"source": str(source)}},
        job_dir, publish_source=publish_source,
    )
    assert built is not None
    assert events == ["publish", "reopen", "compile"]
    state = json.loads((job_dir / "h3" / "build-state.json").read_text(encoding="utf-8"))
    prepared = Path(state["source"])
    assert prepared.resolve() == managed_path.resolve()
    return ({"job_dir": str(job_dir)}, document, source.read_bytes(), prepared.read_bytes(),
            {"path": managed_path, "descriptor": descriptor_record, "events": events,
             "request_digest": request.digest, "assets": dict(built["assets"]),
             "build_plan": dict(built["plan"])})


def test_h3_remote_handoff_archives_only_prepared_source_and_keeps_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astrid.packs.h3_av.shared.stages import source_asset_lineage
    from astrid.packs.vibecomfy.asset_manifest import build_asset_manifest

    plan, original_request, original_bytes, prepared_bytes, publication = _controller_plan(tmp_path, monkeypatch)
    transport = tmp_path / "transport"
    transport.mkdir()
    handoff = _h3_remote_handoff(plan, transport)
    assert handoff is not None
    prepared_path = Path(handoff["inputs"]["asset_map"]["source"])
    assert prepared_path == publication["path"]
    assert prepared_path.read_bytes() == prepared_bytes
    assert prepared_bytes != original_bytes
    assert handoff["descriptor"] == publication["descriptor"]
    assert publication["events"] == ["publish", "reopen", "compile"]
    assert handoff["lineage"]["source_sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert handoff["lineage"]["source_range"] == ["0.5", "2.0"]
    assert handoff["lineage"]["fit"] == "fit"
    assert handoff["lineage"]["request_digest"] == normalize_request(original_request).digest

    graph_lineage = source_asset_lineage(publication["build_plan"])
    graph_archive_manifest = build_asset_manifest(publication["assets"], lineage=graph_lineage)
    graph_source = next(row for row in graph_archive_manifest["assets"] if row["binding"] == "source_video")
    assert graph_source["sha256"] == handoff["descriptor"]["sha256"]
    assert graph_source["lineage"] == {
        "kind": "h3_source_conform",
        "sha256": "sha256:" + handoff["descriptor"]["sha256"],
        "source_sha256": "sha256:" + hashlib.sha256(original_bytes).hexdigest(),
        "source_range_seconds": ["0.5", "2.0"],
        "fit": "fit",
    }

    archived_handoff = json.loads(
        Path(handoff["inputs"]["asset_map"]["__astrid_h3_prepared__"]).read_text(encoding="utf-8"))
    assert archived_handoff["request_digest"] == handoff["lineage"]["request_digest"]
    assert archived_handoff["conform"]["source"]["range_seconds"] == ["0.5", "2.0"]
    assert archived_handoff["conform"]["output"]["fit"] == "fit"
    assert json.loads(Path(handoff["inputs"]["request"]).read_text(encoding="utf-8")) == original_request

    archive_path = tmp_path / "entry-inputs.zip"
    write_archive(archive_path, handoff["inputs"], [
        {"name": "request", "type": "file", "required": False},
        {"name": "asset_map", "type": "file_map", "required": False},
    ])
    restored = read_archive(archive_path, tmp_path / "worker-inputs")
    assert restored["request"] == str(tmp_path / "worker-inputs" / "files" / "0.json")
    assert Path(restored["asset_map"]["source"]).read_bytes() == prepared_bytes
    assert Path(restored["asset_map"]["__astrid_h3_prepared__"]).read_bytes() == Path(
        handoff["inputs"]["asset_map"]["__astrid_h3_prepared__"]).read_bytes()
    with zipfile.ZipFile(archive_path) as archive:
        archived_files = [archive.read(member) for member in archive.namelist() if member.startswith("files/")]
    assert original_bytes not in archived_files
    assert prepared_bytes in archived_files


@pytest.mark.parametrize("failure", ["descriptor", "reopened_bytes"])
def test_controller_refuses_invalid_prepared_managed_source_before_compile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    events: list[str] = []
    _controller_plan(tmp_path, monkeypatch, publish_failure=failure, events=events)


def test_remote_archive_reconstruction_builds_from_frozen_source_without_reconforming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import astrid.packs.vibecomfy.extensions  # noqa: F401  (register H3)

    plan, original_request, original_bytes, prepared_bytes, publication = _controller_plan(tmp_path, monkeypatch)
    request_digest = normalize_request(original_request).digest
    original_source_path = Path(plan["job_dir"]) / "h3" / "build-state.json"
    state = json.loads(original_source_path.read_text(encoding="utf-8"))
    prepared_path = Path(state["source"])
    transport = tmp_path / "transport"
    transport.mkdir()

    handoff = _h3_remote_handoff(plan, transport)
    assert handoff is not None
    assert prepared_path.read_bytes() == prepared_bytes
    managed_source = Path(publication["path"])
    assert managed_source.read_bytes() == prepared_bytes

    entry_archive = tmp_path / "entry-inputs.zip"
    write_archive(entry_archive, handoff["inputs"], [
        {"name": "request", "type": "file", "required": False},
        {"name": "asset_map", "type": "file_map", "required": False},
    ])
    worker_inputs = read_archive(entry_archive, tmp_path / "worker-entry-inputs")
    worker_files = {"source_video": str(managed_source)}
    _validate_h3_prepared_handoff(worker_inputs, worker_files)

    entry = entries.find_entry("h3")
    assert entry is not None
    build_hook = entries.entry_hooks(entry)["build"]
    # The contribution loader uses the private pack module that the worker hook
    # itself loaded, so patch precisely the function instance it would call.
    hook_conform = build_hook.__globals__["_shared"]("conform")

    def unexpected_reconform(*_args, **_kwargs):
        pytest.fail("worker attempted to conform the controller-prepared source again")

    monkeypatch.setattr(hook_conform, "conform_source", unexpected_reconform)
    built = entries.build_entry(entry, worker_inputs, tmp_path / "worker-build")
    assert built is not None
    worker_source = Path(built["assets"]["source_video"])
    assert hashlib.sha256(worker_source.read_bytes()).hexdigest() == handoff["descriptor"]["sha256"]
    assert worker_source.read_bytes() == prepared_bytes
    assert hashlib.sha256(original_bytes).hexdigest() == handoff["lineage"]["source_sha256"]
    worker_state = json.loads(
        (tmp_path / "worker-build" / "h3" / "build-state.json").read_text(encoding="utf-8"))
    assert worker_state["request_digest"] == request_digest
    assert worker_state["preparation"]["source_conform"]["source"]["sha256"] == handoff[
        "lineage"]["source_sha256"]
    assert worker_state["preparation"]["source_conform"]["output"]["sha256"] == handoff[
        "descriptor"]["sha256"]
    assert worker_state["preparation"]["managed_source"] == handoff["descriptor"]
    assert original_bytes != worker_source.read_bytes()

    # The worker consumes the validated files from its input map; no call to
    # the raw original source is available after archive reconstruction.
    assert worker_inputs["asset_map"]["source"] != str(tmp_path / "original.mp4")
