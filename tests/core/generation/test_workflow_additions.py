"""LE-3 Agent boundary: resolution and repair preserve actual CLI controls."""
from __future__ import annotations

import subprocess

import pytest

from astrid.core.generation.preflight import GenerationReadinessError
from astrid.core.generation.workflow_additions import resolve_workflow_additions

URL = "https://github.com/example/node-pack"
PIN = "a" * 40


def test_resolve_once_and_replay_full_pin_without_lookup(monkeypatch):
    calls = []

    def remote(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, f"{PIN}\trefs/heads/main\n", "")

    monkeypatch.setattr("vibecomfy.custom_node_refs.subprocess.run", remote)
    refs = resolve_workflow_additions([URL + "@main"], retry_argv=[
        "astrid", "generate", "workflow", "source.json", "--as-entry", "example",
        "--add", URL + "@main",
    ])
    assert refs == [{"slug": "node-pack", "source": "git", "url": URL,
                     "commit": PIN, "entry_add": True}]
    assert len(calls) == 1
    assert resolve_workflow_additions([URL + "@" + PIN], retry_argv=[
        "astrid", "generate", "workflow", "source.json", "--as-entry", "example",
        "--add", URL + "@" + PIN,
    ]) == refs
    assert len(calls) == 1


@pytest.mark.parametrize("equal_form", [False, True])
def test_failed_resolution_has_corrected_retry_and_drops_stale_code_consent(monkeypatch, equal_form):
    def remote(command, **kwargs):
        raise subprocess.CalledProcessError(2, command)

    monkeypatch.setattr("vibecomfy.custom_node_refs.subprocess.run", remote)
    add = ["--add=" + URL + "@main"] if equal_form else ["--add", URL + "@main"]
    argv = ["astrid", "generate", "workflow", "source.json", "--as-entry", "example",
            "--placement", "runpod", "--replace", "--no-run", "--dry-run", "--prompt", "hello",
            "--consent", "user_env", "--consent=code=0123456789ab", *add]
    with pytest.raises(GenerationReadinessError) as raised:
        resolve_workflow_additions([URL + "@main"], retry_argv=argv)
    envelope = raised.value.to_envelope()
    assert envelope["status"] == "blocked"
    assert envelope["attempt_count"] == 0
    assert envelope["issues"][0]["code"] == "commit_unavailable"
    fix = envelope["fixes"][0]
    assert envelope["issues"][0]["fix_ids"] == [fix["id"]]
    assert fix["who"] == "human"
    assert "use a full commit or an exact branch or tag" in fix["instruction"]["step"]
    assert fix["instruction"]["then_argv"] == [
        "astrid", "generate", "workflow", "source.json", "--as-entry", "example",
        "--placement", "runpod", "--replace", "--no-run", "--prompt", "hello",
        "--consent", "user_env", "--add", "<NODE_REF_1>", "--json",
    ]
    assert set(fix["placeholders"]) == {"NODE_REF_1"}


def test_literal_angle_brackets_and_empty_input_survive_as_explicit_placeholders(monkeypatch):
    def remote(command, **kwargs):
        raise subprocess.CalledProcessError(128, command)

    monkeypatch.setattr("vibecomfy.custom_node_refs.subprocess.run", remote)
    argv = ["astrid", "generate", "workflow", "source.json", "--as-entry", "example",
            "--prompt", "<face>", "--negative-prompt", "", "--add", URL]
    with pytest.raises(GenerationReadinessError) as raised:
        resolve_workflow_additions([URL], retry_argv=argv)
    envelope = raised.value.to_envelope()
    assert envelope["issues"][0]["code"] == "source_unreachable"
    fix = envelope["fixes"][0]
    assert len(fix["placeholders"]) == 3
    then = fix["instruction"]["then_argv"]
    assert then[then.index("--prompt") + 1].startswith("<ORIGINAL_ARG_")
    assert then[then.index("--negative-prompt") + 1].startswith("<ORIGINAL_ARG_")
