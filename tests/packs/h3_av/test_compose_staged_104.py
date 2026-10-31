import hashlib
import json
import zipfile
from pathlib import Path

from astrid.packs.h3_av.actions.compose import run


def test_separate_av_is_bundled_inside_compose_and_preparation_is_declared(tmp_path, monkeypatch):
    prep = tmp_path / "prep.json"
    compilation = tmp_path / "compilation.json"
    prep.write_text(json.dumps({"request_digest": "request", "assets": [], "mask_schedule": {"digest": "schedule"}}))
    compilation.write_text(json.dumps({"request_digest": "request", "workflow": {}, "managed_assets": {}}))
    video, audio = tmp_path / "raw-video", tmp_path / "raw-audio"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    seen = {}
    def compose_candidate(*, preparation, generated, out_dir, **kwargs):
        with zipfile.ZipFile(generated) as bundle:
            metadata = json.loads(bundle.read("manifest.json"))
            seen["records"] = metadata["outputs"]
            seen["bytes"] = [bundle.read(row["member"]) for row in metadata["outputs"]]
        seen["preparation"] = preparation
        out_dir.mkdir(parents=True, exist_ok=True)
        candidate = out_dir / "made.media"
        candidate.write_bytes(b"\x00\x00\x00\x18ftypisom")
        return {"candidate": {"path": str(candidate)}}
    monkeypatch.setattr(run, "compose_candidate", compose_candidate)
    out = tmp_path / "out"
    def association(role, path):
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        return {"association_id": role + "-association", "run_id": "run", "task_id": "task", "attempt_id": "attempt",
                "object_id": digest, "digest": digest, "size": path.stat().st_size,
                "output_port": "vibecomfy_run", "producer": {"output_port": role, "producer_output_id": role + "-id"}}
    provenance = {"run_id": "run", "task_id": "task", "attempt_id": "attempt",
                  "output_contract": "separate_av_full_timeline",
                  "generated_outputs": {"video": association("video", video), "audio": association("audio", audio)}}
    assert run.main(["--preparation", str(prep), "--compilation", str(compilation), "--generated", str(video),
                     "--generated-audio", str(audio), "--generation-provenance", json.dumps(provenance), "--out", str(out)]) == 0
    assert seen["bytes"] == [b"video", b"audio"]
    assert [row["role"] for row in seen["records"]] == ["video", "audio"]
    assert seen["preparation"]["provenance"]["request_digest"] == "request"
    assert seen["preparation"]["provenance"]["runtime"] == provenance
    assert [row["producer_output_id"] for row in seen["records"]] == ["video-id", "audio-id"]
    manifest = json.loads((out / "manifest.json").read_text())
    assert any(row["name"] == "preparation" and row["path"] == "provenance-preparation.json" for row in manifest["outputs"])
