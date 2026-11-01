from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tests.editing_conformance.fixture_loader import load_fixture

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.mark.parametrize('side', ['input', 'output'])
def test_record_restoration_does_not_rewrite_opaque_input_payload(tmp_path, side):
    source = json.loads((FIXTURES / 'recipe_m2_clip_add.json').read_text())
    opaque = {
        'records': [{
            'command': {'created': ['authored'], 'reads': ['authored']},
            'reads': ['authored'],
        }],
        'command': {'reads': ['opaque — \n']},
        'reads': {'records': [None, False, {'created': 'authored'}]},
    }
    source[side]['parent_revision']['payload']['opaque'] = copy.deepcopy(opaque)
    expected_bytes = json.dumps(opaque, ensure_ascii=False).encode('utf-8')
    fixture = tmp_path / 'recipe_m2_clip_add.json'
    fixture.write_text(json.dumps(source))

    loaded = load_fixture(fixture)

    actual = loaded[side]['parent_revision']['payload']['opaque']
    assert actual == opaque
    assert json.dumps(actual, ensure_ascii=False).encode('utf-8') == expected_bytes
