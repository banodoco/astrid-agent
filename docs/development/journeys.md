# `astrid dev journey`: real-film merge gate

Run on a machine allowed to start the Runtime, Remotion and process groups, with the reference stack stopped.
It adopts a private clone of the reference realm into a disposable data root (the reference is never written),
brings the Runtime up there, and reaps or reports any leftover process group at the end.

## Scratch Runtime for CLI work

Activate the Python virtual environment that has Astrid and Runtime dependencies (so `$VIRTUAL_ENV` is set), then
point Python at both source checkouts. Use disposable paths; `workspace create` writes a new realm and selects it as
the workspace. The Runtime source manifest binds that workspace to the exact source and Python environment, and
must be passed when creating and running it:

```sh
export ASTRID_CHECKOUT=/absolute/path/to/astrid-agent
export RUNTIME_CHECKOUT=/absolute/path/to/astrid-runtime
export PYTHONPATH="$ASTRID_CHECKOUT:$RUNTIME_CHECKOUT${PYTHONPATH:+:$PYTHONPATH}"
export SCRATCH_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/astrid-runtime-scratch.XXXXXX")"

python - <<'PY'
import json
import os
from pathlib import Path

Path(os.environ["SCRATCH_ROOT"], "source-manifest.json").write_text(json.dumps({
    "profile": "astrid",
    "source_checkout": os.environ["ASTRID_CHECKOUT"],
    "runtime_checkout": os.environ["RUNTIME_CHECKOUT"],
    "runtime_environment": os.environ["VIRTUAL_ENV"],
}, indent=2) + "\n", encoding="utf-8")
PY

python -m astrid_runtime workspace create --profile astrid \
  --data-root "$SCRATCH_ROOT" \
  --realm-root "$SCRATCH_ROOT/runtime/realms/scratch" \
  --realm-id scratch --source-manifest "$SCRATCH_ROOT/source-manifest.json" \
  --process-control sandbox --json

python -m astrid_runtime run --profile astrid --data-root "$SCRATCH_ROOT" \
  --source-manifest "$SCRATCH_ROOT/source-manifest.json" --hosted-origin off
```

`run` stays in the foreground; press Ctrl+C to stop it. The `sandbox` process-control setting is selected during
workspace creation, and `--hosted-origin off` leaves out the browser door.

```sh
python -m astrid dev journey --tier smoke \
  --reference <stopped data root> --timeline almost-ready/<TIMELINE> --summary /private/tmp/journey.json
```

Options beyond `--tier`, `--reference`, `--timeline` and `--summary`:

| Option | Meaning |
| --- | --- |
| `--reference generated` | Build a small stopped reference instead of using a film: one project (`journey`), three 4 s ffmpeg test clips and one media-only timeline (`main`), made by `astrid/core/dev/reference_realm.py` (`astrid_runtime workspace create` plus an in-process `RuntimeService`, no launcher). `--timeline` defaults to `journey/main` and `--range` to `2 6`. Needs `ffmpeg`. |
| `--backend rendering.remotion\|rendering.ffmpeg` | Renderer for every render, including the App-shaped one (default: Remotion for the CLI renders, and no `selector` in the App-shaped request, so the host chooses: Remotion when it can run it, else ffmpeg). With ffmpeg the Node-pin preflight is skipped and `--review` is never passed (the ffmpeg backend takes `--range` but not review); the film must be media-only. |
| `--app-checkout PATH` | A astrid-app checkout whose HEAD is recorded in the summary. |
| `--skip-app-render` | `runtime_restart` restarts the Runtime but does not admit the App-shaped render (on a real film that render is the whole film). |

Before anything starts the journey refuses unless the temp volume has free disk of at least the Runtime floor
(`ASTRID_RUNTIME_FREE_DISK_FLOOR_BYTES`, default 1 GiB) plus the expected scratch of one excerpt render
(an estimate per backend, `SCRATCH_BYTES_PER_SECOND`; the real figure is the capability's and is unreadable before the
Runtime is up). The numbers are in the refusal and in the summary (`disk_preflight`). Every child process gets a scrubbed
environment (`scrub_child_env`): no `NODE_OPTIONS`, `NODE_PATH`, `LD_*`, `DYLD_*`, non-interactive `GIT_*`,
Python-startup overrides, or the variables the journey replaces itself, because Remotion runtime checks fails closed on them.

The summary's `revisions` hold the Astrid HEAD, the imported `runtime_protocol` checkout and its HEAD, the App HEAD
(when `--app-checkout` is given) and the `version` of Astrid's `artifacts.json` and the Runtime's `contract/artifacts.json`,
each with whether the tree was dirty when the run started. `outcome` is `success`, `failure` or `hung`.

