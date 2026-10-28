# Canonical engine release and volume startup

This path packages ordinary complete Astrid, Runtime, lifecycle and SDK wheels,
plus an explicit wheel inventory of their dependencies. It runs the existing
installed `astrid.console:main` entrypoint. It contains no source, environment,
model or engine installer of its own. Engine ownership remains with
`engine_custody.LinuxEngineServiceClient`; the original prepared lifecycle owner
alone owns pod admission and release.

The current frozen sources do **not** form a complete release: the supervisor,
resolved Linux lock and dependency wheels must be supplied and verified. A
`--fixture` archive permits only the exact canonical dry-run below and always
refuses startup. Passing these CPU tests does not qualify installation, an
image, a pod or generation.

## Build and freeze

Build each whole product wheel normally from its frozen checkout, using the
repository build requirements and the existing build interpreter. Do not copy
selected modules or invent package initializers. Freeze each base commit plus
all additional patches; keep their exact bytes and SHA256s. Build-tool versions,
wheel hashes and source-to-wheel provenance must be reviewed by the producer.
The builder binds the producer's source records to the supplied wheel bytes;
it does not certify reproducible source-to-wheel compilation.

`engine_release.build_release(wheels, provenance, output)` performs an ordinary
offline `pip install --no-deps --no-index --target` in an owned staging directory.
It inventories the entire installed tree. Provenance is a JSON object keyed by
`astrid`, `astrid-runtime`, `runpod-lifecycle` and `runpod`. Each value contains
`commit` (40 hex), `patches` (ordered `{name,sha256}` records), and `wheel_sha256`.
Every dependency wheel is separately inventoried by filename/name/version/SHA.

The default build rejects missing required modules/distributions, unsatisfied
metadata dependencies (including requested extras), direct dependencies without
a reviewed wheel mapping, unresolved Linux requirements, invalid packaged-lock
hashes, or failure of the isolated canonical CLI/runtime import check. That
check validates packaged locks, imports the actual native/context/custody modules
and checks the agreed client methods without starting a service. Real finite
helper execution/source retention/shutdown remain supervisor integration gates.
Full product metadata is
checked conservatively, so this first implementation can require more wheels
than a future reviewed engine-only dependency inventory. It does not silently
omit metadata dependencies to save space. A resolved requirements list must
still be produced and independently checked by the canonical lock owner.

The release records its exact build Python implementation/version and OS/machine;
the launcher refuses a different interpreter/platform before product imports.
Build using the independently verified target bootstrap interpreter, rather than
assuming native dependency wheels are portable across Python versions.

The resulting `sha256-<archive-sha>.zip` contains `release.json`, `run.py` and
`site/` with full installed-wheel files. The inventory hashes every payload file;
the externally frozen archive SHA covers the inventory itself. This is a release
file inventory, not another generation manifest or readiness authority. No
inline source envelope size target is used. The stdlib verifier bounds extraction
to 1 GiB and 50,000 regular files; those are application bounds, not provider
limits. It rejects links, duplicates, traversal, member-set mismatch and altered
bytes. Content addressing is verified immutability, not a storage WORM claim.

## Carrier and verification

The supported carrier is an **already staged regular archive file** on the
existing volume. This code does not fetch URLs, use AWS/S3, upload, publish, read
credential files, or create a provider claim. A future approved immutable URL
must be staged by a separately reviewed bounded transport before this path is
used; no URL or delivery availability is assumed here. RunPod S3 credentials are
separate from RunPod API credentials and must never be sent to the pod.

With a reviewed trusted copy of the stdlib verifier, extraction is:

```sh
/usr/bin/python3 -I -S /trusted/engine_release.py extract \
  --archive /workspace/astrid/artifacts/sha256-<FINAL_SHA>.zip \
  --sha256 <FINAL_SHA> --retain /workspace/astrid/releases
```

This verifies archive bytes before extraction and retains the tree at
`/workspace/astrid/releases/sha256-<FINAL_SHA>`. Every subsequent extraction
verifies both the archive and the existing entire tree, including the original
manifest bytes. Do this before each launch. Never remove that archive or tree
while any installation/session/helper uses it. The retained launcher's second
check verifies every file before importing product modules.

For a labelled incomplete fixture, the only admitted product command is:

```sh
/usr/bin/python3 -I -S <retained-tree>/run.py cli \
  engines install vibecomfy-h3 --placement local --dry-run
```

The actual installed entrypoint and full original package initializers are used,
with no controller site-packages or `PYTHONPATH` imports.

