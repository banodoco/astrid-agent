#!/usr/bin/env python3
"""Standalone real-Mac install/update diagnostic; never imports ci_journey."""
import fcntl, hashlib, json, os, plistlib, re, subprocess, sys, threading, time, traceback, urllib.request
from pathlib import Path

base=Path(sys.argv[1]); run=base/'run'; home=run/'home'; root=home/'.local/share/astrid'; data=run/'data'; evidence=base/'evidence'; evidence.mkdir(); run.mkdir(mode=0o700)
for p in (home,run/'tmp'): p.mkdir()
port=os.environ['ASTRID_RUNTIME_DOOR_PORT']; labels=('haus.astrid.runtime','haus.astrid.runtime.r1')
env=dict(os.environ); env.update(HOME=str(home),TMPDIR=str(run/'tmp'),TMP=str(run/'tmp'),TEMP=str(run/'tmp'),XDG_DATA_HOME=str(home/'.local/share'),XDG_CONFIG_HOME=str(home/'.config'),XDG_STATE_HOME=str(home/'.local/state'),ASTRID_RUNTIME_DATA_ROOT=str(data),ASTRID_HOST_EXECUTION_PROFILE='installed')
env.pop('RUNNER_TEMP',None); env.pop('PYTHONPATH',None); env.pop('PYTHONHOME',None)
status={'stages':{},'outcome':'failed','cleanup':'unverified','uninstall':'not-run'}; initial={}; owned_label=None; old_pid=None; old_birth=None; observation=[]
def save(): (evidence/'result.json').write_text(json.dumps(status,sort_keys=True,indent=2)+'\n')
def mark(name,value='pass'): status['stages'][name]=value;save();print('STAGE',name,value,flush=True)
def cmd(name,args,timeout=3600,work=None,custom_env=None):
 with (evidence/(name+'.log')).open('wb') as f:
  p=subprocess.run([str(x) for x in args],cwd=work or base,env=custom_env or env,stdout=f,stderr=subprocess.STDOUT,timeout=timeout)
 mark(name,{'exit':p.returncode})
 if p.returncode: raise RuntimeError(name+' failed')
 return (evidence/(name+'.log')).read_text()
def absent(label):
 p=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True,text=True,timeout=20)
 m=f'Could not find service "{label}" in domain for user gui: {os.getuid()}'
 return p.returncode==113 and p.stderr.strip().replace('\r\n','\n') in (m,'Bad request.\n'+m)
def states():
 p=subprocess.run(['/bin/launchctl','print-disabled',f'gui/{os.getuid()}'],capture_output=True,text=True,check=True,timeout=20)
 if 'disabled services' not in p.stdout: raise RuntimeError('launchd disabled state unavailable')
 return {label:(m.group(1) if (m:=re.search(r'(?m)^\s*"'+re.escape(label)+r'"\s*=>\s*(\w+)\s*$',p.stdout)) else 'not-listed') for label in labels}
def birth(pid):
 p=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart='],capture_output=True,text=True,timeout=10)
 return p.stdout.strip() if p.returncode==0 else None
def current():
 marker=json.loads((root/'install.json').read_text()); name=(root/'current').read_text().strip()
 if marker.get('data_root')!=str(data) or not re.fullmatch(r'[A-Za-z0-9._-]+',name): raise RuntimeError('Installed selection outside task')
 path=root/'versions'/name/'version.json'; version=json.loads(path.read_text())
 if path.resolve().parent!=Path(version['root']).resolve() or not path.resolve().is_relative_to(root/'versions'): raise RuntimeError('Installed version outside task')
 if str(version.get('env',{}).get('ASTRID_RUNTIME_DOOR_PORT'))!=port: raise RuntimeError('Installed door port differs')
 return name,version
def healthy():
 req=urllib.request.Request('http://127.0.0.1:'+port+'/api/runtime/v1/health',headers={'Origin':'https://astrid.haus'})
 with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=5) as response: health=json.load(response)
 d=json.loads((data/'runtime/discovery.json').read_text())
 if health.get('status')!='ok' or not health.get('runtime_instance_id') or health['runtime_instance_id']!=d.get('runtime_instance_id'): raise RuntimeError('Health and discovery differ')
 return d
