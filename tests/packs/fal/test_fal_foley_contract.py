from __future__ import annotations

import json
import hashlib
import runpy
import sys
from pathlib import Path

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.core.pack.canonical import validate_canonical_pack

PACK_ROOT = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "fal"


class _FakeClient:
    def register_secret(self, _value: str) -> None:
        return None

    def get_bytes(self, _url: str, *, timeout: int, max_bytes: int) -> bytes:
        assert timeout == 300
        assert max_bytes == 512 * 1024 * 1024
        return b"audio"


def test_foley_missing_audio_url_is_a_coded_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    from astrid.packs.fal.actions.fal_foley import run

    with pytest.raises(AstridError) as raised:
        run._save_audio(_FakeClient(), {"request_id": "request-1"}, tmp_path / "audio.wav")

    envelope = raised.value.to_envelope()
    assert envelope["code"] == "FAL_FOLEY_MISSING_AUDIO_URL"
    assert envelope["message"] == "fal foley result had no audio url: {'request_id': 'request-1'}"
    assert envelope["field"] == "audio.url"
    assert "Retry" in envelope["fix"]


def test_foley_command_renders_missing_audio_url_code_and_fix(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from astrid.core.util import generation_helpers, http

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    monkeypatch.setattr(generation_helpers, "load_provider_key", lambda *args, **kwargs: "test-key")
    monkeypatch.setattr(http, "default_client", lambda: _FakeClient())
    monkeypatch.setattr(http, "fal_submit_and_poll", lambda *args, **kwargs: {"request_id": "request-1"})
    monkeypatch.setattr(
        sys, "argv", ["run", "--clip", str(clip), "--prompt", "rain", "--out", str(tmp_path / "audio.wav")]
    )
    monkeypatch.delitem(sys.modules, "astrid.packs.fal.actions.fal_foley.run", raising=False)

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("astrid.packs.fal.actions.fal_foley.run", run_name="__main__")

    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "FAL_FOLEY_MISSING_AUDIO_URL" in stderr
    assert "Retry the Foley request" in stderr


def test_foley_command_preserves_argparse_help_exit(monkeypatch, capsys) -> None:
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    monkeypatch.setattr(sys, "argv", ["run", "--help"])
    monkeypatch.delitem(sys.modules, "astrid.packs.fal.actions.fal_foley.run", raising=False)

    with pytest.raises(SystemExit) as raised:
        runpy.run_module("astrid.packs.fal.actions.fal_foley.run", run_name="__main__")

    assert raised.value.code == 0
    assert "--clip" in capsys.readouterr().out


def test_foley_host_contract_declares_inputs_outputs_and_typed_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    # The entrypoint guard refuses import unless the host marks the invocation internal.
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
    from astrid.packs.fal.actions.fal_foley import run

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    out = tmp_path / "run"
    client = _FakeClient()

    monkeypatch.setattr(run, "load_provider_key", lambda *args, **kwargs: "test-key")
    monkeypatch.setattr(run, "default_client", lambda: client)
    monkeypatch.setattr(
        run,
        "fal_submit_and_poll",
        lambda *args, **kwargs: {
            "request_id": "request-1",
            "audio": {
                "url": "https://example.test/audio.wav",
                "content_type": "audio/wav",
                "file_name": "provider.wav",
            },
        },
    )

    assert run.main(
        [
            "--clip",
            str(clip),
            "--prompt",
            "footsteps on gravel",
            "--out",
            str(out / "audio.wav"),
        ]
    ) == 0

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    sidecar_bytes = (out / "audio.wav.fal.json").read_bytes()
    assert manifest["outputs"] == [
        {
            "name": "audio",
            "path": "audio.wav",
            "type": "file",
            "artifact_type": "audio",
            "media_type": "audio/wav",
            "ordinal": 0,
            "role": "result",
            "is_primary": True,
            "bytes": 5,
            "content_hash": "sha256:"
            + hashlib.sha256(b"audio").hexdigest(),
        },
        {
            "name": "audio_provenance",
            "path": "audio.wav.fal.json",
            "type": "file",
            "artifact_type": "application/json",
            "media_type": "application/json",
            "ordinal": 1,
            "role": "auxiliary",
            "is_primary": False,
            "bytes": len(sidecar_bytes),
            "content_hash": "sha256:" + hashlib.sha256(sidecar_bytes).hexdigest(),
        },
    ]
    assert manifest["provider_extension"] == {
        "model_id": "fal-ai/hunyuan-video-foley",
        "request_id": "request-1",
        "source_url": "https://example.test/audio.wav",
        "source_file_name": "provider.wav",
    }

    declared = validate_canonical_pack(PACK_ROOT).definition.actions["fal_foley"]
    assert [
        (output["name"], output.get("artifact_type"), output["path_template"])
        for output in declared["outputs"]
    ] == [
        ("audio", "audio", "{out}/audio.wav"),
        ("audio_manifest", None, "{out}/manifest.json"),
        ("audio_provenance", "application/json", "{out}/audio.wav.fal.json"),
    ]
    assert declared["inputs"][0]["name"] == "clip"
    assert declared["inputs"][1]["name"] == "prompt"
    assert list(declared["invocation"]["command"]["argv"][-6:]) == [
        "--clip",
        "{clip}",
        "--prompt",
        "{prompt}",
        "--out",
        "{out}/audio.wav",
    ]
