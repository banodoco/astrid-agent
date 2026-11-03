"""Non-authoritative, disposable real-systemd diagnostic; not an R0 journey.

Executed only by installer_native_manager_venue.sh as a non-root user on a
systemd host. Individual cases always record their failures and observations.
"""
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import traceback

root = Path(os.environ['PROOF_ROOT'])
result = {'uid': os.getuid(), 'euid': os.geteuid(), 'cases': {},
          'authority': False, 'authentic_r0': False,
          'limitations': ['diagnostic fixture, not an authentic R0 launch',
                          'current membership is not retained containment history',
                          'endpoint samples do not prove continuous manager identity',
                          'no exhaustive effective environment or trigger proof',
                          'no issuer barrier, Runtime verification or lifecycle admission']}


def run(*args):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=15, check=False)
    return {'argv': list(args), 'exit': p.returncode, 'stdout': p.stdout,
            'stderr': p.stderr}


def case(name, fn):
    try:
        result['cases'][name] = {'exit': 0, 'observations': fn()}
    except Exception as exc:
        result['cases'][name] = {'exit': 1, 'error': repr(exc),
                                 'traceback': traceback.format_exc()}


def peer_and_definition():
    assert os.getuid() > 0 and os.geteuid() == os.getuid()
    path = os.environ['XDG_RUNTIME_DIR'] + '/systemd/private'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(path)
        pid, uid, gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    assert uid == os.getuid() and pid > 1
    exe = os.readlink('/proc/%d/exe' % pid)
    cgroup = Path('/proc/%d/cgroup' % pid).read_text()
    commands = [
        ('owner', ('busctl', '--user', 'call', 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                   'org.freedesktop.DBus', 'GetNameOwner', 's', 'org.freedesktop.systemd1')),
        ('credentials', ('busctl', '--user', 'call', 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                         'org.freedesktop.DBus', 'GetConnectionCredentials', 's', 'org.freedesktop.systemd1')),
        ('manager', ('systemctl', '--user', 'show', '-p', 'Version', '-p', 'Environment')),
        ('definition', ('systemctl', '--user', 'show', 'r18-native-probe.service', '-p', 'Id',
                        '-p', 'LoadState', '-p', 'FragmentPath', '-p', 'DropInPaths', '-p', 'ExecStart',
                        '-p', 'User', '-p', 'Group', '-p', 'Environment', '-p', 'EnvironmentFiles',
                        '-p', 'ControlGroup', '-p', 'NeedDaemonReload')),
        ('jobs', ('systemctl', '--user', 'list-jobs', '--no-legend', '--no-pager')),
    ]
    outputs = {name: run(*cmd) for name, cmd in commands}
    assert all(item['exit'] == 0 for item in outputs.values()), outputs
    return {'direct_private_socket_so_peercred': {'pid': pid, 'uid': uid, 'gid': gid,
                                                   'exe': exe, 'cgroup': cgroup},
            'queries': outputs,
            'qualification': 'socket peer and bus queries are separate connections; not continuous identity or complete effective definition'}


def start_fixture():
    # Deliberately not called an authentic launch: this is a fresh diagnostic service.
    script = ("import os,time,pathlib; p=os.fork(); "
              "pathlib.Path(%r).write_text(str(os.getpid())+' '+str(p)) if p else None; "
              "time.sleep(40)") % str(root / 'fixture-pids')
    launch = run('systemd-run', '--user', '--unit=r18-native-probe.service',
                 '--property=Type=simple', sys.executable, '-c', script)
    assert launch['exit'] == 0, launch
    deadline = time.monotonic() + 10
    while not (root / 'fixture-pids').exists() and time.monotonic() < deadline:
        time.sleep(.05)
    parent, child = map(int, (root / 'fixture-pids').read_text().split())
    assert parent > 1 and child > 1
    return launch, parent, child


def containment(parent, child):
    proc = {}
    for pid in (parent, child):
        proc[str(pid)] = {'cgroup': Path('/proc/%d/cgroup' % pid).read_text(),
                          'stat': Path('/proc/%d/stat' % pid).read_text(),
                          'status_uid': next(line for line in Path('/proc/%d/status' % pid).read_text().splitlines() if line.startswith('Uid:'))}
    assert proc[str(parent)]['cgroup'] == proc[str(child)]['cgroup']
    assert 'r18-native-probe.service' in proc[str(parent)]['cgroup']
    cgpath = next(line.split(':', 2)[2] for line in proc[str(parent)]['cgroup'].splitlines() if line.startswith('0::'))
    members = Path('/sys/fs/cgroup', cgpath.lstrip('/'), 'cgroup.procs').read_text().split()
    assert {str(parent), str(child)}.issubset(set(members))
    return {'processes': proc, 'cgroup_procs': members,
            'qualification': 'current membership of fresh fixture only; no old-launch history or escaped-descendant census'}


