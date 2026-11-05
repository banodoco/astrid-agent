#!/usr/bin/env python3
"""Stop only the verified R0 LaunchAgent owned by the failed MAC-UPDATE-01 task."""
import fcntl,hashlib,json,os,plistlib,pwd,re,stat,subprocess,sys,time
from pathlib import Path
UID=503; LABEL='haus.astrid.runtime'; OTHER=LABEL+'.r1'
if os.getuid()!=UID or os.environ.get('RUNNER_NAME')!='hannah-mac' or sys.platform!='darwin': raise SystemExit('Unexpected runner')
home=Path(pwd.getpwuid(UID).pw_dir); base=home/'.astrid-r1fix-stopped'; task=base/'task.mibzjrmv'; temp=Path(os.environ['RUNNER_TEMP']); out=temp/'mac-update04-stop-audit'
if temp!=home/'actions-runner/_work/_temp' or temp.resolve()!=temp or temp.stat().st_uid!=UID: raise SystemExit('Unsafe temp')
if base.is_symlink() or base.resolve()!=base or task.is_symlink() or task.resolve()!=task or task.stat().st_uid!=UID: raise SystemExit('Unsafe task')
for part in (task/'evidence',home/'.astrid-mac-update04-private'):
 if part.is_symlink() or not part.is_dir() or part.stat().st_uid!=UID or not part.resolve().is_relative_to(home):raise SystemExit('Evidence directory identity differs')
