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
EXCEPTIONS = frozenset({
    'MachineError', 'MachineRunError', 'MachineRunCancelled', 'RemotePlacementError',
    'RunPodWorkerOwnerError', 'TargetObservationError', 'AcquisitionUnresolvedError',
    'PreparedWorkerError', 'PreparationDeadlineExceeded', 'DigestMismatch',
    'CustodyChanged', 'TimeoutError', 'ValueError', 'RuntimeError', 'OSError',
    'ConnectionError', 'PermissionError', 'FileNotFoundError', 'IntegrityError',
    'DeadlineExceeded', 'UnknownError',
})


def _safe_fact(value):
    if type(value) is int and 0 <= value < 10**12:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', value):
        return value
    return None


def fallback():
    return {'code': 'unknown', 'category': 'unknown'}


def host_summary(error, diagnostic=None, *, stage='execution'):
    """Journal only bounded facts; exception text and remote output are untrusted."""
    safe = summarize(diagnostic) if isinstance(diagnostic, Mapping) else fallback()
    if isinstance(diagnostic, Mapping) and {'code', 'category'} <= set(diagnostic) <= {'code', 'category', *FACTS}:
        code = diagnostic.get('code')
        if isinstance(code, str) and code in CODES and diagnostic.get('category') == CODES[code]:
            safe = {'code': code, 'category': CODES[code]}
            safe.update({key: diagnostic[key] for key in FACTS if key in diagnostic
                         and _safe_fact(diagnostic[key]) == diagnostic[key]})
    name = type(error).__name__ if error is not None else 'UnknownError'
    if name not in EXCEPTIONS:
        name = 'UnknownError'
    return {'exception': name, 'stage': stage if stage in {'claim', 'preparation', 'start', 'poll', 'fetch',
            'execution', 'reset', 'cancel'} else 'execution', **safe}


def safe_stored_summary(value):
    """Revalidate a journal read before it reaches machine listing or CLI."""
    if not isinstance(value, Mapping):
        return fallback()
    result = host_summary(None, value, stage=value.get('stage'))
    name = value.get('exception')
    if isinstance(name, str) and name in EXCEPTIONS:
        result['exception'] = name
    return result


def summarize(value):
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
                        fact = _safe_fact(details.get(key))
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


def read_result(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            fact = os.fstat(stream.fileno())
            if not stat.S_ISREG(fact.st_mode) or fact.st_size > MAX_RESULT_BYTES:
                return fallback()
            raw = stream.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            return fallback()
        return summarize(json.loads(raw))
    except (OSError, ValueError, TypeError, RecursionError):
        return fallback()


def parse_frame(stdout):
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
            facts = {key: value[key] for key in FACTS if key in value and _safe_fact(value[key]) == value[key]}
            if set(facts) == set(value) - {'code', 'category'}:
                return {'code': code, 'category': CODES[code], **facts}
    except (ValueError, TypeError, RecursionError):
        pass
    return fallback()


if __name__ == '__main__':
    print(MARK + json.dumps(read_result(sys.argv[1]) if len(sys.argv) == 2 else fallback(), sort_keys=True))
