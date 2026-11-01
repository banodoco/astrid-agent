#!/usr/bin/env python3
"""T-11 installed-pack harness. Never emits a qualification pass without live observation."""
import functools
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import urllib.request


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    root = Path(os.environ['T11_ROOT']).resolve()
    receipt_path = Path(os.environ['T11_RECEIPT']).resolve()
    receipt = {
        'status': 'not-qualified',
        'qualification': False,
        'phase': 'input',
        'observations': {},
        'gap': None,
    }
    obs = receipt['observations']
    try:
        shas = {name: os.environ[name.upper() + '_SHA']
                for name in ('agent', 'runtime', 'app', 'pinokio')}
        if not all(re.fullmatch(r'[0-9a-f]{40}', sha) for sha in shas.values()):
            raise ValueError('all four tuple SHAs must be full lowercase 40-character commit IDs')
        obs['requested_tuple'] = shas
        if root.exists():
            raise ValueError('T11_ROOT must not exist')
        root.mkdir(parents=True)
        home, data, program, sources, work = [root / s for s in ('home', 'data', 'program', 'sources', 'work')]
        for directory in (home, data, program, sources, work):
            directory.mkdir()
        obs['data_root_initially_empty'] = not any(data.iterdir())
        env = {'HOME': str(home), 'PATH': os.defpath, 'LANG': 'C.UTF-8',
               'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_DATA_HOME': str(home / '.local/share'),
               'TMPDIR': str(root)}
        urls = {'agent': 'https://github.com/banodoco/astrid-agent.git',
                'runtime': 'https://github.com/banodoco/astrid-runtime.git',
                'app': 'https://github.com/banodoco/astrid-app.git'}
        receipt['phase'] = 'published-sources'
        for name, url in urls.items():
            dest = sources / name
            subprocess.run(['git', 'init', '-q', str(dest)], env=env, check=True)
            subprocess.run(['git', '-C', str(dest), 'fetch', '-q', '--depth=1', url, shas[name]], env=env, check=True)
            subprocess.run(['git', '-C', str(dest), 'checkout', '-q', '--detach', 'FETCH_HEAD'], env=env, check=True)
            resolved = subprocess.check_output(['git', '-C', str(dest), 'rev-parse', 'HEAD^{commit}'], env=env, text=True).strip()
            if resolved != shas[name]:
                raise ValueError(name + ' source SHA mismatch')
        agent, runtime, app = (sources / n for n in ('agent', 'runtime', 'app'))
        schema_name = 'astrid/core/timeline/schema/timeline-config.schema.json'
        agent_schema = (agent / schema_name).read_bytes()
        runtime_schema = (runtime / 'contract/schemas/timeline-config.schema.json').read_bytes()
        lock = json.loads((agent / 'runtime.lock.json').read_text())
        pin = lock['files'][schema_name]
        if pin['from'] != 'contract/schemas/timeline-config.schema.json' or pin['sha256'] != 'sha256:' + digest(agent_schema) or agent_schema != runtime_schema:
            raise ValueError('tuple Timeline schema or lock pin mismatch')
        obs['verified_tuple_timeline_schema_sha256'] = digest(agent_schema)
        def archive(base):
            dependency = json.loads((base / 'package.json').read_text())['dependencies']['@banodoco/timeline-composition']
            if not dependency.startswith('file:') or not dependency.endswith('.tgz'):
                raise ValueError('Composition dependency is not a recorded tarball')
            path = (base / dependency[5:]).resolve(strict=True)
            if not path.is_relative_to(base.resolve()):
                raise ValueError('Composition archive escapes tuple source')
            return path.read_bytes()
        agent_archive, app_archive = archive(agent / 'remotion'), archive(app)
        if agent_archive != app_archive:
            raise ValueError('Agent and App recorded Composition archives differ')
        obs['recorded_composition_archive_sha256'] = digest(agent_archive)
        obs['schema_consumption'] = 'unobserved'
        obs['composition_execution'] = 'unobserved'
        manifest_bytes = (app / 'public/install-manifest.json').read_bytes()
        bootstrap = (app / 'public/install').read_bytes()
        with urllib.request.urlopen('https://astrid.haus/install', timeout=60) as response:
            hosted_bootstrap = response.read()
        if bootstrap != hosted_bootstrap:
            raise ValueError('hosted installer bootstrap differs from selected App commit')
        obs['app_manifest_sha256'] = digest(manifest_bytes)
        obs['bootstrap_sha256'] = digest(bootstrap)
        manifest = json.loads(manifest_bytes)
        for name in ('agent', 'runtime'):
            manifest[name] = {**manifest[name], 'sha': shas[name], 'repo': str(sources / name)}
        manifest_path = root / 'local-install-manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        obs['installer_manifest_sha256'] = digest(manifest_path.read_bytes())
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            receipt['phase'] = 'official-installer'
            env['ASTRID_INSTALL_MANIFEST_URL'] = 'http://127.0.0.1:%d/local-install-manifest.json' % server.server_port
            subprocess.run(['sh', '-s', '--', '--root', str(program), '--data-root', str(data)], input=bootstrap, cwd=work, env=env, check=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        receipt['phase'] = 'installed-profile'
        profile_path = data / 'runtime/source-profiles/astrid.json'
        profile_bytes = profile_path.read_bytes()
        profile = json.loads(profile_bytes)
        declaration_path = Path(profile['service_declaration'])
        declaration = json.loads(declaration_path.read_text())
        hosts = [s for s in declaration['services'] if s['name'] == 'pack-host']
        if len(hosts) != 1:
            raise ValueError('expected one pack-host declaration')
        host = hosts[0]
        expected = {'ASTRID_RUNTIME_DATA_ROOT': str(data), 'ASTRID_SUPPORT_ROOT': str(data),
                    'ASTRID_WORKSPACE_DISCOVERY': str(data / 'runtime/discovery.json')}
        if any(host['env'].get(k) != v for k, v in expected.items()):
            raise ValueError('pack-host declaration does not bind the clean data root')
        obs['source_profile_sha256'] = digest(profile_bytes)
        obs['service_declaration_sha256'] = digest(declaration_path.read_bytes())
        obs['profile_data_root_declaration_matches'] = True
        receipt['phase'] = 'installed-example-render'
        example_receipt = root / 't11-example-render-receipt.json'
        example_output = work / 't11-example-render.mp4'
        interpreter = Path(profile['runtime_environment']) / 'bin' / 'python'
        runner = Path(__file__).with_name('t11_example_render.py')
        completed = subprocess.run(
            [str(interpreter), '-I', str(runner),
             '--profile', str(profile_path), '--data-root', str(data),
             '--work-dir', str(work), '--output', str(example_output),
             '--receipt', str(example_receipt), '--agent-source', str(agent),
             '--runtime-source', str(runtime), '--app-source', str(app),
             '--agent-sha', shas['agent'], '--runtime-sha', shas['runtime'],
             '--app-sha', shas['app'], '--pinokio-sha', shas['pinokio']],
            cwd=work, env=env, check=False,
        )
        if example_receipt.is_file():
            obs['example_render'] = json.loads(example_receipt.read_text())
        obs['example_render_exit_code'] = completed.returncode

        # The helper may have completed a real render, but that does not expose
        # the object returned by the actual schema consumer or the module graph
        # executed by the bundler/browser.  The isolated observer records the
        # exact selected-source seam and must return its deliberate refusal.
        receipt['phase'] = 'same-process-observation'
        observer_receipt = root / 't11-observer-receipt.json'
        observer = Path(__file__).with_name('t11_observer.py')
        observed = subprocess.run(
            [sys.executable, '-I', str(observer),
             '--agent-source', str(agent), '--data-root', str(data),
             '--profile', str(profile_path), '--example-receipt', str(example_receipt),
             '--receipt', str(observer_receipt),
             '--agent-sha', shas['agent'], '--runtime-sha', shas['runtime'],
             '--app-sha', shas['app'], '--pinokio-sha', shas['pinokio'],
             '--schema-sha256', obs['verified_tuple_timeline_schema_sha256'],
             '--composition-archive-sha256', obs['recorded_composition_archive_sha256']],
            cwd=work, env=env, check=False,
        )
        if not observer_receipt.is_file():
            raise RuntimeError('same-process observer did not retain a receipt')
        obs['same_process_observer'] = json.loads(observer_receipt.read_text())
        obs['same_process_observer_exit_code'] = observed.returncode
        if observed.returncode != 2:
            raise RuntimeError('same-process observer did not fail closed with its expected refusal')
        receipt['phase'] = 'live-observation-unavailable'
        receipt['gap'] = ('Durable example/render evidence is partial operational evidence only. '
                          'The source-hashed observer refusal retains the missing in-process seams; '
                          'schema consumption and Composition execution remain unobserved.')
        return 2
    except Exception as exc:
        # Do not upload command output, environment, paths or exception text that may contain private state.
        receipt['gap'] = 'Prototype stopped in phase %s (%s); consult local job logs.' % (receipt['phase'], type(exc).__name__)
        return 1
    finally:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    sys.exit(main())
