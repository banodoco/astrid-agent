"""Portable reads of genuine pre-enforcement PARAM acceptance (see fixture README)."""
from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

ARCHIVE = Path(__file__).parents[1] / 'sdk/fixtures/selected-param-legacy/accepted.zip'


def accepted_params():
    with ZipFile(ARCHIVE) as archive:
        return json.loads(archive.read('accepted.json'))
