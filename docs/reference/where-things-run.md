# Where generation runs

Linux supports local generation through VibeComfy: run your own ComfyUI and
pass `comfy_url` to `vibecomfy.run`. H3 (`h3_av`) currently performs inference
only on RunPod workers. A locally running controller does not imply local
model execution.

**Managed setup** means Astrid supplies an engine environment or prepares a
worker. **Bring your own** means you supply the running engine, admitted
interpreter/session, or provider account. **Unsupported** refers to the named
execution route, not the operating system’s ability to run Astrid’s client.
Cloud API clients can run locally on all three OSes while inference stays
with their provider; running such a client on RunPod does not move inference
to RunPod.

The table records code routes, not hardware qualification. In particular,
Windows NVIDIA environments are candidates, and a bring-your-own native
session still requires a compatible engine/device. No macOS CUDA support is
implied. Each cell cites the setup, dispatch or provider code that defines it.
The scope is model generation and its wrappers, rather than media processing,
renderers (for example Blender), or LoRA training.

| Pack / route | Windows locally | Linux locally | macOS locally | RunPod |
| --- | --- | --- | --- | --- |
| `vibecomfy` [pack.yaml:2](../../astrid/packs/vibecomfy/pack.yaml#L2) — existing ComfyUI (`comfy_url`) | Bring your own running ComfyUI. [run.py:510](../../astrid/packs/vibecomfy/actions/run/run.py#L510) | Bring your own running ComfyUI; no managed installation. [run.py:510](../../astrid/packs/vibecomfy/actions/run/run.py#L510) [gpu_environment.py:134](../../astrid/core/generation/gpu_environment.py#L134) | Bring your own running ComfyUI; no managed installation. [run.py:510](../../astrid/packs/vibecomfy/actions/run/run.py#L510) [gpu_environment.py:134](../../astrid/core/generation/gpu_environment.py#L134) | Bring your own ComfyUI on the executor host; URL must be loopback. [vibecomfy_local_server.py:14](../../astrid/core/generation/backends/vibecomfy_local_server.py#L14) |
| `vibecomfy` [pack.yaml:2](../../astrid/packs/vibecomfy/pack.yaml#L2) — managed/runtime route | Managed Windows NVIDIA candidate environment (not device-qualified). [gpu_environment.py:181](../../astrid/core/generation/gpu_environment.py#L181) | No managed setup in this planner; use the bring-your-own URL route above. [gpu_environment.py:134](../../astrid/core/generation/gpu_environment.py#L134) [run.py:510](../../astrid/packs/vibecomfy/actions/run/run.py#L510) | No managed setup in this planner; use the bring-your-own URL route above. [gpu_environment.py:134](../../astrid/core/generation/gpu_environment.py#L134) [run.py:510](../../astrid/packs/vibecomfy/actions/run/run.py#L510) | Managed/prepared worker route: H3 sends its VibeComfy generation child to the selected RunPod worker. [run.py:1464](../../astrid/packs/h3_av/actions/transform/run.py#L1464) [run.py:1331](../../astrid/packs/h3_av/actions/transform/run.py#L1331) |
| `wan2gp` [pack.yaml:2](../../astrid/packs/wan2gp/pack.yaml#L2) — native session | Managed Windows NVIDIA candidate environment; host must admit the native session. [gpu_environment.py:181](../../astrid/core/generation/gpu_environment.py#L181) [driver.py:10](../../astrid/packs/wan2gp/src/driver.py#L10) | Bring your own admitted checkout/interpreter; no managed environment. Engine/device compatibility is required. [driver.py:10](../../astrid/packs/wan2gp/src/driver.py#L10) [gpu_environment.py:134](../../astrid/core/generation/gpu_environment.py#L134) | Bring your own admitted checkout/interpreter; no managed environment. Engine/device compatibility is required. [driver.py:10](../../astrid/packs/wan2gp/src/driver.py#L10) [gpu_environment.py:134](../../astrid/core/generation/gpu_environment.py#L134) | Bring your own admitted host session; this pack has no RunPod setup action. [driver.py:10](../../astrid/packs/wan2gp/src/driver.py#L10) [pack.yaml:151](../../astrid/packs/wan2gp/pack.yaml#L151) |
| `h3_av` [pack.yaml:2](../../astrid/packs/h3_av/pack.yaml#L2) — H3 transform inference | Unsupported locally; select a RunPod worker. [run.py:1283](../../astrid/packs/h3_av/actions/transform/run.py#L1283) | Unsupported locally; select a RunPod worker. [run.py:1283](../../astrid/packs/h3_av/actions/transform/run.py#L1283) | Unsupported locally; select a RunPod worker. [run.py:1283](../../astrid/packs/h3_av/actions/transform/run.py#L1283) | RunPod only: explicit worker target, qualified worker receipt, or prepared worker selection. [run.py:1283](../../astrid/packs/h3_av/actions/transform/run.py#L1283) [run.py:1331](../../astrid/packs/h3_av/actions/transform/run.py#L1331) |
| `comfy_wrap` [pack.yaml:2](../../astrid/packs/comfy_wrap/pack.yaml#L2) — workflow wrapper | Bring your own VibeComfy runtime; calls `run_sync`, with no `comfy_url` input. [run.py:135](../../astrid/packs/comfy_wrap/actions/run/run.py#L135) | Bring your own VibeComfy runtime; calls `run_sync`, with no `comfy_url` input. [run.py:135](../../astrid/packs/comfy_wrap/actions/run/run.py#L135) | Bring your own VibeComfy runtime; calls `run_sync`, with no `comfy_url` input. [run.py:135](../../astrid/packs/comfy_wrap/actions/run/run.py#L135) | Bring your own worker runtime; same `run_sync` call, no pack-owned RunPod setup. [run.py:135](../../astrid/packs/comfy_wrap/actions/run/run.py#L135) |
| `generation` [pack.yaml:2](../../astrid/packs/generation/pack.yaml#L2) — local image/video/audio backend | Bring your own admitted VibeComfy runtime; managed NVIDIA environment planning is available. [vibecomfy.py:59](../../astrid/core/generation/backends/vibecomfy.py#L59) [gpu_environment.py:181](../../astrid/core/generation/gpu_environment.py#L181) | Bring your own VibeComfy runtime; this facade selects embedded/checkout profiles, not the direct URL route. [vibecomfy.py:59](../../astrid/core/generation/backends/vibecomfy.py#L59) | Bring your own VibeComfy runtime; this facade selects embedded/checkout profiles, not the direct URL route. [vibecomfy.py:59](../../astrid/core/generation/backends/vibecomfy.py#L59) | Bring your own worker runtime; backend selection remains VibeComfy. [vibecomfy.py:59](../../astrid/core/generation/backends/vibecomfy.py#L59) |
| `generation` [pack.yaml:2](../../astrid/packs/generation/pack.yaml#L2) — cloud fal backend | Bring your own fal credential; inference is on fal. [pack.yaml:20](../../astrid/packs/generation/pack.yaml#L20) | Bring your own fal credential; inference is on fal. [pack.yaml:20](../../astrid/packs/generation/pack.yaml#L20) | Bring your own fal credential; inference is on fal. [pack.yaml:20](../../astrid/packs/generation/pack.yaml#L20) | Unsupported as RunPod inference: this route submits to fal. [pack.yaml:20](../../astrid/packs/generation/pack.yaml#L20) |
| `generation` [pack.yaml:2](../../astrid/packs/generation/pack.yaml#L2) — OpenAI image route | Bring your own OpenAI credential; inference is on OpenAI. [pack.yaml:1068](../../astrid/packs/generation/pack.yaml#L1068) | Bring your own OpenAI credential; inference is on OpenAI. [pack.yaml:1068](../../astrid/packs/generation/pack.yaml#L1068) | Bring your own OpenAI credential; inference is on OpenAI. [pack.yaml:1068](../../astrid/packs/generation/pack.yaml#L1068) | Unsupported as RunPod inference: this route calls OpenAI. [pack.yaml:1068](../../astrid/packs/generation/pack.yaml#L1068) |
| `generation` [pack.yaml:2](../../astrid/packs/generation/pack.yaml#L2) — speech (`edge-tts`) | Bring your own `edge-tts` dependency; synthesis uses Microsoft’s service. [pack.yaml:1571](../../astrid/packs/generation/pack.yaml#L1571) | Bring your own `edge-tts` dependency; synthesis uses Microsoft’s service. [pack.yaml:1571](../../astrid/packs/generation/pack.yaml#L1571) | Bring your own `edge-tts` dependency; synthesis uses Microsoft’s service. [pack.yaml:1571](../../astrid/packs/generation/pack.yaml#L1571) | Unsupported as RunPod inference: synthesis uses Microsoft’s service. [pack.yaml:1571](../../astrid/packs/generation/pack.yaml#L1571) |
| `fal` [pack.yaml:2](../../astrid/packs/fal/pack.yaml#L2) — MiniMax H3 video / Hunyuan Foley | Bring your own `FAL_KEY`; inference is on fal.ai. [run.py:278](../../astrid/packs/fal/actions/h3_video/run.py#L278) | Bring your own `FAL_KEY`; inference is on fal.ai. [run.py:278](../../astrid/packs/fal/actions/h3_video/run.py#L278) | Bring your own `FAL_KEY`; inference is on fal.ai. [run.py:278](../../astrid/packs/fal/actions/h3_video/run.py#L278) | Unsupported as RunPod inference: the provider is fal.ai. [run.py:278](../../astrid/packs/fal/actions/h3_video/run.py#L278) |
| `seedance_local` [pack.yaml:2](../../astrid/packs/seedance_local/pack.yaml#L2) — reference video | Bring your own `FAL_KEY`; inference is on fal.ai despite the pack name. [run.py:217](../../astrid/packs/seedance_local/actions/reference_video/run.py#L217) | Bring your own `FAL_KEY`; inference is on fal.ai despite the pack name. [run.py:217](../../astrid/packs/seedance_local/actions/reference_video/run.py#L217) | Bring your own `FAL_KEY`; inference is on fal.ai despite the pack name. [run.py:217](../../astrid/packs/seedance_local/actions/reference_video/run.py#L217) | Unsupported as RunPod inference: the provider is fal.ai. [run.py:217](../../astrid/packs/seedance_local/actions/reference_video/run.py#L217) |
| `foley` [pack.yaml:2](../../astrid/packs/foley/pack.yaml#L2) — spatial soundscape orchestration | Bring your own provider credentials; delegates synthesis to `fal.fal_foley`. [pack.yaml:19](../../astrid/packs/foley/pack.yaml#L19) | Bring your own provider credentials; delegates synthesis to `fal.fal_foley`. [pack.yaml:19](../../astrid/packs/foley/pack.yaml#L19) | Bring your own provider credentials; delegates synthesis to `fal.fal_foley`. [pack.yaml:19](../../astrid/packs/foley/pack.yaml#L19) | Unsupported as RunPod inference: generation delegates to fal. [pack.yaml:19](../../astrid/packs/foley/pack.yaml#L19) |
| `discord_local` [pack.yaml:2](../../astrid/packs/discord_local/pack.yaml#L2) — personal browser adapter | Bring your own local browser, signed-in Discord account and generation bot. [pack.yaml:28](../../astrid/packs/discord_local/pack.yaml#L28) | Bring your own local browser, signed-in Discord account and generation bot. [pack.yaml:28](../../astrid/packs/discord_local/pack.yaml#L28) | Bring your own local browser, signed-in Discord account and generation bot. [pack.yaml:28](../../astrid/packs/discord_local/pack.yaml#L28) | Unsupported as a declared RunPod inference route: uses a local headed browser and a Discord bot. [pack.yaml:24](../../astrid/packs/discord_local/pack.yaml#L24) |

## Connect your own ComfyUI

Find running local servers with this read-only discovery helper:

```sh
python3 -m astrid.packs.vibecomfy.local scan
```

Pass the discovered loopback URL as `comfy_url` to `vibecomfy.run`. Astrid
attaches to that ComfyUI; it does not install, start or restart it. See the
[VibeComfy connection flow](../../astrid/packs/vibecomfy/docs/SKILL.md#run-on-your-own-comfyui).
This direct URL route is distinct from the embedded/checkout runtimes used
by `comfy_wrap` and the `generation` facade.

## Select H3 execution deliberately

`h3_av.transform` runs its orchestrator locally and assigns generation to a
RunPod worker. Supply an explicit RunPod worker execution request, a
`--worker-qualification` receipt, or a `--prepared-worker` selection.
`--require-worker-qualification` additionally requires a qualification receipt
or prepared selection. A dry run may describe stages without selecting a
worker, but an explicit local target is rejected even in a dry run.

The fal pack’s MiniMax H3 endpoint is a separate provider route; it does not
make `h3_av` available for local inference.
