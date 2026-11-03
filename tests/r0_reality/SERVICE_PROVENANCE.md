# R0 login service fixtures

Pinned source: `banodoco/astrid-agent` commit
`3204bb00135bec8ee2d2d3fb21eed5abfceca649`. The three files under
`service_definitions/` are exact byte copies from `tests/fixtures/installer/`
at that commit. They are reviewed **sample** definitions, not observations of
an actual installation. In particular, the Windows sample uses the normalized
`2000-01-01T00:00:00+00:00` StartBoundary; upstream's snapshot test normalizes
the generated value before comparing (`tests/test_installer_platforms.py:20-29,64-75`).

| file | SHA-256 |
| --- | --- |
| launchd.plist | `42e9ddb04d8b6293ca4bfddc6bf08fb441eca52f13d831885ffa1e99220f71dc` |
| systemd.service | `9f98c45a8aff00545a7b7ffa7d0156b661ee5867b500a134d36ea9e4d0887903` |
| task.xml | `a8b7a467136c7aaef9a128cee9247198dd86bea257249cd9355b478fb5feb944` |

R0 definition generation is in `astrid/installer/service.py:68-148`: launchd
RunAtLoad and KeepAlive, systemd Restart=always and RestartSec=5, and Windows
logon and time repetitions (PT1M), IgnoreNew, and RestartOnFailure (PT1M/999).
R0 disable operations are at `:190-206`, stopping at `:228-240`, registration
at `:251-268`, and start at `:286-295`. `tests/test_installer_platforms.py:78-103,106-136`
corroborates those generated structures and commands. The snapshots are no-env-file
samples; actual registration passes an env file (`service.py:251-257`), so
consumers needing an installation-specific command must supply their own.

`ServiceManagerFake` is a deterministic virtual-time state model, not an OS
manager. It models unexpected exit and restart, systemd disable separately from
stop, launchd bootout unloading the job, and Windows disable separately from
stop with repeating triggers and IgnoreNew. It collapses Windows' two trigger
streams to one cadence, models KeepAlive restart as immediate, treats manager
stop as completing immediately, and does not model native process trees,
permissions, scheduler throttling, pending trigger delivery, restart limits
across scheduler sessions, or real time. Its state and event sequence are test
instruments, not captured operating-system records or native proof.

`render.render_definition` vendors R0 `service.py:68–148` for consumers that
need installation-specific paths and environment. It adds a `Version` test
record, renames `render`, and injects an optional Windows StartBoundary; the
rendering code otherwise follows the pinned source. Its golden test reproduces
all three reviewed samples byte-for-byte; production registration supplies
`env_file`, covered separately. These are generated test definitions, not
observations of installed services.
