"""Focused hosted proof; each destructive operation is limited to our fresh roots."""
import hashlib, json, os, pathlib, platform, plistlib, re, shutil, socket, stat, subprocess, sys, tempfile, time, traceback
P = pathlib.Path
WS = P(os.environ['GITHUB_WORKSPACE']).resolve()
OUT = WS / 'out'
AGENT, RUNTIME, APP = (WS / x for x in ('agent','runtime','app'))
EXPECTED = {'agent':'098ae387521b3adb782f9b62bf60aa3ba2feb711', 'runtime':'a95e0e836234bc6d27ba557ba1297da95ed55290', 'app':'2145dc85b21f3c5a22f96a842c0d257ae29051e5'}
LABEL = 'haus.astrid.runtime'
def record(name, value):
    (OUT / name).write_text(json.dumps(value, sort_keys=True, indent=2, default=str) + '\n')
def run(args, **kw):
    print('+', repr([str(x) for x in args]), flush=True)
    p = subprocess.run([str(x) for x in args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    print('exit',p.returncode, '\n',p.stdout, flush=True)
    return p

def inspect():
    from astrid.installer.service import (Service, LABEL, platform as native, absolute,
        bootstrap_root, owned_by_current_user, birth_ids_match)
    home, root, data = (P(os.environ[x]) for x in ('PROOF_HOME','PROOF_ROOT','PROOF_DATA'))
    def launch(args):
        p = run(args, timeout=30)
        if p.returncode: raise subprocess.CalledProcessError(p.returncode, args, output=p.stdout)
        return p.stdout
    svc = Service(root, home / 'runtime.log', launch, home=home, system='darwin')
    deadline = time.monotonic()+180
    while True:
        try:
            discovery = json.loads((data/'runtime/discovery.json').read_text())
            pid, birth = discovery['pid'], discovery['process_birth_id']
            identity = svc.admit_launchd_bootout(data, owner_pid=pid, owner_birth_id=birth)
            if not identity or not identity['live_owner']: raise ValueError('not running')
            break
        except (FileNotFoundError, KeyError, ValueError, OSError) as e:
            if time.monotonic() >= deadline: raise RuntimeError('selected R0 owner not admitted: '+str(e)) from e
            time.sleep(3)
    observed = native.process_identity(pid); exe = P(native.process_executable(pid)).resolve(strict=True)
    program = P(identity['program']); config = program.parent.parent/'pyvenv.cfg'
    live = identity['live_owner']; retained = svc.retain_launchd_bootout_identity(identity)
    checks = {'pid_matches': observed.get('pid') == pid == live['pid'],
              'uid_matches': observed.get('uid') == os.getuid() == live['uid'],
              'birth_retained': live['process_birth_id'] == observed.get('birth_id'),
              'program_resolves_to_executable': program.resolve(strict=True) == exe,
              'external_to_selected_root': not exe.is_relative_to(root.resolve()),
              'trusted_interpreter': svc._trusted_launchd_interpreter(program, exe),
              'v2_witness': live['format'] == 'astrid-launchd-r0-live-owner/v2',
              'retained_matches': svc.launchd_bootout_identity_matches(identity, retained)}
    record('native-owner.json', {'discovery_pid':pid, 'discovery_birth_id':birth, 'observed':observed,
        'program':str(program),'executable':str(exe),'config':config.read_text(),
        'config_sha256':hashlib.sha256(config.read_bytes()).hexdigest(),
        'executable_sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),
        'live_owner':live,'predicates':checks})
    if not all(checks.values()): raise AssertionError('native owner predicates did not all pass')
    # Focused private-helper negative, not full product service admission.
    # The copy is confined to a fresh proof-owned sibling of both selected root and home.
    scratch = home.parent.resolve(strict=True)
    foreign_scratch = P(tempfile.mkdtemp(prefix='foreign-', dir=scratch)).resolve(strict=True)
    foreign = foreign_scratch/'python'; shutil.copytree(exe.parent.parent, foreign, symlinks=True)
    foreign_exe = foreign/'bin'/exe.name
    uv = home/'.local/share/astrid/bootstrap/uv'
    fvenv = root/'versions'/'foreign-proof'/'py'; fvenv.parent.mkdir(parents=True)
    p = run([uv,'venv',fvenv,'--python',foreign_exe], timeout=180)
    if p.returncode: raise RuntimeError('foreign venv failed')
    foreign_program = fvenv/'bin/python'
    target_exe = foreign_program.resolve(strict=True)
    bootstrap_python = absolute(bootstrap_root(home, 'darwin')/'python')
    if not target_exe.is_relative_to(foreign.resolve()): raise RuntimeError('foreign venv did not use foreign interpreter')
    if target_exe.is_relative_to(root.resolve(strict=True)): raise AssertionError('foreign target inside selected root')
    if target_exe.is_relative_to(bootstrap_python): raise AssertionError('foreign target inside bootstrap python')
    label = 'org.astrid.proof.r18.' + os.environ['GITHUB_RUN_ID'] + '.' + os.environ['GITHUB_RUN_ATTEMPT']
    path = home/'foreign-proof.plist'; log = home/'foreign-proof.log'
    path.write_bytes(plistlib.dumps({'Label':label,'ProgramArguments':[str(foreign_program),'-c','import time; time.sleep(300)'], 'RunAtLoad':True,'StandardOutPath':str(log),'StandardErrorPath':str(log)}))
    target = f'gui/{os.getuid()}/{label}'
    before = run(['launchctl','print',target],timeout=30)
    if before.returncode == 0 or not ('Could not find service' in before.stdout or 'Could not find specified service' in before.stdout): raise RuntimeError('foreign label not proven absent')
    loaded = False
    try:
        p = run(['launchctl','bootstrap',f'gui/{os.getuid()}',path],timeout=30)
        if p.returncode: raise RuntimeError('foreign launchd bootstrap failed')
        loaded = True
        deadline = time.monotonic()+30
        while True:
            state = run(['launchctl','print',target],timeout=30)
            match = re.search(r'^\s*pid = (\d+)\s*$',state.stdout,re.M)
            if state.returncode == 0 and match and str(path) in state.stdout: break
            if time.monotonic()>deadline: raise RuntimeError('foreign process not running')
            time.sleep(1)
        fpid = int(match.group(1)); fo = native.process_identity(fpid)
        fexe = P(native.process_executable(fpid)).resolve(strict=True)
        if fexe != target_exe or fo.get('uid') != os.getuid() or fo.get('pid') != fpid: raise AssertionError('foreign native identity mismatch')
        # Independently record each input predicate, including those the product
        # short-circuits after rejecting bootstrap containment.
        config = fvenv/'pyvenv.cfg'; config_text = config.read_text(encoding='utf-8')
        homes = [v.strip() for line in config_text.splitlines() for k, sep, v in [line.partition('=')] if sep and k.strip().lower() == 'home']
        def custody(path):
            st = path.lstat()
            return {'path':str(path), 'uid':st.st_uid, 'gid':st.st_gid,
                    'mode':oct(stat.S_IMODE(st.st_mode)), 'regular':stat.S_ISREG(st.st_mode),
                    'symlink':stat.S_ISLNK(st.st_mode), 'owned_by_current_user':owned_by_current_user(path, st),
                    'safe_mode':not bool(stat.S_IMODE(st.st_mode) & 0o022)}
        foreign_entries = [foreign_scratch, foreign, foreign/'bin', fexe]
        bootstrap_entries = [bootstrap_python.parent, bootstrap_python]
        for part in exe.relative_to(bootstrap_python).parts:
            bootstrap_entries.append(bootstrap_entries[-1]/part)
        foreign_custody = [custody(p) for p in foreign_entries]
        bootstrap_custody = [custody(p) for p in bootstrap_entries]
        cstat = custody(config)
        again = native.process_identity(fpid)
        program_parent = absolute(foreign_program.parent)
        venv_absolute = absolute(foreign_program.parent.parent)
        configured_home = P(homes[0]) if len(homes) == 1 and homes[0] else None
        checks = {
            'state_running': True, 'pid_matches': fo.get('pid') == fpid == again.get('pid'),
            'uid_matches': fo.get('uid') == os.getuid() == again.get('uid'),
            'birth_matches_reobservation': isinstance(fo.get('birth_id'), str) and birth_ids_match(fo['birth_id'], again.get('birth_id'), fpid, again.get('pid')),
            'program_absolute_normalized': foreign_program.is_absolute() and foreign_program == P(os.path.abspath(foreign_program)),
            'executable_absolute_normalized': fexe.is_absolute() and fexe == P(os.path.abspath(fexe)),
            'program_parent_link_free': program_parent == foreign_program.parent,
            'program_and_parent_inside_root': foreign_program.is_relative_to(root.resolve()) and program_parent.is_relative_to(root.resolve()),
            'program_resolves_to_observed_executable': foreign_program.resolve(strict=True) == target_exe == fexe,
            'executable_canonical_regular': fexe.resolve(strict=True) == fexe and stat.S_ISREG(fexe.lstat().st_mode),
            'outside_selected_root': not fexe.is_relative_to(root.resolve(strict=True)),
            'darwin_and_program_symlink': sys.platform == 'darwin' and foreign_program.is_symlink(),
            'venv_link_free': venv_absolute == fvenv,
            'config_regular_owned_safe': cstat['regular'] and cstat['owned_by_current_user'] and cstat['safe_mode'],
            'one_absolute_normalized_link_free_home_matching_executable_parent': configured_home is not None and configured_home.is_absolute() and configured_home == P(os.path.abspath(configured_home)) and absolute(configured_home) == configured_home == fexe.parent,
            'bootstrap_python_directory': bootstrap_python.is_dir(),
            'outside_bootstrap_python': not fexe.is_relative_to(bootstrap_python),
            'foreign_copy_owned_safe': all(c['owned_by_current_user'] and c['safe_mode'] for c in foreign_custody),
            'copied_executable_hash_matches_source': hashlib.sha256(fexe.read_bytes()).digest() == hashlib.sha256(exe.read_bytes()).digest(),
            'bootstrap_to_positive_executable_owned_safe': all(c['owned_by_current_user'] and c['safe_mode'] for c in bootstrap_custody),
        }
        trusted = svc._trusted_launchd_interpreter(foreign_program,fexe)
        refused = False
        try:
            svc._launchd_live_pid_is_bound({'program':str(foreign_program)}, {'state':'running','pid':fpid}, owner_pid=fpid, owner_birth_id=fo['birth_id'])
        except ValueError as e:
            refused = True; reason = str(e)
        record('foreign-owner.json', {'scope':'private live-owner helper with self-supplied birth; not full product admission',
            'target':target,'pid':fpid,'observed':fo,'reobserved':again,'program':str(foreign_program),
            'program_link_target':os.readlink(foreign_program) if foreign_program.is_symlink() else None,
            'executable':str(fexe),'executable_sha256':hashlib.sha256(fexe.read_bytes()).hexdigest(),
            'source_executable_sha256':hashlib.sha256(exe.read_bytes()).hexdigest(),
            'config':config_text,'config_sha256':hashlib.sha256(config.read_bytes()).hexdigest(),
            'config_custody':cstat,'foreign_custody':foreign_custody,'bootstrap_custody':bootstrap_custody,
            'bootstrap_python':str(bootstrap_python),'foreign_scratch':str(foreign_scratch),
            'predicates':checks,'bootstrap_target_containment':False,
            'bootstrap_to_foreign_custody':'not applicable: target outside bootstrap',
            'trusted_interpreter':trusted,'refused':refused,'reason':reason if refused else None})
        if not all(checks.values()): raise AssertionError('foreign fixture or identity predicates did not all pass')
        if trusted: raise AssertionError('foreign interpreter trusted')
        if not refused: raise AssertionError('foreign interpreter admitted')
    finally:
        if loaded:
            state = run(['launchctl','print',target],timeout=30)
            if state.returncode or str(path) not in state.stdout: raise RuntimeError('foreign cleanup refused: service changed')
            p = run(['launchctl','bootout',target],timeout=30)
            record('foreign-cleanup.json',{'exit':p.returncode, 'target':target})
            if p.returncode: raise RuntimeError('foreign cleanup failed')
    return 0

def main():
    OUT.mkdir(exist_ok=True)
    if sys.platform != 'darwin' or os.getuid() == 0: raise RuntimeError('requires macOS non-root')
    for name, sha in EXPECTED.items():
        p = run(['git','-C',WS/name,'rev-parse','HEAD'],timeout=30)
        if p.returncode or p.stdout.strip()!=sha: raise RuntimeError('wrong '+name+' checkout')
    manifest = APP/'public/install-manifest.json'; raw=manifest.read_bytes(); doc=json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != '528615bf87637471ef38072e6899e745bdba7e54d5a65436b1bac5af3447b45c' or doc['agent']['sha'] != EXPECTED['agent'] or doc['runtime']['sha'] != EXPECTED['runtime'] or doc['release'] != 1: raise RuntimeError('candidate manifest mismatch')
    fixture=AGENT/'tests/fixtures/release0'; checks=run(['shasum','-a','256','-c','SHA256SUMS'],cwd=fixture,timeout=30)
    if checks.returncode: raise RuntimeError('R0 fixture checksum mismatch')
    if hashlib.sha256((fixture/'SHA256SUMS').read_bytes()).hexdigest() != '58221a50aa7940e3121d6239ddeb92d6409df3711096aac1161c7ec130b755c0': raise RuntimeError('R0 checksum manifest mismatch')
    domain=f'gui/{os.getuid()}'; target=domain+'/'+LABEL
    if run(['launchctl','print',domain],timeout=30).returncode: raise RuntimeError('GUI domain unavailable')
    existing=run(['launchctl','print',target],timeout=30)
    if existing.returncode == 0 or not ('Could not find service' in existing.stdout or 'Could not find specified service' in existing.stdout): raise RuntimeError('label not demonstrably absent')
    for address in ('127.0.0.1','::1'):
        with socket.socket(socket.AF_INET6 if ':' in address else socket.AF_INET, socket.SOCK_STREAM) as s: s.bind((address,17340))
    scratch=P(tempfile.mkdtemp(prefix='r18-mac-',dir=os.environ['RUNNER_TEMP'])).resolve(); home=scratch/'home'; root=scratch/'program'; data=scratch/'data'; home.mkdir(mode=0o700)
    env=dict(os.environ, HOME=str(home), PROOF_HOME=str(home), PROOF_ROOT=str(root), PROOF_DATA=str(data), BROWSER='true', UV_CACHE_DIR=str(scratch/'uv-cache'))
    record('preflight.json',{'os':platform.platform(),'uid':os.getuid(),'domain':domain,'port_17340':'free ipv4 and ipv6','label':'absent','scratch':str(scratch),'candidate_manifest_sha256':hashlib.sha256(raw).hexdigest()})
    install_started=False; result=1
    try:
        install_started=True
        p=run(['sh',fixture/'served/install','--manifest',fixture/'served/install-manifest.json','--root',root,'--data-root',data],env=env,timeout=3600)
        record('bootstrap-exit.json',{'exit':p.returncode})
        (OUT/'bootstrap.log').write_text(p.stdout)
        if p.returncode: raise RuntimeError('R0 bootstrap failed')
        current=(root/'current').read_text().strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]+',current): raise RuntimeError('invalid selected version')
        version=json.loads((root/'versions'/current/'version.json').read_text())
        py=P(version['python']);
        if not py.is_relative_to(root/'versions'/current): raise RuntimeError('unexpected selected python')
        record('installed.json',{'marker':json.loads((root/'install.json').read_text()),'current':current,'version':version,'r0_manifest':json.loads((fixture/'served/install-manifest.json').read_text())})
        env['PYTHONPATH']=str(AGENT)+os.pathsep+str(RUNTIME)
        p=run([py,P(__file__).resolve(),'--inspect'],env=env,timeout=500)
        record('inspection-exit.json',{'exit':p.returncode})
        if p.returncode: raise RuntimeError('native inspection failed')
        result=0
    finally:
        plist=home/'Library/LaunchAgents'/f'{LABEL}.plist'
        state=run(['launchctl','print',target],timeout=30)
        cleanup={'target':target,'state_exit':state.returncode,'bootout':'not attempted'}
        if state.returncode==0:
            # A loaded job is touched only if the observed path and its exact program point inside our root.
            try:
                d=plistlib.loads(plist.read_bytes()); argv=d['ProgramArguments']
                if str(plist) not in state.stdout or d['Label']!=LABEL or not P(argv[0]).is_relative_to(root/'versions') or str(root) not in state.stdout: raise ValueError('loaded job is not ours')
                p=run(['launchctl','bootout',target],timeout=30); cleanup['bootout']=p.returncode
                if p.returncode: raise RuntimeError('bootout failed')
            except Exception as e: cleanup['error']=repr(e); result=1
        elif 'Could not find service' not in state.stdout and 'Could not find specified service' not in state.stdout:
            cleanup['error']='could not establish service absence'; result=1
        if plist.exists():
            (OUT/'launchagent.plist').write_bytes(plist.read_bytes())
        log=home/'Library/Logs/Astrid Runtime/install.log'
        if log.exists(): shutil.copyfile(log,OUT/'r0-install.log')
        cleanup['scratch_retained_until_runner_disposal']=str(scratch)
        record('cleanup.json',cleanup)
    return result
if __name__ == '__main__':
    try: sys.exit(inspect() if '--inspect' in sys.argv else main())
    except Exception:
        traceback.print_exc(); sys.exit(1)
