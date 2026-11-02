# R-24 Linux runner invocation boundary

This is incomplete venue wiring, not a hosted receipt or qualification. The
exact producer is `6bf95fd631864a5dbfa86df5ffc165493fffc41e`; its product
candidate is separately pinned in `ci_journey_r24_hook.py` and differs from the
current Release-1 tuple. Do not replace one tuple with the other.

On the Linux runner that owns the real pinned daemon and an installed managed
Runtime with a genuinely active task, provision the five clean source checkouts,
PTerm and daemon dependencies described by the producer's `R24-LINUX.md`. Put
the exact producer configuration in `CONFIG`. Then:

```
python3 -B scripts/ci_journey_r24_hook.py --producer-root /absolute/producer \
  --config /absolute/CONFIG --output /absolute/new-bundle
```

The hook validates the producer commit and five source hashes, creates a typed
request with a fresh run ID, and invokes the producer with its new `export`
child. It retains the canonical request and configuration, producer stdout and
stderr, outer exit code, unverified Actions environment, and hashes in
`receipt.json`. On success it checks the exact six export files, their manifest,
typed stdout/result, observations, matching producer session, configured engine
identity, and action request IDs. It leaves producer export bytes unchanged.
The process exits nonzero even after a structurally valid export, and reports
`AWAITING_EVIDENCE`: this interface must not be consumed as a slot receipt.

The immutable producer discards the inner action's stderr, and reports only a
generic error if the action fails; it exposes neither its exact failure exit
code nor the raw inner result as an independently retained file. Its projected
observations do not replace that missing interface. The Actions environment is
not authenticated, and there is no same-job artifact upload or independently
verified job/artifact ID, archive digest, or hosted receipt binding here. The
base workflow does not provision or invoke this hook. A hosted materializer
must provision a real active Runtime task and the exact engine; it must not use
fixtures as observations. Producer and hosted-custody interfaces must be
completed before enabling a successful route or making a qualification claim.
