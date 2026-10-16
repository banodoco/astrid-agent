"""WP2 real host/provider-fixture launches; no model, service or live data."""
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from astrid import omp_agent
from astrid.core.execution import generic_host as host
from tests.test_generic_host import _write_manifest


PROBE = r'''
import errno,json,os,subprocess,sys
from pathlib import Path
inside,outside,output=sys.argv[1:4]
def read(path):
    try:
        return {"value":Path(path).read_text()}
    except OSError as exc:
        return {"errno":exc.errno}
result={"pid":os.getpid(),"ppid":os.getppid(),"inside":read(inside),"outside":read(outside),"env":dict(os.environ),"cwd":os.getcwd()}
if len(sys.argv)==4:
    child=subprocess.run([sys.executable,"-I","-S","-B",__file__,inside,outside,output,"descendant"],capture_output=True,text=True,start_new_session=True,check=True)
    result["descendant"]=json.loads(child.stdout)
    if output!="-":
        Path(output).write_text(json.dumps(result))
print(json.dumps(result))
'''


@pytest.fixture
def launch(tmp_path):
    attempt=tmp_path/'attempt'
    private=attempt/'private'
    project=attempt/'project'
    for p in (private,project): p.mkdir(parents=True,mode=0o700)
    inside=project/'inside.txt'; inside.write_text('synthetic-inside')
    outside=tmp_path/'fake-credential.txt'; outside.write_text('synthetic-outside-credential')
    probe=tmp_path/'fake_provider.py'; probe.write_text(PROBE)
    python=Path(sys.executable).resolve()
    prefix=Path(sys.base_prefix)
    python_library=host._python_runtime_library(prefix)
    files=(python, *((python_library,) if python_library is not None else ()),
           Path('/opt/homebrew/opt/gettext/lib/libintl.8.dylib'),
           Path('/dev/null'),Path('/dev/urandom'),Path('/dev/random'),probe)
    roots=(Path(host.sysconfig.get_path('stdlib')).resolve(),
           Path(host.sysconfig.get_path('purelib')).resolve(),
           Path(host.sysconfig.get_path('platlib')).resolve(),Path('/usr/lib'),
           Path('/System/Library/Frameworks/CoreFoundation.framework'),
           Path('/System/Library/Frameworks/CoreServices.framework'),
           Path('/System/Volumes/Preboot/Cryptexes/OS/System/Library/dyld'),
           Path('/System/Volumes/Preboot/Cryptexes/OS/usr/lib'))
    if sys.platform!='darwin': pytest.skip('real sandbox proof requires macOS; refusals run on all platforms')
    # Explicit configured bootstrap closure on this platform, never a home/root grant.
    policy=host._ConfinementPolicy(attempt,private,host._SandboxProjectScope(project,(project,),(project,)),
                                  files,roots,host._SandboxEnvironmentPolicy())
    return policy,probe,inside,outside


def command_fixture(tmp_path, launch, *, policy=True, env=None):
    config,probe,inside,outside=launch
    root=tmp_path/'pack'
    manifest=_write_manifest(root,cwd=str(config.private_write_root))
    data=json.loads(manifest.read_text())
    data['command']['argv']=[str(Path(sys.executable).resolve()),'-I','-S','-B',str(probe),str(inside),str(outside),'{out}/answer.txt']
    data['command']['env']=env or {}
    manifest.write_text(json.dumps(data))
    h=host.GenericPackHost(pack_roots=[root],confinement_policy=config if policy else None)
    h.discover()
    return h,h.capabilities['test.echo']


def assert_probe(result, config):
    for p in (result,result['descendant']):
        assert p['inside']=={'value':'synthetic-inside'}
        assert p['outside'].get('errno') in (errno.EPERM,errno.EACCES)
        expected=dict(host._confined_launch_plan([str(Path(sys.executable).resolve())],policy=config).environment)
        # CoreFoundation may add this inert OS-generated value in the existing
        # custody trampoline. It is not selected from caller input.
        if '__CF_USER_TEXT_ENCODING' in p['env']:
            expected['__CF_USER_TEXT_ENCODING']=f'0x{os.getuid():X}:0x0:0x0'
        assert p['env']==expected
        assert p['cwd']==str(config.private_write_root)
    assert result['pid'] != result['descendant']['pid']


