# Scope audit

Product base: `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`. Product changes are limited to `astrid/core/execution/generic_host.py` and `tests/core/execution/test_generic_host_actions_v3.py`; `patch.diff` is the complete product diff. The test changes update fixture evidence and calls to the current hook factory, including a changed source-content input. Existing success, finalization, stop and release assertions remain present. No Runtime, VibeComfy, bridge contract, lock pin, integration assembly, workflow, or manager-owned file was edited. The `diagnostic-agent` checkout contains a separate one-line, uncommitted host-callback experiment shown in `host-callback-diagnostic.diff`; it is not part of the pushed product code. Runtime and VibeComfy checkouts remain at the requested heads.

This is an incomplete lane. The binding fix alone must not be treated as release or integration proof. The real hook's explicit refusal and the host callback's pending result were retained, not converted to success. The output directory contains diagnostic instrumentation that forwards the unmodified actual binding and native results; it does not replace the factory or managed release.

The lane additionally contains the non-product `task-evidence/AG2-C07-HOST-BINDING-R1/` snapshot required for review. Its receipt identifies the product commit whose tests produced the snapshot; the task-folder receipt records the final pushed head and its independently rerun proof.

Product-only `git diff --check` returned 0. Full-lane `git diff --check` returned 2 for trailing whitespace present in verbatim captured pytest tracebacks; those logs were preserved as emitted. See `product-diff-check.log` and `scope-diff-check.log`.
