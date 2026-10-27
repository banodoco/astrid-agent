"""Explicit forward recovery of a retained, same-schema update candidate.

The checkpoint is immutable and anchored in the installer transaction. Mutable
progress never authorizes restoring a backup or launching the previous version.
Runtime remains the authority for upgrade journals and task interruption fences.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
from pathlib import Path

from .app_update import inspect_schema, locked
from .core import Version, absolute, current, linked, read_json, switch, write_json
from .service import LABEL
from .tools import validate_manifest

FORMAT = 'astrid-forward-recovery/v1'
MARKERS = {'generic-host.json', 'generic-host.ready.json'}
SUPPORT_IGNORED = MARKERS | {'realms', 'bootstrap.lock', 'generic-host.lock', 'generic-host.busy.json',
                             'discovery.json', 'instance.lock', 'logs'}


def digest(path: Path) -> str | None:
    absolute(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f'Recovery expected a regular file: {path}')
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def tree_digest(path: Path, *, ignored=frozenset()) -> dict:
    """Hash exact regular contents, rejecting linked directories and special files."""
    absolute(path)
    if not path.is_dir():
        raise ValueError(f'Recovery evidence directory is missing: {path}')
    result = {}
    for entry in sorted(path.iterdir()):
        if entry.name in ignored or entry.name == '__pycache__' or entry.suffix in {'.pyc', '.pyo'}:
            continue
        if linked(entry):
            raise ValueError(f'Recovery evidence contains a link: {entry}')
        result[entry.name] = tree_digest(entry) if entry.is_dir() else digest(entry)
    return result


def realm_digest(path: Path) -> dict:
    """Bind logical SQLite contents, independent of WAL checkpoint/sidecar churn."""
    import sqlite3

    result = tree_digest(path, ignored={'realm.sqlite3', 'realm.sqlite3-wal', 'realm.sqlite3-shm', 'realm.sqlite3-journal'})
    database = absolute(path / 'realm.sqlite3')
    if not database.is_file():
        raise ValueError('The recovery realm database is missing.')
    value = hashlib.sha256()
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as connection:
        connection.execute('BEGIN')
        for statement in connection.iterdump():
            value.update(statement.encode('utf-8'))
            value.update(b'\0')
    result['realm.sqlite3:logical'] = value.hexdigest()
    return result


def _transaction_identity(transaction: dict) -> dict:
    return {key: transaction.get(key) for key in ('format', 'operation_id', 'data_root', 'old', 'candidate', 'realm', 'backup')}


class ForwardRecovery:
    def __init__(self, installer):
        self.installer = installer
        self.root, self.data, self.backend = installer.root, installer.data, installer.backend
        self.path = absolute(self.root / 'update-transaction.json')
        self.transaction = None

    def _load(self):
        tx = read_json(self.path)
        if (tx.get('format') != 'astrid-install-update/v1'
                or not re.fullmatch('[0-9a-f]{32}', str(tx.get('operation_id', '')))
                or absolute(tx.get('data_root', '')) != self.data
                or not tx.get('candidate') or not tx.get('old') or not tx.get('backup')):
            raise ValueError('Forward recovery requires an interrupted update with a retained candidate and backup.')
        if tx.get('step') in {'complete', 'rolled_back'} and not tx.get('recovery'):
            raise ValueError('This update does not need forward recovery.')
        marker = read_json(self.root / 'install.json')
        record = self.backend.service.recorded()
        if (marker.get('owner') != LABEL or marker.get('data_root') != str(self.data)
                or marker.get('managed_by') or record.get('owner') != LABEL
                or record.get('managed_by') or record.get('uninstalled')
                or record.get('program_root') != str(self.root) or record.get('data_root') != str(self.data)):
            raise ValueError('Forward recovery requires the selected Astrid login service and workspace ownership.')
        old, candidate = Version.load(tx['old']), Version.load(tx['candidate'])
        for version in (old, candidate):
            if (version.root.parent != self.root / 'versions' or not version.python.is_file()
                    or not version.python.is_relative_to(version.root)
                    or not version.profile.is_relative_to(version.root)
                    or read_json(version.root / 'version.json') != version.document()):
                raise ValueError('A retained version does not match its managed installation record.')
        if current(self.root) not in (old, candidate):
            raise ValueError('The selected version changed; refusing forward recovery.')
        backup = absolute(tx['backup']['root'])
        if backup.parent != self.root / 'backups' or not (backup / 'backup.json').is_file():
            raise ValueError('Forward recovery requires its retained owned backup.')
        realm = tx['realm']
        absolute(realm['realm_root'])
        if (record.get('realm_id') not in (None, realm['realm_id'])
                or record.get('realm_root') not in (None, realm['realm_root'])):
            raise ValueError('The selected service workspace changed.')
        catalog = read_json(self.data / 'runtime/catalog.json')
        rows = [row for row in catalog.get('realms', []) if row.get('realm_id') == realm['realm_id']]
        if (catalog.get('selected_realm_id') != realm['realm_id'] or len(rows) != 1
                or absolute(rows[0]['data_root']) != absolute(realm['realm_root'])):
            raise ValueError('The live workspace catalog differs from the update transaction.')
        self.transaction = tx
        return tx, old, candidate

    def _versions(self, old, candidate):
        result = {}
        for version in (old, candidate):
            files = [version.root / 'version.json', version.root / 'manifest.json', version.profile, version.python]
            profile = read_json(version.profile)
            for key in ('service_declaration',):
                if profile.get(key):
                    path = absolute(profile[key])
                    if not path.is_relative_to(version.root):
                        raise ValueError('A retained profile points outside its version.')
                    files.append(path)
            if version == candidate:
                validate_manifest(read_json(version.root / 'manifest.json'))
                closure = self.backend.validate_recovery_candidate(candidate)
            else:
                closure = {}
            hashes = {str(path): digest(path) for path in files}
            for key in ('runtime_checkout',):
                path = absolute(profile.get(key, ''))
                if not path.is_relative_to(version.root):
                    raise ValueError('A retained Runtime checkout points outside its version.')
                hashes[str(path)] = tree_digest(path)
            for key in ('runtime_root', 'protocol_root'):
                if closure.get(key):
                    path = absolute(closure[key])
                    hashes[str(path)] = tree_digest(path)
            result[version.root.name] = hashes
        return result

    def _schema(self, old, candidate, realm):
        prior = inspect_schema(self.backend, old, realm)
        if inspect_schema(self.backend, candidate, realm) != prior:
            raise ValueError('Forward recovery requires identical current workspace schema identities.')
        return prior

    def _journal(self, realm):
        journal = read_json(self.data / 'runtime/upgrade-journal.json')
        if journal and (journal.get('state') != 'complete' or journal.get('realm_id') != realm['realm_id']
                        or journal.get('operation') != 'astrid-runtime-upgrade/v1' or journal.get('version') != 1
                        or not re.fullmatch('[0-9a-f]{32}', str(journal.get('operation_id', '')))
                        or journal.get('realm_root', realm['realm_root']) != realm['realm_root']
                        or journal.get('source_support_root', str(self.data)) != str(self.data)):
            raise ValueError('Runtime upgrade journal is incomplete or ambiguous. Preserve it and use Runtime-supported '
                             'upgrade reconciliation before resuming; legacy Runtime releases may require a compatible '
                             'updated candidate. No journal was deleted or marked complete.')
        return digest(self.data / 'runtime/upgrade-journal.json')

    @contextlib.contextmanager
    def _quiescent(self, realm):
        # Bootstrap serializes competing Runtime startup. The SQLite fence blocks
        # task admission during final capture/retirement; never copy PID lockfiles.
        from runtime_protocol.lifecycle import interruption_fence
        with locked(self.data / 'runtime/bootstrap.lock'):
            with interruption_fence(realm['realm_root']):
                yield

    def _evidence(self, tx, old, candidate):
        return {
            'transaction': _transaction_identity(tx),
            'versions': self._versions(old, candidate),
            'install_marker': digest(self.root / 'install.json'),
            'service_env': digest(self.root / 'service-env.json'),
            'selection': digest(self.backend.service.selection),
            'current': digest(self.root / 'current'),
            'service': self.backend.service.recovery_state((old, candidate), self.data),
            'backup': tree_digest(absolute(tx['backup']['root'])),
            'support': tree_digest(self.data / 'runtime', ignored=SUPPORT_IGNORED),
            'realm': realm_digest(absolute(tx['realm']['realm_root'])),
            'schema': self._schema(old, candidate, tx['realm']),
        }

    def _save(self, tx, phase, **details):
        recovery = {**tx.get('recovery', {}), 'phase': phase, **details}
        self.installer._save_update(tx, 'recovery_required', recovery=recovery, service_healthy=False)

    def record_failure(self, failure) -> str:
        """Persist a diagnostic only after this instance validated its ownership."""
        from ..core.execution.boot_identity import BootIdentityUnavailable

        message = str(failure)
        if isinstance(failure, BootIdentityUnavailable):
            cause: BaseException = failure
            while cause.__cause__ is not None:
                cause = cause.__cause__
            message = ('Trusted operating-system boot proof is unavailable: ' + str(cause) +
                       '. Recovery remains stopped. Check the supported Windows x64 build and native boot-counter access.')
        tx = self.transaction
        if tx is not None:
            if tx.get('recovery'):
                if tx['recovery'].get('error') != message:
                    self._save(tx, tx['recovery']['phase'], error=message)
            elif tx.get('recovery_error') != message:
                self.installer._save_update(tx, 'recovery_required', recovery_error=message, service_healthy=False)
        return message

    def status(self):
        tx = read_json(self.path)
        recovery = tx.get('recovery', {})
        self.installer.say(json.dumps({
            'operation_id': tx.get('operation_id'), 'step': tx.get('step'),
            'phase': recovery.get('phase'), 'error': recovery.get('error') or tx.get('recovery_error'),
            'data_restored': tx.get('data_restored', False), 'data_validated': tx.get('data_validated', False),
            'service_healthy': tx.get('service_healthy', False),
            'candidate': (tx.get('candidate') or {}).get('root'),
            'backup_retained': (tx.get('backup') or {}).get('root'),
            'checkpoint': recovery.get('checkpoint'),
        }, indent=2, sort_keys=True))

    def prepare(self):
        from runtime_protocol import platform

        from ..core.execution.boot_identity import capture_boot_identity
        from ..sdk.host_bootstrap import capture_legacy_host_recovery

        tx, old, candidate = self._load()
        prior = tx.get('recovery', {})
        if prior and prior.get('phase') != 'preparing':
            raise ValueError('A recovery checkpoint already exists. Keep it and run --recover resume or --recover status.')
        # Validate source artifacts/schema before stopping anything. This invokes
        # read-only candidate code, never either release's host startup.
        self._versions(old, candidate)
        self._schema(old, candidate, tx['realm'])
        self._journal(tx['realm'])
        self.backend.service.recovery_state((old, candidate), self.data)
        boot = capture_boot_identity()
        if prior and prior.get('boot') != boot:
            raise ValueError('Preparation was interrupted across Restart before its checkpoint committed; preserve the evidence for support.')
        self._save(tx, 'preparing', boot=boot)
        self.backend.stop_for_recovery(current(self.root), self.data, tx['realm'])
        folder = absolute(self.root / 'recovery' / tx['operation_id'])
        platform.private_directory(folder)
        checkpoint = folder / 'checkpoint.json'
        with self._quiescent(tx['realm']):
            state = self._evidence(tx, old, candidate)
            self.backend.assert_recovery_stopped(self.data)
            if not state['service']['disabled'] or state['service']['running']:
                raise ValueError('The owned Astrid service must remain disabled across Restart.')
            proof = capture_legacy_host_recovery(self.data / 'runtime')
            if capture_boot_identity() != boot or (proof is not None and proof['boot_identity'] != boot):
                raise ValueError('Boot identity changed while preparing the recovery checkpoint.')
            value = {'format': FORMAT, 'root': str(self.root), 'data_root': str(self.data),
                     'boot': boot, 'evidence': state, 'host': proof}
            if checkpoint.exists():
                if read_json(checkpoint) != value:
                    raise ValueError('An interrupted preparation checkpoint differs from live state; preserve it for diagnosis.')
            else:
                write_json(checkpoint, value)
                platform.owner_only(checkpoint)
            tx['data_validated'] = True
            self._save(tx, 'prepared', checkpoint=str(checkpoint), checkpoint_sha256=digest(checkpoint))
        self.installer.say('Recovery prepared. Astrid is stopped and disabled; your live workspace and backup are retained. '
                           'Save other work, use Windows Restart, then run astrid install --recover resume.')

    def _checkpoint(self, tx):
        checkpoint = absolute(tx.get('recovery', {}).get('checkpoint', ''))
        expected = self.root / 'recovery' / tx['operation_id'] / 'checkpoint.json'
        from runtime_protocol import platform
        if not platform.is_owner_only(checkpoint) or not platform.is_owner_only(checkpoint.parent, directory=True):
            raise ValueError('Recovery checkpoint must remain private to its owner.')
        if checkpoint != expected or digest(checkpoint) != tx['recovery'].get('checkpoint_sha256'):
            raise ValueError('The private recovery checkpoint changed or does not match this transaction.')
        value = read_json(checkpoint)
        if (value.get('format') != FORMAT or value.get('root') != str(self.root)
                or value.get('data_root') != str(self.data)
                or value['evidence']['transaction'] != _transaction_identity(tx)):
            raise ValueError('Recovery checkpoint ownership or operation identity changed.')
        return checkpoint, value

    def _assert_phase_ownership(self, tx, old, candidate, evidence, phase):
        if digest(self.root / 'install.json') != evidence['install_marker']:
            raise ValueError('The installation ownership marker changed after preparation.')
        if digest(self.root / 'service-env.json') != evidence['service_env']:
            raise ValueError('Astrid service settings changed after recovery preparation.')
        selected = current(self.root)
        if phase in {'prepared', 'retiring', 'reconciled', 'upgrading', 'upgraded'}:
            if digest(self.root / 'current') != evidence['current']:
                raise ValueError('The selected version changed before candidate selection.')
        elif phase in {'selected', 'registered', 'started', 'verified', 'complete'} and selected != candidate:
            raise ValueError('The recovery candidate is no longer selected.')
        allowed = (candidate,) if phase in {'registered', 'started', 'verified', 'complete'} else (old, candidate)
        state = self.backend.service.recovery_state(allowed, self.data)
        if phase in {'prepared', 'retiring', 'reconciled'}:
            self.backend.assert_recovery_stopped(self.data)
        if phase in {'prepared', 'retiring', 'reconciled', 'upgrading', 'upgraded', 'stopped'}:
            if state != evidence['service'] or digest(self.backend.service.selection) != evidence['selection']:
                raise ValueError('The disabled service or its selection changed before candidate registration.')
        if phase == 'reconciled':
            if (tree_digest(self.data / 'runtime', ignored=SUPPORT_IGNORED) != evidence['support']
                    or realm_digest(absolute(tx['realm']['realm_root'])) != evidence['realm']):
                raise ValueError('The live workspace changed before candidate reconciliation.')

    def resume(self):
        from ..core.execution.boot_identity import (
            boot_identities_differ,
            capture_boot_identity,
        )
        from ..sdk.host_bootstrap import retire_preboot_host

        tx, old, candidate = self._load()
        checkpoint, value = self._checkpoint(tx)
        boot = capture_boot_identity()
        if not boot_identities_differ(value['boot'], boot):
            raise ValueError('Recovery requires a different trusted operating-system boot. Use Windows Restart first.')
        phase = tx['recovery']['phase']
        try:
            self._assert_phase_ownership(tx, old, candidate, value['evidence'], phase)
            if phase in {'prepared', 'retiring'}:
                with self._quiescent(tx['realm']):
                    observed = self._evidence(tx, old, candidate)
                    changed = [key for key in observed if observed[key] != value['evidence'].get(key)]
                    if changed:
                        raise ValueError('Checkpoint-bound state changed since prepare: ' + ', '.join(changed))
                    self._save(tx, 'retiring')
                    if value['host'] is not None:
                        retire_preboot_host(self.data / 'runtime', value['host'], prepared_boot=value['boot'],
                                            current_boot=boot, quarantine=checkpoint.parent / 'host-markers')
                    elif any((self.data / 'runtime' / name).exists() for name in MARKERS):
                        raise ValueError('A host appeared after recovery preparation.')
                    self._save(tx, 'reconciled')
                phase = 'reconciled'
            # Artifact/backup bindings survive every phase; upgrade legitimately
            # changes catalog, credentials, discovery and SQLite housekeeping.
            if self._versions(old, candidate) != value['evidence']['versions']:
                raise ValueError('Recovery source artifacts changed.')
            if tree_digest(absolute(tx['backup']['root'])) != value['evidence']['backup']:
                raise ValueError('Retained recovery backup changed.')
            self._schema(old, candidate, tx['realm'])
            if phase == 'upgrading':
                self._journal(tx['realm'])
                # The prior call may have returned and left its temporary
                # Runtime alive before our phase publication was interrupted.
                self.backend.stop(candidate, self.data)
            if phase in {'reconciled', 'upgrading'}:
                # A complete Runtime upgrade is replayable, including a crash
                # after return but before our next durable phase. Incomplete
                # Runtime journals remain Runtime-owned and fail closed.
                with self._quiescent(tx['realm']):
                    self._journal(tx['realm'])
                    if not self.backend.service.recovery_state((old, candidate), self.data)['disabled']:
                        raise ValueError('Astrid was enabled before forward reconciliation finished.')
                self._save(tx, 'upgrading')
                self.backend.upgrade(candidate, self.data)
                self._journal(tx['realm'])
                self._save(tx, 'upgraded')
                phase = 'upgraded'
            if phase == 'upgraded':
                self.backend.stop(candidate, self.data)  # upgrade may own a temporary Runtime/host
                self._save(tx, 'stopped')
                phase = 'stopped'
            if phase == 'stopped':
                switch(self.root, candidate)
                self._save(tx, 'selected')
                phase = 'selected'
            if phase == 'selected':
                self.backend.register(candidate, self.data)
                self._save(tx, 'registered')
                phase = 'registered'
            if phase == 'registered':
                self.backend.start(candidate, self.data)
                self._save(tx, 'started')
                phase = 'started'
            if phase in {'started', 'verified', 'complete'}:
                self.backend.verify(candidate, self.data, tx['realm']['realm_id'])
                self._save(tx, 'verified')
                tx['recovery']['phase'] = 'complete'
                self.installer._save_update(tx, 'complete', service_healthy=True)
            else:
                raise ValueError(f'Unknown forward recovery phase: {phase}')
        except (Exception, KeyboardInterrupt) as failure:
            self._save(tx, tx['recovery']['phase'], error=str(failure))
            raise
        self.installer.say('Astrid candidate verified. Recovery complete; the live workspace, checkpoint and backup are retained.')
