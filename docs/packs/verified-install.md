# Verified single-pack install

`astrid.core.pack.install.install_pack` is the shared entry point for adding or
replacing one pack. It returns `InstallResult(pack_id, path, commit,
observed_digest, origin, changed)`. Refusals raise `PackInstallError` with a
stable `code`; compatibility refusals also carry the pure validator's result.
Trusted host hook exceptions propagate after staged data is discarded.

```python
from astrid.core.pack.install import install_pack

result = install_pack(
    source="https://github.com/banodoco/poms_packs.git",
    revision=full_commit_sha,
    subpath="hello_ext",
    pack_id="hello_ext",
    expected_digest=expected_pack_tree_digest,
    selected=selected_pack_facts,
    release_pins=release_pin_facts,
    import_contract=frozen_import_contract,
)
```

The CLI obtains consent before calling this function. Pass the source and
revision separately; parsing CLI `URL@rev` syntax belongs to the CLI. Revisions
are a full SHA, branch, tag, explicit ref, or `HEAD`. Only objects reachable from
an advertised source ref are accepted, including ancestors and annotated tags.
A raw SHA being fetchable does not authorize it. Branch/tag ambiguity refuses.
The source may be a local bare repository, a local file URI, HTTPS, or SSH.
Transport helpers and credentials embedded in HTTPS URLs are refused.

Omitting `revision` copies a local folder. Folder PINs have null commit/digest
and origin `folder`; the result still records its observed source digest. Git
installs have origin `user` unless the caller explicitly supplies `default`.
Defaults require both a full SHA and an expected digest. A default cannot
overwrite an existing user/folder/personal install. Source-provided PINs must
agree with the requested provenance and observed bytes. A PIN is never
silently relabelled. Repeating an identical install preserves its directory
inode and returns `changed=False`; modified source bytes are replaced.

The destination collection uses M1-09b's shared personal-root resolver:
`packs_root`, then `ASTRID_PACKS_PATH`, then `~/Astrid/packs`. An empty/relative
configuration refuses. The reserved personal identity and all bundled directory
identities cannot be installed over. Existing personal-origin PINs also refuse
replacement. Symlinked source/destination/root components, traversal, case
collisions, special files, Git symlinks and submodules refuse.

Git content is read with fetch/ls-tree/cat-file into a private sibling staging
directory. There is no checkout, submodule initialization, hook, filter,
dependency installation, Python import from the pack, or pack entry invocation.
Git receives an isolated environment without inherited Git configuration.
Source-provided `.git`, build output, caches and bytecode are omitted. The
canonical v3 parser and M2's pure validator/import checker run before a PIN is
written and before publication. Python-package facts default to installed
metadata in the current interpreter; callers may supply immutable snapshots.
The caller supplies selected dependency facts; the installer does not discover
or fetch missing dependencies. The default import contract exposes the core
facade and public SDK; callers provide the frozen contract for narrower SDK
surfaces and dependency/core public APIs.

`before_swap(stage_path)` is an optional **trusted host** hook for building
assets and quiescing workers. It is never resolved from pack code. Source bytes,
PIN and path safety are checked again afterwards. It may raise to abort. Requests
with a host hook always publish, so a new toolchain's generated output is not
discarded merely because the source digest is unchanged. M1-16
must verify all emitted browser artifacts, retire the affected process epochs,
coordinate same-commit dependency groups, and refresh inventory after commit.
A sequence of single-pack installs is **not** an atomic group transaction.

An OS advisory lock serializes installers sharing a collection. Files are
synced before publication. New installs use one rename; replacements use Linux
`renameat2(RENAME_EXCHANGE)` or macOS `renamex_np(RENAME_SWAP)`. Unsupported
platforms/filesystems refuse replacement without moving the old directory.
There is no two-rename fallback. The successful rename is the commit point;
cleanup cannot turn a committed operation into a reported failure. SIGKILL
before it preserves the old install (or absence); after it the new directory is
complete. Hidden crash residues are ignored, never automatically published.
This is a process-crash guarantee; power-loss durability and active-reader epoch
coordination are not established by the single-pack primitive's tests.

The Linux acceptance tests use real bare repositories, the real parser/checker/
validator, real concurrent processes, SIGKILL, and a real permission-denied
exchange. macOS's exchange path still needs qualification on macOS.