## Explicit provider startup argv

`engine_release.startup_argv(...)` returns the exact argv for a provider
**entrypoint override**, without contacting the provider. It begins:

```text
/usr/bin/python3 -I -S -c <exact stdlib engine_release.py source> start
  --archive /workspace/astrid/artifacts/sha256-<FINAL_SHA>.zip
  --sha256 <FINAL_SHA> --retain /workspace/astrid/releases
  --volume /workspace --root <existing-canonical-install-root>
  --attempt <new-authorized-claim-attempt> --seconds 3600 --step-seconds 1800
```

The source is the trusted small verifier, not an inline product bundle. The
product code/dependencies/locks all come from the one archive. The verifier
needs no preinstalled Astrid. It verifies/extracts, refuses incomplete fixtures,
then executes the retained `run.py startup` under the release's managed
interpreter (see below); bootstrap time is charged to `--seconds`.
Provider JSON arrays are `dockerEntrypoint = list(argv[:4])` and
`dockerStartCmd = list(argv[4:])`. Freeze the resulting exact arrays and verifier
source SHA before dispatch. Do not infer that `dockerArgs` alone overrides an
unknown image ENTRYPOINT. The current lifecycle GPU create API has no reviewed
P2 override selector; its original owner must wire/review this argv at that seam.
This document supplies a command, not that integration or authorization.

Preserve the selected image exactly:
`runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404`.
Do not substitute a generic PyTorch image or assume the tag's CMD/ENTRYPOINT.
Its immutable digest/runtime contents remain unqualified; the recorded registry
429 means unknown, not absent. The explicit override, Python path/version,
Linux x86_64/CUDA 13 image/driver, CA/TLS, git, system pip and the recipe's pinned
uv/managed Python bootstrap must be verified. An absolute `/usr/bin/python3`
is an explicit prerequisite in this example, not a claim about that image; set
`python=` to the independently verified executable when freezing argv. Bash is
needed only if the original provider path explicitly uses a shell.

## Managed interpreter handoff

A complete release records its build interpreter, which must equal the H3
recipe's managed Python (`python_version` 3.12.10, Linux x86_64) taken from the
hash-indexed recipe inside the verified tree. When the image's `python3` patch
version differs, the stdlib verifier does not relax that identity. After the
archive digest, member set, every retained file hash and `complete=true` are
verified, and before any product import, it runs the recipe's own bootstrap
commands from the retained `_engine_python_bootstrap.py` (the same helper the
Linux platform adapter uses): `python3 -m pip --version`, the pinned
`uv==<uv_version>` prefix install, `uv --version`, `uv venv --allow-existing
--python <python_version> --managed-python <root>/runtime/venv`, then a live
identity probe of `<root>/runtime/venv/bin/python`. Only an exact identity match
execs that interpreter on the same retained `run.py`, whose launcher repeats the
exact manifest check. A matching running interpreter is used directly. A wrong
platform, incomplete fixture, recipe/manifest mismatch, missing pip, wrong uv,
wrong managed version or exceeded bound fails before exec. No resolver, source,
model or engine step runs here; the canonical runner still owns installation.

Operations with the same verifier source (`-I -S -c <source>`):

```text
prepare-python --archive A --sha256 S --retain R --root ROOT [--seconds N]
  -> one JSON line {schema, python, release, run, bootstrapped, identity}
cli --archive A --sha256 S --retain R --root ROOT [--seconds N] -- <canonical CLI args with --root ROOT>
  -> exec <python> -I -S <release>/run.py cli <args>
```

`engine_release.release_cli_argv(...)` builds the `cli` argv for one existing
authenticated agent job. Bootstrap children stay in that job's process group:
the original job's cancel/timeout signals reach them, and each child is bounded
by the remaining `--seconds`. They receive only an allowlisted environment
(`PATH`, `HOME`, `LANG`, `LC_ALL`, proxy and CA variables) plus the recipe overlay;
`DEB_PYTHON_INSTALL_LAYOUT=deb` is added for the system `pip --prefix` command
only. No token, provider key, SSH or inherited `UV_*`/`PYTHON*` variable passes. The bootstrap is idempotent and not install-locked; one job
per root is the caller's ownership rule, and the runner later re-verifies uv
and the environment under its install lock. A failed handoff leaves only the
recipe's own `runtime/bootstrap` and `runtime/venv`; rerun the same job command.

## Sequence, ownership and evidence

