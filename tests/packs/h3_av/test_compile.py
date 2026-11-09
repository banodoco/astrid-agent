from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.packs.h3_av.shared.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request


PROMPT = (
    'End state: Morpheus remains in the same close/medium shot, seated in the red chair in the dark room, '
    'wearing the black coat, with the same camera position, lighting, identity, and acoustic perspective. '
    'In the generated continuation, he begins by saying the exact line: "This is your last chance." He then '
    'continues with the exact line: "You can poo or pee on my face." Preserve the supplied audiovisual prefix, '
    "including Morpheus's original voice, cadence, room tone, and timing, then carry that voice and acoustic "
    'perspective into both requested lines. Keep his expression calm and deliberate, looking forward with one '
    'hand resting in a restrained natural gesture. No camera change, reset, new character, subtitles, or visible text.'
)


def _request(prompt: str = PROMPT):
    return normalize_request(
        {
            "version": 1,
            "operation": "continue",
            "source": {"asset": "source", "range": [0, 4]},
            "output": {"duration": 8},
            "content": {"prompt": prompt},
            "changes": {
                "video": [{"during": [4, 8], "area": {"full_frame": True}, "action": "generate"}],
                "audio": [{"during": [4, 8], "action": "generate"}],
            },
            "references": [],
            "overrides": {"seed": 42, "steps": 8},
        }
    )


def test_compile_freezes_bundle_and_is_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture-video")
    request = _request()
    preparation = prepare_request(request, asset_map={"source": str(source)})

    first = compile_preparation(preparation, out_dir=tmp_path / "first")
    second = compile_preparation(preparation, out_dir=tmp_path / "second")

    assert first["compilation_digest"] == second["compilation_digest"]
    assert Path(first["managed_assets"]["path"]).read_bytes() == Path(second["managed_assets"]["path"]).read_bytes()
    inputs = first["workflow_inputs"]
    assert inputs["model"] == "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
    assert inputs["seed"] == 42
    assert inputs["steps"] == 8
    assert inputs["prompt"] == PROMPT
    assert inputs["duration"] == 141 / 24
    assert inputs["source_start"] == 0.0
    assert inputs["source_frames"] == 96
    assert inputs["source_video"] == next(
        iter(first["managed_assets"]["manifest"]["assets"])
    )["member"].split("/")[-1]
    assert first["continuation_timing"]["requested_new_frames"] == 96
    assert first["continuation_timing"]["generated_capacity_frames"] == 102
    assert first["continuation_timing"]["trim_tail_frames"] == 6
    assert first["capabilities"]["output_contract"] == "muxed_av_full_timeline"
    for name in ("workflow.py", "workflow.vibe.json", "source.json"):
        frozen = Path(first["workflow"][name]["path"])
        assert frozen.parent.name == "workflow-bundle"
        assert frozen.is_file()


def test_compile_binds_arbitrary_prompt_in_the_native_graph(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture-video")
    preparation = prepare_request(_request("a different prompt"), asset_map={"source": str(source)})

    compiled = compile_preparation(preparation, out_dir=tmp_path / "compiled")

    assert compiled["workflow_inputs"]["prompt"] == "a different prompt"


def test_compilation_manifest_round_trips(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture-video")
    result = compile_preparation(
        prepare_request(_request(), asset_map={"source": str(source)}),
        out_dir=tmp_path / "compiled",
    )
    persisted = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
    assert persisted["compilation_digest"] == result["compilation_digest"]
    assert persisted["capabilities"]["operation"] == "continue"


