"""Hosted Pinokio launcher row driver (Linux first; macOS and Windows reuse it).

The driver exercises the Pinokio launcher's own commands end to end: the
environment, the install action, ``session.mjs serve``, the exact lifecycle stop
functions, a stop that races a running task, an update to a stand-in of the next
release (N+1 for a candidate of Release N), and uninstall. It writes one
``pinokio-row-v1`` receipt. Every step must pass.
It never force-kills a process; a stuck process is left for the runner to reap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

try:
    from . import ci_journey
    from . import verify_installed
except ImportError:  # executed as scripts/pinokio_row_driver.py
    import ci_journey
    import verify_installed

SCHEMA = "pinokio-row-v1"
FRESH_SCHEMA = "pinokio-row-v2"
STEP_NAMES = ("install", "start_task", "stop_start", "busy_stop_data", "update_ready_task", "uninstall_data")
JOB_OS = {"pinokio-linux": "linux", "pinokio-macos": "macos", "pinokio-windows": "windows"}
PLATFORM_OS = {"linux": "linux", "darwin": "macos", "win32": "windows"}
APP_NAME = "astrid-row"
PROJECT = "pinokio-row"
MARKER_TEXT = "pinokio row data marker\n"
MARKER_NAME = "pinokio-row-marker.txt"
# The chiptune SFX blip takes 0.1s by default. A longer duration keeps the task
# running while Stop is requested; the pack's upper bound is not in this checkout.
# The chiptune task renders far faster than its audio length, so a short task can
# finish before Stop lands. Escalate the length until a task is caught running.
BUSY_DURATIONS_S = (120, 600, 1800)
BUSY_REFUSAL = re.compile(r"Astrid is busy \(\d+ tasks running\)")
TERMINAL = ci_journey.TERMINAL


def _pid_alive(pid: int) -> bool:
    """Liveness probe only. On Windows os.kill(pid, 0) would terminate the process."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class RowError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RowError(f"{path}: expected a JSON object")
    return value


def _tail(text: str, limit: int = 8000) -> str:
    text = re.sub(r"(?i)(token|password|secret|authorization)(\s*[=:]\s*)([^\s,;]+(?:\s+[^\s,;]+)?)",
                  r"\1\2<redacted>", text)
    text = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer <redacted>", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,})\b", "<redacted>", text)
    return text[-limit:]


