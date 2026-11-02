# M2-11a session channel: frozen frame schema (v1)

Status: FROZEN for L1 (host broker), L2 (worker client), L3 (parity harness). Changes need M-GPU approval.

## Transport
- One private socketpair per run, created by the host. The child end goes to the worker via pass_fds; the host keeps the parent end. The token never leaves the host.
- Frames use the existing helpers: `_send_activation_frame` / `_read_activation_frame` in `astrid/core/execution/generic_host.py` (newline-delimited canonical JSON, the frame size limit, duplicate-key rejection). No new framing.

## Request (worker -> host)
```
{"version": "astrid.session-channel/v1", "run_id": "<str>", "seq": <int>, "op": "submit" | "progress" | "cancel", "args": {...}}
```
- `run_id` must equal the run's id. `seq` starts at 1 and strictly increases. Any other value is a protocol error.

## Response (host -> worker)
Success:
```
{"version": "...v1", "run_id": "<str>", "seq": <int>, "ok": true, "result": {...}}
```
Refusal or error:
```
{"version": "...v1", "run_id": "<str>", "seq": <int>, "ok": false, "error": {"code": "<str>", "message": "<str>"}}
```

## Ops (the allow-list; anything else is refused)
1. `submit`, args `{"inputs": {...}, "timeout_seconds": <number>}`. `inputs` is the raw request inputs, not compiled settings; the worker never sends native settings.
   - Allowed once per run. A second submit is refused with code `submit_already_used`.
   - The host compiles and validates `inputs` through the host-owned profile for the managed tool (`ManagedToolProfile.compile_settings`, the same checks applied before `invoke`). A non-object `inputs` or inputs the profile rejects: code `invalid_settings`.
   - result: the evidence mapping the profile `invoke` returns today, unchanged in shape, plus `compiled_settings`: the non-secret echo of the host-compiled settings that were invoked.
2. `progress`, args `{}`.
   - result `{"state": "running" | "terminal", "snapshot": {...}}`. After the run ends, progress returns `"terminal"` with the final snapshot, and the host then closes the channel.
3. `cancel`, args `{"reason": "<str>"}`.
   - Idempotent. result `{"state": "cancel_requested" | "already_cancelled" | "terminal"}`.

## Refusal and close
- Any op outside the list: error code `not_allowed`, then the host closes the channel.
- Malformed frame, bad `run_id`, bad `seq`: error code `protocol`, then close.
- After close, any request fails at the transport layer. The worker must treat that as a terminal error.
- The channel closes at run end, on any host error, and on any refusal. Close is fail-closed.

## Worker client (L2)
- `SessionClient(fd, run_id, timeout)` with `submit(inputs, timeout_seconds)`, `progress()`, `cancel(reason)`.
- Raises `SessionChannelError` with `.code` on an error response, and on transport failure.
- Client-side timeout per request. No retry of `submit` (never resubmit).

## Parity contract (L3)
- Same inputs to `_run_wan_session` (host path) and to the pack in-process action over the channel. Both evidence mappings are canonicalized, then compared exactly.
- Ignored (run identity and timing only): `wan_native.token_id`, `wan_native.invocation_id`, `wan_native.birth`, `wan_native.process_id`, `wan_mapping.manifest.created`.
- Run roots: `wan_native.source_root`, `wan_mapping.output_root` and `wan_mapping.manifest.spool` are replaced by `<root>`. File paths (`output_snapshots[].path`, `staged_outputs[].path`) are compared relative to their run root, so the relative structure must match exactly: same files, same names, same nesting. An extra, missing or renamed file fails parity.
- Stat: only `st_size` is compared (index 2). `st_dev`, `st_ino`, `st_mtime_ns` and `st_ctime_ns` are scratch metadata. `size` and `sha256` stay exact through `manifest.outputs[].bytes`.
- Cleanup links: no such key exists in the evidence, so nothing is compared for them. Recorded here as absent.
- Any other difference is a parity failure. Types are compared, so `1` and `True` differ.

## Deadline (lane note)
- The host sends `run_channel_timeout_seconds` = the remaining run deadline (`max(0.1, execution_deadline - now)`). It bounds each request on the channel: `submit` waits its `timeout_seconds` plus the channel timeout; `progress` and `cancel` wait the channel timeout. It is not a cap on the whole action.
- The worker accepts any finite value in (0, 86400] and refuses anything else (`session_channel_timeout`). 86400 is a sanity bound for malformed payloads, not a render limit. A 5090 Wan render longer than 15 minutes is covered by the host run deadline.
