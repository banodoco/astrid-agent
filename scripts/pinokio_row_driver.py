"""Hosted Pinokio launcher row driver (Linux first; macOS and Windows reuse it).

The driver exercises the Pinokio launcher's own commands end to end: the
environment, the install action, ``session.mjs serve``, the exact lifecycle stop
functions, a stop that races a running task, an update to a stand-in Release 2,
and uninstall. It writes one ``pinokio-row-v1`` receipt. Every step must pass.
It never force-kills a process; a stuck process is left for the runner to reap.
"""

from __future__ import annotations

import argparse
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
except ImportError:  # executed as scripts/pinokio_row_driver.py
    import ci_journey

SCHEMA = "pinokio-row-v1"
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


class RowError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RowError(f"{path}: expected a JSON object")
    return value


def _tail(text: str, limit: int = 8000) -> str:
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
        self.pinokio_home = run_root / "pinokio-home"
        self.app_dir = self.pinokio_home / "api" / APP_NAME
        self.root = self.app_dir / "app" / "astrid"
        self.manifest = app_checkout / "public" / "install-manifest.json"
        self.standin = self.scratch / "release2" / "install-manifest.json"
        self.env: dict[str, str] = {}
        self.child: subprocess.Popen | None = None
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
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", dir=self.scratch,
                                         prefix=f"command-{self.log_count}-", delete=False) as output:
            proc = subprocess.Popen([str(part) for part in argv], env=self.env, cwd=str(self.home),
                                    stdout=output, stderr=subprocess.STDOUT, text=True)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise RowError(f"command timed out (pid {proc.pid} left running): {argv[0]}") from exc
            output.flush()
            output.seek(0)
            text = output.read()
        if proc.returncode:
            raise RowError(f"command failed (exit {proc.returncode}): {argv[0]}: {_tail(text)}")
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
        realm = Path(discovery["realm_root"])
        if not realm.is_absolute() or not realm.is_relative_to(self.data.parent):
            raise RowError("Runtime realm is outside the selected data root")
        self.realm = str(realm)
        return {"instance": instance, "realm": str(realm), "identity": identity}

    def assert_same_runtime(self) -> None:
        live = self.health()
        if live["instance"] != self.instance or live["realm"] != self.realm:
            raise RowError("Runtime instance or realm changed while the session was running")

    # Install and record ------------------------------------------------------

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

    def installer_argv(self, manifest: Path | None, *, uninstall: bool = False) -> list[str]:
        if self.os_name == "windows":
            argv = ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(self.app_checkout / "public" / "install.ps1")]
        else:
            argv = ["sh", str(self.app_checkout / "public" / "install")]
        argv += ["--root", str(self.root), "--managed-by", "pinokio"]
        if uninstall:
            argv.append("--uninstall")
        else:
            argv += ["--manifest", str(manifest)]
        return argv

    def install(self, manifest: Path, *, expected: dict) -> None:
        self.command(self.installer_argv(manifest), timeout=3600)
        self.data, version, record = self.record()
        installed = read_json(version / "manifest.json")
        if any(installed.get(key) != value for key, value in expected.items()):
            raise RowError("installed manifest differs from the requested manifest")
        if record.get("identity", {}).get("managed_by") != "pinokio":
            raise RowError("installed version is not Pinokio-managed")

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
        if read_json(self.manifest).get("release") != 1:
            raise RowError("the App candidate manifest must be Release 1")
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
        self.env = json.loads(self.command(["node", "-e", code, self.launcher / "pinokio" / "environment.js",
                                            self.app_dir], timeout=30))
        if self.env.get("HOME") != str(self.home):
            raise RowError("candidate environment rejected the isolated HOME")
        # Stand-in Release 2 is generated from the App's own shipped manifest.
        self.standin.parent.mkdir(parents=True, exist_ok=True)
        self.command([sys.executable, self.agent / "scripts" / "make_standin_release.py", self.manifest,
                      "--output", self.standin], timeout=120)
        if read_json(self.standin).get("release") != 2 or not read_json(self.standin).get("qualification_marker"):
            raise RowError("generated stand-in is not a Release 2 qualification manifest")

    def install_initial(self) -> None:
        self.install(self.manifest, expected=read_json(self.manifest))

    def update_install(self) -> None:
        self.install(self.standin, expected=read_json(self.standin))

    def uninstall(self) -> None:
        self.command(self.installer_argv(None, uninstall=True), timeout=3600)

    # Session and stop --------------------------------------------------------

    def start(self) -> None:
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
                return
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

    def finish_stop(self) -> None:
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

    def stop(self) -> None:
        self.assert_same_runtime()
        self.stop_request()
        self.finish_stop()

    # Busy stop, marker and task ---------------------------------------------

    def task_once(self, *, create_project: bool) -> None:
        self.task_id = ci_journey.task(self.version, self.evidence, PROJECT, create_project=create_project)

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
            raise RowError("every busy task finished before it was seen running; busy stop was not exercised")
        caught = f" (task running, {duration}s audio, attempt {attempt})"
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

    def release2_check(self) -> None:
        _, version, record = self.record()
        installed = read_json(version / "manifest.json")
        if installed != read_json(self.standin):
            raise RowError("installed Release 2 manifest differs from the stand-in")
        if record.get("identity") != {"release": 2, "managed_by": "pinokio"}:
            raise RowError("installed identity is not Release 2 managed by Pinokio")
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
        ("install", [row.prepare, row.install_initial, detail("installed release 1 and verified its record")]),
        ("start_task", [row.start, lambda: row.task_once(create_project=True),
                        detail("session ready and one chiptune task settled")]),
        ("stop_start", [row.stop, row.start, detail("graceful stop exited 0 and the session restarted")]),
        ("busy_stop_data", [row.busy_stop]),
        ("update_ready_task", [row.stop, row.update_install, row.start, row.release2_check,
                               lambda: row.task_once(create_project=False),
                               detail("release 2 identity; data marker kept; task settled")]),
        ("uninstall_data", [row.stop, row.uninstall, row.cleanup_check,
                            detail("uninstalled; no program files; data marker kept")]),
    ]


def _step(name: str, status: str, detail: str) -> dict:
    text = re.sub(r"\s+", " ", str(detail)).strip()[:500] or status
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
    args = parser.parse_args(argv)
    for path in (args.pinokio_checkout, args.agent_checkout, args.app_checkout, args.run_root):
        if not path.is_absolute():
            raise SystemExit(f"{path} must be an absolute path")
    sha = read_json(args.candidate).get("pinokio")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise SystemExit("candidate pinokio must be a full commit SHA")
    row = Row(job_id=args.job_id, os_name=args.os, run_root=args.run_root, pinokio=args.pinokio_checkout,
              agent=args.agent_checkout, app_checkout=args.app_checkout, candidate_sha=sha)
    try:
        value = receipt(args.job_id, args.os, sha, run_row(row))
    except Exception as exc:  # noqa: BLE001 - an unexpected driver error still yields a failing receipt
        value = _failed_receipt(args.job_id, args.os, sha, f"driver error: {type(exc).__name__}: {exc}")
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    args.receipt.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    failed = [step["name"] for step in value["steps"] if step["status"] != "pass"]
    print(json.dumps({"job_id": args.job_id, "failed_steps": failed}), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
