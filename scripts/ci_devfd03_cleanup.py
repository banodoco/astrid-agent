#!/usr/bin/env python3
"""Retain sanitized evidence, then remove this job's isolated temporary tree."""
import hashlib, json, os, pwd, re, shutil, stat, subprocess, sys
from pathlib import Path
UID=503
if os.getuid()!=UID or os.environ.get('RUNNER_NAME')!='hannah-mac' or sys.platform!='darwin': raise SystemExit('Unexpected runner')
home=Path(pwd.getpwuid(UID).pw_dir); temp=Path(os.environ['RUNNER_TEMP']); run=os.environ.get('GITHUB_RUN_ID',''); attempt=os.environ.get('GITHUB_RUN_ATTEMPT','')
if not re.fullmatch(r'[0-9]+',run) or not re.fullmatch(r'[0-9]+',attempt): raise SystemExit('Bad run identity')
if temp!=home/'actions-runner/_work/_temp' or temp.resolve()!=temp or temp.stat().st_uid!=UID: raise SystemExit('Unsafe temp')
source=temp/'r1-full-artifact-runtime'; task=temp/('r1-full-runtime-'+run+'-'+attempt)
parent=home/'.astrid-devfd03-evidence'; parent.mkdir(mode=0o700,exist_ok=True)
if parent.is_symlink() or parent.resolve()!=parent or parent.stat().st_uid!=UID or stat.S_IMODE(parent.stat().st_mode)!=0o700: raise SystemExit('Unsafe evidence parent')
if source.is_symlink() or not source.is_dir() or source.resolve()!=source or source.stat().st_uid!=UID: raise SystemExit('Evidence unavailable')
files=list(source.iterdir())
if not files or any(p.is_symlink() or not p.is_file() or p.stat().st_uid!=UID or p.stat().st_size>25000000 for p in files): raise SystemExit('Unsafe evidence')
result=json.loads((source/'result.json').read_text())
if result.get('runtime_sha')!='607c8eec784efaa0b2bfb4f57a91712b1657ed09': raise SystemExit('Evidence identity mismatch')
dest=parent/(run+'-'+attempt); dest.mkdir(mode=0o700)
for path in files:
 with path.open('rb') as inp, (dest/path.name).open('xb') as out: shutil.copyfileobj(inp,out)
 os.chmod(dest/path.name,0o600)
 if hashlib.sha256(path.read_bytes()).digest()!=hashlib.sha256((dest/path.name).read_bytes()).digest(): raise SystemExit('Evidence copy differs')
if result.get('cleanup')!={'task_argument_pids':[],'open_file_scan':'clear'}: raise SystemExit('Preceding cleanup check not clear')
if not shutil.rmtree.avoids_symlink_attacks: raise SystemExit('Safe rmtree unavailable')
for target in (task,source):
 if target.is_symlink() or not target.is_dir() or target.resolve()!=target or target.stat().st_uid!=UID: raise SystemExit('Unsafe task directory')
 st=target.stat(); listing=subprocess.run(['/bin/ps','-ww','-U',str(UID),'-o','pid=,command='],capture_output=True,text=True,check=True,timeout=20)
 if any(str(target) in x for x in listing.stdout.splitlines()): raise SystemExit('Task referenced')
 opened=subprocess.run(['/usr/sbin/lsof','-n','+D',str(target)],capture_output=True,text=True,timeout=180)
 if opened.returncode!=1 or opened.stdout.strip() or opened.stderr.strip(): raise SystemExit('Open-file check failed')
 for directory,dirs,files in os.walk(target,followlinks=False,onerror=lambda exc: (_ for _ in ()).throw(exc)):
  for name in dirs+files:
   st_entry=os.lstat(os.path.join(directory,name))
   if st_entry.st_uid!=UID or st_entry.st_dev!=st.st_dev: raise SystemExit('Foreign owner or filesystem boundary')
 fd=os.open(temp,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:
  current=os.stat(target.name,dir_fd=fd,follow_symlinks=False)
  if (current.st_dev,current.st_ino,current.st_uid)!=(st.st_dev,st.st_ino,st.st_uid): raise SystemExit('Task identity changed')
  shutil.rmtree(target.name,dir_fd=fd)
 finally: os.close(fd)
print(json.dumps({'status':'removed task and staging directories','evidence':'retained under runner account','run_id':run}))
