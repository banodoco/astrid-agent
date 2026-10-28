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
        out = "" if mode == "empty" else "{not json"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(verify_module.subprocess, "run", run)


@pytest.mark.parametrize("mode", ["exit", "missing", "empty", "garbled"])
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
