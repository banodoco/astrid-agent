# R18 connected delivery: early recommendation

2026-11-03T06:28Z — design and source diagnosis only; protected review pending.

Recommendation: Agent's installation transaction must independently and durably burn a delivery attempt before Runtime's first descriptor send. Runtime's delivery marker and session ledger are subordinate transport records, never sufficient evidence of a new transaction. Add an authenticated create-only initialization plus monotone reserve/query authority API under Agent's existing installation lock. A surviving reserved/spent authority refuses re-send even if both Runtime records disappear. Never infer delivery success from absence.

At exact Runtime candidate `96c0391d286b150dbb87ebfe3e5f1f9d7aa08b78`, `_deliver_provisional` checks the marker, queries ledger state, reserves the marker, records recovery-required, then sends aliases (`astrid_runtime/lifecycle_transfer.py:567–606`). Marker absence returns None (`runtime_protocol/_lifecycle_transfer_delivery.py:20–24`); missing ledger yields observed/empty (`query`, to be detailed in DESIGN). Losing both therefore erases those two replay barriers. Existing connected-delivery tests cover replay/restart, lost reply, pre-send crash, one missing marker and marker replacement; no both-records-lost test exists in that file. These are source observations, not a test-run result.

If all authoritative durable state is lost, a recovered process cannot distinguish never-sent from sent-and-forgotten. It can preserve no-resend only by refusing ambiguous recovery. Safe automatic first-delivery/retry availability then requires a surviving external monotone authority; randomness, a free lock, an empty directory, or a newly initialized journal cannot reconstruct history. No automatic repeat or authority reset is proposed.

The requested M-RT full review is inaccessible at the published input and local outbox paths inspected at docs HEAD `edb233602abdf7f0fb7800266a9a4b586a73f759`; the 06:18:19Z relay is acknowledged as a request, not review approval. The requested subworker is already running and will supply independent candidate diagnosis.

WIP artifact will be published on `lane/M-R1FIX/R18-CONNECTED-DELIVERY-RECOVERY-design`, based exactly on Agent `b54117c9312165e2799554ec27480665d5cad2b1`. Final packet will define initialization provenance, durable owner, ordering, identity binding, crash matrix, replacement behavior and focused/native test obligations. No product implementation, bridge or migration changes.
