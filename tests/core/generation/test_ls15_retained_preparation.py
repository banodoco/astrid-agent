"""LS-15 retains custody and immutable facts, never the preceding job context."""
from __future__ import annotations

import copy
import hashlib
import os
import time
from concurrent.futures import CancelledError
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import CustodyUnresolved, DeadlineExceeded
from astrid.core.generation.managed_preparation import (
    ManagedEnginePreparation,
    digest,
    preparation_scope,
    read_preparation,
)
from astrid.core.generation.retained_preparation import RetainedPreparation, installation_generation
from tests.core.generation import test_managed_preparation

preparation = test_managed_preparation.preparation


@pytest.fixture
def retained_install(preparation, monkeypatch):
    from pathlib import Path

    from astrid.core.generation import engine_recipe, engine_runner, settings
    from astrid.core.generation.backends import vibecomfy_local_server
    report, source = preparation
    report = copy.deepcopy(report)
    root = Path(report["installation"]["root"])
    # This unit double represents a Linux managed installation. Its wire
    # locations stay POSIX even when the backing fixture files live on a
    # Windows test runner; the real native installation is proved separately.
    report["installation"]["root"] = "/ls15-managed-install"
    report["manifest"]["models_root"] = "/ls15-managed-install/models"
    report["manifest_sha256"] = digest(report["manifest"])
    report["output_dir"] = "/ls15-managed-install/output"
    members = {"source/code.py": "print('original')\n", "env/package.py": "ORIGINAL = 1\n",
               ".astrid-engine/install.json": "receipt-1", ".astrid-engine/environments/lock": "requirements-1",
               "runtime/out/sessions/comfyui/launch.json": "native-birth-1", ".astrid-engine/install.lock": "job"}
    for name, text in members.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    recipe = SimpleNamespace(engine="comfyui", sources=[SimpleNamespace(name="comfyui", relative_path="source")],
                             environments=[SimpleNamespace(relative_path="env")])
    recipe_generation = ["recipe-1"]
    monkeypatch.setattr(engine_recipe, "recipe_digest", lambda _: recipe_generation[0])
    monkeypatch.setattr(engine_runner, "helper_manifest", lambda _: {"helper": "unchanged"})
    monkeypatch.setattr(settings, "model_trust_unchanged", lambda: False)
    lock = SimpleNamespace(released=False)
    class Lease:
        def __exit__(self, *_):
            assert not lock.released
            lock.released = True
    cleanups, probes = [], []
    monkeypatch.setattr(vibecomfy_local_server, "cleanup_prompts",
                        lambda url, output, **kwargs: cleanups.append((url, output, kwargs)))
    class Runner:
        def __init__(self, plan, context, adapter, deadline, **kwargs):
            self.context = context
            self.service_owner = None
        def launch_spec(self):
            return {"generation": "owned"}
        def service(self, operation, **kwargs):
            assert operation == "probe" and self.service_owner == "owner-1"
            probes.append(self.context)
            return {"state": "owned", "owner_id": "owner-1"}
    monkeypatch.setattr(engine_runner, "_Runner", Runner)

    def job(number):
        result = ManagedEnginePreparation.__new__(ManagedEnginePreparation)
        result.task, result.attempt = f"task-{number}", f"attempt-{number}"
        result.root, result.recipe = str(root), recipe
        result.mode, result.visibility, result.device = "check", None, None
        result.context, result.adapter = SimpleNamespace(service_client=object()), object()
        result.deadline, result.cancelled = time.monotonic() + 30, lambda: False
        result.lock, result.report, result.uncertain, result._preparing_engine = None, None, False, False
        return result
    first = job(1)
    first.report = copy.deepcopy(report)
    first.report.update(task_id=first.task, attempt_id=first.attempt)
    first.lock = Lease()
    first._alignment_runner = SimpleNamespace(plan=object(), service_owner="owner-1")
    retained = RetainedPreparation(first)
    return SimpleNamespace(root=root, source=source, report=report, job=job, first=first, retained=retained,
                           lock=lock, probes=probes, cleanups=cleanups, recipe_generation=recipe_generation)


def test_reuse_rebinds_attempt_inputs_and_native_client_while_retaining_lock(retained_install):
    world = retained_install
    assert world.first.lock is None and not world.lock.released
    world.first.cancelled = lambda: True
    second, third = world.job(2), world.job(3)
    for job in (second, third):
        assert world.retained.observe(job)
        report = world.retained.reuse(job, execution_identity="execution", input_digests=world.report["inputs"])
        assert report["task_id"] == job.task and report["attempt_id"] == job.attempt
        assert report is not world.retained.report and report["manifest"] is not world.retained.report["manifest"]
        with preparation_scope(report):
            assert read_preparation(task=job.task, attempt=job.attempt, endpoint=report["endpoint"],
                execution_identity="execution", inputs={"workflow": world.source}) == report
        world.retained.register(job, world.root / job.attempt)
        assert job.readiness_reuse["audits"] == 0 and job.readiness_reuse["content_bytes_verified"] > 0
    assert world.probes == [second.context, third.context]
    assert world.first.context not in world.probes
    world.retained.finish(second, worker_drained=True)
    assert len(world.cleanups) == 1 and world.cleanups[0][1].name == "attempt-2"
    assert not world.lock.released and id(third) in world.retained.jobs
    world.retained.close()
    assert world.cleanups[1][1].name == "attempt-3" and world.lock.released


@pytest.mark.parametrize("member", ["source/code.py", "env/package.py", ".astrid-engine/install.json",
    ".astrid-engine/environments/lock", "runtime/out/sessions/comfyui/launch.json"])
