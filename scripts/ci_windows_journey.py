"""Real Windows installation journey; no installed-package imports in the driver."""

from __future__ import annotations

import argparse
import functools
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "X5-windows-journey"
HEALTH_URL = "http://127.0.0.1:17340/api/runtime/v1/health"
TASK_OUTPUT_TAIL_BYTES = 65536
TASK_OUTPUT_METADATA_BYTES = 2048


def save(name, value):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    (EVIDENCE / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def run(argv, name, *, env=None, timeout=300, show=True):
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    print(f"Running {name}", flush=True)
    if ntpath.basename(str(argv[0])).casefold() in {"powershell.exe", "pwsh.exe"}:
        env = {key: value for key, value in (os.environ if env is None else env).items()
               if key.casefold() != "psmodulepath"}
    try:
        with (EVIDENCE / name).open("w", encoding="utf-8") as log:
            result = subprocess.run([str(arg) for arg in argv], env=env, stdout=log,
                                    stderr=subprocess.STDOUT, timeout=timeout, check=False)
    finally:
        if show:
            print((EVIDENCE / name).read_text(encoding="utf-8", errors="replace"), flush=True)
    if result.returncode:
        raise RuntimeError(f"{name}: exit {result.returncode}")
    return (EVIDENCE / name).read_text(encoding="utf-8-sig", errors="replace")


def powershell(code, name, *, show=True, timeout=300):
    return run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "$ErrorActionPreference='Stop'; "
                "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); " + code], name,
               show=show, timeout=timeout)


def source(repo):
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"Not a full source commit: {repo}")
    return {"sha": sha, "repo": str(repo.resolve())}


def install_manifest(agent, runtime, app):
    manifest = json.loads((app / "public" / "install-manifest.json").read_text(encoding="utf-8"))
    # Replace the entire source records, including any released schema digest.
    manifest.update(agent=source(agent), runtime=source(runtime))
    return manifest


class ManifestHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, document, **kwargs):
        self.document = document
        super().__init__(*args, **kwargs)

    def do_GET(self):
        if self.path != "/install-manifest.json":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.document)))
        self.end_headers()
        self.wfile.write(self.document)