Startup requires the existing claim's `ASTRID_POD_TOKEN` in its dedicated env
input; it consumes it and gives it only to the existing standalone token agent.
CLI children receive an allowlisted public environment with no inherited token,
provider/AWS key, proxy credentials, Python path or user startup variables.
The agent uses the existing observation policy and port 8000: health plus curated
closed files/canonical receipt only. No extra service, auth route or public
ComfyUI port is introduced.

The wrapper starts the canonical CLI **before** launching the separate observer.
It does not request or wait for agent health. Exactly one sequence runs:

1. `astrid engines install vibecomfy-h3 --placement local --platform linux-x86_64-cu130 --root ROOT --deadline REMAINING`
2. The same installed entrypoint with `engines status` and the same selection.
3. The exact install again; every returned step must be `skipped` and `passed`.

Each is bounded by the smaller of the step and total remaining budgets. Exit 0
alone is insufficient: the existing strict receipt parser checks identity and
healthy installed evidence; install output must match the canonical receipt.
The wrapper never fabricates an install receipt, retries a failure, resolves
locks, clones engine sources or downloads models. Native Runtime process handles
stop/drain finite CLI children on timeout/cancel. The shared custody supervisor
alone starts/retains/stops the engine. On shutdown the wrapper calls its agreed
`stop_owned(deadline=...)`; it never claims provider cleanup. Shutdown writes
`session_stop=stopped` only after that call returns; a custody failure records
`session_stop=unresolved`, failed status and its existing validated recovery handle.
An attempt refused before installer launch cannot stop an existing session. The local receipt
remains `qualified=false`; B1/Runtime admission and actual HTTPS engine calls are
still required before generation.

For attempt `A`, public files live in `/workspace/astrid/startup/A/`:

- `status.json`: atomically replaced sequence state, bounded child results,
  canonical receipt path/hash and snapshot references, log/truncation facts.
- `first-install.json`, `status-install.json`, `rerun-install.json`, and on
  failure `failure-install.json`: canonical receipt observations when present.
- `first-result.json`, `status-result.json`, `rerun-result.json`: validated CLI
  result documents, atomically published after successful validation.
- `stdout.NNNNNN.log` / `stderr.NNNNNN.log` with SHA256 sidecars: only closed
  segments, at most 1 MiB each and 8 MiB retained per stream for the entire
  attempt. Excess bytes are drained and counted. Active segments remain under
  the private path until atomic publication.

Private scratch is `/workspace/astrid/private/startup/A/`, outside the agent
allowlist. An existing attempt is refused; no age-based reset or automatic
cleanup occurs. The canonical receipt remains **ROOT/.astrid-engine/install.json**.
All status/log/receipt paths are under the existing volume independently of
agent health. The existing volume-read transport can retrieve them even if the
agent never becomes healthy, once separately authorized volume credentials and
transport are available. No independent receipt retrieval is qualified here.

The wrapper remains for the bounded original lifetime after successful sequence,
so the single custody session can serve generation; reserve extra controller
teardown time beyond `--seconds`. Startup install/status success is not final
engine HTTPS/generation readiness. The parent lifecycle owner must still compare
exact claim/pod/volume/DC/image/rate immediately after create and release on
mismatch under the existing admission rules. A local mount check does not prove
provider volume identity.

## Recovery after verified old-pod destruction

Use the original owner and its existing release/reconciliation record. Before
any ephemeral cleanup, require the provider's **exact old-pod GET 404**, bound to
the original account/claim operation ID and preserved volume ID/mount, plus the
original owner's confirmed release record. An empty list, DELETE acceptance,
old timestamp, missing PID, free flock or missing socket is insufficient.
If the old pod is live or unknown, stop and reconcile that same owner; never
start a replacement from local guesses.

After that evidence and a new authorized pod's exact same-volume attachment are
verified, the original owner may remove **only the old pod/incarnation's explicit
private session/socket/attempt scratch identified by Sol's supervisor**. Do not
infer its filenames or delete `.astrid-engine` wholesale. Preserve the canonical
receipt as old evidence, the immutable archive/extraction, every model/cache byte,
pinned source checkout and resolved lock. Preserve public observations for review.
No cleanup function or broad deletion command is provided here because the
supervisor's concrete ephemeral paths are not yet delivered. On the new attempt,
run this same retained artifact and canonical installer to rebuild current
receipt/health; the canonical downloader rechecks model size/SHA before reuse.
No PID adoption, crash journal, age reset, lock bypass, or second lifecycle owner
is added. Owner loss on a still-live pod requires an explicit service/environment
stop through the original authority, not this old-pod-destruction procedure.
