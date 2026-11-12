"""The pod's embedded log finalizer works without an installed Astrid package."""

import hashlib
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from astrid.core._shared.run_logs import LogDecl, declare
from astrid.packs.runpod import machine


def _run(tmp_path, body):
    job = tmp_path / "job"
    job.mkdir()
    (job / "stdout").write_text("job output\n")
    (job / "stderr").write_text("job error\n")
    script = job / "command.sh"
    script.write_text("#!/bin/bash\n" + body + "\n")
    done = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
    return job, done


def test_finish_collects_declared_slices_and_prints_log_manifest_digest(tmp_path):
    remote = str(tmp_path / "run")
    out = Path(remote) / "out"
    out.mkdir(parents=True)
    engine = tmp_path / "engine.log"
    engine.write_bytes(b"previous run\nthis run\n")
    declare(out, LogDecl("engine", "engine", str(engine), start=len(b"previous run\n")))
    job, done = _run(tmp_path, machine.finish_command(remote, python=sys.executable))

    assert done.returncode == 0
    index = json.loads((out / "logs/index.json").read_text())
    assert {row["name"] for row in index["logs"]} == {"engine", "job.stdout", "job.stderr", "generation.stdout"}
    engine_row = next(row for row in index["logs"] if row["name"] == "engine")
    assert (out / "logs" / engine_row["file"]).read_bytes() == b"this run\n"
    digest = hashlib.sha256((out / "logs/wrapper-manifest.json").read_bytes()).hexdigest()
    marks = [line for line in done.stdout.splitlines() if line.startswith("LOGS_MANIFEST_SHA256=")]
    assert marks == [f"LOGS_MANIFEST_SHA256={digest}"]
    assert (out / "logs/job.stdout.log").read_text() == "job output\n"
    assert (out / "logs/job.stderr.log").read_text() == "job error\n"


def test_finish_creates_output_and_collects_job_files_when_out_is_absent(tmp_path):
    remote = str(tmp_path / "run")
    job, done = _run(tmp_path, machine.finish_command(remote, python=sys.executable))
    assert done.returncode == 0
    out = Path(remote) / "out"
    rows = json.loads((out / "logs/index.json").read_text())["logs"]
    assert {row["name"] for row in rows} >= {"job.stdout", "job.stderr"}
    assert (out / "logs/job.stdout.log").read_text() == "job output\n"
    assert (out / "logs/job.stderr.log").read_text() == "job error\n"


def test_generation_stdout_survives_the_failure_log_fetch(tmp_path):
    remote = tmp_path / "run"
    out = remote / "out"
    out.mkdir(parents=True)
    (remote / "result.json").write_text('{"ok":false}\n')
    job, done = _run(tmp_path, machine.finish_command(str(remote), python=sys.executable))
    assert done.returncode == 0

    class LocalOps:
        @staticmethod
        def get(source, destination):
            shutil.copyfile(source, destination)

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    owner = object.__new__(machine.RunPodMachine)
    run = SimpleNamespace(remote_root=str(remote), scratch=scratch)
    collected = owner._fetch_failure_logs(LocalOps(), run, done.stdout, tmp_path / "receipt")
    row = next(row for row in collected["files"] if row["name"] == "generation.stdout")
    assert Path(row["path"]).read_text() == '{"ok":false}\n'


def test_generation_script_finishes_logs_once_on_early_and_generation_failures():
    command = machine.generation_command("entry", "/remote/run", mode="t2i")
    assert command.count("finish_logs() {") == 1
    assert "find " not in command and "-mmin" not in command
    assert "/failure" not in command and "FAILURE_MANIFEST" not in command
    failure = command.index("if [ $rc -ne 0 ]")
    assert command.index("finish_logs\n", command.index("rc=$?")) < failure
    assert command.count("|| { finish_logs; exit 3; }") == 3
    frame_writer = machine.Path(machine.__file__).with_name("failure_diagnostics.py").read_text(encoding="utf-8")
    assert command.index("finish_logs\n", command.index("rc=$?")) < command.index(shlex.quote(frame_writer))
