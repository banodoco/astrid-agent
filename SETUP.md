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
