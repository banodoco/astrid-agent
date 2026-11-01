---
name: wan2gp
description: >
  Native Wan2GP pack — generate video through the GenericPackHost-owned
  upstream Python session (shared.api.init / WanGPSession.submit_task). The
  selected upstream candidate is deepbeepmeep/Wan2GP@f3f204e5.
---

# Wan2GP

Native Wan2GP is the **local** video-generation engine for Astrid. Astrid owns
the typed capability, settings compiler, thin host adapter, and lifecycle
mapping; the upstream checkout and installation are explicit host admission
inputs, not Worker-relative discovery. This pack does not claim native model
residency, warm reuse, deployment readiness, or real engine/GPU qualification.

## Engine seam

```
shared.api.init(root=..., output_dir=...) -> WanGPSession
WanGPSession.submit_task(settings: dict) -> SessionJob
SessionJob.result(timeout=...) -> GenerationResult
```

- `root` is the Wan2GP checkout root (contains `wgp.py`, `shared/`, `wgp_config.json`).
- `output_dir` is the per-runner private spool (attempt-scoped, not shared).
- `settings` is the compiled Wan2GP settings dict (deterministic compiler in `astrid/packs/wan2gp/src/compiler.py`).
- Cancellation is cooperative via `SessionJob.cancel()` / `WanGPSession.cancel()`.

## Executors

| Executor | What it does |
|---|---|
| `wan2gp.generate_video` | Typed host-mediated generation: compiles inputs, submits one native job through the retained host child, and returns structured terminal evidence. |
| `wan2gp.validate_settings` | Validates and compiles inputs without executing the engine. |

## Session lifecycle

GenericPackHost owns native session initialization, job submission, output
custody, cooperative cancellation, settlement, and release. The direct-run
guard never performs per-attempt `init()`/`close()` ownership, and the pack does
not create a second session manager. F05's synthetic host fixture proves the
route and fail-closed boundaries only; it does not prove warm reuse or model
residency.

## Inputs (portable vs machine-local)

Portable (part of capability digest): `prompt`, `negative_prompt`, `model`, `resolution`, `num_frames`/`video_length`, `fps`/`force_fps`, `seed`, `guidance_scale`, `loras`, etc.

Machine-local (excluded from digest, part of reuse fingerprint): `wan2gp_path`, `attempt_root`, `device`, `output_dir`.

## Quick-start

```python
import astrid.sdk as sdk

# Validate without running
sdk.invoke("wan2gp.validate_settings", kind="executor", inputs={"prompt": "a cat", "model": "wan-2.2"})

# Host-mediated generation (requires explicit Wan source/config/interpreter admission)
sdk.invoke("wan2gp.generate_video", kind="executor", inputs={"prompt": "a cat", "model": "wan-2.2", "resolution": "1280x720"})
```

## Windows with NVIDIA

Wan2GP on Windows with NVIDIA is untested, but runs by default. Please tell us
how it went, including the GPU model and driver from `nvidia-smi`. Wan2GP still
needs a detected NVIDIA GPU and working driver.

The candidate Python/CUDA pins are in `environment.windows-nvidia.json` and
`requirements.windows-nvidia.txt`. Provision the environment from the clean,
already admitted upstream checkout with the shared pack provisioner:

```powershell
python3 -m astrid.core.generation.gpu_environment wan2gp --checkout C:\Astrid\Wan2GP --plan
python3 -m astrid.core.generation.gpu_environment wan2gp --checkout C:\Astrid\Wan2GP
```

This needs user-local uv 0.11.32 and a network connection on the tester's
computer. It creates a uv-managed Python 3.12.10 environment under
`%LOCALAPPDATA%\Astrid GPU\wan2gp\<identity>`, without administrator access or a system
CUDA toolkit. It combines the CUDA wheel pins with the checkout's own
`requirements.txt`, resolves with the recorded upload cutoff, writes a fully
versioned, hash-bound `requirements.lock`, syncs that lock, checks dependencies
and probes `torch.cuda`. Keep the lock and `astrid-gpu-environment.json` with
test evidence; replay that lock with `uv pip sync --require-hashes` to
reproduce the resolved environment, including its exact PyTorch wheel URLs. Resolution or CUDA
probe failures leave no success receipt and do not qualify the installation.

Use the printed `python` path in the existing `wan_session` admission, alongside
the source/config digests and owner directory. Provisioning does not create a
config, download models, start the engine, or replace session admission. Use a
new `--destination` for repairs so a warm interpreter is never overwritten.
macOS and Linux retain their existing environment/admission path.