def replacement_race():
    # Two independent processes and actual kernel rename. Force the editor to
    # replace after the guard's comparison and before its os.replace mutation.
    unitdir = Path(os.environ['XDG_CONFIG_HOME']) / 'systemd/user'
    unitdir.mkdir(parents=True, exist_ok=True)
    target = unitdir / 'r18-native-race.service'
    pending = unitdir / '.r18-native-ours'
    signal = root / 'race-go'; done = root / 'race-done'
    def unit(description):
        return ('[Unit]\nDescription=' + description + '\n[Service]\nExecStart=/usr/bin/true\n').encode()
    target.write_bytes(unit('original')); pending.write_bytes(unit('ours'))
    initial_reload = run('systemctl', '--user', 'daemon-reload')
    initial = run('systemctl', '--user', 'show', 'r18-native-race.service', '-p', 'Description', '--value')
    assert initial_reload['exit'] == initial['exit'] == 0 and initial['stdout'].strip() == 'original'
    before = target.stat(); assert target.read_bytes() == unit('original')
    worker = ("import os,pathlib,sys,time; t,p,g,d=map(pathlib.Path,sys.argv[1:]); "
              "\nwhile not g.exists(): time.sleep(.005)\n"
              "p.write_bytes(b'[Unit]\\nDescription=independent-replacement\\n[Service]\\nExecStart=/usr/bin/true\\n'); os.replace(p,t); d.write_text('done')")
    editor = subprocess.Popen([sys.executable, '-c', worker, str(target), str(unitdir / '.r18-native-editor'), str(signal), str(done)])
    try:
        signal.write_text('go')
        deadline = time.monotonic() + 10
        while not done.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert done.exists() and editor.wait(timeout=5) == 0
        replacement = target.stat(); assert target.read_bytes() == unit('independent-replacement')
        replacement_reload = run('systemctl', '--user', 'daemon-reload')
        replacement_loaded = run('systemctl', '--user', 'show', 'r18-native-race.service', '-p', 'Description', '--value')
        assert replacement_reload['exit'] == replacement_loaded['exit'] == 0 and replacement_loaded['stdout'].strip() == 'independent-replacement'
        os.replace(pending, target)
        observed = target.read_bytes()
        final_reload = run('systemctl', '--user', 'daemon-reload')
        final_loaded = run('systemctl', '--user', 'show', 'r18-native-race.service', '-p', 'Description', '--value')
        assert observed == unit('ours') and before.st_ino != replacement.st_ino
        assert final_reload['exit'] == final_loaded['exit'] == 0 and final_loaded['stdout'].strip() == 'ours'
        return {'independent_editor_exit': editor.returncode, 'original_inode': before.st_ino,
                'replacement_inode': replacement.st_ino, 'initial': initial, 'initial_reload': initial_reload,
                'replacement_loaded': replacement_loaded, 'replacement_reload': replacement_reload,
                'final_loaded': final_loaded, 'final_reload': final_reload,
                'result': 'precheck then rename overwrote independent replacement',
                'qualification': 'diagnostic race, not a safe product mutation'}
    finally:
        if editor.poll() is None:
            editor.kill(); editor.wait()


try:
    if os.getuid() == 0 or os.geteuid() != os.getuid():
        raise RuntimeError('requires genuine non-root user')
    launch, parent, child = start_fixture()
    result['fixture_launch'] = launch
    case('row1_peer_and_effective_definition', peer_and_definition)
    case('row2_current_containment_only', lambda: containment(parent, child))
    case('row3_independent_replacement_race', replacement_race)
except Exception as exc:
    result['setup_error'] = repr(exc)
    result['setup_traceback'] = traceback.format_exc()
finally:
    result['fixture_stop'] = run('systemctl', '--user', 'stop', 'r18-native-probe.service')
    (root / 'result.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')

sys.exit(0 if len(result['cases']) == 3 and all(c['exit'] == 0 for c in result['cases'].values()) else 1)
