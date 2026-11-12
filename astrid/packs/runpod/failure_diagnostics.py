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
# The submitted graph is the controller's own file, read locally and never sent
# anywhere; NAME_RE and MAX_GRAPH_NAMES still bound what can come out of it.
MAX_GRAPH_BYTES = 8 * 1024 * 1024
MAX_GRAPH_NODE_IDS = 4096
CODES = {
    'placement_refused': 'qualification',
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
    'comfy_execution_error': 'execution',
    'executor_failed': 'execution',
    'task_failed': 'execution',
    'history_prompt_mismatch': 'execution',
    'missing_structured_outputs': 'execution',
    'declared_final_output_missing': 'execution',
    'declared_final_output_cardinality': 'execution',
    'declared_final_output_empty_descriptor': 'execution',
    'declared_final_output_not_video': 'execution',
    'declared_final_output_empty': 'execution',
    'final_media_missing': 'execution',
    'final_media_unverified': 'execution',
    'final_video_stream_missing': 'execution',
    'final_audio_stream_missing': 'execution',
    'final_media_duration_too_short': 'execution',
    'final_media_width_mismatch': 'execution',
    'final_media_height_mismatch': 'execution',
    'unsafe_comfy_output_descriptor': 'execution',
    'comfy_node_errors': 'execution',
    'comfy_errors': 'execution',
    'comfy_execution_errors': 'execution',
    'comfy_ignored_nodes': 'execution',
    'comfy_ignored_outputs': 'execution',
    'comfy_rejected_nodes': 'execution',

}
FACTS = ('node_id', 'node_class', 'field', 'constraint', 'exception_type', 'oom')
EXCEPTION_TYPES = frozenset({
    'OutOfMemoryError', 'RuntimeError', 'ValueError', 'KeyError', 'TypeError',
    'IndexError', 'FileNotFoundError', 'ModuleNotFoundError', 'ImportError',
    'AssertionError', 'AttributeError', 'NotImplementedError', 'MemoryError',
    'OSError', 'OverflowError', 'ZeroDivisionError', 'TimeoutError',
})
FACT_ALLOW_BY_ENGINE = {
    'comfyui': {
        'node_class': frozenset({
            'KSampler', 'CLIPTextEncode', 'SaveImage', 'LoadImage', 'EmptyLatentImage',
            'CheckpointLoaderSimple', 'VAEDecode', 'VAEEncode', 'LoadAudio',
            'SaveAudio', 'TextToSpeech', 'ModelLoader',
        }),
    },
}
FACT_ALLOW = {
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
# Graph-authorised names (see graph_facts): identifier-shaped and short, so a
# sentence, path, URL or key-like secret can never qualify as a "name".
NAME_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,63}')
MAX_GRAPH_NAMES = 2048
EXCEPTIONS = frozenset({
    'MachineError', 'MachineRunError', 'MachineRunCancelled', 'RemotePlacementError',
    'RunPodWorkerOwnerError', 'TargetObservationError', 'AcquisitionUnresolvedError',
    'PreparedWorkerError', 'PreparationDeadlineExceeded', 'DigestMismatch',
    'CustodyChanged', 'TimeoutError', 'ValueError', 'RuntimeError', 'OSError',
    'ConnectionError', 'PermissionError', 'FileNotFoundError', 'IntegrityError',
    'DeadlineExceeded', 'UnknownError',
    'PaidSelectionError', 'RunPodSetupRequiredError', 'OfferObservationError',
    'CapacityExhaustedError', 'CapacityWaitTimeout',
    'ManagedSourcesError', 'WorkerPreparationError', 'PreparationJournalError',
    'CapacityWaitCancelled',
    'AllocationRejected', 'AttachmentPending', 'BuildError', 'CapacityUnavailable',
    'InvalidPodField', 'ManagedSessionError', 'ResetError', 'StagingEvidenceStaleError',
    'WorkerOwnerConflict', 'WorkerProcessError', 'BoundedObserverUnreaped', 'MachineCancelled',
})


def fact_allowlist(engine_id):
    """Return fixed safe fact values for one engine; unknown engines authorize none."""
    return FACT_ALLOW_BY_ENGINE.get(engine_id, {})


def _safe_fact(key, value, node_ids=None, node_names=frozenset(), *, engine_id=None):
    if key == 'exception_type':
        return value if isinstance(value, str) and value in EXCEPTION_TYPES else None
    if key == 'oom':
        return True if value is True else None
    if key == 'node_id':
        if type(value) is int and 0 <= value < 10**12:
            return str(value) if node_ids is None or str(value) in node_ids else None
        if isinstance(value, str) and re.fullmatch(r'[0-9]{1,12}', value) and int(value) < 10**12:
            canonical = str(int(value))
            return canonical if node_ids is None or canonical in node_ids else None
    elif isinstance(value, str) and value in (
            fact_allowlist(engine_id).get(key, ()) if key == 'node_class' else FACT_ALLOW.get(key, ())):
        return value
    elif (key in ('node_class', 'field') and isinstance(value, str) and value in node_names
          and NAME_RE.fullmatch(value)):
        # Names the submitted graph itself uses (never its values): the fixed
        # list above stays the floor, the graph widens it for this job only.
        return value
    return None


