"""Lifecycle attestations for fully verified managed workflow requirements.

A v4 record binds full verification and doctor to one retained native engine
handle, its random child generation and its supervisor launch token. There is
no file inventory. W3 live model-file checks and per-job validation always run.

The node residual is BROADER than W3's: other same-user writers under a live
engine (ComfyUI-Manager, custom nodes writing custom_nodes, runtime pip/installer
nodes, node-time downloads, and drift in imported dependencies) are accepted
residuals, not prevented or detected by this lifecycle witness. Isolation would
be needed to exclude those writers. models.trust_unchanged must be exactly True.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ._engine_target_files import _check, _TargetFiles
from .engine_contracts import CustodyUnresolved, EngineError

CLAIM_ID: ContextVar[str | None] = ContextVar('warm_attestation_claim', default=None)
SCHEMA = 'astrid.warm-attestation.v4'
# Enable only after the lifecycle acceptance matrix passes on this branch.
WARM_ATTESTATION_ENABLED = False
_SHA = re.compile(r'[0-9a-f]{64}\Z')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _receipt_identity(raw):
    stable = {key: value for key, value in raw.items() if key not in {'health', 'steps'}}
    stable['steps'] = [step for step in raw.get('steps', []) if step['kind'] not in {'start', 'health'}]
    return _digest(stable)


def requirement_digest(workflow, manifest):
    """Bind raw declaration values, preserving order/spelling; exclude prompt literals.

    In particular do not use runtime_requirements_from_workflow's normalized
    package names/versions as a replacement for the actual declaration bytes.
    """
    declarations = {name: getattr(workflow.requirements, name)
                    for name in ('models', 'custom_nodes', 'custom_node_refs',
                                 'missing_models', 'missing_nodes', 'unsupported', 'runtime')}
    return _digest({'models': manifest['models'], 'nodes': manifest['extra']['nodes'],
                    'declarations': declarations,
                    'classes': sorted({node.class_type for node in workflow.nodes.values()})})


def valid_lifecycle(value):
    from .engine_custody import _valid_lifecycle
    return _valid_lifecycle(value)


def attestation_deadline(deadline):
    now = time.monotonic()
    return now + min(10.0, max(0.0, (deadline - now) * 0.1))


def invalidate(root, *, deadline):
    from .engine_custody import _invalidate_warm
    _invalidate_warm(root, deadline=deadline)


def supervisor_incarnation(client, deadline):
    """Observe the authenticated live socket peer without spawning a supervisor."""
    from .engine_custody import _directory, _vibe_birth
    with _directory(client.control, create=False) as fd:
        marker = client._marker(fd)
        if marker is None or marker['identity'] != client.identity:
            raise ValueError('supervisor identity unavailable')
        channel = client._connect(fd, deadline, start=False)
        if channel is None:
            raise ValueError('supervisor absent')
        with channel:
            pid, uid, _gid = struct.unpack('3i', channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid() or marker['process']['pid'] != pid:
                raise ValueError('supervisor peer differs')
            return {'pid': pid, 'start_ticks': _vibe_birth(pid), 'uid': uid}


class WarmAttestation:
    def __init__(self, root, recipe, runner, claim_id, deadline, *, enabled, check=None, reuse=True):
        self.root, self.recipe, self.runner = Path(root), recipe, runner
        self.claim_id = claim_id
        self.enabled = (WARM_ATTESTATION_ENABLED is True and enabled is True
                        and (claim_id is None or isinstance(claim_id, str) and bool(claim_id)))
        self.job_deadline = deadline
        self.deadline = attestation_deadline(deadline) if self.enabled else deadline
        self.budget_remaining = max(0.0, self.deadline - time.monotonic()) if self.enabled else 0.0
        self._budget_depth = 0
        self.check = check or (lambda: None)
        self.files = _TargetFiles(str(self.root))
        self.path = str(self.root / '.astrid-engine/warm-attestation.json')
        self.saved, self.matched, self.facts = None, False, None
        if self.enabled and reuse:
            try:
                with self._budget():
                    self.check()
                    self._private_record()
                    saved = self.files.read_json(self.path, deadline=self.deadline)
                    self._validate(saved)
                    facts = self.probe()
                    if (facts is not None and self.identity(facts['lifecycle']) == saved['identity']
                            and facts['object_info_sha256'] == saved['object_info_sha256']):
                        self.saved, self.matched, self.facts = saved, True, facts
            except (OSError, ValueError, TypeError, KeyError, AttributeError, EngineError, struct.error):
                pass  # Optional observation failure is a miss, never mutation authority.

    @contextmanager
    def _budget(self):
        """Charge only attestation work, sharing one sub-budget across its phases."""
        if self._budget_depth:
            yield
            return
        started = time.monotonic()
        self.deadline = min(attestation_deadline(self.job_deadline), started + self.budget_remaining)
        self._budget_depth = 1
        try:
            _check(self.deadline)
            yield
        finally:
            self.budget_remaining = max(0.0, self.budget_remaining - (time.monotonic() - started))
            self._budget_depth = 0

    def _private_record(self):
        from .engine_custody import _directory, _private
        with _directory(str(self.root / '.astrid-engine'), create=False) as fd:
            info = os.stat('warm-attestation.json', dir_fd=fd, follow_symlinks=False)
            _private(info, stat.S_ISREG)
            if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise ValueError('warm record is not private')

    @staticmethod
    def _validate(saved):
        if (not isinstance(saved, dict) or set(saved) != {'schema', 'identity', 'requirements', 'object_info_sha256'}
                or saved['schema'] != SCHEMA or not isinstance(saved['identity'], dict)
                or not valid_lifecycle(saved['identity'].get('lifecycle'))
                or not isinstance(saved['requirements'], list) or len(saved['requirements']) != 1
                or not all(isinstance(item, str) and _SHA.fullmatch(item) for item in saved['requirements'])
                or not isinstance(saved['object_info_sha256'], str) or not _SHA.fullmatch(saved['object_info_sha256'])):
            raise ValueError('warm lifecycle record')

    def probe(self):
        """No-start health verifies both child liveness and listener ownership."""
        with self._budget():
            self.check()
            facts = self.runner.context.service_client.observe_generation(
                self.runner.service_payload(), deadline=self.deadline)
            self.check()
            if (not isinstance(facts, dict) or facts.get('state') != 'owned'
                    or facts.get('healthy') is not True or facts.get('required_nodes_present') is not True
                    or not valid_lifecycle(facts.get('lifecycle'))):
                return None
            return facts

    def identity(self, lifecycle):
        from . import model_verification
        from .engine_recipe import recipe_digest
        self.check()
        raw = self.files.read_json(str(self.root / '.astrid-engine/install.json'), deadline=self.deadline)
        # Only fixed control/code files are read. Never scan installed trees.
        w3 = Path(model_verification.__file__).resolve()
        w3_fact = _TargetFiles(str(w3.parent)).file_fact(str(w3), hash_bytes=True, deadline=self.deadline)
        if not w3_fact.regular or not w3_fact.sha256:
            raise ValueError('W3 implementation unavailable')
        return {'claim_id': self.claim_id, 'lifecycle': lifecycle,
                'recipe_sha256': recipe_digest(self.recipe), 'helper_sha256': self.runner.helper_sha256,
                'receipt_sha256': _receipt_identity(raw), 'w3_sha256': w3_fact.sha256}

    def contains(self, requirement):
        return self.matched and requirement in self.saved['requirements']

    def write(self, requirement, manifest, *, object_info_sha256, protected_packages=(), expected_lifecycle=None):
        """Publish LAST, only for the generation that passed verification AND doctor."""
        if not self.enabled:
            return
        try:
            with self._budget():
                self.check()
                facts = self.probe()
                if facts is None or facts['lifecycle'] != expected_lifecycle:
                    return
                identity = self.identity(expected_lifecycle)
                body = {'schema': SCHEMA, 'identity': identity, 'requirements': [requirement],
                        'object_info_sha256': object_info_sha256}
                self._validate(body)
                from .engine_custody import _directory
                with _directory(str(self.root / '.astrid-engine'), create=False):
                    self.files.write_json_atomic(self.path, body, deadline=self.deadline, check=self.check)
                self.check()
        except BaseException as error:
            # Cancellation can arrive just after rename too. Revoke the new
            # witness before propagating cancellation or any unexpected error.
            invalidate(self.root, deadline=time.monotonic() + 1)
            if (isinstance(error, CustodyUnresolved) or not isinstance(
                    error, (OSError, ValueError, TypeError, KeyError, AttributeError, EngineError, struct.error))):
                raise
            # Attestation failure never spoils a fully verified job.


def collection_receipt(*, root, engine, platform, claim_id, deadline, trust_unchanged):
    """Optional pod-local preparation/collection status shortcut under install custody.

    The controller still validates and independently reads the canonical receipt.
    Misses (including an unreadable witness) retain canonical status verification.
    """
    if (WARM_ATTESTATION_ENABLED is not True or trust_unchanged is not True
            or not isinstance(claim_id, str) or not claim_id):
        return None
    deadline = attestation_deadline(deadline)
    from .engine_execution import create_local_backend
    from .engine_recipe import load_recipe
    from .engine_runner import (
        _parse_receipt,
        _Runner,
        plan_from_receipt,
        plan_install,
        require_current_receipt,
    )
    try:
        recipe = load_recipe(engine, platform)
        context, adapter = create_local_backend(engine=engine, platform=platform, placement='local',
                                                root=root, deadline=deadline)
        with adapter.local_install_lock(root, deadline=deadline, cancelled=lambda: False):
            raw = context.read_json(str(Path(root) / '.astrid-engine/install.json'), deadline=deadline)
            receipt = _parse_receipt(raw, plan_install(recipe, root=root, placement='local'))
            plan = plan_from_receipt(recipe, receipt, root=root, placement='local')
            require_current_receipt(receipt, plan, recipe)
            if receipt.status != 'installed':
                return None
            runner = _Runner(plan, context, adapter, deadline)
            # The lifecycle comparison binds the installed helper digest and
            # authenticated owner; do not start a helper/supervisor on a hit.
            warm = WarmAttestation(root, recipe, runner, claim_id, deadline, enabled=True)
            return raw if warm.matched else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EngineError, struct.error):
        return None
