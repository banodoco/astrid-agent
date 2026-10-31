"""Named, installed-product install/update journeys for Linux, macOS and Windows.

The App checkout supplies the shipped manifest verbatim.  ``--source-override``
is an explicit test mode for candidate Agent and Runtime Git commits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from . import ci_windows_journey as windows
    from .journey_matrix import RELEASE1_MODES, validate_job
else:
    import ci_windows_journey as windows
    from journey_matrix import RELEASE1_MODES, validate_job


ROOT = Path(__file__).resolve().parents[1]
HEALTH_URL = windows.HEALTH_URL
TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
OWNER_FILE = "tst-b1-owner.json"
VERSION_JUMP_LEGS = ("update-from-served", "update-from-release:pre-updater",
                     "update-over-old-declaration", "old-runtime-busy",
                     "release0-to-release1")
RUNNER_FAMILY = {"ubuntu-24.04": "linux", "ubuntu-22.04": "linux",
                 "windows-2022": "win32", "macos-14": "darwin"}
GITHUB_RUNNER_OS = {"Linux": "linux", "Windows": "win32", "macOS": "darwin"}


def runner_identity() -> dict:
    """Record the workflow's declared image and raw hosted-runner observations."""
    label = os.environ.get("ASTRID_CI_EXPECTED_RUNNER") or "local"
    raw = {key: os.environ[key] for key in ("RUNNER_OS", "ImageOS", "ImageVersion")
           if os.environ.get(key)}
    return {"runner_image": label,
            "runner_provenance": {"observed_os_family": sys.platform, **raw}}


def assert_runner_identity(identity: dict) -> None:
    label = identity["runner_image"]
    if label != "local":
        if label not in RUNNER_FAMILY:
            raise ValueError(f"Unknown controlled runner label: {label}")
        if RUNNER_FAMILY[label] != sys.platform:
            raise RuntimeError(f"Runner label {label} disagrees with observed {sys.platform}")
        missing = [key for key in ("RUNNER_OS", "ImageOS", "ImageVersion")
                   if key not in identity["runner_provenance"]]
        if missing:
            raise RuntimeError(f"Hosted runner provenance is missing: {', '.join(missing)}")
    reported_os = identity["runner_provenance"].get("RUNNER_OS")
    if reported_os is not None and GITHUB_RUNNER_OS.get(reported_os) != sys.platform:
        raise RuntimeError(f"RUNNER_OS {reported_os} disagrees with observed {sys.platform}")


def credential_name(name: str) -> bool:
    upper = name.upper()
    return (upper in {"FAL_KEY", "GH_TOKEN", "GITHUB_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS"}
            or upper.startswith(("AWS_", "AZURE_"))
            or upper.endswith(("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS")))


@contextmanager
def without_provider_credentials():
    removed = {key: value for key, value in os.environ.items() if credential_name(key)}
    for key in removed:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.update(removed)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _under(path: Path, parent: Path) -> bool:
    return path != parent and path.is_relative_to(parent)


def run_scope(run_root: Path, root: Path, data: Path, leg: str) -> tuple[Path, Path, Path, Path]:
    """Fail closed before a lifecycle command can touch login-managed state."""
    if not all(path.is_absolute() for path in (run_root, root, data)):
        raise ValueError("--run-root, --root and --data-root must be absolute")
    if any(path != path.resolve() for path in (run_root, root, data)):
        raise ValueError("Journey roots cannot contain symlinks or path aliases")
    temporary = Path(tempfile.gettempdir()).resolve()
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve() if os.environ.get("RUNNER_TEMP") else temporary
    run_root = run_root.resolve()
    if not any(_under(run_root, allowed) for allowed in (temporary, runner_temp)):
        raise ValueError("--run-root must be a child of the runner temporary directory")
    if root.resolve() != run_root / "program" or data.resolve() != run_root / "data":
        raise ValueError("--root and --data-root must be the run-owned program/data children")
    root, data = root.resolve(), data.resolve()
    owner_path = run_root / OWNER_FILE
    if leg == "fresh-install" or leg in VERSION_JUMP_LEGS:
        if run_root.exists() and any(run_root.iterdir()):
            raise ValueError("A fresh journey requires an empty run-owned directory")
        run_root.mkdir(parents=True, exist_ok=True)
        with owner_path.open("x", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "run_id": uuid.uuid4().hex,
                       "root": str(root), "data_root": str(data)}, stream)
            stream.write("\n")
    else:
        if owner_path.is_symlink():
            raise ValueError("Journey ownership record cannot be a symlink")
        owner = read_json(owner_path)
        if (owner.get("schema_version") != 1 or
                not re.fullmatch(r"[0-9a-f]{32}", str(owner.get("run_id", ""))) or
                owner.get("root") != str(root) or owner.get("data_root") != str(data)):
            raise ValueError("Existing journey roots lack this run's ownership record")
    for scoped in (run_root / "home", run_root / "localappdata", run_root / "appdata",
                   run_root / "tmp", run_root / "home" / ".config",
                   run_root / "home" / ".local", run_root / "home" / ".local" / "share"):
        if scoped != scoped.resolve():
            raise ValueError("Journey environment path cannot alias another installation")
    evidence = run_root / "evidence" / leg
    if evidence != evidence.resolve():
        raise ValueError("Journey evidence cannot alias another path")
    return run_root, root, data, evidence


