"""One installed parent → H3 prepare journey; invalid CPU input must fail terminally."""
import dataclasses
import hashlib
from pathlib import Path
import shutil
import sys
import time

from .common import exact_path, inventory, pages, read, require, save, sha
from .contract import ASSETS
from .evidence import assert_ready, final_snapshot, restart_snapshot


def restart_and_validate(ctx, worker_path, *, start, verify, stop, list_capabilities):
    """Service process edges are supplied at construction; captures remain real."""
    try:
        try:
            start()
        finally:
            snapshot = restart_snapshot(ctx, worker_path, list_capabilities)
        # Snapshot manifest and every available artifact exist before verify/assert.
        require(not snapshot['errors'] and snapshot['service_logs'],
                'restart-capture', ctx.evidence, str(snapshot))
        verify()
        assert_ready(ctx, snapshot)
    except BaseException:
        try:
            stop()
        except BaseException as cleanup:
            save(ctx.evidence / 'cleanup-error.json', {'stage': 'restart-stop', 'error': repr(cleanup)})
        raise


def run(ctx):
    from astrid.installer.backend import Backend
    from astrid.installer.core import current, write_json
    from runtime_protocol.local_worker import _digest
    from runtime_protocol.local_worker_composition import _pack_inventory
    from astrid_runtime_client import WorkspaceClient

    version = current(ctx.install / 'program')
    profile = read(version.profile)
    worker_path = exact_path(profile['worker_profile'], kind='file')
    require(worker_path.is_relative_to(ctx.install), 'profile', worker_path, 'test-owned profile required')
    worker = read(worker_path)
    ready = read(ctx.install / 'data/runtime/worker-astrid-ready.json')
    # Preserve ordinary evidence before inspecting it.
    for name, value in [('profile.json', profile), ('worker-profile.json', worker), ('ordinary-ready.json', ready)]:
        save(ctx.evidence / name, value)
    require(worker['execution_mode'] == 'host_only_cpu' and worker['max_concurrency'] >= 2
            and worker['source_checkout'] == profile['source_checkout'] == str(ctx.purelib),
            'ordinary-readiness', worker_path, 'installed CPU profile identity/concurrency mismatch')
    declaration = read(exact_path(profile['service_declaration'], kind='file'))
    save(ctx.evidence / 'services.json', declaration)
    host = next(row for row in declaration['services'] if row['name'] == 'pack-host')
    require(host['argv'][-1] == '--verified-local-worker'
            and host['ready']['file'] == 'worker-astrid-ready.json',
            'ordinary-readiness', version.profile, 'verified local worker declaration required')
    require(ready['source_checkout'] == str(ctx.purelib) and ready['python_executable'] == str(ctx.python)
            and 'h3_av.prepare' in ready['ready_capabilities'],
            'ordinary-readiness', ctx.install, 'ordinary installed ready identity/capability mismatch')
    discovery = read(ctx.install / 'data/runtime/discovery.json')
    realm = discovery.get('active_realm')
    require(isinstance(realm, str) and realm.strip(), 'realm', ctx.install, 'active_realm missing')
    backend = Backend(ctx.install / 'program', ctx.install / 'canary.log', ctx.install / 'bin/uv')
    backend.stop(version, ctx.install / 'data')
    stopped_on_failure = False
    try:
        pack = ctx.install / 'canary-pack'
        require(not pack.exists(), 'pack-inventory', pack, 'fresh test-owned pack required')
        shutil.copytree(ASSETS, pack, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        expected_inventory = ctx.payload['parent_inventory']
        require(inventory(pack) == _pack_inventory(pack) == expected_inventory,
                'pack-inventory', pack, 'exact reviewed membership/hash mismatch')
        original_matrix = exact_path(worker['capability_matrix'], kind='file')
        require(sha(original_matrix) == ctx.payload['matrix']['shipped_sha256'],
                'matrix', original_matrix, 'installed shipped matrix differs')
        (ctx.evidence / 'original-installed-capabilities.json').write_bytes(original_matrix.read_bytes())
        # Boot/source pins and all remaining installed fields are preserved.
        worker['additional_pack_roots'] = [{'path': str(pack), 'files': expected_inventory}]
        worker['capability_matrix'] = ctx.payload['matrix']['selected_path']
        launch = {name: worker[name] for name in ('source_checkout', 'pack_root', 'boot_manifest_path',
                 'boot_manifest_hash', 'capability_matrix', 'environment', 'max_concurrency', 'additional_pack_roots')}
        worker['session_config_digest'] = _digest(launch)
        worker['profile_digest'] = _digest({k:v for k,v in worker.items() if k != 'profile_digest'})
        write_json(worker_path, worker)
        credential_path = exact_path(discovery['credential_file'], kind='file')
        require(credential_path.is_relative_to(ctx.install / 'data/credentials'),
                'credentials', credential_path, 'test-owned credential required')
        owner = WorkspaceClient(discovery['endpoint'], credential_path.read_text().strip())
        def stop_on_failure():
            nonlocal stopped_on_failure
            stopped_on_failure = True
            backend.stop(version, ctx.install / 'data')
        restart_and_validate(ctx, worker_path,
            start=lambda: backend.start(version, ctx.install / 'data'),
            verify=lambda: backend.verify(version, ctx.install / 'data', realm),
            stop=stop_on_failure, list_capabilities=owner.list_capabilities)
        h3(ctx, owner)
    finally:
        # Only this selected test installation is stopped, including preparation failures.
        primary = sys.exc_info()[1]
        try:
            if not stopped_on_failure:
                backend.stop(version, ctx.install / 'data')
        except BaseException as cleanup:
            save(ctx.evidence / 'cleanup-error.json', {'stage': 'journey-stop', 'error': repr(cleanup)})
            if primary is None:
                raise
        finally:
            final_snapshot(ctx)


def validate_child(child, *, parent_id, project_id, caps, binding, objects):
    require(child is not None and child.get('attempt_id'), 'h3-claim', parent_id,
            'real H3 prepare was not admitted and claimed')
    require(child['capability_id'] == 'h3_av.prepare' and child['capability_digest'] == caps['h3_av.prepare']
            and child['project_id'] == project_id, 'h3-claim', parent_id, 'child capability/project mismatch')
    lineage = child['spec']['delegated_parent']
    require(lineage['parent_task_id'] == parent_id and lineage['project_id'] == project_id
            and lineage['stage'] == 'prepare' and lineage['parent_placement'] == binding,
            'h3-lineage', parent_id, 'delegated project/stage/placement mismatch')
    placement = child['execution_binding']
    require(placement['actual_target']['kind'] == 'machine' and placement['actual_target'] == binding['actual']
            and placement['verification']['verified'] is True
            and child['input_object_ids'] == list(objects.values()),
            'h3-placement', parent_id, 'verified placement/input identity mismatch')


def h3(ctx, owner):
    from astrid.core.execution._child_bridge import resource, task_resource
    from astrid.sdk.host_bootstrap import PACK_HOST_ACTOR, PACK_HOST_SCOPES
    from astrid.sdk.execution_request import normalize_execution_request
    from runtime_protocol.auth import CredentialStore
    from runtime_protocol.daemon import WORKER_SCOPES

    support = ctx.install / 'data/runtime'
    metadata = CredentialStore(support / 'credentials').actor_metadata(PACK_HOST_ACTOR)
    ready = read(ctx.evidence / 'canary-ready.json')
    require(tuple(PACK_HOST_SCOPES) == tuple(WORKER_SCOPES) and metadata['scopes'] == sorted(PACK_HOST_SCOPES),
            'h3-scopes', support, 'production host scopes differ')
    binding, receipt = metadata['execution_binding'], metadata['local_launch_receipt']
    require(binding['actual']['kind'] == 'machine' and binding['verification']['verified'] is True
            and binding['verification']['method'] == 'credential_claim'
            and receipt['execution_mode'] == 'host_only_cpu' and binding['actual']['id'] == receipt['machine_id']
            and binding['executor_incarnation'] == receipt['executor_incarnation'] == ready['activation']['executor_incarnation']
            and binding['verification']['evidence_digest'] == receipt['evidence_digest'] == ready['activation']['evidence_digest'],
            'h3-custody', support, 'credential/receipt/ready binding mismatch')
    caps = {row.capability_id: row.definition_digest for row in pages(owner.list_capabilities)}
    # These ids are the fixed reviewed journey, never arbitrary caller acquisition targets.
    require(all(caps.get(n) for n in ('installed_canary.parent', 'h3_av.prepare')),
            'h3-admission', support, 'required discovered capability pins absent')
    key = hashlib.sha256(str(ctx.task).encode()).hexdigest()[:16]
    project = resource(owner.create_project('Installed CPU canary', slug='installed-canary-' + key,
                                            idempotency_key='project-' + key))
    project_id = project.get('project_id') or project['id']
    objects, files = {}, {}
    for name, content in [('request', b'{}'), ('input_bundle', b'CPU canary: deliberately invalid bundle')]:
        value = owner.ingest_project_object(project_id, content, filename=name + '.bin',
                  media_type='application/octet-stream', idempotency_key='object-' + key + '-' + name)
        objects[name] = value.get('object_id') or value['digest']
        files[name] = {'object_id': objects[name], 'digest': objects[name], 'filename': name + '.bin',
                       'media_type': 'application/octet-stream', 'size': len(content)}
    ordered = [{'name': name, 'root_object_id': oid} for name, oid in objects.items()]
    policy = {'capabilities': [{'capability_id': 'h3_av.prepare', 'capability_digest': caps['h3_av.prepare']}],
        'targets': [{'kind': 'default'}], 'input_object_ids': list(objects.values()),
        'stages': [{'name': 'prepare', 'capability_id': 'h3_av.prepare', 'capability_digest': caps['h3_av.prepare'],
                    'target': {'kind': 'default'}, 'inputs': ordered}]}
    admitted = owner.admit_task(capability_id='installed_canary.parent', capability_digest=caps['installed_canary.parent'],
        project_id=project_id, input_object_ids=list(objects.values()),
        spec={'inputs': {'request_oid': objects['request'], 'bundle_oid': objects['input_bundle'], **files}},
        execution_request=normalize_execution_request({'target': {'kind': 'default'},
            'inputs': [{'name': name, **value, 'required': True} for name, value in files.items()]}),
        child_delegation=policy, idempotency_key='parent-' + key)
    parent_id = admitted.task_id
    save(ctx.evidence / 'h3-parent-admission-receipt.json', dataclasses.asdict(admitted))
    deadline = time.monotonic() + 45
    child, events = None, []
    while time.monotonic() < deadline:
        events = pages(owner.list_events)
        for event in events:
            if event.event_type == 'task.admitted' and event.aggregate_id != parent_id:
                candidate = task_resource(owner.get_task(event.aggregate_id))
                if candidate.get('spec', {}).get('delegated_parent', {}).get('parent_task_id') == parent_id:
                    child = candidate
        save(ctx.evidence / 'h3-observed-child-receipt.json', {'parent_id': parent_id, 'child': child,
                                                              'events': [dataclasses.asdict(e) for e in events]})
        if child is not None and child.get('attempt_id'):
            break
        time.sleep(.1)
    validate_child(child, parent_id=parent_id, project_id=project_id, caps=caps, binding=binding, objects=objects)
    save(ctx.evidence / 'h3-child-evidence.json', {'parent_id': parent_id, 'child': child, 'project_id': project_id})
    deadline = time.monotonic() + 45
    terminal = {}
    while time.monotonic() < deadline:
        parent = task_resource(owner.get_task(parent_id))
        final_child = task_resource(owner.get_task(child['task_id']))
        terminal = {'parent': parent, 'child': final_child, 'expected': 'both failed: deliberately invalid CPU input'}
        save(ctx.evidence / 'h3-terminal-receipts.json', terminal)
        if all(item['state'] in {'failed', 'completed', 'cancelled'} for item in (parent, final_child)):
            break
        time.sleep(.1)
    all_events = [dataclasses.asdict(e) for e in pages(owner.list_events)]
    save(ctx.evidence / 'h3-complete-events.json', all_events)
    require(terminal and all(item['state'] == 'failed' and item.get('attempt_id') and item.get('result', {}).get('error')
                            for item in (terminal['parent'], terminal['child'])),
            'h3-terminal', ctx.evidence, 'both tasks must fail with attempts and explicit invalid-input errors')
    for item in (terminal['parent'], terminal['child']):
        kinds = {e['event_type'] for e in all_events if e['aggregate_id'] == item['task_id']}
        require({'task.admitted', 'task.claimed', 'task.failed'} <= kinds,
                'h3-terminal', item['task_id'], 'admission/claim/failure event sequence absent')
    save(ctx.evidence / 'h3-terminal-pass.json', {'result': 'PASS', 'parent_id': parent_id,
                                               'child_id': child['task_id'], 'parent_state': 'failed', 'child_state': 'failed'})
