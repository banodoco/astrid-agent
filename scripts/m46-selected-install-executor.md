# Selected-install admission and evidence, v1

This unit is **not an install executor**. It is an executable, fail-closed
admission boundary on Linux for the selected backend install. It verifies the
four pinned clean checkouts, Pinokio commit/tree/parent, the clean runner head
and its direct parent, the selected source bytes and manifest, and an empty
private evidence directory. It then invokes the selected Agent's actual
`PinokioActionGraph.collect` in an isolated Python process. Its stdout, stderr,
actual exit and SHA-256 hashes are retained in the output directory. The request
hash in the result is SHA-256 of the sorted, compact, ASCII-escaped JSON object.

```
PYTHONDONTWRITEBYTECODE=1 python3 scripts/m46_selected_install_executor.py /absolute/request.json
```

The closed request and result schemas are adjacent to this document. A valid
request always returns 2 with a blocked result. Invalid requests return 2 and
may fail before creating any output. On the selected source, the collector
refuses dynamic imports; even a graph that passes that collector cannot bind
install, since it collects start/stop/update only. No user-supplied command,
app mapping, fixture or ledger is accepted as an authorization override.

`graph_check.exit_code` belongs to the collector process, not to the install
or an installer child. `action_invoked=false`, `action_exit=null`,
`readback=null` and `settlement=unknown` are deliberate. This unit does not
start Docker, a daemon, PTerm, an app, or a protected observer; it does not
produce an observer binding or M4-6 receipt. Action dispatch, container
isolation, actual child exit and readback capture require a reviewed install
closure, unambiguous app mapping, and the existing protected observer's live
admission. The schema currently permits only blocked results and must be
versioned or explicitly revised when a real execution contract exists.
