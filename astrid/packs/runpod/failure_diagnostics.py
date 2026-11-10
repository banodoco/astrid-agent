"""Fixed, bounded failure protocol for the remote generation boundary.

This file is shipped as standard-library-only source to the owned pod. Neither
side of the boundary returns producer messages, paths, URLs or loose output.
"""
from __future__ import annotations

import json
import os
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


def fallback():
    return {'code': 'unknown', 'category': 'unknown'}


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
                if isinstance(details, Mapping):
                    candidates.append(details.get('engine_code'))
                candidates.append(issue.get('code'))
    error = value.get('error')
    if isinstance(error, Mapping):
        candidates.append(error.get('code'))
    candidates.append(value.get('code'))
    for code in candidates:
        if isinstance(code, str) and code in CODES:
            return {'code': code, 'category': CODES[code]}
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
        if not isinstance(value, dict) or set(value) != {'code', 'category'}:
            return fallback()
        code = value['code']
        if isinstance(code, str) and code in CODES and value['category'] == CODES[code]:
            return {'code': code, 'category': CODES[code]}
    except (ValueError, TypeError, RecursionError):
        pass
    return fallback()


if __name__ == '__main__':
    print(MARK + json.dumps(read_result(sys.argv[1]) if len(sys.argv) == 2 else fallback(), sort_keys=True))
