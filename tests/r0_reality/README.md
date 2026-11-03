# R0 reality fixtures (L0)

This importable, test-only package models the pinned R0 Agent
`3204bb00135bec8ee2d2d3fb21eed5abfceca649` and Runtime
`6d53a19070faf63fc15ae1f81417722edc2c8a5f` for M-R1FIX and M-RT L1 tests.
Import it from the Agent checkout root, e.g. `from tests.r0_reality.services
import ServiceManagerFake`. Pin the containing Agent commit when consuming it.
Nothing here changes product code or constitutes native OS qualification.

* `discovery.captured_discovery(system)` returns a fresh historical R0
  discovery object for `linux`, `macos`/`darwin`, or `windows`/`win32`.
  `linux_birth_id(ticks)`, `darwin_birth_id(seconds)` and
  `windows_birth_id(filetime)` format caller values; `R0Owner(...).marker()`,
  `.discovery(...)`, and `.leave_marker(runtime_support)` model publication
  and leftover support markers. Captured paths, PIDs and endpoints are stale.
  See [discovery-provenance.md](discovery-provenance.md).
* `services.service_definition(system)` returns byte-exact reviewed source
  snapshots, with a normalized Windows boundary. `render.Version` and
  `render.render_definition(system, version, data, log, ...)` render arbitrary
  paths, environment and optional Windows clock from the pinned source.
  `ServiceManagerFake(system)` models registration, unexpected exit, restart,
  disable, stop, bootout and virtual time. Use `darwin`, `linux` or `win32`.
  See [SERVICE_PROVENANCE.md](SERVICE_PROVENANCE.md).
* `lifetime.OwnerLock(path)` provides a real OS file lock for a controlled test
  root; `OwnerBusy` signals contention. `purge_after_unlock(root, after_unlock=...)`
  demonstrates the ordering race, and `session_worker()` / POSIX
  `daemon_with_session_worker()` create disposable session-separated workers.
  These helpers must only be used with test-owned directories and processes.

Lifetime source: Runtime `runtime_protocol/store.py:763–800,894–918,1342–1359`
acquires a nonblocking exclusive owner lock across store lifetime and closes the
connection before release; `platform.py:231–307` selects flock/LockFileEx;
`cli.py:336–350` closes before rmtree after confirmation and tombstone checks;
`local_worker_composition.py:262–269` and `platform.py:737–777` create separate
POSIX sessions / Windows Jobs. C5 amendments explicitly exclude audit reads and
interruption-fence rollback from the SQL-writer claim and require refusing an
in-progress purge or tombstone. The lock helper does not model realm admission
or SQLite, atomic/security guarantees, concurrent deletion on Windows, or all
native handle semantics. The purge helper assumes the checks passed, deliberately
exposes the unsafe gap, and must not be mistaken for a safe purge implementation.
POSIX worker tests demonstrate topology only; Windows Job/handle retention is
not emulated. Real managers, native scheduling and cross-platform proof require
L2/L3 venue tests. Linux lock tests exercise local flock; they do not prove macOS
or Windows behavior.
