# Saved edits and clashes

Load `timeline-review` when an edit needs approval or recovery. A clash raises
`CommitConflict` and, when `pending_id` is present, saves the refused edit for
follow-up. Pass the original function to `tl.retry(err, edit)` so it can run
again on the newest timeline.

| Retry status | Meaning | Next step |
|---|---|---|
| `accepted`, `merged`, `rebased` | The edit landed. | Report its summary and receipt. |
| `landed` | The edit had already landed. | Report the existing result. |
| `part_landed` | One part landed; saved work remains. | Continue recovery until the saved edit is closed. |
| `preview` | A saved reviewed list still applies. | Show its diff and commit only after approval. |
| `re_derive` | A reviewed list no longer fits. | Inspect the saved edit, re-run the producer on a fresh timeline, preview, then commit with the original key and `pending=` id. |
| `refused` | The saved request is invalid or its key was reused. | Inspect it, fix the request, then retry or drop it. |

If retry says the timeline changed again, retry with the same function. If it
says the function overlaps an intervening write, nothing was overwritten.
Keep the other edit with `tl.pending_drop(err.pending_id, reason="kept theirs")`;
replace it only when the person asked, with
`tl.retry(err, edit, overwrite=True)`. Never use overwrite in a batch loop.

To inspect edits from any agent on this Runtime:

```bash
python3 -m astrid timelines pending list <timeline> --project <project> --all-actors
python3 -m astrid timelines pending show <timeline> <id> --project <project>
```

The CLI retry can print a `--commit TOKEN` command when a saved list is ready
for review. Use `pending drop` for an edit that should not be finished; it did
not land, so it cannot be undone. Undo a recovered edit by its landed receipt.

When same-actor writers share a local Runtime outbox, one may see `session base
changed` or `outbox changed during evaluation`. The exact captured edit is
saved; retry it with its original function. If retry reports `landed` because
the desired values already match a peer's receipt, that saved edit is closed
as already applied. Clash packets may also show `part_landed` when a multi-part
edit still has work left; do not report completion while that record remains
open.
