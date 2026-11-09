# training.training_run

Generic built-in LoRA training orchestration. The action consumes a
training-run config and a prepared dataset manifest, then coordinates compute,
remote trainer execution, artifact pulls, checkpoint review, registration, and
teardown through registry-declared backends and actions.

This stage intentionally keeps provider-specific work behind backends and
action dependencies. Generic code must not call RunPod helper functions
directly.

## Dry Run

Use dry-run before any live operation. It validates the config, reports missing
declared secrets, writes the normalized manifest, builds the ai-toolkit config,
writes `planned_cost.json`, and persists local planning state. It performs no
RunPod, network, or GPU calls.

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "training.training_run",
    kind="action",
    project="demo",
    orchestrator_args=("--config", "configs/training-run.json"),
    dry_run=True,
)
```

## Smoke

Smoke mode performs the same local-only artifact generation as dry-run, with a
state mode that distinguishes CI/smoke validation from an operator dry-run.

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "training.training_run",
    kind="action",
    project="demo",
    orchestrator_args=("--config", "configs/training-run.json", "--smoke"),
)
```

Equivalent SDK call:

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "training.training_run",
    kind="action",
    project="demo",
    orchestrator_args=('--config', 'configs/training-run.json', '--dry-run'),
)
```

## Seinfeld Example

`examples/configs/training/seinfeld-training.yaml` carries the current Seinfeld
LoRA defaults as config for `training.training_run`. Its wired vocabulary lives
at `docs/examples/seinfeld/vocabulary.yaml`; active runs should use this built-in
action and explicit example config.

## Live Run

Live mode fails closed before provisioning if any environment variable declared
in `secrets.required_env` is missing. Use `--confirm-spend` only after reviewing
dry-run output and spend limits in the config. After successful training and
local sample download, live mode pauses at the checkpoint review gate and keeps
the pod teardown guard in state for resume or explicit follow-up.

```python
# requires RUNPOD_API_KEY, HF_TOKEN
import astrid.sdk as sdk
result = sdk.invoke(
    "training.training_run",
        kind="action", project="demo",
    orchestrator_args=("--config", "configs/training-run.json", "--confirm-spend"),
)
```

## Resume

Resume uses the training run directory's persisted state and continues from a
paused checkpoint-review gate. It validates `--pick` against
`checkpoint_manifest.json`, registers the selected checkpoint locally before
teardown, and records the final registration metadata. Use `--skip-teardown`
only when you intentionally want to keep the pod alive after registration.

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "training.training_run",
    kind="action",
    project="demo",
    orchestrator_args=('resume', '--out', 'runs/training/my-run', '--pick', 'final', '--notes', 'best checkpoint'),
)
```

## Outputs

- `last_run.json`: run state and resumability metadata.
- `manifests/ai-toolkit-ltx/manifest.json`: normalized training-owned manifest.
- `trainer/ai-toolkit-ltx/config.yaml`: generated ai-toolkit trainer config.
- `planned_cost.json`: local estimated-cost and capability planning output.
- `review/index.html`: local review index for downloaded sample assets.
- `registered/registered_lora.json`: metadata for the selected registered checkpoint.
