"""CPU installations retain the production Git source-integrity boundary."""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import IntegrityError
from astrid.core.generation.engine_execution import create_local_backend
from astrid.core.generation.engine_runner import _Runner, plan_install
from tests.integrations._pinned_source import (  # noqa: F401
    pinned_comfy_checkout,
    pinned_comfy_source,
    pinned_cpu_install,
)
from tests.integrations.test_managed_host_preparation import managed_install, session  # noqa: F401

pytestmark = pytest.mark.integration


def test_ci_requires_explicit_pinned_comfy_source(tmp_path, monkeypatch):
    from tests.integrations import _pinned_source

    # CI still requires the explicit input even if a local source was cached.
    pin = '6bfaacc67c2103481e5f0c84d75257cd0581d86a'
    monkeypatch.setattr(_pinned_source, "_SOURCE_CACHE", {(pin, None): tmp_path})
    monkeypatch.delenv("ASTRID_TEST_COMFY_SOURCE", raising=False)
    monkeypatch.setenv("ASTRID_CI_JOB", "lanes")
    with pytest.raises(pytest.fail.Exception, match=(
        "ASTRID_TEST_COMFY_SOURCE.*6bfaacc67c2103481e5f0c84d75257cd0581d86a"
    )):
        pinned_comfy_checkout(tmp_path)


def test_pinned_comfy_source_is_verified_once_per_session(tmp_path):
    from tests.integrations import _pinned_source

    source = pinned_comfy_checkout(tmp_path / "first")
    count = dict(_pinned_source.SOURCE_COUNTS)
    assert pinned_comfy_checkout(tmp_path / "second") == source
    assert _pinned_source.SOURCE_COUNTS == count


def test_sparse_weight_double_preserves_changed_and_foreign_file_checks(tmp_path, monkeypatch):
    """Only unchanged declared CPU weights receive synthetic GPU digests."""
    import hashlib

    from astrid.core.generation import entries
    from astrid.core.generation._engine_target_files import _TargetFiles
    from tests.integrations._pinned_source import _sparse_model_inventory

    monkeypatch.setattr(entries, 'ready_entries', lambda: [])
    monkeypatch.setattr(entries, 'runpod_engines', lambda: [])
    root = tmp_path / 'install'
    provider = SimpleNamespace(_receipt_for=lambda engine: {'models': [{
        'path': '/models/checkpoints/cpu.safetensors', 'bytes': 32, 'sha256': 'a' * 64,
    }, {'path': '/models/aux/config.json', 'bytes': 32, 'sha256': 'a' * 64}]})
    _sparse_model_inventory(provider, (None, None, None, root), monkeypatch)
    model_root = root / 'runtime/ComfyUI/models'
    weight = model_root / 'checkpoints/cpu.safetensors'
    files = _TargetFiles(str(model_root))
    deadline = time.monotonic() + 5
    assert files.file_fact(str(weight), hash_bytes=True, deadline=deadline).sha256 == 'a' * 64
    auxiliary = model_root / 'aux/config.json'
    assert files.file_fact(str(auxiliary), hash_bytes=True, deadline=deadline).sha256 == hashlib.sha256(auxiliary.read_bytes()).hexdigest()
    weight.write_bytes(b'changed'.ljust(32, b'!'))
    assert files.file_fact(str(weight), hash_bytes=True, deadline=deadline).sha256 == hashlib.sha256(weight.read_bytes()).hexdigest()
    foreign = model_root / 'source.py'
    foreign.write_text('real source bytes')
    assert files.file_fact(str(foreign), hash_bytes=True, deadline=deadline).sha256 == hashlib.sha256(foreign.read_bytes()).hexdigest()


@pytest.fixture
def shallow_pinned_install(session, monkeypatch, tmp_path, pinned_comfy_source):  # noqa: F811
    """Make depth-one source locally; no network and no dependency on CI depth."""
    source = tmp_path / "shallow-comfy"
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    revision = subprocess.check_output(
        ["git", "-C", str(pinned_comfy_source), "rev-parse", "HEAD"], text=True,
    ).strip()
    subprocess.run(
        ["git", "-C", str(source), "fetch", "--quiet", "--depth=1", str(pinned_comfy_source), revision],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "checkout", "--quiet", "--detach", "FETCH_HEAD"], check=True)
    with pinned_cpu_install(session, monkeypatch, tmp_path, source) as installed:
        yield installed


def test_shallow_checkout_retains_product_source_integrity(shallow_pinned_install):
    """Depth one needs no history; the recipe still requires its exact origin."""
    _client, _payload, recipe, root, _started, _bootstrap = shallow_pinned_install
    deadline = time.monotonic() + 30
    context, adapter = create_local_backend(
        engine=recipe.engine, platform=recipe.platform, placement="local",
        root=str(root), deadline=deadline,
    )
    runner = _Runner(plan_install(recipe, root=str(root), placement="local"), context, adapter, deadline)
    source = next(source for source in recipe.sources if source.name == "comfyui")
    runner.step_id = "source:comfyui"
    checkout = root / source.relative_path
    assert subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "--is-shallow-repository"], text=True,
    ).strip() == "true"
    runner.source(source, readonly=True)
    subprocess.run(
        ["git", "-C", str(checkout), "remote", "set-url", "origin",
         "https://github.com/Comfy-Org/ComfyUI"], check=True,
    )
    try:
        with pytest.raises(IntegrityError, match="source origin differs from canonical pin"):
            runner.source(source, readonly=True)
    finally:
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "set-url", "origin", source.pin.repository],
            check=True,
        )


