#!/usr/bin/env python3
"""Run a stopped-owner R0-to-R1 test in a runner-owned sandbox."""
import hashlib
import json
import os
import pwd
import re
import shutil
import socket
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UID = 503
REFS = {"agent": ("astrid-agent", "57cabe57f8294eded12c555d8eb3d5e1f1d20368"),
        "app": ("astrid-app", "878c28df7a6c80fa9aaa6540c5fccad95473e7c1")}



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


def port_free(port=17340):
    """Require lsof to prove no TCP listener on the selected Runtime port."""
    result = subprocess.run(["/usr/sbin/lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN"],
                            capture_output=True, text=True, timeout=20)
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def guard_host(home, port=17340):
    # The R3 contract explicitly asks for the port observation first.
    if not port_free(port):
        raise RuntimeError("Selected Runtime port is occupied or cannot be verified; no installation attempted")
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
             "Selected Runtime port is occupied or cannot be verified; no installation attempted", "Runner-owned Python is unavailable",
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
    (output / "custody.json").write_bytes(redact((json.dumps(status, indent=2, sort_keys=True) + "\n").encode(), home, root))
    rows=[]
    for path in sorted(output.rglob("*")):
        if path.is_file() and not path.is_symlink() and path.name != "SHA256SUMS":
            rows.append(digest(path) + "  " + path.relative_to(output).as_posix())
    (output / "SHA256SUMS").write_text("\n".join(rows) + "\n")


