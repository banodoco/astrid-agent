# Release-1 bridge fixture package

This directory is Agent-only implementation evidence.  `worker.py` is a
controlled subprocess boundary supplied to `scripts/bridge_fixture_executor.py`;
it is not a hosted worker, release artifact, qualification run, or acceptance
claim.

The scenarios and records are file-backed.  The release-1 manifest fixes Node
`20.19.4`.  `release0-adoption.json` points at the existing byte-preserved
`tests/fixtures/release0` capture and its registered-service artifacts; it does
not copy or reserialize those inputs.  The release-0-skip scenario consumes the
real files and retains them under the worker data root for a release-2 fixture
observation.

The executor requires a real child process, exact argv and provenance, durable
job/journal/lock files, exact route observations, and operation identity
bindings.  It fails closed on synthetic/source-only tags or missing evidence.
It emits precise `NEEDS:` metadata for product-settled wire details that remain
outside this fixture package.
