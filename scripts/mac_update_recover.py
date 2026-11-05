#!/usr/bin/env python3
"""Stop only the verified R0 LaunchAgent owned by the failed MAC-UPDATE-01 task."""
import fcntl,hashlib,json,os,plistlib,pwd,re,stat,subprocess,sys,time
from pathlib import Path
UID=503; LABEL='haus.astrid.runtime'; OTHER=LABEL+'.r1'
if os.getuid()!=UID or os.environ.get('RUNNER_NAME')!='hannah-mac' or sys.platform!='darwin': raise SystemExit('Unexpected runner')
home=Path(pwd.getpwuid(UID).pw_dir); base=home/'.astrid-r1fix-stopped'; task=base/'task.17rl0_df'; temp=Path(os.environ['RUNNER_TEMP']); out=temp/'mac-update-stop-audit'
if temp!=home/'actions-runner/_work/_temp' or temp.resolve()!=temp or temp.stat().st_uid!=UID: raise SystemExit('Unsafe temp')
if base.is_symlink() or base.resolve()!=base or task.is_symlink() or task.resolve()!=task or task.stat().st_uid!=UID: raise SystemExit('Unsafe task')
receipt=task/'evidence/result.json'
if receipt.is_symlink() or not receipt.is_file() or hashlib.sha256(receipt.read_bytes()).hexdigest()!='41ecc2069040d12e7686db0c7b7724d955fb1a12c40551a72d8d8537bfb9b263': raise SystemExit('Task receipt provenance differs')
record=json.loads(receipt.read_text())
if record.get('stages',{}).get('service-preflight')!='pass' or record.get('old_pid')!=62859 or record.get('label')!=LABEL or record.get('outcome')!='failed': raise SystemExit('Task initial state provenance differs')
out.mkdir(mode=0o700)
with open(os.environ['GITHUB_ENV'],'a') as fh: fh.write('MAC_STOP_UPLOAD='+str(out)+'\n')
result={'status':'not-run','initial_label':'unknown','final_label':'unknown'}
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
 result['status']='failed'; result['error_type']=type(exc).__name__; result['error']=str(exc); save()
finally:
 if changed:
  try:
   if not absent(LABEL) or not absent(OTHER): raise RuntimeError('Service absence unverified')
   subprocess.run(['/bin/launchctl','enable',f'gui/{UID}/{LABEL}'],check=True,timeout=30)
   final=states(); result['final_label']=final[LABEL]; result['final_r1_label']=final[OTHER]
   if final[LABEL] not in ('enabled','false','not-listed'): raise RuntimeError('Label restore unverified')
  except Exception as exc: result['restore_error']=str(exc); result['status']='failed'
 save(); print(json.dumps(result,sort_keys=True))
if result['status']!='stopped' or result.get('final_label') not in ('enabled','false','not-listed'): raise SystemExit(1)
