import fcntl, hashlib, json, os, plistlib, re, sqlite3, subprocess, sys, time, traceback, uuid
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

def stop_owned(name="stop.log"):
 pid,birth,plist=prove_owned(); realm=j.workspace_root(data)
 j.run(['/bin/launchctl','bootout',f'gui/{os.getuid()}/haus.astrid.runtime'],evidence/name,timeout=120)
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
service_run=run
r1=Path(str(data)+'-r1'); realm=None; realm_id=None
try:
 agent_fixture=base/'agent/tests/fixtures/release0'
 driver_fixture=base/'driver/tests/fixtures/release0'
 if (agent_fixture/'SHA256SUMS').read_bytes()!=(driver_fixture/'SHA256SUMS').read_bytes(): raise RuntimeError('Agent and driver fixture manifests differ')
 fixture, oldmanifest, receipt=v.verified_release0_fixture(driver_fixture); done('r0-fixture-verification')
 app_manifest=base/'app/public/install-manifest.json'; app_bytes=app_manifest.read_bytes()
 (evidence/'app-publishing-manifest.json').write_bytes(app_bytes)
 status['app_publishing_sha']='1daa20c6051b82a481d1d96627e76bb93609d6a5'
 pins=json.loads(app_bytes)
 if hashlib.sha256(app_bytes).hexdigest()!='949c4dd06cd23a705f0714f74cce4423eaed992d34c9931b59968fea2f8085df' or pins['agent']['sha']!='45a1af120aa4c718e57d051a2abf6d69b78fe7f7' or pins['runtime']['sha']!='9fc53b771134940aaac901e4cfc5d03c1db1a20a': raise RuntimeError('Candidate manifest mismatch')
 status['app_manifest_sha256']=hashlib.sha256(app_bytes).hexdigest(); done('app-manifest-captured')
 with j.isolated_environment(run,root,data):
  v.bootstrap_release0(fixture,oldmanifest,'sh',evidence/'release0',root,data); done('r0-install')
  j.wait_ready(data); oldpath,_,old=j.installation(root,data)
  if v.installed_release(old)!=0: raise RuntimeError('Installed release is not R0')
  installed_manifest=Path(old['root'])/'manifest.json'
  if installed_manifest.is_symlink() or not installed_manifest.is_file() or not installed_manifest.resolve().is_relative_to(root): raise RuntimeError('Installed manifest identity unsafe')
  manifest_bytes=installed_manifest.read_bytes(); (evidence/'installed-r0-manifest.json').write_bytes(manifest_bytes)
  status['installed_manifest_sha256']=hashlib.sha256(manifest_bytes).hexdigest()
  def tree(path,depth=0):
   rows=[]
   entries=sorted(path.iterdir())
   if len(entries)>2000: raise RuntimeError('Program tree exceeds evidence bound')
   for item in entries:
    row={'name':item.name,'kind':'symlink' if item.is_symlink() else 'directory' if item.is_dir() else 'file' if item.is_file() else 'other'}
    if row['kind']=='directory' and depth<1: row['children']=tree(item,depth+1)
    rows.append(row)
   return rows
  (evidence/'installed-program-tree.json').write_text(json.dumps(tree(root),indent=2)+'\n'); done('installed-program-inventory')
  prove_owned(); done('r0-service-identity')
  keys=v.direct_workspace(root,old,data,evidence/'seed','seed',{'project':'ws02-r0-workspace','generation':'release0','task_id':None,'create_project':True}); done('seed')
  realm=j.workspace_root(data)
  prove_owned()
  query=subprocess.run(['/bin/launchctl','print-disabled',f'gui/{os.getuid()}'],capture_output=True,text=True,check=True,timeout=30)
  if 'disabled services' not in query.stdout: raise RuntimeError('LaunchAgent disabled state not inspectable')
  found=re.search(r'(?m)^\s*"haus\.astrid\.runtime"\s*=>\s*(\w+)\s*$',query.stdout)
  if found and found.group(1) not in ('enabled','false'): raise RuntimeError('LaunchAgent was previously disabled or state unknown')
  disabled_by_us=True
  subprocess.run(['/bin/launchctl','disable',f'gui/{os.getuid()}/haus.astrid.runtime'],check=True,timeout=30); done('launchagent-disable')
  stop_owned(); done('r0-stopped-and-lock-free')
  database=realm/'realm.sqlite3'
  if database.is_symlink() or not database.is_file() or not database.resolve().is_relative_to(data): raise RuntimeError('R0 database identity unsafe')
  connection=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
  try: rows=connection.execute('SELECT id FROM realm').fetchall()
  finally: connection.close()
  if len(rows)!=1 or not isinstance(rows[0][0],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',rows[0][0]): raise RuntimeError('R0 realm identity ambiguous')
  realm_id=rows[0][0]; done('r0-realm-identity')
  # R0 is stopped; install the pinned R1 into its own program/data/HOME.
  state=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/haus.astrid.runtime'],capture_output=True,text=True,timeout=20)
  missing='Could not find service "haus.astrid.runtime" in domain for user gui: '+str(os.getuid())
  if state.returncode!=113 or state.stderr.strip().replace('\r\n','\n') not in (missing,'Bad request.\n'+missing) or runner_owners(data): raise RuntimeError('R0 service absence unverified before R1 install')
  sockcheck=subprocess.run(['/usr/sbin/lsof','-nP','-iTCP:17340','-sTCP:LISTEN'],capture_output=True,text=True,timeout=20)
  if sockcheck.returncode!=1 or sockcheck.stdout.strip() or sockcheck.stderr.strip(): raise RuntimeError('R1 installer port unavailable')
  subprocess.run(['/bin/launchctl','enable',f'gui/{os.getuid()}/haus.astrid.runtime'],check=True,timeout=30); disabled_by_us=False
  old_root,old_data=root,data
  service_run=base/'r1-install'; service_run.mkdir(mode=0o700)
  root=service_run/'program'; data=service_run/'data'
  with (service_run/'tst-b1-owner.json').open('x',encoding='utf-8') as owner:
   json.dump({'schema_version':1,'run_id':uuid.uuid4().hex,'root':str(root),'data_root':str(data)},owner); owner.write('\n')
  with j.isolated_environment(service_run,root,data):
   j.bootstrap(base/'app',app_manifest,'sh','install',evidence/'r1-install',root,data); done('r1-install')
   j.wait_ready(data); r1path,_,installed=j.installation(root,data)
   selected=j.read_json(Path(installed['root'])/'manifest.json')
   if v.installed_release(installed)!=1 or any(selected.get(k)!=pins.get(k) for k in ('agent','runtime')): raise RuntimeError('Installed R1 tuple differs')
   r1_manifest_bytes=(Path(installed['root'])/'manifest.json').read_bytes()
   (evidence/'installed-r1-manifest.json').write_bytes(r1_manifest_bytes); status['installed_r1_manifest_sha256']=hashlib.sha256(r1_manifest_bytes).hexdigest()
   (evidence/'installed-r1-current.txt').write_bytes((root/'current').read_bytes())
   (evidence/'installed-r1-program-tree.json').write_text(json.dumps(tree(root),indent=2)+'\n'); done('installed-r1-inventory')
   prove_owned(); done('r1-service-identity')
   v.direct_workspace(root,installed,data,evidence/'r1-seed','seed',{'project':'ws05-r1-workspace','generation':'release1','task_id':None,'create_project':True}); done('r1-seed')
   prove_owned(); disabled_by_us=True
   subprocess.run(['/bin/launchctl','disable',f'gui/{os.getuid()}/haus.astrid.runtime'],check=True,timeout=30); done('r1-launchagent-disable')
   stop_owned('r1-stop.log'); done('r1-stopped-and-lock-free')
  root,data=old_root,old_data; service_run=run
  # Prove the original source lock remains free immediately before adoption.
  source_fd=os.open(realm/'owner.lock',os.O_RDWR|os.O_NOFOLLOW)
  try: fcntl.flock(source_fd,fcntl.LOCK_EX|fcntl.LOCK_NB); fcntl.flock(source_fd,fcntl.LOCK_UN)
  finally: os.close(source_fd)
  interp=Path(installed['python']); env=dict(os.environ); env.update(installed['env'])
  r1home=base/'r1-install/home'; r1tmp=base/'r1-install/tmp'
  env.update({'HOME':str(r1home),'XDG_CONFIG_HOME':str(r1home/'.config'),'XDG_DATA_HOME':str(r1home/'.local/share'),'XDG_STATE_HOME':str(r1home/'.local/state'),'TMPDIR':str(r1tmp),'TMP':str(r1tmp),'TEMP':str(r1tmp)})
  env.pop('ASTRID_RUNTIME_DATA_ROOT',None); env.pop('ASTRID_RUNTIME_HOME',None)
  # Use an unoccupied, non-default port for the task-owned R1 door.
  import socket
  with socket.socket() as sock: sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
  if port==17340: raise RuntimeError('Unexpected R1 door port')
  env['ASTRID_RUNTIME_DOOR_PORT']=str(port)
  cli=[str(interp),'-I','-m','astrid_runtime']
  def command(name,args):
   result=subprocess.run([*cli,*args],env=env,cwd=str(base/'runtime'),capture_output=True,text=True,timeout=1200)
   (evidence/(name+'.stdout.json')).write_text(result.stdout); (evidence/(name+'.stderr.txt')).write_text(result.stderr)
   status['stages'][name]={'exit':result.returncode}; save(); return result
  adopt=command('adopt',['workspace','adopt','--data-root',str(r1),'--from',str(realm),'--realm-id',realm_id,'--json'])
  if adopt.returncode==0: done('adopt-complete')
  target=r1/'runtime/realms'/realm_id
  if target.is_dir() and not target.is_symlink():
   inspect=command('inspect',['workspace','inspect','--data-root',str(r1),'--expected-realm-id',realm_id,'--json'])
   def cas_map(path):
    result={}; rootcas=path/'cas/sha256'
    for item in rootcas.rglob('*') if rootcas.exists() else ():
     if item.is_symlink(): raise RuntimeError('CAS contains symlink')
     if item.is_file():
      digest=hashlib.sha256()
      with item.open('rb') as stream:
       for chunk in iter(lambda:stream.read(1024*1024),b''): digest.update(chunk)
      result[item.relative_to(rootcas).as_posix()]=digest.hexdigest()
      if len(result)>1000: raise RuntimeError('CAS inventory exceeds evidence bound')
    return result
   before=cas_map(realm); after=cas_map(target); status['cas']={'source_count':len(before),'target_count':len(after),'identical':before==after,'source_sha256':before,'target_sha256':after}; save()
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
    result=subprocess.run([*cli,'down','--data-root',str(r1),'--json'],capture_output=True,text=True,timeout=180,env=env)
    (evidence/'r1-down.stdout.json').write_text(result.stdout); (evidence/'r1-down.stderr.txt').write_text(result.stderr)
    if result.returncode!=0: raise RuntimeError('R1 graceful down failed')
    end=time.monotonic()+120
    while j.process_birth(pid)==birth:
     if time.monotonic()>end: raise RuntimeError('R1 owner did not exit')
     time.sleep(1)
  state=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/haus.astrid.runtime'],capture_output=True,text=True,timeout=20)
  if state.returncode==0:
   with j.isolated_environment(service_run,root,data): stop_owned()
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
 for label,log in (('r0-installer',run/'home/Library/Logs/Astrid Runtime/install.log'),('r1-installer',base/'r1-install/home/Library/Logs/Astrid Runtime/install.log'),('r1-runtime',r1/'runtime/runtime.log')):
  if log.is_file() and not log.is_symlink() and log.resolve().is_relative_to(base):
   with log.open('rb') as stream:
    stream.seek(0,2); stream.seek(max(0,stream.tell()-100000)); (evidence/(label+'-tail.txt')).write_bytes(stream.read())
 save()
