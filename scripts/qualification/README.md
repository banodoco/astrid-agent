# H3 installed qualification preparation

These two tools prepare plans for a later authorized installed session. They do
not install Astrid, activate a service, execute contracts, or qualify source
Python.

```bash
python scripts/qualification/h3_r8_preflight.py --dry-run
python scripts/qualification/h3_g0_capture.py --dry-run
```

Both commands print deterministic JSON to stdout and create no files. To have the
preflight validate an actual product prefix, supply `--prefix` and a JSON tuple
record containing exactly `agent`, `runtime`, and `vibecomfy`. The Runtime value
must come from a new M-GPU note; until then it is the required literal
`REQUIRED_FROM_NEW_MGPU_NOTE`. A placeholder never qualifies an installed
prefix or permits future execution.

The preflight checks installer ownership, the installed manifest tuple, the venv
executable, product site-packages, and existing package RECORDs using filesystem
metadata only. It prints all five contracts as `NOT RUN` and their mapped
entrypoints, prerequisites, and command templates. This preparation runner
does not execute those commands. The future worker-profile and internal Runtime
contract drivers still need to be supplied from the authorized canary runbook.

The G0 planner enumerates every P1–P12 row that the G0 report marks
`UNCAPTURED`, and carries forward only command vectors actually present in the
retained G0 command index. It does not open or hash media in dry-run. Although
`--capture` is available as an explicit opt-in, it currently fails closed:
P3–P10 and P12 lack a reviewed installed-session command/input mapping, and the
known P1/P2/P11 vectors cover only part of those rows. The old G0 harness and
comparator remain evidence references, not reusable installed-session proof.
Do not treat this plan as a complete `h3-capture/v1` packet.
