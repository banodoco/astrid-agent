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

disabled_by_us=False
r1=run/'r1-data'; realm=None; realm_id=None
try:
 agent_fixture=base/'agent/tests/fixtures/release0'
 driver_fixture=base/'driver/tests/fixtures/release0'
 if (agent_fixture/'SHA256SUMS').read_bytes()!=(driver_fixture/'SHA256SUMS').read_bytes(): raise RuntimeError('Agent and driver fixture manifests differ')
 fixture, oldmanifest, receipt=v.verified_release0_fixture(driver_fixture); done('r0-fixture-verification')
 with j.isolated_environment(run,root,data):
  v.bootstrap_release0(fixture,oldmanifest,'sh',evidence/'release0',root,data); done('r0-install')
  j.wait_ready(data); oldpath,_,old=j.installation(root,data)
  if v.installed_release(old)!=0: raise RuntimeError('Installed release is not R0')
  prove_owned(); done('r0-service-identity')
  keys=v.direct_workspace(root,old,data,evidence/'seed','seed',{'project':'ws02-r0-workspace','generation':'release0','task_id':None,'create_project':True}); done('seed')
  health=j.healthy(data); realm_id=str(health['realm_id']); realm=j.workspace_root(data)
  if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',realm_id): raise RuntimeError('Unexpected realm id')
  prove_owned()
  query=subprocess.run(['/bin/launchctl','print-disabled',f'gui/{os.getuid()}'],capture_output=True,text=True,check=True,timeout=30)
  if 'disabled services' not in query.stdout: raise RuntimeError('LaunchAgent disabled state not inspectable')
  found=re.search(r'(?m)^\s*"haus\.astrid\.runtime"\s*=>\s*(\w+)\s*$',query.stdout)
  if found and found.group(1) not in ('enabled','false'): raise RuntimeError('LaunchAgent was previously disabled or state unknown')
  disabled_by_us=True
  subprocess.run(['/bin/launchctl','disable',f'gui/{os.getuid()}/haus.astrid.runtime'],check=True,timeout=30); done('launchagent-disable')
  stop_owned(); done('r0-stopped-and-lock-free')
  venv=base/'r1-venv'; py=Path(sys.executable); j.run([py,'-m','venv',venv],evidence/'venv.log',timeout=300); interp=venv/'bin/python'
  j.run([interp,'-m','pip','install','-e',base/'runtime','-e',base/'runtime/packages/python','-e',base/'agent'],evidence/'r1-install.log',timeout=1200); done('r1-runtime-install')
  profile=base/'r1-profile.json'; profile.write_text(json.dumps({'profile':'astrid','runtime_checkout':str(base/'runtime'),'source_checkout':str(base/'agent'),'runtime_environment':str(venv)}))
  os.chmod(profile,0o600); env=dict(os.environ); env['ASTRID_RUNTIME_SOURCE_MANIFEST']=str(profile)
  # Use an unoccupied, non-default port for the task-owned R1 door.
  import socket
  with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
  if port==17340: raise RuntimeError('Unexpected R1 door port')
  env['ASTRID_RUNTIME_DOOR_PORT']=str(port)
  cli=venv/'bin/astrid-runtime'
  def command(name,args):
   result=subprocess.run([str(cli),*args],env=env,cwd=str(base/'runtime'),capture_output=True,text=True,timeout=1200)
   (evidence/(name+'.stdout.json')).write_text(result.stdout); (evidence/(name+'.stderr.txt')).write_text(result.stderr)
   status['stages'][name]={'exit':result.returncode}; save(); return result
  adopt=command('adopt',['workspace','adopt','--data-root',str(r1),'--from',str(realm),'--realm-id',realm_id,'--json'])
  if adopt.returncode==0: done('adopt-complete')
  target=r1/'runtime/realms'/realm_id
  if target.is_dir() and not target.is_symlink():
   inspect=command('inspect',['workspace','inspect','--data-root',str(r1),'--realm-root',str(target),'--json'])
   def cas_map(path):
    result={}; rootcas=path/'cas/sha256'
    for item in rootcas.rglob('*') if rootcas.exists() else ():
     if item.is_symlink(): raise RuntimeError('CAS contains symlink')
     if item.is_file(): result[item.relative_to(rootcas).as_posix()]=hashlib.sha256(item.read_bytes()).hexdigest()
    return result
   before=cas_map(realm); after=cas_map(target); status['cas']={'source_count':len(before),'target_count':len(after),'identical':before==after}; save()
  else: status['stages']['inspect']='not-run-target-missing'; status['cas']='not-run-target-missing'
  status['outcome']='pass' if adopt.returncode==0 and status['stages'].get('inspect',{}).get('exit')==0 and isinstance(status.get('cas'),dict) and status['cas']['identical'] and status['cas']['source_count']>0 else 'failed'
