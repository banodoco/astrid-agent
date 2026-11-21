"""The Agent's shipped transcription workflow carries its runtime node pin."""

from __future__ import annotations

import ast
from pathlib import Path


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
