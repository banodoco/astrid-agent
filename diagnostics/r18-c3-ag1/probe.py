#!/usr/bin/env python3
"""Isolated C3 observations. No admission authority is produced."""
import ctypes as C
import errno
import json
import os
import pathlib
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
import traceback
import auth_helper as auth

root = pathlib.Path(os.environ['PROOF_ROOT'])
mode = sys.argv[1]
result = {'schema': 'r18-c3-ag1-native-v1', 'mode': mode, 'authority': False,
          'admission_exit': 1, 'uid': os.getuid(), 'euid': os.geteuid(), 'pid': os.getpid(),
          'uname': list(os.uname()), 'controls': {}, 'errors': []}
CG = pathlib.Path('/sys/fs/cgroup')

def attempt(fn):
    try:
        return {'state': 'observed', 'value': fn()}
    except Exception as e:
        return {'state': 'unknown', 'error': type(e).__name__, 'errno': getattr(e, 'errno', None),
                'message': str(e), 'trace': traceback.format_exc(limit=5)}

def control(name, fn):
    result['controls'][name] = attempt(fn)

def run(argv, timeout=20):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return {'argv': argv, 'exit': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr}

def current():
    line = next(x for x in pathlib.Path('/proc/self/cgroup').read_text().splitlines() if x.startswith('0::'))
    path = pathlib.PurePosixPath(line[3:])
    if not path.is_absolute() or '..' in path.parts: raise ValueError('unsafe cgroup path')
    return path

def pathfor(p):
    return CG / str(p).lstrip('/')

def boundary(p):
    wanted = 'user@%s.service' % os.getuid()
    parts = p.parts
    if wanted not in parts: raise ValueError('not inside user@UID.service: ' + str(p))
    return pathlib.PurePosixPath(*parts[:parts.index(wanted)+1])

def ownership():
    p = current(); entries=[]
    while True:
        f = pathfor(p) / 'cgroup.procs'
        try:
            s = f.stat(); entries.append({'path': str(p), 'uid': s.st_uid, 'gid': s.st_gid,
                                         'mode': oct(stat.S_IMODE(s.st_mode)), 'access_write': os.access(f, os.W_OK)})
        except OSError as e:
            entries.append({'path': str(p), 'errno': e.errno})
        if str(p) == '/': break
        p = p.parent
    return {'current': str(current()), 'boundary': str(boundary(current())), 'ancestors': entries,
            'mountinfo': [x for x in pathlib.Path('/proc/self/mountinfo').read_text().splitlines() if ' - cgroup2 ' in x]}

def move(target):
    # Separate disposable child attempts to move itself; parent is never moved.
    code = '''import json,os,sys\np=sys.argv[1]\ntry:\n f=os.open(p,os.O_WRONLY); os.write(f,(str(os.getpid())+"\\n").encode()); os.close(f); r={"errno":0}\nexcept OSError as e: r={"errno":e.errno,"error":str(e)}\nprint(json.dumps(r))\n'''
    return run([sys.executable, '-c', code, str(pathfor(target) / 'cgroup.procs')])

def clone(target):
    # clone3 ABI: CLONE_INTO_CGROUP with SIGCHLD; child exits immediately.
    class CloneArgs(C.Structure):
        _fields_ = [(k, C.c_uint64) for k in ('flags','pidfd','child_tid','parent_tid','exit_signal',
                     'stack','stack_size','tls','set_tid','set_tid_size','cgroup')]
    fd = os.open(pathfor(target), os.O_RDONLY | os.O_DIRECTORY)
    try:
        args = CloneArgs(); args.flags = 0x200000000; args.exit_signal = signal.SIGCHLD; args.cgroup = fd
        libc = C.CDLL(None, use_errno=True); libc.syscall.restype = C.c_long
        rv = libc.syscall(435, C.byref(args), C.sizeof(args)); err = C.get_errno() if rv < 0 else 0
        if rv == 0: os._exit(0)
        waited = os.waitpid(rv, 0)[1] if rv > 0 else None
        return {'target': str(target), 'syscall': 435, 'flags': hex(args.flags), 'return': rv,
                'errno': err, 'errno_name': errno.errorcode.get(err), 'child_wait_status': waited}
    finally: os.close(fd)

def movement():
    p=current(); b=boundary(p); dest=b.parent
    pre={'inside_boundary': str(p).startswith(str(b)+'/') or p == b,
         'destination_outside_boundary': dest != b and b not in dest.parents,
         'source_exists': pathfor(p).is_dir(), 'target_exists': pathfor(dest).is_dir(),
         'source': str(p), 'boundary': str(b), 'destination': str(dest)}
    return {'preconditions': pre, 'same_cgroup_write_positive': attempt(lambda: move(p)),
            'outside_write': attempt(lambda: move(dest)),
            'same_cgroup_clone3_positive': attempt(lambda: clone(p)),
            'outside_clone3': attempt(lambda: clone(dest))}