def test_actual_generic_command_inside_outside_descendant_and_environment(tmp_path, launch, monkeypatch):
    config,probe,inside,outside=launch
    launches=[]
    real_spawn=host.popen_owned_group
    def observe_spawn(argv,**options):
        launches.append({'argv':argv,'options':options})
        return real_spawn(argv,**options)
    monkeypatch.setattr(host,'popen_owned_group',observe_spawn)
    synthetic_env={'HOME':str(config.private_write_root),'TMPDIR':str(config.private_write_root),
                   'PATH':'/usr/bin:/bin','LC_ALL':'C'}
    control=subprocess.run([str(Path(sys.executable).resolve()),'-I','-S','-B',str(probe),str(inside),str(outside),'-'],env=synthetic_env,capture_output=True,text=True,check=True)
    control=json.loads(control.stdout)
    assert control['outside']=={'value':'synthetic-outside-credential'}
    assert control['descendant']['outside']==control['outside']
    output=config.project.project_root/'outputs'; output.mkdir()
    results=[]
    for attack in ({},{'HOME':str(outside.parent),'PATH':str(outside.parent),'PYTHONPATH':str(outside.parent),
                        'PYTHONSTARTUP':str(outside),'DYLD_INSERT_LIBRARIES':str(outside),'OMP_BIN':str(outside),
                        'ASTRID_NETWORK_POLICY':'{}'}):
        if attack:
            monkeypatch.setenv('BANODOCO_RUNTIME_CREDENTIAL','synthetic-secret')
        h,record=command_fixture(tmp_path/('attack' if attack else 'clean'),launch,env=attack)
        result=h._run_command_definition(record,{},output,config.attempt_root)
        observed=json.loads((output/'answer.txt').read_text())
        assert_probe(observed,config)
        assert result.process_id==observed['pid']
        assert not h._active_processes
        results.append(observed)
    assert results[0]['env']==results[1]['env']
    assert launches[0]==launches[1]
    print('WP2_GENERIC_CANARY',json.dumps({'control':control,'confined':results,'actual_launches':launches}))


@pytest.mark.parametrize('entry',['command','native'])
@pytest.mark.parametrize('policy',[None,{},False])
def test_real_generic_entrypoints_refuse_absent_invalid_policy(tmp_path,monkeypatch,entry,policy):
    monkeypatch.setattr(host,'popen_owned_group',lambda *a,**k:pytest.fail('unconfined spawn'))
    attempt=tmp_path/'attempt'; attempt.mkdir()
    h=host.GenericPackHost(pack_roots=[],confinement_policy=policy)
    if entry=='native':
        with pytest.raises(host.HostError,match='policy is missing or invalid'):
            h.invoke_capability(capability_kind='executor',capability_id='fixture',request={},attempt=attempt)
    else:
        root=tmp_path/'pack'; _write_manifest(root); h.pack_roots=(root,); h.discover()
        with pytest.raises(host.HostError,match='policy is missing or invalid'):
            h._run_command_definition(h.capabilities['test.echo'],{},attempt,attempt)


@pytest.mark.parametrize('entry',['command','native'])
@pytest.mark.parametrize('platform,backend',[('linux','/usr/bin/sandbox-exec'),('darwin',None)])
def test_generic_real_seam_refuses_platform_or_backend(tmp_path,launch,monkeypatch,entry,platform,backend):
    config,*_=launch
    h,record=command_fixture(tmp_path,launch)
    monkeypatch.setattr(host.sys,'platform',platform)
    monkeypatch.setattr(host.shutil,'which',lambda _:backend)
    monkeypatch.setattr(host,'popen_owned_group',lambda *a,**k:pytest.fail('unconfined spawn'))
    with pytest.raises(host.HostError,match='sandbox'):
        if entry=='command':
            h._run_command_definition(record,{},config.project.project_root,config.attempt_root)
        else:
            h.invoke_capability(capability_kind='executor',capability_id='fixture',request={},attempt=config.attempt_root)


