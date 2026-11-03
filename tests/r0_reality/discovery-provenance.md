# Discovery and owner marker model provenance

This module constructs source-shaped **modeled** inputs from values supplied by
the consumer. Its examples are not captured discovery records and do not prove
that an OS process exists or owns a lock. No captured Windows or Darwin process
birth value is asserted. The caller chooses all process, realm, path, endpoint,
and time values. The Linux `11220` example is cited by the TEST LADDER as a
value observed in run 41617035310; the raw artifact was not available to this
worker, so it is not represented as a full captured record.

All following paths and lines refer to `astrid-runtime` commit
`6d53a19070faf63fc15ae1f81417722edc2c8a5f`:

* `runtime_protocol/platform.py:464–480` combines the high and low halves of
  `GetProcessTimes` creation FILETIME and prefixes `windows-creation:`.
* `runtime_protocol/platform.py:527–546` checks two Darwin observations, then
  formats local process-start seconds as `ps-lstart:` plus weekday, month,
  space-padded day, time and year. The helper models formatting only; it does
  not perform either native observation or the stability check. Locale and
  timezone affect the output just as in the source.
* `runtime_protocol/platform.py:568–578` reads `/proc/<pid>/stat` twice,
  rejects changes and zombie states, and formats field 22 as
  `proc-start-ticks:`. The helper models formatting only, not those checks.
* `astrid_runtime/bootstrap.py:934–943,957–960,976–995` obtains the birth ID,
  writes the five-field `instance.lock` ownership marker and constructs the
  bootstrap discovery record. `astrid_runtime/paths.py:83–94` gives their
  locations. `runtime_protocol/daemon.py:269–272` also publishes the marker
  and its own discovery shape; `runtime_protocol/catalog.py:185–198` filters
  daemon discovery to its allowlist. The module's `discovery` models the
  bootstrap publication (including `advertised_at` and `capability_digest`),
  not the daemon's separately filtered publication.
* `astrid_runtime/bootstrap.py:1147–1165` conditionally removes matching
  records during normal stop. A marker may remain after abnormal termination;
  `leave_marker` writes a file without acquiring or simulating a kernel lock.
  Filesystem security, atomic writes, credential contents, process inspection,
  normal-stop cleanup and native liveness are outside this model.
