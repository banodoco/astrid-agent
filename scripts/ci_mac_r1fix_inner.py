import fcntl, hashlib, json, os, plistlib, re, subprocess, sys, time, traceback, uuid
from pathlib import Path
base=Path(sys.argv[1]); driver=base/'driver'; app=base/'candidate-app'; run=base/'run'; data=run/'data'; home=run/'home'; root=home/'.local/share/astrid'; evidence=base/'evidence'; evidence.mkdir()
sys.path.insert(0,str(driver/'scripts'))
import ci_journey as j
import journey_version_jumps as v
def runner_owners(data):
 p=subprocess.run(['/bin/ps','-ww','-u',str(os.getuid()),'-o','pid=,command='],capture_output=True,text=True,timeout=20,check=True)
 rows=[]
 for line in p.stdout.splitlines():
  match=re.match(r'^\s*(\d+)\s+(.*)$',line)
  if match: rows.append([int(match.group(1)),match.group(2)])
  elif rows: rows[-1][1]+='\n'+line
 return [pid for pid,cmd in rows if j._runtime_owner_command(cmd)]
j.runtime_processes=runner_owners
run.mkdir(mode=0o700)
with (run/'tst-b1-owner.json').open('x',encoding='utf-8') as owner:
 json.dump({'schema_version':1,'run_id':uuid.uuid4().hex,'root':str(root),'data_root':str(data)},owner); owner.write('\n')
status={'stages':{},'outcome':'not-run','cleanup':'unverified'}
def done(name):
 status['stages'][name]='pass'; print('STAGE '+name+' pass',flush=True)
def save(): (evidence/'result.json').write_text(json.dumps(status,indent=2,sort_keys=True)+'\n')
def prove_owned():
 service=j.assert_direct_service_ownership(root,data)
 plist=Path(service['service_path']); spec=plistlib.loads(plist.read_bytes()); args=spec.get('ProgramArguments') or []
 if not isinstance(args,list) or not any(str(root) in str(x) for x in args) or not any(str(data) in str(x) for x in args): raise RuntimeError('LaunchAgent program or data root mismatch')
 p=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/haus.astrid.runtime'],capture_output=True,text=True,timeout=30,check=True)
 loaded_path=re.search(r'(?m)^\s*path\s*=\s*(.+?)\s*$',p.stdout)
 if loaded_path is None or loaded_path.group(1)!=str(plist): raise RuntimeError('Loaded LaunchAgent plist mismatch')
 health=j.healthy(data); pid=int(health['pid']); birth=j.process_birth(pid)
 if not birth or not re.search(r'(?m)^\s*pid\s*=\s*'+str(pid)+r'\s*$',p.stdout): raise RuntimeError('Loaded service PID differs from healthy owner')
 if j.runtime_processes(data)!=[pid]: raise RuntimeError('Runner owner census differs')
 return pid,birth,plist

def stop_owned():
 pid,birth,plist=prove_owned(); realm=j.workspace_root(data)
 j.run(['/bin/launchctl','bootout',f'gui/{os.getuid()}/haus.astrid.runtime'],evidence/'stop.log',timeout=120)
 deadline=time.monotonic()+120
 while j.process_birth(pid)==birth:
  if time.monotonic()>deadline: raise RuntimeError('Stopped owner did not exit')
  time.sleep(1)
 lock=realm/'owner.lock'
 if lock.is_symlink() or not lock.is_file() or not lock.resolve().is_relative_to(data): raise RuntimeError('Realm owner lock identity not proven')
 fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)
 try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); fcntl.flock(fd,fcntl.LOCK_UN)
 finally: os.close(fd)
 return realm

