"""Read-only preparation for a managed engine install Runtime task.

This module does not admit or execute a task. The tracked install route is
disabled; preparation and rechecking remain read-only.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from .engine_platforms import platform_adapter
from .engine_recipe import load_model_lock, load_recipe
from .engine_runner import plan_install


def tracked_install_route(*_args, **_kwargs):
    """Refuse tracked installs until the Runtime route is available."""
    return {
        'code': 'MANAGED_INSTALL_ROUTE_DISABLED',
        'message': 'Tracked installs through Runtime are not available in Phase 1.',
    }


@dataclass(frozen=True)
class InstallBinding:
    engine: str
    platform: str
    placement: str
    root: str
    recipe_sha256: str
    models_lock_sha256: str
    python: str
    python_version: str
    anchor: str
    anchor_device: int
    anchor_inode: int

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class DiskObservation:
    path: str
    available_bytes: int
    locked_model_bytes: int
    # Neither the existing files nor the space for source, wheels, temporary
    # downloads and extraction are known, so this is not a remaining estimate.
    required_additional_bytes: None = None
    sufficiency: str = "unknown"

    def as_dict(self):
        return asdict(self)


def _disk_anchor(root):
    """Open the deepest existing directory without following any symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    anchor = ''
    try:
        for name in root[1:].split('/'):
            try:
                child = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                break
            os.close(fd)
            fd = child
            anchor += '/' + name
        identity = os.fstat(fd)
        usage = os.fstatvfs(fd)
        return anchor or '/', identity.st_dev, identity.st_ino, usage.f_bavail * usage.f_frsize
    finally:
        os.close(fd)


def prepare_install(*, root: str, engine: str = 'vibecomfy-h3',
                    platform: str = 'linux-x86_64-cu130'):
    """Verify the packaged H3 plan and observe free space without creating files."""
    if (engine, platform) != ('vibecomfy-h3', 'linux-x86_64-cu130'):
        raise ValueError('only the indexed local H3 engine is supported')
    recipe = load_recipe(engine, platform)
    plan = plan_install(recipe, root=root, placement='local')
    adapter = platform_adapter(platform)
    primary = next(env for env in recipe.environments if env.name == recipe.primary_environment)
    python = adapter.venv_python(adapter.join(plan.root, primary.relative_path))
    models = load_model_lock(recipe.models_lock)
    anchor, device, inode, available = _disk_anchor(plan.root)
    binding = InstallBinding(engine, platform, 'local', plan.root, plan.recipe_sha256,
                             plan.models_lock_sha256, python, primary.python_version,
                             anchor, device, inode)
    disk = DiskObservation(anchor, available, sum(model.bytes for model in models.models))
    return binding, disk


def recheck_install(binding: InstallBinding):
    """Refuse changed recipe, Python, root or existing filesystem ancestry."""
    if not isinstance(binding, InstallBinding):
        raise TypeError('an exact install binding is required')
    current, disk = prepare_install(root=binding.root, engine=binding.engine,
                                    platform=binding.platform)
    if current != binding:
        raise ValueError('managed install binding changed; re-admit the task')
    return disk
