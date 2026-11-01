from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("ASTRID_INTERNAL_INVOCATION", "1")

from astrid.core._shared.result_manifest import harvest_staged_outputs
from astrid.core.contracts.binding import assert_provided_inputs_bound, expand_command
from astrid.core.execution.executor.actions import action_executor_definition
from astrid.core.execution.generic_host import _bind_host_owned_command_outputs
from astrid.core.pack.canonical import validate_canonical_pack
from astrid.core.pack.discovery import DiscoveredPack
from astrid.core.pack.loader import load_pack_manifest
from astrid.packs.fal.actions.fal_foley import run

PACK_ROOT = Path(__file__).resolve().parents[3] / "astrid" / "packs" / "fal"


def _action() -> dict[str, object]:
    return validate_canonical_pack(PACK_ROOT).definition.actions["fal_foley"]


def _definition() -> object:
    pack = load_pack_manifest(PACK_ROOT / "pack.yaml")
    discovered_pack = DiscoveredPack(pack, "source", 0)
    return action_executor_definition(
        discovered_pack,
        "fal_foley",
        pack.actions["fal_foley"],
    )


def _binding_values(tmp_path: Path, *, env_file: Path | None = None) -> dict[str, object]:
    clip = tmp_path / "fixture clip.mp4"
    clip.write_bytes(b"fixture video")
    values: dict[str, object] = {
        "clip": str(clip),
        "prompt": "quiet rain on glass",
        "out": str(tmp_path / "foley output"),
        "python_exec": sys.executable,
    }
    if env_file is not None:
        values["env_file"] = str(env_file)
    return values


def test_fal_pack_validates_and_declares_receiver_contract() -> None:
    entry = validate_canonical_pack(PACK_ROOT)
    assert entry.definition.schema_version == 3

    action = entry.definition.actions["fal_foley"]
    inputs = {item["name"]: item for item in action["inputs"]}
    assert inputs["env_file"] == {
        "name": "env_file",
        "type": "file",
        "required": False,
        "description": "Optional env file containing FAL_KEY for this invocation.",
    }
    assert action["invocation"]["command"]["input_args"][-1] == {
        "input": "env_file",
        "flag": "--env-file",
        "optional": True,
    }

    assert [
        (output["name"], output["type"], output.get("artifact_type"), output["path_template"])
        for output in action["outputs"]
    ] == [
        ("audio", "file", "audio", "{out}/audio.wav"),
        ("audio_manifest", "file", None, "{out}/manifest.json"),
        ("audio_provenance", "file", "application/json", "{out}/audio.wav.fal.json"),
    ]


def test_fal_binding_omits_optional_env_file(tmp_path: Path) -> None:
    action = _action()
    values = _binding_values(tmp_path)
    binding = expand_command(
        action["invocation"]["command"], action["inputs"], values, action.get("metadata")
    )
    assert "--env-file" not in binding.argv
    assert_provided_inputs_bound(binding, action["inputs"], values, action.get("metadata"))


def test_fal_binding_preserves_env_file_path_with_spaces(tmp_path: Path) -> None:
    action = _action()
    env_file = tmp_path / "credentials with spaces.env"
    env_file.write_text("FAL_KEY=fixture\n", encoding="utf-8")
    values = _binding_values(tmp_path, env_file=env_file)
    binding = expand_command(
        action["invocation"]["command"], action["inputs"], values, action.get("metadata")
    )
    index = binding.argv.index("--env-file")
    assert binding.argv[index + 1] == str(env_file)
    assert binding.argv.count("--env-file") == 1
    assert "env_file" in binding.bound_inputs
    assert_provided_inputs_bound(binding, action["inputs"], values, action.get("metadata"))


