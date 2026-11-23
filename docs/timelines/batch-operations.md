# Batch media adds

`astrid timelines edit FILM --project PROJECT --operations additions.json` applies
all operations in one timeline commit. The JSON file has exactly one top-level
key, `operations`, containing a nonempty array. Each entry has `op: "add"`, a
nonempty `source` (local file, media handle, or existing asset key), numeric
`at` (nonnegative seconds) and `hold` (positive seconds), and optional nonempty
`track` and `id` strings. IDs in the file must be distinct. No other fields or
edit flags are accepted. For example:

```json
{"operations": [
  {"op": "add", "source": "frame.png", "at": 0, "hold": 3, "track": "V1", "id": "opening"},
  {"op": "add", "source": "frame.png", "at": 3, "hold": 2, "track": "V1", "id": "next"}
]}
```

Paths resolve from the current working directory. `--dry-run`, `--json`, and
`--idempotency-key` are supported. A refused operation accepts no timeline edit.
Media imported before a refusal remains in the project catalog and is reported
in the error's `details.imports` and the human message. A repeated idempotency key resolves the original request, including after an
unknown commit outcome, before reading the file again. Use a fresh key for a
new batch.
