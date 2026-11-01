"""Thin discovery and execution helpers for canonical workflow entries."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


def user_entries() -> list[dict[str, Any]]:
    try:
        from vibecomfy.entries import discover_entries
    except ImportError:
        return []
    return discover_entries()


def _ready_entries_for(entry_id: str | None = None) -> list[dict[str, Any]]:
    """Project repository metadata, leaving unclassified modes explicit."""
    try:
        import vibecomfy
        root = Path(vibecomfy.__file__).resolve().parent.parent
        index = json.loads((root / 'template_index.json').read_text())['templates']
        coverage = json.loads((root / 'ready_templates/sources/manifests/coverage.json').read_text())['workflows']
    except (ImportError, OSError, KeyError, ValueError):
        return []
    task_modes = {'text_to_image': 't2i', 'text_to_image_single_frame': 't2i', 'image_to_image': 'i2i', 'image_edit': 'edit',
                  'text_to_video': 't2v', 'image_to_video': 'i2v', 'first_last_frame': 'flf',
                  'first_last_frame_video': 'flf', 'text_to_audio_song': 'music',
                  'text_to_speech_custom_voice': 'tts', 'text_to_speech_voice_clone': 'tts',
                  'text_to_speech_voice_design': 'tts'}
    from vibecomfy.entries import LOADER_ROLES
    from vibecomfy.model_assets import _referenced_model_values
    from vibecomfy.registry.ready import _discover_ready_templates, workflow_from_ready
    from astrid.core.model_catalog.registry import ModelRegistry
    discovery = _discover_ready_templates(include_dynamic=False, include_json_references=False)
    catalog_by_template = {}
    for model in ModelRegistry.load_default().list_all(include_closed=True):
        for mode, spec in model.modes.items():
            local = spec.backends.get('local')
            if local and local.template:
                catalog_by_template.setdefault((local.template, mode), model.id)
    by_template: dict[str, list[dict[str, Any]]] = {}
    for item in coverage:
        template = item.get('ready_template')
        if isinstance(template, str):
            by_template.setdefault(template, []).append(item)
    rows = []
    for item in index:
        if entry_id is not None and item['id'] != entry_id:
            continue
        candidates = by_template.get(item['id'], [])
        kinds = {row.get('media') for row in candidates if row.get('media') in {'image', 'video', 'audio'}}
        modes = {task_modes[row['task']] for row in candidates if row.get('task') in task_modes}
        # Output declarations are stronger than a directory or broad capability.
        for out in item.get('public_outputs', []):
            value = str(out.get('output_type', '')).lower()
            if 'video' in value or 'vhs_' in value: kinds.add('video')
            elif 'audio' in value: kinds.add('audio')
            elif 'image' in value: kinds.add('image')
        kind = next(iter(kinds)) if len(kinds) == 1 else None
        ambiguity = None if kind and modes else 'kind or mode requires review'
        try:
            workflow = workflow_from_ready(item['id'], _discovery=discovery)
            needs = [{**ref, 'role': LOADER_ROLES.get(ref['class_type']),
                      **({'ambiguity': 'unknown loader role'} if ref['class_type'] not in LOADER_ROLES else {})}
                     for ref in _referenced_model_values(workflow)]
            binds = {name: name for name in workflow.inputs}
        except Exception:
            needs, binds = [], {}
            ambiguity = 'ready workflow metadata could not be loaded'
        registry_ids = {catalog_by_template[(item['id'], mode)] for mode in modes
                        if (item['id'], mode) in catalog_by_template}
        rows.append({'id': item['id'], 'kind': kind, 'modes': sorted(modes),
                     'bundle': str((root / item['path']).resolve()), 'needs': needs, 'binds': binds,
                     'defaults': {}, 'inputs': item.get('public_inputs', []), 'source': 'ready-template',
                     'requirements': {'custom_nodes': item.get('custom_nodes', []), 'models': needs},
                     'registry_id': next(iter(registry_ids)) if len(registry_ids) == 1 else None,
                     'ambiguity': ambiguity})
    return rows


@lru_cache(maxsize=1)
def ready_entries() -> list[dict[str, Any]]:
    return _ready_entries_for()


def action_entries(discovery: Any | None = None) -> list[dict[str, Any]]:
    """Project action entry declarations and their authoritative input ports."""
    if discovery is None:
        from astrid.core.pack.discovery import reuse_pack_projection
        return reuse_pack_projection(_discover_action_entries)
    rows = []
    for capability in discovery.actions:
        definition = getattr(capability, "definition", {})
        metadata = definition.get("metadata", {}) if isinstance(definition, dict) else {}
        declaration = metadata.get("generation_entry") if isinstance(metadata, dict) else None
        if not isinstance(declaration, dict):
            continue
        names = declaration.get("inputs")
        ports = {port.name: port for port in capability.inputs}
        local_files = declaration.get("producer_local_inputs", [])
        if (not isinstance(declaration.get("id"), str) or not declaration["id"]
                or declaration.get("kind") not in {"image", "video", "audio"}
                or not isinstance(declaration.get("modes"), list)
                or not declaration["modes"]
                or not all(isinstance(mode, str) and mode for mode in declaration["modes"])
                or not isinstance(names, list) or not names
                or any(not isinstance(name, str) or name not in ports for name in names)
                or len(names) != len(set(names))
                or not isinstance(local_files, list)
                or any(not isinstance(name, str) or name not in names
                       or _input_type(ports[name].type) != "file" for name in local_files)
                or len(local_files) != len(set(local_files))):
            raise ValueError(f"invalid generation entry declaration on {capability.id!r}")
        rows.append({"id": declaration["id"], "kind": declaration["kind"],
                     "modes": list(declaration["modes"]), "action": capability.id,
                     "inputs": [{"name": ports[name].name, "type": ports[name].type,
                                 "required": ports[name].required,
                                 "description": ports[name].description,
                                 **({"file_custody": "producer_local"} if name in local_files else {}),
                                 **_declared_default({"type": ports[name].type,
                                     "default": getattr(ports[name], "default", None)})} for name in names],
                     "requirements": declaration.get("requirements"),
                     "isolation_requirements": definition.get("isolation", {}).get("requirements", [])})
    return rows


def _discover_action_entries() -> list[dict[str, Any]]:
    from astrid.sdk.invocation import discover
    return action_entries(discover())


def bundle_path(entry: dict[str, Any]) -> Path:
    path = Path(entry['bundle']).expanduser()
    return path / 'workflow.py' if path.is_dir() else path


def find_entry(entry_id: str) -> dict[str, Any] | None:
    matches = [entry for entry in user_entries() if entry['id'] == entry_id]
    actions = [entry for entry in action_entries() if entry['id'] == entry_id]
    if actions:
        ready = _ready_entries_for(entry_id)
        from astrid.core.model_catalog.registry import ModelRegistry
        try:
            ModelRegistry.load_default().get(entry_id)
        except KeyError:
            catalog_match = False
        else:
            catalog_match = True
        if len(actions) != 1 or matches or ready or catalog_match:
            raise ValueError(f'ambiguous generation entry {entry_id!r}')
        return actions[0]
    if len(matches) > 1:
        raise ValueError(f'ambiguous workflow entry {entry_id!r}')
    if matches:
        return {**matches[0], 'source': 'user-entry'}
    return next(iter(_ready_entries_for(entry_id)), None)


def _input_type(value: Any) -> str:
    """Normalize declaration spellings, without inferring a missing contract."""
    token = str(value or 'unknown').lower()
    return {'int': 'integer', 'float': 'number', 'bool': 'boolean',
            'str': 'string', 'text': 'string', 'path': 'file',
            'choice': 'string', 'combo': 'string'}.get(token, token)


def _declared_default(port: dict[str, Any]) -> dict[str, Any]:
    value = port.get('default')
    if value is None or value == '':
        return {}
    kind = _input_type(port.get('type'))
    if isinstance(value, str):
        try:
            if kind == 'integer':
                value = int(value)
            elif kind == 'number':
                value = float(value)
            elif kind == 'boolean' and value.lower() in {'true', 'false'}:
                value = value.lower() == 'true'
        except ValueError:
            return {}
    return {'default': value}


def _catalog_inputs(kind: str, spec: Any, mode: str | None = None) -> list[dict[str, Any]]:
    """Mode membership comes from the catalog; scalar metadata from action ports."""
    import yaml
    from astrid.core.pack.loader import packs_root
    declarations = yaml.safe_load((packs_root() / 'generation' / 'pack.yaml').read_text())
    actions = declarations['actions']
    primary = actions[f'generate_{kind}']['inputs']
    ports = {port['name']: port for action in actions.values()
             for port in action.get('inputs', [])}
    ports.update({port['name']: port for port in primary})
    result = []
    for name in spec.supports:
        port = ports.get(name, {})
        result.append({'name': name, 'type': _input_type(port.get('type')),
                       'required': name in spec.requires,
                       **_declared_default(port),
                       **{key: port[key] for key in ('description', 'choices', 'min', 'max') if key in port}})
    backend = spec.backends.get('local')
    if backend is not None and backend.template:
        from astrid.core.generation.backends.vibecomfy import _load_pinned_catalog_workflow
        try:
            workflow = _load_pinned_catalog_workflow(backend)
        except (ImportError, OSError, ValueError, KeyError):
            return result  # retained declared inputs remain useful offline
        fields = _workflow_inputs({'binds': {}, 'defaults': {}}, mode or 'workflow', workflow=workflow)
        for row in fields:
            if {row['name'], *row.get('aliases', ())} & set(spec.requires):
                row['required'] = True
                row['_explicit_required'] = True
        # Legacy count=1 is an admission control, not another writable widget.
        fields.extend({**row, '_hidden': True} for row in result if row['name'] == 'count')
        return fields
    return result


def _ready_ids() -> set[str]:
    """Read identities without building unrelated workflows."""
    try:
        import vibecomfy
        root = Path(vibecomfy.__file__).resolve().parent.parent
        return {item['id'] for item in json.loads((root / 'template_index.json').read_text())['templates']}
    except (ImportError, OSError, KeyError, ValueError):
        return set()


def _load_entry_workflow(entry: dict[str, Any]) -> Any:
    from vibecomfy.security.provenance import Provenance
    from vibecomfy.workflow_bundle import load_bundle
    from vibecomfy.schema import get_authoring_schema_provider
    return load_bundle(bundle_path(entry), trust=Provenance.USER_CONFIRMED,
                       schema_provider=get_authoring_schema_provider(on_demand_schemas=False)).workflow


def _entry_bindings(workflow: Any, binds: dict[str, Any] | None, mode: str,
                    param_map: dict[str, str] | None = None) -> dict[str, Any]:
    """Use the executor's existing canonical-to-public workflow translation."""
    from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
    mapping = dict(binds if binds is not None else {name: name for name in workflow.inputs})
    translation = param_map or VibeComfyBackend.DEFAULT_PARAM_MAP.get(mode, {})
    for canonical, public in translation.items():
        if canonical not in mapping and public in mapping:
            mapping[canonical] = mapping[public]
    return mapping


