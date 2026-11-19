"""Opt-in first-job phase observations from the existing LS-2 observers.

These are instrumented inclusive wall-time spans, not a latency qualification
or an additive critical-path decomposition. Nested phases and concurrent
threads can overlap. Coroutine/generator profile spans are resume-to-yield
segments; suspended time is not silently assigned to their Python bodies.
"""
from __future__ import annotations

from collections import defaultdict
import statistics


def _sites(module, phase, *names):
    return {module + ":" + name: phase for name in names}


PHASE_SITES = {
    **_sites("astrid.sdk.remote", "caller_admission", "RemoteTasks.prepare_admission", "RemoteTasks.dispatch_admission"),
    **_sites("runtime_protocol.service", "runtime_admission", "RuntimeService.create_task"),
    **_sites("runtime_protocol.service", "runtime_claim", "RuntimeService.claim_next"),
    **_sites("runtime_protocol.task_waits", "claim_wait", "TaskWaits.wait_for_work"),
    **_sites("astrid.core.execution.generic_host", "claim_wait", "GenericPackHost._wait_for_ready_work"),
    **_sites("astrid.sdk.invocation", "caller_completion_wait", "_wait_for_kernel_task_loop"),
    **_sites("astrid.core.execution.generic_host", "pack_verify", "_verify_action_admission", "_verify_admitted_source",
             "_source_digest_for_roots", "GenericPackHost._selected_pack_map"),
    **_sites("astrid.core.pack.imports", "pack_verify", "validate_selected_pack_map", "_validate_selected_pack_map"),
    **_sites("astrid.core.pack.identity", "pack_verify", "pack_digest", "_read_pack_tree"),
    **_sites("astrid.core.pack.imports", "worker_import_admission", "install_pack_imports"),
    **_sites("astrid.core.pack.discovery", "discovery_fingerprint", "_discovery_fingerprint", "_tree_fingerprint"),
    **_sites("astrid.core.source_identity", "source_identity", "_source_digest", "source_checkout_closure",
             "source_checkout_closure_digest", "verify_installed_host_startup"),
    **_sites("astrid.core.generation.entries", "workflow_load", "_load_entry_workflow"),
    **_sites("astrid.packs.vibecomfy.production_engine", "workflow_load", "load_workflow_path"),
    **_sites("vibecomfy.workflow_bundle", "workflow_load", "load_bundle"),
    **_sites("vibecomfy.scratchpad_loader", "workflow_load", "load_scratchpad"),
    **_sites("astrid.core.generation.entries", "workflow_inputs", "_workflow_inputs", "_writable_node_bindings",
             "apply_workflow_inputs", "resolve_workflow_inputs"),
    **_sites("vibecomfy.schema.provider", "schema_lookup", "ObjectInfoIndexSchemaProvider.get_schema",
             "SourceSchemaProvider.get_schema"),
    **_sites("vibecomfy.schema.provider", "schema_freshness", "ObjectInfoIndexSchemaProvider._sync_reader"),
    **_sites("vibecomfy.porting.object_info.consume", "schema_snapshot", "class_entry_snapshot", "get_classes"),
    **_sites("vibecomfy.porting.object_info.consume", "schema_freshness", "_sync_reader"),
    **_sites("vibecomfy.workflow", "workflow_compile", "VibeWorkflow.compile"),
    **_sites("vibecomfy.workflow_bundle", "bundle_compile", "WorkflowBundle.compile"),
    **_sites("vibecomfy.workflow_bundle", "bundle_emission", "emit_bundle", "emit_bundle_with_candidate",
             "_with_bundle_object_info_snapshot.<locals>.wrapped"),
    **_sites("astrid.packs.vibecomfy.local_server_engine", "bundle_emission", "_attempt_value_bundle"),
    **_sites("astrid.core.generation.backends.comfy_target", "output_fetch", "ComfyTargetClient.download_output"),
    **_sites("vibecomfy.runtime.output_receipts", "output_verify", "collect_receipt"),
    **_sites("astrid.core.execution.generic_host", "output_store", "GenericPackHost._typed_outputs",
             "GenericPackHost._upload_outputs", "RuntimeProtocolClient.upload_object"),
    **_sites("runtime_protocol.cas", "output_store", "ContentAddressedStore.stage", "ContentAddressedStore.publish"),
    **_sites("runtime_protocol.service", "output_store", "RuntimeService._stage_outputs", "RuntimeService._publish_staged_outputs",
             "RuntimeService._ingest_transfer", "RuntimeService._copy_open_file_at"),
    **_sites("runtime_protocol.service", "output_verify", "RuntimeService._verify_open_file"),
    **_sites("runtime_protocol.cas", "output_verify", "ContentAddressedStore._verify_open_stream"),
    **_sites("runtime_protocol.service", "settlement", "RuntimeService.settle_attempt"),
    **_sites("astrid.core.execution.generic_host", "settlement", "RuntimeProtocolClient.settle"),
    **_sites("runtime_protocol.server", "runtime_reply", "RuntimeHandler._send", "RuntimeHandler._send_file"),
    **_sites("astrid.core.generation.delivery", "caller_delivery", "deliver_generation"),
    **_sites("astrid_runtime_client.generated", "caller_output_receive", "WorkspaceClient._get_object_file"),
}