@contextmanager
def isolated_environment(run_root: Path, root: Path, data: Path):
    """Keep bootstrap state, selection records and platform data under this run."""
    home = run_root / "home"
    local = run_root / "localappdata"
    for path in (home, local, run_root / "tmp"):
        path.mkdir(parents=True, exist_ok=True)
    changes = {"HOME": str(home), "USERPROFILE": str(home), "LOCALAPPDATA": str(local),
               "APPDATA": str(run_root / "appdata"), "TMP": str(run_root / "tmp"),
               "TEMP": str(run_root / "tmp"), "TMPDIR": str(run_root / "tmp"),
               "XDG_DATA_HOME": str(home / ".local" / "share"),
               "XDG_STATE_HOME": str(home / ".local" / "state"),
               "XDG_CONFIG_HOME": str(home / ".config"),
               "ASTRID_RUNTIME_DATA_ROOT": str(data),
               "ASTRID_CI_PROGRAM_ROOT": str(root), "ASTRID_CI_DATA_ROOT": str(data)}
    prior = {key: os.environ.get(key) for key in changes}
    os.environ.update(changes)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def process_birth(pid: int) -> str | None:
    if sys.platform == "win32":
        output = windows.powershell(
            f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
            "if ($p) { $p.CreationDate.ToUniversalTime().ToString('o') }",
            "sentinel-birth.txt", show=False, timeout=30)
        return output.strip() or None
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, timeout=10, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def sentinel_alive(process: subprocess.Popen, birth: str) -> bool:
    return sentinel_observation(process, birth)["alive"]


def sentinel_observation(process: subprocess.Popen, birth: str) -> dict:
    """Keep exit and OS birth checks distinct in the small result receipt."""
    exit_code = process.poll()
    try:
        observed_birth = process_birth(process.pid)
        birth_error = None
    except (OSError, subprocess.SubprocessError) as exc:
        observed_birth = None
        birth_error = f"{type(exc).__name__}: {exc}"
    observation = {"pid": process.pid, "exit_code": exit_code,
                   "observed_birth": observed_birth,
                   "alive": exit_code is None and observed_birth == birth}
    if birth_error:
        observation["birth_error"] = birth_error
    return observation


@contextmanager
def sentinel_process(run_root: Path):
    """A harmless unrelated process, terminated only while its birth still matches."""
    token = uuid.uuid4().hex
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", "import time; time.sleep(7200)", token],
        cwd=run_root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=os.name == "posix")
    try:
        birth = process_birth(process.pid)
        if not birth or not sentinel_alive(process, birth):
            raise RuntimeError("Could not establish the sentinel process birth identity")
        yield process, birth
    finally:
        # Popen retains this child process's handle; poll() cannot mistake a reused PID for it.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


def git_source(checkout: Path) -> dict:
    checkout = checkout.resolve(strict=True)
    sha = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"Not a full Git commit: {checkout}")
    return {"sha": sha, "repo": str(checkout)}


def selected_manifest(app: Path, *, source_override: bool, agent: Path | None,
                      runtime: Path | None, evidence: Path) -> tuple[Path, dict]:
    shipped = app / "public" / "install-manifest.json"
    original = shipped.read_bytes()
    manifest = json.loads(original)
    if not isinstance(manifest, dict):
        raise ValueError("The shipped install manifest is not an object")
    if source_override:
        if agent is None or runtime is None:
            raise ValueError("--source-override requires --agent-checkout and --runtime-checkout")
        manifest = {**manifest, "agent": git_source(agent), "runtime": git_source(runtime)}
        selected = evidence / "source-override-manifest.json"
        write_json(selected, manifest)
    else:
        if agent is not None or runtime is not None:
            raise ValueError("Checkout sources require the explicitly labelled --source-override test mode")
        selected = shipped
        if selected.read_bytes() != original:
            raise RuntimeError("The shipped manifest changed during selection")
    return selected, manifest


def program_root() -> Path:
    home = Path.home()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "Astrid"
    return Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "astrid"


def windows_task_owned(output: str, root: Path) -> bool:
    lines = [line.strip().strip('"') for line in output.splitlines() if line.strip()]
    if not lines or lines[0] != "present" or len(lines) != 2:
        return False
    root_prefix = ntpath.normcase(str(root).rstrip("\\/") + "\\")
    return ntpath.normcase(lines[1].replace("/", "\\")).startswith(root_prefix)


