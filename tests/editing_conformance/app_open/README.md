# Agent writes through the App parser (R15-Q-01)

This is qualification row 5, phase 1: the Agent-produced
`parent.config.bundle` is parsed by the real App `parseTimelineBundle`.
It is not the App loader or a whole-project open test (phase 2 is Q-03).

Only the `transcripts refresh --use-source` case currently **changes the
typed envelope**. The other 35 cases drive a write and re-validate the
unchanged App-authored seed. These are envelope preservation checks, not
independent coverage of envelope-writing logic. Every case also checks a
deliberately invalid copy; the real writer's rejection is never treated as
a successful negative control.

## Qualification invocation

The test module is in the existing default-off `app_checkout` manual lane.
The default suite does not collect it. Explicitly select the lane to run it;
missing App or Node prerequisites then fail (never skip), including when
`ASTRID_REQUIRE_APP_PARSER=1`. Setting the environment variable alone
does not override the default lane selection.

The Qualifier prepares the Agent environment and an exact App checkout with
`npm ci --legacy-peer-deps` already completed, before the offline proof.
The original proof used Node 22; use a Node version supporting
`node --import tsx` (the App pins 20.19.4). The test never installs
dependencies or accesses the network.

**Do not simply prefix `agent-pytest.sh` with the App variables.**
That wrapper invokes `env -i` and strips both
`ASTRID_APP_ROOT` and `ASTRID_REQUIRE_APP_PARSER`.
The following explicit pass-through mirrors its prepared environment and
selects the App lane, without changing the shared worker wrapper.
Run from the root of the Agent checkout on the proof machine:

```sh
CO=$(pwd -P)
E="$CO.env"
: "${ASTRID_APP_ROOT:?export the absolute path to the prepared App checkout}"
NODE_DIR=$(dirname "$(command -v node)")
BT="$E/basetemp/$(date +%s%N)"
env -i PATH="$E/venv/bin:$(cat "$E/uv-dir"):$NODE_DIR:/usr/local/bin:/usr/bin:/bin" \
  HOME="$E/home" TMPDIR="$E/tmp" XDG_CACHE_HOME="$E/cache" LANG=C.UTF-8 \
  PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONSAFEPATH=1 \
  PYTHONPATH="$E/vibecomfy" ASTRID_RUNTIME_CHECKOUT="$E/runtime" \
  ASTRID_VIBECOMFY_CHECKOUT="$E/vibecomfy" ASTRID_TEST_LIFECYCLE_CHECKOUT="$E/lifecycle" \
  ASTRID_APP_ROOT="$ASTRID_APP_ROOT" ASTRID_REQUIRE_APP_PARSER=1 \
  "$E/venv/bin/python" -m pytest -p no:cacheprovider --basetemp="$BT" \
  -m app_checkout -q -rA tests/editing_conformance/test_agent_writes_open_in_app.py
```

The proof machine may also wrap the invocation in its offline network
namespace (`unshare -rn`). Run the unchanged selectors and lane policy
checks separately in the default lane (do not use `-m app_checkout` for
these):

```sh
bash scripts/worker-env/agent-pytest.sh -q -rA \
  tests/editing_conformance/test_agent_writes_open_in_app.py \
  tests/sdk/test_timeline_sync.py \
  tests/packs/timeline/test_cli_commit_move.py \
  tests/editing_conformance/test_recipe_occurrence_create.py \
  tests/packs/timeline/test_cli_media.py \
  tests/test_skip_policy.py tests/test_lane_registry.py
```

For the historical red proof, the Qualifier uses Agent `514499de` with
this harness and App `b714ef32`. The real `test_transcripts_refresh`
must fail with the missing `assetId` diagnostic. For the fixed proof
use the integrated Agent `a3f3b01c` with this harness and App
`fdb96108` (contains App #392, landed as `7bc0d4b1`), the pair in
the Qualifier's rework decision. Record the actual Agent/App SHAs used
for each run. The fixed real refresh must pass, while all negative
controls must still reject. No xfail or assertion change.