@pytest.fixture
def shared_pinned_install(session, monkeypatch, tmp_path):  # noqa: F811
    """Run the shared builder with a tiny, honestly declared ComfyUI source pin."""
    _client, _payload, recipe, root, _mode, _started = session
    source = tmp_path / 'comfy-input'
    # Own the objects: the shared builder replaces the original tiny VibeComfy
    # repository with the product-pinned VibeComfy checkout.
    subprocess.run(
        ['git', 'clone', '--quiet', '--no-hardlinks', str(root / 'runtime/vibecomfy'), str(source)],
        check=True, timeout=10,
    )
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    recipe = replace(recipe, sources=tuple(
        replace(spec, pin=replace(spec.pin, revision=revision)) if spec.name == 'comfyui' else spec
        for spec in recipe.sources))
    cpu_session = (*session[:2], recipe, *session[3:])
    with pinned_cpu_install(cpu_session, monkeypatch, tmp_path, source) as installed:
        yield installed


def test_cpu_install_keeps_product_model_lock_for_declarations(managed_install):  # noqa: F811
    """The one-model CPU install must not replace the catalog's 36 pinned rows."""
    from astrid.core.generation import entries
    from astrid.core.generation.engine_recipe import load_recipe_model_lock

    recipe = managed_install[2]
    lock = load_recipe_model_lock(recipe.engine, recipe.platform)
    assert len(lock.models) == 36
    assets, expected = [], []
    for model in lock.models:
        path = PurePosixPath(model.destination)
        asset = {"name": path.name, "subdir": str(path.parent),
                 "url": f"https://example.invalid/model/resolve/{model.revision}/{path.name}"}
        assets.append(asset)
        expected.append({**asset, "sha256": model.sha256.removeprefix("sha256:"), "size": model.bytes})
    workflow = SimpleNamespace(metadata={"model_assets": assets})
    manifest = entries.engine_table()["comfyui"].workflow_requirements([workflow])
    assert manifest["models"] == expected


def test_shared_pinned_install_starts_the_owned_schema_peer(shared_pinned_install, monkeypatch):
    """The shared server starts through real custody and answers real loopback HTTP."""
    from urllib.request import urlopen

    from astrid.core.generation.engine_local_server import ensure_local_engine_server
    from tests.integrations._pinned_source import owned_cpu_peer
    from tests.integrations.runpod_slot_pod import PodComfy

    _client, _payload, recipe, root, _started, _bootstrap = shared_pinned_install
    receipt = json.loads((root / '.astrid-engine/install.json').read_text())
    classes = {'EmptyImage', 'SaveImage', 'MiniMaxH3ReferenceToVideo', 'UNETLoader', 'VAELoader', 'VHS_VideoCombine'}
    provider = SimpleNamespace(comfy=PodComfy(classes, lambda: []), _receipt_for=lambda engine: receipt)
    with owned_cpu_peer(provider, shared_pinned_install, monkeypatch):
        assert ensure_local_engine_server('comfyui') == 'started'
        with urlopen(f'http://127.0.0.1:{recipe.start.port}/object_info', timeout=5) as response:
            schema = json.load(response)
        assert classes <= schema.keys()


@pytest.mark.parametrize(
    "change,reason",
    [
        ("dirty", "source contains uncommitted content"),
        ("revision", "source revision differs from canonical pin"),
        ("origin", "source origin differs from canonical pin"),
    ],
)
def test_cpu_install_refuses_changed_pinned_source(shared_pinned_install, change, reason):
    """Mutate only the stand-in; the real readonly runner must refuse each drift."""
    _client, _payload, recipe, root, _started, _bootstrap = shared_pinned_install
    deadline = time.monotonic() + 30
    context, adapter = create_local_backend(
        engine=recipe.engine, platform=recipe.platform, placement="local",
        root=str(root), deadline=deadline,
    )
    runner = _Runner(
        plan_install(recipe, root=str(root), placement="local"), context, adapter, deadline,
    )
    source = next(source for source in recipe.sources if source.name == "comfyui")
    runner.step_id = "source:comfyui"
    runner.source(source, readonly=True)
    checkout = root / source.relative_path
    if change == "dirty":
        path = checkout / "tiny.py"
        path.write_text(path.read_text() + "\n# changed after installation\n")
    elif change == "revision":
        subprocess.run(
            ["git", "-C", str(checkout), "-c", "user.name=fixture",
             "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "drift"],
            check=True, capture_output=True, timeout=5,
        )
    else:
        subprocess.run(
            ["git", "-C", str(checkout), "remote", "set-url", "origin", "https://example.invalid/other"],
            check=True, capture_output=True, timeout=5,
        )
    with pytest.raises(IntegrityError, match=reason):
        runner.source(source, readonly=True)