def test_generic_rejects_different_attempt_and_unapproved_descriptors(tmp_path,launch):
    config,*_=launch
    h=host.GenericPackHost(pack_roots=[],confinement_policy=config)
    for attempt,fds,match in [(tmp_path,(),'different attempt'),(config.attempt_root,(123,),'descriptors')]:
        with pytest.raises(host.HostError,match=match):
            h._workload_launch_plan([sys.executable],attempt=attempt,cwd=None,environment={},inherited_fds=fds,broker_endpoint=None)


def test_retained_wan_cannot_use_its_legacy_unconfined_launch():
    adapter=host._ManagedWanChildAdapter(spec={},binding=None,readiness_timeout=1,release_timeout=1,track=lambda _:None,untrack=lambda _:None)
    with pytest.raises(host.HostError,match='confinement descriptor contract'): adapter.start()


@pytest.mark.parametrize('route',['packaged','named','onboard'])
def test_actual_omp_main_sanitizes_environment_and_denies_outside(tmp_path,launch,monkeypatch,capsys,route):
    config,probe,inside,outside=launch
    omp=tmp_path/'fake_omp'
    omp.write_text('#!'+str(Path(sys.executable).resolve())+' -I\n'
                   +'import sys\n'
                   +'if sys.argv[1:]==["onboard","--help"]:\n    print("detect-first provider onboarding"); sys.exit(0)\n'
                   +'if len(sys.argv)<5 or sys.argv[1]!='+repr(str(inside))+':\n    sys.argv=[__file__,'+repr(str(inside))+','+repr(str(outside))+',"-"]\n'+PROBE)
    omp.chmod(0o700)
    config=replace(config,bootstrap_files=(*config.bootstrap_files,omp))
    for key,value in {'OMP_BIN':str(omp),'ASTRID_AGENT_LAUNCHER':str(omp),'ASTRID_HOST_EXECUTION_PROFILE':'installed',
                      'ASTRID_SYSTEM_PROMPT_FILE':str(outside),'PYTHONPATH':str(outside.parent),
                      'PYTHONSTARTUP':str(outside),'DYLD_INSERT_LIBRARIES':str(outside),'ASTRID_STOCK_OMP':'1',
                      'ASTRID_NETWORK_POLICY':'{}','BANODOCO_RUNTIME_CREDENTIAL':'synthetic-secret'}.items():
        monkeypatch.setenv(key,value)
    args={'packaged':['probe'],'named':['--agent','fixture','probe'],'onboard':['--onboard']}[route]
    assert omp_agent.main(args,confinement_policy=config)==0
    observed=json.loads(capsys.readouterr().out)
    assert_probe(observed,config)
    print('WP2_OMP_CANARY',route,json.dumps(observed))


@pytest.mark.parametrize('route',['packaged','named','onboard-probe'])
@pytest.mark.parametrize('config',[None,{},False])
def test_omp_refuses_missing_invalid_policy_at_spawn(monkeypatch,route,config):
    monkeypatch.setattr(omp_agent.subprocess,'run',lambda *a,**k:pytest.fail('unconfined OMP spawn'))
    with pytest.raises(host.HostError,match='policy is missing or invalid'):
        if route=='onboard-probe': omp_agent._omp_supports_onboard('/fixture',confinement_policy=config)
        elif route=='named': omp_agent._run_confined_omp(['/fixture','run','fixture'],confinement_policy=config)
        else: omp_agent._exec_omp_with_packaged_profile('/fixture',[],[],confinement_policy=config)