def census(start):
    members=[]; errors=[]; dirs=[]
    def onerror(e): errors.append({'operation': 'walk', 'path': e.filename, 'errno': e.errno})
    for path, names, files in os.walk(pathfor(start), onerror=onerror):
        names.sort(); dirs.append(path)
        try: ids=pathlib.Path(path, 'cgroup.procs').read_text().split()
        except OSError as e:
            errors.append({'operation': 'cgroup.procs', 'path': path, 'errno': e.errno}); continue
        for pid in ids:
            item={'pid': int(pid), 'cgroup': path}
            try:
                item['proc_cgroup'] = pathlib.Path('/proc',pid,'cgroup').read_text().strip()
                item['exe'] = os.readlink('/proc/'+pid+'/exe')
                item['status_uid'] = next(x for x in pathlib.Path('/proc',pid,'status').read_text().splitlines() if x.startswith('Uid:'))
            except (OSError, StopIteration) as e:
                item['error']={'type': type(e).__name__, 'errno': getattr(e,'errno',None)}
                errors.append({'operation': 'member_identity', 'pid': int(pid), **item['error']})
            members.append(item)
    return {'root': str(start), 'dirs': dirs, 'members': members, 'errors': errors,
            'disposition': 'incomplete_census' if errors else 'complete_observation_only'}

def escape_census():
    p=current(); b=boundary(p); unit='c3-escape-'+str(os.getpid())+'.scope'
    argv=['systemd-run','--user','--scope','--quiet','--unit='+unit,'/bin/sleep','90']
    child=subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        scope=None; show=None
        for _ in range(50):
            show=run(['systemctl','--user','show',unit,'-p','ControlGroup','--value'])
            candidate=show['stdout'].strip()
            if show['exit']==0 and candidate and (pathfor(candidate)/'cgroup.procs').exists(): scope=pathlib.PurePosixPath(candidate); break
            if child.poll() is not None: break
            time.sleep(.1)
        if scope is None: return {'argv':argv,'launcher_exit':child.poll(),'show':show,'state':'unknown_no_scope'}
        ids=[int(x) for x in (pathfor(scope)/'cgroup.procs').read_text().split()]
        snapshot=census(b); found=sorted(set(ids) & {x['pid'] for x in snapshot['members']})
        return {'argv':argv,'launcher_exit':child.poll(),'launch_cgroup':str(p),'scope':str(scope),
                'scope_pids':ids,'scope_outside_launch_subtree':scope != p and p not in scope.parents,
                'scope_inside_user_boundary': scope==b or b in scope.parents,
                'found_pids':found,'census':snapshot,'show':show}
    finally:
        stop=run(['systemctl','--user','stop',unit]); result.setdefault('cleanup',{})['scope_stop']=stop
        try: child.wait(timeout=5)
        except subprocess.TimeoutExpired: child.terminate(); child.wait(timeout=5)

def unreadable_census():
    code='import ctypes,time; c=ctypes.CDLL(None,use_errno=True); print(c.prctl(4,0,0,0,0),flush=True); time.sleep(60)'
    child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        ready=child.stdout.readline().strip(); own=census(current()); full=census(boundary(current()))
        item=next((x for x in full['members'] if x['pid']==child.pid),None)
        return {'child_pid':child.pid,'prctl_return':ready,'target_member':item,
                'target_observed_unreadable': bool(item and item.get('error',{}).get('errno') in (errno.EACCES,errno.EPERM)),
                'leaf_census':own,'full_census':full}
    finally:
        child.terminate(); child.wait(timeout=5)

def auth_control():
    peer=auth.Peer(auth.PRIVATE); fd=None
    try:
        fd=auth.socket_pidfd(peer.identity_socket)
        before={'peercred':list(peer.peer),'pidfd_pid':auth.fd_pid(fd),'readable':auth.readable(fd)}
        q1=auth.exact_system_bus_query(); q2=auth.exact_system_bus_query()
        after={'pidfd_pid':auth.fd_pid(fd),'readable':auth.readable(fd)}
        main=q1['MainPID']; same=(before['peercred'][0]==before['pidfd_pid']==main==after['pidfd_pid'] and
                                  before['peercred'][1]==os.getuid() and not before['readable'] and not after['readable'] and main==q2['MainPID'])
        # The system-bus peer must itself be proven PID 1 and its handler/source independently authenticated.
        return {'before':before,'system_query_before':q1,'system_query_after':q2,'after':after,
                'pidfd_bracket_numeric_match':same,
                'mismatched_mainpid_negative_rejected': not (same and main+1==before['pidfd_pid']),
                'system_bus_peer_pid_is_1':q1['peer']['credentials'][0]==1,
                'source_authentication':'unknown: system bus handler/source and proc image/namespace need independent proof',
                'manager_auth_disposition':'unknown'}
    finally:
        if fd is not None: os.close(fd)
        peer.close()

