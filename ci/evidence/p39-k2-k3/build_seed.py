"""Reproducible, conservative initial admission; never run by a product CI job."""
from pathlib import Path
import ast
from collections import Counter
import gzip
import hashlib
import importlib.util
import json
import re
import shutil
import sys
import textwrap

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
EVIDENCE = ROOT / "evidence-research"
spec = importlib.util.spec_from_file_location("gate", ROOT / "astrid-runtime/scripts/ci/failure_ceiling.py")
gate = importlib.util.module_from_spec(spec); spec.loader.exec_module(gate)
G1_HASH = "d4a4b2d5bce5324c47d0018db9ce651e979550c9361f74354caf65b939a99879"
G2_HASH = "50760681220707845c9bd50e6795a0914fce9f705177359f6300ebc9324be522"
for filename, expected in (("g1-report.json.gz", G1_HASH), ("g2-report.json.gz", G2_HASH)):
    assert hashlib.sha256((EVIDENCE / filename).read_bytes()).hexdigest() == expected
g1 = json.loads(gzip.decompress((EVIDENCE / "g1-report.json.gz").read_bytes()))
g2 = json.loads(gzip.decompress((EVIDENCE / "g2-report.json.gz").read_bytes()))
pairs = json.loads((EVIDENCE / "r15-explicit-owner-candidates.json").read_text())
jobs = {j["id"]: j for j in g2["jobs"]}


def lane(workflow, name):
    if re.fullmatch(r"unit shard \d+ / 8 \(ubuntu-latest\)", name): name = "unit (ubuntu-latest)"
    if re.fullmatch(r"integration shard \d+ / 4", name): name = "integration"
    return workflow + "/" + name


def parameter(node):
    return node[node.index("["):] if "[" in node else None


def key(lane_name, node, subtest=None):
    return {"lane": lane_name, "native_id": node, "parameter": parameter(node) if node.startswith("tests/") or node.startswith("tools/") else None, "subtest": subtest}


def omission(record):
    if record.get("outcome") not in {"skipped", "SKIPPED", "xfailed", "XFAIL"}: return None
    reason = record.get("wasxfail") or record.get("detail") or record.get("diagnostic") or record.get("message") or ""
    try:
        parsed = ast.literal_eval(reason)
        if isinstance(parsed, tuple) and len(parsed) == 3: reason = parsed[2]
    except (ValueError, SyntaxError): pass
    return {"outcome": "xfailed" if record.get("wasxfail") or record.get("outcome") in {"xfailed", "XFAIL"} else "skipped", "diagnostic": gate.diagnostic(reason)}


def source_record(witness, identity, diagnostic):
    j = witness["job"]
    return {**identity, "outcome": "failed", "stage": "call", "diagnostic": diagnostic,
            "sha": j["sha"], "run_id": j["run_id"], "job_id": j["id"],
            "attempt": j.get("attempt", 1), "source": j["url"]}


inventories = {repo: {} for repo in ("astrid-agent", "astrid-runtime", "astrid-app")}
sources = {repo: [] for repo in inventories}
def add(repo, item):
    k = gate.identity(item)
    old = inventories[repo].get(k)
    if old and old != item:
        # Multiple runs never buy a broader skip allowance.
        if old.get("allowed_omission") != item.get("allowed_omission"):
            old.pop("allowed_omission", None)
        return
    inventories[repo][k] = item

# Agent primary selections and raw omissions/subtests, including failed collection.
raw_index = json.loads((EVIDENCE / "raw-g2-agent/native-evidence-index.json").read_text())
artifact_receipts = json.loads((EVIDENCE / "raw-g2-agent/artifact-receipts.json").read_text())
for info in raw_index:
    events = [json.loads(line) for line in Path(info["jsonl"]).read_text().splitlines()]
    reference = json.loads(Path(info["reference"]).read_text())
    name = info["name"]
    if name.startswith("unit"): ln = "CI/unit (ubuntu-latest)"
    elif name.startswith("integration"): ln = "Lanes/integration"
    else: ln = "CI/windows-native-and-installer/" + name
    reports = [e for e in events if e.get("type") == "report"]
    by_id = {}
    for r in reports:
        if r.get("outcome") == "skipped": by_id[r["nodeid"]] = r
    for node in reference["selected"]:
        item = key(ln, node)
        if node in by_id and (allowed := omission(by_id[node])): item["allowed_omission"] = allowed
        add("astrid-agent", item)
    for r in reports:
        if r.get("subtest"):
            item = key(ln, r["nodeid"], r.get("context"))
            if allowed := omission(r): item["allowed_omission"] = allowed
            add("astrid-agent", item)
    sources["astrid-agent"].append({"artifact_id": info["artifact_id"], "name": name, "selected": len(reference["selected"]),
                                   "complete": bool(info.get("finish", {}).get("exitstatus") in (0,1) and not info.get("missing"))})
