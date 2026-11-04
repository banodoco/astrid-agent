#!/usr/bin/env python3
"""Remove only four previously inventoried runner-owned task roots."""
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

UID = 503
FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
MAX_FILES = 1000000
MAX_SECONDS = 300
MAX_RETAINED = 25_000_000
MAX_FILE = 2_000_000
TARGETS = (("r18-41536262841-1", ".astrid-r18", "41536262841-1", False),
           ("r25-run-2TyWZKgP", "r25-option-b-runs", "run.2TyWZKgP", False),
           ("r25-run-OslHRGip", "r25-option-b-runs", "run.OslHRGip", True),
           ("r25-run-Z0Ei3m9T", "r25-option-b-runs", "run.Z0Ei3m9T", True))


def safe_private_dir(path, create=False):
    if not path.exists() and not path.is_symlink():
        if not create: raise RuntimeError("Required private directory absent")
        path.mkdir(mode=0o700)
    st = path.lstat()
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != UID or path != path.resolve()
            or stat.S_IMODE(st.st_mode) != 0o700):
        raise RuntimeError("Private directory ownership or path is unsafe")


def target_identity(path, parent):
    safe_private_dir(parent)
    if path.parent != parent: raise RuntimeError("Target parent differs")
    try: st = path.lstat()
    except FileNotFoundError: return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != UID or path != path.resolve():
        raise RuntimeError("Target root is not a real runner-owned directory")
    return (st.st_dev, st.st_ino, st.st_uid)


def provenance_matches(root, label):
    if label.startswith('r18-'):
        filename = 'candidate.json'
    elif label == 'r25-run-2TyWZKgP':
        filename = 'sequence.env'
    else:
        return True
    rootfd = os.open(str(root), FLAGS)
    try:
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=rootfd)
        with os.fdopen(fd, 'rb') as source:
            st = os.fstat(source.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != UID or st.st_size > 128000:
                raise RuntimeError('Task provenance file is unsafe')
            data = source.read(128001)
        if len(data) > 128000: raise RuntimeError('Task provenance file is too large')
        if label.startswith('r18-'):
            value = json.loads(data)
            expected = {'app': 'ee66de5d100e0a9b8cf5198961556a68b24cad3e',
                        'agent': '80e19ea05d926172bbcf2588e46f6f9919776a81',
                        'runtime': '255ecda21ba904520eb867936ef173cc5f7d3ae4',
                        'pinokio': '3a6a98ae251ca126505245286e2d3e760f4348e9',
                        'manifest_sha256': '9858d63ea5609d30e09f648df35aed3a8a0f519c98bca13bbd27830686f353e3'}
            return isinstance(value, dict) and all(value.get(key) == item for key, item in expected.items())
        lines = set(data.decode('utf-8').splitlines())
        return {'agent_ref=a6b68d5b5478326a4db1971966d33a2cfe61d091',
                'pinokiod_ref=add4a674ad1ba95bb62ec427fce519fe5bf0e9bf',
                'pterm_ref=5be43718701c448b43daf0ce51d8935f170065bb',
                'pinokio_product_ref=3a6a98ae251ca126505245286e2d3e760f4348e9'}.issubset(lines)
    finally:
        os.close(rootfd)


def want_retain(label, rel):
    parts = rel.split('/')
    if label.startswith('r18-'):
        return rel.startswith('retained/evidence-upload/') and Path(rel).suffix.lower() in {'.json', '.txt', '.log', '.exit'}
    if label != 'r25-run-2TyWZKgP': return False
    root_names = {'setup.stdout.log', 'setup.stderr.log', 'sequence.env', 'route.exit', 'sequence.exit', 'cleanup.exit', 'cleanup.log'}
    if rel in root_names or rel in {'results/SHA256SUMS', 'results/summary.json'}: return True
    if len(parts) < 3 or parts[0] != 'results' or parts[1] not in {'pinokio-backend-macos', 'pinokio-fully-local-macos'}: return False
    if len(parts) == 3 and parts[2] in {'runner.exit', 'runner.stdout.log', 'runner.stderr.log'}: return True
    return parts[2] in {'diagnostics', 'output'} and Path(rel).suffix.lower() in {'.json', '.txt', '.log', '.exit', '.jsonl'}


def safe_retain_file(root, relative, data):
    """Create diagnostic copies using no-follow directory descriptors."""
    parts = relative.split('/')
    if not parts or any(part in {'', '.', '..'} for part in parts):
        raise RuntimeError("Retained file path is unsafe")
    opened = [os.open(str(root), FLAGS)]
    try:
        base = os.fstat(opened[0])
        if base.st_uid != UID or not stat.S_ISDIR(base.st_mode) or stat.S_IMODE(base.st_mode) != 0o700:
            raise RuntimeError("Retained root is unsafe")
        for part in parts[:-1]:
            try: os.mkdir(part, mode=0o700, dir_fd=opened[-1])
            except FileExistsError: pass
            child = os.open(part, FLAGS, dir_fd=opened[-1])
            st = os.fstat(child)
            if st.st_uid != UID or not stat.S_ISDIR(st.st_mode) or stat.S_IMODE(st.st_mode) != 0o700:
                os.close(child); raise RuntimeError("Retained directory is unsafe")
            opened.append(child)
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=opened[-1])
        with os.fdopen(fd, 'wb') as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
        for directory in reversed(opened): os.fsync(directory)
    finally:
        for directory in reversed(opened): os.close(directory)


