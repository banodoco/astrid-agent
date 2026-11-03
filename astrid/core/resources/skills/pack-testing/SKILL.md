---
name: pack-testing
description: "Write, review and run tests for an Astrid pack. Use when adding or changing a pack's tests, choosing between a unit test, the real Runtime daemon fixture and an editing scenario, fixing a failing test, deleting an obsolete test, or reviewing a pack change for test quality."
metadata:
  short-description: "Test Astrid packs: fewer, truer tests"
---

# Pack Testing

The yardstick and rulings (R-1 to R-13) are in the
[testing plan](../../../../../docs/development/testing-plan.md); this is the
part a pack author needs day to day. Build the pack with
[pack-builder](../pack-builder/SKILL.md).

## What a good test is

- **Fewer, truer tests.** A test earns its place by catching a failure nothing
  else catches. If another test, the validator or a conformance suite already
  fails on that break, do not add a second one.
- **Assert behaviour and identity** (R-8): "the output is revision R", "the
  task ended in state X", "the manifest lists these three files". Not private
  attributes, source strings or the wording of a message.
- **Every test must pass.** Fix the product bug or stale test behind a failure.
  Do not accept failures, xfails, skips or time-based verdicts, and do not
  increase suite or per-test timeouts. Diagnose and fix hangs at their cause.
- **A fake is trusted only for what it reproduces from recorded real
  responses** (R-4). Anything else runs against the real thing.
- **Old checks go only once their assertions have homes** (R-1). Name where
  each useful assertion now lives before deleting the old test.
- **Red is never normal** (R-6): fix every failure before completion.

## Where a pack's tests go and how they run

Put them in `tests/packs/<pack>/test_*.py`, with an `__init__.py` so a file
name shared with another pack cannot collide (as in
[tests/packs/video_editing/](../../../../../tests/packs/video_editing/)).
Never inside `astrid/packs/<pack>/`: collection is `testpaths = ["tests"]`, so
they would never run. The cross-pack contracts in `tests/packs/test_pack_*.py`
(layout, discovery, validation, recoverability) already cover every pack.

```sh
python3 -m astrid.core.pack.cli validate astrid/packs/<pack>   # structure only, not behaviour
python -m pytest tests/packs/<pack>                             # the default lane
python -m pytest tests/packs/<pack> -m integration             # that pack's integration tests
scripts/check-before-push                                       # before every push (R-12)
```

Lanes are defined in [tests/lanes.py](../../../../../tests/lanes.py) (R-2), and pytest collection and default lane settings are in
[pyproject.toml](../../../../../pyproject.toml). The default run deselects four markers. No scheduled job runs them: run them
yourself, as shown above for one pack or for everything with
`python -m pytest -m "integration or darwin_native or live or opt_in"`.
A `-m` on the command line replaces the default deselection.

| Marker | When |
|---|---|
| `integration` | a pack host or other long-lived subprocess, Node, ffmpeg or Remotion, the network, or a second checkout (such as the Runtime) |
| `darwin_native` | native macOS APIs; run these tests on macOS |
| `live` | real provider credentials |
| `opt_in` | explicitly opt-in, never in the default lane |

Choose the lane honestly: mark a test, or a module with
`pytestmark = pytest.mark.integration`, for what it really needs. Import a
declared dependency plainly so missing dependencies fail and can be fixed;
load a module only the integration lane has with `require_module` from
`tests/lanes.py`.

## How to test an action

1. **Pure logic: a unit test.** Import the function from the action's module
   and feed it small, deterministic input, as
   [tests/packs/music/test_music_pack.py](../../../../../tests/packs/music/test_music_pack.py)
   does with synthetic signals. No Runtime, no subprocess.
2. **Runtime behaviour: the real daemon.** Use the `real_runtime` fixture from
   [tests/conftest.py](../../../../../tests/conftest.py). `real_runtime()`
   starts a daemon on a temp realm, `real_runtime(realm, support_root=...)`
   restarts one at a given realm, and every daemon is stopped at teardown. The
   Runtime is found by the one locator in
   [tests/helpers/runtime.py](../../../../../tests/helpers/runtime.py)
   (`ASTRID_RUNTIME_CHECKOUT`, or the `astrid-runtime` sibling).
   These tests are `integration`. Example:
   [test_runtime_publication_chain.py](../../../../../tests/packs/video_editing/test_runtime_publication_chain.py).
   Do not construct `RuntimeDaemon` yourself.