def main():
    if os.getuid()!=UID or os.environ.get('RUNNER_NAME')!='hannah-mac' or sys.platform!='darwin': raise RuntimeError('Runner identity differs')
    home=Path(pwd.getpwuid(UID).pw_dir); temp=Path(os.environ['RUNNER_TEMP']); root=None
    if temp!=home/'actions-runner/_work/_temp' or temp.resolve()!=temp or temp.stat().st_uid!=UID: raise RuntimeError('Runner temp differs')
    output=temp/'r05-artifact';output.mkdir(mode=0o700)
    with open(os.environ['GITHUB_ENV'],'a') as f:f.write('R05_UPLOAD='+str(output)+'\n')
    status={'status':'not-run','runner_custody':'hannah-mac; self-hosted','qualification':False,'stages':{},'refs':{k:v[1] for k,v in REFS.items()}}
    try:
        with socket.socket() as probe: probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        if port==17340 or not port_free(port):raise RuntimeError('Isolated Runtime port unavailable')
        guard_host(home,port);status['port']=port
        parent=home/'.astrid-r1fix-stopped';parent.mkdir(mode=0o700,exist_ok=True)
        if parent.is_symlink() or parent.resolve()!=parent or parent.stat().st_uid!=UID or stat.S_IMODE(parent.stat().st_mode)!=0o700:raise RuntimeError('Unsafe task parent')
        root=Path(tempfile.mkdtemp(prefix='task.',dir=parent));
        with open(os.environ['GITHUB_ENV'],'a') as f:f.write('WS02_TASK_ROOT='+str(root)+'\n')
        pyroot=home/'actions-runner/_work/_tool/Python/3.11.18/arm64'; py=pyroot/'bin/python3.11'
        if pyroot.is_symlink() or pyroot.resolve()!=pyroot or pyroot.stat().st_uid!=UID or not py.is_file() or not py.resolve().is_relative_to(pyroot):raise RuntimeError('Runner Python unavailable')
        env=environment(home,root,py);logs=root/'private-logs';logs.mkdir(mode=0o700)
        for name,(repo,sha) in REFS.items():
            for step,args in (('clone',['/usr/bin/git','-c','core.hooksPath=/dev/null','clone','--depth=1','--filter=blob:none','--no-checkout','https://github.com/banodoco/'+repo+'.git',name]),('fetch',['/usr/bin/git','-C',name,'-c','core.hooksPath=/dev/null','fetch','--depth=1','origin',sha]),('checkout',['/usr/bin/git','-C',name,'-c','core.hooksPath=/dev/null','checkout','--detach',sha])):
                if shutil.disk_usage(home).free<10_000_000_000:raise RuntimeError('Insufficient disk before source operation')
                if run_process(args,root,env,logs/(name+'-'+step+'.log'),home)['exit']!=0:raise RuntimeError('Source operation failed: '+name+'/'+step)
            head=subprocess.check_output(['/usr/bin/git','-C',str(root/name),'rev-parse','HEAD'],env=env,text=True,timeout=20).strip()
            if head!=sha:raise RuntimeError('Source checkout identity differs')
            status['stages']['checkout-'+name]='pass'
        archive=root/'node.tar.gz'
        args=['/usr/bin/curl','-q','--proto','=https','--proto-redir','=https','-fsSL','--retry','3','--output',str(archive),'https://nodejs.org/dist/v20.19.4/node-v20.19.4-darwin-arm64.tar.gz']
        if run_process(args,root,env,logs/'node-download.log',home)['exit']!=0 or digest(archive)!='8b89801f527a85e3f7de539512c9dde08673ac99db6458cc7cc627e05d707054':raise RuntimeError('Node download or checksum failed')
        if run_process(['/usr/bin/tar','-xzf',str(archive),'-C',str(root)],root,env,logs/'node-extract.log',home)['exit']!=0:raise RuntimeError('Node extraction failed')
        node=root/'node-v20.19.4-darwin-arm64/bin/node'
        if not node.is_file() or not node.resolve().is_relative_to(root) or node.stat().st_uid!=UID:raise RuntimeError('Node executable identity differs')
        if run_process([str(node),'scripts/installer/pin.mjs',REFS['agent'][1]],root/'app',env,logs/'pin.log',home)['exit']!=0:raise RuntimeError('Candidate pin command failed')
        status['stages']['pin']='pass';guard_host(home,port);env.pop('RUNNER_TEMP',None);env['ASTRID_RUNTIME_DOOR_PORT']=str(port)
        inner=Path(__file__).with_name('mac_update_inner.py')
        status['inner']=run_process([str(py),'-u',str(inner),str(root)],root,env,logs/'capture-private.log',home,is_driver=True)
        status['status']='diagnostic-exited'
        receipt=root/'evidence/result.json'
        if receipt.is_file() and not receipt.is_symlink():status['receipt']=json.loads(receipt.read_text())
    except Exception as exc:
        status['error']=safe_error(exc);status['error_type']=type(exc).__name__
        if root is not None and not (root/'evidence').exists():
            (root/'evidence').mkdir(mode=0o700)
            clean='task-owned services stopped; launchd state restored' if service_absent() and not host_owners_exist() else 'unverified'
            (root/'evidence/result.json').write_text(json.dumps({'outcome':'failed','cleanup':clean,'uninstall':'not-run','error':status['error']})+'\n')
            for log in (root/'private-logs').glob('*.log'):
                if log.is_file() and not log.is_symlink() and log.stat().st_size < 16000000: shutil.copyfile(log,root/'evidence'/log.name)
    finally:
        status['service_absent_after']=service_absent();status['disk_free_after']=shutil.disk_usage(home).free
        export_capture(root,home,output,status)
        print(redact(json.dumps({'status':status['status'],'receipt':status.get('receipt'),'service_absent_after':status['service_absent_after']}).encode(),home,root).decode())
    if (status.get('receipt',{}).get('outcome')!='pass' or status.get('receipt',{}).get('uninstall')!='pass' or status.get('receipt',{}).get('cleanup')!='task-owned services stopped; launchd state restored' or not status['service_absent_after']):raise SystemExit(1)
if __name__=='__main__':
    try:main()
    except Exception:
        print(json.dumps({'status':'preflight-failed'}));raise SystemExit(1)
