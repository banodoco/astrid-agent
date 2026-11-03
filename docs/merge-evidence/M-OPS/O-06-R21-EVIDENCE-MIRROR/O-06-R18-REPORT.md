# O-06-R18 canonical receipt — hosted R-24 owner-discovery evidence update

The accepted O-06-R17 packet was copied and compared byte-for-byte before append, then extended with a bounded diagnostic record for GitHub Actions run `41560168557`. The complete 189268-byte O-06-R17 packet remains the unchanged prefix. The supplement binds the run to the exact workflow/head, backend mode, candidate tuple and manifest digest, materializer stage, inner exit/error, and captured owner-discovery predicates. It is diagnostic-only; qualification, product acceptance, and release gates remain open.

This receipt is canonical for the O-06-R18 documentation update. `OUTPUT-HASHES.txt` excludes this `REPORT.md` and itself to avoid recursive hashing.

```json
{
  "schema": "m-ops.sr-proj-hosted-r24-owner-discovery.receipt.v1",
  "task": "O-06-R18",
  "result": "APPENDED_BACKEND_DIAGNOSTIC; QUALIFICATION_ACCEPTANCE_AND_RELEASE_GATES_OPEN",
  "documentation_branch_label": "lane/M-OPS/O-06-R18",
  "base": {
    "path": "/home/user/merge/ws/M-OPS/queue/done/O-06-R17/SR-PROJ.md",
    "bytes": 189268,
    "sha256": "c30f5bc86bd2d70282d5c6d945c97afd920ce33109c581eb4a90da8e538cf1e6"
  },
  "run": {
    "repository": "banodoco/astrid-agent",
    "run_id": "41560168557",
    "job": "r24-live",
    "attempt": "1",
    "workflow": "lane/M-PIN/R-PIN-OWNER-PREDICATE-CAPTURE",
    "workflow_ref": "banodoco/astrid-agent/.github/workflows/r24-linux-live.yml@refs/heads/lane/M-PIN/R-PIN-OWNER-PREDICATE-CAPTURE",
    "head_sha": "0135545f29747cf5249913e7b10f02776123112c",
    "workflow_sha": "0135545f29747cf5249913e7b10f02776123112c",
    "attempt_evidence_directory": "/home/user/repo/docs/merge-plan/evidence/actions/41560168557/r24-first-stop-0135545f29747cf5249913e7b10f02776123112c-41560168557-1/"
  },
  "mode": "backend",
  "candidate": {
    "agent": "ab43bad818ef65ca1e929b973a2f61cd9fc4bf61",
    "app": "a10b36b3ecf1365c7e122643b2e3cae5cb5c1c8a",
    "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
    "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
    "manifest_sha256": "751bb2fcb489c1194c7a158f5d79d3532a594344ae93cc2da4902947382df3d7"
  },
  "first_stop": {
    "status": "blocked",
    "stage": "authenticate-and-start-worker",
    "command_label": "authenticate-health-start-local-worker",
    "inner_exit": 1,
    "worker_error": "TaskBoundaryError: Runtime owner discovery mismatch",
    "materializer_error": "MaterializerError: authenticate-health-start-local-worker exited 1"
  },
  "owner_discovery_diagnostic": {
    "endpoint_equals_canonical": true,
    "endpoint_parse_succeeded": true,
    "expected_owner_token_regular_nonsymlink": true,
    "discovery_credential_path_equals_expected": false,
    "interpretation": "recorded predicates only; no cause inferred"
  },
  "inputs": [
    {
      "path": "/home/user/merge/ws/M-OPS/queue/done/O-06-R17/SR-PROJ.md",
      "bytes": 189268,
      "sha256": "c30f5bc86bd2d70282d5c6d945c97afd920ce33109c581eb4a90da8e538cf1e6"
    },
    {
      "path": "/home/user/repo/docs/merge-plan/evidence/actions/41560168557/r24-first-stop-0135545f29747cf5249913e7b10f02776123112c-41560168557-1/r24-live-run/evidence/materializer/r24-materializer.json",
      "bytes": 6520,
      "sha256": "1c2cd6a9c8fda7393f7a34d2e69473f26c065162e68c1b8f3ecd80e285e59cfb"
    },
    {
      "path": "/home/user/repo/docs/merge-plan/evidence/actions/41560168557/r24-first-stop-0135545f29747cf5249913e7b10f02776123112c-41560168557-1/r24-live-run/evidence/materializer/owner-discovery-diagnostic.json",
      "bytes": 767,
      "sha256": "16ef983510b4cf52b249eb28d01abbddf5f6937dc628cac4788d4d02a3c6c61e"
    },
    {
      "path": "/home/user/repo/docs/merge-plan/evidence/actions/41560168557/r24-first-stop-0135545f29747cf5249913e7b10f02776123112c-41560168557-1/r24-live-run/evidence/materializer/worker-ready.log",
      "bytes": 52,
      "sha256": "1c79d47109f39370fb694845c5b07f14cb0d83911e07fe77192346ef74e63883"
    },
    {
      "path": "/home/user/repo/docs/merge-plan/evidence/actions/41560168557/r24-first-stop-0135545f29747cf5249913e7b10f02776123112c-41560168557-1/r24-live-run/evidence/materializer/install.log",
      "bytes": 1664,
      "sha256": "6a8322ee959ba6bda74e4783eaeb650d7d9478333e556e90776ddfcdf8f6bc62"
    },
    {
      "path": "/home/user/repo/docs/merge-plan/evidence/actions/41560168557/logs/r24-live-125668636161.log",
      "bytes": 37857,
      "sha256": "5834b9c1045c3be86e1b59ffa5c0aeba81acdb41c73ca837f66738ac4cabe188",
      "role": "workflow/transport-step context only; not a substitute for inner exit"
    }
  ],
  "commands_and_results": [
    {
      "command": "sha256sum and wc -c accepted packet and every file under Actions evidence root",
      "result": "completed before copying/citing; hashes and byte counts recorded in INPUT-HASHES.txt"
    },
    {
      "command": "cp accepted O-06-R17 SR-PROJ.md to task-local SR-PROJ.md; cmp source and destination",
      "result": "cmp exit 0 before append; exact 189268-byte copy verified"
    },
    {
      "command": "append bounded O-06-R18 diagnostic evidence section",
      "result": "run/workflow/head, backend mode, tuple/manifest, first-stop stage, inner exit/error, diagnostic predicates, evidence role and open-gate disposition recorded"
    },
    {
      "command": "cmp -n 189268 accepted packet against final SR-PROJ.md",
      "result": "exit 0; complete accepted packet preserved as final packet prefix"
    },
    {
      "command": "sha256sum and wc -c output-hashed deliverables",
      "result": "values recorded in OUTPUT-HASHES.txt; final verification matched"
    },
    {
      "command": "tests or hosted job rerun",
      "result": "none run"
    }
  ],
  "coverage_inventory": [
    "accepted O-06-R17 packet prefix and exact source digest/size",
    "run, job, attempt, workflow ref/lane, workflow SHA and head SHA",
    "backend mode, exact Agent/App/Runtime/Pinokio tuple and manifest digest",
    "materializer blocked status, first-stop stage, inner command exit and error",
    "owner-discovery endpoint, token-file, and credential-path predicates",
    "worker-ready error and hosted log's limited contextual role",
    "complete Actions evidence-root regular-file hash and size inventory",
    "diagnostic-only disposition and explicit open qualification, acceptance, and release gates",
    "allowed task-folder deliverables and output hash inventory"
  ],
  "limitations": [
    "This update records supplied run artifacts and does not independently rerun or authenticate the hosted workflow.",
    "The diagnostic captures predicate values only; it does not establish why credential-path equality was false or a broader product root cause.",
    "The hosted job log provides workflow/transport context only and does not substitute for the materializer inner exit.",
    "R-24/R-25 qualification, product acceptance, freeze, coherent RC binding, publication, deployment, release approval, handoff, and go/no-go remain open, unestablished, unauthorized, or unclaimed as applicable.",
    "No tests or hosted jobs were rerun; no code, workflow, prior task folder, release ref, STATUS.md, ORCHESTRATOR.md, or other manager file was modified."
  ],
  "outputs_hashed": {
    "SR-PROJ.md": {
      "bytes": 192631,
      "sha256": "a1f5f9eac9f03cab160ef624e364546b90868d0f465c64086cf4dd3b7d747bca"
    },
    "INPUT-HASHES.txt": {
      "bytes": 3106,
      "sha256": "14bc35ab88737649df2f97a5a3b6e3cf9494fa8f6b3143173d8fc0bcac261309"
    },
    "VALIDATION.txt": {
      "bytes": 2930,
      "sha256": "62c8d73de0e7287484705ce3b4f6c1a60ea1be18091fc007c6fb32fc8539b925"
    }
  },
  "output_hash_exclusions": ["REPORT.md", "OUTPUT-HASHES.txt"]
}
```
