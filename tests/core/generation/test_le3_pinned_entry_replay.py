"""LE-3: recorded ref resolution through real CLI save and CPU-target replay."""
from __future__ import annotations

import json
import subprocess
import wave

import pytest

from astrid.core.generation import cli, preflight
from tests.core.generation.backends.test_comfy_model_resolution import install_target
from tests.core.generation.backends.test_comfy_target import (  # noqa: F401
    engine_configuration, network, observation_configuration,
)
from tests.core.generation.fixtures.plan56_sam import (  # noqa: F401
    sam_api_path, sam_object_info, sam_offline_lookup,
)
from vibecomfy.entries import entry_added_node_refs, load_entry

URL = "https://github.com/ethanfel/ComfyUI-SAM-Audio"
PIN = "3cfbd03da3019c7252ba13efd5694b1b2ff487e5"


def _record_git(monkeypatch, reply=None, exit_code=None):
    original = subprocess.run
    calls = []

    def run(command, *args, **kwargs):
        if isinstance(command, (list, tuple)) and "ls-remote" in command:
            calls.append(list(command))
            assert command[0] == "git" and URL in command
            if exit_code is not None:
                raise subprocess.CalledProcessError(exit_code, command)
            return subprocess.CompletedProcess(command, 0, reply, "")
        return original(command, *args, **kwargs)

    monkeypatch.setattr("vibecomfy.custom_node_refs.subprocess.run", run)
    return calls


def test_symbolic_add_is_pinned_before_consent_and_saved_replay_is_ready(
    network, monkeypatch, tmp_path, capsys, sam_api_path,
):
    from astrid.core.generation.node_approvals import read_entry_approvals

    client, requests = install_target(network, sam_object_info())
    monkeypatch.setattr(preflight, "_module_available", lambda name: name == "vibecomfy")
    monkeypatch.setattr(preflight, "_comfyui_runtime_available",
                        lambda: pytest.fail("managed engine probed"))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("save or replay opened Runtime"))
    calls = _record_git(monkeypatch, reply=f"{PIN}\trefs/heads/main\n")
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    original = sam_api_path.read_bytes()
    endpoint = client.target.url
    command = ["workflow", str(sam_api_path), "--as-entry", "sam-pinned",
               "--add", URL + "@main", "--placement", endpoint, "--no-run", "--json"]
    assert cli.main([*command, "--dry-run"]) == 2
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["status"] == "blocked"
    assert refusal["issues"][0]["code"] == "needs_consent"
    assert not root.exists()
    assert len(calls) == 1
    then = refusal["fixes"][0]["instruction"]["then_argv"]
    assert then[:2] == ["astrid", "generate"]
    assert then[then.index("--add") + 1] == URL + "@" + PIN
    assert "--dry-run" not in then and "--no-run" in then
    assert cli.main(then[2:]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "saved": True, "ran": False, "entry": "sam-pinned", "replaced": False,
    }
    entry = load_entry(root / "sam-pinned")
    assert {(ref["url"], ref["commit"]) for ref in entry_added_node_refs(entry)} == {(URL, PIN)}
    assert any(row["identity"] == {"repo": URL, "commit": PIN}
               and row["placement_class"] == "attached"
               for row in read_entry_approvals(root / "sam-pinned"))
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    audio = tmp_path / "clip.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(b"\0\0" * 48)
    assert cli.main(["audio", "--entry", "sam-pinned", "--mode", "workflow",
                     "--audio-ref", str(audio), "--placement", endpoint,
                     "--dry-run", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready", result
    assert "audio" in result["entry"]["accepted_inputs"]
    assert len(calls) == 1, "full-pin retry and saved replay must not resolve a moving branch"
    assert ("GET", "/object_info") in requests
    assert all(method == "GET" for method, _ in requests)
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert sam_api_path.read_bytes() == original


@pytest.mark.parametrize("missing", [True, False])
def test_cli_missing_or_ambiguous_ref_has_linked_corrected_retry(
    monkeypatch, tmp_path, capsys, sam_api_path, missing,
):
    root = tmp_path / "entries"
    monkeypatch.setenv("VIBECOMFY_ENTRIES_DIR", str(root))
    monkeypatch.setattr(cli, "_open_client", lambda: pytest.fail("resolution refusal opened Runtime"))
    monkeypatch.setattr(cli, "_generate_facade", lambda: pytest.fail("resolution refusal reached generation"))
    calls = _record_git(monkeypatch, exit_code=2 if missing else None,
                        reply=f"{PIN}\trefs/heads/main\n{'b' * 40}\trefs/tags/main\n")
    assert cli.main(["workflow", str(sam_api_path), "--as-entry", "sam-pinned",
                     "--placement", "managed", "--add", URL + "@main",
                     "--no-run", "--dry-run", "--json"]) == 2
    refusal = json.loads(capsys.readouterr().out)
    assert refusal["issues"][0]["code"] == "commit_unavailable"
    fix = refusal["fixes"][0]
    assert refusal["issues"][0]["fix_ids"] == [fix["id"]]
    assert fix["who"] == "human"
    assert "use a full commit or an exact branch or tag" in fix["instruction"]["step"]
    assert "--dry-run" not in fix["instruction"]["then_argv"]
    assert "<NODE_REF_1>" in fix["instruction"]["then_argv"]
    assert len(calls) == 1
    assert not root.exists()
