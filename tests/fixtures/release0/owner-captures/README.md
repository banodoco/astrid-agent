These are unchanged hosted evidence files, with their original bytes checked
by `SHA256SUMS`. `provenance.json` identifies each source relative to the merge
evidence archive. The frozen pair is Agent `3204bb00` / Runtime `6d53a190`.

`r0-ready.json` embeds the exact discovery and instance-lock bytes, their
digests, and a native process sample (PID 3602, start ticks 12436, UID 1002,
boot and namespace). `r0-selection.json` contains the captured legacy version
object, which has no modern `identity` field. Tests rebase its program prefix
and reconstruct a current pointer and interpreter-presence placeholder; they
do not execute that interpreter. The frozen manifest comes from the existing
checksum-verified `../served/install-manifest.json` fixture.

Historical process tests replay recorded OS observations and HTTP replies;
they do not query or signal historical PID 3602. A separate test obtains a
scoped identity from a live disposable child and exercises Runtime's actual
bridge and the Agent consumers. Signal spies never signal captured PIDs.

The Linux and macOS service files are literal captures from run 41625801442.
Adoption tests verify their bytes before rebasing disposable paths. Windows
reported "Task Scheduler definition unavailable" in that run; its adoption
test continues using the separately registered, checksum-verified R0 task XML
in `../services/task.xml`. This does not claim a hosted Windows capture.
