# Discovery and owner marker model provenance

`captured_discovery` loads complete discovery objects printed by the R0
bootstrap in Actions 41617035310. They were extracted as JSON objects and
reformatted; field values were not changed. They are historical records: PIDs,
paths and endpoints are stale and must not be used to contact or signal a host.
They do not prove current liveness. No credentials or token contents are stored.
`R0Owner` constructs separate **modeled** records from caller-supplied values;
its marker is source-shaped, not a captured `instance.lock` file.

Raw evidence is `docs/merge-plan/evidence/actions/41617035310/` under the docs
checkout `1509593b6dca2f6c107659707a45c19f4fd15155`. All paths below are under
`journey-receipt-direct-<OS>/before-cleanup/install.log`. Extracted values are
from the `discovery` object beginning at the listed line; source files are
hashed before extraction:

| OS | line | source SHA-256 | extracted JSON SHA-256 |
| --- | --- | --- | --- |
| linux | 1194 | `32e742b77e01205fff85d2646880876c226597650e6828c6c8187f1544339f43` | `4c7528daf798af9f630b1d42b21ca154c47689a74f800404da15f41857e0d45e` |
| macos | 1048 | `70a09a5e5f4e5970141c12f2ee75871cff23ad865c8a9da8987ac28aa4e8a7bd` | `a2e7a6dbb2c7ee4958ca1844563c9f75a05532c719212a4f8293f99e59020398` |
| windows | 1174 | `3fa5320d428585e55b5deb0d7a9ebf5b9ca055450da248ec11ac71bcb8f3b23a` | `1fcebf77e1085d25f3163cf6074c831a959b685a23ff8e4c95c58f26e1ac7f58` |

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
