"""Parent and isolated-child call counters for the LS-15 packet proof only."""
from __future__ import annotations

import collections
import contextlib
import inspect
import json
import sys
import threading
import time
from pathlib import Path


def category(frame):
    module = frame.f_globals.get("__name__", "")
    name = frame.f_code.co_name
    qualified = frame.f_code.co_qualname
    if module == "subprocess" and qualified == "Popen.__init__":
        return "process_starts"
    if module.startswith("vibecomfy."):
        if name in {"emit_bundle", "emit_bundle_with_candidate", "materialize_ui_json", "materialize_ui"}:
            return "emissions"
        if name == "compile":
            return "compiles"
        if name in {"object_info", "get_schema", "_build_schema_provider", "_warm_schema_provider"}:
            return "schema_fetches"
        if name in {"template_index", "coverage_manifest", "load_default"}:
            return "pack_scans"
        if name in {"_approval_preconditions", "_rebind_current_bundle"} or qualified == "PreparedWorkflowSession.prepare":
            return "audits"
        if qualified in {"PreparedWorkflowSession.fill", "PreparedWorkflowSession.authorize"}:
            return "per_job_authorization"
    if module.startswith("astrid."):
        if module.endswith("core.pack.discovery") and name.startswith("discover_"):
            return "pack_scans"
        if module.endswith("generation.entries") and name in {
                "user_entries", "contributed_entries", "_ready_entries_for", "action_entries",
                "_discover_action_entries", "find_entry", "describe_entry", "_catalog_inputs", "_load_entry_workflow"}:
            return "pack_scans"
        if module.endswith("comfy_target") and name in {"object_info", "folder_paths", "_capture_model_resolution"}:
            return "schema_fetches"
        if name in {"_live_preflight", "_approve_live", "prepare_local_workflow", "_capture_live_schemas"}:
            return "audits"
        if qualified in {"ManagedEnginePreparation.prepare", "_Runner.check_health"}:
            return "audits"
        if qualified == "_Runner.execute" and getattr(frame.f_locals.get("step"), "kind", None) in {"source", "environment"}:
            return "audits"
        if name in {"pack_digest", "_workflow_content_digest"}:
            return "source_validation"
    return None


class Counters:
    zero_categories = ("process_starts", "emissions", "compiles", "schema_fetches", "pack_scans", "audits")

    def __init__(self, monkeypatch, root):
        from astrid.core.execution import generic_host
        self.root = Path(root)
        self.child = self.root / "child-counters.jsonl"
        self.rows = []
        self.captures = []
        original_argv = generic_host._captured_worker_argv
        # The real isolated bootstrap and captured-source checks still run.
        # No product hook or replacement compilation path participates.
        startup = ("import os,sys,threading,json\n" + inspect.getsource(category) +
            f"\n_ls15_fd=os.open({str(self.child)!r},os.O_WRONLY|os.O_APPEND|os.O_CREAT,0o600)\n" +
            "def _ls15_profile(frame,event,arg):\n"
            " if event=='call':\n"
            "  kind=category(frame)\n"
            "  if kind:\n"
            "   detail=None\n"
            "   if kind=='process_starts':\n"
            "    detail={'args':repr(frame.f_locals.get('args')),'stack':[]}\n"
            "    caller=frame.f_back\n"
            "    for _ in range(8):\n"
            "     if caller is None:break\n"
            "     detail['stack'].append(caller.f_code.co_qualname);caller=caller.f_back\n"
            "   os.write(_ls15_fd,(json.dumps([kind,frame.f_globals.get('__name__'),frame.f_code.co_qualname,detail])+'\\n').encode())\n"
            "sys.setprofile(_ls15_profile)\nthreading.setprofile(_ls15_profile)\n")

        def captured(argv):
            result = original_argv(argv)
            if "--resident-session" in argv:
                result[result.index("-c") + 1] = startup + result[result.index("-c") + 1]
            return result
        monkeypatch.setattr(generic_host, "_captured_worker_argv", captured)
        original_capture = generic_host._portable_pack_capture

        @contextlib.contextmanager
        def capture(*args, **kwargs):
            started = time.monotonic()
            with original_capture(*args, **kwargs) as value:
                if value is not None:
                    files = [path for path in value[2].rglob("*") if path.is_file()]
                    self.captures.append({"pack": value[0], "files": len(files),
                        "bytes": sum(path.stat().st_size for path in files),
                        "capture_seconds": time.monotonic() - started})
                yield value
        monkeypatch.setattr(generic_host, "_portable_pack_capture", capture)

    def profile(self, frame, event, arg):
        if event == "call" and (kind := category(frame)):
            detail = None
            if kind == "process_starts":
                detail = {"args": repr(frame.f_locals.get("args")), "stack": []}
                caller = frame.f_back
                for _ in range(7):
                    if caller is None:
                        break
                    detail["stack"].append(caller.f_code.co_qualname)
                    caller = caller.f_back
            self.rows.append([kind, frame.f_globals.get("__name__"), frame.f_code.co_qualname, detail])

    @contextlib.contextmanager
    def observe(self):
        previous, thread_previous = sys.getprofile(), threading.getprofile()
        sys.setprofile(self.profile)
        threading.setprofile(self.profile)
        try:
            yield self
        finally:
            sys.setprofile(previous)
            threading.setprofile(thread_previous)

    def snapshot(self):
        child = [json.loads(line) for line in self.child.read_text().splitlines()] if self.child.exists() else []
        return {"parent": list(self.rows), "child": child, "captures": list(self.captures)}

    def difference(self, before, after):
        result = {scope: after[scope][len(before[scope]):] for scope in ("parent", "child", "captures")}
        for scope in ("parent", "child"):
            result[scope + "_counts"] = dict(collections.Counter(row[0] for row in result[scope]))
        return result