for job in g2["jobs"]:
    if job["repo"] != "astrid-agent" or job["workflow"] != "Lanes" or job["name"] != "opt-in": continue
    for r in job.get("all_outcomes", []):
        if r.get("nodeid"): add("astrid-agent", key(lane(job["workflow"], job["name"]), r["nodeid"]))

# Runtime native inventories are read from complete, independently verified bundles.
runtime = {14433570144: "CI/unit (ubuntu-latest)", 14441948450: "CI/unit (macos-latest)",
           14432965131: "CI/sandbox-process-control", 14431410903: "CI/portability-lifecycle (ubuntu-latest)",
           14430684562: "CI/portability-lifecycle (macos-latest)", 14429154793: "Nightly/integration"}
for aid, ln in runtime.items():
    inv = json.loads((EVIDENCE / f"raw-g2-runtime-app/{aid}/native-inventory.json").read_text())
    assert not inv["checker_errors"] and not inv["missing_teardowns"] and not inv["unexpected_teardowns"]
    for node in inv["selected_nodeids"]:
        item = key(ln, node)
        for r in inv["primary_reports"].get(node, []):
            if allowed := omission(r):
                item["allowed_omission"] = allowed
                if allowed["outcome"] == "xfailed": allowed["expires"] = "2026-12-01"
        add("astrid-runtime", item)
    sources["astrid-runtime"].append({"artifact_id": aid, "sha256": inv["artifact_sha256"], "lane": ln,
                                    "selected": len(inv["selected_nodeids"]), "subtest_context": "unavailable in original reporter"})

for filename, ln in (("vitest-app.xml.jsonl.inventory.json", "Tests/vitest/app"),
                     ("vitest-sdk-boundary.xml.jsonl.inventory.json", "Tests/vitest/sdk-boundary")):
    data = json.loads((EVIDENCE / "raw-g2-runtime-app/14425679839" / filename).read_text())
    for r in data["collected"]:
        add("astrid-app", {**key(ln, r["id"]), "source": {"file": r["file"], "name": r["name"]}})
    sources["astrid-app"].append({"artifact_id": 14425679839, "group": filename, "selected": len(data["collected"])})

admitted = {repo: [] for repo in inventories}
rejected = {repo: [] for repo in inventories}
# Conservative lexical screening is an exclusion only; it never admits a case.
sensitive = re.compile(r"custody|persist|retry|publication|publish|rollback|durable|lost_ack|lost_acknow|save|commit|history|permission|redact|private|secret|cleanup|cancel", re.I)
for pair in pairs:
    repo = pair["repo"]
    node = pair.get("native_id", pair.get("observed_tuple", {}).get("title", ""))
    reason = None
    unresolved_sensitive_runtime = {"tests/test_direct_render_admission.py", "tests/test_linux_listener.py", "tests/test_operator_lifecycle_sol_closures.py", "tests/test_process_control.py", "tests/test_root_generation_query.py", "tests/test_spec_dedup.py", "tests/test_timeline_producer_checkpoint.py", "tests/test_timeline_verified_media.py"}
    if repo == "astrid-runtime" and node.split("::")[0] in unresolved_sensitive_runtime:
        reason = "matching error alone does not resolve custody/persistence/settlement/refusal cause; outside-cut admission not established"
    if repo == "astrid-agent" and node.split("::")[0] in {"tests/packs/builtin/test_training_run_support.py", "tests/test_provider_route_grant.py"}:
        reason = "live-run record or provider-grant cause unresolved; no sensitive admission from a family label"
    if repo == "astrid-app": reason = "historical internal/occurrence identity and failing-stage binding need a reviewed source-tuple reconciliation"
    elif sensitive.search(node): reason = "sensitive case retained outside ceiling pending explicit cause/installed-witness review"
    old = pair["g1"][0]
    new = pair["g2"]
    a, b = old["record"], new["record"]
    message = gate.diagnostic(a.get("full_message") or a.get("message") or "")
    actual = gate.diagnostic(b.get("full_message") or b.get("message") or "")
    if not reason and (a.get("outcome") not in {"FAILED", "SUBFAILED"} or b.get("outcome") not in {"FAILED", "SUBFAILED"}):
        reason = "failure stage is not bound to the call phase on both measurements"
    if not reason and b.get("failure_phases") not in (None, ["call"]): reason = "G2 has another/ambiguous failure stage"
    if not reason and (not message or len(message) < 20 or "..." in message or "…" in message): reason = "G1 diagnostic is missing, too weak or truncated"
    error_lines = "\n".join(re.findall(r"^E\s+(.*)$", actual, re.M))
    if not reason and message not in {actual, gate.diagnostic(b.get("message", "")), gate.diagnostic(error_lines)}:
        reason = "full error diagnostic differs or no exact untruncated match is available"
    if not reason and len(pair["g1"]) != 1: reason = "multiple baseline observations require phase/diagnostic reconciliation"
    if not reason:
        identity = key(lane(pair["workflow"], pair["lane"]), node, b.get("context") if b.get("subtest") else None)
        record = {**identity, "owner": pair["r15_owner_row"], "source_owner": pair["owner_row"], "release": 15,
                  "admission": "explained-inherited", "stage": "call", "diagnostic": message,
                  "cause": b.get("cause", "explicit R15 family in the source task table"),
                  "g1": source_record(old, identity, message), "g2": source_record(new, identity, message)}
        admitted[repo].append(record)
    else:
        rejected[repo].append({"lane": pair["workflow"] + "/" + pair["lane"], "native_id": node,
                               "owner": pair.get("r15_owner_row"), "reason": reason})

