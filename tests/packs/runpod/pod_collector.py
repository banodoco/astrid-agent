"""Run the shipped failure-bundle collector the way the pod does, against a fake pod's files.

The collector source and its request are decoded from the exact command the
controller sends. The fake pod's files are written under a scratch root, the
request's absolute pod paths are prefixed with that root, and the source runs
in a fresh isolated interpreter (``python -I -``), standard library only, with
the given environment. The folder it writes is copied back into the fake pod.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from astrid.packs.runpod import failure_bundle

COMMAND = re.compile(r'printf %s (\S+) \| base64 -d \| "\$P" - (\S+)$')
PATH_FIELDS = ("failures_root", "run_dir", "job_dir", "engine_root", "prepared")
LIST_FIELDS = ("roots", "secret_dirs", "secret_files")


def is_collector(command: str) -> bool:
    return command.startswith(f": {failure_bundle.COMMAND_MARK};")


def decode(command: str) -> tuple[bytes, dict]:
    found = COMMAND.search(command)
    assert found, command[:200]
    return base64.b64decode(found[1]), json.loads(base64.b64decode(found[2]))


def run(files: dict[str, bytes], command: str, scratch: Path, *, environ: dict[str, str] | None = None,
        log: list | None = None) -> tuple[int, str]:
    source, request = decode(command)
    assert source == Path(failure_bundle.__file__).read_bytes(), "the pod runs the shipped source unchanged"
    if log is not None:
        log.append(dict(request))
    root = Path(scratch) / "pod-disk"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    run_dir = request.get("run_dir")
    # Only what the collector may read is written out: the job's folders, the engine's log and
    # receipt, the prepared connection and credential folders, and the earlier bundle folders.
    prefixes = [value + "/" for value in (run_dir, request.get("job_dir"), request.get("failures_root"),
                                         *(request.get("secret_dirs") or [])) if value]
    engine = request.get("engine_root")
    if engine:
        prefixes += [engine + "/.astrid-engine/", engine + "/runtime/"]
    wanted = [request.get("prepared"), *(request.get("secret_files") or [])]
    for path, data in files.items():
        if not path.startswith("/") or not (path in wanted or path.startswith(tuple(prefixes))):
            continue
        local = root / path.lstrip("/")
        if run_dir and path == f"{run_dir}/job.json":
            job = json.loads(data)
            source_path = (job.get("execution") or {}).get("source")
            if isinstance(source_path, str) and source_path.startswith("/"):
                job["execution"]["source"] = str(root) + source_path  # the scratch root's own path
            data = json.dumps(job).encode()
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
    moved = dict(request)
    for key in PATH_FIELDS:
        if isinstance(moved.get(key), str):
            moved[key] = str(root) + moved[key]
    for key in LIST_FIELDS:
        moved[key] = [str(root) + value for value in moved.get(key) or []]
    blob = base64.b64encode(json.dumps(moved).encode()).decode()
    done = subprocess.run([sys.executable, "-I", "-", blob], input=source, capture_output=True,
                          env=dict(environ or {"PATH": "/usr/bin:/bin"}), timeout=120)
    folder_root = request["failures_root"]
    for path in [path for path in files if path.startswith(folder_root + "/")]:
        del files[path]
    written = root / folder_root.lstrip("/")
    if written.is_dir():
        for path in written.rglob("*"):
            if path.is_file():
                files[folder_root + "/" + path.relative_to(written).as_posix()] = path.read_bytes()
    stdout = done.stdout.decode()
    assert str(root) not in stdout
    return done.returncode, stdout