def fallback():
    return {'code': 'unknown', 'category': 'unknown'}


def host_summary(error, diagnostic=None, *, stage='execution', node_ids=frozenset(), node_names=frozenset(),
                 engine_id='comfyui'):
    """Journal only bounded facts; exception text and remote output are untrusted."""
    # TODO(engine-id): legacy callers do not carry an engine id yet.
    safe = summarize(diagnostic, node_ids=node_ids, node_names=node_names,
                     engine_id=engine_id) if isinstance(diagnostic, Mapping) else fallback()
    if isinstance(diagnostic, Mapping) and {'code', 'category'} <= set(diagnostic) <= {'code', 'category', *FACTS}:
        code = diagnostic.get('code')
        if isinstance(code, str) and code in CODES and diagnostic.get('category') == CODES[code]:
            safe = {'code': code, 'category': CODES[code]}
            safe.update({key: _safe_fact(key, diagnostic[key], node_ids, node_names, engine_id=engine_id)
                         for key in FACTS if key in diagnostic
                         and _safe_fact(key, diagnostic[key], node_ids, node_names, engine_id=engine_id) is not None})
    name = type(error).__name__ if error is not None else 'UnknownError'
    if name not in EXCEPTIONS:
        name = 'UnknownError'
    return {'exception': name, 'stage': stage if isinstance(stage, str) and stage in {'claim', 'preparation', 'start', 'poll', 'fetch',
            'execution', 'reset', 'cancel'} else 'execution', **safe}


def safe_stored_summary(value, *, node_ids=frozenset(), node_names=frozenset(), engine_id='comfyui'):
    """Revalidate a journal read before it reaches machine listing or CLI."""
    # TODO(engine-id): legacy persisted summaries do not carry an engine id.
    if not isinstance(value, Mapping):
        return fallback()
    diagnostic = {key: value[key] for key in ('code', 'category', *FACTS) if key in value}
    result = host_summary(None, diagnostic, stage=value.get('stage'), node_ids=node_ids,
                          node_names=node_names, engine_id=engine_id)
    name = value.get('exception')
    if isinstance(name, str) and name in EXCEPTIONS:
        result['exception'] = name
    return result


def summarize(value, *, node_ids=frozenset(), node_names=frozenset(), engine_id='comfyui'):
    """Project only a known error identifier; categories are locally defined."""
    # TODO(engine-id): legacy worker frames do not carry an engine id.
    if not isinstance(value, Mapping):
        return fallback()
    candidates = []
    # Follow only structured error containers; never traverse arbitrary values.
    # Limits also protect callers that provide cyclic or unusually wide objects.
    stack = [(value, 0, {})]
    visited = 0
    while stack and visited < 512:
        item, depth, inherited = stack.pop()
        visited += 1
        if not isinstance(item, Mapping) or depth > 12:
            continue
        facts = dict(inherited)
        for key in FACTS:
            fact = _safe_fact(key, item.get(key), node_ids, node_names, engine_id=engine_id)
            if fact is not None:
                facts[key] = fact
        details = item.get('details')
        local_facts = dict(facts)
        if isinstance(details, Mapping):
            for key in FACTS:
                fact = _safe_fact(key, details.get(key), node_ids, node_names, engine_id=engine_id)
                if fact is not None:
                    local_facts[key] = fact
        for key in ('engine_code', 'code'):
            candidates.append((depth, item.get(key), local_facts))
        if depth < 12:
            for key in ('details', 'result', 'error', 'engine_diagnostic', 'diagnostics', 'issues'):
                child = item.get(key)
                if isinstance(child, Mapping):
                    stack.append((child, depth + 1, facts))
                elif isinstance(child, list):
                    for entry in reversed(child[:128]):
                        if isinstance(entry, Mapping) and (key != 'issues' or entry.get('severity') == 'error'):
                            stack.append((entry, depth + 1, facts))
    for _, code, facts in sorted(candidates, key=lambda candidate: -candidate[0]):
        if isinstance(code, str) and code in CODES:
            return {'code': code, 'category': CODES[code], **facts}
    if any(isinstance(code, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]{0,127}', code)
           for _, code, _ in candidates):
        return {'code': 'generation_failed', 'category': 'execution'}
    return fallback()


