from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.actions.verify import verify as verify_module
from astrid.packs.h3_av.actions.verify.verify import VerificationError, verify_candidate
from astrid.packs.h3_av.shared.compose import compose_candidate

from astrid.packs.h3_av.shared.request import normalize_request

pytestmark = pytest.mark.ffmpeg


def _request(*, protected_all: bool = False):
    changes = {"video": [], "audio": []} if protected_all else {
        "video": [{"during": [1, 2], "area": {"full_frame": True}, "action": "generate"}],
        "audio": [{"during": [1, 2], "action": "generate"}],
    }
    return normalize_request(
        {
            "version": 1,
            "operation": "edit",
            "source": {"asset": "source", "range": [0, 2 if protected_all else 1]},
            "output": {"duration": 2},
            "content": {"prompt": "preserve the protected source"},
            "changes": changes,
            "references": [],
            "overrides": {},
        }
    )


def _av(path: Path, *, color: str, freq: int, seconds: int) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=32x32:r=24",
         "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000", "-t", str(seconds),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
    )


@pytest.fixture()
def partial(tmp_path: Path):
    source = tmp_path / "source.mp4"
    generated = tmp_path / "generated.mp4"
    _av(source, color="blue", freq=440, seconds=1)
    _av(generated, color="red", freq=880, seconds=2)
    preparation = prepare_request(_request(), asset_map={"source": str(source)})
    composition = compose_candidate(
        preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "composition"
    )
    # Sanity: untampered evidence verifies.
    assert verify_candidate(preparation=preparation, composition=composition, source=source)["status"] == "verified"
    return preparation, composition, source


def _fail_probe(monkeypatch, mode: str, only: Path | None = None) -> None:
    real_run = subprocess.run

    def run(cmd, *args, **kwargs):
        if only is not None and str(only) not in [str(part) for part in cmd]:
            return real_run(cmd, *args, **kwargs)
        if mode == "exit":
            raise subprocess.CalledProcessError(1, cmd)
        if mode == "missing":
            raise FileNotFoundError("ffprobe")
        out = {
            "empty": "", "garbled": "{not json", "no_streams": '{"streams": []}',
            "no_media": '{"format": {}}', "bad_streams": '{"streams": [null]}',
            "empty_stream": '{"streams": [{}]}',
            "unknown_stream": '{"streams": [{"codec_type": "data", "codec_name": "bin_data"}]}',
            "missing_codec": '{"streams": [{"codec_type": "video"}]}',
        }[mode]
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(verify_module.subprocess, "run", run)


@pytest.mark.parametrize("mode", [
    "exit", "missing", "empty", "garbled", "no_streams", "no_media", "bad_streams",
    "empty_stream", "unknown_stream", "missing_codec",
])
def test_candidate_route_fails_closed_when_ffprobe_fails(partial, monkeypatch, mode: str) -> None:
    preparation, composition, source = partial
    _fail_probe(monkeypatch, mode)
    with pytest.raises(VerificationError, match="ffprobe"):
        verify_candidate(preparation=preparation, composition=composition, source=source)


def test_ffprobe_not_installed_fails_closed(partial, monkeypatch) -> None:
    preparation, composition, source = partial
    monkeypatch.setattr(verify_module.shutil, "which", lambda name: None)
    with pytest.raises(VerificationError, match="ffprobe"):
        verify_candidate(preparation=preparation, composition=composition, source=source)


def test_partial_preservation_route_fails_closed_when_source_probe_fails(partial, monkeypatch) -> None:
    preparation, composition, source = partial
    # Candidate probe succeeds, only the source probe breaks.
    _fail_probe(monkeypatch, "exit", only=source.resolve())
    with pytest.raises(VerificationError, match="ffprobe"):
        verify_candidate(preparation=preparation, composition=composition, source=source)


