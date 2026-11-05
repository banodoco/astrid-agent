"""Test-only Windows Pinokio row, using the frozen launcher's lifecycle.

Implements the shared driver run(config) contract. The runner does not signal
processes or clean up an installation after a failure.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Any


class RowError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RowError(f"{path}: expected a JSON object")
    return value


def run_command(argv: list[str], *, env: dict[str, str], cwd: Path, timeout: float = 900) -> str:
    # subprocess.run(timeout=...) terminates the child; do not use it here.
    with tempfile.TemporaryFile(mode="w+b") as output:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                raise RowError(f"command timed out; process {proc.pid} left untouched: {argv[0]}")
            time.sleep(0.2)
        output.seek(0)
        text = output.read().decode("utf-8", errors="replace")
        if proc.returncode:
            raise RowError(f"{argv[0]} exited {proc.returncode}: {text[-8192:]}")
        return text


def launcher_env(pinokio: Path, app: Path) -> dict[str, str]:
    script = ("const p=require(process.argv[1]);"
              "process.stdout.write(JSON.stringify(p.buildEnvironment({appDirectory:process.argv[2]})))")
    value = run_command(["node", "-e", script, str(pinokio / "pinokio" / "environment.js"), str(app)],
                        env=dict(os.environ), cwd=pinokio, timeout=30)
    return json.loads(value)


def runtime_processes(env: dict[str, str], cwd: Path) -> dict[int, tuple[str, str]]:
    script = ("$ErrorActionPreference='Stop'; "
              "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
              "@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine,CreationDate) | ConvertTo-Json -Compress")
    result = run_command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], env=env, cwd=cwd, timeout=30)
    rows = json.loads(result)
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        raise RowError("could not enumerate Runtime processes")
    found = {}
    for row in rows:
        cmd = row.get("CommandLine")
        if cmd is None and str(row.get("Name", "")).lower() in {"python.exe", "pythonw.exe"}:
            raise RowError("Runtime process inspection refused: a Python command line is inaccessible")
        cmd = str(cmd or "")
        if "-m runtime_protocol start" in cmd or ("from astrid_runtime.cli import main" in cmd and re.search(r"main\(\s*\[\s*['\"]run['\"]", cmd)):
            birth = row.get("CreationDate")
            if not birth:
                raise RowError("Runtime process inspection refused: missing creation time")
            found[int(row["ProcessId"])] = (str(birth), cmd)
    return found


class WindowsRow:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.root = Path(config["install_root"])
        self.pinokio = Path(config["pinokio_root"])
        self.agent = Path(config["agent_root"])
        self.manifest = Path(config["manifest"])
        self.standin = Path(config["standin_manifest"])
        self.app = self.root.parent.parent
        self.env: dict[str, str] = {}
        self.child: subprocess.Popen | None = None
        self.data: Path | None = None
        self.identity: dict | None = None
        self.instance: str | None = None
        self.marker: Path | None = None
        self.session_log = None
        self.marker_contents = "pinokio-windows-data-marker\n"

    def install(self) -> None:
        if sys.platform != "win32":
            raise RowError("Windows row requires Windows")
        if self.config.get("row") != "pinokio-windows":
            raise RowError("row must be pinokio-windows")
        if any(not p.is_absolute() for p in (self.root, self.pinokio, self.agent, self.manifest, self.standin)):
            raise RowError("all input paths must be absolute")
        if self.root.name.lower() != "astrid" or self.root.parent.name.lower() != "app" or self.app.parent.name.lower() != "api":
            raise RowError("install_root must be <Pinokio home>/api/<app>/app/astrid")
        if self.root.exists():
            raise RowError("install_root already exists; refusing to overwrite an installation")
        command = self.config.get("task_command")
        if not isinstance(command, list) or not command or any(not isinstance(x, str) or not x for x in command):
            raise RowError("task_command must be a nonempty argv array")
        if read_json(self.manifest).get("release") != 1 or read_json(self.standin).get("release") != 2:
            raise RowError("candidate and stand-in must be releases 1 and 2")
        if self.app.resolve() == self.pinokio.resolve():
            raise RowError("install root would write into the immutable Pinokio candidate")
        self.app.mkdir(parents=True, exist_ok=True)
        self.env = launcher_env(self.pinokio, self.app)
        local = self.env.get("LOCALAPPDATA")
        if not local:
            raise RowError("LOCALAPPDATA is required to check the installer selection")
        if (Path(local) / "astrid-install.json").exists():
            raise RowError("existing Astrid installer selection; use an isolated test profile")
        self.installer(self.manifest)
        record = read_json(self.root / "install.json")
        if record.get("owner") != "haus.astrid.runtime" or record.get("managed_by") != "pinokio":
            raise RowError("installer did not select Pinokio ownership")
        self.data = Path(record["data_root"])
        if not self.data.is_absolute():
            raise RowError("installer returned a non-absolute data root")

    def installer(self, manifest: Path | None, *, uninstall: bool = False) -> None:
        script = self.manifest.parent / "install.ps1"
        if not script.is_file():
            script = self.agent / "repos" / "astrid-app" / "public" / "install.ps1"
        if not script.is_file():
            raise RowError("cannot find candidate App public/install.ps1 beside manifest or under agent_root/repos/astrid-app")
        args = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "--root", str(self.root), "--managed-by", "pinokio"]
        if uninstall:
            args.append("--uninstall")
        if manifest is not None:
            args.extend(["--manifest", str(manifest)])
        run_command(args, env=self.env, cwd=self.app, timeout=3600)

    def version(self) -> dict:
        current = (self.root / "current").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", current):
            raise RowError("invalid installed version pointer")
        folder = self.root / "versions" / current
        record = read_json(folder / "version.json")
        node = Path(record["env"]["ASTRID_NODE_EXECUTABLE"])
        if not node.is_absolute() or not node.resolve().is_relative_to(folder.resolve()):
            raise RowError("pinned Node is outside the selected version")
        return record

    def start(self) -> None:
        record = self.version()
        node = record["env"]["ASTRID_NODE_EXECUTABLE"]
        self.session_log = tempfile.TemporaryFile(mode="w+b")
        self.child = subprocess.Popen([node, str(self.pinokio / "pinokio" / "session.mjs"), "serve", str(self.root), str(self.app / "app" / "editor"), "backend"], cwd=str(self.app), env=self.env, stdout=self.session_log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 900
        last = "no health response"
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                self.session_log.seek(0)
                detail = self.session_log.read().decode("utf-8", errors="replace")[-8192:]
                self.session_log.close()
                raise RowError(f"Pinokio session exited {self.child.returncode} before readiness: {detail}")
            try:
                self.health()
                control = read_json(self.root / "pinokio-control.json")
                if control.get("pid") != self.child.pid or not control.get("token") or not control.get("port"):
                    raise RowError("controller does not identify the session child")
                return
            except (OSError, ValueError, KeyError, RowError) as exc:
                last = str(exc)
                time.sleep(0.5)
        raise RowError(f"session readiness timed out: {last}")

    def health(self) -> dict:
        assert self.data is not None
        request = urllib.request.Request("http://127.0.0.1:17340/api/runtime/v1/health", headers={"Origin": "https://astrid.haus"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            health = json.load(response)
        discovery = read_json(self.data / "runtime" / "discovery.json")
        record = self.version()
        instance = discovery.get("runtime_instance_id")
        if health.get("status") != "ok" or not instance or health.get("runtime_instance_id") != instance or health.get("identity") != record.get("identity"):
            raise RowError("health, discovery and installed identity do not agree")
        self.instance = instance
        self.identity = health["identity"]
        return discovery

    def task(self) -> None:
        run_command(self.config["task_command"], env=self.env, cwd=self.agent)

    def start_health_task(self) -> None:
        self.start()
        if self.identity.get("release") != 1:
            raise RowError("initial Runtime is not release 1")
        self.task()
        assert self.data is not None
        self.marker = self.data / "pinokio-row-data-marker.txt"
        with self.marker.open("x", encoding="utf-8") as stream:
            stream.write(self.marker_contents)

    def check_marker(self) -> None:
        if self.marker is None or self.marker.read_text(encoding="utf-8") != self.marker_contents:
            raise RowError("data marker changed or disappeared")

    def stop(self) -> None:
        expected_instance, expected_identity = self.instance, self.identity
        before = self.health()
        if not expected_instance or self.instance != expected_instance or self.identity != expected_identity:
            raise RowError("Runtime identity changed before Stop")
        record = read_json(self.root / "install.json")
        if record.get("managed_by") != "pinokio" or Path(record["data_root"]).resolve() != self.data.resolve():
            raise RowError("install or data root changed before Stop")
        if self.child is None or self.child.poll() is not None:
            raise RowError("owned session is not running")
        control = read_json(self.root / "pinokio-control.json")
        if control.get("pid") != self.child.pid:
            raise RowError("controller PID does not match owned session")
        selected = int(before["pid"])
        processes = runtime_processes(self.env, self.app)
        if selected not in processes:
            raise RowError("selected Runtime is absent from process snapshot")
        others = {pid: value for pid, value in processes.items() if pid != selected}
        script = ("const l=await import(process.argv[1]);const o={appDirectory:process.argv[3]};"
                  "await l.preflightStop(process.argv[2],o);"
                  "const r=await l.requestGracefulStop(process.argv[2],o);"
                  "if(r.absent)throw Error('controller absent');await l.waitForInstallerLock(process.argv[2],o)")
        node = self.version()["env"]["ASTRID_NODE_EXECUTABLE"]
        # Import an absolute file URL so Windows drive letters are accepted by ESM.
        try:
            run_command([node, "--input-type=module", "-e", script, (self.pinokio / "pinokio" / "lifecycle.mjs").as_uri(), str(self.root), str(self.app)], env=self.env, cwd=self.app, timeout=90)
        except Exception as exc:
            # A refused or timed out stop grants no permission to retry or clean up.
            # Capture preservation checks without hiding the original failure.
            try:
                after = runtime_processes(self.env, self.app)
                self.health()
                record_after = read_json(self.root / "install.json")
                preserved = (after == processes and self.instance == expected_instance
                             and self.identity == expected_identity and record_after == record)
                detail = "preserved" if preserved else "changed or could not be matched"
            except Exception as check:
                detail = f"could not verify preservation: {type(check).__name__}: {check}"
            raise RowError(f"graceful Stop failed: {exc}; Runtime/root check: {detail}") from exc
        deadline = time.monotonic() + 30
        while self.child.poll() is None and time.monotonic() < deadline:
            time.sleep(.2)
        if self.child.poll() is None or self.child.returncode != 0:
            raise RowError("session did not exit cleanly after graceful stop")
        if self.session_log is not None:
            self.session_log.close()
            self.session_log = None
        after = runtime_processes(self.env, self.app)
        if selected in after or after != others:
            raise RowError("selected Runtime remained or another Runtime changed during Stop")
        record = read_json(self.root / "install.json")
        if (record.get("owner") != "haus.astrid.runtime" or record.get("managed_by") != "pinokio"
                or Path(record["data_root"]).resolve() != self.data.resolve()
                or self.version().get("identity") != expected_identity):
            raise RowError("install root, data root or installed identity changed during Stop")
        if (self.root / "pinokio-control.json").exists():
            raise RowError("controller remains after Stop")

    def stop_start(self) -> None:
        old = self.instance
        self.stop()
        self.start()
        if self.instance == old:
            raise RowError("restart did not create a new Runtime instance")
        self.check_marker()

    def preflight(self) -> dict:
        # Query the same read-only Runtime preflight used by stop.js. Preserve
        # its structured blockers so a merely running client is not called busy.
        script = ("const l=await import(process.argv[1]);"
                  "try {let r=await l.preflightStop(process.argv[2],{appDirectory:process.argv[3]});"
                  "console.log(JSON.stringify({safe:true,report:r}))}"
                  "catch(e){console.log(JSON.stringify({safe:false,message:e.message,details:e.details}))}")
        node = self.version()["env"]["ASTRID_NODE_EXECUTABLE"]
        out = run_command([node, "--input-type=module", "-e", script,
                           (self.pinokio / "pinokio" / "lifecycle.mjs").as_uri(),
                           str(self.root), str(self.app)], env=self.env, cwd=self.app, timeout=45)
        return json.loads(out)

    def busy_stop(self) -> None:
        if self.preflight().get("safe") is not True:
            raise RowError("Runtime was not idle before the busy task")
        # No timeout cleanup: if admission or stop fails, preserve the client
        # and Runtime as found for inspection.
        output = tempfile.TemporaryFile(mode="w+b")
        task = subprocess.Popen(self.config["task_command"], env=self.env, cwd=str(self.agent),
                                stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if task.poll() is not None:
                raise RowError(f"task command exited {task.returncode} before a running Runtime task was observed")
            report = self.preflight()
            details = report.get("details") or {}
            active = details.get("active_tasks") or []
            if report.get("safe") is False and any(x.get("task_id") and x.get("status") == "running" for x in active if isinstance(x, dict)):
                self.stop()  # A refusal is the concrete failed stage, never cleanup authority.
                raise RowError("Runtime stopped while a task was running; task settlement was not established")
            if report.get("safe") is False:
                raise RowError(f"preflight failed without a running task: {report}")
            time.sleep(.2)
        raise RowError("timed out waiting for a running Runtime task; client left untouched")

    def update(self) -> None:
        self.stop()
        self.installer(self.standin)
        self.start()
        if self.identity.get("release") != 2 or self.identity.get("managed_by") != "pinokio":
            raise RowError("updated Runtime identity is not Pinokio release 2")
        self.check_marker()

    def release2_health_task(self) -> None:
        self.health()
        self.task()

    def uninstall_cleanup(self) -> None:
        self.stop()
        self.installer(None, uninstall=True)
        if self.root.exists():
            raise RowError("uninstall did not remove the program root")
        self.check_marker()


STEPS = ("install", "start_health_task", "stop_start", "busy_stop", "update",
         "release2_health_task", "uninstall_cleanup")


def run(config):
    """Return ordered plain stage objects, stopping at the first exception."""
    result = []
    try:
        row = WindowsRow(config)
    except Exception as exc:
        return [{"name": "install", "status": "fail", "error": f"{type(exc).__name__}: {exc}"}]
    for name in STEPS:
        try:
            getattr(row, name)()
        except Exception as exc:
            result.append({"name": name, "status": "fail", "error": f"{type(exc).__name__}: {exc}"})
            break
        result.append({"name": name, "status": "pass", "error": None})
    return result
