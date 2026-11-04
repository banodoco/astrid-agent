"""Ordinary PARAM edits preserve numeric and boolean values in film children."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline import commit
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from tests.editing_conformance.test_commit_runtime_numeric_move import (
    exact,
)
from tests.editing_conformance.test_commit_runtime_numeric_move import (
    numeric_film as numeric_film,
)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize('ref', ['parent/clip:music-bed', 'occurrence:occurrence-1/clip:clip-1'])
@pytest.mark.parametrize('before,after', [(2, 2.0), (1, True), (True, 1), (2, 2), (True, True)])
def test_native_film_param_edit_preserves_exact_image_and_inverse(
        numeric_film, ref, before, after):  # noqa: F811 - imported pytest fixture
    case = numeric_film

    def set_x(value, key):
        command = capture_clip_set_params(commit.bundle_from_document(case.document()), ref,
                                          values={'x': value})
        return case.submit([command], key)

    set_x(before, 'initial-x')
    original, base = copy.deepcopy(case.document()), case.head()
    accepted = set_x(after, 'changed-x')
    document = accepted['data']['canonical_document']
    value = State(commit.bundle_from_document(document)).entity(ref)['params']['x']
    assert type(value) is type(after) and value == after
    exact(case.document(), document)
    exact(case.document(base['revision_id']), original)
    exact(document['dependency_manifest']['media'], original['dependency_manifest']['media'])
    assert document['dependency_manifest']['documents'] == original['dependency_manifest']['documents']
    if ref.startswith('occurrence:'):
        exact(document['parent_revision']['payload']['clips'], original['parent_revision']['payload']['clips'])
        owner = State(commit.bundle_from_document(original)).occurrence('occurrence:occurrence-1')['shot_id']
        for shot in original['shot_revisions']:
            if shot['shot_id'] != owner:
                assert shot in document['shot_revisions']

    def undo(publication, key):
        return case.submit([{'schema_version': 1, 'op': 'timeline.inverse', 'version': '1',
                             'select': None, 'args': {'publications': [publication]}}], key)

    undone = undo(accepted['receipt']['receipt_id'], 'undo-numeric')
    exact(undone['data']['canonical_document'], original)
    redone = undo(undone['receipt']['receipt_id'], 'redo-numeric')
    exact(redone['data']['canonical_document'], document)
