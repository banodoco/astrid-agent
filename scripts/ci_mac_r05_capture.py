#!/usr/bin/env python3
"""Run a stopped-owner R0-to-R1 test in a runner-owned sandbox."""
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UID = 503
REFS = {"driver": ("astrid-agent", "997d8c3d95971170b812b8c415fbbf5c1656edd0"),
        "agent": ("astrid-agent", "ce78f3586b02ca2d5526760a062c6240d28da7db"),
        "runtime": ("astrid-runtime", "f27ff70126958d1736a83b606df5126acf268bf5")}



def owner_match(command):
    return ("-m runtime_protocol start" in command or
            (re.search(r"(?:^|\s)-c(?:\s|$)", command) is not None
             and "from astrid_runtime.cli import main" in command
             and re.search(r"main\(\s*\[\s*['\"]run['\"]", command) is not None))


def host_owners_exist():
    result = subprocess.run(["/bin/ps", "-ww", "-u", str(UID), "-o", "pid=,command="], capture_output=True,
                            text=True, timeout=20, check=True)
    records = []
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if match: records.append(match.group(2))
        elif records: records[-1] += "\n" + line
    return any(owner_match(command) for command in records)


def service_absent():
    out = subprocess.run(["/bin/launchctl", "print", "gui/503/haus.astrid.runtime"],
                         capture_output=True, text=True, timeout=20)
    missing = 'Could not find service "haus.astrid.runtime" in domain for user gui: 503'
    return out.returncode == 113 and out.stderr.strip().replace("\r\n", "\n") in {missing, "Bad request.\n" + missing}


def port_free():
    """Require lsof to prove no TCP listener on the default Runtime port."""
    result = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:17340", "-sTCP:LISTEN"],
                            capture_output=True, text=True, timeout=20)
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def guard_host(home):
    # The R3 contract explicitly asks for the port observation first.
    if not port_free():
        raise RuntimeError("Default Runtime port is occupied or cannot be verified; no installation attempted")
    if not service_absent():
        raise RuntimeError("Runner Runtime service present or absence unverified")
    plist = home / "Library/LaunchAgents/haus.astrid.runtime.plist"
    if plist.exists() or plist.is_symlink():
        raise RuntimeError("Runner Runtime plist already exists")
    
    if host_owners_exist():
        raise RuntimeError("Host has existing Runtime owner; no installation attempted")
    if shutil.disk_usage(home).free < 10_000_000_000:
        raise RuntimeError("Insufficient disk")


def safe_error(exc):
    known = {"Insufficient disk", "Runner Runtime service present or absence unverified",
             "Runner Runtime plist already exists", "Host has existing Runtime owner; no installation attempted",
             "Default Runtime port is occupied or cannot be verified; no installation attempted", "Runner-owned Python is unavailable",
             "Runner Python version differs", "Fresh capture root is unsafe", "Insufficient disk before source operation",
             "Source checkout identity differs", "Frozen fixture hashes differ", "Receipt runner image is not local"}
    text = str(exc)
    source_error = re.fullmatch(r"Source operation failed: (driver|release0-agent|release0-runtime)/(clone|fetch|checkout)", text)
    return text if isinstance(exc, RuntimeError) and (text in known or source_error) else "Unexpected operation failure; inspect runner-owned private evidence"


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""): h.update(chunk)
    return h.hexdigest()