def test_fal_dry_run_uses_fixture_clip_without_credentials_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clip = tmp_path / "dry run clip.mp4"
    clip.write_bytes(b"fixture video")
    env_file = tmp_path / "dry run credentials.env"
    env_file.write_text("not consulted\n", encoding="utf-8")
    out = tmp_path / "dry run output" / "audio.wav"

    monkeypatch.setattr(
        run.CredentialsScope,
        "get_local",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("credentials resolved")),
    )
    monkeypatch.setattr(
        run,
        "default_client",
        lambda: (_ for _ in ()).throw(AssertionError("provider client created")),
    )

    assert run.main(
        [
            "--clip",
            str(clip),
            "--prompt",
            "quiet rain",
            "--out",
            str(out),
            "--env-file",
            str(env_file),
            "--dry-run",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "model_id": run.FAL_MODEL_ID,
        "clip": str(clip.resolve()),
        "prompt": "quiet rain",
        "out": str(out.resolve()),
    }


def test_host_binding_and_harvesting_keep_declared_files_contained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _action()
    definition = _definition()
    record = SimpleNamespace(id="fal.fal_foley", definition=definition)
    output_root = tmp_path / "attempt outputs"
    output_root.mkdir()
    values = _binding_values(tmp_path)
    values["out"] = str(output_root)

    command_values, harvest_values = _bind_host_owned_command_outputs(
        record, values, output_root=output_root
    )
    assert command_values["audio"] == str(output_root / "audio.wav")
    assert harvest_values["audio_manifest"] == str(output_root / "manifest.json")
    assert harvest_values["audio_provenance"] == str(output_root / "audio.wav.fal.json")

    binding = expand_command(
        action["invocation"]["command"],
        action["inputs"],
        command_values,
        action.get("metadata"),
    )
    module_index = binding.argv.index("astrid.packs.fal.actions.fal_foley.run")
    runner_args = list(binding.argv[module_index + 1 :])

    synthetic_result = {
        "request_id": "fixture-request",
        "audio": {
            "url": "https://fixture.invalid/audio.wav",
            "content_type": "audio/wav",
            "file_name": "fixture.wav",
        },
    }
    audio_bytes = b"fixture audio bytes\n"
    registered_keys: list[str] = []
    fake_client = SimpleNamespace(
        register_secret=lambda key: registered_keys.append(key),
    )

    def synthetic_submit(
        client: object,
        model_id: str,
        payload: dict[str, object],
        api_key: str,
        *,
        max_wait_sec: int,
    ) -> dict[str, object]:
        assert client is fake_client
        assert model_id == run.FAL_MODEL_ID
        assert payload["text_prompt"] == "quiet rain on glass"
        assert api_key == "fixture-fal-key"
        assert max_wait_sec == 600
        return synthetic_result

    def save_fixture_audio(
        client: object, result: dict[str, object], destination: Path
    ) -> dict[str, object]:
        assert client is fake_client
        assert result is synthetic_result
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(audio_bytes)
        return {
            "path": str(destination),
            "source_url": synthetic_result["audio"]["url"],
            "content_type": "audio/wav",
            "file_name": "fixture.wav",
            "bytes": len(audio_bytes),
        }

    monkeypatch.setattr(
        run.CredentialsScope,
        "get_local",
        lambda *_args, **_kwargs: "fixture-fal-key",
    )
    monkeypatch.setattr(run, "default_client", lambda: fake_client)
    monkeypatch.setattr(run, "fal_submit_and_poll", synthetic_submit)
    monkeypatch.setattr(run, "_save_audio", save_fixture_audio)

    assert run.main(runner_args) == 0
    assert registered_keys == ["fixture-fal-key"]

    manifest_path = output_root / "manifest.json"
    audio_path = output_root / "audio.wav"
    sidecar_path = output_root / "audio.wav.fal.json"
    receipt = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt_outputs = {entry["name"]: entry for entry in receipt["outputs"]}
    assert set(receipt_outputs) == {"audio", "audio_provenance"}

    expected_paths = {
        "audio": audio_path,
        "audio_provenance": sidecar_path,
    }
    for name, expected_path in expected_paths.items():
        content = expected_path.read_bytes()
        descriptor = receipt_outputs[name]
        assert descriptor["path"] == expected_path.name
        assert descriptor["content_hash"] == (
            "sha256:" + hashlib.sha256(content).hexdigest()
        )
        assert descriptor["bytes"] == len(content)
        assert expected_path.resolve().is_relative_to(output_root.resolve())

    harvested = harvest_staged_outputs(
        output_root,
        definition=definition,
        declared_outputs=definition.outputs,
        values=harvest_values,
        require=True,
    )
    assert {item["name"] for item in harvested} == {
        "audio",
        "audio_provenance",
    }
    assert all(
        Path(item["path"]).resolve().is_relative_to(output_root.resolve())
        for item in harvested
    )
    assert not any(
        Path(item["path"]).resolve() == manifest_path.resolve() for item in harvested
    )
    for item in harvested:
        expected_path = expected_paths[item["name"]]
        content = expected_path.read_bytes()
        assert Path(item["path"]).resolve() == expected_path.resolve()
        assert item["content_hash"] == (
            "sha256:" + hashlib.sha256(content).hexdigest()
        )
        assert item["bytes"] == len(content)
