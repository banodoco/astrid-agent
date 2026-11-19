set -euo pipefail
out="$RUNNER_TEMP/fix12-receipt"
cd "$GITHUB_WORKSPACE/src"
export PYTHONPATH="$GITHUB_WORKSPACE/proof/scripts/ci/fix12_pair${PYTHONPATH:+:$PYTHONPATH}"
testcase=tests/integrations/test_dry_run_equals_live_run.py::test_dry_run_equals_live_run
status=0
python -m pytest -v -rA --durations=40 -m 'integration and not opt_in and not app_checkout' \
  -p functional_reports "${testcase}[image/t2i-local-managed]" "${testcase}[video/h3-local-managed]" \
  || status=$?
printf '%s\n' "$status" > "$out/pytest-exit.txt"
exit "$status"
