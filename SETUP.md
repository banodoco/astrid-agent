# Setup

## Linux (GPU pod)

Run once per pod. The pod builds everything itself. No secrets are needed for
these steps. Upload the five source trees (for example `git archive` of each
head) so that these directories exist under the current directory:
`src/runpod-python src/astrid-runtime src/runpod-lifecycle src/astrid-agent src/VibeComfy`.

Job budget: every job start and execute is one agent job record (cap 128); poll with `job poll` (GET state, no record).

1. Bootstrap. The script is idempotent and writes
   `/workspace/astrid/bootstrap/<digest>/ok` on success:

   ```bash
   bash src/astrid-agent/scripts/pod_bootstrap.sh
   ```

   It runs these commands:

   ```bash
   uv python install 3.12.10; uv venv /opt/astrid
   SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RUNPOD=1.12.0+astrid.2 uv pip install --python /opt/astrid \
     src/runpod-python src/astrid-runtime src/runpod-lifecycle src/astrid-agent src/VibeComfy
   uv pip install --python /opt/astrid pip
   uv pip install --python /opt/astrid --no-deps -e src/VibeComfy
   ```

   Also: it checks that the installed astrid engine modules match `src/astrid-agent`
   and reinstalls astrid only when they differ. It then writes the source profile and
   creates the workspace. It never starts a Runtime (`astrid-runtime up`).

   On a rerun, once the engine venv exists, the bootstrap first runs
   `scripts/gpu_sanity.py --venv <engine root>/runtime/venv` (bf16/fp16/fp32 GEMM under the
   engine's own child env; exit 2 on failure, prints the libcublas paths). On a fresh claim
   the venv is absent, so it is skipped; it runs inside the engine install (hook below) and on the next rerun.

2. Install the engine:

   ```bash
   R=/workspace/astrid/engines/vibecomfy-h3
   # sanity hook: bf16/fp16/fp32 GEMM after the environment steps, before the ~53 GB model downloads
   export ASTRID_ENGINE_SANITY_CMD="/opt/astrid/bin/python $PWD/src/astrid-agent/scripts/gpu_sanity.py"
   astrid engines status  vibecomfy-h3 --placement local --root $R || \
   astrid engines install vibecomfy-h3 --placement local --root $R --deadline 5400
   ```

   A failed install can leave engine state under `$R/.astrid-engine/` (an environment
   directory, a session record, and possibly a supervisor process). The bootstrap does not
   touch that state. Before a retry, run `astrid engines status` and follow the recovery
   path in `docs/setup/engines-placement-generation.md`. Do not delete `.astrid-engine` by hand.

3. Check the host. Runtime and pack host come up; create the project once:

   ```bash
   astrid doctor --json
   ```

4. Run the 1 s smoke first:

   ```bash
   python -m astrid.packs.h3_av.actions.transform.run --placement local --engine-root $R \
     --request job/s0/request.json --asset-map job/s0/assets.json --out /workspace/astrid/runs/<id>
   ```

   The clip follows with the unchanged `job/request.json` and a fresh `--out`.

## One-command pod session (Linux GPU and RunPod)

There is no one-command entry point in this patch set yet. The POD-RUN entry
point is not part of it, so the manager runs the steps below in order. When
POD-RUN lands it replaces these steps; the flags here stay the contract.

This section sets the engine root to `/root/astrid/engines/vibecomfy-h3` (container
disk). Do not use `/workspace` for the engine root; the network volume is only for
the bootstrap marker, the workspace, and the backup volume. Where step 2 of the Linux
section shows `/workspace/astrid/engines/vibecomfy-h3`, use the root below instead.

Fixed parameters for every step:

- GPU: `NVIDIA GeForce RTX 5090`, datacenter `EU-RO-1`. The datacenter comes from the
  backup volume; the claim has no datacenter flag.
- Storage: the existing network volume `backup` (10 GB). The claim echoes its size
  unchanged; it is not a flag.
- Container disk: 120 GB (`--container-disk-gb 120`).
- CUDA: `--allowed-cuda-versions 13.0,13.1,13.2,13.3`. The filter is an exact match, so pass the full list.
- Engine root: `R=/root/astrid/engines/vibecomfy-h3`.
- `PATH=/opt/astrid/bin` comes first in every shell that runs `astrid`.

Steps:

1. Claim (on the manager host). With `--transport https`, each claim writes its own
   token file beside the claim handle (`<handle>.<uuid>.token`). A claim never reuses
   another claim's token. Do not print the token or copy it into logs or docs.

   ```bash
   python3 src/astrid-agent/scripts/claim_runpod_5090_backup.py \
     --transport https \
     --gpu-type "NVIDIA GeForce RTX 5090" \
     --storage-name backup \
     --container-disk-gb 120 \
     --allowed-cuda-versions 13.0,13.1,13.2,13.3
   ```

2. Bundle. Upload the five source trees (`git archive` of each head) so that
   `src/runpod-python src/astrid-runtime src/runpod-lifecycle src/astrid-agent src/VibeComfy`
   all exist in the pod's working directory. The bootstrap stops on the first missing one.

3. P1 bootstrap. Idempotent; writes `/workspace/astrid/bootstrap/<digest>/ok` on success:

   ```bash
   export ASTRID_ENGINE_ROOT=/root/astrid/engines/vibecomfy-h3
   bash src/astrid-agent/scripts/pod_bootstrap.sh
   ```

4. Install the engine with the sanity hook. The hook runs bf16/fp16/fp32 GEMM after the
   environment steps and before the model downloads, and aborts the install if it fails:

   ```bash
   export PATH=/opt/astrid/bin:$PATH
   export ASTRID_ENGINE_SANITY_CMD="/opt/astrid/bin/python $PWD/src/astrid-agent/scripts/gpu_sanity.py"
   R=/root/astrid/engines/vibecomfy-h3
   astrid engines status  vibecomfy-h3 --placement local --root $R || \
   astrid engines install vibecomfy-h3 --placement local --root $R --deadline 5400
   ```

5. Rerun the bootstrap in the same shell environment as step 3, with `PATH` set as in step 4.
   Now that the engine venv exists, the rerun runs `gpu_sanity.py` against it (exit 2 on
   failure), checks that the installed astrid matches `src/astrid-agent`, and rewrites the
   source profile if needed. Then check the host:

   ```bash
   bash src/astrid-agent/scripts/pod_bootstrap.sh
   astrid doctor --json
   ```

6. S0, the 1 s smoke, with the commands in step 4 of the Linux section (use `$R` as above).

7. CLIP: the clip run with the unchanged `job/request.json` and a fresh `--out`, as in
   step 4 of the Linux section.

8. Download and verify the outputs. Do not release the pod until the check passes.

9. Release the exact claimed pod. Resume an interrupted release with `--resume`:

   ```bash
   python3 src/astrid-agent/scripts/release_prepared_runpod_worker.py \
     --selection <selection.json> --task-id <settled task id>
   ```

   Verify the release in two parts: a GET for the pod returns 404, and the account pod
   list is empty.

Job budget: every job start and execute is one agent job record, and each claim has a cap of
128 records. Poll state with `runpod-lifecycle job poll <pod id> <job id>`. Polls are reads and
use no record, so never poll with `job start`.

Regression guards (run these after any change to this section or to the bootstrap):

- `tests/scripts/test_pod_bootstrap.py`: idempotent rerun, sanity hook, source profile, pip.
- `tests/scripts/test_gpu_sanity.py`: exit codes and the printed libcublas paths.
- `tests/core/generation/test_engine_child_env.py`: `LD_LIBRARY_PATH` is rebuilt and `/usr/local/cuda*` is dropped.
- `tests/core/generation/test_engine_runner.py` and `tests/core/generation/test_engine_local_model_root.py`: install receipts and the local model root.

### Troubleshooting

Each entry gives the symptom, the cause, and the fix.

- **Claim never allocates on a host with CUDA 13.1, 13.2 or 13.3.**
  Cause: the CUDA filter is an exact match, so `--allowed-cuda-versions 13.0` alone excludes them.
  Fix: pass the full list, `--allowed-cuda-versions 13.0,13.1,13.2,13.3`.

- **Engine install fails with `unsafe_path` on a root under `/workspace`.**
  Cause: the install root is on the network volume.
  Fix: use a local root, `--root /root/astrid/engines/vibecomfy-h3`, and export
  `ASTRID_ENGINE_ROOT` to the same path before rerunning the bootstrap.

- **Engine install fails with `integrity_failed` on the torch `direct_url` receipt.**
  Cause: the torch install in the engine venv does not match the receipt the probe checks
  (FIX-RECEIPT-PROBE).
  Fix: reinstall torch into the engine venv from the pinned set, then rerun step 4.

- **Install fails with `command_failed` at `environment:comfy`, and the venv has no pip.**
  Cause: `uv venv` makes a venv without pip, and the engine helper runs `python3 -m pip`.
  Fix: the bootstrap now installs pip into `/opt/astrid`. Rerun step 3, then step 4.

- **`CUBLAS_STATUS_NOT_INITIALIZED` or `INVALID_VALUE` in GEMM.**
  Cause: `LD_LIBRARY_PATH` shadowing. A system CUDA under `/usr/local/cuda*` supplies a
  different libcublas than the engine venv expects.
  Fix: do not export `LD_LIBRARY_PATH` in the session (`unset LD_LIBRARY_PATH`). The engine
  child env rebuilds it and drops `/usr/local/cuda*`. Check the libcublas paths that
  `gpu_sanity.py` prints.

- **`engine_not_installed` after an earlier run.**
  Cause: a ComfyUI process from the earlier run is still alive, so the engine is not ready.
  Fix: stop the leftover ComfyUI process, or rerun the install command in step 4. The
  `engines status || engines install` pair re-validates the engine. Do not delete
  `$R/.astrid-engine` by hand; follow `docs/setup/engines-placement-generation.md`.

- **HTTP 429 `job limit reached; rotate claim`.**
  Cause: the claim has used all 128 job records. Polls that use `job start` or `job execute`
  spend records; polls that use `job poll` do not.
  Fix: poll only with `runpod-lifecycle job poll`. If the cap is already reached, release
  this claim and make a new one.

- **Runtime port 17340 in use when running `astrid action run`.**
  Cause: a running `astrid-runtime up` instance holds the door port, and `action run` refuses to start beside it.
  Fix: stop that instance (find it with `ss -ltnp | grep 17340`), then run `astrid action run`.
  It starts and stops its own ephemeral Runtime.

- **`An existing workspace has no usable source profile` during install.**
  Cause: the workspace exists, but the source profile at
  `$DATA_ROOT/runtime/source-profiles/astrid.json` is missing or does not match.
  Fix: rerun `pod_bootstrap.sh`. Its `setup_runtime` step writes the profile when it differs.
