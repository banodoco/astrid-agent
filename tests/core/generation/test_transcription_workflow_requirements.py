"""The Agent's shipped transcription workflow carries its runtime node pin."""

from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path

import pytest


WORKFLOW = (Path(__file__).resolve().parents[3]
            / "astrid/packs/generation/workflows/transcription/workflow.py")


def test_agent_transcription_declares_whisper_requirements_and_pin():
    tree = ast.parse(WORKFLOW.read_text(encoding="utf-8"))
    build = next(node for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "build" and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == "ReadyMetadata")
    declared = next((keyword.value for keyword in build.keywords
                     if keyword.arg == "requirements"), None)
    assert declared is not None, "Agent ready metadata must declare workflow requirements"
    requirements = ast.literal_eval(declared)
    assert requirements["custom_nodes"] == [{
        "name": "ComfyUI-Whisper",
        "slug": "ComfyUI-Whisper",
        "source": "git",
        "commit": "006a709848259ed3d69e2f9fcfd7a75f6c860ddd",
        "url": "https://github.com/yuvraj108c/ComfyUI-Whisper.git",
        "class_set": ["Apply Whisper"],
        "class_schema_sha256": "f3933350d1e63c45f36fe1dac1d56691b388c2faaee37e65ff458a5cd5200029",
        "pip_packages": ["openai-whisper", "soundfile"],
    }]


def test_agent_transcription_runtime_pin_and_strict_schema_drift(tmp_path, monkeypatch):
    """Build Agent's shipped module through VibeComfy and check its runtime pin."""
    from vibecomfy.custom_node_refs import workflow_lock_entries
    from vibecomfy.errors import DriftError
    from vibecomfy.runtime.drift import _invalidate_cache_entry, enforce_strict_drift

    workflow_path = Path(os.environ.get("ASTRID_TRANSCRIPTION_WORKFLOW", WORKFLOW))
    spec = importlib.util.spec_from_file_location("agent_transcription_workflow", workflow_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    workflow = module.build()

    requirements = workflow.metadata["requirements"]
    assert "ComfyUI-Whisper" in requirements["custom_nodes"]
    declared = requirements["custom_node_refs"]
    assert declared
    agent_pin = declared[0]
    entries = workflow_lock_entries(workflow)
    assert len(entries) == 1
    lock_pin = entries[0]
    assert lock_pin.name == agent_pin["name"] == "ComfyUI-Whisper"
    assert lock_pin.slug == agent_pin["slug"]
    assert lock_pin.source == agent_pin["source"] == "git"
    assert lock_pin.git_commit_sha == agent_pin["commit"] == "006a709848259ed3d69e2f9fcfd7a75f6c860ddd"
    assert lock_pin.url == agent_pin["url"]
    assert list(lock_pin.class_set) == agent_pin["class_set"] == ["Apply Whisper"]
    assert lock_pin.pip_packages == tuple(agent_pin["pip_packages"])
    # The schema digest is recaptured by VibeComfy; verify propagation without
    # freezing a particular digest in this test.
    assert lock_pin.class_schema_sha256 == agent_pin["class_schema_sha256"]

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("vibecomfy.node_packs.read_lockfile", lambda path=None: [])
    monkeypatch.setattr("vibecomfy.runtime.drift._nodepack_dir", lambda name: tmp_path)
    monkeypatch.setattr("vibecomfy.runtime.drift._git_head", lambda path: lock_pin.git_commit_sha)
    monkeypatch.setattr("vibecomfy.runtime.drift._git_origin", lambda path: lock_pin.url)
    monkeypatch.setattr("vibecomfy.runtime.drift._canonical_pack_schema_hash", lambda entry: {
        "status": "canonical", "hash": "0" * 64,
    })
    monkeypatch.setattr("vibecomfy.runtime.drift._comfyui_git_head", lambda: None)
    _invalidate_cache_entry(workflow)
    with pytest.raises(DriftError, match="schema hash"):
        enforce_strict_drift(workflow)
