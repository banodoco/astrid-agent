# Authentic post-R0 capture intake — AWAITING_EVIDENCE

This directory intentionally contains no platform fixture bytes yet. It is an
intake location for a new fixture set and is **not** a substitute for the
frozen `../release0/` compatibility inputs. Nothing under this directory may
be used by a journey assertion until all of the following are independently
bound to an authenticated hosted run or the approved isolated macOS runner:

- one capture-only result for each of Linux, Windows, and macOS;
- the exact driver workflow SHA, immutable R0 Agent
  `3204bb00135bec8ee2d2d3fb21eed5abfceca649`, and immutable R0 Runtime
  `6d53a19070faf63fc15ae1f81417722edc2c8a5f`;
- OS/runner identity, original run/job/attempt/artifact identity, and the
  `capture-receipt.json` plus its complete artifact hash list;
- bounded, sanitized path/link, discovery/owner, process/birth, service
  definition, and manager-state observations, with explicit absent or error
  states where a native fact was not obtainable.

The receipt producer is `.github/workflows/authentic-r0-capture.yml`. It runs
only frozen R0 bootstrap and R0 seed in a fresh runner-temporary root. It does
not invoke an R1 update and is never Release-1 qualification evidence.

M-OPS owns provenance admission through the manager. After that admission, a
new immutable platform directory may be published here with its own
`provenance.json` and `SHA256SUMS`. Do not rewrite `../release0/`, copy an
unverified artifact here, or call this intake complete before all three
authentic captures are present.
