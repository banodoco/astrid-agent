import fcntl,hashlib,json,os,plistlib,re,sqlite3,subprocess,sys,time,traceback,uuid
from pathlib import Path
base=Path(sys.argv[1]); driver=base/'driver'; run=base/'run'; data=run/'data'; home=run/'home'; root=home/'.local/share/astrid'; evidence=base/'evidence'; evidence.mkdir(); run.mkdir(mode=0o700)
sys.path.insert(0,str(driver/'scripts'))
import ci_journey as j
import journey_version_jumps as v
def runner_owners(data):
 p=subprocess.run(['/bin/ps','-ww','-u',str(os.getuid()),'-o','pid=,command='],capture_output=True,text=True,timeout=20,check=True)
 rows=[]
 for line in p.stdout.splitlines():
  m=re.match(r'^\s*(\d+)\s+(.*)$',line)
  if m: rows.append([int(m.group(1)),m.group(2)])
  elif rows: rows[-1][1]+='\n'+line
 return [pid for pid,cmd in rows if j._runtime_owner_command(cmd)]
j.runtime_processes=runner_owners
with (run/'tst-b1-owner.json').open('x',encoding='utf-8') as owner: json.dump({'schema_version':1,'run_id':uuid.uuid4().hex,'root':str(root),'data_root':str(data)},owner)
status={'stages':{},'outcome':'not-run','cleanup':'unverified'}
def save(): (evidence/'result.json').write_text(json.dumps(status,indent=2,sort_keys=True)+'\n')
def done(name): status['stages'][name]='pass'; save(); print('STAGE '+name+' pass',flush=True)
def absent(label):
 p=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True,text=True,timeout=20)
 missing=f'Could not find service "{label}" in domain for user gui: {os.getuid()}'
 return p.returncode==113 and p.stderr.strip().replace('\r\n','\n') in (missing,'Bad request.\n'+missing)
def label_states():
 p=subprocess.run(['/bin/launchctl','print-disabled',f'gui/{os.getuid()}'],capture_output=True,text=True,check=True,timeout=20)
 if 'disabled services' not in p.stdout: raise RuntimeError('Launchd state unavailable')
 result={}
 for label in ('haus.astrid.runtime','haus.astrid.runtime.r1'):
  m=re.search(r'(?m)^\s*"'+re.escape(label)+r'"\s*=>\s*(\w+)\s*$',p.stdout); result[label]=m.group(1) if m else 'not-listed'
 return result
def prove(label,service_data):
 plist=home/'Library/LaunchAgents'/(label+'.plist')
 if plist.is_symlink() or not plist.is_file() or not plist.resolve().is_relative_to(home): raise RuntimeError('Owned plist unavailable')
 spec=plistlib.loads(plist.read_bytes()); args=spec.get('ProgramArguments') or []
 if spec.get('Label')!=label or not isinstance(args,list) or not any(str(root) in str(x) for x in args) or not any(str(service_data) in str(x) for x in args): raise RuntimeError('Service action differs from owned roots')
 p=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True,text=True,check=True,timeout=20)
 loaded=re.search(r'(?m)^\s*path\s*=\s*(.+?)\s*$',p.stdout)
 if loaded is None or loaded.group(1)!=str(plist): raise RuntimeError('Loaded service path differs')
 discovery=j.healthy(service_data); pid=int(discovery['pid']); birth=j.process_birth(pid)
 if not birth or not re.search(r'(?m)^\s*pid\s*=\s*'+str(pid)+r'\s*$',p.stdout): raise RuntimeError('Loaded service PID differs')
 ps=subprocess.run(['/bin/ps','-ww','-p',str(pid),'-o','uid=,command='],capture_output=True,text=True,timeout=20,check=True).stdout.strip()
 if not ps.startswith(str(os.getuid())+' ') or str(service_data) not in ps: raise RuntimeError('Service process identity differs')
 return pid,birth,j.workspace_root(service_data)
def lock_free(realm):
 lock=realm/'owner.lock'
 if lock.is_symlink() or not lock.is_file() or not lock.resolve().is_relative_to(run): raise RuntimeError('Realm lock identity unsafe')
 fd=os.open(lock,os.O_RDWR|os.O_NOFOLLOW)
 try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); fcntl.flock(fd,fcntl.LOCK_UN)
 finally: os.close(fd)
def stop_owned(label,service_data):
 pid,birth,realm=prove(label,service_data)
 status['stages'][label+'-disable-intent']='recorded'; save()
 subprocess.run(['/bin/launchctl','disable',f'gui/{os.getuid()}/{label}'],check=True,timeout=30)
 subprocess.run(['/bin/launchctl','bootout',f'gui/{os.getuid()}/{label}'],check=True,timeout=120)
 deadline=time.monotonic()+120
 while j.process_birth(pid)==birth:
  if time.monotonic()>deadline: raise RuntimeError('Task owner did not exit')
  time.sleep(1)
 lock_free(realm)
 if not absent(label): raise RuntimeError('Task service remains loaded')
 return realm
