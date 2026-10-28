"""Tests for the partial A2 filesystem component, not a service context."""
import hashlib
import os
import time

import pytest

from astrid.core.generation._engine_target_files import _TargetFiles
from astrid.core.generation.engine_contracts import DeadlineExceeded, RecipeValidationError, UnsafePathError


def deadline():
    return time.monotonic() + 10


def test_readonly_construction_missing_and_atomic_json(tmp_path):
    root = tmp_path / 'root'
    files = _TargetFiles(str(root))
    assert not root.exists()
    assert not files.file_fact(str(root / 'file'), hash_bytes=True, deadline=deadline()).exists
    files.ensure_dir(str(root / 'dir'), deadline=deadline())
    path = root / 'dir' / 'value.json'
    files.write_json_atomic(str(path), {'x': 'é'}, deadline=deadline())
    raw = path.read_bytes()
    assert files.read_json(str(path), deadline=deadline()) == {'x': 'é'}
    fact = files.file_fact(str(path), hash_bytes=True, deadline=deadline())
    assert fact.regular and fact.bytes == len(raw) and fact.sha256 == hashlib.sha256(raw).hexdigest()
    assert os.stat(path).st_mode & 0o777 == 0o600
    files.write_json_atomic(str(path), {'changed': True}, deadline=deadline())
    assert files.read_json(str(path), deadline=deadline()) == {'changed': True}


def test_symlinks_hardlinks_and_outside_refused(tmp_path):
    root, outside = tmp_path / 'root', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    files = _TargetFiles(str(root))
    (root / 'link').symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        files.ensure_dir(str(root / 'link' / 'new'), deadline=deadline())
    with pytest.raises(UnsafePathError):
        files.write_json_atomic(str(outside / 'value'), {}, deadline=deadline())
    (root / 'value').symlink_to(outside / 'value')
    assert files.file_fact(str(root / 'value'), hash_bytes=True, deadline=deadline()).symlink
    with pytest.raises(UnsafePathError):
        files.write_json_atomic(str(root / 'value'), {}, deadline=deadline())
    (root / 'value').unlink()
    (outside / 'value').write_text('{}')
    os.link(outside / 'value', root / 'value')
    with pytest.raises(UnsafePathError):
        files.read_json(str(root / 'value'), deadline=deadline())
    with pytest.raises(UnsafePathError):
        files.write_json_atomic(str(root / 'value'), {}, deadline=deadline())


def test_limits_invalid_json_deadline_and_path(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    files = _TargetFiles(str(root))
    path = root / 'json'
    for text in ('{"x":1,"x":2}', '{"x":NaN}', '[]', 'x'):
        path.write_text(text)
        with pytest.raises(RecipeValidationError):
            files.read_json(str(path), deadline=deadline())
    path.write_bytes(b' ' * (4 * 1024 * 1024 + 1))
    with pytest.raises(RecipeValidationError):
        files.read_json(str(path), deadline=deadline())
    with pytest.raises(DeadlineExceeded):
        files.ensure_dir(str(root), deadline=time.monotonic() - 1)
    with pytest.raises(UnsafePathError):
        files.file_fact(str(root) + '/../else', hash_bytes=False, deadline=deadline())


def test_context_delegates_commands_service_and_lock(tmp_path):
    import json
    from astrid.core.generation.engine_contracts import CommandRequest, CommandResult
    from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
    from astrid.core.generation.engine_platforms import platform_adapter
    from astrid.core.generation.engine_recipe import load_recipe, recipe_digest
    from astrid.core.generation.engine_runner import _sha, helper_manifest

    platform = 'linux-x86_64-cu130'
    root = str(tmp_path / 'engine')
    adapter = platform_adapter(platform)
    class Client:
        def run(self, request):
            self.request = request
            return CommandResult(7, 'output', '', False, 0.1, 'job', False, False)
        def call(self, operation, payload, *, deadline):
            self.call_args = operation, payload, deadline
            return {'state': 'absent'}
    client = Client()
    ctx = LocalLinuxExecutionContext(engine='vibecomfy-h3', platform=platform, root=root,
                                     adapter=adapter, service_client=client)
    limit = deadline()
    ordinary = CommandRequest(('git', 'status'), root, {}, limit)
    assert ctx.run(ordinary).exit_code == 7 and client.request is ordinary
    recipe = load_recipe(ctx.engine, platform)
    payload = {'schema': 'astrid.engine-service.v1', 'engine': ctx.engine, 'platform': platform,
               'root': root, 'recipe_sha256': recipe_digest(recipe),
               'helper_sha256': _sha(helper_manifest(recipe)), 'launch': None, 'owner_id': None,
               'timeout_seconds': 10, 'poll_interval_seconds': 1}
    argv = ('python3', '-I', '-m', 'astrid.core.generation.engine_execution', '--engine-service',
            'probe', json.dumps(payload))
    result = ctx.run(CommandRequest(argv, root, {}, limit))
    assert json.loads(result.stdout) == {'schema': payload['schema'], 'operation': 'probe', 'facts': {'state': 'absent'}}
    assert client.call_args == ('probe', payload, limit)
    payload['root'] = str(tmp_path / 'other')
    with pytest.raises(Exception) as error:
        ctx.run(CommandRequest((*argv[:-1], json.dumps(payload)), root, {}, limit))
    from astrid.core.generation.engine_contracts import IntegrityError
    assert isinstance(error.value, IntegrityError)
    with ctx.acquire_install_lock(root, deadline=limit):
        assert (tmp_path / 'engine' / '.astrid-engine' / 'install.lock').exists()
    with pytest.raises(UnsafePathError):
        ctx.acquire_install_lock(root + '/other', deadline=limit)
    with pytest.raises(DeadlineExceeded):
        ctx.run(CommandRequest(('git',), root, {}, time.monotonic() - 1))


def test_download_delegates_canonical_and_confines_root(tmp_path, monkeypatch):
    from astrid.core.generation import model_downloader
    from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
    from astrid.core.generation.engine_platforms import platform_adapter
    root = str(tmp_path / 'engine')
    ctx = LocalLinuxExecutionContext(engine='vibecomfy-h3', platform='linux-x86_64-cu130',
                                     root=root, adapter=platform_adapter('linux-x86_64-cu130'),
                                     service_client=object())
    seen = []
    sentinel = object()
    monkeypatch.setattr(model_downloader, 'download_model', lambda model, **kw: seen.append((model, kw)) or sentinel)
    model, limit = object(), deadline()
    assert ctx.download_model(model, models_root=root + '/models', deadline=limit) is sentinel
    assert seen == [(model, {'models_root': root + '/models', 'deadline': limit})]
    with pytest.raises(UnsafePathError):
        ctx.download_model(model, models_root=str(tmp_path / 'elsewhere'), deadline=limit)
