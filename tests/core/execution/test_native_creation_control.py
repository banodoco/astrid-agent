"""Disposable Darwin creation experiment; never admits a Wan engine closure."""
from dataclasses import replace
import errno
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from astrid.core.execution import custody_broker as cb, generic_host as host, process_group as groups
from tests.core.execution.test_confinement_launch_integration import launch  # noqa: F401
from tests.core.execution.test_wan_custody_limits import private_socket_transport  # noqa: F401


NATIVE = r'''
#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <pthread.h>
#include <servers/bootstrap.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
extern char **environ;
static int threaded;
static void *thread(void *arg) { threaded=37; return NULL; }
static void marker(const char *path) {
    int fd=open(path,O_WRONLY|O_CREAT|O_APPEND,0600);
    if(fd>=0) { write(fd,"child-executed\n",15); close(fd); }
}
int main(int argc,char **argv) {
    if(argc!=3) return 93;
    const char *mode=argv[1],*path=argv[2];
    if(!strcmp(mode,"child")) { marker(path); return 0; }
    if(!strcmp(mode,"exec")||!strcmp(mode,"exec-wait")) {
        char *args[]={argv[0],!strcmp(mode,"exec")?"exec-probe":"exec-probe-wait",argv[2],NULL};
        execve(argv[0],args,environ); return 94;
    }
    pthread_t t; int thread_error=pthread_create(&t,NULL,thread,NULL);
    if(!thread_error) pthread_join(t,NULL);
    int result=-1,error=0,status=-1; pid_t pid=-1;
    if(!strcmp(mode,"fork")||!strcmp(mode,"double-fork")||!strcmp(mode,"exec-probe")||!strcmp(mode,"exec-probe-wait")) {
        errno=0; pid=fork(); error=errno; result=(int)pid;
        if(pid==0) {
            marker(path);
            if(!strcmp(mode,"double-fork")) {
                setsid(); pid_t second=fork();
                if(second==0) { marker(path); _exit(0); }
                if(second>0) waitpid(second,NULL,0);
            }
            _exit(0);
        }
        if(pid>0) waitpid(pid,&status,0);
    } else if(!strcmp(mode,"vfork")) {
        // Only async-safe _exit in the vfork child: never run Python here.
        errno=0; pid=vfork(); if(pid==0) _exit(91);
        error=errno; result=(int)pid;
        if(pid>0) waitpid(pid,&status,0);
    } else if(!strcmp(mode,"spawn")) {
        char *args[]={argv[0],"child",argv[2],NULL};
        result=posix_spawn(&pid,argv[0],NULL,NULL,args,environ); error=result;
        if(!result) waitpid(pid,&status,0);
    } else if(!strcmp(mode,"ipc")) {
        // No invocation of a real service. Failed lookup must yield no port.
        mach_port_t port=MACH_PORT_NULL;
        result=bootstrap_look_up(bootstrap_port,"com.apple.cfprefsd.agent",&port);
        error=result;
        if(port!=MACH_PORT_NULL) mach_port_deallocate(mach_task_self(),port);
        printf("{\"mode\":\"ipc\",\"result\":%d,\"port\":%u,\"thread\":%d,\"thread_error\":%d,\"pid\":%d}\n",result,port,threaded,thread_error,getpid());
        return 0;
    } else return 95;
    printf("{\"mode\":\"%s\",\"result\":%d,\"errno\":%d,\"status\":%d,\"thread\":%d,\"thread_error\":%d,\"pid\":%d}\n",mode,result,error,status,threaded,thread_error,getpid());
    fflush(stdout);
    if(!strcmp(mode,"exec-probe-wait")) sleep(30);
    return 0;
}
'''


@pytest.fixture
def native(launch, tmp_path):
    policy, _, inside, outside = launch
    source=tmp_path/'native.c'; executable=tmp_path/'native'
    source.write_text(NATIVE)
    built=subprocess.run(['/usr/bin/clang','-Wno-deprecated-declarations','-pthread',str(source),'-o',str(executable)],
                         env={'PATH':'/usr/bin:/bin','HOME':str(tmp_path),'TMPDIR':str(tmp_path)},
                         capture_output=True,text=True,timeout=30)
    assert built.returncode==0,built.stderr
    policy=replace(policy,bootstrap_files=(*policy.bootstrap_files,executable))
    return policy,executable,inside,outside


