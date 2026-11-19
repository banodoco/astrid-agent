"""Run one original SDK first job and retain a path-free diagnostic receipt."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

PINS = {
    "source-a": "e379565641414bc183cd02edebab1cbf77617931",
    "source-b": "7194011c2f41643a699f261c5276886612bc2186",
    "runtime": "62333bd797e84b21cb3995e4bcf580ed0bdcf9f0",
    "vibe": "1ba0ac3ad4fe69fa0b4966cc41e6852224013f97",
    "lifecycle": "7f0b025e9391baac9f5c2fb7393617ea6fef0704",
}
HARNESS = {
    "tests/ls12_phases.py": "c39331b387d733bc91e29b0cdbb548b74784a9f70495173a57809e727d93fc03",
    "tests/_local_speed_process.py": "d19f49cc7dba264adbd5eb1cef3971e69d113915d181f7cd5006e38a056a59dc",
    "tests/_local_speed_hashes.py": "6bf524ec4b1072e60a5af0742079dfb24306ed0bce2b55600314c279b2ed33ff",
    "tests/integrations/test_ls12_first_job.py": "7a366307b5b575e7bdcc33cb3fc3a6e1111c18094c2de4ee481ff46577758192",
    "tests/integrations/ls12_image_fixture.py": "1ae45666e93adaf3c5c43c8268e1fe56172233cb5081c3180c50d9047a00a9e4",
}
TEST = "tests/integrations/test_ls12_first_job.py::test_first_attached_job_observation[0-resident_sdk]"
TOOLS = Path(__file__).resolve().parent


class InvalidReceipt(Exception):
    """The message is a public-safe, fixed diagnostic code."""


def require(condition, code):
    if not condition:
        raise InvalidReceipt(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(path, *args):
    return subprocess.check_output(["git", "-C", path, *args])


def summarize(trace, observation, phase_sites):
    require(trace["test_id"] == TEST and observation["caller"] == "resident_sdk"
            and observation["sample_index"] == 0 and observation["instrumented"]
            and not observation["observer_gaps"], "wrong_or_incomplete_observation")
    start = observation["interval"]["monotonic_start_ns"]
    stop = observation["interval"]["monotonic_reply_ns"]
    spans = [s for s in trace["spans"] if s["end_ns"] and start <= s["start_ns"] <= s["end_ns"] <= stop]
    reads = [r for r in trace["reads"] if start <= r["at_ns"] <= stop and
             any(p.startswith("astrid.core.pack.identity:") for p in r["parents"])]
    files = [r for r in trace["file_digests"] if r["end_ns"] and start <= r["start_ns"] <= r["end_ns"] <= stop
             and any(p.endswith(":_tree_fingerprint") for p in r["parents"])]
    require(reads and files and all(r["successful"] and r["start_position"] == 0 and
            isinstance(r["end_position"], int) for r in files), "missing_or_incomplete_logical_reads")
    def union_ns(rows, a, b):
        total = last = 0
        for lo, hi in sorted((row[a], row[b]) for row in rows):
            total += max(0, hi - max(lo, last))
            last = max(last, hi)
        return total
    phases = {}
    for name in ("discovery_fingerprint", "pack_verify", "workflow_load"):
        rows = [s for s in spans if phase_sites.get(s["site"]) == name]
        require(rows, "missing_phase_" + name)
        cpus = [union_ns([s for s in rows if s["tid"] == thread], "cpu_start_ns", "cpu_end_ns")
                for thread in {s["tid"] for s in rows}]
        phases[name] = {"spans": len(rows), "parent_union_wall_ns": union_ns(rows, "start_ns", "end_ns"),
                        "parent_thread_union_cpu_ns": sum(cpus)}
    member_reads = [r for r in reads if Path(r["path"]).name == "local_server_engine.py"]
    nested = [r for r in trace["reads"] if start <= r["at_ns"] <= stop and
              "astrid.sdk.generation:_check_workflow_source_admission" in r["parents"]]
    discovery_bytes = {}
    for r in files:
        suffix = Path(r["path"]).suffix
        category = {".py": "python_source", ".pyc": "bytecode"}.get(suffix, "other")
        discovery_bytes[category] = discovery_bytes.get(category, 0) + r["end_position"]
    trees = [s for s in spans if s["site"] in ("astrid.core.pack.identity:_read_pack_tree", "astrid.core.pack.identity:pack_digest")]
    outer = [s for s in trees if not any(t is not s and t["tid"] == s["tid"]
             and t["start_ns"] <= s["start_ns"] and s["end_ns"] <= t["end_ns"] for t in trees)]
    require(outer, "missing_pack_pass")
    return {"local_server_engine_reads": len(member_reads),
            "local_server_engine_bytes": sum(r["bytes"] for r in member_reads),
            "source_admission_reads": len(nested), "source_admission_bytes": sum(r["bytes"] for r in nested),
            "discovery_bytes_by_kind": discovery_bytes, "pack_passes": len(outer), "pack_reads": len(reads), "pack_bytes": sum(r["bytes"] for r in reads),
            "discovery_calls": sum(s["site"].endswith(":_discovery_fingerprint") for s in spans),
            "tree_calls": sum(s["site"].endswith(":_tree_fingerprint") for s in spans),
            "file_digests": len(files), "file_bytes": sum(r["end_position"] for r in files),
            "readinto_calls": sum(r["readinto_calls"] for r in files), "phases": phases}


def qualify_origins(trace, source, vibe):
    origins = {}
    for site, paths in trace["origins"].items():
        root = source if site.startswith("astrid.") else vibe
        for raw in paths:
            path = Path(raw).resolve()
            require(path.is_relative_to(root), "unexpected_module_origin")
            rel = path.relative_to(root).as_posix()
            expected = git(root, "show", "HEAD:" + rel)
            require(expected == path.read_bytes(), "unexpected_module_bytes")
            origins[site] = {"path": rel, "sha256": digest(expected)}
    require(origins, "missing_module_origin")
    return origins


def dependency_receipt(loaded, source, workspace):
    """Report actual loaded source ownership, including the Agent's generated client."""
    roots = {"agent": source, "runtime": workspace/"runtime", "lifecycle": workspace/"lifecycle"}
    manifests = {}
    for owner, root in roots.items():
        entries = git(root, "ls-tree", "-r", "-z", "HEAD").split(b"\0")
        index = {}
        for entry in filter(None, entries):
            meta, rel = entry.split(b"\t", 1)
            index[os.fsdecode(rel)] = meta.split()[-1].decode()
        manifests[owner] = index
    result = {key: {"status": "checkout_only", "modules": []} for key in roots}
    failures = []
    for module, raw in sorted(loaded.items()):
        path = Path(raw).resolve()
        owner = next((name for name, root in roots.items() if path.is_relative_to(root)), None)
        if owner is None:
            failures.append({"module": module, "reason": "outside_attested_roots"})
            continue
        rel = path.relative_to(roots[owner]).as_posix()
        data = path.read_bytes()
        blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if manifests[owner].get(rel) != blob:
            failures.append({"module": module, "reason": "unverified_module_bytes"})
            continue
        result[owner]["status"] = "loaded_in_original_pytest_process"
        result[owner]["modules"].append({"module": module, "path": rel, "git_blob": blob})
    return {"owners": result, "unverified": failures}