3. **A fake Runtime, only where it is verified.** A fake may answer only the
   operations recorded in
   [tests/fixtures/runtime-responses/](../../../../../tests/fixtures/runtime-responses/)
   (vendored under a lock), and it must be added to the shape checks in
   [tests/sdk/test_runtime_response_conformance.py](../../../../../tests/sdk/test_runtime_response_conformance.py).
   Do not write another general-purpose `FakeRuntime`.
4. **Editing intent: a scenario.** A timeline-editing verb gets one YAML
   scenario at `tests/editing_scenarios/<area>/<id>.yaml` (area is timing,
   structure, look, audio or code). The runner applies it through the CLI, a
   cut sheet and Python, and checks the documents agree, undo and redo, and
   idempotency. Format and fixtures:
   [tests/editing_conformance/README.md](../../../../../tests/editing_conformance/README.md).
   Run `python -m pytest tests/editing_conformance`.
5. **Contracts every pack meets.** Errors follow the
   [recoverable-error contract](../../../../../docs/contracts/error-model.md),
   held by [tests/test_recoverability_conformance.py](../../../../../tests/test_recoverability_conformance.py)
   (no bare raises or direct stderr writes). A render backend joins
   [tests/core/rendering/test_conformance.py](../../../../../tests/core/rendering/test_conformance.py)
   or [tests/packs/test_renderer_parity.py](../../../../../tests/packs/test_renderer_parity.py).

**Reap what you start** (R-9). Launch subprocesses through the
`owned_process_groups` fixture (`owned_process_groups.popen(...,
start_new_session=True)`, or `add_cleanup`). A leaked daemon or host process
group, or more than 64 leaked file descriptors, fails the session.

A change to `astrid/packs/rendering/**`, `remotion/**` or the host files also
needs the smoke before merge (R-11); `scripts/check-before-push` tells you.
Steps and options: [docs/development/journeys.md](../../../../../docs/development/journeys.md).

Delete tests for removed designs, recording the reason and assertion homes
in [tests/removed.yaml](../../../../../tests/removed.yaml). Update tests for
deliberately changed designs to assert the new behaviour. Fix any failure
in the product or the test; do not record it as an accepted exception.

## What not to do

- **Sleep to synchronise.** Wait on an event, a file or a gate (R-10).
- **Pin strings.** No assertions on source text, log wording, private
  attributes or file absence as a proxy for behaviour.
- **Mock what you can run.** If a temp realm, a small fixture file or the
  `real_runtime` fixture can do it, use that instead of a mock.
- **Skip or xfail a failure.** Tests must pass in their intended lane; fix the
  cause instead of accepting a skipped or expected failure.
- **Judge durations or mask hangs.** No `elapsed < N` assertions and no
  timeout increases; fix the cause of slow or hanging tests.

## Checklist

- [ ] Each test catches what nothing else catches, and asserts behaviour.
- [ ] Files are in `tests/packs/<pack>/`, with an `__init__.py`.
- [ ] Each test is in the lane it really needs; declared imports are plain.
- [ ] Runtime behaviour uses `real_runtime`; any fake is checked against recordings.
- [ ] Editing verbs have a scenario; errors pass the recoverability contract.
- [ ] No sleeps, string pins, duration assertions, accepted xfails or skips, or
      timeout increases.
- [ ] Every subprocess is owned and reaped.
- [ ] Every test passes; each removed test has a `removed.yaml` line and
      its useful assertions a named home.
- [ ] `python3 -m astrid.core.pack.cli validate` passes.
- [ ] The capability catalog is regenerated if a manifest changed:
      `python scripts/gen_capability_index.py` (`make doc-refs` fails on a
      stale catalog).
- [ ] The tests of any lane the change touches were run by hand (see above),
      and `scripts/check-before-push` is green.
