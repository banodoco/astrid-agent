"""DeepSeek Harness harness adapter.

Per-pack symlinks under ``${DSH_HOME:-~/.dsh}/skills/``. DeepSeek Harness
discovers skills from that directory each session, so no config file is touched
and every other key in the person's harness home is preserved. The override
matches the harness's own ``DSH_HOME`` variable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from ..discovery import SkillDescriptor
from .base import (
    Action,
    HarnessAdapter,
    InstallRecord,
    PlannedStep,
    ensure_symlink,
    managed_target_matches,
    remove_symlink,
    target_mechanism,
)


class DshAdapter(HarnessAdapter):
    name = "dsh"

    def _default_home(self) -> Path:
        override = os.environ.get("DSH_HOME")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".dsh"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    def target_for(self, descriptor: SkillDescriptor) -> Path:
        if descriptor.pack_id == "_core":
            return self.skills_dir / "astrid"
        return self.skills_dir / f"astrid-{descriptor.pack_id}"

    def plan(self, action: Action, descriptors: Iterable[SkillDescriptor], **opts) -> list[PlannedStep]:
        steps: list[PlannedStep] = []
        for descriptor in descriptors:
            target = self.target_for(descriptor)
            if action == "install":
                steps.append(PlannedStep(description=f"install {target} from {descriptor.skill_dir}", target=target))
            else:
                steps.append(PlannedStep(description=f"remove {target}", target=target))
        return steps

    def apply(self, action: Action, descriptors: Iterable[SkillDescriptor], **opts) -> list[PlannedStep]:
        force = bool(opts.get("force"))
        steps: list[PlannedStep] = []
        for descriptor in descriptors:
            target = self.target_for(descriptor)
            if action == "install":
                changed = ensure_symlink(target, descriptor.skill_dir, force=force)
                steps.append(
                    PlannedStep(
                        description=("created" if changed else "ok") + f" {target}",
                        target=target,
                        extras={"changed": changed, "mechanism": target_mechanism(target)},
                    )
                )
            else:
                changed = remove_symlink(target)
                steps.append(
                    PlannedStep(
                        description=("removed" if changed else "absent") + f" {target}",
                        target=target,
                        extras={"changed": changed, "mechanism": "symlink"},
                    )
                )
        return steps

    def verify(self, descriptor: SkillDescriptor) -> tuple[bool, str]:
        target = self.target_for(descriptor)
        if managed_target_matches(target, descriptor.skill_dir):
            return True, "ok (copy)" if target.is_dir() and not target.is_symlink() else "ok (symlink)"
        return False, f"{target} missing"

    def discover_installed(self, descriptor: SkillDescriptor) -> Optional[InstallRecord]:
        target = self.target_for(descriptor)
        if managed_target_matches(target, descriptor.skill_dir):
            return InstallRecord(pack_id=descriptor.pack_id, target=target, mechanism=target_mechanism(target))
        return None


__all__ = ["DshAdapter"]
