---
name: timeline-series
description: Edit a group of timelines safely, record each outcome, and recover failures.
---
# Edit a timeline series

Load this skill when the task applies one edit to many timelines. Keep one
idempotency key per timeline and record every outcome before continuing.

```python
from astrid.sdk.timeline.api import CommitConflict, Timeline

def add_outro(t):
    end = max((c.end for c in t.clips(track="titles")), default=0)
    t.add_text("Chapter end", at=end, hold=2, track="titles", id="outro")

for episode in episodes:
    tl = Timeline.open(project, episode)
    try:
        result = tl.commit(
            add_outro,
            label="Add outro",
            idempotency_key=f"add-outro-{episode}",
        )
        print(episode, result["status"], result["summary"])
    except CommitConflict as err:
        if err.pending_id:
            result = tl.retry(err, add_outro)
            print(episode, result["status"], result.get("summary"))
        else:
            print(episode, "not landed:", err)
```

The key makes a repeated request safe for that episode; the loop continues
after a timeline refuses the edit.

Check for an item before editing and record it as skipped when it is already
done. Do not retry a saved result that needs approval or re-derivation until
that follow-up is complete. Keep a durable ledger outside the loop when a run
must resume after a process stops.

Make a variant with `tl.duplicate(as_="variant-a")`, then open the returned
destination with `Timeline.open(project, "variant-a")`. Archive an unwanted
copy with `python3 -m astrid timelines archive variant-a --project <project>`;
`recover` brings an archived timeline back.

For saved-edit states and recovery steps, load `timeline-review` and
`timeline`.