def test_wholly_protected_route_fails_closed_when_ffprobe_fails(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.mp4"
    _av(source, color="blue", freq=440, seconds=2)
    preparation = prepare_request(_request(protected_all=True), asset_map={"source": str(source)})
    composition = compose_candidate(
        preparation=preparation, generated=source, source=source, out_dir=tmp_path / "composition"
    )
    _fail_probe(monkeypatch, "exit")
    with pytest.raises(VerificationError, match="ffprobe"):
        verify_candidate(preparation=preparation, composition=composition, source=source)


@pytest.mark.parametrize("stream", ["video", "audio"])
def test_self_declared_witness_digest_is_rejected(partial, stream: str) -> None:
    preparation, composition, source = partial
    tampered = copy.deepcopy(composition)
    entry = tampered["preservation_evidence"]["protected_samples"][stream][0]
    entry["method"] = "custom-self-attested-v1"
    entry["witness_digest"] = "deadbeef"
    with pytest.raises(VerificationError, match="cannot be recomputed"):
        verify_candidate(preparation=preparation, composition=tampered, source=source)


def test_tampered_recomputable_witness_digest_is_rejected(partial) -> None:
    preparation, composition, source = partial
    tampered = copy.deepcopy(composition)
    entry = tampered["preservation_evidence"]["protected_samples"]["video"][0]
    entry["source_sha256"] = entry["candidate_sha256"] = "0" * 64
    entry["witness_digest"] = "0" * 64
    with pytest.raises(VerificationError, match="does not match candidate"):
        verify_candidate(preparation=preparation, composition=tampered, source=source)


@pytest.mark.parametrize("field", ["source_sha256", "candidate_sha256", "sample_count", "source_sample_count", "candidate_sample_count", "similarity"])
def test_forged_audio_witness_is_rejected(partial, field: str) -> None:
    preparation, composition, source = partial
    tampered = copy.deepcopy(composition)
    entry = tampered["preservation_evidence"]["protected_samples"]["audio"][0]
    if field.endswith("sha256"):
        entry[field] = "0" * 64
    elif field == "similarity":
        entry[field] = float(entry[field]) / 2
    else:
        entry[field] += 1
    with pytest.raises(VerificationError, match="witness does not match candidate"):
        verify_candidate(preparation=preparation, composition=tampered, source=source)


def test_forged_decoded_video_sample_count_is_rejected(partial) -> None:
    preparation, composition, source = partial
    tampered = copy.deepcopy(composition)
    tampered["preservation_evidence"]["protected_samples"]["video"][0]["sample_count"] += 1
    with pytest.raises(VerificationError, match="sample digest does not match candidate"):
        verify_candidate(preparation=preparation, composition=tampered, source=source)


@pytest.mark.parametrize("stream", ["video", "audio"])
def test_changed_mask_schedule_cannot_reuse_old_digest(partial, stream: str) -> None:
    preparation, composition, source = partial
    tampered = copy.deepcopy(preparation)
    tampered["mask_schedule"][stream]["protected_intervals"] = []
    tampered["mask_schedule"][stream]["generated_intervals"] = [[0, 2]]
    with pytest.raises(VerificationError, match="mask schedule digest"):
        verify_candidate(preparation=tampered, composition=composition, source=source)


def test_changed_nested_schedule_provenance_is_rejected(partial) -> None:
    preparation, composition, source = partial
    tampered = copy.deepcopy(composition)
    tampered["provenance"] = {
        "request_digest": preparation["request_digest"],
        "schedule_digest": "0" * 64,
    }
    with pytest.raises(VerificationError, match="mask schedule provenance"):
        verify_candidate(preparation=preparation, composition=tampered, source=source)


@pytest.mark.parametrize("field", ["sha256", "size", "coverage"])
def test_changed_candidate_evidence_is_rejected(partial, field: str) -> None:
    preparation, composition, source = partial
    tampered = copy.deepcopy(composition)
    if field == "coverage":
        tampered["coverage"]["candidate"]["video"]["frames"] += 1
    elif field == "size":
        tampered["candidate"][field] += 1
    else:
        tampered["candidate"][field] = "0" * 64
    with pytest.raises(VerificationError, match="candidate .*does not match"):
        verify_candidate(preparation=preparation, composition=tampered, source=source)


def test_changed_candidate_bytes_are_rejected(partial) -> None:
    preparation, composition, source = partial
    Path(composition["candidate"]["path"]).write_bytes(b"changed candidate")
    with pytest.raises(VerificationError, match="candidate digest"):
        verify_candidate(preparation=preparation, composition=composition, source=source)


@pytest.fixture()
def regional(tmp_path: Path):
    from PIL import Image

    source = tmp_path / "source.mp4"
    generated = tmp_path / "generated.mp4"
    mask = tmp_path / "region.png"
    _av(source, color="blue", freq=440, seconds=2)
    _av(generated, color="red", freq=880, seconds=2)
    image = Image.new("L", (32, 32), 0)
    image.paste(255, (0, 0, 16, 32))
    image.save(mask)
    raw = copy.deepcopy(_request().value)
    raw["source"]["range"] = [0, 2]
    raw["changes"] = {
        "video": [{"during": [0, 2], "area": {"rectangle": [0, 0, 32, 32]},
                   "mask_asset": "region.png", "action": "generate"}],
        "audio": [],
    }
    preparation = prepare_request(normalize_request(raw), asset_map={"source": str(source), "region.png": str(mask)})
    composition = compose_candidate(
        preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "composition"
    )
    assert verify_candidate(preparation=preparation, composition=composition, source=source)["status"] == "verified"
    return preparation, composition, source, mask


@pytest.mark.parametrize("field", ["during", "mask_asset", "mask_sha256", "mask_kind", "mask_frames", "video_segments"])
@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_missing_or_changed_mask_segment_provenance_is_rejected(regional, field: str, mutation: str) -> None:
    preparation, composition, source, _mask = regional
    tampered = copy.deepcopy(composition)
    if field == "video_segments":
        tampered["composition"][field] = [] if mutation == "missing" else [
            *tampered["composition"][field], *copy.deepcopy(tampered["composition"][field])
        ]
    elif mutation == "changed":
        segment = tampered["composition"]["video_segments"][0]
        segment[field] = {
            "during": [0, 1], "mask_asset": "another-mask.png", "mask_sha256": "0" * 64,
            "mask_kind": "video", "mask_frames": 2,
        }[field]
    else:
        del tampered["composition"]["video_segments"][0][field]
    with pytest.raises(VerificationError, match="mask provenance"):
        verify_candidate(preparation=preparation, composition=tampered, source=source)


def test_changed_mask_bytes_cannot_be_attested_by_changed_segment(regional) -> None:
    import hashlib
    from PIL import Image

    preparation, composition, source, mask = regional
    # Making the mask all white would otherwise vacuously protect no pixels.
    Image.new("L", (32, 32), 255).save(mask)
    composition["composition"]["video_segments"][0]["mask_sha256"] = hashlib.sha256(mask.read_bytes()).hexdigest()
    with pytest.raises(VerificationError, match="mask provenance.*preparation asset"):
        verify_candidate(preparation=preparation, composition=composition, source=source)


def test_mask_size_must_match_preparation_asset_evidence(regional) -> None:
    preparation, composition, source, mask = regional
    tampered = copy.deepcopy(preparation)
    record = next(row for row in tampered["assets"] if row["asset"] == mask.name)
    record["size"] += 1
    with pytest.raises(VerificationError, match="mask provenance.*preparation asset"):
        verify_candidate(preparation=tampered, composition=composition, source=source)
