# Capture and comparison result contracts

These are G0 contract shapes, not claims of collected evidence. See `reference/G0-COMPARATOR.md` for the complete original contract. `capture-manifest.json` is a preparation inventory, **not** a capture index and cannot be fed to the comparator.

`h3-capture/v1`: `{"schema":"h3-capture/v1","tuple":{"agent":"...","runtime":"...","vibecomfy":"...","interpreter":"...","locks":"..."},"coverage":{"P1":"uncaptured", "...":"...", "P12":"uncaptured"},"cases":[{"id":"...","inputs_sha256":"<64 lowercase hex>","artifacts":[{"surface":"receipt","path":"relative/path.json","kind":"json","sha256":"<64 lowercase hex>","size":0}],"evidence":[{"path":"relative/raw.json","sha256":"<64 lowercase hex>","size":0}]}]}`. The `...` keys/values are explanatory placeholders, not a valid index. All 12 coverage keys must be `captured` for comparator operation. Index every case and its artifacts with actual byte digest and size, including raw supporting evidence. Raw media is `bytes`, not a JSON digest claim. Confined relative paths only, without empty, `.`, `..`, backslash or symlink components. Comparison JSON aliases must preserve associations; raw evidence stays intact.

`h3-allowed-diff/v1`: `{"schema":"h3-allowed-diff/v1","differences":[{"case":"<case id>","surface":"receipt","pointer":"/field","old":"old leaf","new":"new leaf","reason":"<reviewed reason>"}]}`. Only exact leaf changes to receipt, preview or lifecycle JSON are eligible; M2+Astra review each entry before comparison. An empty array permits no changes. Allowed entries do not waive completeness, input equality, raw media differences, failure/status changes, or tuple qualification.

`h3-comparison/v1`: `{"schema":"h3-comparison/v1","status":"PASS|FAIL|BLOCKED","cases":[{"id":"...","status":"equal|allowed|different","differences":[{"surface":"receipt","pointer":"/field","old":"...","new":"...","allowed":true}]}],"missing_surfaces":[],"error":"<only when blocked>"}`. Command exit 0=PASS, 1=FAIL, 2=BLOCKED. Required surfaces: options, errors, preview, inputs, raw_engine, media, witnesses, receipt, manifest, delivery, export, lifecycle, shared. A PASS checks the supplied files only; an independent reviewer must establish completeness, tuple and genuine provenance. Comparator never creates or changes a capture index.

Exact local comparison command, after independently obtaining complete, compatible captures and reviewing allowances:

```sh
python3 ./comparator.py /path/to/baseline/capture-index.json /path/to/candidate/capture-index.json /path/to/allowed-diff-v1.json /path/to/new/comparison.json
```

Use a new output path, outside the evidence trees. The comparator must not write over its input files or any file named `capture-index.json`. It does not perform provenance, fixture identity, schema-completeness, alias or qualification review and does not independently certify that coverage labels are truthful. Different input identities are blocked; the separately required reviewer must check the full historical coverage census and A-only disposition.

## Manifest command annotations (054)

In `capture-manifest.json`, each case `command` is an argv token vector (not shell text). `argv_provenance` is exactly `argv source-derived` or `argv installed-verified`; `additional_argv` lists supplementary argv vectors for P1 help surfaces. A source-derived vector establishes only parser/entrypoint syntax from the pinned source and never proves fixtures, fault controls, qualification or capture. `command_gap` and `fixture_binding` remain independent disposition fields. Path values may use only `${INSTALL_PREFIX}` and `${DATA}`. Every case remains `uncaptured`; this manifest is not a capture index.


## Case/fault matrix annotations (059)

Each manifest case has one `case_matrix` object with `case_id`, `case_argv_source`, `case_inputs`, `execution_status`, `blocked_reason`, `scenario`, `control`, `fault_injection`, `expected_receipt_artifact`, `expected_evidence`, `refusal_partial_output_oracle`, `missing_source_facts`, and `source_refs`. These fields are preparation instructions and source-backed expectations, not observed behavior. `fixture_binding` remains the authority for approved identity/A-only disposition. `missing_source_facts` blocks execution where the exact fixture, hook, schema or invocation is not established. Evidence path names are operator collection names unless a cited source identifies a product-emitted filename. No matrix entry changes `state: uncaptured` or establishes qualification.