NOTES = [
    "Instrumented diagnostics only; the separate 20 uninstrumented D1 samples determine the budget result.",
    "Per phase, union overlapping spans inside each caller interval before computing its median and slowest across first jobs.",
    "Only observed stand-in execution is subtracted; queue time stays. Inclusive phases overlap and must not be summed.",
    "Python coroutine/generator spans measure active resume-to-yield segments, not suspended time.",
    "Worker Popen-entry to observer-start includes process launch, startup/admission and observer bootstrap; the unchanged security prefix is not internally observed.",
    "Output hash time covers only C constructor/update consumption for the same digest-and-size matched streams; file reads and digest finalization remain outside that subphase.",
    "Caller-start-to-admission and settlement-to-reply are observed endpoint intervals, not claims that one named function owns every intervening cost.",
    "Host claim-wait spans can include concurrent idle service time; admission-to-claim endpoints are reported separately and are not inferred from their sum.",
    "Admission-to-claim and settlement-to-reply start at task-bound service-body returns; surrounding HTTP spans separately include decorator durability and wire reply.",
]


def capture_phases(meter):
    """Export clocks already retained by AttachedMeter and RuntimeObserver."""
    with meter.lock:
        http = [{"method": method, "path": path, "monotonic_start_ns": round(start * 1e9),
                 "monotonic_end_ns": round(end * 1e9)} for method, path, start, end in meter.events]
        timeline = [{"prompt_id": prompt, "queued_ns": round(queued * 1e9),
                     "start_ns": round(start * 1e9), "end_ns": round(end * 1e9)}
                    for prompt, queued, start, end in meter.timeline]
    return {"schema": "astrid.ls12-phases.v1", "phase_sites": PHASE_SITES,
            "runtime_spans": meter.runtime.operation_scopes(meter.start_ns, meter.reply_ns, meter.wall_reply_ns),
            "standin_http": http, "standin_timeline": timeline, "notes": NOTES}


def union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if end < start:
            raise ValueError("phase end precedes start")
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def duration(intervals, excluded=()):
    spans, removed = union(intervals), union(excluded)
    total = sum(end - start for start, end in spans)
    for start, end in spans:
        total -= sum(max(0, min(end, right) - max(start, left)) for left, right in removed)
    return total / 1e9