@pytest.mark.parametrize('platform,backend',[('linux','/usr/bin/sandbox-exec'),('darwin',None)])
def test_omp_refuses_unsupported_backend(launch,monkeypatch,platform,backend):
    config,*_=launch
    monkeypatch.setattr(host.sys,'platform',platform)
    monkeypatch.setattr(host.shutil,'which',lambda _:backend)
    monkeypatch.setattr(omp_agent.subprocess,'run',lambda *a,**k:pytest.fail('unconfined OMP spawn'))
    with pytest.raises(host.HostError,match='sandbox'):
        omp_agent._run_confined_omp([str(Path(sys.executable).resolve())],confinement_policy=config)


def test_no_spawn_retry_if_backend_execution_fails(tmp_path,launch,monkeypatch):
    config,*_=launch
    h,record=command_fixture(tmp_path,launch)
    calls=[]
    def fail(argv,**kwargs):
        calls.append((argv,kwargs)); raise OSError('synthetic sandbox launch failure')
    with monkeypatch.context() as patch:
        patch.setattr(omp_agent.subprocess,'run',fail)
        with pytest.raises(OSError,match='synthetic sandbox'):
            omp_agent._run_confined_omp([str(Path(sys.executable).resolve())],confinement_policy=config)
    assert len(calls)==1 and calls[0][0][:2]==('/usr/bin/sandbox-exec','-p')
    monkeypatch.setattr(host,'popen_owned_group',fail)
    with pytest.raises(OSError,match='synthetic sandbox'):
        h._run_command_definition(record,{},config.project.project_root,config.attempt_root)
    assert len(calls)==2
    assert calls[1][0][:2]==['/usr/bin/sandbox-exec','-p']
    for _,options in calls:
        assert options['close_fds'] is True and options['pass_fds']==()
        assert options['stdin']==options['stdout']==options['stderr']==subprocess.PIPE


def test_provider_retry_rejection_survives_confinement(tmp_path,launch):
    config,*_=launch
    h,record=command_fixture(tmp_path,launch)
    script=tmp_path/'reject.py'
    script.write_text('import json,sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text(json.dumps({"code":"provider_failure","message":"synthetic 429","provider_id":"fixture","retryable":True,"retry_after_seconds":7}))\nsys.exit(1)\n')
    config=replace(config,bootstrap_files=(*config.bootstrap_files,script))
    h._confinement_policy=config
    manifest=tmp_path/'pack/executor.yaml'
    data=json.loads(manifest.read_text())
    data['command']['argv']=[str(Path(sys.executable).resolve()),'-I','-S','-B',str(script),'{out}/.astrid-provider-failure.json']
    manifest.write_text(json.dumps(data)); h.discover()
    with pytest.raises(host.ProviderFailure) as caught:
        h._run_command_definition(h.capabilities['test.echo'],{},config.project.project_root,config.attempt_root)
    assert caught.value.provider_id=='fixture'
    assert caught.value.retryable is True and caught.value.retry_after_seconds==7


