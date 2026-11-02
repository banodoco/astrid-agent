# Dependency group installs

Use `astrid.core.pack.transaction.install_group` when a pack and its declared
pack dependencies must move to one source revision together. It fetches or
copies all requested packs into a private candidate collection, validates the
complete dependency closure, builds each requested pack, and publishes the
collection with one directory exchange. A sequence of `install_pack` calls is
not equivalent: readers could otherwise observe a mixed group.

```python
from astrid.core.pack.transaction import install_group

result = install_group(
    source="https://github.com/example/packs.git",
    revision=full_commit_sha,
    packs={"image_tools": "packs/image_tools", "shared": "packs/shared"},
    packs_root=packs_root,
    python_packages=installed_python_package_versions,
    selected=host_and_core_pack_facts,
    release_pins=release_pin_facts,
)
```

`revision` may name a branch, tag, ref, or `HEAD`; the installer resolves it
once to a full commit and uses that commit for every pack in the group. Each
requested ID maps to a source subpath. The canonical manifest IDs and declared
dependencies must agree with the candidate collection and supplied host/core
facts. The installer does not fetch missing dependencies or update packs that
were not explicitly named. Pass `python_packages` as an explicit snapshot so
the compatibility decision uses known installed distribution versions.

Before publication, the group uses the single-pack source and PIN checks, the
M2 pure compatibility validator and import checker, and the M4 asset builder.
The builder is called for ordinary Python packs too; packs without browser
entries produce no browser artifact manifest. Build outputs and admissions
remain private until the entire group is committed. A source, validation, or
build error leaves the current collection in place.

## Defaults and removals

For managed defaults, pass `origin="default"`, `intent="defaults"`, a full
revision, and the expected digest and release PIN for each default. Defaults
cannot replace an existing user or folder pack with the same ID. A conflict
raises `PackInstallError` with code `default_pin_conflict`; resolve it through
an explicit user action instead of allowing the managed update to take over.

Pass explicitly removed IDs in `remove`. The committed collection records
those removals so a later default upgrade does not silently restore them.
Defaults honor recorded removals. An explicit install of an ID clears its
removal record. Removing a pack is itself part of the same group publication.

## Publication and refresh

`read_group(packs_root)` returns the inventory from one committed collection,
including its revision, transaction ID, removals, admissions, and whether a
refresh is pending. Keep `group_read_lock` while reading pack files from the
returned paths so a writer cannot exchange the directory during the read.
Group writes, single-pack writes, and the built-in loader/discovery scans use
the same stable sibling lock. A refresh callback may read the newly committed
group while the transaction still holds that lock.

`install_group` returns a `GroupResult` with the committed paths and a
`retired_root` for the previous generation. Use
`read_retired_group(packs_root=..., transaction_id=result.transaction_id)` to
read that retained generation while artifact custody is being transferred.
The transaction does not delete retired generations. If a refresh is still
pending when another group commits, its changed-ID list carries forward all
changes since the last accepted acknowledgment.

The commit record and all staged files are synced before the directory swap.
A process crash before the swap leaves the previous complete collection; a
crash after the swap exposes the complete new collection. The old generation
is retained under the sibling `.astrid-retired` directory for artifact
custody. This guarantee covers process interruption, not storage power loss.

An optional trusted `refresh` callback runs only after the swap is durable. It
receives the transaction ID, committed revision, and changed IDs, and must
return an accepted acknowledgment for that revision. A missing, failed, or
invalid acknowledgment leaves a durable pending refresh; retry it with
`retry_refresh(packs_root=..., refresh=...)`. A refresh failure does not undo a
committed install. The callback is host code and is never loaded from a pack.
