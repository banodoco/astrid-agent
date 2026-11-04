# U2/D135 validation

## PASS

- `git diff --check` — exit 0.
- Exact conflict-marker scan on the six conflict paths — no markers found.
- `NODE_PATH=/home/user/work/runtimes/agent-remotion-current/node_modules bun run build:chat` — exit 0.
- `NODE_PATH=/home/user/work/runtimes/agent-remotion-current/node_modules bun run check:chat-bundle` — exit 0.
- `PYTHONPATH=/home/user/work/wt/agt-U2-D135-private-join-1953:/home/user/work/runtime PYTHONDONTWRITEBYTECODE=1 /home/user/work/venv-agent/bin/python -m pytest -q -o addopts= -p no:cacheprovider --timeout=45 tests/sdk/test_host_bootstrap_source_identity.py tests/test_installer_omp_source.py tests/core/generation/test_gpu_environment.py tests/test_installer_platforms.py` — exit 0, 244 passed in 3.91s.
- `test ! -e node_modules && ln -s /home/user/work/runtimes/agent-remotion-current/node_modules node_modules && realpath node_modules && test "$(realpath node_modules)" = /home/user/work/runtimes/agent-remotion-current/node_modules && bun test ./astrid/chat; rc=$?; rm node_modules; exit $rc` — exit 0, 195 passed, 0 failed, 428 expect() calls, 108.48s. The dependency symlink was temporary and removed.

## Baseline or environment failures retained honestly

- `/home/user/work/venv-agent/bin/python -m ruff check <all changed Python files>` — exit 1, 76 findings across the merged source/test scope. No ignore or weakening was used; findings include existing configured-scope issues and upstream additions.
- `/home/user/work/venv-agent-typecheck-v2/bin/python -m mypy <all changed Python files>` — exit 1, 74 errors in 11 files, 43 files checked. No ignore or acquisition was used.
- The first direct Bun test invocation without the temporary local dependency link failed during module resolution (`Cannot find package 'zod'`); the authorized existing dependency tree resolved it without installation or modification.

Full/core suites and delivery/ZIP gates were not run, per task instructions.
