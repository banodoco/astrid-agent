# M1-05a follow-up report

Verdict: READY under the requested no-new-failures rule. Both requested conditions are covered in one commit. The new focused tests pass on head; the two same-folder update cases reproduce Radagast F1 on the exact base. All head regression failures listed below also fail on base. No tests were weakened, skipped, xfailed, deleted, or removed from the selectors.

**Revision and scope**

- Base: `bda5bf40a835b25e29b7dd2567a7ea7537783ebf` (published immutable M1-05a head).
- Head: `ca370d21a3f4488ac075394bde2d130a59de61e8` on `packs-M1-05a-followup`.
- Changed files: `astrid/core/pack/transaction.py` and `tests/core/pack/test_transaction_source_contracts.py` only.
- One local commit, with a subject ending `[skip ci]`; no push.
- Base production code was checked out at the exact base in a task-local shared-object clone. Only the identical new focused test file was overlaid, so the new selectors could execute against unchanged base production code. Original regression tests were unmodified.

**Both conditions and implemented contract**

1. Folder updates are supported. With `revision=None`, an installed folder PIN stores a string URI for the selected pack directory. Base compares that string with `source_text=None`, causing `pin_mismatch`. Head compares the PIN with `_directory(Path(source) / subpath).as_uri()`, while retaining the origin check and the existing Git repository comparison. Tests perform a real folder installation and update, check the new payload and transaction revision, preserve unpinned PIN fields, and inspect the retired old generation. Cases include `subpath="."` and a nested directory containing spaces and `#`. Separate cases require `pin_mismatch` for another source root or another subpath and prove the installed generation is unchanged.
2. The frozen base has no function named `reconcile_defaults`; its default reconciliation boundary is `install_group(..., origin="default", intent="defaults")`. The existing contract **requires a full commit SHA even when `packs={}`**, but **requires no per-pack digests for an empty mapping**. Six cases pin missing SHA/ref/full SHA crossed with absent (`None`) or empty digest mappings. Missing SHA and `main` refuse with `invalid_pin` before publication; a full SHA permits an empty transaction without accessing the source. No production change was made to this contract.

**Radagast F1 re-check**

- Exact base: 2 collected, 0 passed, 2 failed, exit 1. Both failures are the predicted `pin_mismatch`.
- Head: 2 collected, 2 passed, 0 failed, exit 0. F1 is reproduced and fixed.

**Interpreter and immutable environment**

- Interpreter: `/root/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/bin/python3.13`.
- Version: `Python 3.13.14 (main, Jul 18 2026, 17:02:37) [Clang 22.1.3 ]`.
- Interpreter SHA256: `396f548d3fc6ef4f52a81b2c6b51bb3f00d3e60c82081e1a80f66cfeba90b939`.
- uv: `uv 0.11.32 (x86_64-unknown-linux-gnu)`.
- Dedicated venv: `/home/user/astrid/ws-M1/venv`, built once from the base locks before the final sequential base/head comparison. It contains 67 distributions. No editable Astrid install, ambient Python packages, or manual loose Python installs were used. Package manifests before/after each final side are byte-identical.
- The brief contains conflicting phrases “one ... venv for both sides” and “Do not share ... a venv between base and head runs.” The interpretation used was one task-dedicated environment, unchanged between sequential runs and not shared with other tasks; this was stated during work while clarification was pending.
- Binary-only installation failed because locked NumPy 1.26.4 has no usable CPython 3.13 wheel. A no-build-isolation check lacked `mesonpy`. The normal requested hash-verified uv installation succeeded by building the locked NumPy source distribution in build isolation; neither lock nor interpreter was changed. `uv pip check` passed for all 67 distributions.
- Runtime source: `1e6d899efa77a2d6ec247c38250f9748330bb587`, exactly `runtime.lock.json:source_commit` from the frozen base, explicitly selected for both sides. The admission parser selector used this same source. No unpinned newer Runtime was substituted.
- Node: `/opt/node22/bin/node`, v22.23.4, SHA256 `2286ce921a47349726b0e8b037ccbb6627043d24d1fc3c8f22c2dc53c4a1f7ff`.
- esbuild: 0.27.7, binary SHA256 `c638273fcf95573ca74af586677800b4dd874c55f8f945adb54316d6902ba14b`; API SHA256 `bce772de103e1cbb118f1b7313916caa81f0e85461b3b6e3531607e7ed75ec85` matches the production pin. Provisioned task-locally with install scripts disabled; exact npm lock retained in `logs/esbuild-package-lock.json`.

