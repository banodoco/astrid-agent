"""Plan 56 CPU stand-in: exact owner API graph, no weights or paid resources."""
from __future__ import annotations

import json

import pytest

# plans/56-workflow-setup/evidence/sam-test-files/sam-audio-separation.api.json
# source blob 13895ffee92f7bd50e04ff5973253d961f18c87e.
SAM_API = {
    "1": {"class_type": "SAMAudioPipelineLoader", "_meta": {"title": "SAM-Audio Model Loader"},
          "inputs": {"model_name": "facebook/sam-audio-large", "attention_backend": "pytorch",
                     "precision": "auto", "initial_device": "cpu"}},
    "2": {"class_type": "LoadAudio", "_meta": {"title": "Load Audio (set to the clip's audio file)"},
          "inputs": {"audio": "input.wav"}},
    "3": {"class_type": "SAMAudioTextSeparator", "_meta": {"title": "SAM-Audio Text Separate (set description)"},
          "inputs": {"pipeline": ["1", 0], "audio": ["2", 0], "description": "man speaking",
                     "predict_spans": False, "seed": 0, "inference_steps": 32, "chunk_duration": 10.0,
                     "chunk_overlap": 1.0, "reranking_candidates": 1}},
    "4": {"class_type": "SaveAudio", "_meta": {"title": "Save target (the described sound)"},
          "inputs": {"audio": ["3", 0], "filename_prefix": "audio/sam_target"}},
    "5": {"class_type": "SaveAudio", "_meta": {"title": "Save residual (everything else)"},
          "inputs": {"audio": ["3", 1], "filename_prefix": "audio/sam_residual"}},
}


def _schema(required, outputs, *, sink=False):
    return {"input": {"required": required}, "output": outputs,
            "output_name": outputs, "output_is_list": [False] * len(outputs), "output_node": sink}


def sam_object_info():
    """Advertise the graph's exact selected values and connected socket types."""
    return {
        "SAMAudioPipelineLoader": _schema({
            "model_name": [["facebook/sam-audio-large"]],
            "attention_backend": [["pytorch"]], "precision": [["auto"]],
            "initial_device": [["cpu"]]}, ["SAM_AUDIO_PIPELINE"]),
        "LoadAudio": _schema({"audio": [["input.wav"]]}, ["AUDIO"]),
        "SAMAudioTextSeparator": _schema({
            "pipeline": ["SAM_AUDIO_PIPELINE"], "audio": ["AUDIO"],
            "description": ["STRING", {"default": "man speaking"}],
            "predict_spans": ["BOOLEAN", {"default": False}], "seed": ["INT", {"default": 0}],
            "inference_steps": ["INT", {"default": 32}],
            "chunk_duration": ["FLOAT", {"default": 10.0}],
            "chunk_overlap": ["FLOAT", {"default": 1.0}],
            "reranking_candidates": ["INT", {"default": 1}]}, ["AUDIO", "AUDIO"]),
        "SaveAudio": _schema({"audio": ["AUDIO"], "filename_prefix": ["STRING", {"default": "audio"}]},
                             [], sink=True),
    }


@pytest.fixture
def sam_api_path(tmp_path):
    source = tmp_path / "sam.json"
    source.write_text(json.dumps(SAM_API), encoding="utf-8")
    return source
