"""H91 single fresh install, with a CPU-only preparation/rehearsal mode."""
import argparse
import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import time

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
CACHE = Path('/workspace/astrid/engines/vibecomfy-h3/models')
IMAGE = 'runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 << 20), b''):
            h.update(block)
    return h.hexdigest()


def git(repo, *args, **kwargs):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, **kwargs)


def install_diagnostics(paths):
    result = {'schema': 'h91.installer-diagnostics.v1', 'files': []}
    for path in paths:
        if not path.is_file() or path.is_symlink():
            continue
        text = path.read_text(errors='replace')
        sites = []
        for module, line, function in re.findall(r'File "[^"\n]*/(astrid/[^"\n]+\.py)", line ([0-9]+), in ([A-Za-z_][A-Za-z_0-9]*)', text):
            sites.append(dict(module=module, line=int(line), function=function))
        result['files'].append(dict(file=path.name, bytes=path.stat().st_size, sha256=digest(path),
                                    source_sites=sites[-40:], http_codes=re.findall(r'HTTP Error ([45][0-9]{2})', text)[-10:],
                                    exception_types=re.findall(r'^([A-Za-z_][A-Za-z_0-9]*(?:Error|Exception)):', text, re.M)[-15:],
                                    missing_modules=re.findall(r"ModuleNotFoundError: No module named '([A-Za-z0-9_.-]+)'", text)[-10:]))
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--rehearse', action='store_true')
    parser.add_argument('--pod', action='store_true')
    parser.add_argument('--canary', action='store_true')
    parser.add_argument('--canary-lane', type=Path)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--job', type=Path, required=True)
    args = parser.parse_args()
    if sum((args.rehearse,args.pod,args.canary)) != 1 or not args.root.is_absolute() or not args.job.is_absolute():
        parser.error('choose one mode and absolute paths')
    root, job = args.root, args.job
    global CACHE
    if args.canary:
        assert args.canary_lane and args.canary_lane.is_absolute()
        CACHE = root/'model-cache'
    if root.exists():
        raise ValueError('fresh task root required')
    if args.pod and (root != Path('/opt/astrid-h91r2') or job != Path('/workspace/jobs/H-91r2-h3-r8-fresh-install')):
        raise ValueError('pod paths differ')
    assert shutil.disk_usage(root.parent).free >= 2_000_000_000, 'free disk below 2 GB'
    root.mkdir(mode=0o700)
    out = job / 'out'
    out.mkdir(parents=True, exist_ok=True)
    for name in ('home', 'tmp', 'cache', 'source-repos', 'selected-source'):
        (root/name).mkdir(mode=0o700)
    # A selected environment, without inspecting or forwarding ambient credentials.
    env = {'HOME': str(root/'home'),
           'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
           'TMPDIR': str(root/'tmp'), 'PYTHONDONTWRITEBYTECODE': '1',
           'UV_CACHE_DIR': str(root/'cache/uv'), 'XDG_CACHE_HOME': str(root/'cache/xdg'),
           'UV_PYTHON_INSTALL_DIR': str(root/'cache/python'), 'UV_PYTHON_PREFERENCE': 'only-managed',
           'XDG_DATA_HOME': str(root/'user-data'), 'XDG_STATE_HOME': str(root/'state'),
           'UV_SYSTEM_CERTS': 'true', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_NO_REPLACE_OBJECTS': '1'}
    if args.canary:
        env['ASTRID_RUNTIME_DOOR_PORT'] = '19291'
        env['NODE_EXTRA_CA_CERTS'] = '/etc/ssl/certs/ca-certificates.crt'
        env['UV_NO_CACHE'] = '1'  # task-local ephemeral cache; frozen hashes remain mandatory
    Path(env['HOME']).mkdir(parents=True, exist_ok=True)
    os.environ.clear()
    os.environ.update(env)
    def write(name, value):
        (out/name).write_text(json.dumps(value, sort_keys=True, indent=2)+'\n')
    def phase(name, **values):
        line = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())+' '+name
        with (job/'progress.log').open('a') as stream:
            stream.write(line+'\n')
        print(line, json.dumps(values, sort_keys=True), flush=True)
    phase('bootstrap-start')
    sources = json.loads((HERE/'sources.json').read_text())
    for name, record in sources.items():
        pack = HERE/(name+'.pack')
        assert pack.stat().st_size == record['pack_bytes'] and digest(pack) == record['pack_sha256']
        repo = root/'source-repos'/(name+'.git')
        subprocess.run(['git','init','--bare',str(repo)], capture_output=True, check=True, env=env)
        with pack.open('rb') as stream:
            subprocess.run(['git','-C',str(repo),'index-pack','--stdin'], stdin=stream, capture_output=True, check=True, env=env)
        # Only the exact selected commit/tree are transported, never full history.
        (repo/'shallow').write_text(record['sha']+'\n')
        git(repo, 'update-ref', 'refs/heads/h91-source', record['sha'])
        git(repo, 'symbolic-ref', 'HEAD', 'refs/heads/h91-source')
        assert git(repo,'rev-parse','HEAD^{tree}',text=True).stdout.strip() == record['tree']
        git(repo, 'fsck', '--full')
    manifest = json.loads((HERE/'release-install-manifest.json').read_text())
    for name in ('agent','runtime','vibecomfy'):
        manifest[name] = {**manifest.get(name, {}), 'sha': sources[name]['sha'], 'repo': str(root/'source-repos'/(name+'.git'))}
    script = (HERE/'install.sh').read_text()
    for tool, key in (('UV_VERSION','uv'),('PYTHON_VERSION','python')):
        assert re.search(r'(?m)^'+tool+'='+re.escape(manifest['tools'][key]['version'])+'$',script)
    # Restore selected pure Python sources using the same git-archive source
    # contract as the official installer. No environment/workspace is created.
    import tarfile
    for name in ('agent','runtime','harness'):
        repo = root/'source-repos'/(name+'.git')
        archive = root/'tmp'/(name+'.tar')
        git(repo,'archive','--format=tar','--output',str(archive),sources[name]['sha'])
        dest = root/'selected-source'/name
        dest.mkdir()
        with tarfile.open(archive) as bundle:
            # Trees were verified to contain regular files/directories only.
            for member in bundle:
                assert member.isdir() or member.isfile()
                assert not Path(member.name).is_absolute() and '..' not in Path(member.name).parts
            bundle.extractall(dest, filter='data')
        archive.unlink()
    agent, runtime = root/'selected-source/agent', root/'selected-source/runtime'
    harness = root/'selected-source/harness'
    subprocess.run([sys.executable, '-B', str(harness/'canary.py'), 'qualify', '--help'], env=env, capture_output=True, text=True, check=True)
    sys.path[:0] = [str(agent),str(runtime)]
    from astrid.installer.tools import validate_manifest, local_source
    from astrid.installer.cli import parser as installer_parser
    from astrid.core import engine_cli
    from astrid.core.generation import engine_runner as runner, engine_recipe, model_downloader
    assert json.loads((agent/'runtime.lock.json').read_text())['source_commit'] == sources['runtime']['sha']
    validate_manifest(manifest)
    manifest_path = root/'install-manifest.json'
    manifest_path.write_text(json.dumps(manifest,indent=2)+'\n')
    write('source-provenance.json', sources)
    write('install-manifest.json', manifest)
    program, data, engine = root/'program', root/'data', root/'engine'
    flags = ['--root',str(program),'--data-root',str(data),'--manifest',str(manifest_path)]
    if args.canary:
        flags.append('--sandbox')  # supported installer process-control for this host
    installer_parser().parse_args(flags)
    engine_args = ['install','vibecomfy-h3','--platform','linux-x86_64-cu130','--placement','local',
                   '--root',str(engine),'--model-cache-root',str(CACHE),'--deadline','3000']
    engine_cli._parser().parse_args(engine_args)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        assert engine_cli.main(engine_args+['--dry-run']) == 0
    plan = json.loads(captured.getvalue())
    write('canonical-plan.json', plan)
    # Parse and resolve the bootstrap's real source-requirement code without
    # executing its uv/environment creation or the full installer on this host.
    embedded = script.split("python -u -I -c '\n",1)[1].split("\n' \"$manifest\"",1)[0]
    ast.parse(embedded)
    prefix = embedded.split('uv = os.environ["ASTRID_INSTALL_UV"]',1)[0]
    prior_argv = sys.argv
    try:
        sys.argv = ['bootstrap',str(manifest_path),*flags]
        namespace = {}
        exec(compile(prefix,'hosted-bootstrap-source-resolution','exec'),namespace)
        requirements = {name:namespace['git_requirement'](name) for name in ('agent','runtime')}
    finally:
        sys.argv = prior_argv
    for name in ('agent','runtime','vibecomfy'):
        archive = root/'tmp'/(name+'-verified.tar')
        local_source(manifest[name]['repo'],sources[name]['sha'],archive)
        archive.unlink()
    write('preflight.json', dict(status='passed', mode='rehearsal' if args.rehearse else 'pod',
          install_argv=['sh',str(HERE/'install.sh'),*flags], bootstrap_requirements=requirements,
          source_resolution='verified committed trees', generation_entry='install-only job; real H3 entry inventory is separately required before model/inference work',
          local_stop='canary executes official installer and canonical engine; finite-command observer stops before gpu_sanity' if args.canary else 'pre-installer parser rehearsal only',
          python_selector='UV_PYTHON_PREFERENCE=only-managed; no --managed-python in this bootstrap invocation',
          engine_python_selector='recipe-owned --managed-python; UV_PYTHON_PREFERENCE removed for engine',
          uv=manifest['tools']['uv']['version']))
    # Rehearse the diagnostic child wrapper against real selected helper code,
    # including a deliberate identity mismatch, without a supervisor/installer.
    spec = importlib.util.spec_from_file_location('h91_capture',HERE/'capture.py')
    capture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(capture)
    recipe = engine_recipe.load_recipe('vibecomfy-h3','linux-x86_64-cu130')
    payload = dict(schema=runner._HELPER_SCHEMA,engine=recipe.engine,platform=recipe.platform,root=str(engine),
                   recipe_sha256=engine_recipe.recipe_digest(recipe),helper_sha256=runner._sha(runner.helper_manifest(recipe)))
    helper_program = (f'import sys; sys.dont_write_bytecode=True; sys.path.insert(0, {str(agent)!r}); sys.path.insert(0, {str(runtime)!r}); '
                      'from astrid.core.generation.engine_runner_target import main; raise SystemExit(main())')
    checks = []
    for valid in (True, False):
        request = {**payload, 'helper_sha256':payload['helper_sha256'] if valid else '0'*64}
        argv = [sys.executable,'-I','-c',capture.instrument(helper_program),'identity',json.dumps(request)]
        done = subprocess.run(argv,env=env,capture_output=True,text=True,check=True)
        facts = json.loads(done.stdout)['facts']
        assert facts == ({'helper_sha256':payload['helper_sha256']} if valid else {'error_code':'integrity_failed'})
        sites = [json.loads(line[len(capture.PREFIX):]) for line in done.stderr.splitlines() if line.startswith(capture.PREFIX)]
        assert valid or any(site['type']=='IntegrityError' and site['function']=='dispatch' for site in sites)
        checks.append(dict(valid_identity=valid, facts=facts, sites=sites))
    write('capture-rehearsal.json',checks)
    phase('bootstrap-source-parser-rehearsal-passed')
    if args.rehearse:
        return 0
    if args.pod:
        # Provider facts only; no credentials, pod metadata endpoints, or private inputs.
        mounted = [row.split() for row in Path('/proc/self/mountinfo').read_text().splitlines() if row.split()[4]=='/workspace']
        assert len(mounted)==1 and os.stat('/workspace').st_dev != os.stat('/opt').st_dev
        facts = dict(image=IMAGE,container_disk_gb_requested=150,local_disk=shutil.disk_usage(root)._asdict(),
                     workspace_disk=shutil.disk_usage('/workspace')._asdict(),storage_mount=mounted[0],
                     datacenter='broker result.json is authoritative',python=sys.version,source_tuple=sources,
                     paths=dict(root=str(root),program=str(program),data=str(data),engine=str(engine),cache=str(CACHE),
                                home=env['HOME'],uv_cache=env['UV_CACHE_DIR'],output=str(out)))
        gpu = subprocess.run(['nvidia-smi'],env=env,capture_output=True,text=True)
        (out/'nvidia-smi.txt').write_text(gpu.stdout)
        facts['nvidia_smi_exit'] = gpu.returncode
        write('setup-facts.json',facts)
        models = engine_recipe.load_recipe_model_lock('vibecomfy-h3','linux-x86_64-cu130')
        started = time.monotonic()
        inventory = []
        for model in models.models:
            selected = CACHE / model_downloader._cache_relative(model)
            # Exact model-lock paths only. No scan of other jobs or private inputs.
            stat = selected.lstat() if selected.exists() else None
            inventory.append(dict(name=model.name,cache_path=str(selected),exists=stat is not None,
                                  bytes=stat.st_size if stat else None,expected_bytes=model.bytes,
                                  expected_sha256=model.sha256,symlink=selected.is_symlink()))
        write('model-inventory.json',inventory)
        phase('model-cache-listing-complete')
        timings = dict(pod_script_ready_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
                       model_listing_seconds=round(time.monotonic()-started,3),
                       model_integrity='canonical installer will hash the selected cache/model bytes',
                       generation='not requested')
    else:
        mounted = []
        timings = {'mode':'isolated-installed-canary','generation':'not requested'}
    phase('official-installer-start')
    started = time.monotonic()
    private_log = root/'official-installer.log'
    with private_log.open('w') as stream:
        done = subprocess.run(['sh',str(HERE/'install.sh'),*flags],env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=1800)
    timings['astrid_install_seconds'] = round(time.monotonic()-started,3)
    write('installer-diagnostics.json',install_diagnostics([private_log,root/'state/astrid-runtime/logs/install.log']))
    # Explicit task-owned environment contains no credentials or private inputs.
    shutil.copyfile(private_log, out/'official-installer.log')
    write('app-install-result.json',dict(exit_code=done.returncode,install_calls=1,source_tuple=sources))
    phase('official-installer-finished',exit_code=done.returncode)
    if done.returncode:
        timings.update(engine_install='not reached',comfyui_readiness='not reached',model_integrity='not reached')
        write('timings.json',timings)
        return done.returncode
    from astrid.installer.core import current
    version = current(program)
    assert version is not None and version.python.is_file()
    installed_manifest = json.loads((version.root/'manifest.json').read_text())
    for name in ('agent','runtime','vibecomfy'):
        assert installed_manifest[name]['sha']==sources[name]['sha']
    write('installed-version.json',version.document())
    # Installed artifact only; no source path injection or ambient PYTHONPATH.
    child_env = {**env,**version.env}
    child_env.pop('UV_PYTHON_PREFERENCE',None)
    child_env['ASTRID_ENGINE_SANITY_CMD'] = shlex.join([str(version.python),str(version.root/'a/scripts/gpu_sanity.py')])
    smoke = subprocess.run([str(version.python),'-I','-c',
        'import astrid,astrid_runtime,runtime_protocol; from astrid.core import engine_cli; print(astrid.__version__)'],
        env=child_env,capture_output=True,text=True)
    write('installed-import-smoke.json',dict(exit_code=smoke.returncode,stdout_sha256=hashlib.sha256(smoke.stdout.encode()).hexdigest()))
    assert smoke.returncode==0
    inventory_command = [str(version.python), '-I', '-m', 'astrid', 'action', 'run', '--help']
    inventory = subprocess.run(inventory_command, env=child_env, capture_output=True, text=True)
    (out/'action-inventory.stdout').write_text(inventory.stdout)
    (out/'action-inventory.stderr').write_text(inventory.stderr)
    write('action-inventory-command.json', dict(argv=inventory_command, exit_code=inventory.returncode,
          scope='install-only; the frozen CLI exposes H3 action IDs through action run --help'))
    assert inventory.returncode == 0, 'installed action inventory failed; inspect action-inventory.stderr'
    if args.canary:
        phase('installed-lane-start')
        lane = subprocess.run([sys.executable,'-I',str(args.canary_lane),str(out/'installed-version.json')],env=child_env)
        write('installed-lane-result.json',dict(exit_code=lane.returncode))
        # A failed local lane closes submission; still diagnose canonical bootstrap on CPU.
        child_env['H91_CANARY_STOP_BEFORE_GPU'] = '1'
    # The recipe bootstraps with system python3 -m pip; the installed venv has no pip.
    # The installed CLI/helper/sanity interpreters are selected by absolute path.
    child_env['PATH'] = env['PATH']
    phase('canonical-engine-start')
    started = time.monotonic()
    device, mount_id = (str(os.stat('/workspace').st_dev),mounted[0][0]) if args.pod else ('local','local')
    with (root/'engine-capture-private.log').open('w') as stream:
        done = subprocess.run([str(version.python),'-I',str(HERE/'capture.py'),str(engine),str(CACHE),device,mount_id,str(out)],
                              env=child_env,stdout=stream,stderr=subprocess.STDOUT,timeout=3120)
    timings['engine_install_seconds']=round(time.monotonic()-started,3)
    timings['comfyui_readiness']='canonical owned HTTP /system_stats and /object_info polling inside health step; see engine-events.jsonl'
    write('timings.json',timings)
    phase('done',exit_code=done.returncode)
    return done.returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:
        # Site/type are sufficient to locate a harness failure without raw values.
        sites=[]
        trace=exc.__traceback__
        while trace:
            sites.append(dict(file=Path(trace.tb_frame.f_code.co_filename).name,line=trace.tb_lineno,function=trace.tb_frame.f_code.co_name))
            trace=trace.tb_next
        print(json.dumps(dict(schema='h91.driver-failure.v1',type=type(exc).__name__,sites=sites,
              error=str(exc),argv=getattr(exc,'cmd',None),
              stderr=str(getattr(exc,'stderr',''))[-65536:])),flush=True)
        raise SystemExit(1)