def assert_host_available(leg: str, root: Path, data: Path) -> None:
    """Reject an existing real login install before changing any service."""
    default = program_root()
    selection_path = default.parent / "astrid-install.json"
    selection = read_json(selection_path) if selection_path.exists() else None
    owned_uninstalled = (leg == "cleanup" and isinstance(selection, dict)
                         and selection.get("owner") == "haus.astrid.runtime"
                         and selection.get("program_root") == str(root)
                         and selection.get("data_root") == str(data)
                         and selection.get("uninstalled") is True
                         and not selection.get("foreground"))
    if ((default / "install.json").exists() or (default / "current").exists()
            or (selection is not None and not owned_uninstalled)):
        raise RuntimeError("A non-journey Astrid login installation is present")
    fresh = leg in {"fresh-install", "cleanup"}
    if sys.platform == "darwin":
        if (Path.home() / "Library" / "LaunchAgents" / "haus.astrid.runtime.plist").exists():
            raise RuntimeError("A non-journey Astrid LaunchAgent is present")
        manager = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/haus.astrid.runtime"],
                                 capture_output=True, text=True, timeout=10, check=False)
        if manager.returncode == 0:
            expected = str(data.parent / "home" / "Library" / "LaunchAgents" /
                           "haus.astrid.runtime.plist")
            loaded_path = re.search(r"^\s*path\s*=\s*(.+?)\s*$", manager.stdout, re.MULTILINE)
            if fresh or loaded_path is None or loaded_path.group(1) != expected:
                raise RuntimeError("An Astrid LaunchAgent outside this journey is registered")
    elif sys.platform == "linux":
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if (config / "systemd" / "user" / "astrid.service").exists():
            raise RuntimeError("A non-journey Astrid user service is present")
        try:
            manager = subprocess.run(["systemctl", "--user", "show", "astrid.service",
                                      "--property=LoadState,FragmentPath"],
                                     capture_output=True, text=True, timeout=10, check=False)
        except FileNotFoundError:
            manager = None
        if manager is not None and manager.returncode == 0:
            fields = dict(line.split("=", 1) for line in manager.stdout.splitlines() if "=" in line)
            if fields.get("LoadState") not in {None, "not-found"}:
                expected = str(data.parent / "home" / ".config" / "systemd" / "user" /
                               "astrid.service")
                if fresh or fields.get("FragmentPath") != expected:
                    raise RuntimeError("An Astrid user-manager unit outside this journey is loaded")
    elif sys.platform == "win32":
        existing = windows.powershell(
            "$t=Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction SilentlyContinue; "
            "if ($t) { 'present'; $t.Actions | ForEach-Object { $_.Execute } }",
            "preflight-scheduled-task.txt", show=False, timeout=30)
        if existing.strip() and (fresh or not windows_task_owned(existing, root)):
            raise RuntimeError("An Astrid Scheduled Task outside this journey is registered")
    if fresh and runtime_processes(data):
        raise RuntimeError("A Runtime owner is already running on this host")


def _manager_probe(argv: list[str], *, label: str) -> str:
    """Return a native manager version/probe line or fail before bootstrap.

    The direct Release-1 leg has no foreground substitute.  In particular, a
    missing user systemd session is a prerequisite failure, not a reason to let
    the product's compatibility foreground branch make the receipt look green.
    """
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=30,
                                   check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required native service manager {label} is unavailable") from exc
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        raise RuntimeError(f"Required native service manager {label} is unusable{suffix}")
    line = next((line.strip() for line in (completed.stdout or completed.stderr).splitlines()
                 if line.strip()), "native-manager")
    return line


def release1_manager_prerequisite(args: argparse.Namespace, evidence: Path) -> dict:
    """Assert and record the declared owner before any lifecycle command runs."""
    job_id = getattr(args, "job_id", None)
    if not job_id:
        return {"required": False, "outcome": "not-required"}
    row = validate_job(job_id, args.mode)
    if row["mode"] != "direct":
        checkout = getattr(args, "pinokio_checkout", None)
        if checkout is None or not checkout.is_dir() or checkout.is_symlink():
            raise RuntimeError("Pinokio modes require an explicit non-symlink Pinokio checkout")
        return {"required": False, "outcome": "explicit-owner", "managed_by": "pinokio",
                "foreground_fallback": False, "checkout": str(checkout.resolve())}
    if sys.platform == "linux":
        version = _manager_probe(["systemctl", "--user", "--version"], label="systemd --user")
        matched = re.search(r"\bsystemd\s+(\d+)\b", version)
        if matched is None or int(matched.group(1)) < 254:
            raise RuntimeError("Required native service manager systemd --user must be version 254 or newer")
        _manager_probe(["systemctl", "--user", "show-environment"], label="systemd --user session")
        manager = "systemd-user"
    elif sys.platform == "darwin":
        version = _manager_probe(["launchctl", "version"], label="launchd")
        _manager_probe(["launchctl", "print", f"gui/{os.getuid()}"], label="launchd GUI domain")
        manager = "launchd"
    elif sys.platform == "win32":
        # The command checks both the PowerShell service-manager module and its
        # scheduler surface; it does not manufacture or start a task.
        version = windows.powershell(
            "$ErrorActionPreference='Stop'; Get-Command Get-ScheduledTask | Out-Null; "
            "$PSVersionTable.PSVersion.ToString()", "service-manager-prerequisite.log",
            show=False, timeout=30).strip()
        if not version:
            raise RuntimeError("Required native service manager Task Scheduler has no version observation")
        manager = "task-scheduler"
    else:
        raise RuntimeError(f"Unsupported service-manager platform: {sys.platform}")
    return {"required": True, "outcome": "pass", "manager": manager, "version": version,
            "foreground_fallback": False, "evidence": str(evidence / "service-manager-prerequisite.log")}