| Base lock | SHA256 | Git blob |
|---|---|---|
| `requirements/proof.lock` | `d0457889ab50ae929d590e099c58ff54ba76e356d15af9f142bd16106feb2a09` | `48d61c5ae026b084f24506a09f96a9eff34ac981` |
| `requirements/build.lock` | `0b3f194bcf699360b0645c15c73f5baa6124faaf323eaabf5b1bcce37915b2f0` | `fb5c495a611345f755cec0ee9a47f689c116ca55` |

**Exact provisioning commands**

All commands below ran from `/home/user/astrid/ws-M1/work/05a` unless a `-C` or `--prefix` is shown. `TMPDIR` and `UV_CACHE_DIR` were set before the successful venv creation and every install:

```bash
export TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/install-tmp
export UV_CACHE_DIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/uv-cache
uv venv --no-config --python /root/.local/share/uv/python/cpython-3.13.14-linux-x86_64-gnu/bin/python3.13 /home/user/astrid/ws-M1/venv
# Failed wheel-only preflight:
uv pip install --no-config --python /home/user/astrid/ws-M1/venv/bin/python --require-hashes --only-binary=:all: -r requirements/build.lock -r requirements/proof.lock
# Successful locked build-tool installation:
uv pip install --no-config --python /home/user/astrid/ws-M1/venv/bin/python --require-hashes -r requirements/build.lock
# Failed diagnostic source build without an isolated backend:
uv pip install --no-config --python /home/user/astrid/ws-M1/venv/bin/python --require-hashes --no-build-isolation -r requirements/proof.lock
# Successful normal source-capable locked installation:
uv pip install --no-config --python /home/user/astrid/ws-M1/venv/bin/python --require-hashes -r requirements/proof.lock
uv pip check --python /home/user/astrid/ws-M1/venv/bin/python
git clone --shared --no-checkout /home/user/astrid/ws-M1/work/05a /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/base
git -C /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/base checkout --detach bda5bf40a835
cp tests/core/pack/test_transaction_source_contracts.py /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/base/tests/core/pack/test_transaction_source_contracts.py
git clone --shared --no-checkout /home/user/astrid-runtime /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime
GIT_TERMINAL_PROMPT=0 git -C /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime fetch https://github.com/banodoco/astrid-runtime 1e6d899efa77a2d6ec247c38250f9748330bb587
git -C /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime checkout --detach 1e6d899efa77a2d6ec247c38250f9748330bb587
PATH=/opt/node22/bin:/usr/bin:/bin npm_config_cache=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/npm-cache /opt/node22/bin/npm install --prefix /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools --ignore-scripts --no-audit --no-fund --save-exact esbuild@0.27.7
cp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/@esbuild/linux-x64/bin/esbuild /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild
```

The source build and final comparisons held the host’s existing exclusive heavy semaphore through a read-only descriptor (`open("/home/user/astrid/ws-X/.heavy-command.lock", "rb")`, `fcntl.flock(..., LOCK_EX)`). This preserved the shared heavy slot without the host wrapper opening a path outside the write allowlist for writing.

**Focused and regression results**

| Side | Selector set | Collection-only count / exit | Run collected | Passed | Failed | Errors | Skipped | Run exit |
|---|---|---|---|---|---|---|---|---|
| base | focused | 10 / 0 | 10 | 8 | 2 | 0 | 0 | 1 |
| base | f1 | 2 / 0 | 2 | 0 | 2 | 0 | 0 | 1 |
| base | regression | 220 / 0 | 220 | 218 | 2 | 0 | 0 | 1 |
| head | focused | 10 / 0 | 10 | 10 | 0 | 0 | 0 | 0 |
| head | f1 | 2 / 0 | 2 | 2 | 0 | 0 | 0 | 0 |
| head | regression | 220 / 0 | 220 | 218 | 2 | 0 | 0 | 1 |

The F1 selector is a subset of the focused selector; do not add those counts together. The unique acceptance set is the 10 focused cases plus the 220 regression cases: base 230 collected, 226 passed, 4 failed; head 230 collected, 228 passed, 2 failed, with zero errors or skips on either side. Final collection was nonzero on both sides. Initial incomplete-environment startup attempts collected 0 on both sides and exited 1 with `/home/user/astrid/ws-M1/venv/bin/python: No module named pytest`; those are superseded provisioning failures, not passing test results.

**Exact test commands**