def scan_tree(root, expected, label, retain_dir=None, retained_files=None):
    """Scan via dirfds, never traverse links, and optionally retain bounded diagnostics."""
    rootfd = os.open(str(root), FLAGS)
    treehash = hashlib.sha256(); deadline = time.monotonic() + MAX_SECONDS
    counts = {"files": 0, "directories": 0, "symlinks": 0, "bytes": 0,
              "retained_files": 0, "retained_bytes": 0, "retention_omissions": 0}
    try:
        st = os.fstat(rootfd)
        if (st.st_dev, st.st_ino, st.st_uid) != expected: raise RuntimeError("Target root changed during scan")
        device = st.st_dev
        def walk(fd, rel, depth):
            if depth > 128: raise RuntimeError("Target nesting exceeds safety bound")
            with os.scandir(fd) as entries:
                names = sorted(entry.name for entry in entries)
            for name in names:
                if counts['files'] + counts['directories'] + counts['symlinks'] >= MAX_FILES or time.monotonic() > deadline:
                    raise RuntimeError("Target scan exceeded safety bound")
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if st.st_uid != UID: raise RuntimeError("Target contains foreign-owned entry")
                if st.st_dev != device: raise RuntimeError("Target crosses a filesystem boundary")
                child = rel + '/' + name if rel else name
                treehash.update((child + '\0' + str((st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns)) + '\n').encode())
                if stat.S_ISLNK(st.st_mode): counts['symlinks'] += 1; continue
                if stat.S_ISDIR(st.st_mode):
                    counts['directories'] += 1
                    subfd = os.open(name, FLAGS, dir_fd=fd)
                    try:
                        now = os.fstat(subfd)
                        if (now.st_dev, now.st_ino, now.st_uid) != (st.st_dev, st.st_ino, UID): raise RuntimeError("Directory changed during scan")
                        walk(subfd, child, depth + 1)
                    finally: os.close(subfd)
                elif stat.S_ISREG(st.st_mode):
                    counts['files'] += 1; counts['bytes'] += st.st_size
                    if retain_dir is not None and want_retain(label, child):
                        if any(ord(character) < 32 for character in child):
                            raise RuntimeError("Diagnostic filename contains control characters")
                        if st.st_size > MAX_FILE or counts['retained_bytes'] + st.st_size > MAX_RETAINED:
                            counts['retention_omissions'] += 1; continue
                        srcfd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                        try:
                            before = os.fstat(srcfd)
                            if (before.st_dev, before.st_ino, before.st_uid) != (st.st_dev, st.st_ino, UID): raise RuntimeError("Evidence file changed")
                            with os.fdopen(srcfd, 'rb', closefd=False) as stream: data = stream.read(MAX_FILE + 1)
                            after = os.fstat(srcfd)
                            if len(data) > MAX_FILE or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns): raise RuntimeError("Evidence file changed")
                        finally: os.close(srcfd)
                        safe_retain_file(retain_dir, child, data)
                        if retained_files is not None:
                            retained_files.append((child, hashlib.sha256(data).hexdigest()))
                        counts['retained_files'] += 1; counts['retained_bytes'] += len(data)
                else: raise RuntimeError("Target contains unsupported file type")
        walk(rootfd, '', 0)
    finally: os.close(rootfd)
    counts['tree_sha256'] = treehash.hexdigest()
    return counts