def assert_direct_service_ownership(root: Path, data: Path) -> dict:
    """Prove a direct leg selected a native service, never a foreground fallback."""
    selection_path = program_root().parent / "astrid-install.json"
    if selection_path.is_file() and read_json(selection_path).get("foreground"):
        raise RuntimeError("Direct journey selected a forbidden foreground fallback")
    if sys.platform == "linux":
        unit = data.parent / "home" / ".config" / "systemd" / "user" / "astrid.service"
        if not unit.is_file() or unit.is_symlink():
            raise RuntimeError("Direct journey did not install an owned systemd user unit")
        completed = subprocess.run(["systemctl", "--user", "show", "astrid.service",
                                    "--property=LoadState,FragmentPath,ActiveState"], capture_output=True,
                                   text=True, timeout=30, check=False)
        fields = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
        if (completed.returncode or fields.get("LoadState") == "not-found"
                or fields.get("FragmentPath") != str(unit)
                or fields.get("ActiveState") != "active"):
            raise RuntimeError("Direct journey service is not owned by the expected systemd user unit")
        return {"manager": "systemd-user", "service_path": str(unit), "foreground_fallback": False}
    if sys.platform == "darwin":
        plist = data.parent / "home" / "Library" / "LaunchAgents" / "haus.astrid.runtime.plist"
        if not plist.is_file() or plist.is_symlink():
            raise RuntimeError("Direct journey did not install an owned LaunchAgent")
        completed = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/haus.astrid.runtime"],
                                   capture_output=True, text=True, timeout=30, check=False)
        if completed.returncode:
            raise RuntimeError("Direct journey LaunchAgent is not loaded in the GUI domain")
        return {"manager": "launchd", "service_path": str(plist), "foreground_fallback": False}
    if sys.platform == "win32":
        existing = windows.powershell(
            "$t=Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop; "
            "'present'; $t.Actions | ForEach-Object { $_.Execute }",
            "direct-service-ownership.log", show=False, timeout=30)
        if not windows_task_owned(existing, root):
            raise RuntimeError("Direct journey Task Scheduler action is not owned by the run root")
        return {"manager": "task-scheduler", "foreground_fallback": False}
    raise RuntimeError(f"Unsupported service-manager platform: {sys.platform}")


def candidate_checkouts(args: argparse.Namespace) -> dict | None:
    """Record and verify all four immutable candidate checkouts when supplied."""
    candidate_path = getattr(args, "candidate", None)
    if candidate_path is None:
        return None
    candidate = read_json(candidate_path)
    if set(candidate) < {"app", "agent", "runtime", "pinokio"}:
        raise ValueError("candidate must include app, agent, runtime and pinokio refs")
    checkouts = {"app": args.app_checkout,
                 "agent": getattr(args, "candidate_agent_checkout", None),
                 "runtime": getattr(args, "candidate_runtime_checkout", None),
                 "pinokio": getattr(args, "pinokio_checkout", None)}
    observed = {}
    for name, checkout in checkouts.items():
        if checkout is None or not checkout.is_dir() or checkout.is_symlink():
            raise ValueError(f"candidate {name} checkout is required and must not be a symlink")
        source = git_source(checkout)
        if source["sha"] != candidate[name]:
            raise ValueError(f"candidate {name} checkout differs from frozen candidate")
        observed[name] = source
    return observed


def installation(root: Path, expected_data: Path) -> tuple[Path, Path, dict]:
    marker_path = root / "install.json"
    if marker_path.is_symlink():
        raise RuntimeError("Install marker cannot be a symlink")
    marker = read_json(marker_path)
    data = Path(marker["data_root"])
    if data != expected_data:
        raise RuntimeError("Selected data root is outside this journey")
    current_path = root / "current"
    if current_path.is_symlink():
        raise RuntimeError("Selected version identifier is not run-owned")
    current = current_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", current):
        raise RuntimeError("Selected version identifier is not run-owned")
    version_path = root / "versions" / current / "version.json"
    if not version_path.resolve().is_relative_to(root / "versions"):
        raise RuntimeError("Selected version path escapes this journey")
    version = read_json(version_path)
    if Path(version["root"]).resolve() != version_path.parent.resolve():
        raise RuntimeError("Selected version path disagrees with version.json")
    return version_path, data, version


def run(argv: list[str | Path], log: Path, *, env: dict | None = None, timeout: int = 3600) -> str:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as stream:
        result = subprocess.run([str(part) for part in argv], env=env, stdout=stream,
                                stderr=subprocess.STDOUT, timeout=timeout, check=False)
    output = log.read_text(encoding="utf-8-sig", errors="replace")
    if result.returncode:
        raise RuntimeError(f"{log.name}: exit {result.returncode}; see {log}")
    return output


def bootstrap(app: Path, manifest_path: Path, shell: str, mode: str,
              evidence: Path, root: Path, data: Path) -> None:
    flags: list[str | Path] = ["--manifest", manifest_path, "--root", root,
                               "--data-root", data]
    if mode == "update":
        flags.append("--update")
    env = dict(os.environ)
    env["BROWSER"] = f'"{sys.executable}" -c "pass" %s'
    if sys.platform == "win32":
        run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", app / "public" / "install.ps1", *flags], evidence / f"{mode}-bootstrap.log",
            env=env)
    else:
        run([shell, app / "public" / "install", *flags],
            evidence / f"{mode}-bootstrap.log", env=env)


