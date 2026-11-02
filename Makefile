# Astrid local gate — run before pushing to catch CI/deploy failures locally.
#
#   make check   blocking pre-deploy gates, including renderer parity + Remotion typecheck
#   make ci      check + editable import + wheel-install smoke + the default pytest lane — minutes
#
# `make check` green ≈ the CI "Python quality gates" deploy job will pass its fast gates.
# These run the SAME scripts CI runs (see .github/workflows/ci.yml), so they stay in lockstep.

PY ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

.PHONY: help test check ci preflight structure doc-refs doctor ruff mypy cycles remotion-install remotion-typecheck renderer-parity wheel editable lock-build lock-runtime lock-proof lock-validate toolchain-record

help:
	@echo "make check   - blocking gates: structure, doc-refs, doctor, ruff, mypy, cycles, Remotion, renderer parity"
	@echo "make ci      - check + editable + wheel-install smoke + default pytest lane (minutes)"
	@echo "make lock-runtime / lock-build / lock-proof - refresh universal SHA-256 dependency locks with uv"
	@echo "make lock-validate - validate exact pins, hashes, and direct dependency coverage"
	@echo "make <gate>  - run one gate: structure | doctor | ruff | mypy | cycles | remotion-install | remotion-typecheck | renderer-parity | wheel | editable"
	@echo "make preflight - verify Python, Node, ffmpeg, and locked Remotion prerequisites"

# --- Fast gates: catch the common deploy blockers in seconds. Run before every push. ---
check: preflight structure doc-refs doctor ruff mypy cycles remotion-typecheck renderer-parity
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
	@$(PY) -m scripts.reshape.compare_structure_baseline
	@echo "✓ repo structure: no violation beyond the shrink-only baseline"

doc-refs:
	@$(PY) scripts/reshape/check_doc_references.py
	@echo "✓ doc-refs (agent docs match the registry, gateway parser, and generated catalog)"

doctor:
	@$(PY) scripts/reshape/ci_doctor.py
	@echo "✓ doctor (deploy health gate)"

ruff:
	@$(PY) -m scripts.reshape.compare_ruff_baseline
	@echo "✓ ruff baseline (no lint regression)"

mypy:
	@$(PY) -m scripts.reshape.compare_mypy_baseline
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

# --- Slow local gates (adds the wheel-install smoke). Run before a release / when in doubt. ---
ci: check editable wheel test
	@echo "✅ make ci: check + editable + wheel-install + default test lane passed"

test:
	@$(PY) -m pytest -q

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
	@$(PY) -c "from scripts.reshape.release_reproducibility import validate_dependency_locks; validate_dependency_locks(); print('✓ hashed dependency locks and runtime wheels')"

toolchain-record:
	@$(PY) -m scripts.reshape.release_reproducibility --output out/release-toolchain.json >/dev/null
	@echo "✓ release toolchain recorded in out/release-toolchain.json"