def execute(plan, private_socket_transport):
    private_socket_transport[0]=plan.cwd
    process=groups.popen_owned_group(list(plan.argv),**plan.subprocess_options())
    try:
        out,err=process.communicate(timeout=12)
        assert process.returncode==0,err.decode()
        groups.verify_tree_absent(process)
        value=json.loads(out)
        assert value['pid']==process.pid
        if plan.retained_process_only:
            assert process._astrid_custody._inventory_owner is None
            assert process._astrid_custody._launch_broker.child_pipe_binding==[]
        return value,process
    finally:
        if process.poll() is None:groups.terminate_tree(process)
        process.wait(timeout=5)
        for stream in (process.stdin,process.stdout,process.stderr):stream.close()


@pytest.mark.parametrize('operation',['fork','vfork','spawn','double-fork','exec'])
def test_native_kernel_creation_denial_threads_and_confined_exec(native,private_socket_transport,operation):
    policy,executable,*_=native
    marker=policy.project.project_root/'child-code'
    command=[str(executable),operation,str(marker)]
    # Positive control: the same native operation really creates children.
    control=subprocess.run(command,env={'PATH':'/usr/bin:/bin'},capture_output=True,text=True,timeout=5,check=True)
    control=json.loads(control.stdout)
    assert control['result']>=0 and control['status']>=0
    if operation!='vfork':assert marker.exists()
    marker.unlink(missing_ok=True)
    plan=host._wan_creation_control_launch_plan(command,policy=policy,
        parent_environment={'ASTRID_DISABLE_SANDBOX':'1','ASTRID_PROCESS_CREATION':'allow','SECRET':'synthetic'})
    value,process=execute(plan,private_socket_transport)
    assert value['errno']==errno.EPERM
    assert value['result']==(errno.EPERM if operation=='spawn' else -1)
    assert value['thread_error']==0 and value['thread']==37
    assert not marker.exists()
    print('NATIVE_CREATION',json.dumps({'operation':operation,'control':control,'confined':value,
          'registered_pid':process._astrid_custody._launch_broker.registration['pid'],
          'original_wait':process.returncode,'marker':marker.exists()}))


def test_native_ipc_lookup_is_denied_without_service_invocation(native,private_socket_transport):
    policy,executable,*_=native
    plan=host._wan_creation_control_launch_plan([str(executable),'ipc',str(policy.project.project_root/'marker')],policy=policy)
    value,_=execute(plan,private_socket_transport)
    assert value['result']!=0 and value['port']==0
    assert 'mach-lookup' not in plan.argv[2] and 'ipc-' not in plan.argv[2]
    print('NATIVE_IPC',json.dumps(value))


def test_same_process_exec_retains_live_identity_bound_cleanup(native,private_socket_transport):
    policy,executable,*_=native
    marker=policy.project.project_root/'exec-child-code'
    plan=host._wan_creation_control_launch_plan([str(executable),'exec-wait',str(marker)],policy=policy)
    private_socket_transport[0]=plan.cwd
    process=groups.popen_owned_group(list(plan.argv),**plan.subprocess_options())
    try:
        value=json.loads(process.stdout.readline())
        assert value['pid']==process.pid and value['result']==-1 and value['errno']==errno.EPERM
        broker=process._astrid_custody._launch_broker
        original=broker.role_authority.reference()
        groups.terminate_tree(process)
        process.wait(timeout=5)
        groups.verify_tree_absent(process)
        current=broker.role_authority.reference()
        assert current['target']['pid']==original['target']['pid']==process.pid
        assert current['target']['birth_id']==original['target']['birth_id']
        assert process.returncode<0 and not marker.exists()
        print('NATIVE_LIVE_EXEC_CLEANUP',json.dumps({'pid':process.pid,'before':original,
              'after':current,'original_wait':process.returncode}))
    finally:
        if process.poll() is None:groups.terminate_tree(process)
        process.wait(timeout=5)
        for stream in (process.stdin,process.stdout,process.stderr):stream.close()