`--tier smoke` (about 4 min, before every push): first client open starts the pack host, doctor must be healthy with a
usable renderer Node, render the excerpt (`--range 144 152` by default), append a comment to `astrid/core/dev/journey.py`
(inside the host's source closure; restored in `finally`), then:

1. the next client open, run inside an `otto-heavy` job with private one-slot lease state, replaces the outdated idle
   host by itself and the new host outlives the job. The outer journey retains the machine-wide capacity lease;
   the nested job exercises lease cleanup without joining that queue. Job lease and capacity settings are stripped
   from the durable host;
2. a second excerpt renders on the new host;
3. a client with a duplicated PATH reuses the host (neither refused nor replaced);
4. kill -9 of the idle host: the next open recovers with a new host;
5. kill -9 of the host mid-render: the next host start reaps the orphaned render processes
   (`generic-host.task-scopes.json`). The killed task is then read back from the Runtime once its 30 s lease has run out:
   `GET /v1/tasks/{id}/events` must show `task.lease_expired` and exactly one end event (`task.completed`, `task.failed`
   or `task.cancelled`, after the expiry), agreeing with the task's state. The pre-kill and post-settle attempt id and
   lease fence are recorded (`killed_task_lease`). A stale-fence publication is not attempted: it needs the lease id,
   which only the worker holds and the task API does not expose.
6. `runtime_restart`: `astrid_runtime restart`, then, with no Astrid client open, the App's own render request is
   admitted (`build_app_render_admission`: `POST /v1/tasks` with the `Idempotency-Key`
   `astrid.render:v1:<timeline>:head:download:<file>`, the bearer token from `credentials/astrid.json`, the digest and storage
   estimate of `rendering.render` from `GET /v1/capabilities`; the `selector` only when `--backend` is given, as the App names no renderer for an ordinary timeline) and must complete.
   The output file is not inspected: the destination is `download`. Then a render succeeds on the restarted Runtime.

SIGTERM and SIGINT record an interrupted failure and run teardown: restore the source probe, stop the private Runtime,
and reap private process groups. Further interrupts are ignored during teardown. Groups that survive SIGTERM are
checked again for ownership before SIGKILL.

`--tier full` (about 20 min) adds a Codex image (`generation.generate_image`, flux-dev t2i), `pixel.sprites`
(chroma, scale 5), `timelines checkout --fresh`, `edit --replace-asset SLOT=run:<sprites>/sprites#0`
(`--replace-slot`, default `BUGNET`), `check --no-sweep`, `publish`, and a full-film `render --review --wait`,
whose duration is checked (`--expect-duration` for an exact value). All of it happens in the private clone.

Time is never a verdict: each phase's duration and the machine load are recorded in the summary for
information. A command is stopped only when it is genuinely hung (no exit within `HANG_SECONDS`, many times
its usual duration on a loaded Mac), and that is reported as hung. Resource growth is asserted: 4 GiB disk,
20 MiB receipts, 100 MiB logs, and no rooted process left behind. On failure the tails of the Runtime and host
logs are kept beside the summary, and a failed command reports both its stderr and stdout.

Plumbing tests (no Runtime, Remotion or launcher): `python -m pytest -q tests/journeys`. They cover the helpers above, the
runner's three outcomes (success, failure, hung) with `true`, `false` and `sleep`, the App request against a loopback
stand-in for the Runtime, and, when `runtime_protocol`, `astrid_runtime` and `ffmpeg` are importable
(`PYTHONPATH=<astrid-runtime>`), the generated reference: it passes `select_reference_realm`, leaves no WAL, and
opens in-process listing its project, 3 objects and timeline.

## Where it runs

The full smoke runs on macOS only, for now. The Runtime launcher (`python -m astrid_runtime up|restart|down`) takes
Darwin audit-token custody of the Runtime process, and there is no Linux custody provider yet, so on Linux
`adopt` is refused with "Darwin audit-token custody is unavailable" (the generated reference is built and cloned up to that
point). The journey deliberately has no shim for this: it would test a launcher the product does not ship. Remotion
is no longer part of `rendering.render` readiness: it is checked per task (a task that selects `rendering.remotion` on a
host that cannot run it fails at its lease; a task that names no renderer renders with ffmpeg there). With
`--backend rendering.ffmpeg` the Node-pin preflight is skipped, though the `doctor` phase still reports the Remotion fault. Until the launcher has a Linux custody provider,
the end-to-end run stays on the reference Mac; Linux CI can run the plumbing tests only.

The three-product thin slice (F2) is not a phase here. It is an App Playwright spec, `astrid-app/tests/e2e/local/three-products.spec.ts`, run by `npm run test:e2e:three-products` and by the nightly `three-products` job in `astrid-app/.github/workflows/direct-journey.yml`; it runs on Linux in installed Chrome (the render case plays H.264 and AAC) and drives this package's `astrid` CLI and `astrid.core.dev.cross_client` against a throwaway Runtime; `cross_client host --render` makes the same worker host `rendering.render`, which a host without Remotion advertises ready and renders with ffmpeg for a task that names no renderer. `astrid dev journey --tier full` folds it in with card F2-L2.

The wider cross-repo testing plan (what the journeys gate, and the other layers) is in `testing-plan.md`.
