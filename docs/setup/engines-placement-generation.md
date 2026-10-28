# Engine install, placement, and generation (canonical outline)

**Status: design outline for the proof run.** This page records the intended user-facing concepts and the boundaries that the eventual command reference must satisfy. The engine CLI, Linux recipe runner, RunPod endpoint, and H3 routed generation are not implemented in the frozen DOC-1 base. Do not read examples or acceptance goals in the spec as tested commands.

## User-visible states

- **Installed:** pinned source and environment steps completed, model bytes verified, and install receipt written. This state does not mean the engine is qualified for production generation.
- **Qualified:** a separately recorded check established the required H3 nodes in `object_info`, engine health, target identity, and the proof-specific validation. Qualification must not be inferred from install success.
- **Placed:** an engine endpoint has a placement and lifecycle owner. A RunPod endpoint requires authenticated HTTPS through the pod agent; a local endpoint uses loopback.
- **Generated:** a request completed through the shared generation facade with its output, manifest v2, and publish effect receipt.

These state labels should appear in status output and receipts only after the corresponding implementation and evidence exist.

## Source of truth and recipe

The contract defines `engine_contracts.py`, `engine_recipe.py`, `model_downloader.py`, the sole `engine_runner.py`, execution contexts, platform adapters, and `engines_cli.py`. A packaged recipe index resolves `(engine, platform)` to verified data and one canonical pins file. Recipe lookup is local, deterministic, read-only, and credential-free. It must not clone sources or contact a provider. A packaged model lock includes expected byte counts and SHA256 digests. A successful download result is emitted only after full-byte verification and atomic promotion.

The H3 proof recipe is `vibecomfy-h3` on Linux CUDA 13.0. The same pin set must feed H3 compile/generation helpers. CUDA 12.4/12.8 entries belong to legacy profiles and are not evidence of H3 compatibility. ComfyUI stays bound to `127.0.0.1` in either placement.

## Install and status command family

Planned interface from the spec: `astrid engines install <engine> --placement local|runpod` and `astrid engines status`. The documented dry-run acceptance shape in the plan is `astrid engines install vibecomfy-h3 --placement local --dry-run`. All are **pending A1–A5/A4 implementation** in this base. Do not advertise them as working, and do not invent volume, data-center, credential, or dry-run flags beyond a specifically implemented and RUNBOOK-recorded interface.

A future command reference should state:

1. Which target owns execution and the model directory.
2. Exact recipe and source pins, platform, and install root.
3. Lock/download verification and receipt location.
4. Start and health behavior, including loopback binding.
5. How a retry reports `skipped` versus `installed` steps.
6. What the command does not qualify.

## Placement and endpoint security

The intended minimal placement contract yields an endpoint plus credentials. Local placement starts or attaches a local engine at `http://127.0.0.1:<port>`. RunPod placement uses a per-claim bearer token and `https://<pod>-<port>.proxy.runpod.net` through the HTTPS pod agent. The ComfyUI port must not be directly exposed and the recipe must not open an unauthenticated proxy mapping.

For HTTPS-only environments, document only the supported HTTPS transport. The existing direct SSH path timed out in prior evidence, a prior CONNECT attempt was explicitly prohibited, and the complete product HTTPS round trip is not yet qualified in RUNBOOK. Do not suggest SSH, CONNECT, raw TCP, or a generic lifecycle `ship/fetch` command as a workaround.

## RunPod capacity and storage

The proof target is an RTX 5090 attached to the existing volume named `storage`; the H3 builder requires a supplied volume of at least 100 GB and does not resize it. EUR-NO-1 is the earlier owner-selected data center, not a live availability or price guarantee. Any future allocation step must use a fresh manager-approved capacity check and the documented canonical transport. Worker documentation must not create, resize, or claim provider resources absent explicit authorization.

Final cleanup evidence is per-resource provider GET 404 and, after all resources are released, an account inventory of `[]`. These are proof-run manager actions; they are not currently qualified commands in this user-facing outline.

## Canonical generation route

The existing route is the SDK `GenerationFacade` into `generation.generate_*`, a registered backend, `build_generation_manifest`, and the publish effect. Project C completes adoption incrementally; RunPod placement is not a backend merely because a pod exists. H3 retains prepare, compile, compose, verification, and pack receipts while delegating its child generation to the shared route (C5-lite). A routed operation must carry the selected placement and preserve the manifest/publish evidence.

The frozen base does not yet implement the proof's complete route. Until C5-lite and placement land, do not provide a successful H3 routed command or claim its output is canonical.

## Four-image H3 proof path

The final input contract is exactly `inputs/real/img1.png` through `img4.png`, in that order, and `prompt.txt` byte-for-byte as supplied; image purposes come from `NOTES.md`. Preserve and record SHA256 for each image and the prompt. Do not rewrite or regenerate the prompt. Run the H3 child via the canonical generation route at `placement=runpod` on an RTX 5090 with the existing `storage` volume. Capture the output, manifest v2, publish/receipt evidence, and `ffprobe` duration, resolution, and codec.

This is the intended Phase 5 route, not an executable interface in the DOC-1 base. Phase 4 fresh-host rehearsal must succeed using only final user documentation before the real job is authorized.

## Credential and host setup

Credentials are values stored outside source, recipes, profiles, and artifacts. `ASTRID_ENV_FILE` selects the private credentials file; documentation may mention the variable and credential names, never their values. Refer users to the existing RunPod credential guide for its hidden-prompt flow. Do not ask users to paste tokens into commands or logs.

On Linux without a working systemd user manager, the installer may start a detached Runtime without restart-on-crash or reboot behavior. Persistent hosts need the documented foreground process under an external supervisor. A stale Runtime owner is a custody condition: preserve the records and use an implemented recovery path; do not recommend manual deletion or a guessed reset command.

## Evidence required before promoting this outline to a procedure

- Named patch bases and ordered `APPLY.md` steps for the complete source tuple.
- RUNBOOK-passing engine CLI and source import from the intended checkout.
- Local and RunPod dry-run/install receipts, model SHA256s, H3 node keys, and health output.
- Qualified HTTPS exec/upload/download/release cycle and exact GET 404 cleanup evidence.
- Routed small workflow with output, manifest v2, publish effect, and release evidence.
- Fresh Phase 4 rehearsal with no manual fixes, followed by the exact four-image Phase 5 run.
