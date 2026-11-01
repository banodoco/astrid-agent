"""Retain source, executed bytes and native outcomes in the producer envelope."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import is_dataclass, replace
import hashlib
import json
from pathlib import Path

from astrid.core._shared.result_manifest import (
    ResultManifestError, _validate_manifest, validate_result_manifest,
)


def _read_correlated_result(path: Path, *, task_id: str | None, attempt_id: str | None):
    """Check custody/bytes, leaving terminal-profile admission to F06.

    This lane's managed profile still rejects partial outputs on failure.
    Recovery must preserve those bytes and their actual outcomes for the
    terminal publisher; it must never relabel an execution as successful.
    """
    payload = json.loads(path.read_bytes())
    if (payload.get('kind') != 'managed-generation-result.v1' or payload.get('schema_version') != 1
            or task_id is not None and payload.get('task_id') != task_id
            or attempt_id is not None and payload.get('attempt_id') != attempt_id):
        raise ValueError('managed generation result does not match this execution')
    # Reuse universal output custody/hash verification without changing the
    # on-disk managed envelope or interpreting execution/cancel outcomes.
    outputs = [{**output, 'content_hash': 'sha256:' + output['sha256']}
               for output in payload['outputs']]
    if outputs:
        validate_result_manifest({**payload, 'outputs': outputs}, staging_root=path.parent)
    else:
        # The universal custody validator requires a concrete output. There
        # are no bytes to verify on an empty failure; retain the existing
        # shape check without imposing this lane's success-only terminal rules.
        _validate_manifest(payload)
    return payload


def stage_executed_settings(record, destination: Path) -> dict:
    """Retain the frozen API projection passed to the runtime, never a UI graph."""
    from vibecomfy.testing.canonical import canonical_bytes
    from vibecomfy.workflow_bundle import ApprovedProjectionRecord
    from astrid.core.foundation.atomic_io import write_bytes_atomic

    if not isinstance(record, ApprovedProjectionRecord):
        raise TypeError('executed settings require an approved projection record')
    data = canonical_bytes(record.to_dict()['api_projection'])
    sha256 = hashlib.sha256(data).hexdigest()
    if sha256 != record.api_digest:
        raise ValueError('executed settings do not match the approved API digest')
    path = Path(destination).resolve() / 'effective-settings.json'
    write_bytes_atomic(path, data)
    return {'sha256': sha256, 'bytes': len(data), 'filename': path.name, 'path': str(path)}


def bind_executed_settings(result, settings: dict, *, task_id: str, attempt_id: str | None):
    """Add the exact staged settings ref while retaining all producer evidence."""
    from astrid.core.foundation.atomic_io import write_json_atomic

    raw_path = getattr(result, 'managed_generation_result_path', None)
    if not raw_path:
        return result
    path = Path(raw_path)
    payload = _read_correlated_result(path, task_id=task_id, attempt_id=attempt_id)
    payload['inputs']['effective_settings'] = _executed_settings_input(
        payload['inputs'].get('effective_settings'), settings)
    write_json_atomic(path, payload)
    return _with_payload(result, payload)


def _executed_settings_input(existing, settings: dict) -> dict:
    if existing is not None:
        if not isinstance(existing, dict):
            raise ValueError('effective settings must identify the executed API bytes')
        digests = [existing[key] for key in ('sha256', 'digest', 'object_id') if key in existing]
        if not digests or any(not isinstance(digest, str)
                              or digest.removeprefix('sha256:') != settings['sha256'] for digest in digests):
            raise ValueError('effective settings ref differs from the executed API bytes')
        if existing.get('bytes', settings['bytes']) != settings['bytes']:
            raise ValueError('effective settings ref differs from the executed API size')
        # A project-associated ref remains authoritative after spool cleanup.
        bound = {**settings, **existing}
        if existing.get('object_id') or existing.get('digest'):
            bound.pop('path', None)
        else:
            bound.update(settings)
    else:
        bound = dict(settings)
    return bound


def _with_payload(result, payload):
    if hasattr(result, 'managed_generation_result'):
        if is_dataclass(result) and result.__dataclass_params__.frozen:
            return replace(result, managed_generation_result=payload)
        result.managed_generation_result = payload
    return result


def source_input_ref(workflow_path: Path, destination: Path) -> dict | None:
    """Identify the retained source member, before any producer conversion."""
    path = Path(workflow_path)
    if path.suffix == '.py' and path.with_name('source.json').is_file():
        path = path.with_name('source.json')
    if not path.is_file() or path.is_symlink():
        return None
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    from astrid.core.foundation.atomic_io import write_bytes_atomic
    staged = Path(destination).resolve() / 'source-input' / path.name
    write_bytes_atomic(staged, data)
    # A digest of a local file alone is not proof of a project association.
    # The admitted source ref, when supplied by the producer, takes priority.
    return {'sha256': sha256, 'bytes': len(data), 'filename': path.name,
            'path': str(staged)}


def _projection(runtime: dict):
    from vibecomfy.workflow_bundle import ApprovedProjectionRecord
    record = ApprovedProjectionRecord.from_dict(runtime['approved_projection'])
    if runtime.get('api_digest', record.api_digest) != record.api_digest:
        raise ValueError('native receipt differs from its executed API digest')
    return record


def _complete_evidence(payload: dict, *, source: dict | None,
                       attempt: dict, metadata: dict, transport: dict | None):
    """Fill known native facts; never replace supplied refs or owner evidence."""
    inputs = payload['inputs']
    if source is not None:
        inputs.setdefault('source', deepcopy(source))
    inputs.setdefault('values', deepcopy({key: value for key, value in inputs.items()
                                         if key not in {'source', 'effective_settings', 'values'}}))
    producer = payload['evidence']['producer']
    producer.setdefault('engine', {'id': 'comfyui'})
    producer.setdefault('entry', {'id': metadata.get('workflow_id'),
                                  'source': deepcopy(inputs.get('source'))})
    models = []
    for model in metadata.get('model_manifest') or attempt.get('model_manifest') or []:
        recorded, observed = model.get('expected_sha256'), model.get('actual_sha256')
        models.append({'name': model.get('name'), 'source': model.get('url'),
                       'recorded_sha256': recorded, 'observed_sha256': observed,
                       'identity_status': ('verified' if recorded and recorded == observed else
                                           'observed' if observed else 'unknown')})
    producer.setdefault('models', models)
    producer.setdefault('versions', {
        'vibecomfy': {'recorded': None, 'observed': metadata.get('runtime_version') or attempt.get('runtime_version')},
        'comfyui': {'recorded': None, 'observed': metadata.get('comfy_commit') or attempt.get('comfy_commit')},
        'astrid': {'recorded': None, 'observed': None},
        'runtime': {'recorded': None, 'observed': None}})
    producer.setdefault('provider', None)
    producer.setdefault('build', None)
    owner = payload['evidence']['transport']
    for key, value in (transport or {}).items():
        owner.setdefault(key, deepcopy(value))
    owner.setdefault('target', None)
    owner.setdefault('execution_binding', None)
    owner.setdefault('failure', None)
    owner.setdefault('cancel', None)


def workflow_transport(profile_id: str, comfy_url: str) -> dict:
    """Keep route ownership explicit; checkout binding evidence comes from its owner."""
    from astrid.core.generation.targets import TargetRecord
    if profile_id == 'checkout_server':
        return {}
    return {'target': TargetRecord(owner='human' if profile_id == 'local_server' else 'astrid',
                                   endpoint=comfy_url or None).as_dict()}


def complete_producer_result(result, destination: Path, *, task_id: str,
                             attempt_id: str | None, source: dict | None = None,
                             transport: dict | None = None):
    """Stage the native executed projection while its receipt is still in custody.

    Attached and embedded producers persist the exact approved record in the
    existing envelope. Decoding it is evidence readback, never recompilation or
    an attempt to authorize a new run.
    """
    from astrid.core.foundation.atomic_io import write_json_atomic
    raw_path = getattr(result, 'managed_generation_result_path', None)
    if not raw_path:
        return result
    path = Path(raw_path)
    payload = _read_correlated_result(path, task_id=task_id, attempt_id=attempt_id)
    producer = payload['evidence']['producer']
    runtime = producer.get('runtime') or {}
    if 'approved_projection' not in runtime:
        return result
    settings = stage_executed_settings(_projection(runtime), destination)
    result = bind_executed_settings(result, settings, task_id=task_id, attempt_id=attempt_id)
    payload = _read_correlated_result(path, task_id=task_id, attempt_id=attempt_id)
    _complete_evidence(payload, source=source,
                       attempt=(producer.get('attempt') or {}).get('record') or {},
                       metadata=(producer.get('metadata') or {}).get('record') or {},
                       transport=transport)
    write_json_atomic(path, payload)
    return _with_payload(result, payload)


def _native_failure_result(error, receipt: Path, destination: Path, *, task_id: str,
                           attempt_id: str | None, source: dict | None,
                           workflow_path: Path | None, transport: dict | None,
                           workflow_bundle=None, artifact_roots=()):
    """Project a native failed attempt into the existing managed envelope."""
    import asyncio
    import mimetypes
    import shutil
    from datetime import datetime, timezone
    from astrid.core.foundation.atomic_io import write_json_atomic

    if (attempt_id is None or receipt.is_symlink()
            or any(parent.is_symlink() for parent in receipt.parents)
            or not receipt.resolve().is_relative_to(destination.resolve())):
        raise ValueError('native receipt is outside this Runtime attempt')
    attempt = json.loads(receipt.read_bytes())
    runtime = attempt['runtime_evidence']
    record = _projection(runtime)
    if (attempt.get('task_id', task_id) != task_id
            or attempt.get('attempt_id', attempt_id) != attempt_id):
        raise ValueError('native receipt belongs to another Runtime attempt')
    terminal = runtime.get('terminal') or attempt.get('terminal') or {}
    interrupted = isinstance(error, KeyboardInterrupt) or terminal.get('phase') == 'interrupted'
    cancelled = (isinstance(error, asyncio.CancelledError)
                 or isinstance(error, SystemExit) and error.code in {130, 143}
                 or terminal.get('phase') == 'cancelled')
    status = 'interrupted' if interrupted else 'cancelled' if cancelled else 'failed'
    outputs, warnings = [], []
    artifacts = attempt.get('artifacts') or []
    # Use the producer's own declared-output attribution and ordinal rules.
    declarations = []
    if (workflow_path is not None or workflow_bundle is not None) and artifacts:
        from vibecomfy.workflow_bundle import load_bundle
        from vibecomfy.security.provenance import Provenance
        from vibecomfy.runtime.session import _artifact_manifest
        candidates = [destination / 'local-server-workflow' / 'workflow.py', workflow_path,
                      *destination.glob('canonical-ui-*/workflow.py')]
        def bundles():
            if workflow_bundle is not None:
                yield workflow_bundle
                return
            for candidate in candidates:
                if candidate is None or candidate.suffix != '.py' or not candidate.is_file():
                    continue
                try:
                    yield load_bundle(candidate, trust=Provenance.USER_CONFIRMED)
                except (OSError, ValueError):
                    continue
        for bundle in bundles():
            if bundle.revision_id != record.revision_id:
                continue
            workflow = bundle.workflow
            declarations = list(workflow.outputs)
            attribution = _artifact_manifest(workflow, [item.get('reported_path', '') for item in artifacts])
            by_path = {item['path']: item['output'] for item in attribution['attribution']}
            break
        else:
            by_path = {}
    else:
        by_path = {}
    indexed = []
    for artifact in artifacts:
        port = by_path.get(artifact.get('reported_path'))
        matches = [(index, declaration) for index, declaration in enumerate(declarations)
                   if declaration.name == port]
        if len(matches) != 1:
            warnings.append('Native partial artifact has no unambiguous declared output')
            continue
        indexed.append((*matches[0], artifact))
    indexed.sort(key=lambda item: (item[0], str(item[2].get('reported_path') or '')))
    for ordinal, (_, declaration, artifact) in enumerate(indexed):
        try:
            if not isinstance(artifact.get('path'), str) or not artifact['path']:
                raise ValueError('partial artifact has no local bytes')
            path = Path(artifact['path'])
            if not path.is_absolute():
                path = receipt.parent / path
            roots = (destination, receipt.parent, *artifact_roots)
            if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
                    or not any(path.resolve(strict=True).is_relative_to(Path(root).resolve()) for root in roots)):
                raise ValueError('partial artifact escaped native custody')
            media_type = declaration.mime_type or mimetypes.guess_type(path.name)[0]
            if not media_type:
                raise ValueError('partial artifact has no declared media type')
            staged = destination / 'partial-outputs' / f'{ordinal:04d}-{path.name}'
            staged.parent.mkdir(parents=True, exist_ok=True)
            before = path.stat()
            shutil.copyfile(path, staged)
            after = path.stat()
            from astrid.core.foundation.hash import sha256_file
            sha256 = sha256_file(staged)
            size = staged.stat().st_size
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or artifact.get('sha256', sha256) != sha256
                    or artifact.get('bytes', size) != size):
                staged.unlink()
                raise ValueError('partial artifact differs from native byte evidence')
            outputs.append({'producer_output_id': f'vibecomfy:{declaration.name}:{ordinal}',
                            'output_port': declaration.name, 'ordinal': ordinal,
                            'path': staged.relative_to(destination).as_posix(), 'media_type': media_type,
                            'bytes': size, 'sha256': sha256})
        except (OSError, ValueError, KeyError) as exc:
            warnings.append(f'Native partial artifact unavailable: {exc}')
    execution = terminal.get('execution_state')
    execution = execution if execution in {'succeeded', 'failed', 'cancelled', 'interrupted'} else status
    retrieval = terminal.get('retrieval_state')
    retrieval = retrieval if retrieval in {'succeeded', 'failed', 'pending', 'not_started'} else (
        'failed' if warnings else 'succeeded' if outputs else 'not_started')
    verification = terminal.get('verification_state')
    verification = verification if verification in {'succeeded', 'failed', 'pending', 'not_started'} else (
        'failed' if terminal.get('phase') == 'verification' else 'not_started')
    payload = {
        'schema_version': 1, 'kind': 'managed-generation-result.v1',
        'task_id': task_id, 'attempt_id': attempt_id, 'producer_run_id': receipt.parent.name,
        'created': datetime.now(timezone.utc).isoformat(), 'warnings': warnings,
        'inputs': deepcopy(attempt.get('inputs') or {}), 'outputs': outputs,
        'outcomes': {phase: {'status': value} for phase, value in (
            ('execution', execution), ('retrieval', retrieval),
            ('verification', verification), ('publication', 'not_started'))},
        'evidence': deepcopy(attempt.get('evidence') or {'producer': {}, 'transport': {}})}
    payload['inputs'].setdefault('values', record.to_dict()['input_binding'])
    payload['evidence']['producer'].setdefault('runtime', runtime)
    payload['evidence']['producer'].setdefault('attempt', {'path': str(receipt), 'record': attempt})
    _complete_evidence(payload, source=source, attempt=attempt,
                       metadata={}, transport=transport)
    owner = payload['evidence']['transport']
    if owner.get('failure') is None:
        owner['failure'] = {'type': type(error).__name__, 'message': str(error),
                            'receipt_path': str(receipt), 'terminal': terminal}
    if cancelled or interrupted:
        if owner.get('cancel') is None:
            owner['cancel'] = {'requested': True, 'observed_stop': None,
                               'native': terminal}
    settings = stage_executed_settings(record, destination)
    payload['inputs']['effective_settings'] = _executed_settings_input(
        payload['inputs'].get('effective_settings'), settings)
    path = destination / 'managed-generation-result.json'
    write_json_atomic(path, payload)
    _read_correlated_result(path, task_id=task_id, attempt_id=attempt_id)
    error.managed_generation_result_path = path


@contextmanager
def preserve_result_on_error(destination: Path, *, task_id: str, attempt_id: str | None,
                             source: dict | None = None, workflow_path: Path | None = None,
                             transport: dict | None = None, workflow_bundle=None, artifact_roots=()):
    """Expose correlated results, or project a new native receipt at the known path.

    Producer wrappers may replace an exception while retaining its cause and
    native attempt receipt. Keep the existing managed result accessible to F06
    without replacing the exception or creating a second terminal profile.
    """
    destination = Path(destination).resolve()
    previous_receipts = set(destination.glob('.vibecomfy-runtime/out/runs/*/attempt.json'))
    try:
        yield
    except BaseException as error:
        candidates = []
        receipts = []
        current = error
        seen = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            path = getattr(current, 'managed_generation_result_path', None)
            if path:
                candidates.append(Path(path))
            receipt = getattr(current, 'receipt_path', None)
            if receipt:
                candidates.append(Path(receipt).with_name('managed-generation-result.json'))
                receipts.append(Path(receipt))
            current = current.__cause__ or current.__context__
        candidates.append(Path(destination) / 'managed-generation-result.json')
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                path = path.resolve(strict=True)
                _read_correlated_result(path, task_id=task_id, attempt_id=attempt_id)
            except (OSError, ValueError, TypeError, KeyError, AttributeError, ResultManifestError):
                continue
            error.managed_generation_result_path = path
            try:
                complete_producer_result(error, destination, task_id=task_id, attempt_id=attempt_id,
                                         source=source, transport=transport)
            except Exception as evidence_error:
                error.add_note(f'Could not retain producer settings evidence: {evidence_error}')
            break
        else:
            receipts.extend(path for path in destination.glob('.vibecomfy-runtime/out/runs/*/attempt.json')
                            if path not in previous_receipts)
            # An ambiguous native run is recovery work, never an invented run id.
            receipts = [path for path in dict.fromkeys(receipts)
                        if path not in previous_receipts]
            if len(receipts) == 1:
                try:
                    _native_failure_result(error, receipts[0], destination, task_id=task_id,
                        attempt_id=attempt_id, source=source, workflow_path=workflow_path,
                        transport=transport, workflow_bundle=workflow_bundle, artifact_roots=artifact_roots)
                except Exception as evidence_error:
                    error.add_note(f'Could not retain native producer failure evidence: {evidence_error}')
        raise
