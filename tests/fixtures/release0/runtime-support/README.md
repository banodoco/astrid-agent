# R0 Runtime support records

These are **source-modeled examples**, not captured bytes or live identities.
They model the completed bootstrap publication on Linux for Runtime
`6d53a19070faf63fc15ae1f81417722edc2c8a5f`. Values such as PID, paths,
port, IDs and timestamp are invented, mutually consistent test inputs. Copy
`discovery.json` and `instance.lock` to `<data-root>/runtime/` for a test,
then adjust paths and identity together as the test requires. Credential paths
are references only; no credential files or secrets are supplied. These values
must never be used to identify or stop a real process.

At that commit `astrid_runtime/bootstrap.py:960,976-995` writes the marker
and final discovery, `astrid_runtime/io.py:23-30` serializes sorted, indented
JSON with a newline, and `astrid_runtime/paths.py:83-94` defines their
locations. `runtime_protocol/daemon.py:269-272` first publishes a separate
initial discovery without bootstrap's `capability_digest` and `advertised_at`;
the completed bootstrap may optionally include `door_endpoint` and
`door_origin`. This example has no browser door. `runtime_protocol/platform.py:527-573`
defines Linux `proc-start-ticks:<starttime>`; Darwin uses `ps-lstart:` plus a
local formatted start time, and Windows uses `windows-creation:` plus creation
time (`platform.py:480`). These examples do not assert native identity values
on any platform. `workspace-schema-v1` is the discovery protocol schema name,
not the SQLite schema number (31).

`instance.lock` is support metadata and is distinct from the realm's
`owner.lock` OS lock, held over the store lifetime (`runtime_protocol/store.py:526-529,894-910`).
The presence or content of the example marker does not establish lock ownership.
Fixture values alone do not create a live Runtime, credentials, or a held lock.

The parent `SHA256SUMS` is a pinned historical manifest used by the journey
harness, so it is unchanged. Check these additions independently from this
directory with `sha256sum -c SHA256SUMS`.