def no_open_files(path):
    result = subprocess.run(['/usr/sbin/lsof', '-n', '+D', str(path)], capture_output=True, text=True, timeout=180)
    return result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip()


def no_process_reference(path):
    result = subprocess.run(['/bin/ps', '-ww', '-U', str(UID), '-o', 'pid=,command='], capture_output=True,
                            text=True, timeout=20, check=True)
    return not any(str(path) in line for line in result.stdout.splitlines())


def checkpoint(path, audit):
    fd, name = tempfile.mkstemp(prefix='.audit-', dir=str(path.parent))
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as out:
        json.dump(audit, out, indent=2, sort_keys=True); out.write('\n'); out.flush(); os.fsync(out.fileno())
    os.replace(name, str(path))
    dirfd = os.open(str(path.parent), FLAGS)
    try: os.fsync(dirfd)
    finally: os.close(dirfd)


def guarded_remove(target, parent, label, empty_only, retained, record, save):
    try:
        identity = target_identity(target, parent)
        if identity is None:
            record['status'] = 'already-absent'; save(); return
        if not provenance_matches(target, label):
            raise RuntimeError('Task provenance does not match inventoried run')
        record['provenance'] = 'matched' if not empty_only else 'inventoried-empty-root'
        if not no_process_reference(target) or not no_open_files(target):
            raise RuntimeError('Target is referenced by a process or cannot be verified idle')
        retained_files = []
        record['before'] = scan_tree(target, identity, label, retained, retained_files)
        # Persist source-derived hashes without rewalking the retained tree.
        manifest_data = ''.join(digest + '  ' + name + '\n' for name, digest in retained_files).encode()
        safe_retain_file(retained, 'RETAINED-SHA256SUMS', manifest_data)
        record['retained_manifest_sha256'] = hashlib.sha256(manifest_data).hexdigest()
        save()
        if empty_only and (record['before']['files'] or record['before']['directories'] or record['before']['symlinks']):
            raise RuntimeError('Expected empty task root is not empty')
        if not no_process_reference(target) or not no_open_files(target):
            raise RuntimeError('Target became busy before removal')
        second = scan_tree(target, identity, label)
        if second['tree_sha256'] != record['before']['tree_sha256']:
            raise RuntimeError('Target changed before removal')
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError('Safe directory removal is unavailable')
        parentfd = os.open(str(parent), FLAGS)
        try:
            check = os.stat(target.name, dir_fd=parentfd, follow_symlinks=False)
            if (check.st_dev, check.st_ino, check.st_uid) != identity: raise RuntimeError('Target identity changed')
            record['status'] = 'removal-started'; save()
            shutil.rmtree(target.name, dir_fd=parentfd)
            os.fsync(parentfd)
        finally: os.close(parentfd)
        record['status'] = 'removed'; save()
    except Exception as exc:
        if record.get('status') == 'removal-started': record['status'] = 'removal-incomplete-review-required'
        else: record['status'] = 'left-guard-refused'
        record['reason'] = str(exc) if isinstance(exc, RuntimeError) else 'Filesystem or inspection error'
        record['error_type'] = type(exc).__name__
        save()


