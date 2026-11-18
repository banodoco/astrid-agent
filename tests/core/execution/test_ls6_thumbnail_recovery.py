"""LS-6 recovery through authenticated HTTP and ordinary installed actions.

The fixture generator supplies CPU video bytes. Only the external ffmpeg
decoder is substituted: source capture, registration, claims, staging, native
child custody, result harvesting, settlement and SDK following are real.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrid.core.execution.generic_host import GenericPackHost, HostError, RuntimeProtocolClient
from astrid.pack_api import VERSION as PACK_API_VERSION
from astrid.sdk import invocation
from astrid.sdk.remote import RemoteTasks
from astrid.sdk.workspace_client import WorkspaceClient as FacadeClient
from astrid_runtime_client import WorkspaceClient


pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
MAIN_CAPABILITY = "ls6_fixture.generate"
THUMBNAIL_CAPABILITY = "generation.thumbnail_backfill"
INPUT_BYTES = b"LS-6 admitted CPU source"
VIDEO_BYTES = b"generated:" + INPUT_BYTES


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _write_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    control = tmp_path / "decoder-control"
    control.mkdir()
    trace = control / "decoder.jsonl"
    generated = control / "generation.jsonl"
    entered = control / "entered"
    release = control / "release"
    mode = control / "mode"
    mode.write_text("hold", encoding="utf-8")
    image = io.BytesIO()
    Image.new("RGB", (12, 8), (31, 73, 127)).save(image, format="JPEG")
    jpeg = image.getvalue()

    # This is the external decoder boundary, not a host/action replacement.
    # Its control files live outside the captured pack source tree.
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    ffmpeg = binary_dir / "ffmpeg"
    ffmpeg.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, json, sys, time\nfrom pathlib import Path\n"
        "if '-version' in sys.argv:\n    print('ffmpeg ls6 CPU fixture'); raise SystemExit(0)\n"
        "source = Path(sys.argv[sys.argv.index('-i') + 1])\n"
        f"assert source.read_bytes() == {VIDEO_BYTES!r}\n"
        f"with Path({str(trace)!r}).open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'source_digest': 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest(),"
        " 'source': str(source), 'destination': sys.argv[-1]}) + '\\n')\n"
        f"Path({str(entered)!r}).touch()\n"
        f"mode = Path({str(mode)!r}).read_text(encoding='utf-8')\n"
        "if mode == 'hold':\n"
        "    deadline = time.monotonic() + 9\n"
        f"    while not Path({str(release)!r}).exists():\n"
        "        if time.monotonic() >= deadline: raise SystemExit('fixture release was not observed')\n"
        "        time.sleep(0.01)\n"
        "if mode == 'fail':\n"
        "    print('LS6 fixture decoder refused', file=sys.stderr); raise SystemExit(23)\n"
        f"Path(sys.argv[-1]).write_bytes(bytes.fromhex({jpeg.hex()!r}))\n",
        encoding="utf-8",
    )
    ffmpeg.chmod(0o755)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)

    pack = tmp_path / "ls6_fixture"
    pack.mkdir()
    script = pack / "generate.py"
    script.write_text(
        "import argparse\nfrom datetime import datetime, timezone\nfrom pathlib import Path\n"
        "from astrid.pack_api import build_manifest, write_manifest\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--source', type=Path, required=True)\n"
        "parser.add_argument('--out', type=Path, required=True)\n"
        "args = parser.parse_args()\nargs.out.mkdir(parents=True, exist_ok=True)\n"
        "(args.out / 'video.mp4').write_bytes(b'generated:' + args.source.read_bytes())\n"
        f"with Path({str(generated)!r}).open('a', encoding='utf-8') as stream: stream.write('generated\\n')\n"
        "write_manifest(args.out / 'manifest.json', build_manifest(kind='ls6-fixture', inputs={'source': str(args.source)},"
        " outputs=[{'name': 'video', 'path': 'video.mp4', 'ordinal': 0, 'role': 'result',"
        " 'group_key': 'main', 'variant_key': 'original', 'output_port': 'video',"
        " 'is_primary': True, 'media_type': 'video/mp4'}], created=datetime.now(timezone.utc).isoformat()))\n",
        encoding="utf-8",
    )
    (pack / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": "ls6_fixture", "name": "LS6 CPU generation fixture", "version": "1.0.0",
        "pack_api": "%d.%d" % PACK_API_VERSION, "status": "experimental", "domain": "integration",
        "stability": "experimental", "support": "community", "visibility": "visible",
        "description": "CPU bytes for durable thumbnail recovery conformance.",
        "permissions": [{"id": "subprocess", "reason": "Runs the CPU fixture."},
                        {"id": "project_files", "reason": "Reads staged input and writes declared video."}],
        "actions": {"generate": {
            "name": "Generate CPU video", "description": "Copy one admitted source into a fixture video.",
            "version": "1.0", "cache": {"mode": "none"},
            "inputs": [{"name": "source", "type": "file", "required": True}],
            "outputs": [{"name": "video", "type": "file", "mode": "create_or_replace",
                         "path_template": "{out}/video.mp4", "artifact_type": "video/mp4"}],
            "isolation": {"mode": "subprocess", "network": False},
            "metadata": {"adapter_family": "cpu", "output_result_manifest": True, "runtime_file": "generate.py"},
            "invocation": {"kind": "command", "command": {"argv": [
                "{python_exec}", str(script), "--source", "{source}", "--out", "{out}"]}},
            "resources": [{"kind": "implementation", "path": "generate.py"}],
        }},
    }), encoding="utf-8")
    shipped = json.loads((ROOT / "config/astrid-beta-capabilities.json").read_text(encoding="utf-8"))
    thumbnail = next(row for row in shipped["capabilities"] if row["id"] == THUMBNAIL_CAPABILITY)
    matrix = tmp_path / "capability-matrix.json"
    matrix.write_text(json.dumps({"schema_version": 1, "capabilities": [thumbnail, {
        "id": MAIN_CAPABILITY, "disposition": "required", "evidence_reason": "LS6 CPU integration fixture",
        "adapter_family": "cpu", "resource_keys": [], "required_env": [], "required_binaries": [],
        "required_packages": [], "estimated_scratch_bytes": 2 * 1024 * 1024,
        "estimated_output_bytes": 512 * 1024,
    }]}), encoding="utf-8")
    return SimpleNamespace(pack=pack, matrix=matrix, trace=trace, generated=generated,
                           entered=entered, release=release, mode=mode, jpeg=jpeg, root=tmp_path)


def _host(case: SimpleNamespace, daemon) -> GenericPackHost:
    host = GenericPackHost(
        pack_roots=[case.pack, ROOT / "astrid/packs/generation"], capability_matrix=case.matrix,
        client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
        executor_id="astrid-pack-host", attempt_base=case.root / "attempts",
    )
    registration = host.register()["registration"]
    assert host.client.supports_thumbnail_intents()
    for capability_id in (MAIN_CAPABILITY, THUMBNAIL_CAPABILITY):
        record = host.capabilities[capability_id]
        assert record.ready, record.preflight
        published = next(row for row in registration.capabilities if row.capability_id == capability_id)
        assert published.definition_digest == record.capability_digest
    return host


def _admit_main(api: WorkspaceClient, host: GenericPackHost):
    project = api.create_project("LS6 thumbnail recovery", slug="ls6-thumbnails", idempotency_key="ls6-project")
    source = api.ingest_project_object(project.project_id, INPUT_BYTES, media_type="application/octet-stream",
                                       idempotency_key="ls6-input", filename="input.bin")
    assert source["digest"] == _digest(INPUT_BYTES)
    task = api.admit_task(
        capability_id=MAIN_CAPABILITY, capability_digest=host.capabilities[MAIN_CAPABILITY].capability_digest,
        input_object_ids=[source["digest"]], project_id=project.project_id, idempotency_key="ls6-main",
        spec={"inputs": {"source": {"object_id": source["digest"], "digest": source["digest"],
            "sha256": source["digest"].removeprefix("sha256:"), "filename": "input.bin",
            "size": len(INPUT_BYTES), "media_type": "application/octet-stream"}}},
        generation_intent={"version": 1, "modality": "video", "partial_success_policy": "reject",
            "groups": [{"group_key": "main", "selectors": [
                {"selector": "video", "ordinal": 0, "variant_key": "original"}]}]},
        settlement_effect={"effect_type": "generation.publish_v1", "target_id": project.project_id,
            "payload": {"version": 1, "modality": "video", "partial_success_policy": "reject",
                "generation_type": MAIN_CAPABILITY, "metadata": {"shot_id": "ls6"},
                "groups": [{"group_key": "main", "selectors": [
                    {"selector": "video", "ordinal": 0, "variant_key": "original", "output_port": "video"}]}]}},
        storage_estimate={"scratch_bytes": 2 * 1024 * 1024, "output_bytes": 512 * 1024},
    )
    return project, task


def _caller_reply(daemon, task):
    facade = FacadeClient(daemon.endpoint, daemon.token)
    result, ok, attempt = invocation._wait_for_kernel_task_loop(
        SimpleNamespace(tasks=RemoteTasks(facade)), task_id=task.task_id, run_id=task.run_id,
        timeout_seconds=8, poll_seconds=1, read_managed_outputs=True, expects_outputs=True,
    )
    assert ok and attempt and result["ok"], result
    assert any(row["object_id"] == _digest(VIDEO_BYTES) for row in result["managed_outputs"])
    return result


def _tasks(api: WorkspaceClient, project_id: str):
    tasks, cursor = api.list_project_tasks(project_id)
    assert cursor is None
    return tasks


def _one_generation(api: WorkspaceClient, project_id: str):
    generations, cursor = api.list_generations(project_id)
    assert cursor is None and len(generations) == 1
    return generations[0]


def _assert_preview(api: WorkspaceClient, case, project_id, main, thumbnail):
    generation = _one_generation(api, project_id)
    assert generation.metadata["thumbnail"] == {
        "object_id": _digest(case.jpeg), "source_object_id": _digest(VIDEO_BYTES), "recipe_version": 1,
    }
    assert api.get_object(_digest(case.jpeg)).data == case.jpeg
    assert api.get_object(_digest(VIDEO_BYTES)).data == VIDEO_BYTES
    original = api.get_task(main.task_id)
    assert original.state == "succeeded" and original.attempt_id == main.attempt_id
    assert _lines(case.generated) == ["generated"]
    outputs, cursor = api.list_managed_outputs(thumbnail.task_id)
    assert cursor is None
    previews = [row for row in outputs if row.role == "thumbnail"]
    assert len(previews) == 1
    assert previews[0].object_id == _digest(case.jpeg)
    assert previews[0].provenance["thumbnail"] == {"source_object_ids": [_digest(VIDEO_BYTES)], "recipe_version": 1}
    assert len(_tasks(api, project_id)) == 2


def test_main_reply_precedes_thumbnail_and_restart_recovers_one_preview(tmp_path, monkeypatch, real_runtime):
    case = _write_fixture(tmp_path, monkeypatch)
    daemon = real_runtime()
    api = WorkspaceClient(daemon.endpoint, daemon.token)
    host = _host(case, daemon)
    try:
        project, admitted = _admit_main(api, host)
        assert [row.state for row in host.run(once=True)] == ["succeeded"]
        _caller_reply(daemon, admitted)
        main = api.get_task(admitted.task_id)
        intent, = main.result["thumbnail_intents"]
        assert intent["task_id"] == main.task_id and intent["attempt_id"] == main.attempt_id
        assert intent["source_object_id"] == _digest(VIDEO_BYTES)
        assert intent["project_id"] == project.project_id
        assert _lines(case.trace) == [] and not case.release.exists()
        assert len(_tasks(api, project.project_id)) == 1
        assert "thumbnail" not in _one_generation(api, project.project_id).metadata
        assert host.client.pending_thumbnail_intents(executor_id=host.executor_id)["items"] == [intent]

        # Stop in the durable gap: the main effect has committed, while the
        # ordinary thumbnail task has not yet been admitted.
        epoch = main.runtime_epoch
        host.shutdown()
        daemon.stop()
        daemon = real_runtime()
        api = WorkspaceClient(daemon.endpoint, daemon.token)
        host = _host(case, daemon)
        recovered = api.get_task(main.task_id)
        assert recovered.result["thumbnail_intents"] == [intent]
        assert host.client._current_runtime_epoch() > epoch
        assert len(_tasks(api, project.project_id)) == 1

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(host.run, once=True)
            try:
                deadline = time.monotonic() + 30
                while not case.entered.exists():
                    if pending.done():
                        pytest.fail(f"thumbnail host ended before extraction: {pending.result()!r}")
                    assert time.monotonic() < deadline, "ordinary thumbnail action did not reach the decoder"
                    time.sleep(0.01)
                tasks = _tasks(api, project.project_id)
                thumbnail, = [row for row in tasks if row.capability_id == THUMBNAIL_CAPABILITY]
                assert len(tasks) == 2 and thumbnail.state == "running"
                assert thumbnail.input_object_ids == [_digest(VIDEO_BYTES)]
                assert thumbnail.attempt_id and thumbnail.lease_fence
                assert not pending.done() and not case.release.exists()
                assert "thumbnail" not in _one_generation(api, project.project_id).metadata
                _caller_reply(daemon, admitted)
                assert api.get_object(_digest(VIDEO_BYTES)).data == VIDEO_BYTES
            finally:
                case.release.touch()
            assert [row.state for row in pending.result(timeout=30)] == ["succeeded"]

        thumbnail = api.get_task(thumbnail.task_id)
        assert thumbnail.state == "succeeded"
        _assert_preview(api, case, project.project_id, main, thumbnail)
        assert len(_lines(case.trace)) == 1
        assert json.loads(_lines(case.trace)[0])["source_digest"] == _digest(VIDEO_BYTES)
        assert host.run(once=True) == []
        assert host.client.pending_thumbnail_intents(executor_id=host.executor_id)["items"] == []
        _assert_preview(api, case, project.project_id, main, thumbnail)
        assert len(_lines(case.trace)) == 1
    finally:
        case.release.touch()
        host.shutdown()
        daemon.stop()


def test_failed_thumbnail_is_visible_and_retry_does_not_repeat_generation(tmp_path, monkeypatch, real_runtime):
    case = _write_fixture(tmp_path, monkeypatch)
    case.mode.write_text("fail", encoding="utf-8")
    daemon = real_runtime()
    api = WorkspaceClient(daemon.endpoint, daemon.token)
    host = _host(case, daemon)
    try:
        project, admitted = _admit_main(api, host)
        assert [row.state for row in host.run(once=True)] == ["succeeded"]
        _caller_reply(daemon, admitted)
        main = api.get_task(admitted.task_id)
        assert _lines(case.trace) == []
        with pytest.raises(HostError, match="exited"):
            host.run(once=True)
        thumbnail, = [row for row in _tasks(api, project.project_id) if row.capability_id == THUMBNAIL_CAPABILITY]
        assert thumbnail.state == "failed" and thumbnail.attempt_id and thumbnail.lease_fence
        assert "LS6 fixture decoder refused" in json.dumps(thumbnail.result)
        assert "thumbnail" not in _one_generation(api, project.project_id).metadata
        assert _lines(case.generated) == ["generated"] and len(_lines(case.trace)) == 1
        assert api.get_task(main.task_id).attempt_id == main.attempt_id
        assert host.client.pending_thumbnail_intents(executor_id=host.executor_id)["items"] == []
        assert host.run(once=True) == []
        assert len(_tasks(api, project.project_id)) == 2 and len(_lines(case.trace)) == 1

        # Retry is the existing owner-authorized ordinary task transition.
        # The worker receives neither task-write authority nor a new intent.
        case.mode.write_text("success", encoding="utf-8")
        retry = api.retry_task(thumbnail.task_id, expected_version=thumbnail.version, idempotency_key="ls6-preview-retry")
        assert retry.task_id == thumbnail.task_id
        assert [row.state for row in host.run(once=True)] == ["succeeded"]
        completed = api.get_task(thumbnail.task_id)
        assert completed.state == "succeeded" and completed.attempt_id != thumbnail.attempt_id
        assert completed.lease_fence > thumbnail.lease_fence
        _assert_preview(api, case, project.project_id, main, completed)
        assert len(_lines(case.trace)) == 2
        assert host.run(once=True) == []
        assert len(_lines(case.trace)) == 2
    finally:
        case.release.touch()
        host.shutdown()
        daemon.stop()
