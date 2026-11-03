# P-92 exact-base classification r2 — RESULT (Elrond)

Verdict: CLASSIFIED. Both sides fail identically (same 2 nodes, same first error line). Both findings are DEFERRED, pre-existing on the exact base. No regression: candidate adds no failure and fixes none.

## Sources and tuple
- Agent base: ceed0cd46426c9619d055881b5736183c381ab42 (checked out detached).
- Agent candidate: 4c42b13a7e9c9f8824a4e952e404a88431d2d729 (checked out detached).
- Runtime: 1a275bb339ae897e52a5be5e1da99e846e402037 (matches base runtime.lock.json source_commit).
- VibeComfy: 2967033970cbb5884651c9565265778482d3b44e.

## Interpreter and lock identities
- One identical lock-built venv for both sides: /home/user/astrid/p92/venv, interpreter Python 3.13.14 (cpython-3.13.14 from uv).
- Built from base requirements/proof.lock (sha256 6f6e3843fbb39d91e80d5be952cb3d589053503541b61202d341f9757902c110) and build.lock (sha256 0b3f194bcf699360b0645c15c73f5baa6124faaf323eaabf5b1bcce37915b2f0), uv pip install --require-hashes. Runtime and VibeComfy installed --no-deps from the pinned checkouts. runtime.lock.json sha256 0e887754b5b6daa3ddb326ca3d8bc7c5c51e299f406b1dbf588ba6e8ddd4d972.
- Versions: pytest 9.0.2, httpx 0.28.1, wheel 0.46.3, build 1.4.0.

## Command and environment
- Under /home/user/astrid/ops/heavy.sh, env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=p92/home TMPDIR=p92/tmp PYTHONDONTWRITEBYTECODE=1.
- Each side from its worktree root: python -m pytest -q -m integration tests/core/generation/test_generate_target_journey.py
- Run sequentially: base first, then head.

## Counts
- Base: 15 collected, 13 passed, 2 failed, exit 1 (91.44 s).
- Candidate: 15 collected, 13 passed, 2 failed, exit 1 (88.73 s).

## Head-failing nodes (identical on base)
- tests/core/generation/test_generate_target_journey.py::test_public_h3_local_inputs_reach_tracked_runtime_parent — ImportError: cannot import name 'process_binding' from 'runtime_protocol'.
- tests/core/generation/test_generate_target_journey.py::test_public_image_command_registers_view_bytes_and_canonical_receipt — ImportError: cannot import name 'process_binding' from 'runtime_protocol'.

## Findings
- DEFERRED: both failures are an ImportError of process_binding from runtime_protocol. The pinned Runtime 1a275bb does not define process_binding (grep of the checkout and of the installed package finds nothing). The base test expects a symbol the base's own pinned Runtime lacks. This is a pre-existing tuple/test mismatch on the exact base, present before the candidate.
- No BLOCKING finding for this classification.

## Preflight and disk
- Free disk at start about 20 GB (above the 4 GiB floor). End free 20G.
- No pod was run. No GPU, no provider call.
