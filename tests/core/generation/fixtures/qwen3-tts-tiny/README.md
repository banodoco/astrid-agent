# Tiny original Qwen3-TTS checkpoint proof

These are generated test weights, not downloaded model weights. The fixture
script runs the unchanged node source at
`d8122a8ba835b65fd65c113d2b273b1ad1579293` under Transformers 4.57.3 / Torch
2.10.0 CPU. It shrinks the digest-verified real configs, assigns distinct
deterministic values to every state tensor, and saves through `save_pretrained`.
The checkpoint keys therefore retain the original 4.57.3 layout. Encoder and
decoder codebooks contain distinct nonzero vectors. Decoder inputs are the
encoder's actual codes, including their ordering; no codes are substituted.

Regenerate from the repository root (no network or model downloads):

```sh
python3 tests/core/generation/fixtures/generate_qwen3_tts_tiny_equivalence.py \
  --source /home/user/cells/src/ComfyUI-QwenTTS \
  --old-python /home/user/cells/venvs/tts-constructors-tf4573/bin/python
```

A second generation into an evidence directory reproduced all nine generated
files byte for byte (identical SHA256 values and file sets).

## Native Transformers 5.22.1 behavior

Source anchors refer to the installed Transformers package:

- `modeling_utils.py:3732-3762,4793-4797` constructs on meta and materializes
  nonpersistent buffers with `empty_like`. Its initializer recognizes
  `RotaryEmbedding` and copies config-derived values to both frequency buffers
  (`:2347-2358`). Native Qwen3 registers both buffers nonpersistently
  (`models/qwen3/modeling_qwen3.py:98-100`). The two TTS initializers now delegate
  unhandled modules; the tokenizer uses that class-name convention and registers
  `original_inv_freq` as a clone. The test accounts for every state tensor before
  checking buffers: the rename introduces no checkpoint key. Only encoder paths
  change through Transformers' native recursive Mimi conversion.
- `models/qwen3/modeling_qwen3.py:378-382` derives positions from cache length;
  `generation/utils.py:894-916,700-713` prepares padding-aware cumulative positions
  and slices to current inputs. Only remote code receives legacy `cache_position`
  (`:768-778`). The talker respects prepared positions; direct calls use cache
  length to select current positions while retaining left-padding offsets.
- Native Qwen3 calls `cache.update(key_states, value_states, layer_idx)`
  (`models/qwen3/modeling_qwen3.py:251-252`). Dynamic caches append tensors and
  static caches track their own position; rotary/cache-position metadata kwargs
  are not required. The earlier external migration note was corrected accordingly
  and its duplicated padding sentence removed.

## Focused results

Starting candidate: `f90ce03234e4f900b6ab3334a24712be55289f06`, clean tree,
`913c6b8f` confirmed as an ancestor.

Before the fixes, the new tests had **4 failures, 2 passes**: all three offline
loads failed config-derived frequency equality; real masked generation failed on
the first cached step with attention dimensions **9 versus 5**. The independent
strict-load talker/predictor comparison and tokenizer comparison passed.

After the fixes, the two focused Engine-deps files have **25 passes** on 5.22.1:

| Proof | Result | Absolute tolerance (relative tolerance 0) |
| --- | --- | --- |
| Offline original-layout checkpoint tensors | 37 talker + 18 predictor + 217 tokenizer, all exact; no missing/unexpected/mismatched/error/conversion keys | 0 |
| Rotary buffers | Both buffers in all five rotary modules equal config-derived frequencies; CPU, nonpersistent | 0 |
| Talker/predictor prefill + two cached steps vs 4.57.3 | Maximum absolute difference 0.0 | 0 |
| Real left-padded `.generate()` vs 4.57.3 | Identical sequences and scores; full-sequence logits also exact, maximum absolute difference 0.0 | 1e-6 |
| Cached vs full-sequence hidden states | Maximum absolute difference 2.9802322387695312e-08; cache lengths 4, 5, 6 | 1e-6 |
| Tokenizer encode/decode vs 4.57.3 | Identical codes; decoder maximum absolute difference 0.0 | 1e-6 |

The generation observer captures actual decoder embeddings assembled by the
public talker generation path. It recomputes the accumulated sequence without a
cache at every step, with the same mask and padding-aware positions. It compares
all valid queries at prefill and the current queries at cached steps. Both batch
rows are left-padded, by different amounts. No existing test was changed.

The patch content digests were updated in the recipe, and the dependent engine
digest regenerated with the repository tool:

```sh
/home/user/wt/tts3.env/venv/bin/python scripts/vibecomfy_pin.py \
  --source comfyui 6bfaacc67c2103481e5f0c84d75257cd0581d86a
```

A final rerun between successful `git diff --exit-code` calls made no further
tracked change. The lock and package versions are unchanged by this review card.

Focused Agent selectors on candidate and base `913c6b8f` both produced **77 passes,
1 failure**. The sole test failure is the preexisting Linux recipe assertion
requiring `--models-directory` and `{models_root}` in one argument; it was left
unchanged. The requested raw baseline-diff reports **0 regressions**, 1 no-baseline,
3 preexisting, 0 fixed, 0 missing, with 81 parsed entries per side (application log
lines contribute extra entries). A second comparison restricted to pytest result
lines has 78 entries per side: **0 regressions, 0 no-baseline, 1 preexisting,
0 fixed, 0 missing**. The temporary base worktree and environment were removed.

Raw evidence is in `/home/user/cells/evidence/`: `tts5-engine-before.log`,
`tts5-rope-after.log`, `tts5-engine-after.log`, `tts5-before.xml`, `tts5-after.xml`,
`tts5-fixture.log`, `tts5-fixture-reproduced.log`, `tts5-digests.log`,
`tts5-base-agent.log`, `tts5-candidate-agent.log`, `tts5-diff/summary.md`, and
`tts5-diff-filtered/summary.md`. The corrected migration note is
`tts3-transformers.md`. This proof covers tiny CPU models and local generated
checkpoints; no production model weights or GPU runs were used.
