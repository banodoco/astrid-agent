# R-24 Linux runner hook

This opt-in hook is independent of the journey matrix and does not change its
assertions or gate. It is not an M4-6 receipt. It uses the typed consumer from
Agent `81aeac01879a2ba1b7fe0fda68778d6ba0c2d9ff`; its only local API extension
is an optional capture callback recording the producer process streams, status,
and timeout before success parsing. Candidate, Stop and all four producer files
are pinned by that consumer. The producer must be a separate, clean Git checkout
at `008c2eeee9bcb123daad3071494360920848577d`.

Run on a Linux Actions runner with the normal GitHub environment and a real,
runner-local producer config as defined in the producer's `R24-LINUX.md`:

```
python3 scripts/ci_journey_r24_hook.py --config /absolute/path/config.json \
  --producer-root /absolute/path/producer --output /absolute/path/new-bundle
```

The config must name distinct clean candidate checkouts, the pinned PTerm source,
the local Node executable and dependency hashes; the native daemon must already
be available as specified by the producer. The diagnostic-only M-VENUE
materializer plan is not this configuration. The bundle directory must not exist
and should be outside all checkouts. Inputs and stdout/stderr are stored as raw
bytes; `bundle.json` indexes their SHA-256 hashes, the canonical request/config,
Actions environment identity, a fresh session nonce, the request's random run
ID, producer exit status and timeout. A nonzero hook exit is a rejection. The
producer process status is not the inner Stop-client status; an inner status is
available only if the producer includes it in its diagnostic. A crash or forced
termination can leave an incomplete bundle, which must be rejected.

On a hypothetical zero exit, the typed result must bind to the exact request and
its three evidence digests. Supply `--evidence-dir /absolute/path/evidence` with
nonempty `busy.bin`, `unknown.bin`, and `restoration.bin`; all must match those
digests. These caller-supplied files do not authenticate the producer or their
origin. The current producer has no evidence export and its collector always
returns unavailable, so it cannot currently produce this structural success.

`structural-result-only` is never a qualification decision. GitHub environment
variables and the local nonce are self-reported and do not prove Actions job or
producer-internal session identity. The hook has no authenticated artifact
upload, immutable storage, signature, OIDC attestation, collector provenance, or
binding to the producer's private session. Its hashes permit later integrity
comparison only if a trusted party independently protects and authenticates the
bundle. M-PIN must supply an authenticated collector and exported evidence;
M-QUAL must specify and verify the runner/session binding and artifact custody
before using anything as qualifying evidence. No workflow or gate consumes this
bundle here.
