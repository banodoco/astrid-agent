---
name: training
description: >
  Training pack — build video training datasets and run LoRA training
  end to end: clip pools, captioning/review, RunPod-backed trainer
  execution, and checkpoint registration.  Drives the runpod pack
  under the hood.
---

# Training

The training pack assembles video training datasets and runs LoRA training jobs.
It builds clip pools, merges them, searches Hugging Face for LoRAs, and
orchestrates end-to-end training runs on RunPod GPUs. Media inputs must first
be admitted by the runtime; this pack does not own a URL or filesystem cache.

## Actions

| Action | What it does |
|---|---|
| `training.pool_build` | Build the candidate clip pool from triaged source-video scenes. Pipeline step 7. |
| `training.pool_merge` | Merge multiple candidate clip pools into a unified pool for arrangement. Pipeline step 8. |
| `training.search_loras` | Search Hugging Face Hub for LoRAs associated with a base model. |

The former executor/orchestrator split is represented by the unified v3 action
surface. The stable IDs and behavior are unchanged.

| Action | What it does |

| Orchestrator | What it does |
|---|---|
| `training.dataset_build` | Build a generic reviewed video training dataset from configured sources. |
| `training.training_run` | Run a generic LoRA training job from a prepared dataset manifest — provisions RunPod GPUs, executes the trainer remotely, pulls checkpoints, and registers artifacts. |

## When to use

- Use `training.dataset_build` to assemble a complete video training dataset
  (acquire, split, caption, review).
- Use `training.training_run` to run a LoRA training job end to end.
- Use `training.pool_build` and `training.pool_merge` as part of the editorial
  pipeline (steps 7–8).
- Use `training.search_loras` to discover LoRAs on Hugging Face.

## When NOT to use

- Do not use for raw GPU provisioning or one-off remote script execution — use
  the `runpod` pack for bare pod lifecycle control.

## Credentials

| Env var | Used by |
|---|---|
| `RUNPOD_API_KEY` | `training.training_run` (GPU provisioning) |
| `HF_TOKEN` | search_loras (Hugging Face Hub API, optional) |

## Quick-start

```python
# Build a clip pool (pipeline step 7)
import astrid.sdk as sdk
result = sdk.invoke(
    "training.pool_build",
    inputs={"scenes": "./out/scenes.json", "triage": "./out/scene_triage.json"},
    out="./out",
)

# Merge pools (pipeline step 8)
result = sdk.invoke(
    "training.pool_merge",
    inputs={"pools": ["./pool1.json", "./pool2.json"]},
    out="./out/unified_pool.json",
)

# Search Hugging Face for LoRAs
result = sdk.invoke(
    "training.search_loras",
    inputs={"base_model": "black-forest-labs/FLUX.1-dev"},
    out="./loras.json",
)

# Build a training dataset
result = sdk.invoke(
    "training.dataset_build",
    inputs={"sources": "./sources.json"},
    out="./dataset",
)

# Run a LoRA training job
result = sdk.invoke(
    "training.training_run",
    inputs={"dataset": "./dataset/manifest.json"},
    out="./training_output",
)
```
