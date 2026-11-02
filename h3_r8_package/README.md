# Frozen H3 R8 content packaging

Task 94 implements M-GPU's 2026-11-02T23:17:29Z amended acceptance. A
cross-machine package proves source content identity. Pod submission still uses
the original retained machine-1 package and its original checksum verification.
This candidate grants no rehearsal, installer, GPU or pod authority.

The five immutable text snapshots below were restored from task 93's archived
`h3-r8-packaging.patch`. Their original source is
`outputs/91_H3_R8_FRESH_INSTALL-r2/package/<basename>` in proof-codex. Every
snapshot matches the archived `expected-SHA256SUMS.json` and retained bytes.
These are self-contained text docs inputs; importing or executing them is not
part of this build. No binary packs or private media are committed.

| Candidate docs snapshot | SHA256 |
|---|---|
| `inputs/driver.py` | `2548fc3961993d9bb33c57bd502ef48755d1a90ddd9acf4dc0267d0cdd0935b2` |
| `inputs/capture.py` | `b3899ff5eafab6c380c445e29080ad5754786cf44468a9de75742aae2c7a8409` |
| `inputs/run.sh` | `d8f094adeee5643e7b2bc7b95f6c693a05a5efc391da8d37dec93739e6c48a66` |
| `inputs/install.sh` | `100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d` |
| `inputs/release-install-manifest.json` | `fe12781e4da60d441595c08f84cb8d0390d840ab7c71227b8c4eff791553795e` |

The release manifest's older Agent/Runtime pins are frozen bytes: the unchanged
driver substitutes its original frozen local-source tuple before installation.
The installer, source, lock, wheel RECORD and custody checks remain unchanged.

`expected-sources.json` is also the exact retained `sources.json` snapshot
(SHA256 `3e264a4cc4b76c80a1a3b9ea8b4e6184c0bbd1074c6f0d0e2be0a9fb07aa1cd8`).
Its commit/tree/object rows are the content authority; its pack hashes and sizes
are historical machine-1 provenance. The builder copies these metadata bytes
verbatim instead of regenerating them from newly encoded packs. The resulting
rehearsal metadata must not be read as a description of new pack byte sizes or
hashes. Actual pack observations live in `WORK/comparison.json`, outside the
payload, and never replace an expected integrity contract.

`expected-SHA256SUMS.json` is the exact original manifest (SHA256
`797c8d792addd420c332271dede7ec97e71fcfe4e94eae6c6346eebbe65036cb`).
It is copied verbatim into a rebuilt payload. It remains the machine-1 original
pod-package integrity contract. When generated packs have different encoding,
this historical manifest will fail a whole-payload byte check on that rebuild;
the source-content verifier below is the amended rehearsal acceptance. Never
regenerate expected hashes, run the rebuilt `run.sh` as a pod package, or change
the original machine-1 checksum check to accommodate rebuilt bytes.

The builder checks exactly six clean source HEADs, their frozen trees and their
selected object counts. Every generated pack is then indexed with
`git index-pack --strict --stdin` in a new empty bare Git store, without
alternates or donor objects. The selected commit is declared as the shallow
boundary, since these packs intentionally exclude ancestor history. Strict
validation checks complete commit/tree/blob contents below that boundary.
The verifier enumerates the actual pack index, requires exactly the selected
commit and all its trees/blobs with no missing or extra objects, compares the
frozen count, and verifies the exact `refs/heads/frozen` row. Historical pack
SHA256/size is never used to infer content identity. Missing, extra or duplicate
identity/manifest rows are rejected. All seven non-pack members, including
metadata and checksum manifest, must match retained bytes.

See [ROUTE-BUILD.md](ROUTE-BUILD.md) for the build interface and exact host docs
publication handoff. The publication state is **NOT PUSHED / docs publication
pending**. No executable route is published here. B1–B5 remain required, with
B2 interpreted by the amended source-content rule for the rehearsal and original
retained-byte integrity for machine-1 submission. The complete frozen R8,
THE CLIP, C0, Phase C and merged-head production acceptance is preserved in
[PRODUCTION-ACCEPTANCE.md](PRODUCTION-ACCEPTANCE.md); packaging tests prove none
of those installed or GPU rows.
