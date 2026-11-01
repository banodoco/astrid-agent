"""Read-only preparation for a managed engine install Runtime task.

This module does not admit or execute a task.  The caller must retain the
binding in the admitted request and recheck it at the worker boundary.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace

from .engine_platforms import platform_adapter
from .engine_recipe import load_model_lock, load_recipe
from .engine_runner import plan_install


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

    @classmethod
    def from_dict(cls, value):
        """Reconstruct the exact JSON binding before the worker rechecks it.

        The document is data, not proof of admission.  The host must obtain it
        from the verified task envelope and call ``recheck_install`` before work.
        """
        if not isinstance(value, Mapping) or set(value) != {f.name for f in fields(cls)}:
            raise ValueError('invalid managed install binding fields')
        strings = {f.name for f in fields(cls)} - {'anchor_device', 'anchor_inode'}
        if any(type(value[name]) is not str or not value[name] for name in strings):
            raise ValueError('invalid managed install binding values')
        if any(type(value[name]) is not int or value[name] < 0
               for name in ('anchor_device', 'anchor_inode')):
            raise ValueError('invalid managed install binding identity')
        return cls(**dict(value))


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


def _recheck_install_static(binding):
    if not isinstance(binding, InstallBinding):
        raise TypeError('an exact install binding is required')
    if binding.anchor != '/' and not (binding.root == binding.anchor
                                     or binding.root.startswith(binding.anchor + '/')):
        raise ValueError('managed install anchor is outside the root ancestry')
    current, _ = prepare_install(root=binding.root, engine=binding.engine,
                                 platform=binding.platform)
    if replace(current, anchor=binding.anchor, anchor_device=binding.anchor_device,
               anchor_inode=binding.anchor_inode) != binding:
        raise ValueError('managed install binding changed; re-admit the task')
    return current


def recheck_install_under_custody(binding: InstallBinding, *, root_identity=None):
    """Recheck an admitted binding after the installer lock creates its root.

    The caller must hold the install lock throughout this check and the work
    it protects.  The original admission anchor must retain its identity;
    unlike the read-only admission check, newly created descendants are
    expected.  Return the root identity to pin on all subsequent checks.
    """
    current = _recheck_install_static(binding)
    anchor, device, inode, _ = _disk_anchor(binding.anchor)
    if (anchor, device, inode) != (binding.anchor, binding.anchor_device, binding.anchor_inode):
        raise ValueError('managed install anchor changed; re-admit the task')
    if current.anchor != binding.root:
        raise ValueError('managed install root is missing under custody')
    observed = (current.anchor_device, current.anchor_inode)
    if root_identity is not None and root_identity != observed:
        raise ValueError('managed install root changed under custody')
    return observed
