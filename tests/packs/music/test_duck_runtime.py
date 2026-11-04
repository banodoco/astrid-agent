"""Managed duck invocation against a disposable Runtime and project.

Run outside the sandbox (the host requires native process custody):

    source /Users/hannahomalley/Developer/astrid-video/astrid-local/live-door/env.sh
    ASTRID_RUNTIME_CHECKOUT=/Users/hannahomalley/Developer/astrid-video/astrid-local/live-door/Runtime PYTHONPATH="$PWD:/private/tmp/music-duck-test-deps" python -m pytest tests/packs/music/test_duck_runtime.py -m integration -q
"""
from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.packs.music.actions.duck._audio import read_duck_tag
from astrid.packs.music.actions.duck.api import duck_timeline
from astrid.sdk import AstridClient, observe_task_invocation
from astrid.sdk.timeline import Checkout

pytestmark = pytest.mark.integration
PACK = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "music"


def _write_audio(path: Path, *, voice: bool = False) -> Path:
    rate = 48_000
    t = np.arange(rate * 8, dtype=np.float64) / rate
    signal = .055 * np.sin(2 * np.pi * 220 * t) + .02 * np.sin(2 * np.pi * 331 * t)
    if voice:
        signal[:] = 0
        burst = np.arange(round(.24 * rate), dtype=np.float64) / rate
        envelope = np.sin(np.linspace(0, np.pi, len(burst))) ** 1.4
        syllable = .32 * envelope * np.sin(2 * np.pi * 185 * burst)
        for start in (1., 2., 3., 4., 5., 6.):
            begin = round(start * rate)
            signal[begin:begin + len(syllable)] += syllable
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.rint(signal * 32767).astype("<i2").tobytes())
    return path


@pytest.fixture
def duck_runtime(tmp_path, real_runtime, monkeypatch):
    """The real-runtime fixture owns daemon teardown, including failed host setup."""
    daemon = real_runtime(authoring_profile="astrid")
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "support"))
    host = GenericPackHost(
        pack_roots=[PACK], client=RuntimeProtocolClient(daemon.endpoint, daemon.worker_token),
        executor_id="astrid-pack-host", attempt_base=tmp_path / "attempts",
    )
    try:
        host.discover()
        host.register(deliberate=True)
        with AstridClient.open(
            endpoint=daemon.endpoint, credential=daemon.credential_path,
            realm_id=daemon.service.realm["id"], actor_id="owner",
            client_name="music-duck-managed-test", client_version="stage1",
            protocol_version="workspace.v1",
        ) as client:
            yield client, host
    finally:
        host.shutdown()


def _invoke(client, host, project):
    # Admit through the real helper first; a caller-local snapshot would fail
    # here. Drive the registered worker explicitly rather than timing a thread.
    pending = duck_timeline(client, project, "duck-main", wait=False).raise_for_error()
    assert pending.kernel_task_id and pending.run_id
    assert len(host.run(once=True)) == 1
    return observe_task_invocation(
        client, capability_id=pending.capability_id,
        capability_type=pending.capability_type, native_kind=pending.native_kind,
        task_id=pending.kernel_task_id, run_id=pending.run_id,
        read_managed_outputs=True,
    ).raise_for_error()


def test_managed_duck_outputs_and_tagged_rerun_leave_the_input_revision_unchanged(duck_runtime, tmp_path):
    client, host = duck_runtime
    created_project = client.projects.create(
        slug="duck-managed-test", name="Disposable Duck Managed Test", idempotency_key="duck-project",
    )
    assert created_project.ok, created_project.error
    project = created_project.data["project_id"]

    def ingest(name, voice):
        imported = client.media.import_file(
            project=project, path=_write_audio(tmp_path / name, voice=voice),
            idempotency_key="duck-" + name,
        )
        assert imported.ok, imported.error
        return imported.data["object_id"]

    bed_id, voice_id = ingest("bed.wav", False), ingest("voice.wav", True)
    created = client.timelines.create_empty(
        project=project, timeline_id="duck-main", config={"tracks": []}, idempotency_key="duck-timeline",
    )
    assert created.ok, created.error
    tl = Checkout.open(project, "duck-main", client=client)
    parent = tl.bundle["parent"]
    parent["config"]["tracks"] = [{"id": "vo", "kind": "audio"}, {"id": "music", "kind": "audio"}]
    parent["registry"] = {"assets": {
        "bed": {"media_id": bed_id, "type": "audio/wav"},
        "voice": {"media_id": voice_id, "type": "audio/wav"},
    }}
    parent["clips"] = parent["config"]["clips"] = [
        {"id": "voice-1", "clipType": "media", "track": "vo", "asset": "voice", "at": 0, "from": 0, "to": 8},
        {"id": "music-1", "clipType": "media", "track": "music", "asset": "bed", "at": 0, "from": 0, "to": 8},
    ]
    tl.publish("seed disposable duck timeline", client=client, idempotency_key="duck-publish")

    def run_and_verify():
        before = client.timelines.open_bundle(project, "duck-main")
        assert before.ok, before.error
        result = _invoke(client, host, project)
        outputs = {port: result.output(port) for port in ("music", "duck", "manifest")}
        assert {row["filename"] for row in outputs.values()} == {"music.wav", "duck.json", "manifest.json"}
        wav = client.media.read_bytes(outputs["music"]["digest"])
        path = tmp_path / "managed-output.wav"
        path.write_bytes(wav)
        tag = read_duck_tag(path)
        assert tag and tag["source_digest"] == bed_id
        report = json.loads(client.media.read_bytes(outputs["duck"]["digest"]))
        assert report["source_digest"] == bed_id
        assert report["parameters"] == {"bed_lu": 8}
        manifest = json.loads(client.media.read_bytes(outputs["manifest"]["digest"]))
        assert manifest["kind"] == "music.duck"
        assert {row["name"]: row["content_hash"] for row in manifest["outputs"]} == {
            port: outputs[port]["digest"] for port in ("music", "duck")
        }
        after = client.timelines.open_bundle(project, "duck-main")
        assert after.ok, after.error
        assert after.data["revision_id"] == before.data["revision_id"]
        assert after.data["bundle"] == before.data["bundle"]
        return outputs["music"]["digest"], wav

    music_digest, first_wav = run_and_verify()
    # Publish only the managed asset substitution, preserving all clip timing
    # and mix fields, then invoke the same path on that tagged source.
    tl = Checkout.open(project, "duck-main", client=client)
    tl.bundle["parent"]["registry"]["assets"]["ducked"] = {"media_id": music_digest, "type": "audio/wav"}
    tl.clip("music-1").data["asset"] = "ducked"
    tl.publish("adopt tagged duck output", client=client, idempotency_key="duck-adopt")
    rerun_digest, rerun_wav = run_and_verify()
    assert rerun_wav == first_wav
    assert rerun_digest == music_digest