def tree(path,depth=0):
 rows=[]; entries=sorted(path.iterdir())
 if len(entries)>2000: raise RuntimeError('Program tree exceeds bound')
 for item in entries:
  row={'name':item.name,'kind':'symlink' if item.is_symlink() else 'directory' if item.is_dir() else 'file' if item.is_file() else 'other'}
  if row['kind']=='directory' and depth<1: row['children']=tree(item,depth+1)
  rows.append(row)
 return rows
def filemap(path):
 result={}
 for item in path.rglob('*'):
  if item.is_symlink(): raise RuntimeError('Source contains symlink')
  if item.is_file():
   digest=hashlib.sha256()
   with item.open('rb') as f:
    for chunk in iter(lambda:f.read(1024*1024),b''): digest.update(chunk)
   result[item.relative_to(path).as_posix()]=digest.hexdigest()
   if len(result)>10000: raise RuntimeError('Source evidence exceeds bound')
 return result
initial=None; upgrade_attempted=False; r1data=Path(str(data)+'-r1'); manual=r1data; realm=None
try:
 initial=label_states()
 if any(value not in ('enabled','false','not-listed') for value in initial.values()): raise RuntimeError('A service label was already disabled')
 if not all(absent(label) for label in initial): raise RuntimeError('Pre-existing task label loaded')
 for label in initial:
  plist=home/'Library/LaunchAgents'/(label+'.plist')
  if plist.exists() or plist.is_symlink(): raise RuntimeError('Pre-existing task plist')
 done('initial-service-preflight')
 agent_fixture=base/'agent/tests/fixtures/release0'; fixture=base/'driver/tests/fixtures/release0'
 if (agent_fixture/'SHA256SUMS').read_bytes()!=(fixture/'SHA256SUMS').read_bytes(): raise RuntimeError('Agent and driver fixtures differ')
 fixture,oldmanifest,_=v.verified_release0_fixture(fixture); done('r0-fixture')
 manifest=base/'app/public/install-manifest.json'; raw=manifest.read_bytes(); pins=json.loads(raw)
 if hashlib.sha256(raw).hexdigest()!='861ede0410fb615b28c76b5e3736530e59747d79ce524d6920b9f4dffab5b45e' or pins['agent']['sha']!='a052829cfe2f3725b1c2cf608974c10faf8eb99f' or pins['runtime']['sha']!='9fc53b771134940aaac901e4cfc5d03c1db1a20a': raise RuntimeError('App manifest tuple differs')
 (evidence/'app-manifest.json').write_bytes(raw); status['app_manifest_sha256']=hashlib.sha256(raw).hexdigest(); status['app_sha']='aacad4a59464a110b0e9875d4b2ae8ca3de92a57'; done('candidate-manifest')
 with j.isolated_environment(run,root,data):
  v.bootstrap_release0(fixture,oldmanifest,'sh',evidence/'r0-install',root,data); done('r0-install')
  j.wait_ready(data); oldpath,_,old=j.installation(root,data)
  if v.installed_release(old)!=0: raise RuntimeError('Installed release is not R0')
  prove('haus.astrid.runtime',data); done('r0-owner-identity')
  v.direct_workspace(root,old,data,evidence/'seed','seed',{'project':'ws06-r0-workspace','generation':'release0','task_id':None,'create_project':True}); done('r0-seed')
  realm=j.workspace_root(data); prove('haus.astrid.runtime',data)
  if not absent('haus.astrid.runtime.r1'): raise RuntimeError('R1 label became occupied')
  status['stages']['upgrade-intent']='recorded'; save(); upgrade_attempted=True
  env=dict(os.environ); env['BROWSER']=f'"{sys.executable}" -c "pass" %s'
  cmd=['/bin/sh',str(base/'app/public/install'),'--manifest',str(manifest),'--root',str(root),'--data-root',str(data)]
  with (evidence/'upgrade-bootstrap.log').open('wb') as output:
   process=subprocess.run(cmd,env=env,cwd=base/'app',stdout=output,stderr=subprocess.STDOUT,timeout=3600)
  status['stages']['upgrade']={'exit':process.returncode}; save()
  if process.returncode!=0: raise RuntimeError('R1 public upgrade failed')
  marker=j.read_json(root/'install.json')
  if marker.get('r1_data_root')!=str(r1data): raise RuntimeError('R1 data selection differs')
  path,_,installed=j.installation(root,data)
  if v.installed_release(installed)!=1: raise RuntimeError('Selected version is not R1')
  selected=Path(installed['root'])/'manifest.json'; selected_bytes=selected.read_bytes(); selected_json=json.loads(selected_bytes)
  if any(selected_json.get(k)!=pins.get(k) for k in ('agent','runtime')): raise RuntimeError('Installed R1 pins differ')
  (evidence/'installed-r1-manifest.json').write_bytes(selected_bytes); (evidence/'installed-r1-current.txt').write_bytes((root/'current').read_bytes())
  (evidence/'installed-r1-program-tree.json').write_text(json.dumps(tree(root),indent=2)+'\n'); status['installed_manifest_sha256']=hashlib.sha256(selected_bytes).hexdigest()
  upgrade_receipt=r1data/'adopt-receipt.r1.json'
  if upgrade_receipt.is_symlink() or not upgrade_receipt.is_file() or not upgrade_receipt.resolve().is_relative_to(r1data): raise RuntimeError('Upgrade adoption receipt unavailable')
  (evidence/'upgrade-adopt-receipt.json').write_bytes(upgrade_receipt.read_bytes()); done('installed-r1-inventory')
  if not absent('haus.astrid.runtime'): raise RuntimeError('R0 service remained loaded after upgrade')
  lock_free(realm); done('r0-stopped-lock-free')
  stop_owned('haus.astrid.runtime.r1',r1data); done('r1-stopped-lock-free')
  database=realm/'realm.sqlite3'
  if database.is_symlink() or not database.is_file() or not database.resolve().is_relative_to(data): raise RuntimeError('R0 database unsafe')
  connection=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
  try: rows=connection.execute('SELECT id FROM realm').fetchall()
  finally: connection.close()
  if len(rows)!=1 or not isinstance(rows[0][0],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',rows[0][0]): raise RuntimeError('R0 realm ID ambiguous')
  realm_id=rows[0][0]; before=filemap(realm); lock_free(realm)
  interp=Path(installed['python']); env=dict(os.environ); env.update(installed['env']); env.pop('ASTRID_RUNTIME_DATA_ROOT',None)
  cli=[str(interp),'-I','-m','astrid_runtime']
  def command(name,args):
   p=subprocess.run([*cli,*args],env=env,cwd=base/'runtime',capture_output=True,text=True,timeout=1200)
   (evidence/(name+'.stdout.json')).write_text(p.stdout); (evidence/(name+'.stderr.txt')).write_text(p.stderr)
   status['stages'][name]={'exit':p.returncode}; save(); return p
  adopt=command('adopt',['workspace','adopt','--data-root',str(manual),'--from',str(data),'--realm-id',realm_id,'--read-only-source','--no-start','--json'])
  target=manual/'runtime/realms'/realm_id
  if target.is_dir() and not target.is_symlink():
   inspect=command('inspect',['workspace','inspect','--data-root',str(manual),'--expected-realm-id',realm_id,'--json'])
   after=filemap(realm); status['source_unchanged']=before==after
   source_cas=filemap(realm/'cas/sha256'); target_cas=filemap(target/'cas/sha256'); status['cas']={'identical':source_cas==target_cas,'source_sha256':source_cas,'target_sha256':target_cas}
   lock_free(realm); save()
  else: status['stages']['inspect']='not-run-target-missing'; status['cas']='not-run-target-missing'
  status['outcome']='pass' if adopt.returncode==0 and status['stages'].get('inspect',{}).get('exit')==0 and status.get('source_unchanged') and isinstance(status.get('cas'),dict) and status['cas']['identical'] and bool(status['cas']['source_sha256']) else 'failed'
except Exception as exc:
 status['outcome']='failed'; status['error_type']=type(exc).__name__; status['error']=str(exc); traceback.print_exc()
finally:
 try:
  with j.isolated_environment(run,root,data):
   for label,service_data in (('haus.astrid.runtime.r1',r1data),('haus.astrid.runtime',data)):
    if not absent(label): stop_owned(label,service_data)
   if not all(absent(label) for label in ('haus.astrid.runtime','haus.astrid.runtime.r1')): raise RuntimeError('Service absence unverified')
   if initial is not None:
    current=label_states()
    for label,prior in initial.items():
     if current[label] in ('disabled','true') and prior in ('enabled','false','not-listed'):
      subprocess.run(['/bin/launchctl','enable',f'gui/{os.getuid()}/{label}'],check=True,timeout=30)
    if any(x not in ('enabled','false','not-listed') for x in label_states().values()): raise RuntimeError('Launchd restoration unverified')
   status['cleanup']='task-owned services stopped; launchd state restored'
 except Exception as exc: status['cleanup']='unverified'; status['cleanup_error']=str(exc)
 for label,log in (('installer',home/'Library/Logs/Astrid Runtime/install.log'),('r1-runtime',r1data/'runtime/runtime.log')):
  if log.is_file() and not log.is_symlink() and log.resolve().is_relative_to(base):
   with log.open('rb') as f:
    f.seek(0,2); f.seek(max(0,f.tell()-100000)); (evidence/(label+'-tail.txt')).write_bytes(f.read())
 save()