def breakdown(row):
    observation = row.get("phase_observations", {})
    if observation.get("schema") != "astrid.ls12-phases.v1" or observation.get("phase_sites") != PHASE_SITES:
        raise ValueError("missing or different phase observer configuration")
    start, reply = (row["interval"][key] for key in ("monotonic_start_ns", "monotonic_reply_ns"))
    if type(start) is not int or type(reply) is not int or not start < reply:
        raise ValueError("invalid phase caller interval")
    timelines = [span for span in observation["standin_timeline"] if start <= span["queued_ns"] <= reply]
    if len(timelines) != 1 or not start <= timelines[0]["queued_ns"] <= timelines[0]["start_ns"] <= timelines[0]["end_ns"] <= reply:
        raise ValueError("requires the complete first-job stand-in timeline")
    excluded = [(timelines[0]["start_ns"], timelines[0]["end_ns"])]
    intervals, raw_spans = defaultdict(list), []

    def add(phase, span, **identity):
        left, right = span["monotonic_start_ns"], span["monotonic_end_ns"]
        if type(left) is not int or (right is not None and (type(right) is not int or right < left)):
            raise ValueError("invalid phase span")
        if left > reply or (right is not None and right < start):
            return
        clipped = (max(start, left), min(reply, reply if right is None else right))
        intervals[phase].append(clipped)
        raw_spans.append({**span, **identity, "phase": phase, "clipped_start_ns": clipped[0],
                          "clipped_end_ns": clipped[1], "carry_in": left < start,
                          "complete_within_caller": right is not None and right <= reply})

    processes = row["process_observations"]
    for process in processes:
        if "phase_spans" not in process or "output_hash_spans" not in process:
            raise ValueError("phase observation missing in a caller/worker process")
        identity = {"pid": process["pid"], "role": process["role"]}
        for span in process["phase_spans"]:
            if PHASE_SITES.get(span.get("site")) != span.get("phase"):
                raise ValueError("unknown phase function")
            add(span["phase"], span, **identity)
        for span in process["output_hash_spans"]:
            add("output_hash_consumption", span, **identity)
    routes = {"POST /v1/tasks": "runtime_admission_http", "POST /v1/tasks/claim": "runtime_claim_http"}
    runtime = observation["runtime_spans"]
    for span in runtime:
        route = span.get("route") or ""
        phase = routes.get(route)
        if route.endswith("/settle"):
            phase = "settlement_http"
        if phase:
            add(phase, span)
    for span in observation["standin_http"]:
        if span["method"] == "GET" and span["path"].startswith("/view"):
            add("standin_output_http", span)

    landmarks = {"caller_start": 0.0, "caller_reply": (reply - start) / 1e9}
    task_id = row["proof"]["measured_task_id"]
    task_spans = [span for process in processes for span in process["phase_spans"]
                  if span.get("task_id") == task_id and span["monotonic_end_ns"] is not None
                  and start <= span["monotonic_start_ns"] <= span["monotonic_end_ns"] <= reply]
    admitted = [span for span in task_spans if span["site"] == "runtime_protocol.service:RuntimeService.create_task"]
    scopes = {span.get("runtime_scope") for span in admitted} - {None}
    admissions = [span for span in runtime if span.get("route") == "POST /v1/tasks" and span.get("scope") in scopes
                  and start <= span["monotonic_start_ns"] <= reply]
    if len(admissions) != 1 or admissions[0]["monotonic_end_ns"] is None:
        raise ValueError("requires one completed first-job Runtime admission")
    admission = admissions[0]
    admitted_at = max(span["monotonic_end_ns"] for span in admitted)
    landmarks["admission_body_return"] = (admitted_at - start) / 1e9
    for boundary in ("start", "end"):
        landmarks["runtime_admission_" + boundary] = (admission["monotonic_" + boundary + "_ns"] - start) / 1e9
    add("caller_start_to_admission", {"monotonic_start_ns": start,
                                       "monotonic_end_ns": admission["monotonic_start_ns"]})
    starts = [span for process in processes for span in process["process_starts"]
              if span["child_role"] == "pack_worker" and start <= span["at_ns"] <= reply]
    if len(starts) != 1:
        raise ValueError("requires one observed first-job worker start")
    worker = next((process for process in processes if process["pid"] == starts[0]["child_pid"]), None)
    if worker is None or not start <= starts[0].get("spawn_start_ns", -1) <= worker["start_ns"] <= reply:
        raise ValueError("worker bootstrap boundary is missing or reversed")
    landmarks["worker_popen_entry"] = (starts[0]["spawn_start_ns"] - start) / 1e9
    landmarks["worker_popen_return"] = (starts[0]["at_ns"] - start) / 1e9
    landmarks["worker_observer_start"] = (worker["start_ns"] - start) / 1e9
    add("worker_startup_and_unobserved_admission", {"monotonic_start_ns": starts[0]["spawn_start_ns"],
                                                    "monotonic_end_ns": worker["start_ns"]})
    claim_scopes = {span.get("scope") for span in runtime if span.get("route") == "POST /v1/tasks/claim"} - {None}
    claims = [span for span in task_spans if span["site"] == "runtime_protocol.service:RuntimeService.claim_next"
              and span.get("runtime_scope") in claim_scopes
              and admitted_at <= span["monotonic_end_ns"] <= starts[0]["spawn_start_ns"]]
    if not claims:
        raise ValueError("no completed Runtime claim between admission and worker start")
    claimed = max(span["monotonic_end_ns"] for span in claims)
    landmarks["last_task_claim_body_return_before_worker"] = (claimed - start) / 1e9
    add("admission_to_claim", {"monotonic_start_ns": admitted_at, "monotonic_end_ns": claimed})
    settlement_scopes = {span.get("scope") for span in runtime if (span.get("route") or "").endswith("/settle")} - {None}
    settlements = [span for span in task_spans if span["site"] == "runtime_protocol.service:RuntimeService.settle_attempt"
                   and span.get("runtime_scope") in settlement_scopes]
    if not settlements:
        raise ValueError("missing completed first-job settlement")
    settled = max(span["monotonic_end_ns"] for span in settlements)
    landmarks["last_task_settlement_body_return"] = (settled - start) / 1e9
    add("settlement_to_reply", {"monotonic_start_ns": settled, "monotonic_end_ns": reply})
    for phase, spans in intervals.items():
        landmarks[phase + "_first_start"] = (min(left for left, _ in spans) - start) / 1e9
        landmarks[phase + "_last_end"] = (max(right for _, right in spans) - start) / 1e9
    return {"caller": row["caller"], "sample_index": row["sample_index"],
            "instrumented_overhead_seconds": duration([(start, reply)], excluded),
            "phases": {phase: {"spans": len(spans), "wall_seconds": duration(spans),
                                "overhead_seconds": duration(spans, excluded)}
                       for phase, spans in sorted(intervals.items())},
            "landmarks_seconds_from_caller": landmarks, "raw_spans": raw_spans}


def summarize(rows):
    if not rows:
        raise ValueError("missing first-job phase samples")
    result = {}
    for caller in sorted({row["caller"] for row in rows}):
        samples = [row for row in rows if row["caller"] == caller]
        phases = sorted({name for row in samples for name in row["phases"]})
        def stats(values):
            return {"samples": len(values), "median": statistics.median(values), "slowest": max(values)}
        result[caller] = {"samples": len(samples),
            "not_observed_configured_phases": sorted(set(PHASE_SITES.values()) - set(phases)),
            "phases": {
            phase: {**stats([row["phases"].get(phase, {}).get("overhead_seconds", 0.0) for row in samples]),
                    "jobs_with_observed_spans": sum(phase in row["phases"] for row in samples)} for phase in phases},
            "landmarks_seconds_from_caller": {name: stats([row["landmarks_seconds_from_caller"][name]
                for row in samples if name in row["landmarks_seconds_from_caller"]])
                for name in sorted({name for row in samples for name in row["landmarks_seconds_from_caller"]})}}
    return result