def _workflow_inputs(entry: dict[str, Any], mode: str, *, workflow: Any) -> list[dict[str, Any]]:
    # Registered names are authoritative. Entry/common spellings are accepted
    # internally, but never add alias metadata to the public inventory.
    mapping = _entry_bindings(workflow, {**{name: name for name in workflow.inputs},
                                       **entry.get('binds', {})}, mode)
    mapping.update(_writable_node_bindings(workflow))
    fields: dict[tuple[str, str], dict[str, Any]] = {}
    defaults = entry.get('defaults', {})
    for name, target in mapping.items():
        candidate = workflow._resolve_input(target) if isinstance(target, str) else None
        if candidate is not None and _linked(workflow, candidate.node_id, candidate.field):
            continue
        public = _resolve_binding(workflow, name, target)
        spec = workflow.inputs[public]
        identity = (spec.node_id, spec.field)
        if identity in fields:
            fields[identity].setdefault('aliases', []).append(name)
            continue
        aliases = sorted(set((public, *getattr(spec, 'aliases', ()))) - {name})
        kind = _input_type(spec.type)
        node = workflow.nodes.get(str(spec.node_id))
        # IMAGE describes the media, while LoadImage.image's wire value is a
        # filename. Do not infer a scalar type for arbitrary IMAGE sockets.
        if (kind == 'image' and getattr(node, 'class_type', None) == 'LoadImage'
                and spec.field == 'image'):
            kind = 'file'
        if kind == 'unknown':
            # InputSpec defaults and the bound widget are declared workflow
            # values, so their primitive shape is an authoritative fallback.
            for value in (defaults.get(name), spec.default, spec.value):
                primitive = {bool: 'boolean', int: 'integer', float: 'number', str: 'string'}.get(type(value))
                if primitive is not None:
                    kind = primitive
                    break
        row = {'name': name, 'type': kind, 'required': spec.required,
               'default': defaults.get(name, spec.default), '_binding': identity}
        for spelling, binding in mapping.items():
            if binding == target and spelling in defaults:
                row['default'] = defaults[spelling]
        if aliases:
            row['aliases'] = aliases
        if spec.media_semantics:
            row['media_semantics'] = spec.media_semantics
        limits = getattr(spec, 'range', None)
        if isinstance(limits, Mapping):
            row.update({key: limits[key] for key in ('choices', 'min', 'max') if key in limits})
        elif limits is not None:
            row['range'] = limits
        fields[identity] = row
    # Canonical metadata must be JSON representable; report unsupported values
    # rather than silently manufacturing a schema from Python repr strings.
    result = list(fields.values())
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('workflow input metadata is not JSON representable') from exc
    return result