def replacement_control():
    auth.replacement_control()
    return {'raw':auth.result.get('replacement_control'),
            'verdict':auth.result['predicates'].get('socket_pidfd_replacement_and_death')}

def foreign_manager_control():
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as sock:
        sock.settimeout(5); sock.connect('/run/systemd/private')
        creds=struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        return {'peercred':list(creds),'expected_uid':os.getuid(),
                'foreign_uid_rejected':creds[1]!=os.getuid()}

def inherited_listener_control():
    # A child handles an inherited listener while peer credentials name its parent.
    path=root/'inherited.sock'
    code='''import os,socket,sys\ns=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1]); s.listen(1)\np=os.fork()\nif p==0:\n c,_=s.accept(); c.sendall(str(os.getpid()).encode()); c.close(); os._exit(0)\nprint("ready",flush=True); os.waitpid(p,0); sys.stdin.read()\n'''
    child=subprocess.Popen([sys.executable,'-c',code,str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if child.stdout.readline().strip()!='ready': raise RuntimeError('listener did not start')
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(5); sock.connect(str(path)); creds=struct.unpack('3i',sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            fd=auth.socket_pidfd(sock)
            try:
                observed_pid=auth.fd_pid(fd); live=not auth.readable(fd); handler=int(sock.recv(100))
            finally: os.close(fd)
        return {'listener_pid':child.pid,'socket_peercred':list(creds),'pidfd_pid':observed_pid,
                'pidfd_live_before_response':live,'actual_handler_pid':handler,
                'listener_and_handler_differ':handler!=observed_pid,
                'disposition':'socket peer PID alone does not prove handler provenance'}
    finally:
        if child.poll() is None: child.terminate()
        child.wait(timeout=5); path.unlink(missing_ok=True)

def nondelegated():
    p=current(); chain=[]; x=p
    while True:
        f=pathfor(x)/'cgroup.procs'
        try:
            s=f.stat(); chain.append({'path':str(x),'uid':s.st_uid,'mode':oct(stat.S_IMODE(s.st_mode)), 'writable':os.access(f,os.W_OK)})
        except OSError as e: chain.append({'path':str(x),'errno':e.errno})
        if str(x)=='/': break
        x=x.parent
    def make_child():
        child=pathfor(p)/('c3-probe-'+str(os.getpid()))
        os.mkdir(child)
        try: return {'created':True,'path':str(child)}
        finally: os.rmdir(child)
    return {'launch_cgroup':str(p),'ownership':chain,'write_same':attempt(lambda:move(p)),
            'unit_properties':run(['systemctl','show',os.environ['NONDELEGATED_UNIT'],'-p','Delegate','-p','ControlGroup','-p','User']),
            'mkdir_child':attempt(make_child),
            'expectation':'Delegate=no system service launch; refuse delegation proof if unavailable'}

try:
    control('preflight', lambda: {'uid':os.getuid(),'euid':os.geteuid(),'cgroup':str(current()),
                                  'uid_map':pathlib.Path('/proc/self/uid_map').read_text(),
                                  'systemd':run(['systemctl','--version'])})
    if mode=='user':
        for name,fn in [('cgroup_procs_ownership_chain',ownership),('movement_both_apis',movement),
                        ('scope_escape_census',escape_census),('unreadable_member_census',unreadable_census),
                        ('manager_auth_pidfd',auth_control),('dead_pidfd_replacement',replacement_control),
                        ('foreign_manager_negative',foreign_manager_control),
                        ('inherited_listener_negative',inherited_listener_control)]: control(name,fn)
    elif mode=='nondelegated': control('pinokio_non_delegated_launch_cgroup',nondelegated)
    else: raise ValueError('unknown mode')
except Exception as e:
    result['errors'].append({'error':type(e).__name__,'message':str(e),'trace':traceback.format_exc()})
finally:
    result['diagnostic_exit']=int(os.getuid()==0 or bool(result['errors']) or any(x['state']!='observed' for x in result['controls'].values()))
    (root / (mode+'-result.json')).write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
sys.exit(result['diagnostic_exit'])
