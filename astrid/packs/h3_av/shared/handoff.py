"""Controller-to-worker transport for a prepared source."""
from __future__ import annotations

import json
from hashlib import file_digest
from pathlib import Path
from typing import Any, Mapping

from astrid.sdk.exceptions import CapabilityInvocationError


def _h3_remote_handoff(plan: Mapping[str, Any], root: Path) -> dict[str, Any] | None:
    """Freeze the controller's prepared H3 source for transport to its worker."""
    from .request import normalize_request
    from .stages import _authoritative_source_asset_id

    state_path = Path(plan['job_dir']) / 'h3' / 'build-state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    source_id = _authoritative_source_asset_id(normalize_request(state['request']))
    if source_id is None:
        return None
    preparation = state['preparation']
    conform = preparation.get('source_conform')
    if not isinstance(conform, Mapping) or not state.get('source'):
        raise CapabilityInvocationError('H3 remote source requires a frozen prepared-source handoff')
    prepared = Path(state['source'])
    descriptor = preparation.get('managed_source')
    expected = conform['output']['sha256']
    if (not isinstance(descriptor, Mapping) or descriptor.get('digest') != 'sha256:' + expected
            or descriptor.get('sha256') != expected):
        raise CapabilityInvocationError('H3 source was not published as the prepared managed object')
    if not prepared.is_file():
        raise CapabilityInvocationError('managed H3 prepared source digest mismatch')
    with prepared.open('rb') as stream:
        actual = file_digest(stream, 'sha256').hexdigest()
    if actual != expected:
        raise CapabilityInvocationError('managed H3 prepared source digest mismatch')
    lineage = {'digest': descriptor['digest'], 'source_asset': source_id,
               'source_sha256': conform['source']['sha256'],
               'source_range': conform['source']['range_seconds'],
               'fit': conform['output']['fit'], 'request_digest': state['request_digest']}
    conform = {**conform, 'source': {k: v for k, v in conform['source'].items() if k != 'path'}}
    envelope = {'version': 1, 'request_digest': state['request_digest'], 'asset': source_id,
                'managed_source': dict(descriptor), 'conform': conform}
    handoff = root / 'h3-prepared-source.json'
    handoff.write_text(json.dumps(envelope, sort_keys=True), encoding='utf-8')
    request_file = root / 'h3-request.json'
    request_file.write_text(json.dumps(state['request'], sort_keys=True), encoding='utf-8')
    source_map = {item['asset']: item['path'] for item in preparation['assets']}
    source_map['__astrid_h3_prepared__'] = str(handoff)
    return {'inputs': {'request': str(request_file), 'asset_map': source_map},
            'descriptor': descriptor, 'lineage': lineage}
