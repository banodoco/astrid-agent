#!/usr/bin/env bash
# Diagnostic-only wrapper; never print capture content, certificate or paths.
set +x
set -uo pipefail
umask 077

fail() { printf '%s\n' 'Capture unavailable, incomplete, unsafe, or retention failed' >&2; exit 1; }
[[ "${ASTRID_CI_EXPECTED_RUNNER:-}" == macos-15 ]] || fail
[[ "$(uname -s)" == Darwin ]] || fail
[[ "${MAC_ADMISSION_CERT_SHA256:-}" =~ ^[0-9a-f]{64}$ ]] || fail
[[ -n "${MAC_ADMISSION_RECIPIENT_CERT:-}" && -n "${RUNNER_TEMP:-}" && -n "${GITHUB_OUTPUT:-}" ]] || fail

private_dir="$(mktemp -d "${RUNNER_TEMP}/astrid-admission.XXXXXXXX" 2>/dev/null)" || fail
cleanup() { rm -rf -- "$private_dir" 2>/dev/null || true; }
trap cleanup EXIT
chmod 700 "$private_dir" 2>/dev/null && chmod -N "$private_dir" 2>/dev/null || fail
(cd "$private_dir" && test "$(ls -lde . | wc -l | tr -d '[:space:]')" = 1) 2>/dev/null || fail
capture_dir="$private_dir/capture"
mkdir -m 700 "$capture_dir" 2>/dev/null || fail
chmod -N "$capture_dir" 2>/dev/null || fail
(cd "$capture_dir" && test "$(ls -lde . | wc -l | tr -d '[:space:]')" = 1) 2>/dev/null || fail
cert="$private_dir/recipient.pem"
printf '%s\n' "$MAC_ADMISSION_RECIPIENT_CERT" 2>/dev/null > "$cert" || fail
unset MAC_ADMISSION_RECIPIENT_CERT
cert_hash="$(openssl x509 -in "$cert" -outform DER 2>/dev/null | shasum -a 256 | cut -d ' ' -f1)" || fail
[[ "$cert_hash" == "$MAC_ADMISSION_CERT_SHA256" ]] || fail
# Refuse to run if a previous ciphertext could be mistaken for this run.
cipher="$RUNNER_TEMP/macos-admission-capture.p7m"
test ! -e "$cipher" && test ! -L "$cipher" || fail
export ASTRID_MAC_ADMISSION_CAPTURE_DIR="$capture_dir"
run_root="$RUNNER_TEMP/astrid-r1-direct-macos"
journey_rc=0
python -u agent/scripts/ci_journey.py \
  --job-id direct-macos --mode direct --leg release0-to-release1 \
  --candidate "$RUNNER_TEMP/candidate.json" \
  --app-checkout "$PWD/app" --candidate-agent-checkout "$PWD/candidate-agent" \
  --candidate-runtime-checkout "$PWD/runtime" --pinokio-checkout "$PWD/pinokio" \
  --run-root "$run_root" --root "$run_root/program" --data-root "$run_root/data" \
  --shell sh --release0-fixture "$PWD/agent/tests/fixtures/release0" \
  --allow-release0-to-release1 || journey_rc=$?
validation_rc=0
python -I agent/scripts/verify_macos_admission_capture.py || validation_rc=$?
if (( validation_rc == 0 )); then
  (cd "$capture_dir" && for f in admission-*.json; do
    test "$(ls -le "$f" | wc -l | tr -d '[:space:]')" = 1 || exit 1
  done) 2>/dev/null || validation_rc=1
fi
unset ASTRID_MAC_ADMISSION_CAPTURE_DIR
retention_rc=1
if (( validation_rc == 0 )); then
  # Only encrypted bytes leave the runner. This recipient's DER fingerprint is
  # pinned by the caller; the corresponding private key stays outside Actions.
  if (cd "$capture_dir" && tar -cf - admission-*.json) 2>/dev/null | \
     openssl smime -encrypt -binary -aes256 -outform DER -out "$cipher" "$cert" 2>/dev/null; then
    test -s "$cipher" && retention_rc=0
  fi
fi
if (( retention_rc == 0 )); then
  printf '%s\n' 'capture_encrypted=true' >> "$GITHUB_OUTPUT" || retention_rc=1
fi
if (( retention_rc != 0 )); then
  rm -f -- "$cipher" 2>/dev/null || true
fi
printf 'journey_exit=%d validation_exit=%d retention_exit=%d\n' "$journey_rc" "$validation_rc" "$retention_rc"
if (( journey_rc != 0 )); then exit "$journey_rc"; fi
if (( validation_rc != 0 || retention_rc != 0 )); then fail; fi
