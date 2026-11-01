"""Run with PYTHONPATH pointing at archived Agent ce363f6d and Runtime b748ae72.

Produces accepted.json and an authenticated Runtime backup in one portable ZIP.
No accepted records or persisted state are written outside Runtime's normal API.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
import yaml

from astrid.core.element import project_packs as packs
from astrid.sdk.timeline import commit
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from tests.core import test_project_packs
from tests.helpers.runtime import use_runtime
from tests.sdk.test_timeline_selected_schema_runtime import retained_pack

AGENT = 'ce363f6d6d60e504dbd4d79803c2d6299f648ade'
RUNTIME = 'b748ae723b51c6d578ba7a393209ce37d6e2b0c3'


def generate(destination):
    use_runtime()
    from runtime_protocol import service

    assert hashlib.sha256(Path(commit.__file__).read_bytes()).hexdigest() == (
        '524619491e5880b493f89175301098daa66cc37244e64572f8f00a93f0e3124e')
    assert hashlib.sha256(Path(service.__file__).read_bytes()).hexdigest() == (
        'df8cec3469bdefceac4247b80e08cb853b1f9dbf47b32846e13237353302a108')
    with tempfile.TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as patch:
        root = Path(temporary)
        configured = test_project_packs.configured.__wrapped__(root, patch)
        fixture = retained_pack.__wrapped__(configured, root, patch)
        case = next(fixture)
        try:
            definition = yaml.safe_load(case.path.read_text())
            definition['schema']['properties']['seed']['maximum'] = 20
            case.path.write_text(yaml.safe_dump(definition))
            admitted = packs.admit_project_pack('realm', case.project)
            other = admitted['elements'][0]['descriptor']
            document = case.service.get_document(case.project, admitted['documentId'])
            pin = {'document_id': document['document_id'], 'version': document['version'],
                   'content_digest': commit._digest(document['content'])}
            objects = {}
            for admission in document['content']['admissions']:
                descriptor = admission['snapshot_object']
                data = case.service.cas.read(descriptor['object_id'].removeprefix('sha256:'))
                objects[descriptor['object_id']] = {
                    **descriptor, 'bytes_base64': base64.b64encode(data).decode('ascii')}
            options = {'schema_source': pin, 'realm_id': 'realm',
                       'schema_inputs': [({'pin': pin, 'document': document}, objects)]}
            row = copy.deepcopy(State(case.bundle()).entity('parent/clip:effect'))
            row['id'] = 'legacy-alias'
            row['element_ref'] = {key: other[key] for key in ('id', 'kind', 'revision', 'packId')}
            assert row['elementRef'] == case.selected
            assert row['element_ref']['revision'] != case.selected['revision']
            ref = 'parent/clip:legacy-alias'
            case.submit(case.request([capture_clip_add(case.bundle(), 'parent', row)]), 'legacy-birth')
            before = case.head()
            request = case.request([capture_clip_set_params(
                case.bundle(), ref, values={'seed': 8}, **options)])
            accepted = case.submit(request, 'legacy-params')
            assert State(case.bundle()).entity(ref) == {**row, 'params': {**row['params'], 'seed': 8}}
            store = case.service._timeline_commits()[0]
            entries = store.history(case.project, 'film', case.head())['items']
            assert len(entries) == 5
            documents = {entry['revision_id']: store.revision_closure(
                case.project, 'film', entry['revision_id']) for entry in entries}
            payload = {'agent': AGENT, 'runtime': RUNTIME, 'project': case.project,
                       'head': case.head(), 'before': before, 'entries': entries,
                       'documents': documents, 'options': options, 'row': row,
                       'selected': case.selected, 'request': request, 'accepted': accepted}
            backup = root / 'backup'
            case.service.backup(backup)
            with ZipFile(destination, 'w', compression=ZIP_DEFLATED) as archive:
                archive.writestr('accepted.json', json.dumps(payload, sort_keys=True) + '\n')
                # A fixture-only operator key; restore still verifies the real backup HMAC.
                archive.write(case.root / '.operator-backup-key', 'backup-auth.key')
                for path in sorted(backup.rglob('*')):
                    if path.is_file():
                        archive.write(path, 'backup/' + str(path.relative_to(backup)))
        finally:
            fixture.close()


if __name__ == '__main__':
    generate(Path(sys.argv[1]).resolve())
