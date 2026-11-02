# Source-union verification logs

This directory republishes five raw logs from source task `AG-CI-SOURCE-UNION-E425-VERIFY-R1` at `queue/done/AG-CI-SOURCE-UNION-E425-VERIFY-R1/`. The source receipt is `REPORT.md` with SHA-256 `465d2a3435b91696d7135b7643fb62d537ed52fe27f1f2e95c2f46d780a41526`; the source task's `SHA256SUMS` lists each log's expected hash. All five source files matched that manifest before copying; `SHA256SUMS` here records the destination files.

## Exact source tuple

- Agent: `e425903f7a9fa5b82973281d15054c0716b46e2d`
- Runtime: `5453366458f7b4ee874c94d15ae7952e3e82d3c3`
- Source-union candidate: `56d01856f65753a58e21538ae8f675a75455fe6a`

## Logs

- `pip-dev-install.log`: Agent development editable install output.
- `runtime-install.log`: published Runtime editable install output.
- `source-identity-tests.log`: focused source identity test output on the exact Agent/Runtime tuple.
- `candidate-a-passing-tests.log`: readiness, storage, Timeline differential, and CUDA shared-list focused rerun output.
- `collection.log`: named collection selection output.

These are raw source-task logs, not reruns. The source receipt records the identity selection as exit 1, focused passing selection as exit 0, and collection selection as exit 0.
