"""Canonical H3 media handles remain safe file-map IDs through resolution and transport."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation import entries


@pytest.mark.parametrize("handle", [
    "run:run_1/primary",
    "run:run_1/primary#0",
    "task:task-1/primary",
    "task:task-1/primary#2",
])
def test_only_h3_asset_map_accepts_canonical_qualified_output_handle_ids(handle: str) -> None:
    asset_map = {handle: "/tmp/media.mp4"}
    h3_inputs = entries.resolve_entry_inputs("h3", {"asset_map": asset_map}, mode="v2v")[1]
    assert h3_inputs["asset_map"] == asset_map

    generic = {"id": "other-entry", "inputs": [
        {"name": "asset_map", "type": "file_map", "required": False},
    ]}
    with pytest.raises(ValueError, match="nonempty map"):
        entries.validate_inputs(generic, {"asset_map": asset_map})


@pytest.mark.parametrize("bad_key", ["a/b", "run:run_1/primary#x", "run:run_1/a/b#0"])
def test_h3_asset_map_rejects_arbitrary_or_malformed_slash_ids(bad_key: str) -> None:
    with pytest.raises(ValueError, match="nonempty map"):
        entries.resolve_entry_inputs("h3", {"asset_map": {bad_key: "/tmp/media.mp4"}}, mode="v2v")


def test_h3_handle_resolution_merges_json_map_with_document_relative_siblings(tmp_path: Path, monkeypatch) -> None:
    from astrid.sdk import generation

    handle = "run:source-run/primary#0"
    source_bytes = b"synthetic project media"
    digest = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    sibling = tmp_path / "mask.png"
    sibling.write_bytes(b"caller supplied relative sibling")
    map_path = tmp_path / "assets.json"
    map_path.write_text(json.dumps({"mask": "mask.png"}), encoding="utf-8")
    request = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": handle, "range": [0, 1]},
        "output": {"duration": 1},
        "content": {"prompt": "Use the project source."},
        "changes": {"video": [{"during": [0, 1], "area": {"full_frame": True},
                                  "action": "generate"}], "audio": []},
        "references": [],
        "overrides": {},
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")

    class ProjectMedia:
        def __init__(self):
            self.opened: list[Path] = []

        def show(self, project, shown_digest):
            assert project == "selected-project" and shown_digest == digest
            return SimpleNamespace(ok=True, data={
                "digest": digest, "filename": "source.mp4", "media_type": "video/mp4",
                "size": len(source_bytes),
            })

        def open(self, project, opened_digest, **kwargs):
            assert project == "selected-project" and opened_digest == digest
            cache = Path(kwargs["cache_root"])
            cache.mkdir(parents=True)
            path = cache / "source.mp4"
            path.write_bytes(source_bytes)
            self.opened.append(path)
            return SimpleNamespace(ok=True, data={"local_path": str(path)})

    media = ProjectMedia()
    client = SimpleNamespace(
        media=media,
        runs=SimpleNamespace(show=lambda run_id: {"task_ids": ["task-1"]}),
        tasks=SimpleNamespace(list_managed_outputs=lambda task_id: [{
            "run_id": "source-run", "task_id": task_id, "output_port": "primary",
            "ordinal": 0, "digest": digest, "filename": "source.mp4",
        }]),
    )
    monkeypatch.setattr(generation, "_resolve_invoke_destination",
                        lambda **_kwargs: (None, "selected-project"))
    observed: dict = {}

    def capture_admission(self, **kwargs):
        inputs = kwargs["inputs"]
        mapped = inputs["asset_map"]
        assert mapped["mask"] == str(sibling.resolve())
        assert Path(mapped["mask"]).read_bytes() == b"caller supplied relative sibling"
        assert mapped[handle] in {str(path) for path in media.opened}
        assert Path(mapped[handle]).read_bytes() == source_bytes
        observed["map"] = mapped
        observed["context"] = kwargs["idempotency_context"]
        observed["project"] = kwargs["project"]
        return "captured"

    monkeypatch.setattr(generation.GenerationFacade, "_generate_on_target_impl", capture_admission)
    result = generation.GenerationFacade()._generate_on_target(
        model="h3", inputs={"request": str(request_path), "asset_map": str(map_path)},
        client=client, project="caller-project", out=None, project_root=None,
        idempotency_context={},
    )

    assert result == "captured"
    assert observed["project"] == "selected-project"
    assert observed["context"]["media_handles"][0]["handle"] == handle
    assert observed["map"]["mask"] == str(sibling.resolve())
    assert not media.opened[0].exists()


def test_entry_inputs_archive_round_trips_qualified_handle_key(tmp_path: Path) -> None:
    from astrid.core.generation.job_inputs import read_archive, write_archive

    handle = "run:run_2/video#1"
    source = tmp_path / "source.mp4"
    source.write_bytes(b"prepared media")
    archive = tmp_path / "entry-inputs.zip"
    ports = [{"name": "asset_map", "type": "file_map", "required": False}]

    write_archive(archive, {"asset_map": {handle: str(source)}}, ports)
    restored = read_archive(archive, tmp_path / "worker-inputs")

    assert list(restored["asset_map"]) == [handle]
    restored_path = Path(restored["asset_map"][handle])
    assert restored_path.read_bytes() == b"prepared media"


def test_run_handle_passes_h3_validation_build_and_state_archive_with_same_asset_key(
    tmp_path: Path, monkeypatch,
) -> None:
    from astrid.core.generation import entries
    from astrid.core.generation.job_inputs import read_archive, write_archive
    from astrid.sdk import generation
    from astrid.sdk.generation import GenerationFacade
    from tests.packs.h3_av.test_handle_build_compose import _source_markers_and_pulse

    source = tmp_path / "source.mp4"
    _source_markers_and_pulse(source)
    source_bytes = source.read_bytes()
    source_digest = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
    handle = "run:middle-run/primary#0"
    request_document = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": handle, "range": [0.5, 2.5]},
        "output": {"duration": 2},
        "content": {"prompt": "Preserve the source around the generated interval."},
        "changes": {"video": [{"during": [0.25, 0.5], "area": {"full_frame": True},
                                  "action": "generate"}], "audio": []},
        "references": [],
        "overrides": {},
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request_document), encoding="utf-8")

    class ProjectMedia:
        def __init__(self):
            self.opened: list[Path] = []

        def show(self, project, digest):
            assert project == "selected-project" and digest == source_digest
            return SimpleNamespace(ok=True, data={
                "digest": source_digest, "filename": "source.mp4", "media_type": "video/mp4",
                "size": len(source_bytes),
            })

        def open(self, project, digest, **kwargs):
            assert project == "selected-project" and digest == source_digest
            cache = Path(kwargs["cache_root"])
            cache.mkdir(parents=True)
            path = cache / "source.mp4"
            path.write_bytes(source_bytes)
            self.opened.append(path)
            return SimpleNamespace(ok=True, data={"local_path": str(path)})

    media = ProjectMedia()
    client = SimpleNamespace(
        media=media,
        runs=SimpleNamespace(show=lambda run_id: {"task_ids": ["middle-task"]}),
        tasks=SimpleNamespace(list_managed_outputs=lambda task_id: [{
            "run_id": "middle-run", "task_id": task_id, "output_port": "primary",
            "ordinal": 0, "digest": source_digest, "filename": "source.mp4",
        }]),
    )
    monkeypatch.setattr(generation, "_resolve_invoke_destination",
                        lambda **_kwargs: (None, "selected-project"))
    evidence: dict = {}
    job_dir = tmp_path / "h3-build"
    archive_path = tmp_path / "entry-inputs.zip"

    def validate_and_build(self, **kwargs):
        inputs = kwargs["inputs"]
        description, validated = entries.resolve_entry_inputs("h3", inputs, mode="v2v")
        assert description["id"] == "h3"
        assert set(validated["asset_map"]) == {handle}
        assert Path(validated["asset_map"][handle]).read_bytes() == source_bytes

        entry = entries.find_entry("h3")
        assert entry is not None
        write_archive(archive_path, validated, entry["inputs"])
        transported = read_archive(archive_path, tmp_path / "worker-inputs")
        assert list(transported["asset_map"]) == [handle]
        assert Path(transported["asset_map"][handle]).read_bytes() == source_bytes

        import astrid.packs.vibecomfy.extensions  # noqa: F401
        built = entries.build_entry(entry, validated, job_dir)
        state_path = job_dir / "h3" / "build-state.json"
        assert built is not None and built["workflow"] is not None
        assert state_path.is_file()
        state = json.loads(state_path.read_text(encoding="utf-8"))
        preparation = state["preparation"]
        assert state["request"]["source"]["asset"] == handle
        input_record = next(row for row in preparation["input_assets"] if row["asset"] == handle)
        assert input_record["member"] == f"assets/{hashlib.sha256(handle.encode()).hexdigest()[:16]}.mp4"
        assert input_record["sha256"] == source_digest[7:]
        source_conform = preparation["source_conform"]
        assert source_conform["source"]["sha256"] == source_digest[7:]
        assert source_conform["source"]["range_seconds"] == ["0.5", "2.5"]
        graph_source = Path(built["assets"]["source_video"])
        assert graph_source.is_file()
        assert hashlib.sha256(graph_source.read_bytes()).hexdigest() == source_conform["output"]["sha256"]
        evidence["context"] = kwargs["idempotency_context"]
        evidence["state"] = state
        evidence["materialized"] = media.opened[0]
        evidence["input_archive"] = archive_path
        evidence["graph_source_sha256"] = hashlib.sha256(graph_source.read_bytes()).hexdigest()
        return built

    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl", validate_and_build)
    request_inputs = {"request": str(request_path)}
    assert "asset_map" not in request_inputs
    built = GenerationFacade()._generate_on_target(
        model="h3", inputs=request_inputs, client=client, project="caller-project",
        out=None, project_root=None, idempotency_context={},
    )

    assert built["workflow"] is not None
    assert "asset_map" not in request_inputs
    assert evidence["context"]["media_handles"] == [
        {"port": "request.asset", "handle": handle, "digest": source_digest,
         "output": {"run_id": "middle-run", "task_id": "middle-task",
                    "output_port": "primary", "ordinal": 0}},
    ]
    assert evidence["graph_source_sha256"] == evidence["state"]["preparation"]["source_conform"]["output"]["sha256"]
    assert not evidence["materialized"].exists()
