set +e
mkdir -p runtime-focused/validation
if python3 -m venv .r15-proof-venv > runtime-focused/venv-create.log 2>&1; then
  venv_exit=0
else
  venv_exit=$?
fi
printf '%s\n' "$venv_exit" > runtime-focused/venv-create.exit
PYTHON="$PWD/.r15-proof-venv/bin/python"
if [ "$venv_exit" -eq 0 ] && "$PYTHON" -m pip install -e 'runtime/.[test]' > runtime-focused/setup.log 2>&1; then
  setup_exit=0
else
  setup_exit=$?
fi
printf '%s\n' "$setup_exit" > runtime-focused/setup.exit
if [ "$setup_exit" -ne 0 ]; then
  exit "$setup_exit"
fi
name=test_production_daemon_door_advertises_and_executes_selected_updater
if "$PYTHON" -m pytest -q -m integration --collect-only "runtime/tests/test_update_jobs.py::$name" > "runtime-focused/validation/$name.collect.log" 2>&1; then
  validation_collect_exit=0
else
  validation_collect_exit=$?
fi
printf '%s\n' "$validation_collect_exit" > "runtime-focused/validation/$name.collect.exit"
if "$PYTHON" -m pytest -q -m integration "runtime/tests/test_update_jobs.py::$name" > "runtime-focused/validation/$name.log" 2>&1; then
  validation_test_exit=0
else
  validation_test_exit=$?
fi
printf '%s\n' "$validation_test_exit" > "runtime-focused/validation/$name.exit"
selectors=(
  test_reconcile_before_manifest_and_newer_target_checks
  test_unknown_reconciliation_never_prepares_or_launches
  test_unconfirmed_terminal_reconciliation_is_observe_only
  test_reconcile_helper_failure_preserves_uncertainty
  test_unacknowledged_continuation_reply_is_observe_only
  test_reconcile_terminal_result_prevents_preparation_and_new_launch
  test_update_route_h3_returns_confirmed_reconciliation_conflict
  test_update_route_h4_returns_recovery_refusal
  test_update_route_h5_returns_uncertain_acknowledgement
  test_authenticated_continuation_keeps_operation_and_journal
  test_update_route_malformed_acceptance_is_uncertain
  test_status_reads_authoritative_job_and_c2_journal_without_recovery
  test_status_fails_conservatively_for_unmatched_or_corrupt_c2_journal
  test_status_projects_unacknowledged_launching_as_recovery_needed
  test_production_daemon_door_advertises_and_executes_selected_updater
)
proof_failures=0
for name in "${selectors[@]}"; do
  marker=()
  if [ "$name" = test_production_daemon_door_advertises_and_executes_selected_updater ]; then marker=(-m integration); fi
  if "$PYTHON" -m pytest -q "${marker[@]}" --collect-only "runtime/tests/test_update_jobs.py::$name" > "runtime-focused/$name.collect.log" 2>&1; then
    collect_exit=0
  else
    collect_exit=$?
    proof_failures=1
  fi
  printf '%s\n' "$collect_exit" > "runtime-focused/$name.collect.exit"
  if "$PYTHON" -m pytest -q "${marker[@]}" "runtime/tests/test_update_jobs.py::$name" > "runtime-focused/$name.log" 2>&1; then
    test_exit=0
  else
    test_exit=$?
    proof_failures=1
  fi
  printf '%s\n' "$test_exit" > "runtime-focused/$name.exit"
done
exit "$proof_failures"
