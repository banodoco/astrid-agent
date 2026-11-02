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
1. `submit`, args `{"settings": {...}, "timeout_seconds": <number>}`.
   - Allowed once per run. A second submit is refused with code `submit_already_used`.
   - The host validates `settings` with the same checks `_run_wan_session` applies before `invoke_wan_session`. Invalid settings: code `invalid_settings`.
   - result: the evidence mapping `invoke_wan_session` returns today, unchanged in shape.
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
- `SessionClient(fd, run_id, timeout)` with `submit(settings, timeout_seconds)`, `progress()`, `cancel(reason)`.
- Raises `SessionChannelError` with `.code` on an error response, and on transport failure.
- Client-side timeout per request. No retry of `submit` (never resubmit).

## Parity contract (L3)
- Same inputs to `_run_wan_session` (host path) and to the pack in-process action over the channel. Compare the evidence mapping after removing the documented allowed-diff keys (run ids, timing, scratch paths). Anything else must match exactly.