def _graph_nodes(graph):
    """(id, node) pairs of a submitted graph: ComfyUI API format (``{id: {class_type,
    inputs}}``) or an editor-style ``{"nodes": {id: node} | [{id, ...}]}``."""
    if not isinstance(graph, Mapping):
        return []
    nodes = graph.get('nodes')
    if isinstance(nodes, Mapping):
        return list(nodes.items())
    if isinstance(nodes, list):
        pairs = [(node.get('id'), node) for node in nodes if isinstance(node, Mapping)]
        definitions = graph.get('definitions')
        subgraphs = definitions.get('subgraphs') if isinstance(definitions, Mapping) else None
        for sub in subgraphs if isinstance(subgraphs, list) else ():
            inner = sub.get('nodes') if isinstance(sub, Mapping) else None
            pairs.extend((node.get('id'), node) for node in inner if isinstance(node, Mapping)) \
                if isinstance(inner, list) else None
        return pairs
    return [(key, node) for key, node in graph.items() if isinstance(node, Mapping) and 'class_type' in node]


def _node_names(node):
    """Only structural names of one node: its class and its input NAMES, never a value."""
    names = []
    if not isinstance(node, Mapping):
        return names
    for key in ('class_type', 'type'):
        names.append(node.get(key))
    inputs = node.get('inputs')
    if isinstance(inputs, Mapping):
        names.extend(inputs.keys())
    elif isinstance(inputs, list):
        names.extend(item.get('name') for item in inputs if isinstance(item, Mapping))
    return names


def graph_facts(path):
    """``(node_ids, names)`` a submitted, bounded JSON graph authorizes.

    ``names`` holds the node class types and input names the graph itself
    uses, and only those that are identifier-shaped (``NAME_RE``). Values are
    never read, so prompt text and other input values cannot become names. An
    oversized or unreadable graph authorizes nothing (the fixed list applies).
    """
    if not isinstance(path, (str, os.PathLike)):
        return frozenset(), frozenset()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_GRAPH_BYTES:
                return frozenset(), frozenset()
            raw = stream.read(MAX_GRAPH_BYTES + 1)
        if len(raw) > MAX_GRAPH_BYTES:
            return frozenset(), frozenset()
        ids, names = set(), set()
        for node_id, node in _graph_nodes(json.loads(raw)):
            if len(ids) >= MAX_GRAPH_NODE_IDS:
                return frozenset(), frozenset()
            canonical = _safe_fact('node_id', node_id)
            if canonical is not None:
                ids.add(canonical)
            names.update(name for name in _node_names(node) if isinstance(name, str) and NAME_RE.fullmatch(name))
            if len(names) > MAX_GRAPH_NAMES:
                return frozenset(), frozenset()
        return frozenset(ids), frozenset(names)
    except (OSError, ValueError, TypeError, RecursionError):
        return frozenset(), frozenset()


def graph_node_ids(path):
    """Only structural IDs in a submitted, bounded JSON graph authorize node facts."""
    return graph_facts(path)[0]


def graph_names(path):
    return graph_facts(path)[1]


def stored_names(value):
    """Revalidate journaled names; anything malformed authorizes nothing."""
    if not isinstance(value, list) or len(value) > MAX_GRAPH_NAMES:
        return frozenset()
    if not all(isinstance(item, str) and NAME_RE.fullmatch(item) for item in value):
        return frozenset()
    return frozenset(value)


def stored_node_ids(value):
    if not isinstance(value, list) or len(value) > 256:
        return frozenset()
    ids = frozenset(_safe_fact('node_id', item) for item in value)
    return ids if None not in ids and len(ids) == len(value) else frozenset()


def _submitted_graph_facts(job_path):
    try:
        fd = os.open(job_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RESULT_BYTES:
                return frozenset(), frozenset()
            raw = stream.read(MAX_RESULT_BYTES + 1)
        job = json.loads(raw)
        source = job.get('execution', {}).get('source') if isinstance(job, Mapping) else None
        return graph_facts(source), (job.get('engine_id') if isinstance(job, Mapping) else None)
    except (OSError, ValueError, TypeError, RecursionError):
        return (frozenset(), frozenset()), None


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
        (ids, names), engine_id = _submitted_graph_facts(job_path) if job_path else ((frozenset(), frozenset()), None)
        # TODO(engine-id): older shipped job frames do not identify their engine.
        engine_id = engine_id or 'comfyui'
        return summarize(json.loads(raw), node_ids=ids, node_names=names, engine_id=engine_id)
    except (OSError, ValueError, TypeError, RecursionError):
        return fallback()


def parse_frame(stdout, *, node_ids=frozenset(), node_names=frozenset(), engine_id='comfyui'):
    # TODO(engine-id): legacy frame callers do not carry an engine id.
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
            facts = {key: _safe_fact(key, value[key], node_ids, node_names, engine_id=engine_id)
                     for key in FACTS if key in value
                     and _safe_fact(key, value[key], node_ids, node_names, engine_id=engine_id) is not None}
            return {'code': code, 'category': CODES[code], **facts}
    except (ValueError, TypeError, RecursionError):
        pass
    return fallback()


if __name__ == '__main__':
    print(MARK + json.dumps(read_result(sys.argv[1], sys.argv[2]) if len(sys.argv) == 3 else fallback(), sort_keys=True))