def install(shell):
    runtime, app = ROOT / "journey-runtime", ROOT / "journey-app"
    manifest = install_manifest(ROOT, runtime, app)
    save("install-manifest.json", manifest)
    save("sources.json", {"agent": manifest["agent"], "runtime": manifest["runtime"], "app": source(app)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        ManifestHandler, document=json.dumps(manifest).encode("utf-8")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ, ASTRID_INSTALL_MANIFEST_URL=
               f"http://127.0.0.1:{server.server_port}/install-manifest.json")
    # Python's standard browser override suppresses the installer's website open.
    env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
    try:
        run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", app / "public" / "install.ps1"], "bootstrap.log", env=env, timeout=3600)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    installed = version()
    actual = json.loads((Path(installed["root"]) / "manifest.json").read_text(encoding="utf-8"))
    for name in ("agent", "runtime"):
        if actual[name] != manifest[name]:
            raise RuntimeError(f"Installer did not use the selected {name} source")


def program_root():
    return Path(os.environ["LOCALAPPDATA"]) / "Astrid"


def data_root():
    return Path(os.environ["LOCALAPPDATA"]) / "Astrid Runtime"


def version():
    root = program_root()
    current = (root / "current").read_text(encoding="utf-8").strip()
    return json.loads((root / "versions" / current / "version.json").read_text(encoding="utf-8"))


def assert_installed_profile_binding():
    """Check the installed profile and service both select this Python's product tree."""
    installed = version()
    profile = json.loads(Path(installed["profile"]).read_text(encoding="utf-8"))
    source_root = str(profile.get("source_checkout") or "")
    declaration = json.loads(Path(profile["service_declaration"]).read_text(encoding="utf-8"))
    host = next((row for row in declaration.get("services", [])
                 if isinstance(row, dict) and row.get("name") == "pack-host"), {})
    argv = host.get("argv", [])
    try:
        source_index = argv.index("--source-checkout")
        service_root = str(argv[source_index + 1])
    except (ValueError, IndexError):
        service_root = ""
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(installed["env"])
    probe = (
        "import json,sys,sysconfig; from pathlib import Path; "
        "root=Path(sys.argv[1]).resolve(); purelib=Path(sysconfig.get_path('purelib')).resolve(); "
        "print(json.dumps({'source_root_matches_selected_purelib':root==purelib, "
        "'host_module_present':(root/'astrid/core/execution/generic_host.py').is_file(), "
        "'purelib_host_module_present':(purelib/'astrid/core/execution/generic_host.py').is_file()}))"
    )
    result = json.loads(run([installed["python"], "-I", "-c", probe, source_root],
                            "installed-profile-binding.json", env=env))
    service_root_matches = (ntpath.normcase(ntpath.normpath(service_root)) ==
                            ntpath.normcase(ntpath.normpath(source_root)))
    binding = {
        **result,
        "service_root_matches_profile": service_root_matches,
    }
    save("installed-profile-binding-summary.json", binding)
    if not all(binding.values()):
        raise RuntimeError("Installed profile does not bind the selected Python product root")


def cli(module, argv, name, *, timeout=300):
    installed = version()
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(installed["env"])
    return run([installed["python"], "-I", "-m", module, *argv], name, env=env, timeout=timeout)


def health():
    request = urllib.request.Request(HEALTH_URL, headers={"Origin": "https://astrid.haus"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30) as response:
            value = json.load(response)
    except (OSError, ValueError) as exc:
        save("health-error.json", {"error": str(exc)})
        return None
    save("health.json", value)
    return value


def wait_for(label, probe, *, seconds=900):
    deadline = time.monotonic() + seconds
    while True:
        if probe():
            print(label, flush=True)
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Timed out after {seconds}s: {label}; see journey evidence")
        print(f"Waiting: {label}", flush=True)
        time.sleep(5)


def healthy():
    value = health()
    if not value or value.get("status") != "ok":
        return False
    try:
        discovery = json.loads((data_root() / "runtime" / "discovery.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(value.get("runtime_instance_id") and
                value["runtime_instance_id"] == discovery.get("runtime_instance_id"))


def host_ready():
    value = health()
    if not value or value.get("status") != "ok":
        return False
    service = next((row for row in value.get("services", []) if row.get("name") == "pack-host"), {})
    ready_path = data_root() / "runtime" / "generic-host.ready.json"
    if service.get("state") != "running" or not ready_path.is_file():
        return False
    try:
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    save("pack-host-ready.json", ready)
    return (ready.get("status") == "ready" and bool(value.get("runtime_instance_id")) and
            ready.get("runtime_instance_id") == value["runtime_instance_id"])


def task():
    project = "windows-journey"
    # Exercise the installed SDK boundary against the profile written by the
    # real installer. This local SFX action has no external model/GPU needs.
    os.environ["ASTRID_HOST_EXECUTION_PROFILE"] = "installed"
    assert_installed_profile_binding()
    created = json.loads(cli("astrid", ["projects", "create", project, "--name", "Windows journey", "--json"],
                             "project.json"))
    if created.get("ok") is not True:
        raise RuntimeError("Project creation failed")
    request = {
        "capability_id": "chiptune.sfx",
        "kind": "executor",
        "project": project,
        "inputs": {"kind": "blip", "variant": 1, "pitch": 0, "duration_s": 0.1},
    }
    invocation = json.loads(cli("astrid.omp_invoke", [json.dumps(request)], "sdk-invocation.json"))
    task_refs = invocation.get("details", {}).get("astridTaskRefs", [])
    if invocation.get("content", [{}])[0].get("type") != "text" or len(task_refs) != 1:
        raise RuntimeError("Installed-profile SDK invocation did not return one admitted task")
    task_id = task_refs[0].get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("Installed-profile SDK invocation returned no task identity")
    try:
        cli("astrid", ["tasks", "follow", task_id, "--project", project, "--poll-seconds", "2",
                       "--timeout-seconds", "900", "--json"], "task-follow.jsonl", timeout=960)
    except (RuntimeError, subprocess.TimeoutExpired) as follow_error:
        process_rows = best_effort("processes-after-task-follow.json", lambda: snapshot(
            "processes-after-task-follow.json"))
        best_effort("task-python-stacks-before-cancel", lambda: capture_task_python_stacks(process_rows))
        if process_rows is not None:
            best_effort("task-sfx-output-evidence.json", lambda: capture_sfx_output(task_id, process_rows))
        best_effort("task-logs-before-cancel", lambda: capture_task_logs(task_id, process_rows))
        try:
            cli("astrid", ["tasks", "events", task_id, "--project", project, "--json"],
                "task-events-before-cancel.json", timeout=30)
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        observed = json.loads(cli(
            "astrid", ["tasks", "show", task_id, "--project", project, "--json"],
            "task-after-follow-failure.json",
        ))
        row = observed.get("data") or {}
        if row.get("state") not in {"succeeded", "failed", "cancelled"}:
            cli("astrid", ["tasks", "cancel", task_id, "--project", project, "--json"],
                "task-cancel.json")
            try:
                cli("astrid", ["tasks", "follow", task_id, "--project", project, "--poll-seconds", "2",
                               "--timeout-seconds", "300", "--json"], "task-cancel-follow.jsonl", timeout=360)
            except (RuntimeError, subprocess.TimeoutExpired):
                # Follow returns nonzero for cancelled tasks; the public task state is authoritative.
                pass
        settled_after_failure = json.loads(cli(
            "astrid", ["tasks", "show", task_id, "--project", project, "--json"],
            "task-after-cancel.json",
        ))
        row = settled_after_failure.get("data") or {}
        if row.get("state") not in {"succeeded", "failed", "cancelled"}:
            raise RuntimeError(
                f"Task remained {row.get('state')!r} after normal cancellation; Runtime down will be blocked"
            ) from follow_error
        try:
            cli("astrid", ["tasks", "events", task_id, "--project", project, "--json"],
                "task-events-after-failure.json")
        except RuntimeError:
            pass
        raise RuntimeError(
            f"Task follow failed ({follow_error}); task reached terminal state {row.get('state')!r}"
        ) from follow_error
    settled = json.loads(cli("astrid", ["tasks", "show", task_id, "--project", project, "--json"], "task-settled.json"))
    row = settled.get("data") or {}
    outputs = {item.get("name"): item for item in (row.get("result") or {}).get("outputs", [])}
    if (settled.get("ok") is not True or row.get("state") != "succeeded" or
            not {"sfx", "sfx_meta"} <= outputs.keys() or any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", outputs[name].get("digest", "")) or
                outputs[name].get("size", 0) <= 0 for name in ("sfx", "sfx_meta"))):
        raise RuntimeError("Task did not settle successfully with outputs")


def process_key(row):
    return (row["pid"], row["created"])


def capture_task_python_stacks(process_rows):
    """Best-effort stacks for Python processes in the task's captured process tree."""
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if process_rows is None:
        (EVIDENCE / "task-timeout-pystack-process-tree.txt").write_text(
            "py-spy dump not run: process tree snapshot unavailable\n", encoding="utf-8",
        )
        return

    for row in process_rows:
        executable = row.get("exe") or ""
        command = row.get("command") or ""
        if not any(re.search(
                r"(?i)(?:^|[\\/\s\"'])python(?:\d+(?:\.\d+)?)?w?\.exe(?=$|[\s\"'])", value,
        ) for value in (executable, command)):
            continue
        pid = row.get("pid")
        if type(pid) is not int or pid <= 0:
            (EVIDENCE / "task-timeout-pystack-missing-pid.txt").write_text(
                f"py-spy dump not run: Python process has invalid pid {pid!r}\n", encoding="utf-8",
            )
            continue
        evidence = EVIDENCE / f"task-timeout-pystack-{pid}.txt"
        try:
            result = subprocess.run(
                ["py-spy", "dump", "--pid", str(pid)], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30, check=False,
            )
            content = result.stdout
            if result.stderr:
                content += ("\n" if content and not content.endswith("\n") else "") + result.stderr
            if result.returncode:
                content = f"py-spy dump failed with exit {result.returncode}\n{content}"
        except Exception as exc:
            content = f"py-spy dump failed: {type(exc).__name__}: {exc}\n"
        try:
            evidence.write_text(content or "py-spy returned no stack output\n", encoding="utf-8")
        except OSError:
            # Stack capture is diagnostic only; it must never delay cancellation.
            pass


def best_effort(label, action):
    try:
        return action()
    except Exception as exc:
        save(f"{label.removesuffix('.json')}-error.json", {"error": str(exc)})
        return None


def task_attempt_root(task_id, process_rows):
    if process_rows is None:
        return None
    expected_prefix = ntpath.normcase(f"astrid-attempt-{task_id}-")
    for process in process_rows:
        command = process.get("command") or ""
        if "astrid.packs.chiptune.actions.sfx.run" not in command:
            continue
        match = re.search(r'--out\s+(?:"([^"]+)"|(\S+))', command)
        if not match:
            continue
        candidate = match.group(1) or match.group(2)
        normalized = ntpath.normcase(candidate.replace("/", "\\"))
        if expected_prefix in normalized and normalized.endswith("\\outputs"):
            return Path(candidate).parent
    return None


def _copy_tail(source, evidence_name, limit, *, details=False):
    if source.is_symlink() or not source.is_file():
        return None
    try:
        with source.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(max(0, size - limit))
            content = stream.read(limit)
    except OSError:
        return None
    (EVIDENCE / evidence_name).write_bytes(content)
    row = {"source": source.name, "evidence": evidence_name}
    if details:
        row.update(bytes=len(content), truncated=size > limit)
    return row


def _task_output_metadata(attempt):
    source = attempt / "task-output-capture.json" if attempt is not None else None
    missing = {"available": False, "status": "missing", "source": "task-output-capture.json"}
    if source is None or source.is_symlink() or not source.is_file():
        return missing
    try:
        with source.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > TASK_OUTPUT_METADATA_BYTES:
                return {**missing, "status": "oversized", "bytes": size}
            content = stream.read(TASK_OUTPUT_METADATA_BYTES + 1)
    except OSError:
        return {**missing, "status": "unreadable"}
    if len(content) > TASK_OUTPUT_METADATA_BYTES:
        return {**missing, "status": "oversized", "bytes": len(content)}
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {**missing, "status": "malformed", "bytes": len(content)}

    if (not isinstance(value, dict) or set(value) != {"schema_version", "streams"} or
            type(value.get("schema_version")) is not int or value["schema_version"] != 1 or
            not isinstance(value.get("streams"), dict) or set(value["streams"]) != {"stdout", "stderr"}):
        return {**missing, "status": "malformed", "bytes": len(content)}
    for name, filename in (("stdout", "task-stdout-tail.log"), ("stderr", "task-stderr-tail.log")):
        row = value["streams"][name]
        if (not isinstance(row, dict) or set(row) != {"file", "available", "byte_count", "truncated", "error"} or
                row.get("file") != filename or type(row.get("available")) is not bool or
                type(row.get("byte_count")) is not int or
                not 0 <= row["byte_count"] <= TASK_OUTPUT_TAIL_BYTES or
                type(row.get("truncated")) is not bool or
                row.get("error") is not None and
                (not isinstance(row["error"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", row["error"]))):
            return {**missing, "status": "malformed", "bytes": len(content)}

    evidence_name = "task-timeout-task-output-capture.json"
    try:
        (EVIDENCE / evidence_name).write_bytes(content)
    except OSError:
        return {**missing, "status": "copy_failed", "bytes": len(content)}
    return {
        "available": True,
        "status": "valid",
        "source": source.name,
        "evidence": evidence_name,
        "bytes": len(content),
        "streams": value["streams"],
    }


def capture_task_logs(task_id=None, process_rows=None):
    logs = data_root() / "logs"
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    copied = []
    paths = [logs / "service-pack-host.log", logs / "service-worker.log", *logs.glob("worker*.log")]
    for path in paths[:21]:
        if not path.is_file():
            continue
        row = _copy_tail(path, f"task-timeout-{path.name}", 262144)
        if row is not None:
            copied.append(row)
    attempt = task_attempt_root(task_id, process_rows) if task_id else None
    child_logs = []
    missing_child_logs = []
    for name in ("task-stdout-tail.log", "task-stderr-tail.log"):
        row = None
        if attempt is not None:
            row = _copy_tail(
                attempt / name, f"task-timeout-{name}", TASK_OUTPUT_TAIL_BYTES, details=True,
            )
        if row is None:
            missing_child_logs.append(name)
        else:
            child_logs.append(row)
    child_metadata = _task_output_metadata(attempt)
    metadata_valid = child_metadata["status"] == "valid"
    reasons = []
    if missing_child_logs:
        reasons.append("One or more per-attempt task stream tails were unavailable before cancellation.")
    if not metadata_valid:
        reasons.append(f"Task output capture metadata is {child_metadata['status']}.")
    save("task-logs-before-cancel.json", {
        "logs": copied,
        "missing": [name for name in ("service-pack-host.log", "service-worker.log")
                    if name not in {entry["source"] for entry in copied}],
        "task_child_output": {
            "available": bool(child_logs),
            "streams": child_logs,
            "missing": missing_child_logs,
            "metadata": child_metadata,
            "reason": "; ".join(reasons) or None,
        },
    })


def capture_sfx_output(task_id, process_rows):
    """Record only the known journey SFX attempt's small metadata files."""
    attempt = task_attempt_root(task_id, process_rows)
    output_root = attempt / "outputs" if attempt is not None else None

    evidence = {"found": output_root is not None, "files": {}}
    if output_root is not None:
        for name in ("manifest.json", "sfx.json", "sfx.wav"):
            path = output_root / name
            try:
                size = path.stat().st_size
            except OSError:
                evidence["files"][name] = {"exists": False}
                continue
            entry = {"exists": True, "size": size}
            if name.endswith(".json") and size <= 65536:
                try:
                    entry["content"] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    entry["content"] = "unreadable or invalid JSON"
            evidence["files"][name] = entry
    save("task-sfx-output-evidence.json", evidence)


def astrid_processes(rows, roots, tracked=()):
    roots = [ntpath.normcase(str(root)).rstrip("\\/") + "\\" for root in roots]
    selected = {process_key(row) for row in rows if process_key(row) in tracked or
                any(root in ntpath.normcase(row.get("exe") or "") or
                    root in ntpath.normcase(row.get("command") or "") for root in roots) or
                re.search(r"\b(?:astrid_runtime|runtime_protocol)\b|(?:^|\s)-m\s+astrid\b|"
                          r"\bastrid\.core\.execution\b|astrid-install-", row.get("command") or "")}
    by_pid = {row["pid"]: row for row in rows}
    while True:
        added = set()
        for row in rows:
            parent = by_pid.get(row["ppid"])
            if parent and parent["created"] <= row["created"] and process_key(parent) in selected:
                added.add(process_key(row))
        if added <= selected:
            return [row for row in rows if process_key(row) in selected]
        selected.update(added)


def snapshot(name):
    # There is no public enumeration of arbitrary Job handles. Track process
    # incarnations and descendants before down, including generic Job children.
    value = json.loads(powershell("""
$rows = @(Get-CimInstance Win32_Process | ForEach-Object {
    [pscustomobject]@{pid=[int]$_.ProcessId; ppid=[int]$_.ParentProcessId;
        created=$(if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { '' });
        exe=[string]$_.ExecutablePath; command=[string]$_.CommandLine;
        cpu_seconds=[math]::Round((([double]$_.UserModeTime + [double]$_.KernelModeTime) / 10000000), 3);
        thread_count=[int]$_.ThreadCount; handle_count=[int]$_.HandleCount}
})
ConvertTo-Json -InputObject @{collector_pid=$PID; processes=$rows} -Depth 4
""", name, show=False, timeout=30))
    rows = [row for row in value["processes"] if row["pid"] not in {os.getpid(), value["collector_pid"]}]
    tracked_file = EVIDENCE / "tracked-processes.json"
    tracked = json.loads(tracked_file.read_text(encoding="utf-8")) if tracked_file.exists() else []
    selected = astrid_processes(rows, [program_root(), data_root(), program_root().parent / "Astrid Installer"],
                               {process_key(row) for row in tracked})
    selected = [dict(row) for row in selected]
    for row in rows:
        for key in ("cpu_seconds", "thread_count", "handle_count"):
            row.pop(key, None)
    save("tracked-processes.json", list({process_key(row): row for row in tracked + selected}.values()))
    save(name, {"processes": rows, "astrid_processes": selected})
    return selected


def down():
    snapshot("processes-before-down.json")
    powershell("Get-ScheduledTask | Where-Object { $_.TaskName -eq 'Astrid Runtime' -and $_.TaskPath -eq '\\' } "
               "| Disable-ScheduledTask | Out-Null", "disable-scheduled-task.log")
    if (program_root() / "current").is_file():
        installed = version()
        cli("astrid_runtime", ["down", "--profile", "astrid", "--data-root", str(data_root()),
                              "--source-manifest", installed["profile"], "--json"], "down.json", timeout=900)
    else:
        print("No installed current version; checking for partial-install leftovers.")


def collect():
    logs = data_root() / "logs"
    errors = []
    for path in logs.glob("*.log"):
        try:
            shutil.copyfile(path, EVIDENCE / path.name)
        except OSError as exc:
            errors.append(f"{path.name}: {exc}")
    value = health()
    save("health-final.json", value or {"error": "Runtime health unavailable; see health-error.json"})
    if not (EVIDENCE / "health.json").exists():
        save("health.json", {"error": "Runtime never reported health; see health-error.json"})
    try:
        snapshot("processes-final.json")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        errors.append(f"Process snapshot: {exc}")
    save("collection.json", {"errors": errors, "logs_found": sorted(path.name for path in logs.glob("*.log"))})
    if errors:
        raise RuntimeError("Evidence collection was incomplete; see collection.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("install", "start", "health", "host", "task", "down", "stopped", "collect"))
    parser.add_argument("--installer-shell", choices=("powershell.exe", "pwsh.exe"))
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This journey must run on Windows")
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if args.step == "install":
        if not args.installer_shell:
            parser.error("install requires --installer-shell")
        install(args.installer_shell)
    elif args.step == "start":
        powershell("Start-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'", "start.log")
    elif args.step == "health":
        wait_for("Runtime health is ok and belongs to this installation", healthy)
    elif args.step == "host":
        wait_for("pack-host is running with its current ready file", host_ready)
        snapshot("processes-host-ready.json")
    elif args.step == "task":
        task()
    elif args.step == "down":
        down()
    elif args.step == "stopped":
        wait_for("No Astrid/Runtime processes remain", lambda: not snapshot("processes-after-down.json"), seconds=300)
    else:
        collect()


if __name__ == "__main__":
    main()
