"""Load compact conformance fixtures without changing their test-facing shape."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

# These are the authored golden-record locations in the ten compact fixtures.
# Keeping the paths explicit is deliberate: authored input/output payloads may
# contain opaque keys named ``records``, ``command``, or ``reads``.
_RECORD_PATHS: dict[str, tuple[tuple[str | int, ...], ...]] = {
    'recipe_m2_app_clip_scalar_fields.json': (('records',),),
    'recipe_m2_clip_add.json': (('records',),),
    'recipe_m2_clip_split.json': (('records',),),
    'recipe_m2_clip_trim.json': (('records',),),
    'recipe_m2_occurrence_delete.json': (('records',),),
    'recipe_m2_occurrence_duplicate.json': (('records',),),
    'recipe_m2_occurrence_media.json': (('add', 'records'), ('swap', 'records')),
    'recipe_m2_placement.json': (('cases', 0, 'records'), ('cases', 1, 'records')),
    'recipe_m2_placement_create_unplace.json': (
        ('create', 'records'),
        ('unplace', 'records'),
    ),
    'recipe_m2_shot_pool.json': (
        ('add-first', 'records'),
        ('add-second', 'records'),
        ('remove-middle', 'records'),
    ),
}


def load_fixture(path: str | Path) -> Any:
    """Load a compact fixture and restore only its known factored values."""
    path = Path(path)
    fixture = json.loads(path.read_text(encoding='utf-8'))
    if path.name == 'recipe_m2_placement_create_unplace.json':
        for operation in fixture.values():
            for record, command in zip(operation['records'], operation['commands'], strict=True):
                record.setdefault('command', copy.deepcopy(command))
    _restore_record_fields(fixture, _RECORD_PATHS.get(path.name, ()))

    if path.name == 'recipe_m2_placement.json':
        shared_input = fixture.pop('input', None)
        if shared_input is not None:
            for case in fixture['cases']:
                case['input'] = copy.deepcopy(shared_input)
    elif path.name == 'recipe_m2_occurrence_media.json':
        shared_input = fixture.pop('input', None)
        shared_verified_media = fixture.pop('verified_media', None)
        for operation in fixture.values():
            if shared_input is not None:
                operation['input'] = copy.deepcopy(shared_input)
            if shared_verified_media is not None:
                operation['verified_media'] = copy.deepcopy(shared_verified_media)
    elif path.name == 'recipe_m2_placement_create_unplace.json':
        if 'input' not in fixture['unplace']:
            fixture['unplace']['input'] = copy.deepcopy(fixture['create']['output'])

    return fixture


def _restore_record_fields(
    fixture: Any,
    record_paths: tuple[tuple[str | int, ...], ...],
) -> None:
    for record_path in record_paths:
        records = fixture
        for part in record_path:
            records = records[part]
        for record in records:
            command = record.get('command')
            if not isinstance(command, dict):
                continue
            for key in ('created', 'reads', 'selections'):
                if key not in record and key in command:
                    record[key] = copy.deepcopy(command[key])
