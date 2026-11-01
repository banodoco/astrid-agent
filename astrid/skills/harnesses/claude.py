"""Claude Code harness adapter: per-pack symlinks under ~/.claude/skills/."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from ..discovery import SkillDescriptor
from .base import (
    Action,
    HarnessAdapter,
    PlannedStep,
    ensure_symlink,
    managed_target_matches,
    remove_symlink,
)


class ClaudeAdapter(HarnessAdapter):
    name = "claude"

    def _default_home(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    def target_for(self, descriptor: SkillDescriptor) -> Path:
        if descriptor.pack_id == "_core":
            # Preserve the existing manual symlink path.
            return self.skills_dir / "astrid"
        return self.skills_dir / f"astrid-{descriptor.pack_id}"

    def plan(self, action: Action, descriptors: Iterable[SkillDescriptor], **opts) -> list[PlannedStep]:
        steps: list[PlannedStep] = []
        for descriptor in descriptors:
            target = self.target_for(descriptor)
            if action == "install":
                steps.append(
                    PlannedStep(
                        description=f"install {target} from {descriptor.skill_dir}",
                        target=target,
                    )
                )
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
                        extras={"changed": changed},
                    )
                )
            else:
                changed = remove_symlink(target)
                steps.append(
                    PlannedStep(
                        description=("removed" if changed else "absent") + f" {target}",
                        target=target,
                        extras={"changed": changed},
                    )
                )
        return steps

    def verify(self, descriptor: SkillDescriptor) -> tuple[bool, str]:
        target = self.target_for(descriptor)
        if not managed_target_matches(target, descriptor.skill_dir):
            return False, f"{target} does not match {descriptor.skill_dir}"
        return True, "ok"


__all__ = ["ClaudeAdapter"]
