"""Codex harness adapter.

Two FS effects per install:
1. ``~/.codex/skills/astrid-<pack>`` symlinks to the pack's skill dir
   (``~/.codex/skills/astrid`` for ``_core``, preserving symmetry with the
   Claude adapter).
2. A single fenced block in ``~/.codex/AGENTS.md`` lists every installed pack
   with its short description and target path. The block is rewritten on every
   install/uninstall and is byte-stable for unchanged input.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from ..commands import rewrite_commands
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

BEGIN_MARKER = "<!-- astrid:begin -->"
END_MARKER = "<!-- astrid:end -->"


class CodexAdapter(HarnessAdapter):
    name = "codex"

    def _default_home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def agents_md(self) -> Path:
        return self.home / "AGENTS.md"

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
        steps.append(PlannedStep(description=f"rewrite fenced block in {self.agents_md}", target=self.agents_md))
        return steps

    def apply(self, action: Action, descriptors: Iterable[SkillDescriptor], **opts) -> list[PlannedStep]:
        force = bool(opts.get("force"))
        all_after: list[SkillDescriptor] = list(opts.get("all_after_descriptors") or [])
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
        # AGENTS.md reflects the FINAL set of installed descriptors.
        block_changed = self._rewrite_agents_md(all_after)
        steps.append(
            PlannedStep(
                description=("rewrote" if block_changed else "block unchanged") + f" {self.agents_md}",
                target=self.agents_md,
                extras={"changed": block_changed},
            )
        )
        return steps

    def verify(self, descriptor: SkillDescriptor) -> tuple[bool, str]:
        target = self.target_for(descriptor)
        if not managed_target_matches(target, descriptor.skill_dir):
            return False, f"{target} does not match {descriptor.skill_dir}"
        # Block presence check.
        if not self.agents_md.exists():
            return False, f"{self.agents_md} missing"
        text = self.agents_md.read_text(encoding="utf-8")
        if BEGIN_MARKER not in text or END_MARKER not in text:
            return False, "AGENTS.md fenced block missing"
        return True, "ok"

    def discover_installed(self, descriptor: SkillDescriptor) -> Optional[InstallRecord]:
        target = self.target_for(descriptor)
        if not managed_target_matches(target, descriptor.skill_dir):
            return None
        # Symlink points at the right pack dir — treat as installed even if
        # the AGENTS.md fenced block is stale; doctor's verify() catches that.
        return InstallRecord(
            pack_id=descriptor.pack_id,
            target=target,
            mechanism=target_mechanism(target),
        )

    def _rewrite_agents_md(self, descriptors: list[SkillDescriptor]) -> bool:
        block = self._render_block(descriptors)
        existing = self.agents_md.read_text(encoding="utf-8") if self.agents_md.exists() else ""
        updated = self._merge_block(existing, block)
        if updated == existing:
            return False
        self.agents_md.parent.mkdir(parents=True, exist_ok=True)
        self.agents_md.write_text(updated, encoding="utf-8")
        return True

    def _render_block(self, descriptors: list[SkillDescriptor]) -> str:
        if not descriptors:
            inner = "_no Astrid skills installed_"
        else:
            lines = []
            for descriptor in sorted(descriptors, key=lambda d: d.pack_id):
                target = self.target_for(descriptor)
                desc = (descriptor.short_description or descriptor.description or "").strip().splitlines()
                desc_line = desc[0] if desc else ""
                lines.append(f"- `{descriptor.pack_id}` ({target}): {desc_line}".rstrip())
            inner = "\n".join(lines)
        return rewrite_commands(f"{BEGIN_MARKER}\n# Astrid skills\n\n{inner}\n{END_MARKER}")

    @staticmethod
    def _merge_block(existing: str, block: str) -> str:
        if BEGIN_MARKER in existing and END_MARKER in existing:
            before, _, rest = existing.partition(BEGIN_MARKER)
            _, _, after = rest.partition(END_MARKER)
            return f"{before}{block}{after}"
        if not existing:
            return block + "\n"
        suffix = "" if existing.endswith("\n") else "\n"
        return f"{existing}{suffix}\n{block}\n"


__all__ = ["BEGIN_MARKER", "CodexAdapter", "END_MARKER"]
