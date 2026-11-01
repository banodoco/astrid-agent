import json
import subprocess
import sys
import time

import pytest

from astrid.core.generation.engine_attempt_custody import AttemptIdentity, arm_attempt
from astrid.core.generation.engine_contracts import CustodyUnresolved, IntegrityError
from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
from astrid.core.generation.engine_install_task import prepare_install, recheck_install_under_custody
from astrid.core.generation.engine_platforms import platform_adapter


def _context(binding):
    adapter = platform_adapter(binding.platform)
    return LocalLinuxExecutionContext(engine=binding.engine, platform=binding.platform,
                                      root=binding.root, adapter=adapter, service_client=None)


def test_armed_attempt_survives_process_restart_and_refuses_any_replay(tmp_path):
    binding, _ = prepare_install(root=str(tmp_path / 'engine'))
    context = _context(binding)
    deadline = time.monotonic() + 10
    identity = AttemptIdentity('task', 'attempt', 3, 'receipt', binding)
    with context.acquire_install_lock(binding.root, deadline=deadline):
        root_identity = recheck_install_under_custody(binding)
        arm_attempt(context, identity, deadline=deadline, root_identity=root_identity)
    document = json.loads((tmp_path / 'engine/.astrid-engine/attempt-custody.json').read_text())
    assert document['attempts'] == [{'identity': identity.as_dict(), 'state': 'armed'}]
    # A new interpreter and context see the durable record. No inherited
    # in-memory attempt registry or fake filesystem participates in this test.
    code = '''
import json, sys, time
from astrid.core.generation.engine_attempt_custody import AttemptIdentity, arm_attempt
from astrid.core.generation.engine_contracts import CustodyUnresolved
from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
from astrid.core.generation.engine_install_task import InstallBinding, recheck_install_under_custody
from astrid.core.generation.engine_platforms import platform_adapter
binding = InstallBinding.from_dict(json.loads(sys.argv[1]))
adapter = platform_adapter(binding.platform)
ctx = LocalLinuxExecutionContext(engine=binding.engine, platform=binding.platform, root=binding.root, adapter=adapter, service_client=None)
deadline = time.monotonic() + 10
with ctx.acquire_install_lock(binding.root, deadline=deadline):
    root_identity = recheck_install_under_custody(binding)
    for attempt, receipt in [('attempt', 'receipt'), ('new-attempt', 'new-receipt')]:
        try:
            arm_attempt(ctx, AttemptIdentity('task', attempt, 4, receipt, binding), deadline=deadline, root_identity=root_identity)
        except CustodyUnresolved:
            pass
        else:
            raise AssertionError('replayed unresolved work')
'''
    subprocess.run([sys.executable, '-c', code, json.dumps(binding.as_dict())],
                   check=True, timeout=15, capture_output=True, text=True)


def test_invalid_or_replaced_custody_fails_closed(tmp_path):
    binding, _ = prepare_install(root=str(tmp_path / 'engine'))
    context = _context(binding)
    deadline = time.monotonic() + 10
    identity = AttemptIdentity('task', 'attempt', 3, 'receipt', binding)
    with context.acquire_install_lock(binding.root, deadline=deadline):
        root_identity = recheck_install_under_custody(binding)
        path = tmp_path / 'engine/.astrid-engine/attempt-custody.json'
        path.write_text('{"schema":"unknown","attempts":[]}')
        with pytest.raises(IntegrityError, match='custody record'):
            arm_attempt(context, identity, deadline=deadline, root_identity=root_identity)
        path.unlink()
        path.symlink_to(tmp_path / 'elsewhere')
        with pytest.raises(IntegrityError, match='custody record'):
            arm_attempt(context, identity, deadline=deadline, root_identity=root_identity)


@pytest.mark.parametrize('fence', [True, -1, '1'])
def test_identity_rejects_invalid_fences(tmp_path, fence):
    binding, _ = prepare_install(root=str(tmp_path / 'engine'))
    with pytest.raises(ValueError, match='fence'):
        AttemptIdentity('task', 'attempt', fence, 'receipt', binding)
