---
name: timeline-review
description: Preview exact timeline changes, get approval, and recover saved edits.
---
# Review a timeline edit

```python
from astrid.sdk.timeline.api import Timeline

tl = Timeline.open(project, timeline)

def recut(t):
    t.clip("intro").on(1.0)

preview = tl.commit(recut, label="Recut", dry_run=True)
print(preview["data"]["diff"])
commands = preview["data"]["commands"]
# After approval, commit the exact reviewed commands:
result = tl.commit(commands, label="Recut", idempotency_key="recut-episode-1", merge="atomic")
```

The reviewed list applies exactly what was shown; a function can produce
different values on a newer timeline.

If a function edit clashes, call `tl.retry(err, recut)`. A result with status
`preview` still needs approval. Status `re_derive` means the saved list no longer applies:
inspect the pending record, rerun the function on a fresh `Timeline.open`,
preview again, then commit the new commands with the original key and
`pending=<saved-id>`. List pending records with
`python3 -m astrid timelines pending list <timeline> --project <project>`.

After an unknown outcome, repeat the same request with the same idempotency key
and read its result; never resend the reviewed list as a new request. Use
`overwrite=True` only when the person explicitly asked to replace an intervening edit.