def _schema_provider():
    from vibecomfy.schema import get_authoring_schema_provider
    return get_authoring_schema_provider(on_demand_schemas=False)


def _linked(workflow: Any, node_id: str, field: str) -> bool:
    return any(str(edge.to_node) == str(node_id) and edge.to_input == field
               for edge in workflow.edges)


def _writable_node_bindings(workflow: Any, provider: Any = None) -> dict[str, str]:
    """Expose schema-backed literals through Vibe's existing validated binding.

    Missing schema leaves registered inputs usable; it grants no additional
    writable fields. No native imports, target probes or schema installation.
    """
    provider = provider or _schema_provider()
    registered = {(str(spec.node_id), spec.field) for spec in workflow.inputs.values()}
    bindings = {}
    for node_id, node in workflow.nodes.items():
        schema = provider.get_schema(node.class_type)
        if schema is None:
            continue
        for field, spec in schema.inputs.items():
            if (field.startswith('_') or _linked(workflow, node_id, field)
                    or field not in node.inputs and field not in node.widgets):
                continue
            kind = _input_type(spec.type)
            if kind not in {'string', 'integer', 'number', 'boolean', 'json', 'array', 'object'}:
                continue
            name = f'node:{node_id}.{field}'
            # Also accept the exact node spelling of a registered public input;
            # it resolves to the same identity for conflict rejection.
            if (str(node_id), field) in registered:
                continue
            value = node.inputs.get(field, node.widgets.get(field))
            limits = {key: getattr(spec, key) for key in ('choices', 'min', 'max')
                      if getattr(spec, key, None) is not None}
            workflow.register_input(name, str(node_id), field, value,
                                    type=spec.type, default=spec.default, range=limits)
            bindings[name] = name
    return bindings


