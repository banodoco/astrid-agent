# Systemd typed observation hook (diagnostic, incomplete executor)

This is a capture component, **not** a workflow or runnable native proof. It
wraps the exact `_transfer_systemctl_show` and `_bounded_busctl` functions in
Agent candidate `870c3aa1b1c516d4f40787c962dc3483356f2fac`. It does not call
those functions itself, change their result, or authorize an action. The hook
is process-global and intended only for a single-threaded, disposable probe.
Revalidate the function interface for any later candidate.

The future executor must import this module into the process that imports the
exact clean candidate and execute the actual probe inside
`with observe.capture(service_module, events): ...`. In a `finally` block it
must save `events` even when the probe raises, alongside the real probe exit,
non-authoritative predicate results, UID, exact immutable tuple and protected
run/job provenance. The endpoint number advances on each actual text call;
within each endpoint, `resolve`, `property`, `resolve` identify the candidate's
actual D-Bus calls. A property classification is meaningful only when the
associated exits are zero and both resolutions are `expected-object`. Missing,
failed, extra, reordered or inconsistent events remain unknown. Client and
manager versions/builds, peer/bus identity, race and manager replacement still
need independent recording; object-path equality does not authenticate the bus.
Text key counts are structural hints, not a substitute for the candidate's
parser or a proof of loaded definition equivalence.

Only text hashes and recognized key names, typed stream hashes and strict
empty/populated/unknown classifications, counts, timestamps, operations and
child exits are recorded. No paths, tuples, property values, exception messages
or stderr are emitted. Hashes may still be sensitive and belong in protected
artifacts. A failure with no observed child exit remains null, never zero.

Before an Actions request, the executor must be integrated with the isolated
non-root venue and tested with the owner's exact re-pinned candidate. Add
separate disposable empty, one, multiple, optional and drop-in controls, plus
unknown property and wrong-unit-type controls. Empty must yield a decoded
`a(sb)` count 0; populated controls must yield their exact expected nonzero
count; unavailable/wrong-property/wrong-interface must remain unknown. Stop
and fail closed on failed controls, identity changes, missing endpoints, wrong
order or cleanup errors. Never manufacture an empty list. Running and stopped
cases must preserve the original assertions and record all actual inner exits;
unknown/refusal and `authority=false` remain in force. Control observations
are separate from, and cannot repair, either actual endpoint.
