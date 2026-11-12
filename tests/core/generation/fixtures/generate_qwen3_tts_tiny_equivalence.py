"""Generate offline checkpoints and numerical references with pinned original TTS.

Example (no Hub weights):
  python generate_qwen3_tts_tiny_equivalence.py --source /path/ComfyUI-QwenTTS \
    --old-python /path/tf4573/bin/python

The pinned original source runs unchanged. All parameter keys and values
are saved by transformers 4.57.3's save_pretrained.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

HERE = Path(__file__).resolve().parent
PIN = "d8122a8ba835b65fd65c113d2b273b1ad1579293"
DEST = HERE / "qwen3-tts-tiny"


def configurations(configs, tokenizer_configs):
    import math
    payload = json.loads((HERE / "speech-config.json").read_text())
    t = payload["talker_config"]
    for c in (t, t["code_predictor_config"]):
        c.update(hidden_size=6, intermediate_size=12, head_dim=6,
                 num_attention_heads=1, num_key_value_heads=1,
                 num_hidden_layers=1, vocab_size=8, num_code_groups=4)
        if "layer_types" in c:
            c["layer_types"] = c["layer_types"][:1]
    t.update(text_hidden_size=6, text_vocab_size=8)
    t["rope_scaling"]["mrope_section"] = [1, 1, 1]
    talker = configs.Qwen3TTSConfig.from_dict(payload).talker_config
    payload = json.loads((HERE / "speech-tokenizer-config.json").read_text())
    e, d = payload["encoder_config"], payload["decoder_config"]
    for c in (e, d):
        c.update(hidden_size=6, intermediate_size=12, head_dim=6,
                 num_attention_heads=1, num_key_value_heads=1, num_hidden_layers=1,
                 codebook_size=4, num_quantizers=2, vector_quantization_hidden_dimension=2)
    e.update(num_filters=2, codebook_dim=2, upsample_groups=6)
    e["upsampling_ratios"] = [2] * len(e["upsampling_ratios"])
    e["upsampling_ratios"][-1] *= 2
    d["upsample_rates"] = [2] * len(d["upsample_rates"])
    d["upsampling_ratios"] = [2] * len(d["upsampling_ratios"])
    d.update(latent_dim=6, codebook_dim=6, decoder_dim=2 ** len(d["upsample_rates"]))
    stride = math.prod(d["upsample_rates"] + d["upsampling_ratios"])
    e["_frame_rate"] = e["sampling_rate"] / stride
    payload.update(encoder_valid_num_quantizers=2, decode_upsample_rate=stride,
                   encode_downsample_rate=stride)
    return talker, tokenizer_configs.Qwen3TTSTokenizerV2Config.from_dict(payload)


def deterministic_state(model):
    """Distinct, bounded values per tensor, including nonzero VQ codebooks."""
    import torch
    with torch.no_grad():
        for i, (name, tensor) in enumerate(model.state_dict().items(), 1):
            values = (torch.arange(tensor.numel()).reshape(tensor.shape) * .37 + i).sin() * .08
            if "norm" in name and name.endswith("weight") or name.endswith("weight_g"):
                values += 1
            if name.endswith(("cluster_usage", "initialized")):
                values = values.abs() + 1
            if name.endswith(("embed_sum", "embedding_sum")):
                # The decoder API reserves code zero for padding. Make zero far
                # from encoder activations; keep every other vector distinct.
                values[0] += 2
            tensor.copy_(values.to(tensor.dtype))


def direct_comparison(talker, predictor):
    """The previous card's prefill and two cached steps, with strict loading."""
    import torch
    torch.manual_seed(5678)
    with torch.no_grad():
        h = talker.config.hidden_size
        pre = talker(inputs_embeds=torch.randn(1, 2, h), use_cache=True,
                     trailing_text_hidden=torch.randn(1, 1, h), tts_pad_embed=torch.randn(1, 1, h))
        outputs = [pre]
        for _ in range(2):
            prev = outputs[-1]
            outputs.append(talker(input_ids=torch.ones(1, 1, dtype=torch.long),
                past_key_values=prev.past_key_values, use_cache=True, past_hidden=prev.past_hidden,
                generation_step=prev.generation_step, trailing_text_hidden=prev.trailing_text_hidden,
                tts_pad_embed=prev.tts_pad_embed, subtalker_dosample=False))
        p = predictor(inputs_embeds=torch.randn(1, 2, predictor.config.hidden_size), use_cache=True)
        ps = [p]
        for _ in range(2):
            ps.append(predictor(input_ids=torch.ones(1, 1, dtype=torch.long),
                past_key_values=ps[-1].past_key_values, use_cache=True,
                generation_steps=ps[-1].generation_steps))
    return {"talker": [o.logits.tolist() for o in outputs],
            "predictor": [o.logits.tolist() for o in ps]}


