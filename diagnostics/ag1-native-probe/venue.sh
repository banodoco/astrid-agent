#!/usr/bin/env bash
set -uo pipefail
mkdir -p suite-artifacts
running=0
stopped=0
bash agent/diagnostics/ag1-native-probe/case.sh running || running=$?
bash agent/diagnostics/ag1-native-probe/case.sh stopped || stopped=$?
printf 'running=%s\nstopped=%s\n' "$running" "$stopped" | tee suite-artifacts/case-exits.txt
[[ "$running" = 0 && "$stopped" = 0 ]]
