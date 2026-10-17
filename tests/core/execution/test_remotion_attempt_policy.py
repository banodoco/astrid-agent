"""Trusted closure → real read-only Seatbelt projection, never a render proof.

The existing closure-contract fixture is disposable package data. No Runtime
attempt, renderer readiness or successful output is manufactured here.
"""
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution import custody_broker as cb
from astrid.core.execution import generic_host as gh
from astrid.core.execution import remotion_closure
from tests.core.execution import test_remotion_closure_binding as closure_tests
from tests.core.execution.test_inventory_load_commands import run_assertions_with_retained_owner

roots = closure_tests.roots


def inputs(tmp_path, roots):
    binding = remotion_closure.RemotionClosureBinding.admit(**roots[3])
    source = Path(gh.__file__).resolve().parents[2] / 'packs/rendering'
    client = gh.RuntimeProtocolClient('http://127.0.0.1:1', 'unused-policy-test')
    host = gh.GenericPackHost(pack_roots=[source], client=client,
                             remotion_closure_binding=binding)
    host.discover()
    record = host.capabilities['rendering.render']
    record = replace(record, executable_bindings=host._executable_admission.require(
        gh._binary_requirements(record)))
    attempt = tmp_path / 'attempt'
    attempt.mkdir()
    authority = gh._AuthenticatedAttempt((('project_id', 'synthetic-policy-only'), ('fence', 1)))
    return host, record, binding, attempt, authority


def test_admitted_project_read_only_in_real_child(tmp_path, roots, monkeypatch, request):
    if os.environ.get("ASTRID_TEST_RETAINED_OWNER_CHILD") != "1":
        run_assertions_with_retained_owner(tmp_path, request.node.nodeid, ("node", "ffmpeg"))
        return
    assert cb._COMMAND_INVENTORY_FD in os.environ
    host, record, binding, attempt, authority = inputs(tmp_path, roots)
    outside = tmp_path / 'unrelated-private.txt'
    outside.write_text('outside')
    monkeypatch.setenv('ASTRID_REMOTION_PROJECT_DIR', str(outside.parent))
    try:
        policy = host._attempt_policy_factory.build(authority, attempt=attempt, record=record)
        assert binding.project in policy.bootstrap_roots
        assert binding.project.parent not in policy.bootstrap_roots
        assert policy.project.write_roots == ()
        assert policy.network == gh._SandboxNetworkPolicy()
        assert policy.claim_binding == authority.fields
        code = '''
import json,sys
result={}
for name,path,mode in [('read',sys.argv[1],'r'),('write',sys.argv[1],'w'),('outside',sys.argv[2],'r')]:
 try:
  with open(path,mode) as stream: value=stream.read() if mode=='r' else stream.write('bad')
  result[name]={'value':value}
 except OSError as exc: result[name]={'errno':exc.errno}
print(json.dumps(result))
'''
        argv = [sys.executable, '-I', '-S', '-B', '-c', code,
                str(binding.project / 'package.json'), str(outside)]
        plan = gh._confined_launch_plan(argv, policy=policy)
        run = subprocess.run(plan.argv, env=dict(plan.environment), cwd=plan.cwd,
                             capture_output=True, text=True, check=True)
        seen = json.loads(run.stdout)
        assert json.loads(seen['read']['value']) == {'name': 'fixture'}
        assert seen['write']['errno'] in (1, 13)
        assert seen['outside']['errno'] in (1, 13)
        binding.validate(binding.project)
    finally:
        host.shutdown()


def test_changed_binding_refused_before_projection(tmp_path, roots, request):
    if os.environ.get("ASTRID_TEST_RETAINED_OWNER_CHILD") != "1":
        run_assertions_with_retained_owner(tmp_path, request.node.nodeid, ("node", "ffmpeg"))
        return
    assert cb._COMMAND_INVENTORY_FD in os.environ
    host, record, binding, attempt, authority = inputs(tmp_path, roots)
    try:
        binding.receipt.write_text('{}')
        with pytest.raises(gh.HostError, match='closure changed before policy projection'):
            host._attempt_policy_factory.build(authority, attempt=attempt, record=record)
    finally:
        host.shutdown()


def test_other_capability_does_not_inherit_renderer_read_scope(tmp_path, roots, request):
    if os.environ.get("ASTRID_TEST_RETAINED_OWNER_CHILD") != "1":
        run_assertions_with_retained_owner(tmp_path, request.node.nodeid, ("node", "ffmpeg"))
        return
    assert cb._COMMAND_INVENTORY_FD in os.environ
    host, record, binding, attempt, authority = inputs(tmp_path, roots)
    try:
        record = replace(record, definition=replace(record.definition, id='rendering.timeline_visualize'))
        policy = host._attempt_policy_factory.build(authority, attempt=attempt, record=record)
        assert binding.project not in policy.bootstrap_roots
    finally:
        host.shutdown()


def test_untyped_environment_style_binding_refused():
    with pytest.raises(gh.HostError, match='trusted host binding'):
        gh._AttemptPolicyFactory(gh.ExecutableAdmission(os.environ.get('PATH', '')),
                                remotion_closure_binding=SimpleNamespace(project=Path('/tmp')))