def generation_comparison(talker):
    """Real generate, recording each decoder input and no-cache recomputation.

    The hook observes the actual codec/text embeddings assembled by the talker,
    so recomputation exercises the same sequence without rerunning its stochastic
    subgeneration or substituting token embeddings for its multimodal inputs.
    """
    import torch
    torch.manual_seed(8765)
    embeddings, records, position_axes = [], [], []
    mask = torch.tensor([[0, 0, 1, 1], [0, 1, 1, 1]])

    def capture(module, args, kwargs, output):
        embeddings.append(kwargs["inputs_embeds"].detach().clone())
        records.append((kwargs["attention_mask"].clone(), output.last_hidden_state.detach().clone(),
                        output.past_key_values.get_seq_length()))

    def capture_position_ids(module, args):
        observed = args[1].detach()
        assert torch.equal(observed, observed.round())
        position_axes.append(observed.to(torch.long).clone())

    hook = talker.model.register_forward_hook(capture, with_kwargs=True)
    position_hook = talker.model.rotary_emb.register_forward_pre_hook(capture_position_ids)
    try:
        with torch.no_grad():
            generated = talker.generate(inputs_embeds=torch.randn(2, 4, talker.config.hidden_size),
                attention_mask=mask, trailing_text_hidden=torch.randn(2, 2, talker.config.hidden_size),
                tts_pad_embed=torch.randn(2, 1, talker.config.hidden_size),
                max_new_tokens=3, min_new_tokens=3, do_sample=False, subtalker_dosample=False,
                eos_token_id=None, pad_token_id=0, use_cache=True,
                output_scores=True, return_dict_in_generate=True)
    finally:
        hook.remove()
        position_hook.remove()
    full_logits, differences, positions = [], [], []
    with torch.no_grad():
        for i, (attention, cached, _) in enumerate(records):
            position = attention.long().cumsum(-1) - 1
            # Transformers generation assigns padded positions zero; the direct
            # talker path below follows get_rope_index's padding offset instead.
            position.masked_fill_(attention == 0, 0)
            used_positions = position_axes[i]
            assert used_positions.shape[0] == 3
            assert torch.equal(used_positions[0], used_positions[1])
            assert torch.equal(used_positions[0], used_positions[2])
            full_positions = torch.cat(position_axes[:i + 1], dim=-1)
            full = talker.model(inputs_embeds=torch.cat(embeddings[:i+1], dim=1),
                                attention_mask=attention,
                                position_ids=full_positions, use_cache=False)
            # Compare all valid queries at prefill; the one current query later.
            expected = full.last_hidden_state[:, -cached.shape[1]:]
            valid = attention[:, -cached.shape[1]:].bool()
            differences.append(float((cached[valid] - expected[valid]).abs().max()))
            full_logits.append(talker.codec_head(expected[:, -1:]).squeeze(1).tolist())
            # Capture the exact IDs consumed by the model and ensure they agree
            # with the padding-aware positions used for no-cache recomputation.
            current_positions = used_positions[0, :, -embeddings[i].shape[1]:]
            current_valid = attention[:, -embeddings[i].shape[1]:].bool()
            expected_current = position[:, -embeddings[i].shape[1]:]
            assert torch.equal(current_positions[current_valid], expected_current[current_valid])
            positions.append(current_positions.tolist())
    return dict(sequences=generated.sequences.tolist(), scores=[s.tolist() for s in generated.scores],
                full_logits=full_logits, cache_lengths=[r[2] for r in records],
                cached_full_max_abs=differences, positions=positions)


