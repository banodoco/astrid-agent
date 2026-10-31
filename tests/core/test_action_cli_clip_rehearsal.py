"""Run-path rehearsal (attached test engine); install proven on pod.

``astrid action run h3_av.transform --placement local`` on the real clip request, through the real
ephemeral Runtime and pack host, with the engine gate untouched.  The "installed engine" is an install
record plus a fake ComfyUI (``tests/helpers/h3_attached_engine.py``).  This proves the run path from the
canonical command to a verified published composition; it does NOT prove the engine install, which is
proven on a pod.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from astrid.core import action_cli
from tests.helpers import h3_attached_engine as attached

REQUEST = Path(os.environ.get("ASTRID_CLIP_REQUEST", "/home/user/proof/job/request.json"))
ASSET_MAP = Path(os.environ.get("ASTRID_CLIP_ASSET_MAP", "/home/user/proof/job/asset-map.json"))
CLIP_SECONDS = 9
FPS, FRAMES = 24, 216


def _argv(out: Path) -> list[str]:
    return ["run", "h3_av.transform", "--request", str(REQUEST), "--asset-map", str(ASSET_MAP),
            "--placement", "local", "--out", str(out)]


def _probe(video: Path) -> dict:
    done = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
         "stream=nb_read_frames,r_frame_rate", "-of", "json", str(video)],
        capture_output=True, text=True, check=True, timeout=120)
    return json.loads(done.stdout)["streams"][0]


def _last_json_document(text: str) -> dict:
    """The orchestrator child prints its own result document to the shared stdout before the CLI's summary;
    the summary is the last top-level JSON document."""
    decoder, index, last = json.JSONDecoder(), 0, None
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        last, index = decoder.raw_decode(text, index)
    assert isinstance(last, dict), text[-2000:]
    return last


@pytest.fixture
def attached_engine(tmp_path, monkeypatch):
    home, temp = tmp_path / "engine-home", tmp_path / "engine-tmp"
    home.mkdir()
    temp.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(temp))
    monkeypatch.setattr("tempfile.tempdir", None)
    pth = attached.install_pth()
    # The engine returns H3's raw run (the smallest valid run of at least 216 frames); the pipeline trims it to 216.
    from astrid.packs.h3_av.shared.generation import generation_timing

    raw = generation_timing(CLIP_SECONDS)
    engine = attached.AttachedEngine(home, generated_seconds=raw["raw_frames"] / raw["fps"])
    try:
        yield engine
    finally:
        engine.close()
        pth.unlink(missing_ok=True)


@pytest.mark.integration
@pytest.mark.ffmpeg
def test_run_path_rehearsal_attached_test_engine_install_proven_on_pod(attached_engine, tmp_path, capfd):
    # --out is a fresh directory under the clone (kept after the run so the artifacts can be read).
    out_root = Path(__file__).resolve().parents[2] / ".rehearsal-out"
    out_root.mkdir(exist_ok=True)
    out = Path(tempfile.mkdtemp(prefix="clip-", dir=out_root))
    code = action_cli.main(_argv(out))
    captured = capfd.readouterr()
    (tmp_path / "stdout.txt").write_text(captured.out)
    (tmp_path / "stderr.txt").write_text(captured.err)
    assert code == 0, f"exit {code}\nSTDOUT:\n{captured.out}\nSTDERR:\n{captured.err[-4000:]}"
    summary = _last_json_document(captured.out)
    assert summary["ok"] is True and summary["receipt"]

    receipt = json.loads(Path(summary["receipt"]).read_text(encoding="utf-8"))
    # The publication verdict is read from its own field, never from overall_status: the contract says
    # "verified", and the state behind it is a passed effect with its published scope and object.
    publication = receipt["states"]["final_composition_publication"]
    assert receipt["publication_contract"]["final_composition_publication"] == "verified", receipt["publication_contract"]
    assert publication["status"] == "passed", publication
    assert publication["evidence"]["published_scope"] == "final_composition", publication
    assert receipt["placement"] == "local", receipt.get("placement")
    assert receipt["local_attestation"]["kind"] == "local-engine.v1", receipt.get("local_attestation")
    print("RECEIPT_PLACEMENT", json.dumps(receipt["placement"]))

    # The published final composition is the verified candidate; read its frames and rate with ffprobe.
    published = publication["evidence"]["publication"]["object_id"]
    final = out / "07-finalizer" / "retrieved" / "verified-candidate.media"
    assert final.is_file(), sorted(str(p) for p in out.rglob("*.media"))
    assert "sha256:" + hashlib.sha256(final.read_bytes()).hexdigest() == published
    stream = _probe(final)
    assert stream["r_frame_rate"] == f"{FPS}/1" and int(stream["nb_read_frames"]) == FRAMES, stream
    print("FINAL_COMPOSITION", str(final), json.dumps(stream, sort_keys=True))