except Exception as exc:
 status['outcome']='failed'; status['error_type']=type(exc).__name__; status['error']=str(exc); traceback.print_exc()
finally:
 try:
  discovery=r1/'runtime/discovery.json'
  if discovery.is_file() and not discovery.is_symlink():
   value=json.loads(discovery.read_text()); pid=int(value['pid']); birth=j.process_birth(pid)
   if birth:
    ps=subprocess.run(['/bin/ps','-ww','-p',str(pid),'-o','uid=,command='],capture_output=True,text=True,check=True,timeout=20).stdout.strip()
    if not ps.startswith(str(os.getuid())+' ') or str(r1) not in ps: raise RuntimeError('R1 owner identity not proven')
    result=subprocess.run([str(base/'r1-venv/bin/astrid-runtime'),'down','--data-root',str(r1),'--json'],capture_output=True,text=True,timeout=180,env=env)
    (evidence/'r1-down.stdout.json').write_text(result.stdout); (evidence/'r1-down.stderr.txt').write_text(result.stderr)
    if result.returncode!=0: raise RuntimeError('R1 graceful down failed')
    end=time.monotonic()+120
    while j.process_birth(pid)==birth:
     if time.monotonic()>end: raise RuntimeError('R1 owner did not exit')
     time.sleep(1)
  state=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/haus.astrid.runtime'],capture_output=True,text=True,timeout=20)
  if state.returncode==0:
   with j.isolated_environment(run,root,data): stop_owned()
   state=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/haus.astrid.runtime'],capture_output=True,text=True,timeout=20)
  missing='Could not find service "haus.astrid.runtime" in domain for user gui: '+str(os.getuid())
  if state.returncode!=113 or state.stderr.strip().replace('\r\n','\n') not in (missing,'Bad request.\n'+missing): raise RuntimeError('Service absence unverified')
  if disabled_by_us:
   subprocess.run(['/bin/launchctl','enable',f'gui/{os.getuid()}/haus.astrid.runtime'],check=True,timeout=30); disabled_by_us=False
  if runner_owners(data): raise RuntimeError('Runner Runtime owner still present')
  status['cleanup']='task-owned services stopped; launchd state restored'
 except Exception as exc:
  status['cleanup']='unverified'; status['cleanup_error']=str(exc)
  if disabled_by_us:
   try:
    current=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/haus.astrid.runtime'],capture_output=True,text=True,timeout=20)
    missing='Could not find service \"haus.astrid.runtime\" in domain for user gui: '+str(os.getuid())
    if current.returncode!=113 or current.stderr.strip().replace('\r\n','\n') not in (missing,'Bad request.\n'+missing): raise RuntimeError('LaunchAgent absence unverified')
    subprocess.run(['/bin/launchctl','enable',f'gui/{os.getuid()}/haus.astrid.runtime'],check=True,timeout=30); disabled_by_us=False; status['launchd_restore']='enabled'
   except Exception as restore_exc: status['launchd_restore']='unverified'; status['launchd_restore_error']=str(restore_exc)
 save()