# Full source tuples were reconciled against unique unchanged declarations and
# complete native annotations. No runner-internal ID is inferred from a title.
rejected["astrid-app"] = []
app_facts = json.loads((EVIDENCE / "app-source-tuple-stage-facts.json").read_text())
for pair in app_facts["pairs"]:
    t = pair["observed_tuple"]
    node = json.dumps([t["project"], t["file"], int(t["line"]), int(t["column"]), t["title"], 0], ensure_ascii=False, separators=(",", ":"))
    ident = {"lane": "E2E/" + pair["lane"], "native_id": node, "parameter": {"project": t["project"], "repeat": 0}, "subtest": None}
    add("astrid-app", ident)
    if t["file"] == "tests/e2e/timeline/editor-tour.spec.ts" and int(t["line"]) == 375:
        rejected["astrid-app"].append({"lane": ident["lane"], "native_id": node, "owner": pair["r15_owner_row_from_input"], "reason": "expected persisted tour status is undefined; matching diagnostic does not establish a non-persistence cause"})
        continue
    a, b = pair["g1"][0], pair["g2"]
    if isinstance(b, list): b = b[0]
    ta, tb = a["trace_records"][0], b["trace_records"][0]
    fa, fb = ta["last_frame_in_declaring_test_file"], tb["last_frame_in_declaring_test_file"]
    assert tuple(fa[k] for k in ("file", "line", "column")) == tuple(fb[k] for k in ("file", "line", "column"))
    diag_a = gate.diagnostic(textwrap.dedent(a["native_github_annotations"][0]["full_error_segment"]))
    diag_b = gate.diagnostic(textwrap.dedent(b["native_github_annotations"][0]["full_error_segment"]))
    assert diag_a == diag_b and diag_a
    assert a["test_source"]["blob"] == b["test_source"]["blob"]
    assert not a["repeat_control_occurrences_in_test_config_workflow_package_wrapper"]
    assert not b["repeat_control_occurrences_in_test_config_workflow_package_wrapper"]
    assert a["matching_native_github_annotations"] == b["matching_native_github_annotations"] == 1
    stage = f'{fa["file"]}:{fa["line"]}:{fa["column"]}'
    witnesses = []
    for witness in (a, b):
        j = witness["job"]
        witnesses.append({**ident, "outcome": "failed", "stage": stage, "diagnostic": diag_a,
            "sha": j["sha"], "run_id": j["run_id"], "job_id": j["id"], "attempt": j["attempt"], "source": j["url"]})
    admitted["astrid-app"].append({**ident, "owner": pair["r15_owner_row_from_input"], "source_owner": pair["owner_row_from_input"],
        "release": 15, "admission": "explained-inherited", "stage": stage, "diagnostic": diag_a,
        "cause": "unchanged uniquely expanded source declaration and matching complete native error/stage; see app-source-tuple-stage-facts.json.gz",
        "identity_basis": "project, file, declaration line/column, complete title, repeat index from unchanged source/config; internal ID unavailable historically",
        "g1": witnesses[0], "g2": witnesses[1]})
    add("astrid-app", ident)

# Existing omissions need both native outcomes and an explicit source-owned
# registry binding. A reason alone never creates a new allowance.
omission_facts = json.loads((EVIDENCE / "native-omission-source-facts.json").read_text())
for items in inventories.values():
    for item in items.values(): item.pop("allowed_omission", None)