class Row:
    """One Pinokio row on one OS. Stage methods raise RowError on any mismatch."""

    def __init__(self, *, job_id: str, os_name: str, run_root: Path, pinokio: Path,
                 agent: Path, app_checkout: Path, candidate_sha: str):
        self.job_id = job_id
        self.os_name = os_name
        self.run_root = run_root
        self.home = run_root / "home"
        self.scratch = run_root / "work"
        self.evidence = self.scratch / "evidence"
        self.pinokio = pinokio
        self.launcher = pinokio
        self.agent = agent
        self.app_checkout = app_checkout
        self.candidate_sha = candidate_sha
        self.candidate_path: Path | None = None
        self.pinokio_home = run_root / "pinokio-home"
        self.app_dir = self.pinokio_home / "api" / APP_NAME
        self.root = self.app_dir / "app" / "astrid"
        self.manifest = app_checkout / "public" / "install-manifest.json"
        # The manifest of the release last installed: what astrid.haus would
        # serve once this candidate is published.
        self.published: Path | None = None
        self.env: dict[str, str] = {}
        self.child: subprocess.Popen | None = None
        self.adopted_pid: int | None = None
        self.session_count = 0
        self.version: dict | None = None
        self.data: Path | None = None
        self.instance: str | None = None
        self.realm: str | None = None
        self.marker: Path | None = None
        self.task_id: str | None = None
        self.log_count = 0

    # Process and CLI helpers -------------------------------------------------

    def command(self, argv: list[str], *, timeout: float = 900) -> str:
        """Run one launcher command with its output bounded to a file under the run root."""
        self.log_count += 1
        with (
            tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", dir=self.scratch,
                                        prefix=f"command-{self.log_count}-", delete=False) as output,
            tempfile.TemporaryFile(mode="w+", encoding="utf-8", dir=self.scratch) as stderr,
        ):
            proc = subprocess.Popen([str(part) for part in argv], env=self.env, cwd=str(self.home),
                                    stdout=output, stderr=stderr, text=True)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise RowError(f"command timed out (pid {proc.pid} left running): {argv[0]}") from exc
            output.flush()
            output.seek(0)
            text = output.read()
            stderr.seek(0)
            errors = stderr.read()
        if proc.returncode:
            error = RowError(f"command failed (exit {proc.returncode}): {argv[0]}: {_tail(text + errors)}")
            error.child_exit_code = proc.returncode
            error.stderr_tail = _tail(errors, limit=2000)
            raise error
        return text

    def installed(self, argv: list[str], name: str, *, timeout: int = 300) -> str:
        return ci_journey.installed_cli(self.version, argv, self.evidence, name, timeout=timeout)

    def task_state(self, task_id: str) -> str:
        output = self.installed(["tasks", "show", task_id, "--project", PROJECT, "--json"], "task-show.json")
        data = ci_journey._sdk_result(output).get("data") or {}
        state = data.get("state")
        if not isinstance(state, str) or not state:
            raise RowError(f"task {task_id} has no readable state")
        return state

    def wait_for_state(self, task_id: str, wanted: set[str], *, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while True:
            state = self.task_state(task_id)
            if state in wanted:
                return state
            if state in TERMINAL and wanted.isdisjoint(TERMINAL):
                raise RowError(f"task {task_id} reached {state} before {sorted(wanted)}")
            if time.monotonic() >= deadline:
                raise RowError(f"task {task_id} stayed {state}; wanted {sorted(wanted)}")
            time.sleep(0.5)

    def health(self) -> dict:
        """Return the live health identity after checking it against the selected discovery."""
        if self.data is None:
            raise RowError("no selected data root")
        discovery = read_json(self.data / "runtime" / "discovery.json")
        request = urllib.request.Request(ci_journey.HEALTH_URL, headers={"Origin": "https://astrid.haus"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            health = json.load(response)
        record = self.record()[2]
        identity = record.get("identity")
        instance = discovery.get("runtime_instance_id")
        if (health.get("status") != "ok" or health.get("protocol") != "workspace.v1" or not instance
                or health.get("runtime_instance_id") != instance or not isinstance(identity, dict)
                or identity.get("managed_by") != "pinokio" or health.get("identity") != identity
                or discovery.get("identity") != identity):
            raise RowError("Runtime health, release identity and selected discovery do not match")
        if self.candidate_path is not None:
            self.verify_candidate_install()
        realm = Path(discovery["realm_root"])
        if not realm.is_absolute() or not realm.is_relative_to(self.data.parent):
            raise RowError("Runtime realm is outside the selected data root")
        self.realm = str(realm)
        return {"instance": instance, "realm": str(realm), "identity": identity,
                "schema_digest": health.get("schema_digest")}

    def assert_same_runtime(self) -> None:
        live = self.health()
        if live["instance"] != self.instance or live["realm"] != self.realm:
            raise RowError("Runtime instance or realm changed while the session was running")

    # Install and record ------------------------------------------------------

    def candidate_release(self) -> int:
        """The release N declared by the App candidate manifest (an integer >= 1)."""
        release = read_json(self.manifest).get("release")
        if isinstance(release, bool) or not isinstance(release, int) or release < 1:
            raise RowError("the App candidate manifest must declare an integer release >= 1")
        return release

    @property
    def standin(self) -> Path:
        """Stand-in for the next release, Release N+1, under the scratch directory."""
        return self.scratch / f"release{self.candidate_release() + 1}" / "install-manifest.json"

    def record(self) -> tuple[Path, Path, dict]:
        install = read_json(self.root / "install.json")
        if install.get("owner") != "haus.astrid.runtime" or install.get("managed_by") != "pinokio":
            raise RowError("install record does not identify this Pinokio program root")
        data = Path(install["data_root"])
        if not data.is_absolute() or not data.is_relative_to(self.home):
            raise RowError(f"install data root is outside the isolated HOME: {data}")
        current = (self.root / "current").read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", current):
            raise RowError("invalid current version")
        version = self.root / "versions" / current
        record = read_json(version / "version.json")
        if Path(record["root"]) != version:
            raise RowError("version root mismatch")
        return data, version, record

    def installer_argv(self, manifest: Path, *, uninstall: bool = False) -> list[str]:
        if self.os_name == "windows":
            argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(self.app_checkout / "public" / "install.ps1")]
        else:
            argv = ["sh", str(self.app_checkout / "public" / "install")]
        argv += ["--root", str(self.root), "--managed-by", "pinokio"]
        if uninstall:
            argv.append("--uninstall")
        # Always explicit: without --manifest the install line fetches the
        # production manifest, so uninstall would run the published release's
        # installer and Runtime against this candidate's installation.
        return argv + ["--manifest", str(manifest)]

    def install(self, manifest: Path, *, expected: dict) -> None:
        argv = self.installer_argv(manifest)
        if self.published is None:
            # A fresh install asks for setup consent. Pinokio's terminal is
            # interactive and the person answers there; this row has no
            # terminal, so it accepts the recommended choices as a person would.
            # Updates of the existing install ask nothing, as in Pinokio.
            argv.append("--yes")
        self.command(argv, timeout=3600)
        self.data, version, record = self.record()
        installed = read_json(version / "manifest.json")
        if any(installed.get(key) != value for key, value in expected.items()):
            raise RowError("installed manifest differs from the requested manifest")
        if record.get("identity", {}).get("managed_by") != "pinokio":
            raise RowError("installed version is not Pinokio-managed")
        self.published = manifest

    def prepare(self) -> None:
        if PLATFORM_OS.get(sys.platform) != self.os_name:
            raise RowError(f"row {self.job_id} requires {self.os_name}; this host is {sys.platform}")
        if self.job_id not in JOB_OS or JOB_OS[self.job_id] != self.os_name:
            raise RowError("job id and OS do not match")
        head = subprocess.check_output(["git", "-C", str(self.pinokio), "rev-parse", "HEAD"], text=True).strip()
        if head != self.candidate_sha:
            raise RowError("Pinokio checkout differs from the candidate pinokio SHA")
        installer = "install.ps1" if self.os_name == "windows" else "install"
        if not self.manifest.is_file() or not (self.app_checkout / "public" / installer).is_file():
            raise RowError("App candidate installer or manifest is missing")
        self.candidate_release()
        if self.root.exists():
            raise RowError("install root already exists; refusing to modify it")
        if self.home.exists():
            raise RowError("isolated HOME already exists; refusing reuse")
        self.home.mkdir(parents=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        # Pinokio clones the launcher repository into its app folder; the updater
        # looks for pinokio/session.mjs there, so lay the folder out the same way.
        self.app_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.pinokio, self.app_dir, ignore=shutil.ignore_patterns(".git", "tests"))
        self.launcher = self.app_dir
        code = ("const p=require(process.argv[1]);"
                "process.stdout.write(JSON.stringify(p.buildEnvironment({appDirectory:process.argv[2]})))")
        self.env = dict(os.environ, HOME=str(self.home))
        if self.os_name == "windows":
            # Windows keeps per-user data under these, not HOME; isolate them too.
            self.env.update(USERPROFILE=str(self.home),
                            LOCALAPPDATA=str(self.home / "AppData" / "Local"),
                            APPDATA=str(self.home / "AppData" / "Roaming"))
            for key in ("LOCALAPPDATA", "APPDATA"):
                Path(self.env[key]).mkdir(parents=True, exist_ok=True)
        self.env = json.loads(self.command(["node", "-e", code, self.launcher / "pinokio" / "environment.js",
                                            self.app_dir], timeout=30))
        if self.env.get("HOME") != str(self.home):
            raise RowError("candidate environment rejected the isolated HOME")
        if self.os_name == "windows":
            for key in ("USERPROFILE", "LOCALAPPDATA", "APPDATA"):
                self.env[key] = str({"USERPROFILE": self.home, "LOCALAPPDATA": self.home / "AppData" / "Local",
                                     "APPDATA": self.home / "AppData" / "Roaming"}[key])
        # Shared journey selection probes run in-process and must use the same
        # isolated account as Pinokio's installer and Runtime children.
        for key in ("HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "XDG_DATA_HOME"):
            if key in self.env:
                os.environ[key] = self.env[key]
            else:
                os.environ.pop(key, None)
        if self.candidate_path is not None:
            if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
                self.env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
            return
        # The stand-in for release N+1 is generated from the App's own shipped manifest.
        standin = self.standin
        standin.parent.mkdir(parents=True, exist_ok=True)
        self.command([sys.executable, self.agent / "scripts" / "make_standin_release.py", self.manifest,
                      "--output", standin], timeout=120)
        generated = read_json(standin)
        if generated.get("release") != self.candidate_release() + 1 or not generated.get("qualification_marker"):
            raise RowError("generated stand-in is not a qualification manifest for the next release")

    def install_initial(self) -> None:
        self.install(self.manifest, expected=read_json(self.manifest))

    def install_fresh(self) -> dict:
        if self.candidate_path is None:
            raise RowError("fresh lifecycle requires its frozen candidate")
        candidate = read_json(self.candidate_path)
        manifest_bytes = self.manifest.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != candidate.get("manifest_sha256"):
            raise RowError("App manifest SHA differs from the frozen candidate")
        manifest = json.loads(manifest_bytes)
        app_head = subprocess.check_output(["git", "-C", str(self.app_checkout), "rev-parse", "HEAD"], text=True).strip()
        if app_head != candidate.get("app"):
            raise RowError("App checkout differs from the frozen candidate SHA")
        pins = {"agent": candidate.get("agent"), "runtime": candidate.get("runtime")}
        for key, value in pins.items():
            if not isinstance(value, str) or not value or manifest.get(key, {}).get("sha") != value:
                raise RowError(f"candidate {key} pin does not match the App manifest")
        self.launcher_install_action(candidate["app"])
        self.data, _version, _record = self.record()
        self.published = self.manifest
        installed = self.verify_candidate_install()
        selection = ci_journey.pack_host_ready_path(pinokio_row={"home": str(self.home),
            "program_root": str(self.root), "data_root": str(self.data)})
        if selection != self.data / "runtime" / ci_journey.PACK_HOST_READY:
            raise RowError("Pinokio-owned selection resolved outside its selected data root")
        return installed

    def launcher_install_action(self, app_sha: str) -> None:
        """Execute the pinned launcher's install.js action array, including its own custom steps."""
        if not re.fullmatch(r"[0-9a-f]{40}", app_sha):
            raise RowError("launcher install requires a full immutable App SHA")
        self.env["ASTRID_APP_REF"] = app_sha
        code = ("const {spawnSync}=require('node:child_process'); const action=require(process.argv[1]);"
                "(async()=>{for(const step of await action()){if(typeof step.method==='function'){await step.method();continue;}"
                "if(step.method!=='shell.run')throw Error('unsupported Pinokio install action step '+step.method);"
                "const p=step.params, win=process.platform==='win32'; const args=win?['-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',p.message]:['-lc',p.message];"
                "const r=spawnSync(p.shell,args,{cwd:p.path,env:process.env,encoding:'utf8',stdio:['ignore','pipe','pipe']});"
                "if(r.stdout)process.stdout.write(r.stdout);if(r.stderr)process.stderr.write(r.stderr);"
                "if(r.error||r.status!==0)throw Error('Pinokio install shell action failed: '+(r.error?.message||r.status));}})().catch(e=>{console.error(e.stack||e);process.exit(1)});")
        self.command(["node", "-e", code, self.launcher / "pinokio" / "install.js"], timeout=3600)

    def verify_candidate_install(self) -> dict:
        if self.candidate_path is None:
            raise RowError("frozen candidate is unavailable")
        candidate = read_json(self.candidate_path)
        data, version, record = self.record()
        manifest_path = version / "manifest.json"
        if manifest_path.is_symlink():
            raise RowError("selected manifest is a symlink")
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        expected = read_json(self.manifest)
        if manifest != expected or manifest_bytes != verify_installed.installer_manifest_bytes(expected):
            raise RowError("selected installed manifest differs from the frozen candidate and installer provenance")
        if any(manifest.get(key, {}).get("sha") != candidate.get(key) for key in ("agent", "runtime")):
            raise RowError("selected installed Agent/Runtime pins differ from the frozen candidate")
        if record.get("identity") != {"release": 14, "managed_by": "pinokio"}:
            raise RowError("selected install provenance is not Pinokio")
        problems = verify_installed._version_provenance_problems(record, manifest, version / "version.json")
        if problems:
            raise RowError("selected installed provenance failed: " + "; ".join(problems))
        return {"manifest_sha256": candidate["manifest_sha256"],
                "installed_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "manifest": manifest,
                "selected_program_root": str(self.root), "selected_data_root": str(data)}

    def update_install(self) -> None:
        self.install(self.standin, expected=read_json(self.standin))

    def uninstall(self) -> None:
        if self.published is None:
            raise RowError("uninstall needs the manifest of the installed candidate release")
        self.command(self.installer_argv(self.published, uninstall=True), timeout=3600)

    # Session and stop --------------------------------------------------------

    def start(self) -> dict:
        if self.candidate_path is not None:
            self.verify_candidate_install()
        self.data, version, record = self.record()
        self.version = record
        node = Path(record["env"]["ASTRID_NODE_EXECUTABLE"])
        if not node.is_absolute() or not node.is_relative_to(version):
            raise RowError("pinned Node is outside the selected version")
        self.session_count += 1
        log = self.scratch / f"session-{self.session_count}.log"
        with log.open("w", encoding="utf-8") as output:
            self.child = subprocess.Popen(
                [str(node), str(self.launcher / "pinokio" / "session.mjs"), "serve",
                 str(self.root), str(self.root.parent / "editor"), "backend"],
                env=self.env, cwd=str(self.home), stdout=output, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 180
        last: Exception | str = "no response"
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                raise RowError(f"session exited {self.child.returncode} before readiness: "
                               f"{_tail(log.read_text(encoding='utf-8', errors='replace'))}")
            try:
                live = self.health()
                if "ASTRID_PINOKIO_READY" not in log.read_text(encoding="utf-8", errors="replace"):
                    raise RowError("launcher has not emitted its readiness sentinel")
                control = read_json(self.root / "pinokio-control.json")
                if control.get("pid") != self.child.pid or not control.get("token") or not control.get("port"):
                    raise RowError("controller does not identify the session child")
                self.instance = live["instance"]
                receipt = {"readySentinel": True, "runtimeInstanceId": self.instance,
                        "runtimeIdentity": live["identity"], "realmRoot": live["realm"],
                        "selectedProgramRoot": str(self.root), "selectedDataRoot": str(self.data)}
                if self.candidate_path is not None:
                    manifest = read_json(self.manifest)
                    if live.get("schema_digest") != manifest["runtime"]["schema_digest"]:
                        raise RowError("live Runtime schema digest differs from the frozen candidate")
                    receipt["schemaDigest"] = live["schema_digest"]
                return receipt
            except (OSError, ValueError, KeyError, RowError) as exc:
                last = exc
            time.sleep(0.5)
        raise RowError(f"session did not become ready: {last}")

    def stop_request(self) -> None:
        """Invoke the launcher's own stop functions, including preflight and the installer lock wait."""
        code = ("const m=await import(process.argv[1]); const root=process.argv[2], appDirectory=process.argv[3];"
                "const o={appDirectory}; await m.preflightStop(root,o);"
                "const r=await m.requestGracefulStop(root,o);"
                "if(r.absent) throw Error('Active session controller absent');"
                "await m.waitForInstallerLock(root,o);")
        self.command(["node", "--input-type=module", "-e", code,
                      (self.launcher / "pinokio" / "lifecycle.mjs").as_uri(), self.root, self.app_dir], timeout=90)

    def adopt_running(self) -> None:
        """Take over the session the Pinokio updater left running on the new version."""
        self.data, _version, self.version = self.record()
        control = read_json(self.root / "pinokio-control.json")
        pid = control.get("pid")
        if not isinstance(pid, int) or not control.get("token") or not control.get("port"):
            raise RowError("updater left no session controller")
        if not _pid_alive(pid):
            raise RowError("updater's session is not running")
        self.child = None
        self.adopted_pid = pid
        self.instance = self.health()["instance"]

    def finish_stop(self) -> None:
        if self.child is None:
            # An adopted session is not our child: wait for it to exit by pid.
            deadline = time.monotonic() + 15
            while True:
                if not _pid_alive(self.adopted_pid):
                    break
                if time.monotonic() >= deadline:
                    raise RowError("adopted session did not exit after graceful stop; left running")
                time.sleep(0.2)
        else:
            try:
                rc = self.child.wait(timeout=15)
            except subprocess.TimeoutExpired as exc:
                raise RowError("session did not exit after graceful stop; left running") from exc
            if rc != 0:
                raise RowError(f"session failed after stop: exit {rc}")
        if self.record()[0] != self.data:
            raise RowError("selected data root changed during stop")
        discovery = self.data / "runtime" / "discovery.json"
        if discovery.exists() and read_json(discovery).get("runtime_instance_id") not in (None, self.instance):
            raise RowError("a different Runtime appeared at the selected root during stop")

    def stop(self) -> dict:
        self.assert_same_runtime()
        runtime_pid = None
        if self.candidate_path is not None:
            runtime_pid = read_json(self.data / "runtime" / "discovery.json").get("pid")
        self.stop_request()
        self.finish_stop()
        if self.candidate_path is not None:
            if type(runtime_pid) is not int or _pid_alive(runtime_pid):
                raise RowError("the starting Runtime process survived Pinokio Stop")
            if self.child is None or self.child.returncode != 0:
                raise RowError("the owned Pinokio session did not exit cleanly")
        return {"stopped": True, "runtimeInstanceId": self.instance,
                "selectedProgramRoot": str(self.root), "selectedDataRoot": str(self.data),
                "sessionExitCode": self.child.returncode if self.child else None}

    def open_app(self) -> dict:
        """Use the launcher's resolved Open Astrid URL with the frozen App build."""
        code = ("const launcher=require(process.argv[1]); const app=require(process.argv[3]);"
                "const mode=app.readInstallMode();"
                "const target=app.modeOpenTarget(mode); const info={running:(s)=>s==='pinokio/start.js',"
                "exists:(p)=>require('node:fs').existsSync(require('node:path').join(process.argv[2],p)),"
                "local:()=>({url:target.href,mode})};"
                "launcher.menu({},info).then(items=>{const item=items.find(x=>x.text==='Open Astrid');"
                "if(!item)throw Error('Pinokio menu has no Open Astrid action');"
                "process.stdout.write(JSON.stringify({href:item.href,target_full:item.target_full,popout:item.popout,menu:item.text}))})")
        target = json.loads(self.command(["node", "-e", code,
            self.launcher / "pinokio.js", self.app_dir, self.launcher / "pinokio" / "lib.js"], timeout=30))
        if (target.get("menu") != "Open Astrid" or target.get("href") != "/"
                or target.get("target_full") != "https://astrid.haus/?connect=1" or target.get("popout") is not True):
            raise RowError("Pinokio Open Astrid target differs from the hosted connection URL")
        if self.data is None:
            raise RowError("Open Astrid requires the selected installed data root")
        identity = self.health()
        result_path = self.evidence / "open-app.json"
        script = Path(__file__).with_name("journey_pinokio_open.mjs")
        candidate = read_json(self.candidate_path)
        self.command(["node", script, str(self.app_checkout / "dist"), target["target_full"], str(result_path),
                      candidate["app"], candidate["manifest_sha256"],
                      str(self.app_checkout)], timeout=180)
        result = read_json(result_path)
        if (result.get("target") != target["target_full"] or result.get("connected") is not True
                or result.get("runtimeInstanceId") != identity["instance"]
                or result.get("runtimeIdentity") != identity["identity"]
                or result.get("appBuild") != candidate["app"]
                or result.get("manifestSha256") != candidate["manifest_sha256"]
                or result.get("schemaDigest") != identity.get("schema_digest")):
            raise RowError("Open Astrid browser proof does not match the selected live Runtime and App build")
        return result

    # Busy stop, marker and task ---------------------------------------------

    def task_once(self, *, create_project: bool) -> None:
        data, _version, _record = self.record()
        pinokio_row = {"home": str(self.home), "program_root": str(self.root), "data_root": str(data)}
        self.task_id = ci_journey.task(self.version, self.evidence, PROJECT, create_project=create_project,
                                      pinokio_row=pinokio_row)

    def write_marker(self) -> None:
        self.marker = Path(self.realm) / MARKER_NAME
        self.marker.write_text(MARKER_TEXT, encoding="utf-8")

    def verify_marker(self) -> None:
        if not self.marker or self.marker.read_text(encoding="utf-8") != MARKER_TEXT:
            raise RowError("workspace data marker did not survive")
        if self.health()["realm"] != str(self.marker.parent):
            raise RowError("restart selected a different workspace")

    def busy_stop(self) -> str:
        """Stop while a task runs: accept one busy refusal or a clean stop, then prove the task outcome."""
        self.write_marker()
        for attempt, duration in enumerate(BUSY_DURATIONS_S, 1):
            busy = ci_journey.submit_task(self.version, self.evidence, PROJECT, create_project=False,
                                          duration_s=duration)
            self.task_id = busy
            if self.wait_for_state(busy, {"running", *TERMINAL}, timeout=120) == "running":
                break
        else:
            # On a fast runner the task can finish before a poll sees it running. Send Stop
            # straight after admission instead; a busy refusal or a clean stop are both accepted,
            # and the receipt says which happened.
            busy = ci_journey.submit_task(self.version, self.evidence, PROJECT, create_project=False,
                                          duration_s=BUSY_DURATIONS_S[-1])
            self.task_id = busy
            duration, attempt = BUSY_DURATIONS_S[-1], len(BUSY_DURATIONS_S) + 1
        caught = f" ({duration}s audio, attempt {attempt}" + (", Stop sent right after admission)" if attempt > len(BUSY_DURATIONS_S) else ", task seen running)")
        try:
            self.stop_request()
        except RowError as exc:
            if not BUSY_REFUSAL.search(str(exc)):
                raise
            self.assert_same_runtime()
            self.wait_for_state(busy, set(TERMINAL), timeout=900)
            self.stop()
            outcome = "busy stop refused once; same Runtime; task terminal; retried stop succeeded" + caught
        else:
            self.finish_stop()
            outcome = "busy stop completed cleanly" + caught
        self.start()
        self.wait_for_state(busy, set(TERMINAL), timeout=60)
        self.verify_marker()
        return outcome

    def next_release_check(self) -> None:
        _, version, record = self.record()
        installed = read_json(version / "manifest.json")
        if installed != read_json(self.standin):
            raise RowError("installed next-release manifest differs from the stand-in")
        if record.get("identity") != {"release": self.candidate_release() + 1, "managed_by": "pinokio"}:
            raise RowError("installed identity is not the next release managed by Pinokio")
        self.health()
        self.verify_marker()

    def cleanup_check(self) -> None:
        if self.root.exists() and any((self.root / name).exists() for name in ("current", "install.json", "versions", "run")):
            raise RowError("uninstall left program files or installation identity")
        if not self.marker or self.marker.read_text(encoding="utf-8") != MARKER_TEXT:
            raise RowError("uninstall did not preserve the workspace data marker")


def stages(row: Row) -> list[tuple[str, list]]:
    """Each stage returns an optional detail string. The six names match the checker."""
    def detail(text):
        return lambda: text

    return [
        ("install", [row.prepare, row.install_initial, detail("installed the candidate release and verified its record")]),
        ("start_task", [row.start, lambda: row.task_once(create_project=True),
                        detail("session ready and one chiptune task settled")]),
        ("stop_start", [row.stop, row.start, detail("graceful stop exited 0 and the session restarted")]),
        ("busy_stop_data", [row.busy_stop]),
        ("update_ready_task", [row.stop, row.update_install, row.adopt_running, row.next_release_check,
                               lambda: row.task_once(create_project=False),
                               detail("next-release identity; data marker kept; task settled")]),
        ("uninstall_data", [row.stop, row.uninstall, row.cleanup_check,
                            detail("uninstalled; no program files; data marker kept")]),
    ]


def fresh_stages(row: Row) -> list[tuple[str, list]]:
    """R14 scope: a fresh Pinokio installation, hosted Open target, and clean stop."""
    return [
        ("install", [row.prepare, row.install_fresh]),
        ("start", [row.start]),
        ("open_app", [row.open_app]),
        ("stop", [row.stop]),
    ]


def run_fresh_row(row: Row) -> list[dict]:
    observations = []
    failed = None
    for name, operations in fresh_stages(row):
        if failed and not (name == "stop" and failed == "open_app"):
            observations.append({"name": name, "status": "fail", "detail": f"not run: {failed} failed"})
            continue
        result = None
        try:
            for operation in operations:
                result = operation()
            detail = result if isinstance(result, dict) else {}
            observations.append({"name": name, "status": "pass", "detail": "verified",
                                 **({"observation": detail} if detail else {})})
        except Exception as exc:  # noqa: BLE001
            failed = name
            observations.append({"name": name, "status": "fail", "detail": _step(name, "fail", exc)["detail"],
                                 "child_exit_code": getattr(exc, "child_exit_code", None),
                                 "stderr_tail": getattr(exc, "stderr_tail", "")})
    return observations


def _step(name: str, status: str, detail: str) -> dict:
    text = re.sub(r"\s+", " ", str(detail)).strip()
    if len(text) > 500:
        # Keep the start and the end: installers print long output before the actual error.
        text = text[:150] + " ... " + text[-345:]
    text = text or status
    return {"name": name, "status": status, "detail": text}


def run_row(row: Row) -> list[dict]:
    steps: list[dict] = []
    failed: str | None = None
    for name, operations in stages(row):
        if failed is not None:
            steps.append(_step(name, "fail", f"not run: {failed} failed"))
            continue
        message = None
        try:
            for operation in operations:
                result = operation()
                if isinstance(result, str):
                    message = result
            steps.append(_step(name, "pass", message or "ok"))
        except Exception as exc:  # noqa: BLE001 - every stage failure is recorded in the receipt
            failed = name
            detail = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, json.JSONDecodeError):
                # Show what could not be parsed, so a hosted failure is diagnosable from the receipt.
                detail += f" | text: {exc.doc[:1500]!r}"
            # The receipt keeps a short form; the job log gets the whole failure.
            print(f"[pinokio-row] {name} failed:\n{detail}", file=sys.stderr, flush=True)
            steps.append(_step(name, "fail", detail))
    return steps


def receipt(job_id: str, os_name: str, pinokio_sha: str, steps: list[dict]) -> dict:
    return {"schema": SCHEMA, "job_id": job_id, "os": os_name, "pinokio_sha": pinokio_sha, "steps": steps}


def _failed_receipt(job_id: str, os_name: str, pinokio_sha: str, reason: str) -> dict:
    steps = [_step(name, "fail", f"not run: {reason}") for name in STEP_NAMES]
    return receipt(job_id, os_name, pinokio_sha, steps)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-id", required=True, choices=sorted(JOB_OS))
    parser.add_argument("--os", required=True, choices=sorted(set(JOB_OS.values())))
    parser.add_argument("--candidate", required=True, type=Path, help="frozen candidate JSON")
    parser.add_argument("--pinokio-checkout", required=True, type=Path)
    parser.add_argument("--agent-checkout", required=True, type=Path)
    parser.add_argument("--app-checkout", required=True, type=Path)
    parser.add_argument("--run-root", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--schema", choices=(SCHEMA, FRESH_SCHEMA), default=SCHEMA)
    args = parser.parse_args(argv)
    for path in (args.pinokio_checkout, args.agent_checkout, args.app_checkout, args.run_root):
        if not path.is_absolute():
            raise SystemExit(f"{path} must be an absolute path")
    sha = read_json(args.candidate).get("pinokio")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit("candidate pinokio must be a full commit SHA")
    row = Row(job_id=args.job_id, os_name=args.os, run_root=args.run_root, pinokio=args.pinokio_checkout,
              agent=args.agent_checkout, app_checkout=args.app_checkout, candidate_sha=sha)
    if args.schema == FRESH_SCHEMA:
        row.candidate_path = args.candidate
    try:
        steps = run_fresh_row(row) if args.schema == FRESH_SCHEMA else run_row(row)
        value = ({"schema": FRESH_SCHEMA, "job_id": args.job_id, "os": args.os, "pinokio_sha": sha,
                  "candidate": read_json(args.candidate), "stages": steps}
                 if args.schema == FRESH_SCHEMA else receipt(args.job_id, args.os, sha, steps))
    except Exception as exc:  # noqa: BLE001 - an unexpected driver error still yields a failing receipt
        if args.schema == FRESH_SCHEMA:
            value = {"schema": FRESH_SCHEMA, "job_id": args.job_id, "os": args.os, "pinokio_sha": sha,
                     "candidate": read_json(args.candidate), "stages": [
                         {"name": name, "status": "fail", "detail": f"driver error: {type(exc).__name__}: {exc}"}
                         for name in ("install", "start", "open_app", "stop")]}
        else:
            value = _failed_receipt(args.job_id, args.os, sha, f"driver error: {type(exc).__name__}: {exc}")
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = [step["name"] for step in value.get("stages", value.get("steps", [])) if step["status"] != "pass"]
    print(json.dumps({"job_id": args.job_id, "failed_steps": failed}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