def environment(home, root, py):
    env = {key: os.environ[key] for key in ("RUNNER_OS", "RUNNER_NAME", "RUNNER_ENVIRONMENT", "CI", "GITHUB_ACTIONS") if key in os.environ}
    env.update(HOME=str(home), RUNNER_TEMP=str(root), PATH=":".join((str(py.parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin")),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM="/dev/null", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1", GIT_NO_REPLACE_OBJECTS="1", PIP_CONFIG_FILE="/dev/null")
    return env


def stop_job_children(driver_pid):
    listed = subprocess.run(["/bin/ps", "-axo", "uid=,pid=,pgid=,stat="], capture_output=True,
                            text=True, timeout=10, check=True)
    sent = []
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 4: raise RuntimeError("Process census malformed")
        uid, pid, pgid = map(int, fields[:3])
        if uid == UID and pgid == driver_pid and pid != driver_pid and not fields[3].startswith("Z"):
            try:
                if os.getpgid(pid) == driver_pid:
                    os.kill(pid, signal.SIGTERM); sent.append(pid)
            except ProcessLookupError: pass
    return sent


def run_process(argv, cwd, env, logfile, home, is_driver=False):
    with logfile.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        abort = False; census_failed = False; grace_expired = False; started = None
        while proc.poll() is None:
            if not abort and shutil.disk_usage(home).free < 3_000_000_000:
                if is_driver:
                    try: stop_job_children(proc.pid)
                    except Exception: census_failed = True
                else:
                    try: os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError: pass
                abort = True; started = time.monotonic()
            elif abort and not grace_expired and time.monotonic() - started >= 300:
                # Do not force-kill an unknown Runtime or interrupt cleanup again.
                grace_expired = True
            time.sleep(2)
    return {"exit": proc.returncode, "disk_threshold_crossed": abort,
            "process_census_failed": census_failed, "cleanup_grace_expired": grace_expired}


def redact(data, home, root):
    text = data.decode("utf-8", errors="replace")
    text = text.replace(str(root), "<CAPTURE_ROOT>").replace(str(home), "<RUNNER_HOME>")
    text = re.sub(r"/Users/[^/\s\"']+", "<REDACTED_HOME>", text)
    text = re.sub(r"/private/var/folders/[^\s\"']+", "<MAC_TEMP>", text)
    text = re.sub(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)", "<REDACTED_TOKEN>", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+", r"\1<REDACTED>", text)
    text = re.sub(r'(?i)(["\'](?:token|api[_-]?key|secret|password)["\']\s*:\s*["\'])[^"\']*', r'\1<REDACTED>', text)
    return text.encode("utf-8")


def export_capture(root, home, output, status):
    original = root / "evidence" if root is not None else None
    source_rows = []
    if original is not None and original.is_dir() and not original.is_symlink():
        for source in sorted(original.rglob("*")):
            if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(original): continue
            name = source.relative_to(original)
            if name.name == "SHA256SUMS": continue
            if source.stat().st_size > 16_000_000: raise RuntimeError("Evidence exceeds safe export limit")
            data = source.read_bytes()
            dest = output / "capture-upload" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(redact(data, home, root))
            source_rows.append(hashlib.sha256(data).hexdigest() + "  " + (Path("capture-upload") / name).as_posix())
    if root is not None:
        private_log = root / "private-logs/capture-private.log"
        if private_log.is_file() and not private_log.is_symlink():
            data = private_log.read_bytes()[-1_000_000:]
            (output / "driver.log").write_bytes(redact(data, home, root))
    (output / "SOURCE-SHA256SUMS").write_text("\n".join(source_rows) + ("\n" if source_rows else ""))
    (output / "custody.json").write_text(json.dumps(status, indent=2, sort_keys=True) + "\n")
    rows=[]
    for path in sorted(output.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "SHA256SUMS":
            rows.append(digest(path) + "  " + path.relative_to(output).as_posix())
    (output / "SHA256SUMS").write_text("\n".join(rows) + "\n")


def main():
    if os.getuid() != UID or os.environ.get("RUNNER_NAME") != "hannah-mac" or sys.platform != "darwin":
        raise RuntimeError("Runner identity is not the dedicated macOS runner")
    home = Path(pwd.getpwuid(UID).pw_dir)
    root = None
    runner_temp = Path(os.environ["RUNNER_TEMP"])
    if (runner_temp != home / "actions-runner/_work/_temp" or runner_temp.resolve() != runner_temp
            or not runner_temp.is_dir() or runner_temp.is_symlink() or runner_temp.stat().st_uid != UID):
        raise RuntimeError("Runner action temp is unexpected")
    output = runner_temp / "r05-artifact"
    output.mkdir(mode=0o700)
    with open(os.environ["GITHUB_ENV"], "a") as fh: fh.write("R05_UPLOAD=" + str(output) + "\n")
    status = {"status": "not-run", "runner_custody": "hannah-mac; macOS self-hosted diagnostic", "qualification": False,
              "expected_runner_image": "local", "refs": {name: ref for name, (_, ref) in REFS.items()}, "stages": {}}
    try:
        guard_host(home)
        # Do not leave a task root behind when host preflight refuses.
        preserve = home / ".astrid-r1fix-stopped"
        preserve.mkdir(mode=0o700, exist_ok=True)
        if preserve.is_symlink() or preserve.resolve() != preserve or preserve.stat().st_uid != UID or stat.S_IMODE(preserve.stat().st_mode) != 0o700:
            raise RuntimeError("Fresh capture root is unsafe")
        root = Path(tempfile.mkdtemp(prefix="task.", dir=preserve))
        if root.is_symlink() or root.resolve() != root or root.stat().st_uid != UID or stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise RuntimeError("Fresh capture root is unsafe")
        pyroot = home / "actions-runner/_work/_tool/Python/3.11.18/arm64"
        py = pyroot / "bin/python3.11"
        if (pyroot.is_symlink() or pyroot.resolve() != pyroot or not py.is_file()
                or pyroot.stat().st_uid != UID or not py.resolve().is_relative_to(pyroot) or py.stat().st_uid != UID):
            raise RuntimeError("Runner-owned Python is unavailable")
        with open(os.environ["GITHUB_ENV"], "a") as fh: fh.write("WS02_TASK_ROOT=" + str(root) + "\n")
        env = environment(home, root, py)
        version = subprocess.check_output([str(py), "-I", "-c", "import platform; print(platform.python_version())"], env=env, text=True, timeout=20).strip()
        if version != "3.11.18": raise RuntimeError("Runner Python version differs")
        status["interpreter"] = "runner-owned Python " + version
        logs = root / "private-logs"; logs.mkdir(mode=0o700)
        for name, (repo, sha) in REFS.items():
            for step, args in (("clone", ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "clone", "--depth=1", "--filter=blob:none", "--no-checkout", "https://github.com/banodoco/" + repo + ".git", name]),
                               ("fetch", ["/usr/bin/git", "-C", name, "-c", "core.hooksPath=/dev/null", "fetch", "--depth=1", "origin", sha]),
                               ("checkout", ["/usr/bin/git", "-C", name, "-c", "core.hooksPath=/dev/null", "checkout", "--detach", sha])):
                if shutil.disk_usage(home).free < 10_000_000_000: raise RuntimeError("Insufficient disk before source operation")
                result = run_process(args, root, env, logs / (step + "-" + name + ".log"), home)
                if result["exit"] != 0: raise RuntimeError("Source operation failed: " + name + "/" + step)
            head = subprocess.check_output(["/usr/bin/git", "-C", str(root / name), "rev-parse", "HEAD"], env=env, text=True, timeout=20).strip()
            if head != sha: raise RuntimeError("Source checkout identity differs")
            status["stages"]["checkout-" + name] = "pass"
        fixture = root / "agent/tests/fixtures/release0"
        checked = run_process(["/usr/bin/shasum", "-a", "256", "-c", "SHA256SUMS"], fixture, env, logs / "fixture-check.log", home)
        if checked["exit"] != 0: raise RuntimeError("Frozen fixture hashes differ")
        status["fixture_sha256sums"] = digest(fixture / "SHA256SUMS")
        status["stages"]["fixture-check"] = "pass"
        # Recheck service, global owner census and fixed port immediately before execution.
        guard_host(home)
        inner = root / "diagnostic.py"
        inner.write_text('import fcntl, hashlib, json, os, plistlib, re, subprocess, sys, time, traceback, uuid\nfrom pathlib import Path\nbase=Path(sys.argv[1]); driver=base/\'driver\'; app=base/\'candidate-app\'; run=base/\'run\'; data=run/\'data\'; home=run/\'home\'; root=home/\'.local/share/astrid\'; evidence=base/\'evidence\'; evidence.mkdir()\nsys.path.insert(0,str(driver/\'scripts\'))\nimport ci_journey as j\nimport journey_version_jumps as v\ndef runner_owners(data):\n p=subprocess.run([\'/bin/ps\',\'-ww\',\'-u\',str(os.getuid()),\'-o\',\'pid=,command=\'],capture_output=True,text=True,timeout=20,check=True)\n rows=[]\n for line in p.stdout.splitlines():\n  match=re.match(r\'^\\s*(\\d+)\\s+(.*)$\',line)\n  if match: rows.append([int(match.group(1)),match.group(2)])\n  elif rows: rows[-1][1]+=\'\\n\'+line\n return [pid for pid,cmd in rows if j._runtime_owner_command(cmd)]\nj.runtime_processes=runner_owners\nrun.mkdir(mode=0o700)\nwith (run/\'tst-b1-owner.json\').open(\'x\',encoding=\'utf-8\') as owner:\n json.dump({\'schema_version\':1,\'run_id\':uuid.uuid4().hex,\'root\':str(root),\'data_root\':str(data)},owner); owner.write(\'\\n\')\nstatus={\'stages\':{},\'outcome\':\'not-run\',\'cleanup\':\'unverified\'}\ndef done(name):\n status[\'stages\'][name]=\'pass\'; print(\'STAGE \'+name+\' pass\',flush=True)\ndef save(): (evidence/\'result.json\').write_text(json.dumps(status,indent=2,sort_keys=True)+\'\\n\')\ndef prove_owned():\n service=j.assert_direct_service_ownership(root,data)\n plist=Path(service[\'service_path\']); spec=plistlib.loads(plist.read_bytes()); args=spec.get(\'ProgramArguments\') or []\n if not isinstance(args,list) or not any(str(root) in str(x) for x in args) or not any(str(data) in str(x) for x in args): raise RuntimeError(\'LaunchAgent program or data root mismatch\')\n p=subprocess.run([\'/bin/launchctl\',\'print\',f\'gui/{os.getuid()}/haus.astrid.runtime\'],capture_output=True,text=True,timeout=30,check=True)\n loaded_path=re.search(r\'(?m)^\\s*path\\s*=\\s*(.+?)\\s*$\',p.stdout)\n if loaded_path is None or loaded_path.group(1)!=str(plist): raise RuntimeError(\'Loaded LaunchAgent plist mismatch\')\n health=j.healthy(data); pid=int(health[\'pid\']); birth=j.process_birth(pid)\n if not birth or not re.search(r\'(?m)^\\s*pid\\s*=\\s*\'+str(pid)+r\'\\s*$\',p.stdout): raise RuntimeError(\'Loaded service PID differs from healthy owner\')\n if j.runtime_processes(data)!=[pid]: raise RuntimeError(\'Runner owner census differs\')\n return pid,birth,plist\n\ndef stop_owned():\n pid,birth,plist=prove_owned(); realm=j.workspace_root(data)\n j.run([\'/bin/launchctl\',\'bootout\',f\'gui/{os.getuid()}/haus.astrid.runtime\'],evidence/\'stop.log\',timeout=120)\n deadline=time.monotonic()+120\n while j.process_birth(pid)==birth:\n  if time.monotonic()>deadline: raise RuntimeError(\'Stopped owner did not exit\')\n  time.sleep(1)\n lock=realm/\'owner.lock\'\n if lock.is_symlink() or not lock.is_file() or not lock.resolve().is_relative_to(data): raise RuntimeError(\'Realm owner lock identity not proven\')\n fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)\n try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); fcntl.flock(fd,fcntl.LOCK_UN)\n finally: os.close(fd)\n return realm\n\ndisabled_by_us=False\nr1=run/\'r1-data\'; realm=None; realm_id=None\ntry:\n fixture, oldmanifest, receipt=v.verified_release0_fixture(base/\'agent/tests/fixtures/release0\'); done(\'r0-fixture-verification\')\n with j.isolated_environment(run,root,data):\n  v.bootstrap_release0(fixture,oldmanifest,\'sh\',evidence/\'release0\',root,data); done(\'r0-install\')\n  j.wait_ready(data); oldpath,_,old=j.installation(root,data)\n  if v.installed_release(old)!=0: raise RuntimeError(\'Installed release is not R0\')\n  prove_owned(); done(\'r0-service-identity\')\n  keys=v.direct_workspace(root,old,data,evidence/\'seed\',\'seed\',{\'project\':\'ws02-r0-workspace\',\'generation\':\'release0\',\'task_id\':None,\'create_project\':True}); done(\'seed\')\n  health=j.healthy(data); realm_id=str(health[\'realm_id\']); realm=j.workspace_root(data)\n  if not re.fullmatch(r\'[A-Za-z0-9_-]{1,100}\',realm_id): raise RuntimeError(\'Unexpected realm id\')\n  prove_owned()\n  query=subprocess.run([\'/bin/launchctl\',\'print-disabled\',f\'gui/{os.getuid()}\'],capture_output=True,text=True,check=True,timeout=30)\n  if \'disabled services\' not in query.stdout: raise RuntimeError(\'LaunchAgent disabled state not inspectable\')\n  found=re.search(r\'(?m)^\\s*"haus\\.astrid\\.runtime"\\s*=>\\s*(\\w+)\\s*$\',query.stdout)\n  if found and found.group(1) not in (\'enabled\',\'false\'): raise RuntimeError(\'LaunchAgent was previously disabled or state unknown\')\n  disabled_by_us=True\n  subprocess.run([\'/bin/launchctl\',\'disable\',f\'gui/{os.getuid()}/haus.astrid.runtime\'],check=True,timeout=30); done(\'launchagent-disable\')\n  stop_owned(); done(\'r0-stopped-and-lock-free\')\n  venv=base/\'r1-venv\'; py=Path(sys.executable); j.run([py,\'-m\',\'venv\',venv],evidence/\'venv.log\',timeout=300); interp=venv/\'bin/python\'\n  j.run([interp,\'-m\',\'pip\',\'install\',\'-e\',base/\'runtime\',\'-e\',base/\'runtime/packages/python\',\'-e\',base/\'agent\'],evidence/\'r1-install.log\',timeout=1200); done(\'r1-runtime-install\')\n  profile=base/\'r1-profile.json\'; profile.write_text(json.dumps({\'profile\':\'astrid\',\'runtime_checkout\':str(base/\'runtime\'),\'source_checkout\':str(base/\'agent\'),\'runtime_environment\':str(venv)}))\n  os.chmod(profile,0o600); env=dict(os.environ); env[\'ASTRID_RUNTIME_SOURCE_MANIFEST\']=str(profile)\n  # Use an unoccupied, non-default port for the task-owned R1 door.\n  import socket\n  with socket.socket() as sock: sock.bind((\'127.0.0.1\',0)); port=sock.getsockname()[1]\n  if port==17340: raise RuntimeError(\'Unexpected R1 door port\')\n  env[\'ASTRID_RUNTIME_DOOR_PORT\']=str(port)\n  cli=venv/\'bin/astrid-runtime\'\n  def command(name,args):\n   result=subprocess.run([str(cli),*args],env=env,cwd=str(base/\'runtime\'),capture_output=True,text=True,timeout=1200)\n   (evidence/(name+\'.stdout.json\')).write_text(result.stdout); (evidence/(name+\'.stderr.txt\')).write_text(result.stderr)\n   status[\'stages\'][name]={\'exit\':result.returncode}; save(); return result\n  adopt=command(\'adopt\',[\'workspace\',\'adopt\',\'--data-root\',str(r1),\'--from\',str(realm),\'--realm-id\',realm_id,\'--json\'])\n  if adopt.returncode==0: done(\'adopt-complete\')\n  target=r1/\'runtime/realms\'/realm_id\n  if target.is_dir() and not target.is_symlink():\n   inspect=command(\'inspect\',[\'workspace\',\'inspect\',\'--data-root\',str(r1),\'--realm-root\',str(target),\'--json\'])\n   def cas_map(path):\n    result={}; rootcas=path/\'cas/sha256\'\n    for item in rootcas.rglob(\'*\') if rootcas.exists() else ():\n     if item.is_symlink(): raise RuntimeError(\'CAS contains symlink\')\n     if item.is_file(): result[item.relative_to(rootcas).as_posix()]=hashlib.sha256(item.read_bytes()).hexdigest()\n    return result\n   before=cas_map(realm); after=cas_map(target); status[\'cas\']={\'source_count\':len(before),\'target_count\':len(after),\'identical\':before==after}; save()\n  else: status[\'stages\'][\'inspect\']=\'not-run-target-missing\'; status[\'cas\']=\'not-run-target-missing\'\n  status[\'outcome\']=\'pass\' if adopt.returncode==0 and status[\'stages\'].get(\'inspect\',{}).get(\'exit\')==0 and isinstance(status.get(\'cas\'),dict) and status[\'cas\'][\'identical\'] and status[\'cas\'][\'source_count\']>0 else \'failed\'\nexcept Exception as exc:\n status[\'outcome\']=\'failed\'; status[\'error_type\']=type(exc).__name__; status[\'error\']=str(exc); traceback.print_exc()\nfinally:\n try:\n  discovery=r1/\'runtime/discovery.json\'\n  if discovery.is_file() and not discovery.is_symlink():\n   value=json.loads(discovery.read_text()); pid=int(value[\'pid\']); birth=j.process_birth(pid)\n   if birth:\n    ps=subprocess.run([\'/bin/ps\',\'-ww\',\'-p\',str(pid),\'-o\',\'uid=,command=\'],capture_output=True,text=True,check=True,timeout=20).stdout.strip()\n    if not ps.startswith(str(os.getuid())+\' \') or str(r1) not in ps: raise RuntimeError(\'R1 owner identity not proven\')\n    result=subprocess.run([str(base/\'r1-venv/bin/astrid-runtime\'),\'down\',\'--data-root\',str(r1),\'--json\'],capture_output=True,text=True,timeout=180,env=env)\n    (evidence/\'r1-down.stdout.json\').write_text(result.stdout); (evidence/\'r1-down.stderr.txt\').write_text(result.stderr)\n    if result.returncode!=0: raise RuntimeError(\'R1 graceful down failed\')\n    end=time.monotonic()+120\n    while j.process_birth(pid)==birth:\n     if time.monotonic()>end: raise RuntimeError(\'R1 owner did not exit\')\n     time.sleep(1)\n  state=subprocess.run([\'/bin/launchctl\',\'print\',f\'gui/{os.getuid()}/haus.astrid.runtime\'],capture_output=True,text=True,timeout=20)\n  if state.returncode==0:\n   with j.isolated_environment(run,root,data): stop_owned()\n   state=subprocess.run([\'/bin/launchctl\',\'print\',f\'gui/{os.getuid()}/haus.astrid.runtime\'],capture_output=True,text=True,timeout=20)\n  missing=\'Could not find service "haus.astrid.runtime" in domain for user gui: \'+str(os.getuid())\n  if state.returncode!=113 or state.stderr.strip().replace(\'\\r\\n\',\'\\n\') not in (missing,\'Bad request.\\n\'+missing): raise RuntimeError(\'Service absence unverified\')\n  if disabled_by_us:\n   subprocess.run([\'/bin/launchctl\',\'enable\',f\'gui/{os.getuid()}/haus.astrid.runtime\'],check=True,timeout=30); disabled_by_us=False\n  if runner_owners(data): raise RuntimeError(\'Runner Runtime owner still present\')\n  status[\'cleanup\']=\'task-owned services stopped; launchd state restored\'\n except Exception as exc:\n  status[\'cleanup\']=\'unverified\'; status[\'cleanup_error\']=str(exc)\n  if disabled_by_us:\n   try:\n    current=subprocess.run([\'/bin/launchctl\',\'print\',f\'gui/{os.getuid()}/haus.astrid.runtime\'],capture_output=True,text=True,timeout=20)\n    missing=\'Could not find service \\"haus.astrid.runtime\\" in domain for user gui: \'+str(os.getuid())\n    if current.returncode!=113 or current.stderr.strip().replace(\'\\r\\n\',\'\\n\') not in (missing,\'Bad request.\\n\'+missing): raise RuntimeError(\'LaunchAgent absence unverified\')\n    subprocess.run([\'/bin/launchctl\',\'enable\',f\'gui/{os.getuid()}/haus.astrid.runtime\'],check=True,timeout=30); disabled_by_us=False; status[\'launchd_restore\']=\'enabled\'\n   except Exception as restore_exc: status[\'launchd_restore\']=\'unverified\'; status[\'launchd_restore_error\']=str(restore_exc)\n save()\n')
        env.pop("RUNNER_TEMP", None)
        env["ASTRID_CI_EXPECTED_RUNNER"] = "local"
        status["inner"] = run_process([str(py), "-u", str(inner), str(root)], root, env, logs / "capture-private.log", home, is_driver=True)
        status["status"] = "diagnostic-exited"
        receipt = root / "evidence/result.json"
        if receipt.is_file() and not receipt.is_symlink():
            value = json.loads(receipt.read_text())
            status["receipt"] = {"outcome": value.get("outcome"), "stages": value.get("stages"), "cleanup": value.get("cleanup"), "cas": value.get("cas")}

    except Exception as exc:
        status["error"] = safe_error(exc)
        status["error_type"] = type(exc).__name__
    finally:
        try: status["service_absent_after"] = service_absent()
        except Exception: status["service_absent_after"] = "unverified"
        status["disk_free_after"] = shutil.disk_usage(home).free
        export_capture(root, home, output, status)
        print(json.dumps({"status": status["status"], "receipt": status.get("receipt"), "service_absent_after": status["service_absent_after"]}))
    if status["status"] != "diagnostic-exited" or status["inner"]["exit"] != 0 or status.get("receipt", {}).get("outcome") != "pass": raise SystemExit(1)


if __name__ == "__main__":
    try: main()
    except Exception:
        print(json.dumps({"status": "preflight-failed"}))
        raise SystemExit(1)
