"""Explicit frame counts must survive admission, including invalid zero."""
import pytest
from astrid.core.generation import entries


@pytest.mark.parametrize("frames", [0, -1, 4])
def test_h3_plain_invalid_frames_are_rejected(frames):
    namespace = entries.entry_hooks(entries.find_entry("h3"))["build"].__globals__
    with pytest.raises(ValueError, match="duration|frames"):
        document, _ = namespace["_plain_request"](
            {"prompt": "a kite", "frames": frames}, {"image_ref": "/x/look.png"}, object())
        namespace["_shared"]("request").normalize_request(document)


def test_h3_plain_omitted_frames_still_defaults_to_120():
    namespace = entries.entry_hooks(entries.find_entry("h3"))["build"].__globals__
    document, _ = namespace["_plain_request"](
        {"prompt": "a kite"}, {"image_ref": "/x/look.png"}, object())
    assert document["output"]["duration"] == 120 / 24


def test_h3_plain_363_frames_compiles_and_warns(tmp_path):
    from PIL import Image
    from vibecomfy.cli_loader import load_workflow_any
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.compile import compile_preparation

    image = tmp_path / "reference.png"
    Image.new("RGB", (32, 32)).save(image)
    namespace = entries.entry_hooks(entries.find_entry("h3"))["build"].__globals__
    document, assets = namespace["_plain_request"](
        {"prompt": "a kite", "frames": 363}, {"image_ref": str(image)}, object())
    request = namespace["_shared"]("request").normalize_request(document)
    prepared = prepare_request(request, asset_map=assets)
    assert any(warning["limit"] == "job_duration_seconds" for warning in prepared["warnings"])
    compiled = compile_preparation(prepared, out_dir=tmp_path / "compiled")
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(row for row in api.values() if row["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert target["inputs"]["length"] == 379
