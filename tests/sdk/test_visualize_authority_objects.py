"""A visualize task's spec carries digests, not its 1-2 MB snapshot.

Every ``rendering.timeline_visualize`` task used to inline the frozen timeline
snapshot twice (``inputs.filmstrip_authority`` and ``authority_context``), and
the Runtime copied it into the run and the admission receipt. The authority and
the snapshot's timeline and registry are now managed objects; the existing host
materializes them as the executor's input files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from astrid.sdk.invocation import _invoke_internal_result

ASSET = b"source-frame-png"
ASSET_DIGEST = "sha256:" + hashlib.sha256(ASSET).hexdigest()


def _snapshot() -> dict:
    # Shaped like a real composed-capture snapshot: big config and clips, small registry.
    clips = [{"id": f"c{i:03d}", "asset": "frame", "at": i * 0.5, "params": {"text": "x" * 900}} for i in range(600)]
    return {
        "project_slug": "p", "timeline_id": "tl", "timeline_name": "almost-ready", "fps_rational": "30/1",
        "duration_frames": 2700, "video_digest": "sha256:" + "d" * 64, "clips": clips,
        "config": {"timeline": {"clips": clips[:500]}, "fps": 30},
        "registry": {"assets": {"frame": {"media_id": ASSET_DIGEST, "content_sha256": ASSET_DIGEST, "type": "image/png"}}},
        "metadata": {}, "occurrences": [], "scripts": [], "tracks": [],
    }


def _authority() -> dict:
    return {"mode": "composed_capture", "capture_identity": "sha256:" + "d" * 64, "capture_snapshot": _snapshot(),
            "project_id": "p", "timeline_id": "tl", "timeline_slug": "almost-ready", "include_media": False}


class Runtime:
    """A Runtime client: media import (preflight) and transport object ingestion (admission)."""

    def __init__(self, *, can_import: bool = True):
        self.objects = {ASSET_DIGEST: ASSET}
        self.created = []

        class Tasks:
            def create(inner, **kwargs):
                self.created.append(kwargs)
                return SimpleNamespace(ok=False, data=None, error={"message": "captured"})

        self.tasks = Tasks()
        if can_import:
            def import_bytes(*, project, data, filename, media_type, idempotency_key=None):
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                self.objects[digest] = data
                return SimpleNamespace(ok=True, data={"digest": digest, "object_id": digest, "filename": filename}, error=None)

            self.media = SimpleNamespace(import_bytes=import_bytes)

            def ingest_project_object(project, data, *, media_type, idempotency_key, filename=None):
                digest = "sha256:" + hashlib.sha256(data).hexdigest()
                self.objects[digest] = data
                return {"data": {"digest": digest, "object_id": digest}, "receipt": {}}

            self._remote = SimpleNamespace(_transport=SimpleNamespace(ingest_project_object=ingest_project_object))
        else:
            self.media = SimpleNamespace()


@pytest.fixture(autouse=True)
def frozen_authority(monkeypatch):
    monkeypatch.setattr("astrid.sdk.timeline_filmstrip.prepare_filmstrip", lambda inputs, *, project, client: _authority())


def _admit(runtime) -> dict:
    # timeline_visualize is a private backend of `timelines visualize`
    result = _invoke_internal_result("rendering.timeline_visualize", kind="executor", project="p",
                                     client=runtime, inputs={"view": "filmstrip"})
    assert runtime.created, result.error
    return runtime.created[-1]


def test_visualize_spec_carries_digests_not_the_snapshot():
    runtime = Runtime()
    admitted = _admit(runtime)
    assert len(json.dumps(admitted["spec"])) < 20_000
    inputs, authority = admitted["spec"]["inputs"], admitted["spec"]["authority_context"]
    assert set(inputs["filmstrip_authority"]) == {"digest", "filename", "media_type", "size_bytes"}
    assert "capture_snapshot" not in authority and authority["authority_object"] == inputs["filmstrip_authority"]["digest"]
    for port in ("filmstrip_authority", "timeline", "assets_registry"):
        assert inputs[port]["digest"] in admitted["input_manifest"]
    assert ASSET_DIGEST in admitted["input_manifest"]  # registry media stay authorized
    # the stored authority carries the frozen snapshot unchanged, plus the element-resource binding in metadata
    stored = json.loads(runtime.objects[inputs["filmstrip_authority"]["digest"]])["capture_snapshot"]
    expected = _snapshot()
    assert {k: v for k, v in stored.items() if k != "metadata"} == {k: v for k, v in expected.items() if k != "metadata"}
    assert set(stored["metadata"]) == {"element_resource_binding_hash", "element_resources"}
    assert stored["metadata"]["element_resources"] == {}


def test_snapshot_admission_without_object_ingestion_is_refused():
    runtime = Runtime(can_import=False)
    result = _invoke_internal_result("rendering.timeline_visualize", kind="executor", project="p",
                                     client=runtime, inputs={"view": "filmstrip"})
    assert runtime.created == []
    assert result.ok is False
    assert result.error["message"] == "snapshot reference admission requires Runtime object ingestion"


def test_the_current_host_materializes_the_objects_as_the_executors_input_files(tmp_path):
    runtime = Runtime()
    admitted = _admit(runtime)
    host = GenericPackHost(pack_roots=[Path("astrid/packs")], client=SimpleNamespace(get_object=lambda d: runtime.objects["sha256:" + d]))
    task = {"spec": admitted["spec"], "input_object_ids": admitted["input_manifest"]}
    file_inputs = frozenset(p.name for p in host_capability(host).definition.inputs if p.type == "file")
    values = host._materialize_inputs(task, tmp_path / "attempt", file_input_names=file_inputs)
    authority = json.loads(Path(values["filmstrip_authority"]).read_text(encoding="utf-8"))
    assert authority["mode"] == "composed_capture" and authority["capture_snapshot"]["project_slug"] == "p"
    assert json.loads(Path(values["timeline"]).read_text(encoding="utf-8")) == _snapshot()["config"]
    registry = json.loads(Path(values["assets_registry"]).read_text(encoding="utf-8"))
    assert Path(registry["assets"]["frame"]["file"]).read_bytes() == ASSET  # registry media fetched as before
    assert values["materialized_objects"][ASSET_DIGEST[7:]]
    # ...and binds them as the same flags the executor already parses (path or JSON)
    from astrid.core.contracts.binding import expand_command

    record = host_capability(host)
    values.update(out=str(tmp_path / "out"), python_exec="python")
    argv = expand_command(record.definition.command, record.definition.inputs, values, record.definition.metadata).argv
    flag = argv.index("--filmstrip-authority")
    assert argv[flag + 1] == values["filmstrip_authority"] and "--timeline" in argv and "--assets-registry" in argv


def host_capability(host):
    host.discover()
    return host.capabilities["rendering.timeline_visualize"]
