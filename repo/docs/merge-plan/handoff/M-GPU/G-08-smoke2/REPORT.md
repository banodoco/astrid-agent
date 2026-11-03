# 034 — Smoke 2 runner repair

## Changed files

The packet copy is under `/home/user/astrid/proof-codex-gen/work/034/review/G-08/candidate-smoke/`. The deliverable patch is `/home/user/astrid/proof-codex-gen/patches/034_SMOKE2_RUNNER_FIX.patch`.

- `review/G-08/candidate-smoke/run.sh`: enforces the managed pre-install refusal, then the supported engine install dry-run and seven-step/three-model plan, actual supported install, status polling, inventory resolution, generation receipt, and release assertion boundary. CPU rehearsal uses an isolated fake CLI for the full sequence after recipe-stub rejection.
- `README.md`, `assertions.md`, `expected-receipts.json`: document the ordered assertions and CPU/provider evidence boundary.
- `tuple.txt`: records provenance in the existing comment; the one recipe slot remains unchanged.

No product source, pod job, installer, model, credential, or upstream repository was changed.

## Exact pins and recipe input

- Agent case tuple: `61e9e2f63f5eb7bb3d48666bd036197d9d65ec50`
- Runtime: `ecc9f1533234d33bfc082b34e1dc4e18a22e3f3d`
- VibeComfy source pin: `3bebae39655eb460454885040be4d3aefaf2ef31`, bound by `lane/M-GPU/p1-vibecomfy-pin-bind` at Agent commit `264772b364504f792f14e16acf1cd5f1a7de88d0`
- Lifecycle: `96360a2`
- Existing sole recipe slot: `/home/user/astrid/ws-P/work/47/agent/astrid/packs/comfyui/recipe.linux-cu130.json|9898193fe3a44204cfe0345df40fcf54ab776f4a18947dde8b92fb1014c4d791` (P-47 plain packaged ComfyUI recipe)

The recipe raw-file SHA was rechecked against the named path. Install argv follows the existing P-15/PODS supported form, with the documented `--dry-run` option appended; no new CLI surface was introduced. The expected plan shape (7 steps, 3 model steps) is the P-47 packaged plan.

## Commands and results

All commands below ran from `/home/user/astrid/proof-codex-gen/work/034/review/G-08/candidate-smoke`, except the diff check, which ran in a temporary task-local Git validation directory.

- `python3 run.sh --cpu-rehearsal --tuple tuple.txt` — exit 0. Final output reports pre-install exit `2`, code `COMFY_TARGET_NOT_READY`, field `target`; install plan 7 steps / 3 model steps; fake call order `managed-dry-run`, `engine-install-dry-run`, `engine-install`, `readiness-poll`, `inventory`, `generation`, `release-evidence-fake`. Full output: `queue/running/034_SMOKE2_RUNNER_FIX/cpu-rehearsal.log`.
- `python3 -m py_compile run.sh` — exit 0, no output. Log: `queue/running/034_SMOKE2_RUNNER_FIX/py-compile.log`.
- `python3 run.sh --assert-release /home/user/astrid/proof-codex-gen/work/034/valid-release-fixture.json` — exit 0, `astrid.smoke2.release-assertion.v1` passed. Log: `queue/running/034_SMOKE2_RUNNER_FIX/release-assertion.log`.
- `git apply --check patches/034_SMOKE2_RUNNER_FIX.patch` — passed against a temporary Git repository initialized from the dispatch packet.
- `git diff --check` — passed against the applied packet changes in that repository.

The CPU command was first invoked during editing with a temporary, incorrect tuple value (`264772…` in the source-pin field). After correcting the tuple to the VibeComfy source revision (`3bebae…`) and recording the lane commit as its provenance, the exact CPU command was run again and passed as reported above. The earlier result is not treated as final evidence.

The release assertion fixture contained `session_stop=stopped`, `provider_release=verified`, and `provider_readback=released`, plus task-local evidence labels. It was deleted after the check. These values exercise the existing normalized assertion only; they are not provider receipts.

## What rehearsal establishes

The rehearsal confirms recipe digest/stub rejection checks remain active, validates both dry-run gates and plan shape, and exercises the runner's order through controlled install, readiness, inventory, generation receipt, and release-assertion responses. It also exercises capture of stdout, stderr, argv/exit code, progress, timings, model-root placement checks, and PNG header/dimensions/result checks.

It does **not** prove model installation or verification, GPU generation, genuine PNG rendering, actual managed readiness, lifecycle release, provider release/readback, or provider cleanup. The fake readiness is a CLI response; fake generation writes a minimal header/dimension fixture; fake release evidence does not represent provider evidence. In a real batch, the provider lifecycle releases the job after the runner exits; its owned-session receipt and independent provider readback must be retained and then passed to `--assert-release`. No pod acceptance was performed.

## Cleanup and retained outputs

The CPU fake CLI, rehearsal sandbox, and temporary release fixture were in `TemporaryDirectory`/task work paths and were removed. The Python `__pycache__` and temporary Git validation directory were removed. Only the isolated packet copy, final patch, this report, and the three small command logs remain for this task. Available filesystem space was 2.4 GB at final inspection; no further scratch data was created after that reading.
