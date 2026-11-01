# V12 product RunPod journey: Machine 1 handoff

**Blocked until the fields and gates below are resolved.** This is an
assertion and command handoff to V08, not executable batch bytes or a live
result. V08 owns the exact bytes and results; C9 must rehearse those same
bytes with fakes. V12 is one journey in the single V09/G-08 RC batch, run by
Machine 1. Product is the sole pod claimant; podq may coordinate capacity and
evidence but must not claim, attach a replacement, or supply provider secrets
to a pod. No separate B3 or second live runner.

## Freeze before allocating

- [ ] Integrate R03/R04, A10 and F05/F06/F07, freeze A05 verbs, and prove
  the installed public generation, same-host follow and Runtime release-task
  route. At R05 merge `5075e4a73557a20cf7107417ef5ab7e939173e0e`,
  `--target runpod` is parsed but preflight returns
  `COMFY_TARGET_UNSUPPORTED`; do not run the template on that base.
- [ ] M-INT supplies the exact I-11 parent tuple bytes/digest, Agent, Runtime,
  App, VibeComfy and provider/install identities and pins; M03 binds its
  evidence to that tuple. Record G-06 on that final tuple and I-08 RC identity.
  The R05 merge SHA is source context, **not** the RC tuple.
- [ ] A08 incorporates the R05/R06 rows; M05 and V07 pass nonzero installed
  CPU/fake selectors on that tuple. V08 freezes complete batch bytes and input
  hashes; C9 runs those exact bytes against fakes before any allocation.
  V01/V02 and the remaining V09/G-08 prerequisites must also pass.
- [ ] Freeze the actual entry, mode, prompt/inputs, project and output path,
  plus the exact A10 public command and recovery verbs in V08. Confirm the
  installed CLI matches those bytes. The shape supported by this base's
  parser, **not yet a runnable or frozen command**, is:

  ```sh
  astrid generate image --target runpod --engine comfyui --entry '<V08-entry>' --inputs-json '<V08-inputs.json>' --project '<isolated-project>' --out '<V08-output>' --consent paid --max-usd '<approved-journey-cap>' --deadline '<approved-absolute-UTC-deadline>' --no-wait --json
  ```

  V08 must replace every placeholder, use the approved entry's exact required
  input bytes, add any other consent scope actually required, and rehearse the
  resulting byte-for-byte command. Record the exact follow, task readback and
  release commands from the integrated product; do not guess their semantics.
  No-wait must return the original task receipt and activate it only once.
- [ ] Record the per-run cap in USD and absolute UTC deadline, the current
  offer ID/GPU/hourly USD rate, preparation, execution and idle estimates,
  and the operator's fresh explicit paid consent bound to that offer and
  limits. The entire campaign is capped at **$4 total**, not $4 per journey;
  the journey cap must fit the remaining approved campaign budget. The
  deadline and cap must cover preparation, execution and disclosed idle.
  Unknown rate, insufficient bound, changed offer/rate or fallback requires
  stopping and fresh consent; never invent an approval or UTC timestamp.
- [ ] Pin the authenticated non-secret account reference, exact GPU/offer,
  image `name@sha256:<digest>`, existing volume ID/name/size/datacenter and
  canonical absolute mount. Capture the product's provider-observed values and
  reject mismatches. Record the credential reference only, never its bytes.
- [ ] Select and record an isolated absolute Runtime data root, project and
  output namespace on Machine 1. Configure the *actual installed service* to
  use the same root as the CLI; verify that binding rather than assuming an
  environment override reaches an already running service. The product's
  canonical account journal is `<data-root>/runpod/<sha256(account-ref)>/claim.json`;
  record its resolved path and operation ID. Ensure it is fresh and no other
  broker owns or uses that account/namespace for this journey. A created pod
  is named `astrid-product-<operation_id>`. Never make a second journal to
  bypass an unresolved claim.

## Assertions and evidence

- [ ] Capture the one product claim and the immutable command/input hashes,
  fresh consent, offer/rate, limits and exact journal-created pod identity.
  The Runtime execution request, placement, account, pod, GPU, image, volume,
  mount, task, attempt, fence and activation must agree with the journal and
  provider observation. Verify entry/workflow and inputs before claim; ensure
  the ordinary prepared `vibecomfy.run` route is used.
- [ ] Follow the original receipt on the same host; after a restart, recover
  that same task, binding and activation without a second claim, install or
  submission. Capture terminal Runtime task readback and source/settings CAS
  references and digest-checked bytes. Uncertain submission is fenced, never
  replayed as a fresh task.
- [ ] Capture rendered output, manifest and pulled bytes; verify SHA-256 and
  size against the authoritative manifest and read the published artifact
  back from Runtime durable storage after restart. Compare its digest to the
  pulled/rendered bytes. `RemotePlacement._download` checks manifest hashes and
  `_verify` delegates the existing verifier (and probes a candidate); reuse
  those checks where applicable. They do not replace Runtime durable readback.
  A remote process exit or GPU rental alone is not proof.
- [ ] Record actual cost, currency, source, rate and elapsed paid interval,
  including preparation, execution and any idle; reconcile journey cost and
  campaign total with their caps. Keep evidence free of credential bytes.
- [ ] Dispatch the Runtime release task for this exact journal-created pod;
  read it back durably. Establish drain/fence of the exact task, worker stop
  and activation revocation before termination. Independently capture an exact
  pod GET with HTTP **404** and a **successful account list** omitting that
  pod, plus evidence the pinned volume is preserved. DELETE 404, null status,
  list error, stale list or one absence observation alone is insufficient.
- [ ] On publication outage preserve the spool and recover the same receipt.
  On timeout, ambiguous claim/release, failed list/GET, missing stop evidence or
  unresolved cancellation, retain `cleanup_pending`, block admission/renewal,
  and recover the same host, journal, task and release receipt; do not create
  a replacement. Record failures and recovery evidence without secrets.
- [ ] V11 records immutable evidence references, tuple, bytes, CPU/C9 results,
  task/CAS/artifact/cost/release proof and status. Missing, skipped, stale or
  failed V12 is a release blocker; it is not a pass from feasibility evidence.

`$VC/scripts/runpod_model_matrix.py:32` and its documented live image/video
evidence are feasibility context only. They do not establish product custody,
the ordinary generation route, durable output or verified release.