def input_inventory(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The single public row shape; native constraints remain internal."""
    return [{'name': field['name'], 'type': field['type'], 'default': field.get('default')}
            for field in fields if not field.get('_hidden')]


def public_description(description: dict[str, Any]) -> dict[str, Any]:
    return {**description, 'inputs': input_inventory(description['inputs']),
            **({'mode_descriptions': {mode: {**row, 'inputs': input_inventory(row['inputs'])}
                                      for mode, row in description['mode_descriptions'].items()}}
               if 'mode_descriptions' in description else {})}


def merge_input_values(*sources: Mapping[str, Any], assignments=()) -> dict[str, Any]:
    """Merge supplied controls without last-wins behavior, before admission."""
    result = {}
    for source in sources:
        if not isinstance(source, Mapping):
            raise ValueError('inputs must be an object')
        for name, value in source.items():
            if not isinstance(name, str) or not name:
                raise ValueError('input names must be nonempty strings')
            if name in result:
                raise ValueError(f'duplicate input values: {name}')
            result[name] = value
    for assignment in assignments:
        if not isinstance(assignment, str) or '=' not in assignment:
            raise ValueError('--input requires name=value')
        name, raw = assignment.split('=', 1)
        if not name:
            raise ValueError('--input requires a nonempty name')
        if name in result:
            raise ValueError(f'duplicate input values: {name}')
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        result[name] = value
    return result


def describe_entry(entry_id: str, mode: str | None = None, *, registry: Any = None,
                   _native: bool = False) -> dict[str, Any]:
    """Project the shared entry contract using local declarations only.

    No target inventory, readiness checks, Runtime client, provider or installer
    is involved. Multi-mode callers select a mode or inspect mode_descriptions.
    """
    if registry is None:
        from astrid.core.model_catalog.registry import ModelRegistry
        registry = ModelRegistry.load_default()
    try:
        catalog = registry.get(entry_id)
    except KeyError:
        catalog = None
    users = [row for row in user_entries() if row['id'] == entry_id]
    actions = [row for row in action_entries() if row['id'] == entry_id]
    if len(users) > 1 or len(actions) > 1 or (actions and (users or catalog or entry_id in _ready_ids())):
        raise ValueError(f'ambiguous generation entry {entry_id!r}')
    if actions:
        entry = actions[0]
        base = {key: entry[key] for key in ('id', 'kind', 'action', 'requirements', 'isolation_requirements') if key in entry}
        base['source'] = 'action'
        base['engine'] = 'comfyui' if entry['action'] == 'h3_av.transform' else None
        modes = entry['modes']
        schemas = {name: [{**port, 'type': _input_type(port.get('type'))} for port in entry['inputs']]
                   for name in modes}
    elif users or catalog is None:
        matches = users or _ready_entries_for(entry_id)
        if len(matches) != 1:
            raise ValueError(f'unknown generation entry {entry_id!r}')
        entry = {**matches[0], 'source': 'user-entry' if users else 'ready-template'}
        modes = entry['modes']
        if not modes or entry.get('kind') not in {'image', 'video', 'audio'}:
            raise ValueError(f'entry {entry_id!r} kind or mode requires review')
        base = {'id': entry_id, 'kind': entry['kind'], 'source': 'workflow', 'engine': 'comfyui'}
        workflow = _load_entry_workflow(entry)
        schemas = {name: _workflow_inputs(entry, name, workflow=workflow) for name in modes}
    else:
        modes = list(catalog.modes)
        base = {'id': entry_id, 'kind': catalog.modality, 'source': 'catalog',
                'engine': 'comfyui' if any('local' in spec.backends for spec in catalog.modes.values()) else None}
        schemas = {name: _catalog_inputs(catalog.modality, spec, name) for name, spec in catalog.modes.items()}
    if mode is not None and mode not in modes:
        raise ValueError(f'entry {entry_id!r} does not support mode {mode!r}')
    selected = mode or (modes[0] if len(modes) == 1 else None)
    description = {**base, 'modes': list(modes), 'mode': selected,
            'inputs': schemas[selected] if selected is not None else [],
            'mode_descriptions': {name: {'mode': name, 'inputs': fields} for name, fields in schemas.items()}}
    from astrid.core.generation.availability import (
        availability_projection, entry_availability, model_availability,
    )
    for name, row in description['mode_descriptions'].items():
        if base['source'] == 'catalog':
            backends = {backend: availability_projection(model_availability(entry_id, name, backend))
                        for backend in catalog.modes[name].backends}
            row['backends'] = backends
            projection = next((item for item in backends.values() if item['availability'] == 'available'),
                              next(iter(backends.values()), availability_projection({'available': True})))
        else:
            projection = availability_projection(entry_availability(entry, name))
        row.update(projection)
    rows = description['mode_descriptions']
    projection = rows[selected] if selected is not None else next(
        (row for row in rows.values() if row['availability'] == 'available'),
        next(iter(rows.values()), availability_projection({'available': True})))
    description.update({key: projection[key] for key in ('availability', 'reason', 'fix')})
    return description if _native else public_description(description)


def describe_workflow(path: Path, mode: str | None = None, *, _native=False) -> dict[str, Any]:
    """Describe raw/canonical Comfy work through the same offline input path."""
    from astrid.packs.vibecomfy.production_engine import load_workflow_path
    from vibecomfy.entries import draft_entry
    path = path.expanduser()
    if path.is_dir():
        path = path / 'workflow.py'
    with TemporaryDirectory(prefix='astrid-workflow-describe-') as scratch:
        loaded = load_workflow_path(path, scratch, comfy_url='')
        workflow = loaded.resolved.workflow
        entry = draft_entry(workflow, 'workflow', bundle=str(path.resolve()))
        selected = mode or (entry['modes'][0] if len(entry['modes']) == 1 else 'workflow')
        fields = _workflow_inputs(entry, selected, workflow=workflow)
    result = {'id': 'workflow', 'engine': 'comfyui', 'source': 'workflow',
              'kind': entry['kind'], 'modes': [selected], 'mode': selected, 'inputs': fields}
    return result if _native else public_description(result)


def resolve_workflow_inputs(path: Path, inputs: Mapping[str, Any], *, mode=None):
    description = describe_workflow(path, mode=mode, _native=True)
    values = validate_inputs(description, _dimension_values(description, inputs))
    return public_description(description), values


def _dimension_values(description, inputs):
    values = dict(inputs)
    names = {port['name'] for port in description['inputs']}
    supplied = {'size', 'resolution'} & values.keys()
    pair = {'width', 'height'} & values.keys()
    if len(supplied) > 1 or supplied and pair:
        raise ValueError('duplicate/conflicting dimensions: size/resolution and width/height')
    if pair:
        if pair != {'width', 'height'}:
            raise ValueError('width and height must be supplied together')
        if any(type(values[name]) is not int or values[name] <= 0 for name in pair):
            raise ValueError('width and height require positive integers')
        if not pair & names and {'size', 'resolution'} & names:
            values['size' if 'size' in names else 'resolution'] = f"{values.pop('width')}x{values.pop('height')}"
    elif supplied and {'width', 'height'} <= names:
        from astrid.core.generation.backends.base import parse_dimension_pair
        value = values.pop(next(iter(supplied)))
        dimensions = parse_dimension_pair(value) if isinstance(value, str) else None
        if dimensions is None or any(dimension <= 0 for dimension in dimensions):
            raise ValueError('invalid dimensions')
        values.update(width=dimensions[0], height=dimensions[1])
    return values


def validate_inputs(description: dict[str, Any], inputs: Mapping[str, Any], *, required: bool = True) -> dict[str, Any]:
    """Validate declared fields before SDK custody or execution admission."""
    if not isinstance(inputs, Mapping) or any(not isinstance(key, str) for key in inputs):
        raise ValueError('entry inputs must be an object with string keys')
    ports = {port['name']: port for port in description['inputs']}
    aliases: dict[str, str] = {}
    for port in ports.values():
        for alias in port.get('aliases', []):
            if alias in ports and alias != port['name'] or alias in aliases and aliases[alias] != port['name']:
                raise ValueError(f'ambiguous entry input alias {alias!r}')
            aliases[alias] = port['name']
    values: dict[str, Any] = {}
    bound_identities: set[tuple[str, str]] = set()
    for supplied, value in inputs.items():
        name = aliases.get(supplied, supplied)
        if name.startswith('node:') and name not in ports:
            identity = tuple(name[5:].split('.', 1))
            matches = [key for key, port in ports.items() if tuple(port.get('_binding', ())) == identity]
            if len(matches) == 1:
                name = matches[0]
        if name not in ports:
            raise ValueError('unsupported entry inputs: ' + supplied)
        if name in values:
            raise ValueError(f'duplicate/conflicting entry inputs for {name!r}')
        port = ports[name]
        identity = tuple(port.get('_binding', ()))
        if identity and identity in bound_identities:
            raise ValueError(f'duplicate/conflicting entry inputs for {supplied!r}')
        if identity:
            bound_identities.add(identity)
        kind = _input_type(port.get('type'))
        if value is None:
            raise ValueError(f'entry input {name!r} must not be null')
        if (kind == 'file' and port.get('file_custody') != 'producer_local'
                and (description.get('source') in {'action', 'action-entry'} or 'action' in description)):
            from astrid.core.execution.managed_inputs import managed_file_digest
            managed_file_digest(value, name)
        elif kind == 'file' and port.get('file_custody') == 'producer_local':
            if isinstance(value, Path):
                value = str(value)
            if not isinstance(value, str):
                raise ValueError(f'entry input {name!r} requires a string producer-local file path')
            from astrid.core.execution.managed_inputs import managed_file_digest
            try:
                managed_file_digest(value, name)
            except ValueError:
                managed = value.startswith('sha256:')
            else:
                managed = True
            if managed:
                raise ValueError(f'entry input {name!r} requires a string producer-local file path, not a managed digest')
        elif kind in {'string', 'file'}:
            if not isinstance(value, str):
                raise ValueError(f'entry input {name!r} requires a string')
        elif kind == 'integer':
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f'entry input {name!r} requires an integer')
        elif kind == 'number':
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or isinstance(value, float) and not math.isfinite(value)):
                raise ValueError(f'entry input {name!r} requires a finite number')
        elif kind == 'boolean':
            if not isinstance(value, bool):
                raise ValueError(f'entry input {name!r} requires a boolean')
        elif kind in {'json', 'object', 'array'}:
            if kind == 'object' and not isinstance(value, Mapping) or kind == 'array' and not isinstance(value, list):
                raise ValueError(f'entry input {name!r} requires {kind}')
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(f'entry input {name!r} requires JSON') from exc
        else:
            raise ValueError(f'entry input {name!r} has no declared scalar type ({kind})')
        if 'choices' in port and value not in port['choices']:
            raise ValueError(f'entry input {name!r} must be one of {port["choices"]!r}')
        for bound, violates in (('min', lambda limit: value < limit), ('max', lambda limit: value > limit)):
            if bound in port and violates(port[bound]):
                raise ValueError(f'entry input {name!r} violates {bound}={port[bound]}')
        values[name] = value
    if required:
        missing = [name for name, port in ports.items() if port.get('required')
                   and (name in values and (values[name] is None or isinstance(values[name], str) and not values[name].strip())
                        or name not in values and (port.get('_explicit_required') or port.get('default') in (None, '')))]
        if missing:
            raise ValueError('missing required entry inputs: ' + ', '.join(missing))
    return values


def resolve_entry_inputs(entry_id: str, inputs: Mapping[str, Any], *, mode: str | None = None,
                         kind: str | None = None, registry: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
    description = describe_entry(entry_id, mode=mode, registry=registry, _native=True)
    if kind is not None and description['kind'] != kind:
        raise ValueError(f'entry {entry_id!r} does not support {kind}')
    if description['mode'] is None:
        supported = description['modes']
        if inputs.get('image_ref') and inputs.get('image_end_ref'):
            candidates = ['flf']
        elif inputs.get('video_ref'):
            candidates = ['v2v']
        elif inputs.get('image_ref'):
            candidates = ['i2v'] if description['kind'] == 'video' else ['i2i', 'edit']
        else:
            candidates = {'image': ['t2i'], 'video': ['t2v'], 'audio': ['music']}[description['kind']]
        selected = next((name for name in candidates if name in supported), None)
        if selected is None:
            raise ValueError(f'entry {entry_id!r} requires an explicit mode')
        description = {**description, **description['mode_descriptions'][selected]}
    values = _dimension_values(description, inputs)
    validation = description
    ports = {port['name']: port for port in description['inputs']}
    primary = ('request', 'asset_map')
    if (description.get('action') == 'h3_av.transform'
            and all(ports.get(name, {}).get('file_custody') == 'producer_local'
                    for name in primary)):
        # Older H3 callers supplied managed objects. Validate a complete managed
        # pair at its original boundary, without reinterpreting either object as
        # a producer-local path or admitting it through the new producer route.
        from astrid.core.execution.managed_inputs import managed_file_digest
        try:
            for name in primary:
                managed_file_digest(values.get(name), name)
        except ValueError:
            pass
        else:
            validation = {**description, 'inputs': [
                {key: value for key, value in port.items() if key != 'file_custody'}
                if port['name'] in primary else port for port in description['inputs']]}
            description = {**description, 'runtime_parent_required': True}
    return public_description(description), validate_inputs(validation, values)


def infer_kind(workflow: Any) -> str:
    from vibecomfy.entries import infer_output_kind
    return infer_output_kind(workflow)


def _resolve_binding(workflow: Any, name: str, target: Any) -> str:
    if isinstance(target, dict):
        target = f"{target['node']}.{target['field']}"
    if isinstance(target, str) and target.startswith('node:'):
        target = target[5:]
    resolved = workflow._resolve_input(target)
    if resolved is not None:
        if _linked(workflow, resolved.node_id, resolved.field):
            raise ValueError(f'entry input {name!r} targets a graph link')
        return resolved.name
    if '.' not in target:
        raise ValueError(f'entry binding {name!r} refers to unknown public input {target!r}')
    node_id, field = target.split('.', 1)
    if _linked(workflow, node_id, field):
        raise ValueError(f'entry input {name!r} targets a graph link')
    matches = [item.name for item in workflow.inputs.values() if item.node_id == node_id and item.field == field]
    if len(matches) > 1:
        raise ValueError(f'entry binding {name!r} has ambiguous target {target!r}')
    if matches:
        return matches[0]
    from vibecomfy.entries import media_loader_kind
    node = workflow.nodes.get(node_id)
    media = media_loader_kind(node.class_type, field) if node is not None else None
    schema = _schema_provider().get_schema(node.class_type) if node is not None else None
    spec = schema.inputs.get(field) if schema is not None else None
    value = node.inputs.get(field, node.widgets.get(field)) if node is not None else None
    limits = {key: getattr(spec, key) for key in ('choices', 'min', 'max')
              if getattr(spec, key, None) is not None}
    workflow.register_input(name, node_id, field, value,
                            type=getattr(spec, 'type', None), default=value,
                            range=limits, media_semantics=media)
    return name


def prepare(path: Path, *, target_client: Any, mode: str, binds: dict[str, Any] | None,
            values: dict[str, Any], defaults: dict[str, Any] | None = None,
            param_map: dict[str, str] | None = None):
    """Load a canonical workflow and bind controls with its public InputSpecs."""
    from astrid.packs.vibecomfy.production_engine import load_workflow_path
    with TemporaryDirectory(prefix="astrid-entry-inspect-") as scratch:
        loaded = load_workflow_path(path, scratch, comfy_url=target_client.target.url if target_client else "")
        workflow = loaded.resolved.workflow
        return apply_workflow_inputs(workflow, mode=mode, binds=binds, values=values,
                                     defaults=defaults, param_map=param_map)


def apply_workflow_inputs(workflow: Any, *, mode: str, values: dict[str, Any],
                          binds=None, defaults=None, param_map=None):
    """One validated native binding path for catalog, entry and raw workflows."""
    kind = infer_kind(workflow)
    mapping = _entry_bindings(workflow, {**{name: name for name in workflow.inputs}, **(binds or {})}, mode, param_map)
    mapping.update(_writable_node_bindings(workflow))
    fields = _workflow_inputs({'binds': {}, 'defaults': {}}, mode, workflow=workflow)
    values = _dimension_values({'inputs': fields}, values)
    requested = {**(defaults or {}), **values}
    dimensions = requested.pop("size", requested.pop("resolution", None))
    if dimensions is not None:
        from astrid.core.generation.backends.base import parse_dimension_pair
        pair = parse_dimension_pair(str(dimensions))
        if pair is None:
            raise ValueError("invalid dimensions")
        requested.update(width=pair[0], height=pair[1])
    requested.pop("count", None)
    applied, unbound, assets = {}, [], {}
    explicit_bindings = set()
    for name, value in requested.items():
        target = mapping.get(name)
        if target is None and name.startswith('node:'):
            target = name[5:]
            node_id, _, field = target.partition('.')
            known = {(str(spec.node_id), spec.field) for spec in workflow.inputs.values()}
            if (node_id, field) not in known:
                raise ValueError(f'unknown or unwritable input {name!r}')
        if target is None:
            unbound.append(name)
            continue
        public = _resolve_binding(workflow, name, target)
        identity = (workflow.inputs[public].node_id, workflow.inputs[public].field)
        if name in values:
            if identity in explicit_bindings:
                raise ValueError(f'duplicate/conflicting entry inputs for {name!r}')
            explicit_bindings.add(identity)
        # A supplied alias overrides a default for that same binding.
        elif identity in explicit_bindings:
            continue
        validate_inputs({'inputs': fields}, {public: value}, required=False)
        applied[public] = value
        if getattr(workflow.inputs[public], "media_semantics", None) in {"image", "video", "audio", "mask"}:
            assets[public] = Path(value)
        else:
            workflow.set_input(public, value)
    return workflow, {"kind": kind, "bindings": applied, "unbound": sorted(unbound)}, assets


def target_schema(entry: dict[str, Any], workflow: Any, client: Any):
    """Check the selected target's node and exact loader inventory."""
    for need in entry.get("needs", []):
        if need.get("ambiguity"):
            raise ValueError(f"entry requires review: {need['ambiguity']}")
    if client is None:
        return None
    from astrid.packs.runpod.generation_owner import RunPodPlanningTarget
    if isinstance(client, RunPodPlanningTarget):
        client.validate_workflow(entry, workflow)
        return None  # Cold planning carries no live-target approval witness.
    from types import SimpleNamespace
    from astrid.core.generation.backends.comfy_target import _resolve_roles
    from astrid.core.generation.backends.comfy_schema import TargetSchemaProvider
    observed = client.object_info()
    if not isinstance(observed, dict):
        raise ValueError("target model inventory is malformed")
    from astrid.core.generation.preflight import GenerationReadinessError, readiness_envelope
    issues = []
    for class_type in sorted({node.class_type for node in workflow.runtime_nodes().values()} - observed.keys()):
        issues.append({'code': 'missing_dependency', 'severity': 'error',
                       'message': f'target is missing node {class_type!r}',
                       'details': {'engine_code': 'comfyui.missing_node', 'node': class_type, 'bytes': None}})
    roles = {}
    needs_by_key = {}
    for index, need in enumerate(entry.get("needs", [])):
        node_class, field, value = need.get("class_type"), need.get("field"), need.get("value")
        if not (node_class and field and value):
            continue
        key = str(index)
        roles[key] = SimpleNamespace(node_class=node_class, input=field, names=(value,))
        needs_by_key[key] = need
    _, failures = _resolve_roles(SimpleNamespace(model_roles=roles), observed)
    for key in failures:
        need = needs_by_key[key]
        if need['class_type'] not in observed:
            continue  # the missing node already explains this observation
        issues.append({'code': 'missing_dependency', 'severity': 'error',
                       'message': f"target is missing {need.get('role') or 'model'} {need['value']!r} for {need['class_type']}.{need['field']}",
                       'details': {'engine_code': 'comfyui.models_missing', 'model': need['value'],
                                   'node': need['class_type'], 'field': need['field'], 'bytes': need.get('bytes')}})
    if issues:
        raise GenerationReadinessError(readiness_envelope(entry={
            'id': entry.get('id'), 'engine': 'comfyui',
            'inputs': input_inventory(_workflow_inputs(entry, entry.get('mode', 'workflow'), workflow=workflow))},
            target=getattr(client, 'generation_target', None), issues=issues,
            fixes=[{'argv': ['astrid', 'generate', 'describe', '--entry', entry.get('id', 'workflow')],
                    'scope': 'read_only', 'details': {'action': 'Provide the listed nodes/models on this selected target using its supported installer.'}}]))
    return TargetSchemaProvider.from_object_info(observed, server_url=client.http_origin,
        model_availability="target", refresh_object_info=client.object_info)
