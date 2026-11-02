"""Four installed composition contracts retained from reviewed task 54."""
import base64
import csv
import hashlib
import importlib.metadata
import os
import sys
import traceback

from .common import exact_path, read, require, save
from .tool import fingerprint

CONTRACT_NAMES = (
    'worker_profile_emission', 'strict_owner_projection',
    'streamed_locked_inventory_hashing', 'linux_role_bound_custody_broker',
)


def run(ctx):
    results = {}
    stage = CONTRACT_NAMES[0]
    try:
        from astrid.installer.worker_profile import write_worker_profile
        folder = exact_path(str(ctx.work / 'contracts/profile'))
        folder.mkdir(parents=True, mode=0o700)
        emitted = write_worker_profile(folder, ctx.purelib, ctx.work / 'contracts/data')
        worker = read(emitted)
        require(worker['execution_mode'] == 'host_only_cpu' and worker['source_checkout'] == str(ctx.purelib),
                stage, emitted, 'emitted worker identity mismatch')
        records = {name: hashlib.sha256(importlib.metadata.distribution(name).read_text('RECORD').encode()).hexdigest()
                   for name in ('astrid', 'astrid-runtime')}
        results[stage] = {'result': 'PASS', 'emitted_profile': str(emitted), 'RECORD_sha256': records,
                          'profile_digest': worker['profile_digest']}
        save(ctx.evidence / 'contracts.json', results)
        stage = CONTRACT_NAMES[1]
        from astrid_runtime.custody_broker import default_process_identity
        from runtime_protocol.local_execution_supervisor import runtime_owner_record, RUNTIME_OWNER_FIELDS
        observed = default_process_identity(os.getpid())
        require(observed is not None, stage, os.getpid(), 'native identity unavailable')
        projected = runtime_owner_record(observed, ctx.task.name)
        require(set(projected) == RUNTIME_OWNER_FIELDS == {'pid', 'uid', 'birth_id', 'runtime_instance_id', 'coordinator_epoch'}
                and all(projected[k] == observed[k] for k in ('pid', 'uid', 'birth_id')),
                stage, os.getpid(), 'owner projection fields or identity differ')
        results[stage] = {'result': 'PASS', 'native_observation_keys': sorted(observed),
                          'projected_fields': sorted(projected)}
        save(ctx.evidence / 'contracts.json', results)
        stage = CONTRACT_NAMES[2]
        from runtime_protocol import verified_host_launch as gate
        member = ctx.purelib / 'cv2/cv2.abi3.so'
        dist = importlib.metadata.distribution('opencv-python-headless')
        row = next(row for row in csv.reader(dist.read_text('RECORD').splitlines()) if row[0] == 'cv2/cv2.abi3.so')
        require(row[1].startswith('sha256='), stage, member, 'RECORD sha256 absent')
        expected = base64.urlsafe_b64decode(row[1].split('=', 1)[1] + '===').hex()
        gate._stream_member_digest(member, expected)
        require(member.stat().st_size > gate.LIMIT, stage, member, 'fixture is not above import-source bound')
        try:
            gate.read_source(member, expected)
        except gate.HostLaunchError as exc:
            refusal = str(exc)
        else:
            raise RuntimeError(f'stage={stage} path={member}: bounded source accepted oversized member')
        results[stage] = {'result': 'PASS', 'path': str(member), 'size': member.stat().st_size,
                          'expected_sha256': expected, 'read_source_refusal': refusal,
                          'import_limit': gate.LIMIT, 'inventory_limit': gate.INVENTORY_MEMBER_LIMIT}
        save(ctx.evidence / 'contracts.json', results)
        stage = CONTRACT_NAMES[3]
        from astrid_runtime.custody_broker import RoleBoundCustodyBroker
        require(sys.platform == 'linux', stage, sys.platform, 'this contract requires Linux')
        ledger = exact_path(str(ctx.work / 'contracts/custody/ledger'))
        ledger.parent.mkdir(parents=True, mode=0o700)
        broker = RoleBoundCustodyBroker(role='host', identity_provider=default_process_identity, ledger_root=ledger)
        try:
            results[stage] = {'result': 'PASS', 'ledger_root': str(ledger), 'socket_root': str(broker.socket_root),
                              'state': broker.state, 'role': broker.role, 'journal_path': str(broker.journal_path),
                              'ledger_path': str(broker.ledger_path), 'signals': 'none; constructor only'}
        finally:
            broker.abort_before_spawn()
        results['fingerprint'] = fingerprint(ctx)
        save(ctx.evidence / 'contracts.json', results)
    except BaseException as exc:
        results['failure'] = {'stage': stage, 'error_type': type(exc).__name__, 'error': str(exc),
                              'traceback': traceback.format_exc()}
        save(ctx.evidence / 'contracts.json', results)
        raise