The following launcher contents are the literal commands executed, including all selected source paths, private HOME/TMPDIR, basetemp, JUnit output and selectors. Each invocation had its own launcher and sandbox. `--collect-only` invocations validate collection before the corresponding run; all original test and lane defaults are preserved.

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-focused-collect-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/base
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-focused-collect/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-focused-collect/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-focused-collect/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-focused-collect.xml --collect-only tests/core/pack/test_transaction_source_contracts.py
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-focused-run-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/base
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-focused-run/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-focused-run/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-focused-run/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-focused-run.xml tests/core/pack/test_transaction_source_contracts.py
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-f1-collect-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/base
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-f1-collect/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-f1-collect/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-f1-collect/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-f1-collect.xml --collect-only tests/core/pack/test_transaction_source_contracts.py::test_folder_update_matches_installed_string_source_identity
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-f1-run-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/base
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-f1-run/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-f1-run/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-f1-run/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-f1-run.xml tests/core/pack/test_transaction_source_contracts.py::test_folder_update_matches_installed_string_source_identity
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-regression-collect-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/base
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-regression-collect/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-regression-collect/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-regression-collect/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-regression-collect.xml --collect-only tests/core/pack/test_transaction.py tests/core/pack/test_transaction_artifacts.py tests/core/pack/test_cli_manage.py tests/core/pack/test_r8_prod_cli_discovery.py tests/core/pack/test_r8_prod_release_pins.py tests/core/pack/test_r8_prod_refusals.py
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-regression-run-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/base
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-regression-run/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-regression-run/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-regression-run/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-base-regression-run.xml tests/core/pack/test_transaction.py tests/core/pack/test_transaction_artifacts.py tests/core/pack/test_cli_manage.py tests/core/pack/test_r8_prod_cli_discovery.py tests/core/pack/test_r8_prod_release_pins.py tests/core/pack/test_r8_prod_refusals.py
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-focused-collect-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/work/05a
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-focused-collect/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-focused-collect/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-focused-collect/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-focused-collect.xml --collect-only tests/core/pack/test_transaction_source_contracts.py
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-focused-run-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/work/05a
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-focused-run/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-focused-run/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-focused-run/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-focused-run.xml tests/core/pack/test_transaction_source_contracts.py
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-f1-collect-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/work/05a
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-f1-collect/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-f1-collect/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-f1-collect/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-f1-collect.xml --collect-only tests/core/pack/test_transaction_source_contracts.py::test_folder_update_matches_installed_string_source_identity
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-f1-run-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/work/05a
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-f1-run/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-f1-run/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-f1-run/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-f1-run.xml tests/core/pack/test_transaction_source_contracts.py::test_folder_update_matches_installed_string_source_identity
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-regression-collect-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/work/05a
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-regression-collect/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-regression-collect/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-regression-collect/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-regression-collect.xml --collect-only tests/core/pack/test_transaction.py tests/core/pack/test_transaction_artifacts.py tests/core/pack/test_cli_manage.py tests/core/pack/test_r8_prod_cli_discovery.py tests/core/pack/test_r8_prod_release_pins.py tests/core/pack/test_r8_prod_refusals.py
```

`bash /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-regression-run-launch.sh`

```bash
#!/usr/bin/env bash
set -eu
cd /home/user/astrid/ws-M1/work/05a
exec env -i PATH=/opt/node22/bin:/usr/bin:/bin HOME=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-regression-run/home TMPDIR=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-regression-run/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PYTHONPATH=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_RUNTIME_ADMISSION_CHECKOUT=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/runtime ASTRID_BUILD_TEST_NODE=/opt/node22/bin/node ASTRID_BUILD_TEST_NODE_VERSION=22.23.4 ASTRID_BUILD_TEST_ESBUILD=/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/build-tools/node_modules/esbuild/bin/esbuild /home/user/astrid/ws-M1/venv/bin/python -m pytest -p no:cacheprovider -q -ra --basetemp /home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-regression-run/pytest --junitxml /home/user/astrid/ws-M1/queue/running/05a-followup/logs/qualified-head-regression-run.xml tests/core/pack/test_transaction.py tests/core/pack/test_transaction_artifacts.py tests/core/pack/test_cli_manage.py tests/core/pack/test_r8_prod_cli_discovery.py tests/core/pack/test_r8_prod_release_pins.py tests/core/pack/test_r8_prod_refusals.py
```

**Every head-failing node and its first error line**

- `tests/core/pack/test_r8_prod_refusals.py::test_real_cli_add_installs_python_requirement_and_runtime_executes_installed_action` — `astrid.core.execution.generic_host.HostError: capability 'alpha.use_pytest' child exited 1: Traceback (most recent call last):` (also fails on exact base).
- `tests/core/pack/test_transaction.py::test_rollback_restores_build_hook_metadata_with_the_pack_generation` — `subprocess.CalledProcessError: Command '['/home/user/astrid/ws-M1/venv/bin/python', '-c', '\nimport json, sys\nfrom runtime_protocol.store import InstalledPackAdmissions, read_pack_admissions\nreader = InstalledPackAdmissions(sys.argv[1])\nrevision, records = read_pack_admissions(reader, pack_id="alpha")\nprint(json.dumps({"revision": revision, "records": records}, sort_keys=True))\n', '/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-head-regression-run/pytest/test_rollback_restores_build_h0/Astrid/packs']' returned non-zero exit status 1.` (also fails on exact base).

**Base-failing nodes and first error lines**

- `tests/core/pack/test_r8_prod_refusals.py::test_real_cli_add_installs_python_requirement_and_runtime_executes_installed_action` — `astrid.core.execution.generic_host.HostError: capability 'alpha.use_pytest' child exited 1: Traceback (most recent call last):`
- `tests/core/pack/test_transaction.py::test_rollback_restores_build_hook_metadata_with_the_pack_generation` — `subprocess.CalledProcessError: Command '['/home/user/astrid/ws-M1/venv/bin/python', '-c', '\nimport json, sys\nfrom runtime_protocol.store import InstalledPackAdmissions, read_pack_admissions\nreader = InstalledPackAdmissions(sys.argv[1])\nrevision, records = read_pack_admissions(reader, pack_id="alpha")\nprint(json.dumps({"revision": revision, "records": records}, sort_keys=True))\n', '/home/user/astrid/ws-M1/queue/running/05a-followup/scratch/qualified-base-regression-run/pytest/test_rollback_restores_build_h0/Astrid/packs']' returned non-zero exit status 1.`
- `tests/core/pack/test_transaction_source_contracts.py::test_folder_update_matches_installed_string_source_identity[.]` — `astrid.core.pack.install.PackInstallError: Pack local_ext has a conflicting source PIN; update from its installed source.`
- `tests/core/pack/test_transaction_source_contracts.py::test_folder_update_matches_installed_string_source_identity[packs/local # folder]` — `astrid.core.pack.install.PackInstallError: Pack local_ext has a conflicting source PIN; update from its installed source.`

Verified Runtime failure details: the rollback parser import fails with `ImportError: cannot import name 'InstalledPackAdmissions' from 'runtime_protocol.store'` at the base-locked Runtime SHA (direct import diagnostic retained). The installed-action child fails with `ModuleNotFoundError: No module named 'runtime_protocol'` although the parent process explicitly selects the Runtime source checkout. These are observed on base and head; the installed-action proof is not qualified by this run. The newer Runtime 26ef6f9d used in Radagast’s joined review was not substituted for the frozen base’s lock.

**Findings**

- **BLOCKING — resolved:** Radagast F1, same-folder update with string source PIN incorrectly refused with `pin_mismatch`. Base reproduces both cases; head passes both and preserves refusal for a different folder.
- **DEFERRED — contract/documentation discrepancy:** The review’s F2 prose says empty groups skip the SHA/digest requirement. At the actual frozen base, SHA remains mandatory and per-pack digest requirements are vacuous for an empty group. Tests now pin that existing behavior. A `reconcile_defaults` helper is absent from this base; no new API was introduced.
- **DEFERRED — pre-existing regression failures:** Each head-failing node above is present in the exact-base failures. These were recorded and left unchanged, as required; no out-of-scope product fixes were made.
- No unresolved BLOCKING finding and no head-only failure in the requested narrow comparison.

**Artifacts, validation and cleanup**

- `git diff --check bda5bf40a835..HEAD` passed. Both changed Python files compiled successfully with the required interpreter. Base/head focused test file contents were identical (SHA256 `9f35d46fe08ffe49d9955769ce7634a5df5fc32f454f83ea718803a43062bbf8`).
- Bundle command and verification:

```bash
git bundle create /home/user/astrid/ws-M1/queue/running/05a-followup/M1-05a-followup.bundle bda5bf40a835..HEAD
git bundle verify /home/user/astrid/ws-M1/queue/running/05a-followup/M1-05a-followup.bundle
```

Verification succeeded: bundle contains `ca370d21a3f4488ac075394bde2d130a59de61e8` as HEAD and requires only `bda5bf40a835b25e29b7dd2567a7ea7537783ebf`; it is incremental.

- Appended exactly one READY-TO-PUSH row with this head, base and bundle path. No push was performed.
- Raw logs, JUnit XML, per-run launchers, package manifests, environment identities and tool lock remain under this task’s `logs/` directory. The scratch base/Runtime clones, Node tools/cache, uv cache/build output and all TMPDIR/basetemp/home sandboxes were removed after recording results. The explicitly requested dedicated venv remains at `/home/user/astrid/ws-M1/venv`.
