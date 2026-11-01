from contextlib import contextmanager
import json
import time

import pytest

from astrid.core.generation.engine_attempt import AttemptInstaller
from astrid.core.generation.engine_contracts import CustodyUnresolved
from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
from astrid.core.generation.engine_install_task import prepare_install
from astrid.core.generation.engine_platforms import platform_adapter
from tests.core.generation import test_engine_runner as fake_installer


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    root = tmp_path / 'engine'
    binding, _ = prepare_install(root=str(root))
    monkeypatch.setattr(fake_installer, 'ROOT', str(root))
    adapter = platform_adapter(binding.platform)
    real = LocalLinuxExecutionContext(engine=binding.engine, platform=binding.platform,
                                      root=binding.root, adapter=adapter, service_client=None)

    class Context(fake_installer.FakeContext):
        # Run the real installer against its established fake operations, but
        # store attempt custody with the real lock and local filesystem.
        def file_fact(self, path, *, hash_bytes, deadline):
            if path.endswith('/attempt-custody.json'):
                return real.file_fact(path, hash_bytes=hash_bytes, deadline=deadline)
            return super().file_fact(path, hash_bytes=hash_bytes, deadline=deadline)

        def read_json(self, path, *, deadline):
            if path.endswith('/attempt-custody.json'):
                return real.read_json(path, deadline=deadline)
            return super().read_json(path, deadline=deadline)

        def write_json_atomic(self, path, value, *, deadline):
            if path.endswith('/attempt-custody.json'):
                return real.write_json_atomic(path, value, deadline=deadline)
            return super().write_json_atomic(path, value, deadline=deadline)

        @contextmanager
        def acquire_install_lock(self, path, *, deadline):
            with real.acquire_install_lock(path, deadline=deadline):
                with super().acquire_install_lock(path, deadline=deadline):
                    yield

    context = Context()
    context.deadline = time.monotonic() + 20
    return binding, context, root


def test_real_runner_emits_attempt_bound_committed_checkpoints(fake_install):
    binding, context, root = fake_install
    checkpoints = []
    installer = AttemptInstaller(context=context, adapter=context.adapter, deadline=context.deadline,
                                 receipt_id='receipt-1', on_checkpoint=checkpoints.append)
    attempt = installer.begin_attempt('task-1', 'attempt-1', 7, binding)
    receipt = attempt.wait(15)
    assert receipt.status == 'installed'
    writes = [event for event in context.events if event[0] == 'write'
              and event[1] == binding.root + '/.astrid-engine/install.json']
    assert len(checkpoints) == len(writes) == len(receipt.steps) + 2
    assert all(c.identity == attempt.identity and c.receipt['receipt_id'] == 'receipt-1'
               for c in checkpoints)
    assert checkpoints[-1].receipt['status'] == 'installed'
    record = json.loads((root / '.astrid-engine/attempt-custody.json').read_text())
    assert record['attempts'][0]['state'] == 'completed'
    with pytest.raises(CustodyUnresolved, match='already used'):
        installer.begin_attempt('task-1', 'attempt-1', 7, binding)


def test_root_replacement_blocks_next_callback(fake_install):
    binding, context, root = fake_install
    checkpoints = []
    def replace_root(checkpoint):
        checkpoints.append(checkpoint)
        root.rename(root.parent / 'old-engine')
        root.mkdir()
    installer = AttemptInstaller(context=context, adapter=context.adapter, deadline=context.deadline,
                                 receipt_id='receipt-2', on_checkpoint=replace_root)
    attempt = installer.begin_attempt('task-2', 'attempt-2', 9, binding)
    with pytest.raises(Exception):
        attempt.wait(15)
    assert len(checkpoints) == 1
    record = json.loads((root.parent / 'old-engine/.astrid-engine/attempt-custody.json').read_text())
    assert record['attempts'][0]['state'] == 'armed'
