# Astrid local gate — run before pushing to catch CI/deploy failures locally.
#
#   make check   blocking pre-deploy gates, including renderer parity + Remotion typecheck
#   make ci      full mirror of the CI deploy job (adds wheel-install + pytest+coverage) — minutes
#
# `make check` green ≈ the CI "Python quality gates" deploy job will pass its fast gates.
# These run the SAME scripts CI runs (see .github/workflows/ci.yml), so they stay in lockstep.

PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: help check ci preflight structure doctor ruff mypy cycles remotion-install remotion-typecheck renderer-parity parity-guard wheel ci-mirror editable lock-build lock-runtime lock-proof lock-validate toolchain-record s1-gate m4-baseline m4-gate m7-gate m8-gate

help:
	@echo "make check   - blocking gates: structure, doctor, ruff, mypy, cycles, Remotion, renderer parity"
	@echo "make ci      - full CI deploy mirror: check + editable + wheel-install + pytest/coverage (minutes)"
	@echo "make parity-guard - run the focused feature parity guard"
	@echo "make s1-gate - m1 S1 gate: 12 focused lanes + durable summary/logs in out/s1-gate/latest"
	@echo "make m4-baseline - m4 Step 1: run pre-change selectors and retain artifacts/m4/baseline.json (fails closed)"
	@echo "make m4-gate - m4 Step 33: 13 focused lanes + authority lint + drift rejection + feasibility admission (fails closed)"
	@echo "make m7-gate - m7 GA evidence: admitted selectors 1-10 + provisional/retained dispositions (fails closed)"
	@echo "make m8-gate - m8 packaged GA evidence: digest validation + atomic six-file release publication (set M8_EVIDENCE=... to publish a bundle)"
	@echo "make lock-runtime / lock-build / lock-proof - refresh universal SHA-256 dependency locks with uv"
	@echo "make lock-validate - validate exact pins, hashes, and direct dependency coverage"
	@echo "make <gate>  - run one gate: structure | doctor | ruff | mypy | cycles | remotion-install | remotion-typecheck | renderer-parity | parity-guard | wheel | ci-mirror | editable | s1-gate | m4-baseline | m4-gate | m7-gate | m8-gate"
	@echo "make preflight - verify Python, Node, ffmpeg, and locked Remotion prerequisites"

# --- Fast gates: catch the common deploy blockers in seconds. Run before every push. ---
check: preflight structure doctor ruff mypy cycles remotion-typecheck renderer-parity
	@echo "✅ make check: blocking pre-deploy gates passed"

preflight:
	@$(PY) -c "import importlib.util, sys; required=('pytest','pytest_cov','pytest_timeout','mypy','ruff','build','banodoco_timeline_schema'); missing=[name for name in required if importlib.util.find_spec(name) is None]; problems=[]; sys.version_info < (3,11) and problems.append('Python >=3.11 is required (found %s)' % sys.version.split()[0]); missing and problems.append('missing Python modules: %s (run %s -m pip install -e \'.[dev]\')' % (', '.join(missing), sys.executable)); problems and sys.exit('PREFLIGHT FAILED: ' + '; '.join(problems))"
	@for tool in ffmpeg ffprobe node npm; do \
		if ! command -v "$$tool" >/dev/null 2>&1; then \
			echo "PREFLIGHT FAILED: '$$tool' is required; install it and retry (macOS: brew install ffmpeg node)" >&2; \
			exit 1; \
		fi; \
	done
	@if [ ! -d remotion/node_modules ]; then \
		echo "PREFLIGHT FAILED: remotion/node_modules is absent; run 'cd remotion && npm ci' and retry" >&2; \
		exit 1; \
	fi
	@echo "✓ local gate preflight ($(PY))"

structure:
	@$(PY) -c "import sys; from astrid.core.structure import validate_repo_structure as v; r=v(); [print('STRUCTURE ERROR:', e) for e in r.errors]; sys.exit(1 if r.errors else 0)"
	@echo "✓ repo structure (canonical top-level dirs)"

doctor:
	@$(PY) scripts/reshape/ci_doctor.py
	@echo "✓ doctor (deploy health gate)"

ruff:
	@$(PY) scripts/reshape/compare_ruff_baseline.py
	@echo "✓ ruff baseline (no lint regression)"

mypy:
	@$(PY) scripts/reshape/compare_mypy_baseline.py
	@echo "✓ mypy baseline (no type regression)"

cycles:
	@$(PY) -m scripts.reshape.import_cycles --baseline scripts/reshape/baselines/import_cycles.json
	@echo "✓ import cycles (no new cross-package cycle)"

remotion-install:
	@$(PY) scripts/reshape/remotion_gate.py install

remotion-typecheck:
	@$(PY) scripts/reshape/remotion_gate.py typecheck

renderer-parity:
	@$(PY) scripts/reshape/remotion_gate.py parity

parity-guard:
	@$(PY) -m pytest -q tests/parity/test_parity.py

# Canonical declaration -> Python and shared Composition TypeScript primitives.
# COMPOSITION_ROOT optionally updates/checks an admitted renderer checkout.
.PHONY: generate-motion-curves check-motion-curves
generate-motion-curves:
	@$(PY) scripts/gen_motion_curves.py $(if $(COMPOSITION_ROOT),--composition-root "$(COMPOSITION_ROOT)")

check-motion-curves:
	@$(PY) scripts/gen_motion_curves.py --check $(if $(COMPOSITION_ROOT),--composition-root "$(COMPOSITION_ROOT)")