try:
 manifest=app/'public/install-manifest.json'; raw=manifest.read_bytes(); value=json.loads(raw)
 if hashlib.sha256(raw).hexdigest()!='3b49b1f930b3ee71683cb3b0ec462ebae6dea717434297446ddadb60c17226b1' or value['agent']['sha']!='30761f4a3916ce6e5822669dd5865f65a4e6654a' or value['runtime']['sha']!='de98a5aa44768419656bfeffcdaa1b2eda9e9845': raise RuntimeError('Candidate manifest mismatch')
 done('candidate-manifest-pins')
 fixture, oldmanifest, receipt=v.verified_release0_fixture(driver/'tests/fixtures/release0'); done('r0-fixture-verification')
 with j.isolated_environment(run,root,data):
  v.bootstrap_release0(fixture,oldmanifest,'sh',evidence/'release0',root,data); done('r0-install')
  j.wait_ready(data); oldpath,_,old=j.installation(root,data)
  if v.installed_release(old)!=0: raise RuntimeError('Installed release is not R0')
  prove_owned(); done('r0-service-identity')
  j.verify('observe',oldpath,data,evidence/'seed'); done('r0-observe')
  keys=v.direct_workspace(root,old,data,evidence/'seed','seed',{'project':'r1fix-stopped-workspace','generation':'release0','task_id':None,'create_project':True}); done('seed')
  prove_owned(); before=v.direct_workspace(root,old,data,evidence/'snapshot','snapshot',keys); done('snapshot-before')
  selection=json.loads((root.parent/'astrid-install.json').read_text())
  if selection.get('owner')!='haus.astrid.runtime' or selection.get('program_root')!=str(root) or selection.get('data_root')!=str(data): raise RuntimeError('Selection does not identify this R0')
  realm=stop_owned(); done('r0-stopped-and-lock-free')
  if hashlib.sha256(manifest.read_bytes()).hexdigest()!='3b49b1f930b3ee71683cb3b0ec462ebae6dea717434297446ddadb60c17226b1': raise RuntimeError('Candidate changed before update')
  previous=os.getcwd()
  try:
   os.chdir(app); j.run(['/bin/sh','public/install','--manifest','public/install-manifest.json'],evidence/'update-bootstrap.log',env=dict(os.environ),timeout=3600)
  finally: os.chdir(previous)
  done('candidate-command')
  j.wait_ready(data); path,_,new=j.installation(root,data); actual=j.read_json(Path(new['root'])/'manifest.json')
  if any(actual.get(k)!=value.get(k) for k in ('agent','runtime')): raise RuntimeError('Installed manifest pins differ')
  if v.installed_release(new)!=1: raise RuntimeError('Installed release is not R1')
  prove_owned(); done('r1-service-and-pins')
  j.verify('update',path,data,evidence/'verification',before=evidence/'seed/verify-observe.json'); done('r1-update-verification')
  j.verify('install',path,data,evidence/'verification'); done('r1-install-verification')
  after=v.direct_workspace(root,new,data,evidence/'after','snapshot',keys); v.assert_preserved(before,after); done('workspace-survives')
  status['outcome']='pass'
except Exception as exc:
 status['outcome']='failed'; status['error_type']=type(exc).__name__; status['error']=str(exc); traceback.print_exc()
finally:
 try:
  state=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/haus.astrid.runtime'],capture_output=True,text=True,timeout=20)
  if state.returncode == 0:
   with j.isolated_environment(run,root,data): stop_owned()
   state=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/haus.astrid.runtime'],capture_output=True,text=True,timeout=20)
  missing='Could not find service \"haus.astrid.runtime\" in domain for user gui: '+str(os.getuid())
  if state.returncode!=113 or state.stderr.strip().replace('\r\n','\n') not in (missing,'Bad request.\n'+missing): raise RuntimeError('Service absence unverified')
  if runner_owners(data): raise RuntimeError('Runner Runtime owner still present')
  status['cleanup']='owned service stopped; workspace retained'
 except Exception as exc: status['cleanup']='unverified'; status['cleanup_error']=str(exc)
 log=home/'Library/Logs/Astrid Runtime/install.log'
 if log.is_file() and not log.is_symlink() and log.resolve().is_relative_to(home):
  (evidence/'install-log-tail.txt').write_bytes(log.read_bytes()[-100000:])
 save()
