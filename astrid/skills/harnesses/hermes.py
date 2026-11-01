"""Hermes harness adapter.

Two mechanisms:
- ``symlink`` (default): per-pack symlinks under ``${HERMES_HOME:-~/.hermes}/skills/``.
- ``external-dir``: register the whole packs tree via ``~/.hermes/config.yaml``
  ``skills.external_dirs`` and skip per-pack symlinks. Idempotent and preserves
  every other key in the config file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from astrid.core.foundation.paths import REPO_ROOT

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

PACKS_DIR_FOR_EXTERNAL = REPO_ROOT / "astrid" / "packs"


class HermesAdapter(HarnessAdapter):
    name = "hermes"

    def _default_home(self) -> Path:
        override = os.environ.get("HERMES_HOME")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".hermes"

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def config_path(self) -> Path:
        return self.home / "config.yaml"

    def target_for(self, descriptor: SkillDescriptor) -> Path:
        if descriptor.pack_id == "_core":
            return self.skills_dir / "astrid"
        return self.skills_dir / f"astrid-{descriptor.pack_id}"

    def plan(self, action: Action, descriptors: Iterable[SkillDescriptor], **opts) -> list[PlannedStep]:
        mechanism = opts.get("mechanism") or "symlink"
        steps: list[PlannedStep] = []
        if mechanism == "external-dir":
            entry = str(PACKS_DIR_FOR_EXTERNAL.resolve())
            verb = "add" if action == "install" else "remove"
            steps.append(
                PlannedStep(
                    description=f"{verb} {entry} in {self.config_path} skills.external_dirs",
                    target=self.config_path,
                )
            )
            return steps
        for descriptor in descriptors:
            target = self.target_for(descriptor)
            if action == "install":
                steps.append(PlannedStep(description=f"install {target} from {descriptor.skill_dir}", target=target))
            else:
                steps.append(PlannedStep(description=f"remove {target}", target=target))
        return steps

    def apply(self, action: Action, descriptors: Iterable[SkillDescriptor], **opts) -> list[PlannedStep]:
        mechanism = opts.get("mechanism") or "symlink"
        force = bool(opts.get("force"))
        if mechanism == "external-dir":
            entry = str(PACKS_DIR_FOR_EXTERNAL.resolve())
            changed = self._rewrite_config(action, entry)
            verb = "registered" if action == "install" else "removed"
            return [
                PlannedStep(
                    description=(verb if changed else "unchanged") + f" {self.config_path}",
                    target=self.config_path,
                    extras={"changed": changed, "mechanism": "external-dir", "entry": entry},
                )
            ]
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
        # external-dir mechanism: confirm the packs dir is in config.yaml.
        if not self.config_path.exists():
            return False, f"{target} missing and {self.config_path} not present"
        cfg = self._load_config()
        external_dirs = list((cfg.get("skills") or {}).get("external_dirs") or [])
        wanted = str(PACKS_DIR_FOR_EXTERNAL.resolve())
        if wanted in external_dirs:
            return True, "ok (external-dir)"
        return False, f"{target} missing and {wanted} not in skills.external_dirs"

    def discover_installed(self, descriptor: SkillDescriptor) -> Optional[InstallRecord]:
        target = self.target_for(descriptor)
        if managed_target_matches(target, descriptor.skill_dir):
            mechanism = target_mechanism(target)
            return InstallRecord(pack_id=descriptor.pack_id, target=target, mechanism=mechanism)
        # external-dir mechanism: packs dir registered in config.yaml
        if self.config_path.exists():
            cfg = self._load_config()
            external_dirs = list((cfg.get("skills") or {}).get("external_dirs") or [])
            if str(PACKS_DIR_FOR_EXTERNAL.resolve()) in external_dirs:
                return InstallRecord(
                    pack_id=descriptor.pack_id,
                    target=self.config_path,
                    mechanism="external-dir",
                )
        return None

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        try:
            import yaml
        except ImportError:  # pragma: no cover
            return {}
        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def _rewrite_config(self, action: Action, entry: str) -> bool:
        try:
            import yaml
        except ImportError:  # pragma: no cover
            raise RuntimeError("PyYAML is required for hermes external-dir mechanism")
        data = self._load_config()
        skills = data.setdefault("skills", {})
        if not isinstance(skills, dict):
            skills = {}
            data["skills"] = skills
        external_dirs = list(skills.get("external_dirs") or [])
        original = list(external_dirs)
        if action == "install":
            if entry not in external_dirs:
                external_dirs.append(entry)
        else:
            external_dirs = [item for item in external_dirs if item != entry]
        if external_dirs == original and self.config_path.exists():
            return False
        skills["external_dirs"] = external_dirs
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
        return True


__all__ = ["HermesAdapter", "PACKS_DIR_FOR_EXTERNAL"]