# --- Full mirror of the CI deploy job (slow). Run before a release / when in doubt. ---
ci: check editable wheel ci-mirror
	@echo "✅ make ci: full CI deploy mirror passed — deploy should be green"

editable:
	@$(PY) -c "import astrid; print('✓ editable install imports')"

wheel:
	PYTHON_BIN="$(PY)" bash scripts/smoke_wheel_install.sh

lock-build:
	uv pip compile requirements/build.in --universal --python-version 3.11 --generate-hashes --no-annotate --custom-compile-command 'make lock-build' --output-file requirements/build.lock

lock-runtime:
	uv pip compile pyproject.toml --universal --python-version 3.11 --generate-hashes --no-annotate --custom-compile-command 'make lock-runtime' --output-file requirements/runtime.lock

lock-proof:
	uv pip compile requirements/proof.in --universal --python-version 3.11 --generate-hashes --no-annotate --custom-compile-command 'make lock-proof' --output-file requirements/proof.lock

lock-validate:
	@$(PY) -c "from scripts.reshape.release_reproducibility import validate_dependency_locks; validate_dependency_locks(); print('✓ hashed dependency locks')"

toolchain-record:
	@$(PY) -m scripts.reshape.release_reproducibility --output out/release-toolchain.json >/dev/null
	@echo "✓ release toolchain recorded in out/release-toolchain.json"

ci-mirror:
	PYTHON_BIN="$(PY)" bash scripts/reshape/run_ci_checks.sh

# --- m1 S1 gate (plan step 23): the twelve focused m1 lanes, one command ---
# Runs the complete fresh-database, conformance, crash, contention, lint,
# runtime, and provider lane hermetically (fresh ASTRID_PROJECTS_ROOT /
# ASTRID_HOME, scrubbed task env) and retains a machine-readable summary plus
# per-lane logs and JUnit XML under out/s1-gate/latest (gitignored). CI uploads
# that directory with `if: always()` so evidence survives failures. This is the
# SAME target the local CI mirror (run_ci_checks.sh) and GitHub Actions invoke,
# keeping both entry points in lockstep. PY overrides the interpreter (e.g. a
# runtime venv that provides banodoco_timeline_schema for schema lanes).
s1-gate:
	@rm -rf out/s1-gate/latest
	@$(PY) scripts/reshape/s1_gate.py --out-dir out/s1-gate/latest
	@echo "✓ s1-gate (12 focused lanes; summary + logs retained in out/s1-gate/latest)"

# --- m4 Step 1 baseline (plan step 1 / task T1) ---
# Runs the pre-change selectors (v10 contract, writer/UoW, timeline
# repository, media pipeline) and retains schema-versioned
# evidence at artifacts/m4/baseline.json with the git SHA, tool versions,
# per-selector pass/fail, and timestamps. The script fails closed: a failed,
# absent, or malformed baseline exits non-zero, which blocks Step 2 onward.
# PY overrides the interpreter (same convention as the other gates).
m4-baseline:
	@$(PY) scripts/reshape/m4_baseline.py
	@echo "✓ m4-baseline (pre-change selectors green; evidence retained in artifacts/m4/baseline.json)"

# --- m4 Step 33 finalizer gate (plan step 33 / task T37) ---
# The final m4 admission boundary: 13 retained focused lanes (contracts,
# composition, owner lock, services, CLI, media/task/run/pack
# conformance, crash/contention, secrets, platform, authority lint), the
# live-tree authority lint, forbidden authority/schema/surface drift
# rejection, the Python 3.11/3.12 matrix, and the present-accepted
# feasibility admission. Fails closed on any missing/rejected admission,
# drift, or lane failure. Evidence: per-lane
# logs/JUnit in out/m4-gate/latest (gitignored) and the schema-versioned
# admission at artifacts/m4/finalizer-admission.json. PY overrides the
# interpreter (same convention as the other gates).
m4-gate:
	@$(PY) scripts/reshape/m4_gate.py
	@echo "✓ m4-gate (13 focused lanes + authority lint + drift + feasibility admission; admission retained in artifacts/m4/finalizer-admission.json)"

# --- m7 GA evidence gate (plan step 9 / task T12_impl) ---
# The script first validates the fresh Phase 0 admission, runs the explicit
# whole-file selectors for GA items 1–10, and revalidates admission immediately
# before atomically publishing acceptance.json and defects.md.  Item 11 is
# source/build provisional evidence; item 12 retains the M3 source/test proof
# pending the M8 installed-artifact rerun.
m7-gate:
	@$(PY) scripts/reshape/m7_gate.py --gate --artifact-dir artifacts/m7 --admission artifacts/m7/finalizer-admission.json
	@echo "✓ m7-gate (GA items 1–10 executed; items 11–12 honestly staged; evidence retained in artifacts/m7)"

# --- m8 packaged GA release gate (plan step 9 / task T11) ---
# With M8_EVIDENCE set, publish the supplied installed-lane bundle.  Without
# it, the script still builds exactly one installed wheel and retains the
# missing-evidence diagnostics under out/m8-gate; it never creates a ship file.
m8-gate:
	@if [ -n "$(M8_EVIDENCE)" ]; then \
		$(PY) scripts/reshape/m8_gate.py --gate --evidence "$(M8_EVIDENCE)" --artifact-dir artifacts/m8 --out-dir out/m8-gate; \
	else \
		$(PY) scripts/reshape/m8_gate.py --gate --artifact-dir artifacts/m8 --out-dir out/m8-gate; \
	fi