def profiler_diagnostics(log):
    marker = "Exception ignored in:"
    warnings = log.count("PytestUnraisableExceptionWarning")
    signatures = []
    for part in log.split(marker)[1:]:
        exception = re.search(r"(?m)^([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)): (.*)$", part)
        row = {"class": exception[1] if exception else "unknown"}
        if exception:
            row["message_sha256"] = digest(exception[2].encode())
            if exception[2] == "argument of type 'NoneType' is not iterable":
                row["message"] = exception[2]
        signatures.append(row)
    return {"ignored_exceptions": signatures, "unraisable_warning_mentions": warnings}


def native_receipt(xml, receipt, public_file):
    receipt["junit_sha256"] = digest(xml)
    cases = ET.fromstring(xml).findall(".//testcase")
    require(len(cases) == 1 and cases[0].get("classname") == "tests.integrations.test_ls12_first_job"
            and cases[0].get("name") == TEST.split("::")[1], "unexpected_native_inventory")
    outcome = next((tag for tag in ("error", "failure", "skipped") if cases[0].find(tag) is not None), "passed")
    receipt["native_outcome"] = outcome
    # Keep native identity/outcome and provenance, strip captured streams and traceback paths.
    suite = ET.Element("testsuite", name="ls12-observed-sdk", tests="1",
                       errors=str(int(outcome == "error")), failures=str(int(outcome == "failure")),
                       skipped=str(int(outcome == "skipped")))
    info = ET.SubElement(suite, "properties")
    ET.SubElement(info, "property", name="original_sha256", value=receipt["junit_sha256"])
    ET.SubElement(info, "property", name="original_pytest_exit", value=str(receipt["pytest_exit"]))
    item = ET.SubElement(suite, "testcase", classname=cases[0].get("classname"), name=cases[0].get("name"))
    if outcome != "passed":
        ET.SubElement(item, outcome)
    ET.ElementTree(suite).write(public_file, encoding="utf-8", xml_declaration=True)
    return outcome