def main():
    if os.getuid()!=UID or os.environ.get('RUNNER_NAME')!='hannah-mac' or sys.platform!='darwin': raise RuntimeError('Unexpected runner identity')
    home=Path(pwd.getpwuid(UID).pw_dir)
    if home.is_symlink() or home.resolve()!=home or home.stat().st_uid!=UID: raise RuntimeError('Unsafe runner home')
    base=home/'.astrid-r1fix-stopped'; safe_private_dir(base)
    temp=Path(os.environ['RUNNER_TEMP'])
    if temp != home/'actions-runner/_work/_temp' or temp.resolve()!=temp or temp.stat().st_uid!=UID: raise RuntimeError('Unexpected temp')
    output=temp/'r1fix-clean'; output.mkdir(mode=0o700)
    with open(os.environ['GITHUB_ENV'],'a') as f: f.write('R1FIX_CLEAN_UPLOAD='+str(output)+'\n')
    auditdir=home/'.astrid-r1fix-clean-audits'; safe_private_dir(auditdir,create=True)
    runid=os.environ.get('GITHUB_RUN_ID','')+'-'+os.environ.get('GITHUB_RUN_ATTEMPT','')
    if not re.fullmatch(r'[0-9]+-[0-9]+',runid): raise RuntimeError('Invalid run id')
    auditpath=auditdir/(runid+'.json')
    audit={'schema':'r1fix-source-clean/v1','before':shutil.disk_usage(home).free,'runs':{},'preserved':['task workspace and installed program','task evidence and private logs'],'status':'checking'}
    def save(): checkpoint(auditpath,audit)
    save()
    runs=sorted(base.iterdir())
    if len(runs)!=2 or any(not re.fullmatch(r'task\.[A-Za-z0-9_]{8}',p.name) for p in runs): raise RuntimeError('Task root inventory differs')
    expected={'driver':'997d8c3d95971170b812b8c415fbbf5c1656edd0','agent':'30761f4a3916ce6e5822669dd5865f65a4e6654a','runtime':'de98a5aa44768419656bfeffcdaa1b2eda9e9845','int-app':'878c28df7a6c80fa9aaa6540c5fccad95473e7c1','candidate-app':'37ac4d709e9bb211f88b8df6520a0d0e2de0228e','r0-launcher':'bbba620e3d89eeff8021417f365e6ea584203105'}
    identified={}
    for run in runs:
        safe_private_dir(run)
        result=run/'evidence/result.json'
        if result.is_symlink() or not result.is_file() or result.stat().st_uid!=UID: raise RuntimeError('Missing task result')
        value=json.loads(result.read_text())
        stages=value.get('stages',{})
        label='retry' if stages.get('r0-stopped-and-lock-free')=='pass' else 'first' if stages.get('r0-observe')=='pass' and stages.get('seed')!='pass' else None
        if not label or label in identified or value.get('cleanup')!='owned service stopped; workspace retained': raise RuntimeError('Task result identity differs')
        identified[label]=run
        for name,sha in expected.items():
            target=run/name
            identity=target_identity(target,run)
            if identity is None: raise RuntimeError('Expected checkout absent')
            found=subprocess.run(['/usr/bin/git','-C',str(target),'-c','core.hooksPath=/dev/null','rev-parse','HEAD'],capture_output=True,text=True,timeout=20,env={'PATH':'/usr/bin:/bin','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_SYSTEM':'/dev/null','GIT_NO_REPLACE_OBJECTS':'1'},check=True).stdout.strip()
            if found!=sha: raise RuntimeError('Checkout source identity differs')
    if set(identified)!={'first','retry'}: raise RuntimeError('Expected two distinct task receipts')
    for label,run in sorted(identified.items()):
        audit['runs'][label]={}
        for name in expected:
            target=run/name; record=audit['runs'][label][name]={'status':'checking'}; save()
            try:
                identity=target_identity(target,run)
                if not no_process_reference(target) or not no_open_files(target): raise RuntimeError('Target busy or not verified')
                first=scan_tree(target,identity,'source-only'); record['before']=first; save()
                if not no_process_reference(target) or not no_open_files(target): raise RuntimeError('Target became busy')
                second=scan_tree(target,identity,'source-only')
                if first['tree_sha256']!=second['tree_sha256']: raise RuntimeError('Target changed')
                if not shutil.rmtree.avoids_symlink_attacks: raise RuntimeError('Safe rmtree unavailable')
                fd=os.open(str(run),FLAGS)
                try:
                    st=os.stat(name,dir_fd=fd,follow_symlinks=False)
                    if (st.st_dev,st.st_ino,st.st_uid)!=identity: raise RuntimeError('Target identity changed')
                    record['status']='removal-started'; save(); shutil.rmtree(name,dir_fd=fd); os.fsync(fd)
                finally: os.close(fd)
                record['status']='removed'
            except Exception as exc:
                record['status']='failed'; record['reason']=str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__
            save()
    audit['after']=shutil.disk_usage(home).free
    audit['status']='complete' if all(r['status']=='removed' for tasks in audit['runs'].values() for r in tasks.values()) else 'partial'
    save(); shutil.copyfile(auditpath,output/'audit.json')
    print(json.dumps({'status':audit['status'],'before':audit['before'],'after':audit['after'],'runs':audit['runs']}))
    if audit['status']!='complete': raise SystemExit(1)

if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'status':'preflight-failed','error_type':type(exc).__name__})); raise SystemExit(1)
