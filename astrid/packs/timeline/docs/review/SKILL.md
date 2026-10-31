---
name: timeline-review
description: Preview exact timeline changes, get approval, and recover saved edits.
---
# Review a timeline edit

Load this skill when a person must approve exact values before they are
committed.

```python
def recut(t):
    t.clip("intro").on(1.0)

preview = tl.commit(recut, label="Recut", dry_run=True)
print(preview["data"]["diff"])
commands = preview["data"]["commands"]
# After approval, commit the exact reviewed commands:
result = tl.commit(commands, label="Recut", idempotency_key="recut-episode-1", merge="atomic")
```

The reviewed list applies the displayed commands; a function commit can
re-evaluate against a newer timeline and produce different values.

If a function edit clashes, pass its error and the function to `tl.retry(err,
recut)`. A returned `preview` still needs approval. A returned `re_derive` means
the saved list no longer applies: inspect the pending record, rerun the
producer on a fresh `Timeline.open`, preview again, then commit the new
commands with the original key and `pending=<saved-id>`.

Never resend a reviewed list after an unknown outcome. Repeat the same request
with the same idempotency key, then inspect its result. Use `overwrite=True`
only when the person explicitly asked to replace an intervening edit.

For saved-record inspection, load `timeline` and use
`python3 -m astrid timelines pending list <timeline> --project <project>`.
