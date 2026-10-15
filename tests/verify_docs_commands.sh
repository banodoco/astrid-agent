#!/usr/bin/env bash
set -euo pipefail

# verify_docs_commands.sh
# Extract gateway command shapes from the canonical CLI journey and README,
# skip placeholders/assets, and verify each unique subcommand path exists
# by running it with --help (or directly for doctor).
#
# Also expands [A|B|C] shorthand from README to verify each variant.

PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

FAILED=0
TMPFILE="$(mktemp)"
trap 'rm -f "$TMPFILE"' EXIT

echo "=== verify_docs_commands.sh ==="
echo "Extracting command shapes from docs..."

# Extract from the canonical user-facing CLI guide. README's command is embedded
# in HTML, and template STAGE files demonstrate internal module entry points,
# so neither is an authoritative census of the public gateway.
CLI_GUIDE="$REPO_ROOT/docs/guides/cli-journeys.md"
if [ ! -f "$CLI_GUIDE" ]; then
  echo "FAILED: canonical CLI guide is missing: $CLI_GUIDE" >&2
  exit 1
fi
awk '/^```bash$/{found=1; next} /^```$/{found=0} found' "$CLI_GUIDE" | \
  grep -Eo 'python3 -m astrid [^ ]+( [^ ]+)?' >> "$TMPFILE" || true

if [ ! -s "$TMPFILE" ]; then
  echo "FAILED: extracted zero public Astrid command shapes from $CLI_GUIDE" >&2
  exit 1
fi

echo ""
echo "Raw extracted command prefixes (before dedup):"
if [ -s "$TMPFILE" ]; then
  sort "$TMPFILE" | uniq -c | sort -rn
else
  echo "  (none found)"
fi
echo ""

# Deduplicate
sort -u -o "$TMPFILE" "$TMPFILE"

# Expand shorthand [A|B|C] entries using Python for reliable string handling
EXPANDED="$(mktemp)"
trap 'rm -f "$TMPFILE" "$EXPANDED"' EXIT

"$PYTHON_BIN" - "$TMPFILE" "$EXPANDED" <<'PYEOF'
import sys, re

with open(sys.argv[1]) as f:
    lines = [l.strip() for l in f if l.strip()]

expanded = set()
for line in lines:
    m = re.search(r'\[([^]]+)\]', line)
    if m:
        variants = m.group(1).split('|')
        prefix = line[:m.start()]
        suffix = line[m.end():]
        for v in variants:
            expanded.add(prefix + v + suffix)
    else:
        expanded.add(line)

with open(sys.argv[2], 'w') as f:
    for e in sorted(expanded):
        f.write(e + '\n')
PYEOF

echo "After shorthand expansion:"
if [ -s "$EXPANDED" ]; then
  sort "$EXPANDED" | uniq -c | sort -rn
else
  echo "  (none)"
fi
echo ""

# Classify and verify each unique command shape
echo "Verifying unique subcommand paths..."
echo ""

while IFS= read -r cmd_prefix; do
  [ -z "$cmd_prefix" ] && continue

  # Skip if it contains placeholder tokens that indicate a non-literal example
  if echo "$cmd_prefix" | grep -qE 'builtin\.example|path/to/|<[^>]+>'; then
    echo "  SKIP (placeholder): $cmd_prefix"
    continue
  fi

  # Help parsing verifies the documented path and flags without connecting to
  # the runtime or requiring workspace credentials/configuration.
  echo -n "  HELP  $cmd_prefix ... "
  command_args="${cmd_prefix#python3 }"
  read -r -a command_argv <<< "$command_args"
  command_ok=0
  if [[ " $command_args " == *" --help "* ]]; then
    "$PYTHON_BIN" "${command_argv[@]}" >/dev/null 2>&1 && command_ok=1
  else
    "$PYTHON_BIN" "${command_argv[@]}" --help >/dev/null 2>&1 && command_ok=1
  fi
  if [ "$command_ok" -eq 1 ]; then
    echo "OK"
  else
    echo "FAILED (exit=$?)"
    FAILED=1
  fi
done < "$EXPANDED"

echo ""
if [ $FAILED -eq 0 ]; then
  echo "All docs command verifications passed."
else
  echo "Some verifications FAILED." >&2
  exit 1
fi

echo ""
echo "=== Verify commands and frontmatter in authored skills ==="
PYTHON_BIN="$PYTHON_BIN" "$PYTHON_BIN" "$SCRIPT_DIR/verify_skill_cli_commands.py"

echo ""
echo "=== Verify CLI guidance strings ==="
PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" -m pytest -q "$SCRIPT_DIR/test_cli_guidance.py"
