---
name: fast-iteration
short_description: "Iterate fast on Astrid with evidence you can trust."
description: "How to iterate fast on Astrid code: the venue ladder, focused proofs without early stops, shared caches and cheap clones, judging by the inner exit code, retry families, receipts and carry-forward, the Runtime → Agent → App tuple train, landing pre-checks and disk hygiene. Use when changing Astrid, running or debugging proofs and CI or GPU jobs, preparing a change to land, or when the loop feels slow."
metadata:
  astrid:
    audience: developer
---

# Fast iteration

Aim for minutes from edit to result, with evidence that still holds up at
release time. Start from [Developing Astrid](../SKILL.md) for the repositories
and their build and test commands; the gates that evidence must satisfy are in
[Releasing](../releasing/SKILL.md).

## 1. The inner loop

1. **Clone in seconds.** When a local copy of the repository exists, make a
   shared-object clone instead of a network clone, then push your branch from
   it as usual:

   ```sh
   git clone -q --shared --no-checkout "$SOURCE_REPO" "$WORK/agent"
   git -C "$WORK/agent" remote set-url origin https://github.com/banodoco/astrid-agent
   git -C "$WORK/agent" checkout -q --detach <sha-or-branch>
   ```

   It shares objects with the source (keep the source in place while the clone
   exists) and has its own branches, so deleting it never touches the source.
2. **Reuse the package caches.** Keep `UV_CACHE_DIR`, `PIP_CACHE_DIR` and
   `npm_config_cache` pointed at the shared cache; put the venv and
   `node_modules` inside the work folder. Install from the lock the proof uses
   (`uv pip sync --require-hashes <lock>` where one exists).
3. **Keep scratch in the work folder** (`TMPDIR` under it), not in `/tmp` or
   `$HOME`, so cleaning up is one `rm -rf`.

## 2. The venue ladder

Pick the cheapest venue that proves the claim:

| Need | Venue |
| --- | --- |
| Focused unit and integration tests (seconds to minutes) | Locally, in the work folder |
| Linux suites (minutes to an hour) | GitHub Actions |
| macOS and Windows journeys | GitHub Actions hosted runners |
| GPU work, live hardware evidence, or a suite that truly needs a pod | RunPod |

A higher venue is not stronger evidence. Move up only for a real environment
requirement, and keep each remote request small: name the tests that prove the
change.

## 3. Focused proofs

- Run exactly the tests that prove "done", and run all of them. **Never use
  `--maxfail`, `-x` or another early stop in a proof run**: a stopped run hides
  the tests it never executed. If the repository configuration adds options,
  clear them: `.venv/bin/python -m pytest -o addopts= -q <tests>`.
- At intermediate checkpoints use change-directed coverage: freshness and
  regeneration checks, the affected integration tests, the protected tests for
  any behaviour you touched, and cross-platform runs when platform code
  changed. The complete matrix belongs to the checkpoints that need it (see
  [Releasing](../releasing/SKILL.md)).
- One authentic run may satisfy several obligations only when every assertion
  stays separately attributable.

## 4. Verdicts: judge the inner exit

A wrapper (a CI dispatcher, a pod broker, `timeout`, a relay) is transport.
The result is the exit code of the inner test, build or install command.

- Capture the inner exit explicitly and report it; "job done" or "upload ok"
  only means the transport worked.
- Use one vocabulary: `PASS`, `FAIL`, `RETRYING` (infrastructure is being
  retried, not a product result) and `NO-RESULT` (missing evidence).
- Exit `125` is a setup or preflight failure and `127` a missing command; they
  are environment failures, not test failures.
- A `PASS` is valid only for the exact revision or tree it names.

## 5. Retry families

- Retries belong to the task family, not to the attempt number.
- Infrastructure failures (capacity, readiness loss, eviction, transient
  network, quota, `NO-RESULT`) are retried with the same inputs; don't open a
  new diagnosis for them. A request rejected as invalid is fixed, not retried.
- **After two failures with the same signature, do not run a third time
  unchanged.** Record the code, fixture, environment or input change that
  addresses the signature first. Diagnose inline in the next attempt; open a
  separate diagnosis only for a genuinely unknown cause.
- When waiting on someone else, say so precisely: the dependency, its
  producer, the job or SHA, the event awaited and a deadline. Keep doing
  independent work meanwhile.

## 6. Receipts and carry-forward

- Write **one receipt per attempt**: task and attempt, repository, head, base
  and parents, the Runtime/Agent/App tuple, tool versions, exact commands,
  inner exits, coverage (including anything skipped), artifact hashes and
  limitations. Reports and gate tables reference receipts; they don't restate
  them.
- **Carry a receipt forward** when a new head differs from a proven head only
  in tests, fixtures or docs, and product, dependency, generated and installed
  bytes are proven unchanged. Re-run only what changed and record the original
  receipt IDs. Protected surfaces never carry: they need an exact-head proof.

## 7. The tuple train

Astrid lands in pin order, Runtime → Agent → App, with merge commits only, and
landed branches pin only published SHAs. To keep the chain under an hour:

1. Downstream keeps a **provisional candidate** pinned to the upstream
   *candidate* on a work-in-progress branch (never handed off as landed).
2. When upstream lands, re-pin to the **published** SHA and run the freshness
   checks (regenerate, then expect a clean diff).
3. Report the provisional, re-pinned, upstream-candidate and upstream-landed
   SHAs, then decide:
   - **Carry**: identical trees apart from the pin, so the provisional proof applies.
   - **Protected**: protected surfaces changed, so run exact-head protected proofs.
   - **Reproof**: anything else; re-run the focused proof.

## 8. Landing pre-check

Before asking for a landing, dry-run the checks: the branch and exact commit,
ancestry and a clean merge tree against the integration base, published pins
in Runtime → Agent → App order, evidence naming that exact SHA or tree, and
changed paths against the protected-path list. Protected changes keep their
full gates. A dry run is not a landing, and only accepted candidates are
handed off.

## 9. Disk and processes

- Keep a free-space floor that matches your concurrency, and lower the
  concurrency before the disk fills: a full disk turns into many unrelated
  failures.
- Remove finished work folders; first bundle anything dirty or unpushed
  (`git bundle create`). Bound shared caches with a size policy instead of
  deleting them while jobs run.
- Never move a work folder while a job still writes into it.
- Replace a running script atomically (write `name.new`, then `mv`); never edit
  it in place.

## Never trade away

Speed never waives the protected surfaces listed in
[Releasing](../releasing/SKILL.md): install, update, rollback, data and bridge
journeys with the frozen `POST /v1/update`, Timeline persistence, the App
update guard, live GPU evidence at RC, and honest evidence.