def run(cell, receipt):
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    require(git(workspace/"controller", "rev-parse", "HEAD").decode().strip() == receipt["controller"],
            "wrong_controller_commit")
    source = workspace / cell
    private = Path(os.environ["RUNNER_TEMP"]) / ("ls12-" + cell)
    private.mkdir()
    for name, sha in PINS.items():
        require(git(workspace / name, "rev-parse", "HEAD").decode().strip() == sha, "wrong_source_pin")
    require(json.loads((source/"runtime.lock.json").read_bytes())["source_commit"] == PINS["runtime"] and
            json.loads((source/"astrid/packs/vibecomfy/pins.json").read_bytes())["sources"]["vibecomfy"]["revision"]
            == PINS["vibe"], "wrong_dependency_authority")
    for name in (cell, "runtime", "vibe", "lifecycle"):
        git(workspace/name, "diff", "--exit-code", "--quiet", "HEAD")
    for name, sha in HARNESS.items():
        data = (workspace/"source-b"/name).read_bytes()
        require(digest(data) == sha, "harness_digest_mismatch")
        if cell == "source-a":
            (source/name).write_bytes(data)
    env = dict(os.environ)
    for key in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        folder = private/key.lower()
        folder.mkdir()
        env[key] = str(folder)
    env.update(ASTRID_CI_JOB="lanes-integration", ASTRID_RUNTIME_CHECKOUT=str(workspace/"runtime"),
               ASTRID_VIBECOMFY_CHECKOUT=str(workspace/"vibe"), ASTRID_TEST_LIFECYCLE_CHECKOUT=str(workspace/"lifecycle"),
               ASTRID_LS12_SAMPLES="1", ASTRID_LS12_OBSERVE="1", ASTRID_LS12_PHASES="1", PYTHONNOUSERSITE="1",
               PYTHONDONTWRITEBYTECODE="1", LS12_OWNED_TRACE=str(private/"trace.json"),
               PYTHONPATH=os.pathsep.join(map(str, (TOOLS, source, workspace/"runtime",
                    workspace/"runtime/packages/python", workspace/"vibe", workspace/"lifecycle/src"))),
               PATH=str(Path(sys.executable).parent)+os.pathsep+env["PATH"])
    command = [sys.executable, "-m", "pytest", "-q", "-s", "-rA", "-m", "integration", TEST,
               "-p", "ls12_owned_trace", "--junitxml", str(private/"native.xml"), "--basetemp", str(private/"pytest")]
    with (private/"console").open("w") as log:
        receipt["pytest_exit"] = subprocess.run(command, cwd=source, env=env, timeout=900, stdout=log, stderr=subprocess.STDOUT).returncode
    log = (private/"console").read_text(errors="replace")
    receipt["console_sha256"] = digest((private/"console").read_bytes())
    faults = profiler_diagnostics(log)
    receipt["profiler_diagnostics"] = faults
    receipt["observer_shutdown_error"] = bool(faults["ignored_exceptions"] or faults["unraisable_warning_mentions"])
    xml = (private/"native.xml").read_bytes()
    public = Path(os.environ["RUNNER_TEMP"])/"ls12-public"
    outcome = native_receipt(xml, receipt, public/(cell + ".junit.xml"))
    require(receipt["pytest_exit"] == 0 and outcome == "passed", "original_case_did_not_pass")
    reports = [json.loads(line.split(" ",1)[1]) for line in log.splitlines() if line.startswith("LS12_FIRST_JOB_OBSERVATION ")]
    require(len(reports) == 1, "missing_or_duplicate_native_report")
    trace_data = (private/"trace.json").read_bytes()
    trace = json.loads(trace_data)
    receipt["trace_sha256"] = digest(trace_data)
    receipt["profile_state"] = trace["profile_state"]
    receipt["loaded_dependencies"] = dependency_receipt(trace["loaded_dependencies"], source, workspace)
    receipt["origins"] = qualify_origins(trace, source, workspace/"vibe")
    receipt["measurements"] = summarize(trace, reports[0], reports[0]["phase_observations"]["phase_sites"])
    require(not receipt["loaded_dependencies"]["unverified"], "unverified_module_origins")
    require(trace["profile_state"]["current_hook_restored"] and trace["profile_state"]["thread_hook_restored"],
            "profile_hook_restoration_failed")
    require(not receipt["observer_shutdown_error"], "observer_shutdown_error")


if __name__ == "__main__":
    cell = sys.argv[1]
    require(cell in ("source-a","source-b"), "unknown_cell")
    public = Path(os.environ["RUNNER_TEMP"])/"ls12-public"
    public.mkdir(exist_ok=True)
    receipt = {"diagnostic_only": True, "cell": cell, "source": PINS[cell], "pins": PINS, "selector": TEST,
               "controller": os.environ["GITHUB_SHA"], "harness_sha256": HARNESS,
               "profiler_sha256": digest((TOOLS/"ls12_owned_trace.py").read_bytes()),
               "runner_image": os.environ.get("ImageVersion"), "python": platform.python_version(),
               "platform": platform.platform(), "pytest_exit": None}
    try:
        run(cell, receipt)
    except Exception as error:
        receipt["error"] = str(error) if isinstance(error, InvalidReceipt) else type(error).__name__
        raise
    finally:
        (public/(cell+".json")).write_text(json.dumps(receipt, indent=2, sort_keys=True)+"\n")
