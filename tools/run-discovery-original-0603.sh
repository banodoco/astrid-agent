#!/bin/bash
# Called from the normal test-service worker snapshot; all products stay installed.
set -euo pipefail
RUN_TMPDIR=NOTCREATED
RUN_EVIDENCE=NOTCREATED
LAUNCH_STATE=NOTRUN
printf 'discovery-wrapper-before cwd=%q argv=' "$PWD"
printf '%q ' "$@"
printf '\nPATH=%q PYTHONPATH=%q ASTRID_HOST_EXECUTION_PROFILE=%q ASTRID_REMOTION_PROJECT_DIR=%q ASTRID_NODE_EXECUTABLE=%q ASTRID_TEST_NODE_MODULES=%q\n' \
    "${PATH-}" "${PYTHONPATH-}" "${ASTRID_HOST_EXECUTION_PROFILE-}" \
    "${ASTRID_REMOTION_PROJECT_DIR-}" "${ASTRID_NODE_EXECUTABLE-}" "${ASTRID_TEST_NODE_MODULES-}"
# Keep the shell alive so env/interpreter startup failures also get an after
# record in the normal service log. Python owns body and JUnit status once run.
trap 'rc=$?; printf "discovery-wrapper-after exit=%s body=%s junit=%s origins=%s evidence=%q tmpdir=%q elapsed=%s\n" "$rc" "$LAUNCH_STATE" "$LAUNCH_STATE" "$LAUNCH_STATE" "$RUN_EVIDENCE" "$RUN_TMPDIR" "$SECONDS"' EXIT
RUNNER="$PWD/tools/run-discovery-original-0603.py"
RUN_TMPDIR=$(mktemp -d /tmp/w-discovery-0638.XXXXXX)
RUN_EVIDENCE=/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a/scratchpad/orch/workers/agt-F-discovery-harness-sol-0638/evidence/${RUN_TMPDIR##*/}
test -r "$RUNNER"
test -x /usr/bin/python3
LAUNCH_STATE=SEE_PYTHON_EVIDENCE
env -i HOME=/root LANG=C.UTF-8 LC_ALL=C.UTF-8 PATH=/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 TMPDIR="$RUN_TMPDIR" DISCOVERY_EVIDENCE="$RUN_EVIDENCE" \
    /usr/bin/python3 -P "$RUNNER" "$@"
