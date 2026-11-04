# Windows boundary task-definition verifier

`astrid.installer.windows_task_identity` is an independent, read-only piece of
the F-767ae0 Windows boundary implementation. It has no scheduler, filesystem,
process, journal or service-mutation calls. It is deliberately not connected to
`BoundaryRegistration`, `r0_stop`, or `BoundaryHandoff`: their Windows refusals
remain necessary. This component does not qualify Windows upgrades.

## Contract

`verify_task_definition(xml, task_path=..., task_name=..., expected=...,
enabled=...)` raises `TaskIdentityError` for an unknown or changed observation.
It returns `None` only when the definition and requested enabled bit match.
An error or empty export is never treated as task absence.

The caller supplies a `TaskIdentity` with an independently trusted task name,
current user's SID, complete executable, serialized arguments, working
directory, and frozen **manager-exported** XML. The name must be `Astrid Runtime`
or `Astrid Runtime R1`; both observation and XML URI must identify that exact
task in the root folder. A single interactive-token principal must own the
single Exec action, with omitted or explicit `LeastPrivilege` RunLevel. Every
action string is matched literally, including whitespace, quoting and inline
Python. The verifier does not resolve paths, expand variables or execute text.

Only `Settings/Enabled` may change from the frozen definition, and its observed
value must match the requested boolean. An omitted value means true per the
[Task Scheduler schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema).
Trigger-enabled bits remain frozen. Omitted RunLevel is accepted for the
captured native definition; explicit elevation is refused. This does not prove
the privileges of a running process: Windows can ignore RunLevel for some
accounts or UAC configurations, as documented for
[Principal.RunLevel](https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-runlevel).

All remaining permitted XML content participates in equality, including
metadata, triggers, repetition, restart/idle settings and R1 publication
metadata. The parser rejects duplicate fields, extra principals/actions,
unrecognized fields/attributes, foreign namespaces, mixed content, comments,
processing instructions, DTDs, malformed output and inputs over 128 Ki
characters. It accepts only a conservative task-version-1.4 subset with at most
one logon and one time trigger; it is not a general XSD validator. It normalizes
namespace prefixes, attribute order and indentation between elements, but
preserves leaf text and child order. Unexpected scheduler serialization fails
closed; it must be investigated, not broadly normalized away.

## Admission and mutation are still missing

The expected XML is **not** an arbitrary live export promoted to trusted state.
Initial admission still has to bind a manager export to the selected R0
Version, exact rendered action, current SID and retained local definition; for
R1 it must bind a unique publication token and disabled initial registration
to the durable journal. Generated local XML is not interchangeable with an
export: the retained native fixture includes scheduler-added fields/defaults,
SID conversion and a different element order. This verifier does not implement
that initial normalization/admission protocol or issue a durable authority.

A matching observation also does not prove absence, stopped state, owner-lock
release, child custody, or immunity to replacement of a same-name task.
Integration must solve the registration-replacement race before using a match
to authorize stop/disable/start/unregister. Query-then-mutate-by-name plus a
postcheck is insufficient.

Remaining Agent work is the live manager adapter and exclusive disabled
creation, admitted/journal-bound identities, fixed launcher and child custody,
Windows handle-safe independent source observation and owner locks,
publication/recovery, and selection/rollback integration. Runtime must port
`workspace_copy` with exclusive destination publication and copy-only mutable
files plus the agreed CAS clone/hard-link policy. At Runtime
`9fc53b771134940aaac901e4cfc5d03c1db1a20a`, `adopt_copy` explicitly refuses
Windows and `_publish` has only Linux/macOS primitives. Removing Agent's guard
before these pieces exist remains unsafe.

## Evidence and validation scope

`tests/fixtures/windows-task-boundary-41906110096.xml` is the LF-normalized
`before-cleanup/unit-file-before-cleanup.txt` from the Windows artifact of
[run 41906110096](https://github.com/banodoco/astrid-agent/actions/runs/41906110096),
artifact ID `13359824046`. It contains hosted fixture paths and SID, not a user
installation. Original artifact file SHA-256:
`503121fb54a548017edc12e59c9d70eb840d7c3950bd8b96902ccef4337dfd22`.
Committed LF-normalized fixture SHA-256: `514835a97ef57ef260a459e1920663d42b39405539ceb5144f2e57de4226e67b`.
The artifact stores decoded text with a UTF-16 declaration; tests read it as
UTF-8 text before XML parsing, just as the API requires decoded manager output.

That run used Agent `fd49d01031655aaead79af0aca1b6934274bffc7` and Runtime
`9fc53b771134940aaac901e4cfc5d03c1db1a20a`. It failed at the existing Windows
registration refusal before R1 install checks. It supplies a real R0 export,
not a passing R1 qualification receipt.

Host-independent tests replay the export and adversarial mutations, including
R1 publication changes, literal special-character paths, state transitions,
and continued no-command Windows refusals. They neither register a task nor
test native scheduler serialization/mutation, launch custody, adoption or
rollback. Native tests and the independent data check remain prerequisites
for eventual Windows integration; no workflow change or pin bump is included.
