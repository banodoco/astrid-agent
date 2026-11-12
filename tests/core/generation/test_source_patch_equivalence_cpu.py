"""Offline original-checkpoint and numerical proof for the declared TTS patches."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import torch
from safetensors.torch import load_file

from test_source_patch_config_cpu import node  # noqa: F401 (shared declared-patch fixture)

pytestmark = pytest.mark.engine_deps
FIXTURES = Path(__file__).parent / "fixtures"
CHECKPOINTS = FIXTURES / "qwen3-tts-tiny"
REFERENCE = json.loads((CHECKPOINTS / "reference.json").read_text())
ATOL = 1e-6
spec = importlib.util.spec_from_file_location(
    "tts_equivalence_generator", FIXTURES / "generate_qwen3_tts_tiny_equivalence.py",
)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def constructors(node):
    c, m, tc, tm = node
    t, tok = harness.configurations(c, tc)
    return {
        "talker": (m.Qwen3TTSTalkerForConditionalGeneration, dict(config=t)),
        "predictor": (m.Qwen3TTSTalkerCodePredictorModelForConditionalGeneration,
                      dict(config=t.code_predictor_config, talker_config=t)),
        "tokenizer": (tm.Qwen3TTSTokenizerV2Model, dict(config=tok)),
    }


def assert_close(actual, expected, atol=ATOL):
    actual, expected = torch.tensor(actual), torch.tensor(expected)
    torch.testing.assert_close(actual, expected, atol=atol, rtol=0)
    return float((actual - expected).abs().max())


@pytest.mark.parametrize("component", ["talker", "predictor", "tokenizer"])
def test_original_layout_from_pretrained_initializes_rotary_buffers(node, component, record_property):
    cls, kwargs = constructors(node)[component]
    model, info = cls.from_pretrained(CHECKPOINTS / component, local_files_only=True,
                                     output_loading_info=True, **kwargs)
    for field in ("missing_keys", "unexpected_keys", "mismatched_keys", "error_msgs", "conversion_errors"):
        assert not info.get(field), info
    original = load_file(CHECKPOINTS / component / "model.safetensors")
    loaded = model.state_dict()
    assert len(original) == len(loaded)
    # Every source tensor has a distinct deterministic value. Match exact tensor
    # contents bijectively after the native recursive Mimi conversion, without
    # renaming keys in the loading path or supplying a conversion mapping.
    remaining = dict(original)
    changed_names = []
    for name, tensor in loaded.items():
        matches = [key for key, value in remaining.items()
                   if value.shape == tensor.shape and torch.equal(value, tensor)]
        assert len(matches) == 1, (name, matches)
        key = matches[0]
        if key != name:
            changed_names.append((key, name))
        del remaining[key]
    assert not remaining
    if component == "tokenizer":
        assert changed_names
        assert all(a.startswith("encoder.") and b.startswith("encoder.") for a, b in changed_names)
    else:
        assert not changed_names
    count = 0
    for name, module in model.named_modules():
        if not hasattr(module, "inv_freq"):
            continue
        expected, _ = module.compute_default_rope_parameters(module.config)
        for buffer_name in ("inv_freq", "original_inv_freq"):
            actual = getattr(module, buffer_name)
            assert actual.device.type == "cpu", (name, buffer_name)
            torch.testing.assert_close(actual, expected, atol=0, rtol=0)
            assert buffer_name in module._non_persistent_buffers_set
            assert f"{name}.{buffer_name}" not in loaded
            assert f"{name}.{buffer_name}" not in original
        count += 1
    assert count > 0
    record_property("loaded_tensor_count", len(loaded))
    record_property("rotary_module_count", count)
    record_property("max_abs_loaded_tensor_difference", 0.0)


def test_original_talker_predictor_prefill_and_cached_steps(node, record_property):
    models = {}
    for component in ("talker", "predictor"):
        cls, kwargs = constructors(node)[component]
        models[component] = cls(**kwargs).eval()
        models[component].load_state_dict(load_file(CHECKPOINTS / component / "model.safetensors"), strict=True)
    actual = harness.direct_comparison(**models)
    differences = [assert_close(step, reference, atol=0) for name in models
                   for step, reference in zip(actual[name], REFERENCE["direct"][name], strict=True)]
    record_property("max_abs_difference", max(differences))


def test_generate_left_padded_batch_matches_full_sequence_and_original(node, record_property):
    cls, kwargs = constructors(node)["talker"]
    model = cls(**kwargs).eval()
    model.load_state_dict(load_file(CHECKPOINTS / "talker/model.safetensors"), strict=True)
    actual = harness.generation_comparison(model)
    expected = REFERENCE["generation"]
    assert actual["cache_lengths"] == expected["cache_lengths"] == [4, 5, 6]
    assert actual["positions"] == expected["positions"]
    assert actual["positions"][1] == [[2], [3]]
    assert actual["positions"][2] == [[3], [4]]
    assert actual["sequences"] == expected["sequences"]
    assert max(actual["cached_full_max_abs"]) <= ATOL
    differences = [assert_close(actual[field], expected[field]) for field in ("scores", "full_logits")]
    record_property("max_abs_cross_version_difference", max(differences))
    record_property("max_abs_cached_full_difference", max(actual["cached_full_max_abs"]))


def test_tokenizer_nonzero_codebooks_encode_then_decode_matches_original(node, record_property):
    cls, kwargs = constructors(node)["tokenizer"]
    model = cls.from_pretrained(CHECKPOINTS / "tokenizer", local_files_only=True, **kwargs).eval()
    codebooks = [value for name, value in model.state_dict().items()
                 if name.endswith(("embed_sum", "embedding_sum"))]
    assert codebooks
    for codebook in codebooks:
        assert (codebook != 0).all()
        assert torch.unique(codebook, dim=0).shape[0] == codebook.shape[0]
    actual = harness.tokenizer_comparison(model)
    assert actual["codes"] == REFERENCE["tokenizer"]["codes"]
    difference = assert_close(actual["audio"], REFERENCE["tokenizer"]["audio"])
    record_property("max_abs_decoder_difference", difference)
