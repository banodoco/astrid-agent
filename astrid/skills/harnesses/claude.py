"""Claude Code harness adapter: per-pack symlinks under ~/.claude/skills/."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

from ..discovery import SkillDescriptor
from astrid.core.contracts.errors import AstridError
from .base import (
    Action,
    sync_target_name,
    HarnessAdapter,
    PlannedStep,
    ensure_symlink,
    managed_target_matches,
    remove_symlink,
    require_owned_target,
)


class ClaudeAdapter(HarnessAdapter):
    name = "claude"

    def _default_home(self) -> Path:
        return Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    def target_for(self, descriptor: SkillDescriptor) -> Path:
        return self.skills_dir / sync_target_name(descriptor)

    def plan(self, action: Action, descriptors: Iterable[SkillDescriptor], **opts) -> list[PlannedStep]:
        steps: list[PlannedStep] = []
        for descriptor in descriptors:
            target = self.target_for(descriptor)
            try:
                require_owned_target(target, (descriptor.skill_dir.resolve(),
                    *opts.get("owned_sources", {}).get(target, ())))
            except AstridError as exc:
                steps.append(PlannedStep(str(exc), target=target,
                    extras={"changed": False, "conflict": str(exc)}))
                continue
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
                try:
                    changed = ensure_symlink(target, descriptor.skill_dir, force=force, mechanism=opts.get("mechanism", "symlink"),
                        owned_sources=opts.get("owned_sources", {}).get(target, ()))
                except AstridError as exc:
                    steps.append(PlannedStep(str(exc), target=target, extras={"changed": False, "conflict": str(exc)}))
                    continue
                steps.append(
                    PlannedStep(
                        description=("created" if changed else "ok") + f" {target}",
                        target=target,
                        extras={"changed": changed},
                    )
                )
            else:
                try:
                    changed = remove_symlink(target, sources=(descriptor.skill_dir.resolve(),
                        *opts.get("owned_sources", {}).get(target, ())))
                except AstridError as exc:
                    steps.append(PlannedStep(str(exc), target=target, extras={"changed": False, "conflict": str(exc)}))
                    continue
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
