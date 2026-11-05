#!/usr/bin/env python3
"""Remove only this completed WS02 sandbox after its artifact was uploaded."""
import json, os, pwd, re, shutil, stat, subprocess, sys
from pathlib import Path
UID=503
if os.getuid()!=UID or os.environ.get('RUNNER_NAME')!='hannah-mac' or sys.platform!='darwin': raise SystemExit('Unexpected runner')
home=Path(pwd.getpwuid(UID).pw_dir); temp=Path(os.environ['RUNNER_TEMP']); parent=home/'.astrid-r1fix-stopped'; task=parent/'task.mibzjrmv'; output=temp/'mac-update04-stop-audit'
if temp!=home/'actions-runner/_work/_temp' or temp.resolve()!=temp or temp.stat().st_uid!=UID: raise SystemExit('Unsafe temp')
if parent.is_symlink() or parent.resolve()!=parent or parent.stat().st_uid!=UID or task.parent!=parent or not re.fullmatch(r'task\.[A-Za-z0-9_]{8}',task.name): raise SystemExit('Unsafe task parent')
receipt=output/'audit.json'
if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_uid!=UID: raise SystemExit('Result unavailable')
value=json.loads(receipt.read_text())
if value.get('status')!='stopped' or value.get('owner_lock')!='free' or value.get('task_processes')!=[] or value.get('final_label') not in ('enabled','false','not-listed'): raise SystemExit('Task recovery not proven')
for label in ('haus.astrid.runtime','haus.astrid.runtime.r1'):
 p=subprocess.run(['/bin/launchctl','print',f'gui/{UID}/{label}'],capture_output=True,text=True,timeout=20)
 missing=f'Could not find service "{label}" in domain for user gui: {UID}'
 if p.returncode!=113 or p.stderr.strip().replace('\r\n','\n') not in (missing,'Bad request.\n'+missing):raise SystemExit('Task label absence not proven')
if not shutil.rmtree.avoids_symlink_attacks: raise SystemExit('Safe rmtree unavailable')
for target in (task,output):
 if target.is_symlink() or not target.is_dir() or target.resolve()!=target or target.stat().st_uid!=UID: raise SystemExit('Unsafe task directory')
 st=target.stat(); listing=subprocess.run(['/bin/ps','-ww','-U',str(UID),'-o','pid=,command='],capture_output=True,text=True,check=True,timeout=20)
 records=[]
 for line in listing.stdout.splitlines():
  match=re.match(r'^\s*(\d+)\s+(.*)$',line)
  if match: records.append(match.group(2))
  elif records: records[-1]+='\n'+line
 if any(str(target) in command for command in records): raise SystemExit('Task referenced')
 opened=subprocess.run(['/usr/sbin/lsof','-n','+D',str(target)],capture_output=True,text=True,timeout=180)
 if opened.returncode!=1 or opened.stdout.strip() or opened.stderr.strip(): raise SystemExit('Open-file check failed')
 for directory,dirs,files in os.walk(target,followlinks=False,onerror=lambda exc: (_ for _ in ()).throw(exc)):
  for name in dirs+files:
   item=os.lstat(os.path.join(directory,name))
   if item.st_uid!=UID or item.st_dev!=st.st_dev: raise SystemExit('Foreign owner or filesystem boundary')
 fd=os.open(target.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try:
  current=os.stat(target.name,dir_fd=fd,follow_symlinks=False)
  if (current.st_dev,current.st_ino,current.st_uid)!=(st.st_dev,st.st_ino,st.st_uid): raise SystemExit('Task identity changed')
  shutil.rmtree(target.name,dir_fd=fd)
 finally: os.close(fd)
print('Removed only MAC-UPDATE-01 task and audit-staging directories; uploaded evidence retained.')