def verify(mode: str, version_path: Path, data: Path, evidence: Path,
           *, before: Path | None = None) -> Path:
    output = evidence / f"verify-{mode}.json"
    argv: list[str | Path] = [sys.executable, ROOT / "scripts" / "verify_installed.py",
                              "--mode", mode, "--version", version_path,
                              "--data-root", data, "--output", output]
    if before is not None:
        argv += ["--before", before]
    run(argv, evidence / f"verify-{mode}.log", timeout=180)
    if not output.is_file():
        raise RuntimeError(f"Shared verifier wrote no receipt: {output}")
    return output


def installed_cli(version: dict, argv: list[str], evidence: Path, name: str,
                  *, module: str = "astrid", timeout: int = 300) -> str:
    env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
    env.update(version["env"])
    return run([version["python"], "-I", "-m", module, *argv], evidence / name,
               env=env, timeout=timeout)


def healthy(data: Path) -> dict:
    request = urllib.request.Request(HEALTH_URL, headers={"Origin": "https://astrid.haus"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        health = json.load(response)
    discovery = read_json(data / "runtime" / "discovery.json")
    if (health.get("status") != "ok" or not health.get("runtime_instance_id") or
            health["runtime_instance_id"] != discovery.get("runtime_instance_id")):
        raise RuntimeError("Live Runtime health does not match selected discovery")
    return discovery


def runtime_processes(data: Path) -> list[int]:
    """Count every live Runtime owner process on this isolated journey host."""
    if sys.platform == "win32":
        rows = json.loads(windows.powershell(
            "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
            "runtime-owner-processes.json", show=False, timeout=30))
        rows = rows if isinstance(rows, list) else [rows]
        return [int(row["ProcessId"]) for row in rows if
                "-m runtime_protocol start" in str(row.get("CommandLine") or "")]
    output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
    found = []
    for line in output.splitlines():
        pid, _, command = line.strip().partition(" ")
        if "-m runtime_protocol start" in command:
            found.append(int(pid))
    return found


def assert_one_runtime(data: Path) -> None:
    discovery = healthy(data)
    owners = runtime_processes(data)
    if owners != [int(discovery["pid"])]:
        raise RuntimeError(f"Expected exactly the selected Runtime owner {discovery['pid']}, found {owners}")


def wait_ready(data: Path, *, timeout: int = 900) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            healthy(data)
            return
        except (OSError, ValueError, KeyError, RuntimeError):
            if time.monotonic() >= deadline:
                raise RuntimeError("Installed Runtime did not become healthy")
            time.sleep(5)


def task(version: dict, evidence: Path, project: str, *, create_project: bool = True) -> str:
    """Exercise the installed SDK through a settled, materialized SFX output."""
    os.environ["ASTRID_HOST_EXECUTION_PROFILE"] = "installed"
    if create_project:
        created = json.loads(installed_cli(version, ["projects", "create", project, "--name", project,
                                                   "--json"], evidence, "project.json"))
        if created.get("ok") is not True:
            raise RuntimeError("Installed project creation failed")
    request = {"capability_id": "chiptune.sfx", "kind": "executor", "project": project,
               "inputs": {"kind": "blip", "variant": 1, "pitch": 0, "duration_s": 0.1}}
    invocation = json.loads(installed_cli(version, [json.dumps(request)], evidence,
                                          "sdk-invocation.json", module="astrid.omp_invoke"))
    refs = invocation.get("details", {}).get("astridTaskRefs", [])
    if len(refs) != 1 or not refs[0].get("task_id"):
        raise RuntimeError("Installed SDK did not admit exactly one task")
    task_id = refs[0]["task_id"]
    try:
        installed_cli(version, ["tasks", "follow", task_id, "--project", project,
                                "--poll-seconds", "2", "--timeout-seconds", "900", "--json"],
                      evidence, "task-follow.jsonl", timeout=960)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        state = json.loads(installed_cli(version, ["tasks", "show", task_id, "--project", project,
                                                 "--json"], evidence, "task-follow-failure.json"))
        if (state.get("data") or {}).get("state") not in TERMINAL:
            installed_cli(version, ["tasks", "cancel", task_id, "--project", project, "--json"],
                          evidence, "task-cancel.json")
            try:
                installed_cli(version, ["tasks", "follow", task_id, "--project", project,
                                        "--poll-seconds", "2", "--timeout-seconds", "300", "--json"],
                              evidence, "task-cancel-follow.jsonl", timeout=360)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
        final = json.loads(installed_cli(version, ["tasks", "show", task_id, "--project", project,
                                                 "--json"], evidence, "task-after-cancel.json"))
        if (final.get("data") or {}).get("state") not in TERMINAL:
            raise RuntimeError("Task remained nonterminal after cancellation") from exc
        raise RuntimeError(f"Task follow failed: {exc}") from exc
    settled = json.loads(installed_cli(version, ["tasks", "show", task_id, "--project", project,
                                               "--json"], evidence, "task-settled.json"))
    row = settled.get("data") or {}
    outputs = {item.get("name"): item for item in (row.get("result") or {}).get("outputs", [])}
    if (settled.get("ok") is not True or row.get("state") != "succeeded" or
            not {"sfx", "sfx_meta"} <= outputs.keys() or any(
                not re.fullmatch(r"sha256:[0-9a-f]{64}", outputs[name].get("digest", "")) or
                outputs[name].get("size", 0) <= 0 for name in ("sfx", "sfx_meta"))):
        raise RuntimeError("Task did not settle successfully with materialized outputs")
    return task_id


def workspace_root(data: Path) -> Path:
    discovery = read_json(data / "runtime" / "discovery.json")
    root = Path(discovery["realm_root"])
    if not root.is_dir() or root != root.resolve() or not root.is_relative_to(data.parent):
        raise RuntimeError("Selected workspace is missing")
    return root


def assert_content(version: dict, evidence: Path, project: str, marker: Path,
                   digest: str) -> None:
    if marker.is_symlink():
        raise RuntimeError("Workspace marker cannot be a symlink")
    if hashlib.sha256(marker.read_bytes()).hexdigest() != digest:
        raise RuntimeError("Workspace marker did not survive the install/update leg")
    shown = json.loads(installed_cli(version, ["projects", "show", project, "--json"],
                                    evidence, "project-after-leg.json"))
    if shown.get("ok") is not True:
        raise RuntimeError("Installed project content did not survive")


def selected_tuple(manifest: dict, version: dict) -> dict:
    actual = read_json(Path(version["root"]) / "manifest.json")
    for name in ("agent", "runtime"):
        if actual.get(name) != manifest.get(name):
            raise RuntimeError(f"Installed {name} source differs from selected manifest")
    return {"agent": actual["agent"]["sha"], "runtime": actual["runtime"]["sha"],
            "schema_digest": actual["runtime"].get("schema_digest"),
            "tools": {name: row.get("version") for name, row in actual.get("tools", {}).items()
                      if isinstance(row, dict) and row.get("version")}}


def _execute_owned(args: argparse.Namespace, root: Path, data: Path,
                   evidence: Path, sentinel: subprocess.Popen, birth: str) -> dict:
    if args.leg in VERSION_JUMP_LEGS:
        if __package__:
            from . import journey_version_jumps
        else:
            import journey_version_jumps
        return journey_version_jumps.execute_owned(args, root, data, evidence, sentinel, birth)
    evidence.mkdir(parents=True, exist_ok=True)
    result = {"schema_version": 2, "receipt_kind": "release1-installed-lifecycle",
              "job_id": getattr(args, "job_id", None), "mode": getattr(args, "mode", "direct"),
              "scenario": args.leg, "status": "not-run",
              "os_build": platform.platform(), "shell": args.shell,
              **runner_identity(),
              "sources": None,
              "candidate_checkouts": getattr(args, "candidate_checkouts", None),
              "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
              "source_override_test_mode": args.source_override,
              "retained_for_next_leg": getattr(args, "retain_for_next_leg", False),
              "installed_tuple": None, "import_origins": None,
              "run_roots": {"program": str(root), "data": str(data)},
              "sentinel": {"pid": sentinel.pid, "birth": birth},
              "checks": {"task_settled": "not-run", "workspace_content": "not-run",
                         "one_runtime": "not-run", "unrelated_sentinel": "not-run"}}
    result_path = evidence / "journey-result.json"
    write_json(result_path, result)
    try:
        if getattr(args, "job_id", None) and args.mode != "direct":
            # M4-3a owns the fail-closed driver/receipt boundary.  The native
            # Pinokio lifecycle adapters themselves land in M4-4a/M4-4b; do
            # not silently substitute the direct bootstrap while they are absent.
            raise RuntimeError(f"{args.mode} requires its dedicated Pinokio lifecycle adapter")
        app = args.app_checkout.resolve(strict=True)
        manifest_path, manifest = selected_manifest(
            app, source_override=args.source_override, agent=args.agent_checkout,
            runtime=args.runtime_checkout, evidence=evidence)
        result["sources"] = {"app": git_source(app), "agent": manifest.get("agent"),
                             "runtime": manifest.get("runtime")}
        old_version = old_data = old = None
        project = "windows-journey" if sys.platform == "win32" else "tst-b1-fresh-install"
        if args.leg != "fresh-install":
            old_version, old_data, old = installation(root, data)
            assert_one_runtime(data)
            if args.leg == "stale-update":
                old_manifest = read_json(Path(old["root"]) / "manifest.json")
                if all(old_manifest.get(name) == manifest.get(name) for name in ("agent", "runtime")):
                    raise RuntimeError("stale-update requires an installed Agent or Runtime distinct from the selected manifest")
            verify("observe", old_version, old_data, evidence)
            marker = workspace_root(old_data) / "tst-b1-workspace-marker.txt"
            if not marker.is_file():
                raise RuntimeError("Pre-existing journey workspace marker is missing")
            digest = hashlib.sha256(marker.read_bytes()).hexdigest()
        else:
            if (root / "current").exists():
                raise RuntimeError("fresh-install requires an uninstalled machine")
            marker = None
            digest = ""
        before_sentinel = sentinel_observation(sentinel, birth)
        result["sentinel"]["before_lifecycle"] = before_sentinel
        if not before_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel exited before the lifecycle operation")
        if args.leg == "reinstall":
            installed_cli(old, ["install", "--uninstall", "--root", str(root),
                                "--data-root", str(data)], evidence, "uninstall.log", timeout=1800)
        bootstrap(app, manifest_path, args.shell, "update" if args.leg == "stale-update" else "install",
                  evidence, root, data)
        after_sentinel = sentinel_observation(sentinel, birth)
        result["sentinel"]["after_lifecycle"] = after_sentinel
        if not after_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel did not survive the lifecycle operation")
        version_path, data, version = installation(root, data)
        if old_data is not None and data != old_data:
            raise RuntimeError("Installer changed the selected data root")
        if getattr(args, "job_id", None) and args.mode == "direct":
            result["service_manager"] = assert_direct_service_ownership(root, data)
        if sys.platform == "win32":
            windows.EVIDENCE = evidence
            windows.powershell("Start-ScheduledTask -TaskName 'Astrid Runtime' -TaskPath '\\'",
                               "start.log")
        wait_ready(data)
        result["installed_tuple"] = {"app": result["sources"]["app"]["sha"],
                                     **selected_tuple(manifest, version)}
        verify_mode = "update" if args.leg == "stale-update" else "install"
        try:
            verify(verify_mode, version_path, data, evidence,
                   before=evidence / "verify-observe.json" if args.leg == "stale-update" else None)
        finally:
            receipt_path = evidence / f"verify-{verify_mode}.json"
            if receipt_path.is_file():
                observation = read_json(receipt_path)
                result["import_origins"] = (observation.get("details") or {}).get("origin.imports")
        assert_one_runtime(data)
        result["checks"]["one_runtime"] = "pass"
        task_project = project if args.leg == "fresh-install" else f"tst-b1-{args.leg}"
        if sys.platform == "win32":
            windows.EVIDENCE = evidence
            windows.wait_for("pack-host is running with its current ready file", windows.host_ready)
            windows.snapshot("processes-host-ready.json")
            windows.task(task_project)
        else:
            task(version, evidence, task_project)
        result["checks"]["task_settled"] = "pass"
        if args.leg == "fresh-install":
            marker = workspace_root(data) / "tst-b1-workspace-marker.txt"
            marker.write_text("TST-B1 workspace content\n", encoding="utf-8")
            digest = hashlib.sha256(marker.read_bytes()).hexdigest()
        assert_content(version, evidence, project, marker, digest)
        result["checks"]["workspace_content"] = "pass"
        settled_sentinel = sentinel_observation(sentinel, birth)
        result["sentinel"]["at_settlement"] = settled_sentinel
        if not settled_sentinel["alive"]:
            raise RuntimeError("Unrelated sentinel exited before journey settlement")
        result["checks"]["unrelated_sentinel"] = "pass"
        result["status"] = "pass"
    except Exception as exc:
        result["status"] = "fail"
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        write_json(result_path, result)
    return result


def execute(args: argparse.Namespace) -> dict:
    run_root, root, data, evidence = run_scope(args.run_root, args.root, args.data_root, args.leg)
    windows.EVIDENCE = evidence
    identity = runner_identity()
    try:
        assert_runner_identity(identity)
        if getattr(args, "job_id", None):
            expected = validate_job(args.job_id, args.mode)
            if expected["runner"] != identity["runner_image"]:
                raise RuntimeError(f"Release-1 job {args.job_id} requires {expected['runner']}, not {identity['runner_image']}")
            if expected["shell"] != args.shell:
                raise RuntimeError(f"Release-1 job {args.job_id} requires shell {expected['shell']}")
            args.candidate_checkouts = candidate_checkouts(args)
            try:
                args.service_manager_prerequisite = release1_manager_prerequisite(args, evidence)
            except Exception as manager_error:
                args.service_manager_prerequisite = {
                    "required": args.mode == "direct", "outcome": "fail",
                    "foreground_fallback": False,
                    "error": f"{type(manager_error).__name__}: {manager_error}",
                }
                raise
        assert_host_available("fresh-install" if args.leg in VERSION_JUMP_LEGS else args.leg, root, data)
    except Exception as exc:
        write_json(evidence / "journey-result.json", {
            "schema_version": 2, "receipt_kind": "release1-installed-lifecycle",
            "job_id": getattr(args, "job_id", None), "mode": getattr(args, "mode", "direct"),
            "scenario": args.leg, "status": "fail",
            "os_build": platform.platform(), "shell": args.shell, **identity,
            "run_roots": {"program": str(root), "data": str(data)},
            "candidate_checkouts": getattr(args, "candidate_checkouts", None),
            "service_manager_prerequisite": getattr(args, "service_manager_prerequisite", None),
            "installed_tuple": None, "import_origins": None,
            "checks": {"task_settled": "not-run", "workspace_content": "not-run",
                       "one_runtime": "not-run", "unrelated_sentinel": "not-run"},
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
    with isolated_environment(run_root, root, data):
        with sentinel_process(run_root) as (sentinel, birth):
            try:
                result = _execute_owned(args, root, data, evidence, sentinel, birth)
            finally:
                if not getattr(args, "retain_for_next_leg", False):
                    result_path = evidence / "journey-result.json"
                    try:
                        if (root / "current").is_file():
                            _, _, installed = installation(root, data)
                            installed_cli(installed, ["install", "--uninstall", "--root", str(root),
                                                      "--data-root", str(data)], evidence,
                                          "final-uninstall.log", timeout=1800)
                            if (root / "current").exists() or runtime_processes(data):
                                raise RuntimeError("Owned installation remained after final uninstall")
                        cleanup_sentinel = sentinel_observation(sentinel, birth)
                        if result_path.is_file():
                            recorded = read_json(result_path)
                            recorded.setdefault("sentinel", {})["after_cleanup"] = cleanup_sentinel
                            write_json(result_path, recorded)
                        assert_host_available("cleanup", root, data)
                        if not cleanup_sentinel["alive"]:
                            raise RuntimeError("Unrelated sentinel did not survive final uninstall")
                        if result_path.is_file():
                            recorded = read_json(result_path)
                            recorded["final_cleanup"] = "pass"
                            write_json(result_path, recorded)
                        if "result" in locals():
                            result["final_cleanup"] = "pass"
                    except Exception as cleanup_error:
                        recorded = read_json(result_path) if result_path.is_file() else {
                            "schema_version": 1, "scenario": args.leg}
                        recorded.update(status="fail", final_cleanup="fail",
                                        cleanup_error=f"{type(cleanup_error).__name__}: {cleanup_error}")
                        write_json(result_path, recorded)
                        raise
                else:
                    result_path = evidence / "journey-result.json"
                    if result_path.is_file():
                        recorded = read_json(result_path)
                        recorded["final_cleanup"] = "retained-for-next-leg"
                        write_json(result_path, recorded)
                    if "result" in locals():
                        result["final_cleanup"] = "retained-for-next-leg"
            return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leg", choices=("fresh-install", "stale-update", "reinstall", *VERSION_JUMP_LEGS), required=True)
    parser.add_argument("--old-declaration", choices=("bare", "pruned"),
                        help="required for update-over-old-declaration; corrupt only the run-owned chat command")
    parser.add_argument("--app-checkout", type=Path, required=True)
    parser.add_argument("--run-root", type=Path, required=True,
                        help="fresh run-owned directory under the runner temporary directory")
    parser.add_argument("--root", type=Path, required=True, help="RUN_ROOT/program")
    parser.add_argument("--data-root", type=Path, required=True, help="RUN_ROOT/data")
    parser.add_argument("--shell", default="powershell.exe" if sys.platform == "win32" else "sh")
    parser.add_argument("--job-id", help="fixed Release-1 matrix job id; enables the fail-closed receipt contract")
    parser.add_argument("--mode", choices=RELEASE1_MODES, default="direct",
                        help="declared lifecycle owner; direct never permits foreground fallback")
    parser.add_argument("--candidate", type=Path,
                        help="frozen four-ref candidate JSON used to verify exact checkouts")
    parser.add_argument("--candidate-agent-checkout", type=Path)
    parser.add_argument("--candidate-runtime-checkout", type=Path)
    parser.add_argument("--pinokio-checkout", type=Path)
    parser.add_argument("--release0-fixture", type=Path,
                        help="the checked-in frozen release-0 fixture directory for the direct 0-to-1 leg")
    parser.add_argument("--allow-release0-to-release1", action="store_true",
                        help="admit only the exact frozen release-0 direct journey to release 1")
    parser.add_argument("--source-override", action="store_true", help="TEST MODE: install Agent and Runtime from local Git commits")
    parser.add_argument("--agent-checkout", type=Path)
    parser.add_argument("--runtime-checkout", type=Path)
    parser.add_argument("--retain-for-next-leg", action="store_true",
                        help="keep only this run's installed service for the next named leg")
    args = parser.parse_args(argv)
    if (args.leg == "update-over-old-declaration") != (args.old_declaration is not None):
        parser.error("--old-declaration is required only for update-over-old-declaration")
    if bool(args.job_id) != bool(args.candidate):
        parser.error("--job-id and --candidate are required together for Release-1 receipts")
    if args.job_id:
        try:
            validate_job(args.job_id, args.mode)
        except ValueError as exc:
            parser.error(str(exc))
        if not all((args.candidate_agent_checkout, args.candidate_runtime_checkout, args.pinokio_checkout)):
            parser.error("Release-1 receipts require candidate Agent, Runtime and Pinokio checkouts")
    if args.leg == "release0-to-release1":
        if not (args.job_id and args.mode == "direct" and args.candidate):
            parser.error("release0-to-release1 requires a frozen direct Release-1 receipt")
        if args.release0_fixture is None or not args.allow_release0_to_release1:
            parser.error("release0-to-release1 requires --release0-fixture and --allow-release0-to-release1")
    elif args.release0_fixture is not None or args.allow_release0_to_release1:
        parser.error("release-0 fixture admission is only valid for release0-to-release1")
    if sys.platform not in {"linux", "darwin", "win32"}:
        parser.error("unsupported journey operating system")
    if sys.platform == "win32" and args.shell not in {"powershell.exe", "pwsh.exe"}:
        parser.error("Windows requires powershell.exe or pwsh.exe")
    if sys.platform != "win32" and args.shell != "sh":
        parser.error("Linux/macOS require sh")
    try:
        with without_provider_credentials():
            execute(args)
    except Exception as exc:
        print(f"Journey failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
