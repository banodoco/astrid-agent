#!/usr/bin/env python3
"""Run a stopped-owner R0-to-R1 test in a runner-owned sandbox."""
import base64
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


def runner_updaters_exist():
    result=subprocess.run(['/bin/ps','-ww','-u',str(UID),'-o','pid=,command='],capture_output=True,text=True,timeout=20,check=True)
    commands=[]
    for line in result.stdout.splitlines():
        match=re.match(r'^\s*(\d+)\s+(.*)$',line)
        if match: commands.append(match.group(2))
        elif commands: commands[-1]+='\n'+line
    return any(re.search(r'(?:^|\s)\S*/public/install(?:\s|$)',c) or re.search(r'(?:^|\s)-m\s+astrid\s+install(?:\s|$)',c) for c in commands)

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
    if runner_updaters_exist():
        raise RuntimeError("Runner updater is active; no installation attempted")
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



def run_process(argv, cwd, env, logfile, home, is_driver=False):
    with logfile.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        abort = False; census_failed = False; grace_expired = False; started = None
        while proc.poll() is None:
            if not abort and shutil.disk_usage(home).free < 3_000_000_000:
                # Do not signal a product installer with unknown handlers.
                # For preparatory commands, terminate only our direct child.
                if not is_driver:
                    try: proc.terminate()
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
    target=output/'capture-upload';target.mkdir(mode=0o700)
    value={}
    if root is not None:
        receipt=root/'evidence/result.json'
        if receipt.is_file() and not receipt.is_symlink():
            v=json.loads(receipt.read_text())
            value={k:v.get(k) for k in ('stages','outcome','cleanup','uninstall','recovery_stop','launch_admission_refusal','private_service_log_retained','private_service_log_state','private_service_log_sha256','candidate_manifest_sha256','error_type','error_frames','recorded_door_mismatch','rollback_unknown_state','initial_label_states')}
            (target/'result.json').write_text(json.dumps(value,indent=2,sort_keys=True)+'\n')
        diag=root/'evidence/sanitized-lock-diagnostic.json'
        if diag.is_file() and not diag.is_symlink() and diag.stat().st_size<1000000:
            (target/'sanitized-lock-diagnostic.json').write_bytes(diag.read_bytes())
    safe={'status':status.get('status'),'service_absent_after':status.get('service_absent_after'),'disk_free_after':status.get('disk_free_after'),'receipt':value}
    (output/'custody.json').write_text(json.dumps(safe,indent=2,sort_keys=True)+'\n')
    rows=[digest(p)+'  '+p.relative_to(output).as_posix() for p in sorted(output.rglob('*')) if p.is_file() and not p.is_symlink() and p.name!='SHA256SUMS']
    (output/'SHA256SUMS').write_text('\n'.join(rows)+'\n')

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
        raw=base64.b64decode('ewogICJzY2hlbWFfdmVyc2lvbiI6IDEsCiAgInJlbGVhc2UiOiAyLAogICJhZ2VudCI6IHsKICAgICJzaGEiOiAiNTdjYWJlNTdmODI5NGVkZWQxMmM1NTVkOGViM2Q1ZTFmMWQyMDM2OCIKICB9LAogICJydW50aW1lIjogewogICAgInNoYSI6ICIyNzc2YmE5NzNiNmM4ZjM1NTViNTYyOWIwODQwZDk2OTFhOThkM2MyIiwKICAgICJzY2hlbWFfZGlnZXN0IjogInNoYTI1Njo5NjI4YmI1ZmQ2ZTFkYmJlYzM5MmRkMzlmMmUwMTAzMTYyMmRjZjUyZDdjY2ZmN2NkMWYwYWQxMDc0M2RjYjFjIgogIH0sCiAgInRvb2xzIjogewogICAgInV2IjogewogICAgICAidmVyc2lvbiI6ICIwLjExLjMyIgogICAgfSwKICAgICJweXRob24iOiB7CiAgICAgICJ2ZXJzaW9uIjogIjMuMTEuMTMiCiAgICB9LAogICAgImJ1biI6IHsKICAgICAgInZlcnNpb24iOiAiMS4zLjE0IiwKICAgICAgImFzc2V0cyI6IHsKICAgICAgICAibGludXgteDY0IjogewogICAgICAgICAgInVybCI6ICJodHRwczovL2dpdGh1Yi5jb20vb3Zlbi1zaC9idW4vcmVsZWFzZXMvZG93bmxvYWQvYnVuLXYxLjMuMTQvYnVuLWxpbnV4LXg2NC56aXAiLAogICAgICAgICAgInNoYTI1NiI6ICI5NTFlZTJhZWU4NTVmMDg1OTVhZWVjNjIyNTIyNmEyOThkM2ZlYTgzYTNkY2Q2NDY1YzA5Y2JjY2RmN2U4NDhmIgogICAgICAgIH0sCiAgICAgICAgImxpbnV4LWFybTY0IjogewogICAgICAgICAgInVybCI6ICJodHRwczovL2dpdGh1Yi5jb20vb3Zlbi1zaC9idW4vcmVsZWFzZXMvZG93bmxvYWQvYnVuLXYxLjMuMTQvYnVuLWxpbnV4LWFhcmNoNjQuemlwIiwKICAgICAgICAgICJzaGEyNTYiOiAiYTI3ZmZiNjNhODMxMDM3NTgzNmUwZDZmNjY4YWUxN2ZhOGQ4ZDE4Yjg4YzM3YzgyMWM2NTMzMTk3M2ExOWEzYiIKICAgICAgICB9LAogICAgICAgICJtYWNvcy14NjQiOiB7CiAgICAgICAgICAidXJsIjogImh0dHBzOi8vZ2l0aHViLmNvbS9vdmVuLXNoL2J1bi9yZWxlYXNlcy9kb3dubG9hZC9idW4tdjEuMy4xNC9idW4tZGFyd2luLXg2NC56aXAiLAogICAgICAgICAgInNoYTI1NiI6ICI0MTgzZGYzMzc0NjIzZTViYWIzMTVjNTQ3Y2ZhMDk3NDUzM2NkNDU3ZDg2YjczYjYzOWY3YTg3OTc0Y2Q2NjMzIgogICAgICAgIH0sCiAgICAgICAgIm1hY29zLWFybTY0IjogewogICAgICAgICAgInVybCI6ICJodHRwczovL2dpdGh1Yi5jb20vb3Zlbi1zaC9idW4vcmVsZWFzZXMvZG93bmxvYWQvYnVuLXYxLjMuMTQvYnVuLWRhcndpbi1hYXJjaDY0LnppcCIsCiAgICAgICAgICAic2hhMjU2IjogImQ4Yjk2MjIxODI4YWQ2Zjk3YWM3YWMwYWI3ZTk1ODcyMzQxYWY3NjMwMDFlODgwM2U4MjY3NjUyYzI2NTI2MjAiCiAgICAgICAgfSwKICAgICAgICAid2luZG93cy14NjQiOiB7CiAgICAgICAgICAidXJsIjogImh0dHBzOi8vZ2l0aHViLmNvbS9vdmVuLXNoL2J1bi9yZWxlYXNlcy9kb3dubG9hZC9idW4tdjEuMy4xNC9idW4td2luZG93cy14NjQuemlwIiwKICAgICAgICAgICJzaGEyNTYiOiAiMGEwNjIwOTMwYjY2NzVkN2JhNDQwZTgxZjRlMGUwMGQzY2ZiZTA5NmM0YjE0MGQzZmZmMDIyMDVlOWUxODkyMiIKICAgICAgICB9CiAgICAgIH0KICAgIH0sCiAgICAib21wIjogewogICAgICAic291cmNlIjogewogICAgICAgICJyZXBvIjogImh0dHBzOi8vZ2l0aHViLmNvbS9wZXRlcm9tYWxsZXQvb2gtbXktcGkiLAogICAgICAgICJzaGEiOiAiNzhmMGUwOTYwZjQ3MWMyYzYyYjk0MzI3MmE0OGMzMzM2NmYyMzlmZSIKICAgICAgfQogICAgfSwKICAgICJub2RlIjogewogICAgICAidmVyc2lvbiI6ICIyMC4xOS40IiwKICAgICAgImFzc2V0cyI6IHsKICAgICAgICAibGludXgteDY0IjogewogICAgICAgICAgInVybCI6ICJodHRwczovL25vZGVqcy5vcmcvZGlzdC92MjAuMTkuNC9ub2RlLXYyMC4xOS40LWxpbnV4LXg2NC50YXIuZ3oiLAogICAgICAgICAgInNoYTI1NiI6ICJkODBhMzM3MDc2MDVjZWQ5YTMxYjhmNTQzY2VhOWFiNTEyYmMzZDJmZWYyYzE0OGYzMWE1MGU5MzlmZjA3NTYwIgogICAgICAgIH0sCiAgICAgICAgImxpbnV4LWFybTY0IjogewogICAgICAgICAgInVybCI6ICJodHRwczovL25vZGVqcy5vcmcvZGlzdC92MjAuMTkuNC9ub2RlLXYyMC4xOS40LWxpbnV4LWFybTY0LnRhci5neiIsCiAgICAgICAgICAic2hhMjU2IjogImQyMDA3OTgzMzJiN2E1NmQzNTU4ODhjZTU4ZTZhNjM5ZmFjNzkzOWE0ODMzZTViYzg3ODBjNjY4ODhlMWNlNGQiCiAgICAgICAgfSwKICAgICAgICAibWFjb3MteDY0IjogewogICAgICAgICAgInVybCI6ICJodHRwczovL25vZGVqcy5vcmcvZGlzdC92MjAuMTkuNC9ub2RlLXYyMC4xOS40LWRhcndpbi14NjQudGFyLmd6IiwKICAgICAgICAgICJzaGEyNTYiOiAiY2M0N2VkMDk0YzE4NzZiMjExYTkwOTFkMDlhNzg1MzdjMWNiYmIwZjhjZjVhNDljOWZkNzk5MzNlZThmZDdmZCIKICAgICAgICB9LAogICAgICAgICJtYWNvcy1hcm02NCI6IHsKICAgICAgICAgICJ1cmwiOiAiaHR0cHM6Ly9ub2RlanMub3JnL2Rpc3QvdjIwLjE5LjQvbm9kZS12MjAuMTkuNC1kYXJ3aW4tYXJtNjQudGFyLmd6IiwKICAgICAgICAgICJzaGEyNTYiOiAiOGI4OTgwMWY1MjdhODVlM2Y3ZGU1Mzk1MTJjOWRkZTA4NjczYWM5OWRiNjQ1OGNjN2NjNjI3ZTA1ZDcwNzA1NCIKICAgICAgICB9LAogICAgICAgICJ3aW5kb3dzLXg2NCI6IHsKICAgICAgICAgICJ1cmwiOiAiaHR0cHM6Ly9ub2RlanMub3JnL2Rpc3QvdjIwLjE5LjQvbm9kZS12MjAuMTkuNC13aW4teDY0LnppcCIsCiAgICAgICAgICAic2hhMjU2IjogIjFiZjgzZTU5NTgxNTdkMTM2NzM1MDczNDkyMzgyMzZhZWM0ZjZlZmM5NWNmNDI2Y2JlMTI2YTk5OWEzZTRjMGIiCiAgICAgICAgfSwKICAgICAgICAid2luZG93cy1hcm02NCI6IHsKICAgICAgICAgICJ1cmwiOiAiaHR0cHM6Ly9ub2RlanMub3JnL2Rpc3QvdjIwLjE5LjQvbm9kZS12MjAuMTkuNC13aW4tYXJtNjQuemlwIiwKICAgICAgICAgICJzaGEyNTYiOiAiMTU1NDI1MTAyN2Q3NzdkM2RiYjI3NmFmMDU1M2IyZTlmODk4MjJhMTU3OTA2NzIxNmJjMGI5NTIyYTNhMjJkMyIKICAgICAgICB9CiAgICAgIH0KICAgIH0sCiAgICAiZmZtcGVnIjogewogICAgICAidmVyc2lvbiI6ICI3LjAuMiIsCiAgICAgICJhc3NldHMiOiB7CiAgICAgICAgImxpbnV4LXg2NCI6IHsKICAgICAgICAgICJ1cmwiOiAiaHR0cHM6Ly9naXRodWIuY29tL2Jhbm9kb2NvL2FzdHJpZC1hZ2VudC9yZWxlYXNlcy9kb3dubG9hZC9kZXBzLWZmbXBlZy03LjAuMi0yMDI2MTEwNC9mZm1wZWctNy4wLjItYW1kNjQtc3RhdGljLnRhci54eiIsCiAgICAgICAgICAic2hhMjU2IjogImFiZGE4ZDc3Y2U4MzA5MTQxZjgzYWI4ZWRmMDU5NjgzNDA4N2M1MjQ2N2Y2YmFkZjM3NmE2YTJhNGM4N2NmNjciCiAgICAgICAgfSwKICAgICAgICAibGludXgtYXJtNjQiOiB7CiAgICAgICAgICAidXJsIjogImh0dHBzOi8vam9obnZhbnNpY2tsZS5jb20vZmZtcGVnL3JlbGVhc2VzL2ZmbXBlZy03LjAuMi1hcm02NC1zdGF0aWMudGFyLnh6IiwKICAgICAgICAgICJzaGEyNTYiOiAiZjQxNDliYjJiMDc4NGUzMGU5OWJkZGE4NTQ3MWM5YjU5MzBkMzQwMjAxNGU5MzRhNTA5OGI0MWQwZjcyMDFiMSIKICAgICAgICB9LAogICAgICAgICJtYWNvcy14NjQiOiB7CiAgICAgICAgICAidXJscyI6IFsKICAgICAgICAgICAgImh0dHBzOi8vZmZtcGVnLm1hcnRpbi1yaWVkbC5kZS9kb3dubG9hZC9tYWNvcy9hbWQ2NC8xNzI2MDc1ODMzXzcuMC4yL2ZmbXBlZy56aXAiLAogICAgICAgICAgICAiaHR0cHM6Ly9mZm1wZWcubWFydGluLXJpZWRsLmRlL2Rvd25sb2FkL21hY29zL2FtZDY0LzE3MjYwNzU4MzNfNy4wLjIvZmZwcm9iZS56aXAiCiAgICAgICAgICBdLAogICAgICAgICAgInNoYTI1NiI6IFsKICAgICAgICAgICAgIjJiMTFmMWFkMzVhNjdiOWJiZTBiZjcyNmEzZWIwMTcxZDRlZWFiZjk1MDgxYmIzNGFmMWIzNzFlY2FhMWM2MDciLAogICAgICAgICAgICAiZWZiOWIyMTUzZGQ3NzQ4ZGM4ZTA5MDI3YmRkYjEzZjRhMTQ3MmFlNDM4MTk2NmYzZDJlNjY0NmU0MzUwZGYzNyIKICAgICAgICAgIF0KICAgICAgICB9LAogICAgICAgICJtYWNvcy1hcm02NCI6IHsKICAgICAgICAgICJ1cmxzIjogWwogICAgICAgICAgICAiaHR0cHM6Ly9mZm1wZWcubWFydGluLXJpZWRsLmRlL2Rvd25sb2FkL21hY29zL2FybTY0LzE3MjYwNzMzMTNfNy4wLjIvZmZtcGVnLnppcCIsCiAgICAgICAgICAgICJodHRwczovL2ZmbXBlZy5tYXJ0aW4tcmllZGwuZGUvZG93bmxvYWQvbWFjb3MvYXJtNjQvMTcyNjA3MzMxM183LjAuMi9mZnByb2JlLnppcCIKICAgICAgICAgIF0sCiAgICAgICAgICAic2hhMjU2IjogWwogICAgICAgICAgICAiY2IwYzA3MWU1ODk0NTIzMWFlOTU5NmNlMjFjNDZiOTQ4MTAxNDJkMGE2NjRlZDFkY2MyZTljNmYxYjI1MjVjNCIsCiAgICAgICAgICAgICI3N2E4Njc4ZWExZTQ3YTg0OTVkZGViZjY1NjFhMTQyYWJmZDgxMDVjMTI4NmJmZGEwODBkYmE1ZjViMDY0ZWUyIgogICAgICAgICAgXQogICAgICAgIH0sCiAgICAgICAgIndpbmRvd3MteDY0IjogewogICAgICAgICAgInVybCI6ICJodHRwczovL2dpdGh1Yi5jb20vR3lhbkQvY29kZXhmZm1wZWcvcmVsZWFzZXMvZG93bmxvYWQvNy4wLjIvZmZtcGVnLTcuMC4yLWZ1bGxfYnVpbGQuemlwIiwKICAgICAgICAgICJzaGEyNTYiOiAiMGFhNjIzZmVlYzk1MDY2ODM5ZGI0MTI1Njc5Y2QwNGQ0YThmYzEwMzllZDg4NzdhY2NhOTgyNjExM2U0Mjc2OCIKICAgICAgICB9CiAgICAgIH0KICAgIH0KICB9Cn0K',validate=True)
        if hashlib.sha256(raw).hexdigest()!='582741c3378d1a652c0b9615d53351b4e6ab527f816621a99fd33b2655b9a197':raise RuntimeError('Baseline manifest bytes differ')
        (root/'app/public/install-manifest.json').write_bytes(raw)
        status['stages']['verified-baseline']='pass';guard_host(home,port);env.pop('RUNNER_TEMP',None);env['ASTRID_RUNTIME_DOOR_PORT']=str(port)
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
        print(json.dumps({'status':status['status'],'service_absent_after':status['service_absent_after']}))
    if (status.get('receipt',{}).get('outcome')!='pass' or status.get('receipt',{}).get('uninstall')!='pass' or status.get('receipt',{}).get('cleanup')!='task-owned services stopped; launchd state restored' or not status['service_absent_after']):raise SystemExit(1)
if __name__=='__main__':
    try:main()
    except Exception:
        print(json.dumps({'status':'preflight-failed'}));raise SystemExit(1)