receipt=task/'evidence/sanitized-lock-diagnostic.json'
if receipt.is_symlink() or not receipt.is_file() or hashlib.sha256(receipt.read_bytes()).hexdigest()!='7f17bfe8513fe69aa100ecabdba97393c1b3c097220ca5c060631c03fe1c3323': raise SystemExit('Diagnostic receipt provenance differs')
result_path=task/'evidence/result.json'
if result_path.is_symlink() or not result_path.is_file() or result_path.stat().st_uid!=UID:raise SystemExit('Task result unsafe')
diag=json.loads(receipt.read_text()); record=json.loads(result_path.read_text())
if diag.get('update_exit')!=0 or record.get('stages',{}).get('after-task')!='pass' or record.get('stages',{}).get('uninstall',{}).get('exit')!=1 or record.get('initial_label_states')!={LABEL:'enabled',OTHER:'not-listed'}:raise SystemExit('Task result identity differs')
private=home/'.astrid-mac-update04-private'/('service-log-'+task.name+'.log')
if private.is_symlink() or not private.is_file() or private.stat().st_uid!=UID or hashlib.sha256(private.read_bytes()).hexdigest()!=record.get('private_service_log_sha256'):raise SystemExit('Preserved service log differs')
out.mkdir(mode=0o700)
with open(os.environ['GITHUB_ENV'],'a') as fh: fh.write('MAC04_STOP_UPLOAD='+str(out)+'\n')
result={'status':'not-run','initial_label':'unknown','final_label':'unknown','private_service_log_retained':True}
def save(): (out/'audit.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
def print_service(label): return subprocess.run(['/bin/launchctl','print',f'gui/{UID}/{label}'],capture_output=True,text=True,timeout=20)
def absent(label):
 p=print_service(label); missing=f'Could not find service "{label}" in domain for user gui: {UID}'
 return p.returncode==113 and p.stderr.strip().replace('\r\n','\n') in (missing,'Bad request.\n'+missing)
def states():
 p=subprocess.run(['/bin/launchctl','print-disabled',f'gui/{UID}'],capture_output=True,text=True,timeout=20,check=True)
 if 'disabled services' not in p.stdout: raise RuntimeError('Disabled state unverified')
 result={}
 for label in (LABEL,OTHER):
  m=re.search(r'(?m)^\s*"'+re.escape(label)+r'"\s*=>\s*(\w+)\s*$',p.stdout); result[label]=m.group(1) if m else 'not-listed'
 return result
def task_pids():
 p=subprocess.run(['/bin/ps','-ww','-u',str(UID),'-o','pid=,command='],capture_output=True,text=True,timeout=20,check=True)
 records=[]
 for line in p.stdout.splitlines():
  match=re.match(r'^\s*(\d+)\s+(.*)$',line)
  if match: records.append([int(match.group(1)),match.group(2)])
  elif records: records[-1][1]+='\n'+line
 return [pid for pid,command in records if str(task) in command]
changed=False
try:
 state=states(); result['initial_label']=state[LABEL]; result['initial_r1_label']=state[OTHER]
 if state[LABEL] not in ('enabled','false','not-listed') or not absent(OTHER): raise RuntimeError('Unexpected initial service state')
 root=task/'run/home/.local/share/astrid'; data=task/'run/data'; plist=task/'run/home/Library/LaunchAgents'/f'{LABEL}.plist'
 version=(root/'current').read_text().strip()
 if not re.fullmatch(r'[A-Za-z0-9._-]+',version):raise RuntimeError('Selected version differs')
 mpath=root/'versions'/version/'manifest.json'
 if mpath.is_symlink() or not mpath.resolve().is_relative_to(root/'versions'):raise RuntimeError('Selected manifest unsafe')
 manifest=json.loads(mpath.read_text())
 if manifest.get('agent',{}).get('sha')!='39a3ed6da488b32196df3148cc3bd4da19f82472' or manifest.get('runtime',{}).get('sha')!='9fc53b771134940aaac901e4cfc5d03c1db1a20a' or manifest.get('release')!=3:raise RuntimeError('Selected candidate tuple differs')
 for filename in (task/'evidence/uninstall.log',task/'run/home/Library/Logs/Astrid Runtime/install.log'):
  if filename.is_file() and not filename.is_symlink() and filename.resolve().is_relative_to(task):
   content=filename.read_text(errors='replace')
   frames=re.findall(r'File "[^"\n]*/(astrid/installer/(?:backend|core|cli|service)\.py)", line (\d+)',content)
   if frames:result['installer_error_frames']=[{'file':f,'line':int(n)} for f,n in frames[-8:]]
   tail=content.rsplit('Traceback (most recent call last):',1)[-1]
   errors=re.findall(r'(?m)^([A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):\s*(.{0,350})$',tail)
   if errors:result['installer_exception_type']=errors[-1][0]
   result['uninstall_known_errors']=[phrase for phrase in ('The launchd service state is unknown; refusing to stop it.','uninstall','in progress','recovery','not found','Permission denied') if phrase.lower() in content.lower()]
 for target in (plist,data,root):
  for component in (target,*target.parents):
   if component==task.parent:break
   info=component.lstat()
   if stat.S_ISLNK(info.st_mode) or info.st_uid!=UID or info.st_dev!=task.stat().st_dev: raise RuntimeError('Task component identity differs')
 if plist.is_symlink() or not plist.is_file() or not plist.resolve().is_relative_to(task): raise RuntimeError('Task plist unsafe')
 spec=plistlib.loads(plist.read_bytes()); args=spec.get('ProgramArguments')
 if spec.get('Label')!=LABEL or not isinstance(args,list) or not all(isinstance(x,str) for x in args) or not any(str(root) in x for x in args) or not any(str(data) in x for x in args): raise RuntimeError('Task plist action differs')
 service=print_service(LABEL)
 if service.returncode: raise RuntimeError('Task service is not loaded')
 head,sep,rest=service.stdout.partition('\targuments = {\n'); paths=re.findall(r'(?m)^[ \t]*path = ([^\r\n]+?)[ \t]*$',head)
 if paths!=[str(plist)] or not sep or not rest.startswith(''.join('\t\t'+x+'\n' for x in args)+'\t}\n'): raise RuntimeError('Loaded action differs')
 # No PID is required: launchd reports spawn-scheduled and may respawn
 # between observations. Full loaded action and plist identity authorize only
 # this dedicated task job; never signal individual processes.
 if plist.stat().st_uid!=UID: raise RuntimeError('Task plist owner differs')
 pid_match=re.search(r'(?m)^\s*pid\s*=\s*(\d+)\s*$',service.stdout)
 pid=int(pid_match.group(1)) if pid_match else None
 if pid is None:raise RuntimeError('Expected live task service PID absent')
 ps=subprocess.run(['/bin/ps','-ww','-p',str(pid),'-o','uid=,lstart=,command='],capture_output=True,text=True,timeout=20,check=True).stdout.strip()
 if not ps.startswith('503 ') or str(task) not in ps:raise RuntimeError('Service process ownership differs')
 realms=data/'runtime/realms'
 if realms.is_symlink() or not realms.is_dir() or not realms.resolve().is_relative_to(data): raise RuntimeError('Task realms unsafe')
 candidates=[p for p in realms.iterdir() if p.is_dir() and not p.is_symlink()]
 if len(candidates)!=1: raise RuntimeError('Task realm identity ambiguous')
 lock=candidates[0]/'owner.lock'
 if lock.is_symlink() or not lock.is_file() or not lock.resolve().is_relative_to(data): raise RuntimeError('Task owner lock unsafe')
 result['status']='stop-intent'; result['pid']=pid; save()
 intent=task/'evidence/recovery-intent.json'; tmp=intent.with_suffix('.tmp')
 with tmp.open('x') as f: json.dump(result,f); f.flush(); os.fsync(f.fileno())
 os.replace(tmp,intent)
 dfd=os.open(intent.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
 try: os.fsync(dfd)
 finally: os.close(dfd)
 service2=print_service(LABEL); head2,sep2,rest2=service2.stdout.partition('\targuments = {\n')
 if service2.returncode or re.findall(r'(?m)^[ \t]*path = ([^\r\n]+?)[ \t]*$',head2)!=[str(plist)] or not sep2 or not rest2.startswith(''.join('\t\t'+x+'\n' for x in args)+'\t}\n'): raise RuntimeError('Loaded action changed')
 changed=True
 subprocess.run(['/bin/launchctl','disable',f'gui/{UID}/{LABEL}'],check=True,timeout=30)
 subprocess.run(['/bin/launchctl','bootout',f'gui/{UID}/{LABEL}'],check=True,timeout=120)
 deadline=time.monotonic()+120
 while not absent(LABEL) or task_pids():
  if time.monotonic()>deadline: raise RuntimeError('Task processes or service remain')
  time.sleep(1)
 fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)
 try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); fcntl.flock(fd,fcntl.LOCK_UN)
 finally: os.close(fd)
 result['owner_lock']='free'; result['task_processes']=[]; result['status']='stopped'; save()
except Exception as exc:
 result['status']='failed'; result['error_type']=type(exc).__name__; result['error']='recovery failed; inspect runner-owned private evidence'; save()
finally:
 if changed:
  try:
   if not absent(LABEL) or not absent(OTHER): raise RuntimeError('Service absence unverified')
   subprocess.run(['/bin/launchctl','enable',f'gui/{UID}/{LABEL}'],check=True,timeout=30)
   final=states(); result['final_label']=final[LABEL]; result['final_r1_label']=final[OTHER]
   if final[LABEL] not in ('enabled','false','not-listed'): raise RuntimeError('Label restore unverified')
  except Exception as exc: result['restore_error_type']=type(exc).__name__; result['status']='failed'
 save(); print(json.dumps(result,sort_keys=True))
if result['status']!='stopped' or result.get('final_label') not in ('enabled','false','not-listed'): raise SystemExit(1)