def direct_masked_comparison(talker):
    """Exercise direct cached calls with a padded batch, outside generate()."""
    import torch
    torch.manual_seed(4321)
    embeddings, records, position_axes = [], [], []
    mask = torch.tensor([[0, 0, 1, 1], [0, 1, 1, 1]])
    trailing = torch.randn(2, 2, talker.config.hidden_size)
    pad = torch.randn(2, 1, talker.config.hidden_size)

    def capture(module, args, kwargs, output):
        embeddings.append(kwargs["inputs_embeds"].detach().clone())
        records.append((kwargs["attention_mask"].detach().clone(),
                        output.last_hidden_state.detach().clone(),
                        output.past_key_values.get_seq_length()))

    def capture_position_ids(module, args):
        observed = args[1].detach()
        assert torch.equal(observed, observed.round())
        position_axes.append(observed.to(torch.long).clone())

    hook = talker.model.register_forward_hook(capture, with_kwargs=True)
    position_hook = talker.model.rotary_emb.register_forward_pre_hook(capture_position_ids)
    try:
        with torch.no_grad():
            output = talker(inputs_embeds=torch.randn(2, 4, talker.config.hidden_size),
                attention_mask=mask, trailing_text_hidden=trailing, tts_pad_embed=pad,
                use_cache=True, subtalker_dosample=False)
            for step in range(2):
                mask = torch.cat((mask, torch.ones(2, 1, dtype=mask.dtype)), dim=-1)
                output = talker(input_ids=torch.ones(2, 1, dtype=torch.long),
                    attention_mask=mask, past_key_values=output.past_key_values,
                    past_hidden=output.past_hidden, generation_step=output.generation_step,
                    trailing_text_hidden=trailing, tts_pad_embed=pad,
                    subtalker_dosample=False, use_cache=True)
    finally:
        hook.remove()
        position_hook.remove()

    full_errors, positions = [], []
    with torch.no_grad():
        for i, (attention, cached, cache_length) in enumerate(records):
            full_position = attention.long().cumsum(-1) - 1
            full_position.masked_fill_(attention == 0, 1)
            used_positions = position_axes[i]
            assert used_positions.shape[0] == 3
            assert torch.equal(used_positions[0], used_positions[1])
            assert torch.equal(used_positions[0], used_positions[2])
            current_position = used_positions[0, :, -embeddings[i].shape[1]:]
            assert torch.equal(current_position, full_position[:, -embeddings[i].shape[1]:])
            full = talker.model(inputs_embeds=torch.cat(embeddings[:i + 1], dim=1),
                                attention_mask=attention,
                                position_ids=full_position.unsqueeze(0).expand(3, -1, -1),
                                use_cache=False)
            expected = full.last_hidden_state[:, -cached.shape[1]:]
            valid = attention[:, -cached.shape[1]:].bool()
            full_errors.append(float((cached[valid] - expected[valid]).abs().max()))
            positions.append(current_position.tolist())
            assert cache_length == 4 + i
    return dict(cache_lengths=[r[2] for r in records], positions=positions,
                cached_full_max_abs=full_errors)


def tokenizer_comparison(tokenizer):
    import torch
    torch.manual_seed(9876)
    wave = torch.randn(1, 4 * tokenizer.config.encode_downsample_rate)
    with torch.no_grad():
        encoded = tokenizer.encode(wave, padding_mask=torch.ones_like(wave, dtype=torch.long))
        codes = encoded.audio_codes[0]
        assert (codes[:, 0] != 0).all(), "fixture must decode the complete encoded sequence"
        decoded = tokenizer.decode(codes.unsqueeze(0)).audio_values[0]
    return dict(codes=codes.tolist(), audio=decoded.tolist())


def reference_worker():
    import torch
    import transformers
    from qwen_tts.core.models import configuration_qwen3_tts as c, modeling_qwen3_tts as m
    from qwen_tts.core.tokenizer_12hz import configuration_qwen3_tts_tokenizer_v2 as tc
    from qwen_tts.core.tokenizer_12hz import modeling_qwen3_tts_tokenizer_v2 as tm
    assert transformers.__version__ == "4.57.3"
    torch.set_num_threads(1)
    t, tok = configurations(c, tc)
    talker = m.Qwen3TTSTalkerForConditionalGeneration(t).eval()
    predictor = m.Qwen3TTSTalkerCodePredictorModelForConditionalGeneration(t.code_predictor_config, t).eval()
    tokenizer = tm.Qwen3TTSTokenizerV2Model(tok).eval()
    models = dict(talker=talker, predictor=predictor, tokenizer=tokenizer)
    dest = Path(os.environ["TTS_REFERENCE_OUTPUT"])
    for name, model in models.items():
        deterministic_state(model)
        model.save_pretrained(dest / name)
    result = dict(source_revision=PIN, transformers=transformers.__version__, torch=torch.__version__,
                  direct=direct_comparison(talker, predictor), generation=generation_comparison(talker),
                  tokenizer=tokenizer_comparison(tokenizer))
    (dest / "reference.json").write_text(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--old-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEST)
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "-C", str(args.source), "rev-parse", "HEAD"], text=True).strip()
    assert revision == PIN
    archive = subprocess.check_output(["git", "-C", str(args.source), "archive", PIN, "qwen_tts"])
    with tempfile.TemporaryDirectory(prefix="tts-original-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive)) as files:
            files.extractall(tmp, filter="data")
        env = dict(os.environ, PYTHONPATH=tmp, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   PYTHONDONTWRITEBYTECODE="1", TTS_REFERENCE_OUTPUT=str(args.output.resolve()))
        subprocess.run([str(args.old_python), str(Path(__file__).resolve()), "--worker"], env=env, check=True)
    print(f"wrote original 4.57.3 checkpoints and references to {args.output}")


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["--worker"]:
        reference_worker()
    else:
        main()
