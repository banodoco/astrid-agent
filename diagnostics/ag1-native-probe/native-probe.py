#!/usr/bin/env python3
"""Read-only P19 candidate probe. Run as the real non-root service user."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

p = argparse.ArgumentParser()
p.add_argument("--agent", type=Path, required=True, help="candidate Git checkout")
p.add_argument("--head", required=True, help="exact reviewed candidate Git commit")
p.add_argument("--root", type=Path, required=True, help="installed program root")
p.add_argument("--data", type=Path, required=True, help="installed data root")
p.add_argument("--home", type=Path, required=True, help="service user's actual home")
p.add_argument("--expect", choices=("running", "stopped"), required=True)
a = p.parse_args()
assert os.getuid() != 0 and os.getuid() == os.geteuid(), "requires real non-root service user"
assert all(path.is_absolute() for path in (a.agent, a.root, a.data, a.home))
assert Path.home() == a.home, "run under the selected service user's actual HOME"
head = subprocess.check_output(["git", "-C", str(a.agent), "rev-parse", "HEAD"], text=True).strip()
assert head == a.head, "checkout is not the exact candidate head"
assert not subprocess.check_output(["git", "-C", str(a.agent), "status", "--porcelain", "--", "astrid/installer/service.py"], text=True).strip(), "service.py is dirty"
sys.path.insert(0, str(a.agent))
from astrid.installer.core import current
from astrid.installer.service import Service

version = current(a.root)
assert version is not None, "selected Version unavailable"
def no_mutation(*args, **kwargs):
    raise AssertionError("unexpected service operation")
manager = Service(a.root, a.home / ".local/state/astrid-runtime/logs/runtime-service.log", no_mutation,
                  home=a.home, system="linux")
snapshot = manager._snapshot_linux_r0(version, a.data)
results = {key: value.result for key, value in snapshot.predicates.items()}
print(json.dumps({"head": head, "uid": os.getuid(), "binding_digest": snapshot.binding_digest,
                  "started_ns": snapshot.started_ns, "ended_ns": snapshot.ended_ns,
                  "authority": snapshot.authority,
                  "predicates": {k: {"result": v.result, "reason": v.reason}
                                 for k, v in snapshot.predicates.items()},
                  "candidate_count": len(snapshot.candidates)}, indent=2, sort_keys=True))
assert snapshot.authority is False
assert all(results[k] == "match" for k in ("selection", "current", "version_record", "fragment", "unit_target", "sample_stability")), "exact selected R0 binding not observed"
assert all(results[k] == "unknown" for k in ("manager_identity", "effective_action", "effective_principal", "loaded_disk_equivalence", "historical_containment", "current_descendant_completeness"))
if a.expect == "running":
    assert results["active_state"] == "mismatch" and results["main_pid"] == "mismatch", "running negative control not observed"
else:
    assert all(results[k] == "match" for k in ("active_state", "sub_state", "main_pid", "control_pid", "unit_job")), "stopped unit state not observed"
    assert all(results[k] != "present" for k in ("selected_processes", "cgroup_members", "known_children")), "current candidate present"
print("PROBE_ASSERTIONS_PASSED (non-authoritative checkpoint only)")