def ready():
 end=time.monotonic()+900
 while time.monotonic()<end:
  try:return healthy()
  except (OSError,ValueError,KeyError,RuntimeError):time.sleep(3)
 raise RuntimeError('Runtime did not become healthy')
def prove(label):
 plist=home/'Library/LaunchAgents'/(label+'.plist')
 if plist.is_symlink() or not plist.is_file() or not plist.resolve().is_relative_to(home):raise RuntimeError('Task plist unavailable')
 spec=plistlib.loads(plist.read_bytes()); args=spec.get('ProgramArguments') or []
 if spec.get('Label')!=label or not any(str(root) in str(x) for x in args) or not any(str(data) in str(x) for x in args): raise RuntimeError('Task service arguments differ')
 out=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{label}'],capture_output=True,text=True,check=True,timeout=20).stdout
 m=re.search(r'(?m)^\s*path\s*=\s*(.+?)\s*$',out)
 if not m or m.group(1)!=str(plist): raise RuntimeError('Loaded plist differs')
 d=healthy(); pid=int(d['pid']); born=birth(pid)
 if not born or not re.search(r'(?m)^\s*pid\s*=\s*'+str(pid)+r'\s*$',out): raise RuntimeError('Loaded PID differs')
 ps=subprocess.run(['/bin/ps','-ww','-p',str(pid),'-o','uid=,command='],capture_output=True,text=True,check=True,timeout=20).stdout.strip()
 if not ps.startswith(str(os.getuid())+' ') or str(data) not in ps:raise RuntimeError('Runtime process differs')
 return pid,born,out
def task(version,tag,create):
 e=dict(env);e.update(version['env']); py=version['python']; proj='mac-update-smoke'
 def cli(name,args,module='astrid',timeout=300):
  return json.loads(cmd(tag+'-'+name,[py,'-I','-m',module,*args],timeout=timeout,custom_env=e))
 if create and cli('create',['projects','create',proj,'--name',proj,'--json']).get('ok') is not True:raise RuntimeError('Project creation failed')
 request={'capability_id':'chiptune.sfx','kind':'executor','project':proj,'inputs':{'kind':'blip','variant':1,'pitch':0,'duration_s':0.1}}
 inv=cli('invoke',[json.dumps(request)],module='astrid.omp_invoke'); refs=inv.get('details',{}).get('astridTaskRefs',[])
 if len(refs)!=1 or not refs[0].get('task_id'):raise RuntimeError('Task invocation failed')
 tid=refs[0]['task_id']; cmd(tag+'-follow',[py,'-I','-m','astrid','tasks','follow',tid,'--project',proj,'--poll-seconds','2','--timeout-seconds','900','--json'],timeout=960,custom_env=e)
 result=cli('show',['tasks','show',tid,'--project',proj,'--json']); row=result.get('data') or {}; outputs={i.get('name'):i for i in (row.get('result') or {}).get('outputs',[])}
 if result.get('ok') is not True or row.get('state')!='succeeded' or not {'sfx','sfx_meta'}<=outputs.keys() or any(not re.fullmatch(r'sha256:[0-9a-f]{64}',outputs[n].get('digest','')) or outputs[n].get('size',0)<=0 for n in ('sfx','sfx_meta')): raise RuntimeError('Task did not materialize outputs')
 return tid
