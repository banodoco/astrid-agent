# Plan 87 tracked render experiments

These stopgaps are present for exploratory renders. Do not offer this branch for merge while this list is non-empty.

- `b051e8cc` (ported as `0639b752b`): requests a 7,200-second heartbeat lease and extends the lifecycle client timeout; the initial claim is unchanged and the generated Runtime client is modified. RN-8 replaces and removes this experiment.
- `7265aa5d` (ported as `7c21eaa6f`): waits before custody retry; it does not cover the observed `pid_for_task` failure. RN-9 replaces and removes this experiment.
