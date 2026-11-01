# Pre-enforcement selected PARAM history

`accepted.zip` contains a normal authenticated Runtime backup and unmodified
portable reads of its history entries, revision closures, admission document,
managed snapshot objects, request and acceptance. The operator key is only for
this test fixture. No rows, accepted records, IDs, clocks or receipts were
inserted or rewritten.

Sources were immutable `git archive` exports:

- Agent `ce363f6d6d60e504dbd4d79803c2d6299f648ade`, tree
  `78af007de1d7c5a59f86fffacd9823de355cd260` (before selection enforcement).
- Runtime `b748ae723b51c6d578ba7a393209ce37d6e2b0c3`, tree
  `1711b198be7f693f11ee4640eb0922ec0d0da952`, public contract 0.2.13.

To regenerate, extract those sources to private directories, then run from
this checkout with the dependency-only Agent environment:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/old-agent \
ASTRID_RUNTIME_CHECKOUT=/private/runtime \
/path/to/venv-agent/bin/python tests/sdk/fixtures/selected-param-legacy/generate.py \
tests/sdk/fixtures/selected-param-legacy/accepted.zip
```

The generator reuses the old native admission fixture, admits a newer max20
revision, births an alias-bearing clip and commits its first PARAM through the
old provider and real Runtime acceptance. `elementRef` selects the original
max10 revision; ignored `element_ref` names the newer admitted max20 revision.
Opaque params retain null, false and an empty object. New IDs and timestamps
are expected on regeneration.

The detached selected-schema test consumes these genuine accepted facts, then
projects only the PARAM dependency/binding metadata to the older pre-snapshot
record shape, as the original test did. That projection tests inverse
compatibility, not authenticated current authority. The native companion
restores the untouched backup, verifies the exact accepted history, registers
the current provider and exercises ledger retry, max10 refusal, inverse/redo
and a new authorized PARAM without local pack sources. It never projects or
rewrites native accepted history.
