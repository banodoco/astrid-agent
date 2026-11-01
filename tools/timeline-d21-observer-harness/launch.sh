#!/bin/sh
set -eu
HARNESS=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
unset PYTEST_PLUGINS PYTEST_ADDOPTS
if [ -z "${TMPDIR:-}" ] || [ ! -d "$TMPDIR" ] || [ ! -w "$TMPDIR" ] || [ ! -x "$TMPDIR" ]; then
  printf '%s\n' 'invalid live TMPDIR' >&2
  exit 2
fi
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$HARNESS"
export D21_EVIDENCE_DIR="${D21_EVIDENCE_DIR:-$TMPDIR/d21-observer-evidence}"
export D21_RUN_ID="${D21_RUN_ID:-d21}"
case "${1:-full}" in
  boot) printf '%s\n' 'D21 CANONICAL LAUNCHER BOOT PASS'; exit 0;;
  preflight|cohort|full|post-g5-2225) ;;
  *) printf '%s\n' 'invalid harness mode' >&2; exit 2;;
esac
[ "$#" -le 1 ] || { printf '%s\n' 'invalid launcher argv' >&2; exit 2; }
/home/user/work/venv-agent/bin/python -P "$HARNESS/preflight.py" "$0" preflight
if [ "${1:-full}" = preflight ]; then exit 0; fi
set +e
if [ "${1:-full}" = cohort ]; then
  /bin/sh "$HARNESS/cohort-body.sh"
else
  /bin/sh "$HARNESS/full-body.sh"
fi
RESULT=$?
set -e
/home/user/work/venv-agent/bin/python -P "$HARNESS/preflight.py" "$0" post
exit "$RESULT"
