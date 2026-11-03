Status: PROOF_COMPLETE — CPU base/head Agent assembly proof; LOCAL ONLY, NO PUSH.
Final head 608b46c6c1aec28808ad17d806d936ebddd0f9c7; base 751547766957f0a907871617d9261318a1c8cdb8; branch lane/M-GPU/r1-assembled-astrid-agent-r5.
Runtime/SDK 5453366458f7; VibeComfy e2905d06; pinned testenv/041 Python and pinned-env PASS.
* Reused applicable measurements retain original SHAs in RECEIPT; H28 combines exact complete module/case evidence. c = selected-candidate base context; † baseline is the named accepted block, not int/r1. Absent selectors earn no passes.
| Command | Base → final head (P/F/E/S) |
|---|---|
| joint-H08-H31 | 3/2/0/0 → 5/0/0/0* |
| H07-focused | 44/2/1/0 → 45/1/1/0* |
| H07-regression | 108/1/0/0 → 116/1/0/0* |
| H08-http-dispatch | 92/5/0/0 → 97/0/0/0* |
| H08-focused | 60/1/0/0 (+2 absent) → 79/0/0/0* |
| H12-H14-delivery | 34/9/12/0 → 55/0/0/0 |
| H13-H14-settlement | 300/13/0/1 (+1 absent) c → 323/4/0/1* |
| H18-align | 66/0/0/0 (+2 absent) → 101/0/0/0 |
| H20-F07 | 100/0/0/0 (+2 absent) c → 138/1/0/0* |
| H20-native-handoff | 30/0/0/0 → 30/0/0/0* |
| H21-catalog | 111/0/0/0 (+1 absent) → 116/0/0/0 |
| H21-template | 79/0/0/2 → 79/0/0/2 |
| H22b-sdk | absent → 13/0/0/0 |
| H23b-release | 50/1/0/0 (+2 absent) → 78/5/4/0 |
| H24-receipts | 48/0/0/0 (+1 absent) → 53/0/0/0 |
| H25-reap | 87/0/0/5 → 91/0/0/5 |
| H26-warm | 170/11/0/1 → 182/1/0/1* |
| H27-claim | 5/20/0/0 → 18/7/0/0* |
| H29-discovery | 21/0/0/0 → 23/0/0/0* |
| H31-F06 | 193/0/0/0 → 212/0/0/0* |
| H31-F06-regression | 56/0/0/0 → 56/0/0/0* |
| H28-sel1 | 108/16/0/0 (+2 absent) → 135/0/0/0* |
| H28-sel2 | absent → 1/0/0/0 |
| H28-sel3 | 40/0/0/0 (+1 absent) → 49/0/0/0* |
| H28-sel4 | 1892/55/22/0 → 2036/40/9/0* |
| H3-admission | N/A → 1/0/0/0* |
| H14-lock-regression | 6/0/0/0 → 7/0/0/0* |
| H31-result-regression | 18/0/0/0 → 18/0/0/0* |
| H18-journey | 13/0/0/0 → 13/0/0/0 |
| H27 exact-GET fixture diagnostic | N/A → 4/0/0/0* |
| H3 output-delivery diagnostic | absent → 5/0/0/0 |
| H26 accepted-block / H20 lifecycle diagnostics† | 1/0/0/0 → 0/1/0/0*; lifecycle N/A → 4/0/0/0* |
| H08 deadline diagnostic | 1/0/0/0 → 1/0/0/0* |
| H21 accepted parity diagnostic† | 16/3/0/0 → 16/3/0/0* |
| H18 accepted issue-code diagnostics† | 0/3/0/0 → 0/3/0/0* |
| H23 supervisor timing diagnostics | N/A → 8/0/0/0 |
| Committed wheel build | PASS → PASS |
NEW fixed: H18 Runtime-before-validation by da456e88e, completed with 608b46c6c; four H3 delivery failures by 608b46c6c. Final neighbours: H18-align 101/0, H3 delivery 5/0, H12/H14 delivery 55/0.
NEW unrelated: four H27 unstubbed exact-GET fixtures (unchanged assertions pass 4/4); one H20/H26 constructor-count assertion conflicts with intentional accepted H20 reuse (four lifecycle neighbours pass). No fix needed; failing assertions retained.
NEW unrelated: two provenance and one F04 generic-code assertions reproduce on pristine accepted H18 bf4cde7b4e; local Foley parity reproduces on accepted H21 7edf27db24. Accepted product contracts require the observed behavior; no test edits.
Eight additional H23 supervisor timeout failures/errors pass focused final-head checks with identical source/test blobs; full 78/5/4 outcomes remain recorded. The remaining read-only .pth failure and all other residual failures/errors are base-identical or causally unrelated.
H28 reconciles base 1,969 and head 2,085 collected cases exactly once; complete per-module base→head counts and measured SHAs are in RECEIPT. No unresolved failures. H19 exclusion, vendor/lock checks and final wheel PASS; no tests weakened, provider calls or push.
Unproven: live GPU/provider/Foley qualification, managed installed native journeys/Linux custody, real managed CPython and canonical release bodies blocked by shared read-only/missing-RunPod fixtures, and absent baseline selectors.