def test_same_size_same_mtime_content_change_invalidates_readiness(retained_install, member):
    world = retained_install
    path = world.root / member
    before = path.stat()
    path.write_bytes(b"x" * before.st_size)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    job = world.job(2)
    assert not world.retained.observe(job)
    assert world.retained.reuse(job, execution_identity="execution", input_digests={}) is None
    assert not world.probes and not world.lock.released
    world.retained.close()
    assert world.lock.released


@pytest.mark.parametrize("change", ["recipe", "new-source", "workflow", "visibility", "mode"])
def test_relevant_generation_or_scope_change_requires_full_preparation(retained_install, change):
    world, identity = retained_install, "execution"
    job = world.job(2)
    if change == "recipe":
        world.recipe_generation[0] = "recipe-2"
    elif change == "new-source":
        (world.root / "source/new.py").write_text("NEW = 1\n")
    elif change == "workflow":
        identity = "another-execution"
    elif change == "visibility":
        job.visibility = "2"
    else:
        job.mode = "trust"
    observed = world.retained.observe(job)
    assert observed is (change == "workflow")
    assert world.retained.reuse(job, execution_identity=identity, input_digests={}) is None
    assert job.report is None and not world.lock.released
    world.retained.close()


def test_replaced_install_lock_and_unproved_prompt_drain_keep_custody(retained_install):
    world = retained_install
    lock_path = world.root / ".astrid-engine/install.lock"
    lock_path.rename(lock_path.with_suffix(".old"))
    lock_path.write_text("job")
    with pytest.raises(CustodyUnresolved, match="lock identity changed"):
        world.retained.observe(world.job(2))
    assert not world.lock.released
    # Separate native-drain failure keeps the retained lease reachable too.
    job = world.job(3)
    world.retained.register(job, world.root / job.attempt)
    with pytest.raises(CustodyUnresolved):
        world.retained.finish(job, worker_drained=False)
    with pytest.raises(CustodyUnresolved):
        world.retained.close()
    assert not world.lock.released and id(job) in world.retained.jobs


@pytest.mark.parametrize("interruption", ["cancel", "deadline"])
def test_interruption_while_reading_generation_cannot_reuse_readiness(retained_install, monkeypatch, interruption):
    from pathlib import Path

    world = retained_install
    job = world.job(2)
    cancelled = [False]
    job.cancelled = lambda: cancelled[0]
    original_open = Path.open

    @contextmanager
    def open_during_interruption(path, *args, **kwargs):
        with original_open(path, *args, **kwargs) as stream:
            if path == world.root / "source/code.py":
                class InterruptedRead:
                    fileno = stream.fileno

                    def read(self, size):
                        data = stream.read(size)
                        if interruption == "cancel":
                            cancelled[0] = True
                        else:
                            job.deadline = time.monotonic() - 1
                        return data
                yield InterruptedRead()
            else:
                yield stream

    monkeypatch.setattr(Path, "open", open_during_interruption)
    with pytest.raises(CancelledError if interruption == "cancel" else DeadlineExceeded):
        world.retained.observe(job)
    assert not world.probes and job.report is None and not world.lock.released
    assert world.retained.reuse(job, execution_identity="execution", input_digests={}) is None
    world.retained.close()


def test_io_and_unselected_models_do_not_replace_declared_model_verification(retained_install):
    world = retained_install
    for name in ("input", "output", "temp", "models"):
        folder = world.root / "source" / name
        folder.mkdir()
        (folder / "unselected.bin").write_bytes(b"per-job or unselected model bytes")
    assert world.retained.observe(world.job(2))
    model_root = world.root / "source/models"
    model = model_root / "selected.bin"
    model.write_bytes(b"declared model bytes")
    world.retained.report["manifest"].update(models_root=str(model_root), models=[{
        "name": model.name, "subdir": "", "sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
        "size": model.stat().st_size,
    }])
    world.retained.generation, _ = installation_generation(world.job(3), world.retained.report)
    assert world.retained.observe(world.job(4))
    before = model.stat()
    model.write_bytes(b"x" * before.st_size)
    os.utime(model, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not world.retained.observe(world.job(5))
    assert not world.lock.released
    world.retained.close()


@pytest.mark.parametrize("native_absent", [True, False], ids=["drained", "unproved"])
def test_generation_retirement_releases_installation_only_after_native_absence(retained_install, native_absent):
    from astrid.packs.vibecomfy.resident import HostPreparationCache

    world = retained_install
    cache = HostPreparationCache(world.root / "cache", "scope", "compiler")
    cache.managed = world.retained
    cache.invalidate()
    adapter = SimpleNamespace(has_jobs=False, retained_resources=[cache], vibecomfy_preparations={"scope": cache})
    class Owner:
        occupied = True
        current_adapter = adapter
        def release(self, *, reason):
            assert not world.lock.released
            if not native_absent:
                raise CustodyUnresolved("native release unproved")
            cache.close()
            adapter.retained_resources.clear()
            self.occupied = False
    host = SimpleNamespace(managed_tool_session=Owner())
    if native_absent:
        cache.retire_managed(host, clear_catalogue=True)
        assert world.lock.released and cache.root.exists() and not cache.invalidated
        assert not host.managed_tool_session.occupied
        cache.close()
    else:
        with pytest.raises(CustodyUnresolved, match="native release unproved"):
            cache.retire_managed(host, clear_catalogue=True)
        assert not world.lock.released and cache.invalidated and cache.root.exists()
        assert adapter.retained_resources == [cache] and host.managed_tool_session.occupied
        cache.close()