PYTHON_PROBE = r'''
import ctypes,errno,json,os,socket,stat,subprocess,sys,threading
from pathlib import Path
inside,outside,marker=sys.argv[1:]
value={'pid':os.getpid(),'env':dict(os.environ)}
t=[]
thread=threading.Thread(target=lambda:t.append('ran'));thread.start();thread.join()
value['thread']=t
try:
    subprocess.run([sys.executable,'-I','-S','-B','-c',f'from pathlib import Path;Path({marker!r}).touch()'],check=True)
    value['subprocess']='allowed'
except OSError as exc:value['subprocess']=exc.errno
for key,path in [('inside',inside),('outside',outside)]:
    try:value[key]={'value':Path(path).read_text()}
    except OSError as exc:value[key]={'errno':exc.errno}
try:Path(outside).write_text('BAD');value['outside_write']='allowed'
except OSError as exc:value['outside_write']=exc.errno
Path(marker+'-inside').write_text('synthetic')
try:
    sock=socket.socket();sock.connect(('127.0.0.1',9));value['network']='allowed'
except OSError as exc:value['network']=exc.errno
finally:
    if 'sock' in locals():sock.close()
# Native self census, including handles above any guessed FD limit.
class FD(ctypes.Structure):_fields_=[('fd',ctypes.c_int32),('kind',ctypes.c_uint32)]
lib=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
lib.proc_pidinfo.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_uint64,ctypes.c_void_p,ctypes.c_int]
lib.proc_pidinfo.restype=ctypes.c_int
size=lib.proc_pidinfo(os.getpid(),1,0,None,0)
entries=(FD*(size//ctypes.sizeof(FD)+128))()
used=lib.proc_pidinfo(os.getpid(),1,0,ctypes.byref(entries),ctypes.sizeof(entries))
assert used>0 and used<ctypes.sizeof(entries) and used%ctypes.sizeof(FD)==0
value['fds']=[{'fd':entry.fd,'device':os.fstat(entry.fd).st_dev,'inode':os.fstat(entry.fd).st_ino,
               'type':stat.S_IFMT(os.fstat(entry.fd).st_mode)} for entry in entries[:used//ctypes.sizeof(FD)]]
print(json.dumps(value),flush=True)
'''


def test_python_subprocess_environment_files_network_and_descriptors(launch,tmp_path,private_socket_transport):
    policy,_,inside,outside=launch
    script=tmp_path/'fake_engine.py';script.write_text(PYTHON_PROBE)
    policy=replace(policy,bootstrap_files=(*policy.bootstrap_files,script))
    marker=policy.project.project_root/'child-code'
    plan=host._wan_creation_control_launch_plan([str(Path(sys.executable).resolve()),'-I','-S','-B',str(script),str(inside),str(outside),str(marker)],
        policy=policy,parent_environment={'HOME':str(outside.parent),'SECRET':'synthetic','ASTRID_COMMAND_CONTROL_FD':'0'})
    with (tmp_path/'host-lock').open('w') as lock:
        os.set_inheritable(lock.fileno(),True)
        forbidden=os.fstat(lock.fileno())
        value,_=execute(plan,private_socket_transport)
    assert value['thread']==['ran'] and value['subprocess']==errno.EPERM
    assert value['inside']=={'value':'synthetic-inside'}
    assert value['outside']['errno'] in {errno.EPERM,errno.EACCES}
    assert value['outside_write'] in {errno.EPERM,errno.EACCES}
    assert value['network'] in {errno.EPERM,errno.EACCES}
    assert outside.read_text()=='synthetic-outside-credential'
    assert not marker.exists() and Path(str(marker)+'-inside').read_text()=='synthetic'
    assert {entry['fd'] for entry in value['fds']}=={0,1,2}
    assert all((entry['device'],entry['inode'])!=(forbidden.st_dev,forbidden.st_ino) for entry in value['fds'])
    expected=dict(plan.environment)
    if '__CF_USER_TEXT_ENCODING' in value['env']:expected['__CF_USER_TEXT_ENCODING']=f'0x{os.getuid():X}:0x0:0x0'
    assert value['env']==expected
    print('NATIVE_PYTHON',json.dumps(value))


