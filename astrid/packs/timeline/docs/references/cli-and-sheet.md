# Timeline CLI and sheet

Load `timeline` for the normal edit path. Use these direct commands for one
literal move or trim, or for editing a native cut sheet.

```bash
python3 -m astrid timelines edit <timeline> --project <project> \
  --clip parent/clip:<clip-id> --on 1.5 --idempotency-key move-1 --json
python3 -m astrid timelines edit <timeline> --project <project> \
  --clip parent/clip:<clip-id> --trim-out 3.0 --idempotency-key trim-1 --json
```

`--on` is timeline seconds; `--trim-in` and `--trim-out` are file seconds.
These forms commit directly. Other edit flags may change a detached working
copy.

For a native sheet, export only the current published clips or occurrences you
select, edit the rows, then apply with a new key:

```bash
python3 -m astrid timelines show <timeline> --project <project> --as sheet --native --clip <clip-id> > cut.sheet
python3 -m astrid timelines apply <timeline> cut.sheet --project <project> --idempotency-key sheet-1
```

Undo a landed CLI edit by its receipt:
`python3 -m astrid timelines undo <timeline> --project <project> --commit <receipt-id> --idempotency-key undo-1`.
