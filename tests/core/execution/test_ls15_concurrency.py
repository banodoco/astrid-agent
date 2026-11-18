"""LS-15 explicit contexts overlap without borrowing another job's state."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from tests.core.execution import test_resident_sessions

install_action = test_resident_sessions.install_action
run_job = test_resident_sessions.run_job
resident_host = test_resident_sessions.resident_host


def test_two_resident_dispatches_overlap_with_separate_contexts(resident_host, tmp_path):
    root, host, runtime = resident_host
    manifest = json.loads((root / "pack.yaml").read_text())
    manifest["actions"]["run"]["metadata"]["resident_job_context"] = {
        "protocol": "astrid.job-context.v1", "max_jobs": 2,
    }
    (root / "pack.yaml").write_text(json.dumps(manifest))
    rendezvous = tmp_path / "rendezvous"
    rendezvous.mkdir()
    install_action(resident_host, f'''import json
import os
import time
from pathlib import Path
from astrid.sdk import current_pack_job

def run(value='default'):
    context = current_pack_job()
    entered = Path({str(rendezvous)!r})
    (entered / context.task_id).write_text(value)
    deadline = time.monotonic() + 5
    while len(list(entered.iterdir())) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(list(entered.iterdir())) == 2, 'jobs did not overlap'
    path = context.output_root / 'private.txt'
    path.write_text(value)
    print('private-log-' + value)
    return {{'pid': os.getpid(), 'task': context.task_id, 'attempt': context.attempt_id,
             'value': value, 'output': str(path), 'cwd': os.getcwd(),
             'environment': dict(context.environment)}}
''')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_job, host, runtime, index, value=value, keep_attempt=True)
                   for index, value in ((1, "left"), (2, "right"))]
        results = [future.result(timeout=30) for future in futures]
    left, right = [result["action_result"] for result in results]
    assert left["pid"] == right["pid"]
    assert left["pid"] != __import__("os").getpid()
    assert (left["task"], right["task"]) == ("resident-task-1", "resident-task-2")
    assert (left["attempt"], right["attempt"]) == ("attempt-1", "attempt-2")
    assert (left["value"], right["value"]) == ("left", "right")
    assert left["output"] != right["output"]
    assert left["cwd"] == right["cwd"]
    for index, result in enumerate((left, right), 1):
        from pathlib import Path
        path = Path(result["output"])
        assert path.read_text() == result["value"]
        assert f"resident-task-{index}-attempt-{index}" in str(path)
        assert f"resident-task-{index}-attempt-{index}" in result["environment"]["ASTRID_PROGRESS_PATH"]
        assert not any(name.startswith("ASTRID_RUNTIME_CREDENTIAL") for name in result["environment"])
    assert left["environment"]["ASTRID_PROGRESS_PATH"] != right["environment"]["ASTRID_PROGRESS_PATH"]
    assert host.managed_tool_session.active
