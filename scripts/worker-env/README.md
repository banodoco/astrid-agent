# Worker environment

Setup and test scripts a single-repo cloud worker runs from this checkout's root. They live in this repository, so a worker runs its own repository's code rather than code cloned from elsewhere, which a cloud session's auto mode refuses.

- `bash scripts/worker-env/agent-setup.sh` builds `<checkout>.env/` next to the checkout. It creates a Python 3.13 venv with `-e .[dev]`, then git-fetches the Runtime (installed `--no-deps`) and VibeComfy at this checkout's pins. Nothing downloads a GitHub archive.
- `bash scripts/worker-env/agent-pytest.sh -q <selectors>` runs pytest in that environment.
- `bash scripts/worker-env/agent-pytest-pod.sh <selectors>` runs the pod-path harness. It needs `<checkout>.env/lifecycle` at the pinned runpod-lifecycle commit.
- `bash scripts/worker-env/smoke.sh agent <one selector>` runs setup plus one test, and prints ENV-PASS or ENV-FAIL.

The canonical copies are in astrid-dev `env/`; keep the two in step.