try:
 initial=states()
 if any(v not in ('enabled','false','not-listed') for v in initial.values()) or not all(absent(l) for l in labels): raise RuntimeError('Initial service state unavailable or occupied')
 mark('service-preflight')
 manifest=base/'app/public/install-manifest.json'; pins=json.loads(manifest.read_text()); lock=json.loads((base/'agent/runtime.lock.json').read_text())
 if pins['agent']['sha']!='57cabe57f8294eded12c555d8eb3d5e1f1d20368' or pins['runtime']['sha']!=lock['source_commit']:raise RuntimeError('Manifest source pins differ')
 (evidence/'candidate-manifest.json').write_bytes(manifest.read_bytes());status['candidate_manifest_sha256']=hashlib.sha256(manifest.read_bytes()).hexdigest();mark('manifest-pins')
 install=['/bin/sh',str(base/'app/public/install'),'--manifest',str(manifest),'--root',str(root),'--data-root',str(data)]
 cmd('fresh-install',install,work=base/'app');ready();name,v=current(); active=[l for l in labels if not absent(l)]
 if len(active)!=1: raise RuntimeError('Expected one Runtime service')
 owned_label=active[0];old_pid,old_birth,out=prove(owned_label);(evidence/'launchctl-before.txt').write_text(out);status.update(label=owned_label,old_pid=old_pid,old_birth=old_birth,old_version=name);mark('fresh-healthy')
 first=task(v,'before',True);status['before_task']=first;mark('before-task')
 standin=base/'standin.json';cmd('standin',[sys.executable,str(base/'agent/scripts/make_standin_release.py'),manifest,'--output',standin,'--sums',base/'standin-SHA256SUMS']);s=json.loads(standin.read_text())
 if s['release']!=pins['release']+1 or s['agent']!=pins['agent'] or s['runtime']!=pins['runtime']:raise RuntimeError('Stand-in pins or release differ')
 (evidence/'standin.json').write_bytes(standin.read_bytes());mark('standin-pins')
 prove(owned_label)
 stop=threading.Event()
 def observe():
  while not stop.wait(.25):
   p=subprocess.run(['/bin/ps','-ww','-u',str(os.getuid()),'-o','pid=,lstart=,command='],capture_output=True,text=True,timeout=10)
   lines=[line for line in p.stdout.splitlines() if str(data) in line and 'astrid_runtime' in line]
   observation.append({'t':time.monotonic(),'old_alive':birth(old_pid)==old_birth,'runtime_count':len(lines),'lines':lines})
   if len(observation)>20000: return
 watcher=threading.Thread(target=observe,daemon=True);watcher.start()
 try: cmd('update',['/bin/sh',str(base/'app/public/install'),'--manifest',str(standin),'--root',str(root),'--data-root',str(data),'--update'],work=base/'app')
 finally:
  stop.set();watcher.join(timeout=15);(evidence/'process-transition.json').write_text(json.dumps(observation,indent=2));p=subprocess.run(['/bin/launchctl','print',f'gui/{os.getuid()}/{owned_label}'],capture_output=True,text=True,timeout=20);(evidence/'launchctl-after.txt').write_text('exit='+str(p.returncode)+'\n'+p.stdout+'\n'+p.stderr)
 ready();new,v2=current()
 if name==new or not (root/'versions'/name).is_dir() or not (root/'versions'/new).is_dir():raise RuntimeError('Version switching or retention failed')
 if [l for l in labels if not absent(l)]!=[owned_label]:raise RuntimeError('Service label changed')
 prove(owned_label);mark('version-and-service')
 result=json.loads(cmd('data-check',[v2['python'],'-I','-m','astrid','tasks','show',first,'--project','mac-update-smoke','--json'],custom_env={**env,**v2['env']}))
 if not result.get('ok') or (result.get('data') or {}).get('state')!='succeeded':raise RuntimeError('Previous task missing after update')
 mark('data-intact');task(v2,'after',False);mark('after-task');status['outcome']='pass'
except Exception as exc:
 status['error_type']=type(exc).__name__;status['error']=str(exc);traceback.print_exc()
finally:
 log=home/'Library/Logs/Astrid Runtime/install.log'
 if log.is_file() and not log.is_symlink() and log.resolve().is_relative_to(home): (evidence/'install.log').write_bytes(log.read_bytes())
 try:
  active=[l for l in labels if not absent(l)]
  if active:
   if len(active)!=1 or active[0]!=owned_label:raise RuntimeError('Cannot prove service cleanup identity')
   prove(owned_label)
  if owned_label is not None and (root/'install.json').is_file():
   current()
   status['uninstall']='attempted';save()
   try:
    cmd('uninstall',['/bin/sh',str(base/'app/public/install'),'--manifest',str(manifest),'--root',str(root),'--data-root',str(data),'--uninstall'],work=base/'app')
    status['uninstall']='pass'
   except Exception:
    status['uninstall']='failed';raise
  if not all(absent(l) for l in labels):raise RuntimeError('Service still loaded after uninstall')
  after=states()
  if after!=initial:raise RuntimeError('Launchd disabled state differs from initial state')
  status['cleanup']='task-owned services stopped; launchd state restored'
 except Exception as exc:status['cleanup_error']=str(exc)
 if status['uninstall']!='pass' or status['cleanup']!='task-owned services stopped; launchd state restored':
  status['outcome']='failed'
 save()
