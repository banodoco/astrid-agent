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
        "agent": ("astrid-agent", "a052829cfe2f3725b1c2cf608974c10faf8eb99f"),
        "runtime": ("astrid-runtime", "9fc53b771134940aaac901e4cfc5d03c1db1a20a"),
        "app": ("astrid-app", "aacad4a59464a110b0e9875d4b2ae8ca3de92a57")}



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
    
    r1plist = home / "Library/LaunchAgents/haus.astrid.runtime.r1.plist"
    if r1plist.exists() or r1plist.is_symlink(): raise RuntimeError("Runner R1 plist already exists")
    r1status = subprocess.run(["/bin/launchctl", "print", "gui/503/haus.astrid.runtime.r1"], capture_output=True, text=True, timeout=20)
    missing = 'Could not find service "haus.astrid.runtime.r1" in domain for user gui: 503'
    if r1status.returncode != 113 or r1status.stderr.strip().replace("\r\n", "\n") not in {missing, "Bad request.\n" + missing}: raise RuntimeError("Runner R1 service present or absence unverified")
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
        inner.write_text('import fcntl,hashlib,json,os,plistlib,re,sqlite3,subprocess,sys,time,traceback,uuid\nfrom pathlib import Path\nbase=Path(sys.argv[1]); driver=base/\'driver\'; run=base/\'run\'; data=run/\'data\'; home=run/\'home\'; root=home/\'.local/share/astrid\'; evidence=base/\'evidence\'; evidence.mkdir(); run.mkdir(mode=0o700)\nsys.path.insert(0,str(driver/\'scripts\'))\nimport ci_journey as j\nimport journey_version_jumps as v\ndef runner_owners(data):\n p=subprocess.run([\'/bin/ps\',\'-ww\',\'-u\',str(os.getuid()),\'-o\',\'pid=,command=\'],capture_output=True,text=True,timeout=20,check=True)\n rows=[]\n for line in p.stdout.splitlines():\n  m=re.match(r\'^\\s*(\\d+)\\s+(.*)$\',line)\n  if m: rows.append([int(m.group(1)),m.group(2)])\n  elif rows: rows[-1][1]+=\'\\n\'+line\n return [pid for pid,cmd in rows if j._runtime_owner_command(cmd)]\nj.runtime_processes=runner_owners\nwith (run/\'tst-b1-owner.json\').open(\'x\',encoding=\'utf-8\') as owner: json.dump({\'schema_version\':1,\'run_id\':uuid.uuid4().hex,\'root\':str(root),\'data_root\':str(data)},owner)\nstatus={\'stages\':{},\'outcome\':\'not-run\',\'cleanup\':\'unverified\'}\ndef save(): (evidence/\'result.json\').write_text(json.dumps(status,indent=2,sort_keys=True)+\'\\n\')\ndef done(name): status[\'stages\'][name]=\'pass\'; save(); print(\'STAGE \'+name+\' pass\',flush=True)\ndef absent(label):\n p=subprocess.run([\'/bin/launchctl\',\'print\',f\'gui/{os.getuid()}/{label}\'],capture_output=True,text=True,timeout=20)\n missing=f\'Could not find service "{label}" in domain for user gui: {os.getuid()}\'\n return p.returncode==113 and p.stderr.strip().replace(\'\\r\\n\',\'\\n\') in (missing,\'Bad request.\\n\'+missing)\ndef label_states():\n p=subprocess.run([\'/bin/launchctl\',\'print-disabled\',f\'gui/{os.getuid()}\'],capture_output=True,text=True,check=True,timeout=20)\n if \'disabled services\' not in p.stdout: raise RuntimeError(\'Launchd state unavailable\')\n result={}\n for label in (\'haus.astrid.runtime\',\'haus.astrid.runtime.r1\'):\n  m=re.search(r\'(?m)^\\s*"\'+re.escape(label)+r\'"\\s*=>\\s*(\\w+)\\s*$\',p.stdout); result[label]=m.group(1) if m else \'not-listed\'\n return result\ndef prove(label,service_data):\n plist=home/\'Library/LaunchAgents\'/(label+\'.plist\')\n if plist.is_symlink() or not plist.is_file() or not plist.resolve().is_relative_to(home): raise RuntimeError(\'Owned plist unavailable\')\n spec=plistlib.loads(plist.read_bytes()); args=spec.get(\'ProgramArguments\') or []\n if spec.get(\'Label\')!=label or not isinstance(args,list) or not any(str(root) in str(x) for x in args) or not any(str(service_data) in str(x) for x in args): raise RuntimeError(\'Service action differs from owned roots\')\n p=subprocess.run([\'/bin/launchctl\',\'print\',f\'gui/{os.getuid()}/{label}\'],capture_output=True,text=True,check=True,timeout=20)\n loaded=re.search(r\'(?m)^\\s*path\\s*=\\s*(.+?)\\s*$\',p.stdout)\n if loaded is None or loaded.group(1)!=str(plist): raise RuntimeError(\'Loaded service path differs\')\n discovery=j.healthy(service_data); pid=int(discovery[\'pid\']); birth=j.process_birth(pid)\n if not birth or not re.search(r\'(?m)^\\s*pid\\s*=\\s*\'+str(pid)+r\'\\s*$\',p.stdout): raise RuntimeError(\'Loaded service PID differs\')\n ps=subprocess.run([\'/bin/ps\',\'-ww\',\'-p\',str(pid),\'-o\',\'uid=,command=\'],capture_output=True,text=True,timeout=20,check=True).stdout.strip()\n if not ps.startswith(str(os.getuid())+\' \') or str(service_data) not in ps: raise RuntimeError(\'Service process identity differs\')\n return pid,birth,j.workspace_root(service_data)\ndef lock_free(realm):\n lock=realm/\'owner.lock\'\n if lock.is_symlink() or not lock.is_file() or not lock.resolve().is_relative_to(run): raise RuntimeError(\'Realm lock identity unsafe\')\n fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)\n try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); fcntl.flock(fd,fcntl.LOCK_UN)\n finally: os.close(fd)\ndef stop_owned(label,service_data):\n pid,birth,realm=prove(label,service_data)\n status[\'stages\'][label+\'-disable-intent\']=\'recorded\'; save()\n subprocess.run([\'/bin/launchctl\',\'disable\',f\'gui/{os.getuid()}/{label}\'],check=True,timeout=30)\n subprocess.run([\'/bin/launchctl\',\'bootout\',f\'gui/{os.getuid()}/{label}\'],check=True,timeout=120)\n deadline=time.monotonic()+120\n while j.process_birth(pid)==birth:\n  if time.monotonic()>deadline: raise RuntimeError(\'Task owner did not exit\')\n  time.sleep(1)\n lock_free(realm)\n if not absent(label): raise RuntimeError(\'Task service remains loaded\')\n return realm\ndef tree(path,depth=0):\n rows=[]; entries=sorted(path.iterdir())\n if len(entries)>2000: raise RuntimeError(\'Program tree exceeds bound\')\n for item in entries:\n  row={\'name\':item.name,\'kind\':\'symlink\' if item.is_symlink() else \'directory\' if item.is_dir() else \'file\' if item.is_file() else \'other\'}\n  if row[\'kind\']==\'directory\' and depth<1: row[\'children\']=tree(item,depth+1)\n  rows.append(row)\n return rows\ndef filemap(path):\n result={}\n for item in path.rglob(\'*\'):\n  if item.is_symlink(): raise RuntimeError(\'Source contains symlink\')\n  if item.is_file():\n   digest=hashlib.sha256()\n   with item.open(\'rb\') as f:\n    for chunk in iter(lambda:f.read(1024*1024),b\'\'): digest.update(chunk)\n   result[item.relative_to(path).as_posix()]=digest.hexdigest()\n   if len(result)>10000: raise RuntimeError(\'Source evidence exceeds bound\')\n return result\ninitial=None; upgrade_attempted=False; r1data=Path(str(data)+\'-r1\'); manual=r1data; realm=None\ntry:\n initial=label_states()\n if any(value not in (\'enabled\',\'false\',\'not-listed\') for value in initial.values()): raise RuntimeError(\'A service label was already disabled\')\n if not all(absent(label) for label in initial): raise RuntimeError(\'Pre-existing task label loaded\')\n for label in initial:\n  plist=home/\'Library/LaunchAgents\'/(label+\'.plist\')\n  if plist.exists() or plist.is_symlink(): raise RuntimeError(\'Pre-existing task plist\')\n done(\'initial-service-preflight\')\n agent_fixture=base/\'agent/tests/fixtures/release0\'; fixture=base/\'driver/tests/fixtures/release0\'\n if (agent_fixture/\'SHA256SUMS\').read_bytes()!=(fixture/\'SHA256SUMS\').read_bytes(): raise RuntimeError(\'Agent and driver fixtures differ\')\n fixture,oldmanifest,_=v.verified_release0_fixture(fixture); done(\'r0-fixture\')\n manifest=base/\'app/public/install-manifest.json\'; raw=manifest.read_bytes(); pins=json.loads(raw)\n if hashlib.sha256(raw).hexdigest()!=\'861ede0410fb615b28c76b5e3736530e59747d79ce524d6920b9f4dffab5b45e\' or pins[\'agent\'][\'sha\']!=\'a052829cfe2f3725b1c2cf608974c10faf8eb99f\' or pins[\'runtime\'][\'sha\']!=\'9fc53b771134940aaac901e4cfc5d03c1db1a20a\': raise RuntimeError(\'App manifest tuple differs\')\n (evidence/\'app-manifest.json\').write_bytes(raw); status[\'app_manifest_sha256\']=hashlib.sha256(raw).hexdigest(); status[\'app_sha\']=\'aacad4a59464a110b0e9875d4b2ae8ca3de92a57\'; done(\'candidate-manifest\')\n with j.isolated_environment(run,root,data):\n  v.bootstrap_release0(fixture,oldmanifest,\'sh\',evidence/\'r0-install\',root,data); done(\'r0-install\')\n  j.wait_ready(data); oldpath,_,old=j.installation(root,data)\n  if v.installed_release(old)!=0: raise RuntimeError(\'Installed release is not R0\')\n  prove(\'haus.astrid.runtime\',data); done(\'r0-owner-identity\')\n  v.direct_workspace(root,old,data,evidence/\'seed\',\'seed\',{\'project\':\'ws06-r0-workspace\',\'generation\':\'release0\',\'task_id\':None,\'create_project\':True}); done(\'r0-seed\')\n  realm=j.workspace_root(data); prove(\'haus.astrid.runtime\',data)\n  if not absent(\'haus.astrid.runtime.r1\'): raise RuntimeError(\'R1 label became occupied\')\n  status[\'stages\'][\'upgrade-intent\']=\'recorded\'; save(); upgrade_attempted=True\n  env=dict(os.environ); env[\'BROWSER\']=f\'"{sys.executable}" -c "pass" %s\'\n  cmd=[\'/bin/sh\',str(base/\'app/public/install\'),\'--manifest\',str(manifest),\'--root\',str(root),\'--data-root\',str(data)]\n  with (evidence/\'upgrade-bootstrap.log\').open(\'wb\') as output:\n   process=subprocess.run(cmd,env=env,cwd=base/\'app\',stdout=output,stderr=subprocess.STDOUT,timeout=3600)\n  status[\'stages\'][\'upgrade\']={\'exit\':process.returncode}; save()\n  if process.returncode!=0: raise RuntimeError(\'R1 public upgrade failed\')\n  marker=j.read_json(root/\'install.json\')\n  if marker.get(\'r1_data_root\')!=str(r1data): raise RuntimeError(\'R1 data selection differs\')\n  path,_,installed=j.installation(root,data)\n  if v.installed_release(installed)!=1: raise RuntimeError(\'Selected version is not R1\')\n  selected=Path(installed[\'root\'])/\'manifest.json\'; selected_bytes=selected.read_bytes(); selected_json=json.loads(selected_bytes)\n  if any(selected_json.get(k)!=pins.get(k) for k in (\'agent\',\'runtime\')): raise RuntimeError(\'Installed R1 pins differ\')\n  (evidence/\'installed-r1-manifest.json\').write_bytes(selected_bytes); (evidence/\'installed-r1-current.txt\').write_bytes((root/\'current\').read_bytes())\n  (evidence/\'installed-r1-program-tree.json\').write_text(json.dumps(tree(root),indent=2)+\'\\n\'); status[\'installed_manifest_sha256\']=hashlib.sha256(selected_bytes).hexdigest()\n  upgrade_receipt=r1data/\'adopt-receipt.r1.json\'\n  if upgrade_receipt.is_symlink() or not upgrade_receipt.is_file() or not upgrade_receipt.resolve().is_relative_to(r1data): raise RuntimeError(\'Upgrade adoption receipt unavailable\')\n  (evidence/\'upgrade-adopt-receipt.json\').write_bytes(upgrade_receipt.read_bytes()); done(\'installed-r1-inventory\')\n  if not absent(\'haus.astrid.runtime\'): raise RuntimeError(\'R0 service remained loaded after upgrade\')\n  lock_free(realm); done(\'r0-stopped-lock-free\')\n  stop_owned(\'haus.astrid.runtime.r1\',r1data); done(\'r1-stopped-lock-free\')\n  database=realm/\'realm.sqlite3\'\n  if database.is_symlink() or not database.is_file() or not database.resolve().is_relative_to(data): raise RuntimeError(\'R0 database unsafe\')\n  connection=sqlite3.connect(database.as_uri()+\'?mode=ro\',uri=True)\n  try: rows=connection.execute(\'SELECT id FROM realm\').fetchall()\n  finally: connection.close()\n  if len(rows)!=1 or not isinstance(rows[0][0],str) or not re.fullmatch(r\'[A-Za-z0-9_-]{1,100}\',rows[0][0]): raise RuntimeError(\'R0 realm ID ambiguous\')\n  realm_id=rows[0][0]; before=filemap(realm); lock_free(realm)\n  interp=Path(installed[\'python\']); env=dict(os.environ); env.update(installed[\'env\']); env.pop(\'ASTRID_RUNTIME_DATA_ROOT\',None)\n  cli=[str(interp),\'-I\',\'-m\',\'astrid_runtime\']\n  def command(name,args):\n   p=subprocess.run([*cli,*args],env=env,cwd=base/\'runtime\',capture_output=True,text=True,timeout=1200)\n   (evidence/(name+\'.stdout.json\')).write_text(p.stdout); (evidence/(name+\'.stderr.txt\')).write_text(p.stderr)\n   status[\'stages\'][name]={\'exit\':p.returncode}; save(); return p\n  adopt=command(\'adopt\',[\'workspace\',\'adopt\',\'--data-root\',str(manual),\'--from\',str(data),\'--realm-id\',realm_id,\'--read-only-source\',\'--no-start\',\'--json\'])\n  target=manual/\'runtime/realms\'/realm_id\n  if target.is_dir() and not target.is_symlink():\n   inspect=command(\'inspect\',[\'workspace\',\'inspect\',\'--data-root\',str(manual),\'--expected-realm-id\',realm_id,\'--json\'])\n   after=filemap(realm); status[\'source_unchanged\']=before==after\n   source_cas=filemap(realm/\'cas/sha256\'); target_cas=filemap(target/\'cas/sha256\'); status[\'cas\']={\'identical\':source_cas==target_cas,\'source_sha256\':source_cas,\'target_sha256\':target_cas}\n   lock_free(realm); save()\n  else: status[\'stages\'][\'inspect\']=\'not-run-target-missing\'; status[\'cas\']=\'not-run-target-missing\'\n  status[\'outcome\']=\'pass\' if adopt.returncode==0 and status[\'stages\'].get(\'inspect\',{}).get(\'exit\')==0 and status.get(\'source_unchanged\') and isinstance(status.get(\'cas\'),dict) and status[\'cas\'][\'identical\'] and bool(status[\'cas\'][\'source_sha256\']) else \'failed\'\nexcept Exception as exc:\n status[\'outcome\']=\'failed\'; status[\'error_type\']=type(exc).__name__; status[\'error\']=str(exc); traceback.print_exc()\nfinally:\n try:\n  with j.isolated_environment(run,root,data):\n   for label,service_data in ((\'haus.astrid.runtime.r1\',r1data),(\'haus.astrid.runtime\',data)):\n    if not absent(label): stop_owned(label,service_data)\n   if not all(absent(label) for label in (\'haus.astrid.runtime\',\'haus.astrid.runtime.r1\')): raise RuntimeError(\'Service absence unverified\')\n   if initial is not None:\n    current=label_states()\n    for label,prior in initial.items():\n     if current[label] in (\'disabled\',\'true\') and prior in (\'enabled\',\'false\',\'not-listed\'):\n      subprocess.run([\'/bin/launchctl\',\'enable\',f\'gui/{os.getuid()}/{label}\'],check=True,timeout=30)\n    if any(x not in (\'enabled\',\'false\',\'not-listed\') for x in label_states().values()): raise RuntimeError(\'Launchd restoration unverified\')\n   status[\'cleanup\']=\'task-owned services stopped; launchd state restored\'\n except Exception as exc: status[\'cleanup\']=\'unverified\'; status[\'cleanup_error\']=str(exc)\n for label,log in ((\'installer\',home/\'Library/Logs/Astrid Runtime/install.log\'),(\'r1-runtime\',r1data/\'runtime/runtime.log\')):\n  if log.is_file() and not log.is_symlink() and log.resolve().is_relative_to(base):\n   with log.open(\'rb\') as f:\n    f.seek(0,2); f.seek(max(0,f.tell()-100000)); (evidence/(label+\'-tail.txt\')).write_bytes(f.read())\n save()\n')
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