for fact in omission_facts["reports"]:
    repo = fact["repo"]
    ln = lane("Nightly" if repo == "astrid-runtime" and fact["lane"] == "integration" else "CI", fact["lane"])
    ident = key(ln, fact["native_id"])
    item = inventories[repo][gate.identity(ident)]
    binding = omission_facts["source_bindings"][fact["source_binding"]]
    source_bound = binding.get("source_marker_name_matches_native_registry_key")
    if fact["native_kind"] == "xfail":
        matches = binding.get("registry_reason_strict_raises_source_matches", [])
        source_bound = (binding.get("exact_registry_node_present") and len(matches) == 1
                        and all(matches[0].get(k) for k in ("reason_equal", "strict_equal", "raises_name_equal"))
                        and fact["quarantine_expiry"])
    if (not fact.get("registry_fact") or not fact.get("native_reason_matches_registry")
            or not source_bound or binding["declaration_ambiguities"]):
        item["observed_unapproved_omission"] = {"diagnostic": fact["native_reason"], "reason": "no explicit registry binding; source fact is not an allowance"}
        continue
    declaration = binding["declaration"]
    allowed = {"outcome": "xfailed" if fact["native_kind"] == "xfail" else "skipped", "diagnostic": gate.diagnostic(fact["native_reason"]),
               "source": f'https://github.com/banodoco/{repo}/blob/{fact["g2_revision"]}/{binding["file"]}#L{declaration["line"]}',
               "registry": fact["registry_fact"]}
    if fact["quarantine_expiry"]: allowed["expires"] = fact["quarantine_expiry"]
    item["allowed_omission"] = allowed

for repo in inventories:
    out = ROOT / repo / "ci"
    evidence = out / "evidence/p39-k2-k3"; evidence.mkdir(parents=True, exist_ok=True)
    authority = {"dev_sha": "4223b30a2b56c7eb59da3a47b200516372bdca5a", "decision": "6995664680", "amendment": "6996255390",
                 "seed_and_reconciliation_decision": "6998651356", "g1_report_sha256": G1_HASH, "g2_report_sha256": G2_HASH}
    ledger = {"schema": 1, "repository": "banodoco/" + repo, "authority": authority,
              "entries": sorted(admitted[repo], key=lambda r: gate.key_text(gate.identity(r))), "retired": []}
    gate.validate_ledger(ledger, "banodoco/" + repo)
    (out / "failure-ceiling.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")
    expected = sorted(inventories[repo].values(), key=lambda r: gate.key_text(gate.identity(r)))
    lanes = sorted({r["lane"] for r in expected})
    unresolved = [{"id": "RS1.3-nine-literal-missing-IDs", "status": "resolved", "authority": "6998651356", "reason": "eight exact aliases and ID9's two explicit coverage replacements accepted by Lead; no global rename or masking allowance"}]
    unresolved.append({"id": "complete-native-inventory", "status": "unknown", "reason": "lanes without a full retained census must supply complete native collection; see seed-summary.json"})
    inventory = {"schema": 1, "repository": "banodoco/" + repo, "authority": authority,
                 "tiers": {"merge": [x for x in lanes if not x.startswith(("Lanes/", "Nightly/", "E2E/", "Direct Journey/"))], "release": lanes},
                 "reconciliation": unresolved, "tests": expected}
    (out / "test-inventory.json.gz").write_bytes(gzip.compress((json.dumps(inventory, separators=(",", ":"), ensure_ascii=False) + "\n").encode(), mtime=0))
    summary = {"schema": 1, "repository": repo, "authority": authority, "admitted": len(admitted[repo]),
               "explicit_candidates_not_admitted": len(rejected[repo]), "native_inventory": len(expected),
               "owners": dict(Counter(r["owner"] for r in admitted[repo])), "source_artifacts": sources[repo],
               "not_admitted": rejected[repo], "limits": ["residual R15-1/R15-2 groups require supported cause evidence; no blanket admission",
               "App identities use uniquely reconciled full source tuples; internal IDs remain unavailable in historical logs",
               "no later exact-head product retirement receipt was found; executable fixture proof is separate"]}
    (evidence / "seed-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    for file in ("nine-id-reconciliation.json", "subtest-reconciliation-33.json", "coverage-gap-native-records.json", "app-source-tuple-stage-facts.json", "native-omission-source-facts.json", "lead-6998651356.json"):
        raw = (EVIDENCE / file).read_bytes()
        (evidence / (file + ".gz")).write_bytes(gzip.compress(raw, mtime=0))
    shutil.copyfile(__file__, evidence / "build_seed.py")
    print(repo, "admitted",len(admitted[repo]),"excluded",len(rejected[repo]),"inventory",len(expected))