def test_host_selection_cannot_be_weakened_and_delegation_is_closed(launch):
    policy,*_=launch
    argv=[str(Path(sys.executable).resolve()),'-I','-S','-B','-c','pass']
    plan=host._wan_creation_control_launch_plan(argv,policy=replace(policy,workload_process_creation=True))
    assert '(allow process*)' not in plan.argv[2]
    assert '(deny process-fork)' in plan.argv[2]
    assert plan.subprocess_options()['_delegate_inventory'] is False
    assert plan.pass_fds==()
    for invalid in [None,{},False]:
        with pytest.raises(host.HostError):host._wan_creation_control_launch_plan(argv,policy=invalid)
    with pytest.raises(host.HostError,match='process creation'):
        host._confined_launch_plan(argv,policy=replace(policy,workload_process_creation='allow'))
    network=host._SandboxNetworkPolicy((host._SandboxTcpEndpoint('127.0.0.1',9),))
    with pytest.raises(host.HostError,match='no admitted delegation'):
        host._wan_creation_control_launch_plan(argv,policy=replace(policy,network=network))
    read,write=os.pipe()
    try:
        with pytest.raises(host.HostError,match='no admitted delegation'):
            host._wan_creation_control_launch_plan(argv,policy=replace(policy,pass_fds=(read,write)))
    finally:os.close(read);os.close(write)


def test_fake_admitted_api_init_submit_result_and_close(launch,tmp_path,private_socket_transport):
    """Interface-only positive workload; deliberately no real engine import."""
    policy,*_=launch
    project=policy.project.project_root
    script=tmp_path/'fake_api_worker.py'
    script.write_text(r'''
import json,os,sys,threading
from pathlib import Path
class SessionJob:
    def __init__(self,path):self.path=path;self.cancelled=False
    def result(self,timeout):
        assert timeout>0
        thread=threading.Thread(target=lambda:self.path.write_text('synthetic-media'))
        thread.start();thread.join()
        return {'generated_files':[str(self.path)]}
    def cancel(self):self.cancelled=True;return True
class WanGPSession:
    def __init__(self,output_dir):self.output=Path(output_dir);self.closed=False
    def submit_task(self,settings):return SessionJob(self.output/settings['name'])
    def close(self):self.closed=True
class FakeSharedAPI:
    @staticmethod
    def init(root,output_dir):
        assert Path(root).is_dir()
        return WanGPSession(output_dir)
session=None
for line in sys.stdin:
    frame=json.loads(line)
    if frame['operation']=='init':
        session=FakeSharedAPI.init(frame['root'],frame['output_dir']);result={'ready':True}
    elif frame['operation']=='run':
        job=session.submit_task(frame['settings']);result=job.result(timeout=1)
    elif frame['operation']=='cancel':
        job=session.submit_task({'name':'cancelled'});result={'cancelled':job.cancel()}
    elif frame['operation']=='close':
        session.close();result={'closed':session.closed}
    else:raise RuntimeError('unknown operation')
    print(json.dumps({'pid':os.getpid(),'operation':frame['operation'],'result':result}),flush=True)
    if frame['operation']=='close':break
''')
    policy=replace(policy,bootstrap_files=(*policy.bootstrap_files,script))
    plan=host._wan_creation_control_launch_plan([str(Path(sys.executable).resolve()),'-I','-S','-B',str(script)],policy=policy)
    private_socket_transport[0]=plan.cwd
    frames=[{'operation':'init','root':str(project),'output_dir':str(project)},
            {'operation':'run','settings':{'name':'fake-output'}},
            {'operation':'cancel'},{'operation':'close'}]
    process=groups.popen_owned_group(list(plan.argv),**plan.subprocess_options())
    try:
        output,error=process.communicate(input=b''.join(json.dumps(f).encode()+b'\n' for f in frames),timeout=10)
        assert process.returncode==0,error.decode()
        results=[json.loads(line) for line in output.splitlines()]
        assert [r['operation'] for r in results]==['init','run','cancel','close']
        assert all(r['pid']==process.pid for r in results)
        assert results[0]['result']=={'ready':True}
        assert results[1]['result']=={'generated_files':[str(project/'fake-output')]}
        assert results[2]['result']=={'cancelled':True}
        assert results[3]['result']=={'closed':True}
        assert (project/'fake-output').read_text()=='synthetic-media'
        groups.verify_tree_absent(process)
        print('NATIVE_FAKE_API',json.dumps({'results':results,'original_wait':process.returncode,
                                          'compatibility':'fixture-only'}))
    finally:
        if process.poll() is None:groups.terminate_tree(process)
        process.wait(timeout=5)
        for stream in (process.stdin,process.stdout,process.stderr):stream.close()
