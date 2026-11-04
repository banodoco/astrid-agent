# Reproducible release inputs

Astrid keeps publishable dependency ranges in `pyproject.toml`, but the release
gate never resolves those ranges directly. Release builds, installed-wheel
smokes, and factoring proofs use three universal, SHA-256-verified locks:

- `requirements/build.lock` pins the PEP 517 builder, backend, wheel writer,
  and their transitive dependencies. The canonical wheel is built with
  `--no-isolation` only after this lock is installed with `--require-hashes`.
- `requirements/runtime.lock` pins every public runtime dependency and
  transitive dependency for the supported CPython 3.11/3.12 Linux/macOS
  matrix. The wheel is then installed with `--no-deps --no-index`, followed by
  `pip check`, so wheel metadata cannot trigger a second resolution.
- `requirements/proof.lock` pins the runtime closure plus the test runner and
  timeout plugin used by source-copy and installed-wheel factorability proofs.
  Those proofs provision a disposable interpreter from this lock; they never
  use host or user-site packages.

Refresh and validate the locks deliberately:

```sh
make lock-build
make lock-runtime
make lock-proof
make lock-validate
```

Lock refresh requires `uv`; lock consumption requires only pip. Review every
version and hash diff like source code. Never hand-edit one pin without
regenerating the complete transitive lock.

`make lock-validate` also proves the runtime lock is binary-installable at
CPython **3.11.13**. It provisions a disposable interpreter and uses `uv pip
install --dry-run --require-hashes --only-binary :all:` against an empty target
for each supported release target: macOS arm64 (`aarch64-apple-darwin`), macOS
x86_64 (`x86_64-apple-darwin`), Windows x86_64
(`x86_64-pc-windows-msvc`), Linux x86_64 (`x86_64-unknown-linux-gnu`), and
Linux arm64 (`aarch64-unknown-linux-gnu`). Any source-only pin, missing wheel,
or hash mismatch fails the lock check. This is lock-resolution evidence, not a
hosted macOS or Windows installation receipt.

## Installer source pins and VibeComfy readiness

The installer installs VibeComfy with `--no-deps` beside the Runtime and agent.
The published `install-manifest.json` may pin a `vibecomfy` source entry; when
it is absent, the installer uses `peteromallet/VibeComfy` at
the revision in `VIBECOMFY_ENGINE_REVISION`
(`astrid/core/generation/vibecomfy_dependency.py`). Before publishing this release,
the release owner should:

- Publish the agent commit and confirm the pinned VibeComfy commit is
  reachable from its upstream repository.
- Add the VibeComfy SHA to the published `install-manifest.json`. Refresh the
  agent and Runtime pins for ordinary releases as well.
- Qualify a clean install and a normal `--update`, confirming the pack host
  imports the installed VibeComfy and `vibecomfy.run` reports package readiness
  without an additional service restart. Repair retains installed code, so it
  does not add VibeComfy to older installations; those users need `--update`
  from a terminal outside chat.

Dependency audit for the pinned VibeComfy revision: `httpx==0.28.1`,
`pyyaml==6.0.3`, and `websockets==16.1.1` are satisfied by the Astrid runtime
closure. `huggingface_hub[hf_xet]>=0.32.0` is declared in `pyproject.toml`;
the runtime and proof locks include `huggingface-hub==2.3.0`, `hf-xet==1.7.0`,
and their transitive dependencies with hashes. Regenerate both locks when
changing these requirements, then run `make lock-validate`. See the
[fresh-install qualification](../testing/fresh-install-vibecomfy.md) for results.

The m8 installed matrix also writes `astrid.release_toolchain.v1` evidence.
It records the exact CPython patch and executable, GNU Make, Bash, Git, FFmpeg,
FFprobe, and dependency-lock digests. A missing tool, Python-series mismatch,
or range/unhashed dependency fails before the installed lanes can be accepted.

## Remotion adapter provisioning

The optional Remotion adapter is a separate, lockfile-owned Node closure. A
clean checkout must use Node **20.19.4** and npm **10.8.2**, recorded in the
repository `.node-version` and enforced by `remotion/package.json` plus
`remotion/.npmrc`. After installing that toolchain, run from the repository
root:

```sh
python3 scripts/reshape/remotion_gate.py all
```

The gate runs `npm ci` (never `npm install` or `npx`) from `remotion/`, checks
free space before and after installation, generates the renderer types, runs
`npm run typecheck`, and executes the blocking renderer-parity selector. It
refuses to start the install below 2 GiB free and does not provision or commit
`node_modules/`. CI uses the same exact Node/npm versions and lockfile.