def test_actual_native_worker_and_provider_descendant_remain_confined(tmp_path,launch,monkeypatch):
    config,probe,inside,outside=launch
    h,record=command_fixture(tmp_path,launch)
    # Enumerate this source checkout and fixture provider as bootstrap code;
    # neither exception includes the sibling fake credential or the run root.
    config=replace(config,
                   bootstrap_files=(*config.bootstrap_files,Path('/opt/homebrew/opt/libb2/lib/libb2.1.dylib'),
                                    Path('/opt/homebrew/opt/openssl@3/lib/libcrypto.3.dylib'),
                                    Path('/opt/homebrew/opt/openssl@3/lib/libssl.3.dylib')),
                   bootstrap_roots=(*config.bootstrap_roots,Path(host.__file__).resolve().parents[3],tmp_path/'pack'))
    h._confinement_policy=config
    definition,admission=h.admit('executor','test.echo')
    launches=[]
    real_spawn=host.popen_owned_group
    def observe_spawn(argv,**options):
        launches.append({'argv':argv,'options':options})
        return real_spawn(argv,**options)
    monkeypatch.setattr(host,'popen_owned_group',observe_spawn)
    result=h.invoke_capability(capability_kind='executor',capability_id='test.echo',
                              request={'out':str(config.project.project_root),'inputs':{},
                                       'project':'synthetic-project','project_was_auto_resolved':True},
                              attempt=config.attempt_root,definition=definition,admission=admission,
                              child_env={'PYTHONPATH':str(outside.parent),'ASTRID_NETWORK_POLICY':'{}',
                                         'HOME':str(outside.parent),'BANODOCO_RUNTIME_CREDENTIAL':'synthetic-secret'})
    assert result.ok is True
    observed=json.loads((config.project.project_root/'answer.txt').read_text())
    for p in (observed,observed['descendant']):
        assert p['inside']=={'value':'synthetic-inside'}
        assert p['outside'].get('errno') in (errno.EPERM,errno.EACCES)
        assert 'BANODOCO_RUNTIME_CREDENTIAL' not in p['env'] and 'PYTHONPATH' not in p['env']
        assert p['env']['HOME']==str(config.private_write_root)
    assert not h._active_processes
    print('WP2_NATIVE_CANARY',json.dumps({'worker_pid':result.process_id,'provider':observed,'actual_launches':launches}))


def test_native_engine_graph_has_no_unrestricted_fallback(monkeypatch):
    monkeypatch.setattr(host.subprocess,'Popen',lambda *a,**k:pytest.fail('unconfined retained engine'))
    graph=host._NativeEngineGraph(request={'profile':{}})
    with pytest.raises(host.HostError,match='policy is missing or invalid'):
        graph.prepare()
    assert graph.process is None


def test_trusted_constructor_default_cwd_and_module_bootstrap_without_factory(tmp_path, launch):
    config, _, _, outside = launch
    pack = tmp_path / 'bootstrap-pack'
    manifest = _write_manifest(pack)
    module = pack / 'executors'
    module.mkdir()
    (module / '__init__.py').write_text('')
    (module / 'run.py').write_text('''
import json,os,sys
from pathlib import Path
try:
    Path(sys.argv[2]).read_text()
    outside=0
except OSError as exc:
    outside=exc.errno
Path(sys.argv[1]).write_text(json.dumps(dict(cwd=os.getcwd(),marker=os.environ.get('ASTRID_INTERNAL_INVOCATION'),
    pythonpath=os.environ.get('PYTHONPATH'),outside=outside)))
''')
    definition = json.loads(manifest.read_text())
    definition['command']['argv'] = ['{python_exec}', '-m', 'executors.run', '{out}/answer.txt', str(outside)]
    definition['command']['env'] = {'PYTHONPATH': str(tmp_path), 'ASTRID_INTERNAL_INVOCATION': 'forged'}
    manifest.write_text(json.dumps(definition))
    policy = replace(config, bootstrap_roots=(*config.bootstrap_roots, pack))
    h = host.GenericPackHost(pack_roots=[pack], confinement_policy=policy)
    h.discover()
    assert h._required_confinement_policy(config.attempt_root) is policy
    result = h._run_command_definition(h.capabilities['test.echo'], {}, config.private_write_root, config.attempt_root)
    assert result is not None
    observed = json.loads((config.private_write_root / 'answer.txt').read_text())
    assert observed == dict(cwd=str(config.private_write_root), marker='1', pythonpath=None, outside=errno.EPERM)
    # The new parent search is a literal directory rule, never a recursive read.
    plan = h._workload_launch_plan([str(config.bootstrap_files[0])], attempt=config.attempt_root,
        cwd=config.attempt_root, environment={}, inherited_fds=(), broker_endpoint=None)
    assert f'(allow file-read* (literal "{tmp_path}"))' in plan.argv[2]
    assert f'(allow file-read* (subpath "{tmp_path}"))' not in plan.argv[2]
