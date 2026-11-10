"""Fixed, bounded failure protocol for the remote generation boundary.

This file is shipped as standard-library-only source to the owned pod. Neither
side of the boundary returns producer messages, paths, URLs or loose output.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from collections.abc import Mapping

MARK = 'astrid-generation-failure:'
MAX_RESULT_BYTES = 64 * 1024
MAX_FRAME_BYTES = 4096
CODES = {
    'input_rejected': 'input', 'missing_dependency': 'dependency',
    'engine_not_found': 'installation', 'unknown_engine': 'installation',
    'unsupported_platform': 'installation', 'unsupported_placement': 'installation',
    'invalid_recipe': 'installation', 'invalid_model_lock': 'installation',
    'engine_reinstall_required': 'installation', 'integrity_failed': 'integrity',
    'unsafe_path': 'integrity', 'attestation_invalid': 'integrity',
    'endpoint_ownership': 'ownership', 'custody_unresolved': 'ownership',
    'target_unreachable': 'health', 'endpoint_unhealthy': 'health',
    'missing_model': 'model', 'download_failed': 'model', 'missing_node_pack': 'dependency',
    'deadline_exceeded': 'deadline', 'transport_failed': 'transport',
    'command_failed': 'execution', 'generation_failed': 'execution',
    'generation_route_unqualified': 'qualification',
}
FACTS = ('node_id', 'node_class', 'field', 'constraint')
FACT_ALLOW = {
    'node_class': frozenset({
        'KSampler', 'CLIPTextEncode', 'SaveImage', 'LoadImage', 'EmptyLatentImage',
        'CheckpointLoaderSimple', 'VAEDecode', 'VAEEncode', 'LoadAudio',
        'SaveAudio', 'TextToSpeech', 'ModelLoader',
    }),
    'field': frozenset({
        'prompt', 'negative_prompt', 'seed', 'steps', 'cfg', 'sampler_name',
        'scheduler', 'denoise', 'model', 'clip', 'vae', 'latent_image',
        'image', 'audio', 'text', 'filename_prefix', 'width', 'height',
        'strength', 'source', 'input', 'output',
    }),
    'constraint': frozenset({
        'required', 'min', 'max', 'type', 'enum', 'range', 'positive',
        'nonempty', 'missing', 'invalid', 'unsupported', 'min_value',
        'max_value', 'choice', 'format', 'integer', 'finite',
    }),
}
EXCEPTIONS = frozenset({
    'MachineError', 'MachineRunError', 'MachineRunCancelled', 'RemotePlacementError',
    'RunPodWorkerOwnerError', 'TargetObservationError', 'AcquisitionUnresolvedError',
    'PreparedWorkerError', 'PreparationDeadlineExceeded', 'DigestMismatch',
    'CustodyChanged', 'TimeoutError', 'ValueError', 'RuntimeError', 'OSError',
    'ConnectionError', 'PermissionError', 'FileNotFoundError', 'IntegrityError',
    'DeadlineExceeded', 'UnknownError',
})


def _safe_fact(key, value, node_ids=None):
    if key == 'node_id':
        if type(value) is int and 0 <= value < 10**12:
            return str(value) if node_ids is None or str(value) in node_ids else None
        if isinstance(value, str) and re.fullmatch(r'[0-9]{1,12}', value) and int(value) < 10**12:
            canonical = str(int(value))
            return canonical if node_ids is None or canonical in node_ids else None
    elif isinstance(value, str) and value in FACT_ALLOW.get(key, ()):
        return value
    return None


def fallback():
    return {'code': 'unknown', 'category': 'unknown'}


def host_summary(error, diagnostic=None, *, stage='execution', node_ids=frozenset()):
    """Journal only bounded facts; exception text and remote output are untrusted."""
    safe = summarize(diagnostic, node_ids=node_ids) if isinstance(diagnostic, Mapping) else fallback()
    if isinstance(diagnostic, Mapping) and {'code', 'category'} <= set(diagnostic) <= {'code', 'category', *FACTS}:
        code = diagnostic.get('code')
        if isinstance(code, str) and code in CODES and diagnostic.get('category') == CODES[code]:
            safe = {'code': code, 'category': CODES[code]}
            safe.update({key: _safe_fact(key, diagnostic[key], node_ids) for key in FACTS if key in diagnostic
                         and _safe_fact(key, diagnostic[key], node_ids) is not None})
    name = type(error).__name__ if error is not None else 'UnknownError'
    if name not in EXCEPTIONS:
        name = 'UnknownError'
    return {'exception': name, 'stage': stage if isinstance(stage, str) and stage in {'claim', 'preparation', 'start', 'poll', 'fetch',
            'execution', 'reset', 'cancel'} else 'execution', **safe}


def safe_stored_summary(value, *, node_ids=frozenset()):
    """Revalidate a journal read before it reaches machine listing or CLI."""
    if not isinstance(value, Mapping):
        return fallback()
    diagnostic = {key: value[key] for key in ('code', 'category', *FACTS) if key in value}
    result = host_summary(None, diagnostic, stage=value.get('stage'), node_ids=node_ids)
    name = value.get('exception')
    if isinstance(name, str) and name in EXCEPTIONS:
        result['exception'] = name
    return result


def summarize(value, *, node_ids=frozenset()):
    """Project only a known error identifier; categories are locally defined."""
    if not isinstance(value, Mapping):
        return fallback()
    candidates = []
    issues = value.get('issues')
    if isinstance(issues, list):
        for issue in issues:
            if isinstance(issue, Mapping) and issue.get('severity') == 'error':
                details = issue.get('details')
                facts = {}
                if isinstance(details, Mapping):
                    for key in FACTS:
                        fact = _safe_fact(key, details.get(key), node_ids)
                        if fact is not None:
                            facts[key] = fact
                    candidates.append((details.get('engine_code'), facts))
                candidates.append((issue.get('code'), facts))
    error = value.get('error')
    if isinstance(error, Mapping):
        candidates.append((error.get('code'), {}))
    candidates.append((value.get('code'), {}))
    for code, facts in candidates:
        if isinstance(code, str) and code in CODES:
            return {'code': code, 'category': CODES[code], **facts}
    return fallback()


def graph_node_ids(path):
    """Only structural IDs in a submitted, bounded JSON graph authorize node facts."""
    if not isinstance(path, (str, os.PathLike)):
        return frozenset()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESULT_BYTES:
                return frozenset()
            raw = stream.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            return frozenset()
        graph = json.loads(raw)
        nodes = graph.get('nodes') if isinstance(graph, Mapping) else None
        identifiers = nodes.keys() if isinstance(nodes, Mapping) else (
            [node.get('id') for node in nodes if isinstance(node, Mapping)] if isinstance(nodes, list) else ())
        result = set()
        for value in identifiers:
            if len(result) >= 256:
                return frozenset()
            canonical = _safe_fact('node_id', value)
            if canonical is not None:
                result.add(canonical)
        return frozenset(result)
    except (OSError, ValueError, TypeError, RecursionError):
        return frozenset()


def stored_node_ids(value):
    if not isinstance(value, list) or len(value) > 256:
        return frozenset()
    ids = frozenset(_safe_fact('node_id', item) for item in value)
    return ids if None not in ids and len(ids) == len(value) else frozenset()


def _submitted_graph_ids(job_path):
    try:
        fd = os.open(job_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESULT_BYTES:
                return frozenset()
            raw = stream.read(MAX_RESULT_BYTES + 1)
        job = json.loads(raw)
        source = job.get('execution', {}).get('source') if isinstance(job, Mapping) else None
        return graph_node_ids(source)
    except (OSError, ValueError, TypeError, RecursionError):
        return frozenset()


def read_result(path, job_path=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            fact = os.fstat(stream.fileno())
            if not stat.S_ISREG(fact.st_mode) or fact.st_size > MAX_RESULT_BYTES:
                return fallback()
            raw = stream.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            return fallback()
        return summarize(json.loads(raw), node_ids=_submitted_graph_ids(job_path) if job_path else frozenset())
    except (OSError, ValueError, TypeError, RecursionError):
        return fallback()


def parse_frame(stdout, *, node_ids=frozenset()):
    if not isinstance(stdout, str) or len(stdout.encode('utf-8')) > MAX_FRAME_BYTES:
        return fallback()
    lines = [line[len(MARK):] for line in stdout.splitlines() if line.startswith(MARK)]
    if len(lines) != 1:
        return fallback()
    try:
        value = json.loads(lines[0])
        if not isinstance(value, dict) or not {'code', 'category'} <= set(value) or not set(value) <= {'code', 'category', *FACTS}:
            return fallback()
        code = value['code']
        if isinstance(code, str) and code in CODES and value['category'] == CODES[code]:
            facts = {key: _safe_fact(key, value[key], node_ids) for key in FACTS if key in value
                     and _safe_fact(key, value[key], node_ids) is not None}
            return {'code': code, 'category': CODES[code], **facts}
    except (ValueError, TypeError, RecursionError):
        pass
    return fallback()


if __name__ == '__main__':
    print(MARK + json.dumps(read_result(sys.argv[1], sys.argv[2]) if len(sys.argv) == 3 else fallback(), sort_keys=True))
