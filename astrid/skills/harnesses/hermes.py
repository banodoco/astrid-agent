"""Hermes harness adapter.

Two mechanisms:
- ``symlink`` (default): per-pack symlinks under ``${HERMES_HOME:-~/.hermes}/skills/``.
- ``external-dir``: register the whole packs tree via ``~/.hermes/config.yaml``
  ``skills.external_dirs`` and skip per-pack symlinks. Idempotent and preserves
  every other key in the config file.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Iterable, Optional

from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.contracts.errors import AstridError

from ..discovery import SkillDescriptor
from .base import (
    Action,
    sync_target_name,
    HarnessAdapter,
    InstallRecord,
    PlannedStep,
    ensure_symlink,
    managed_target_matches,
    remove_symlink,
    require_owned_target,
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
        return self.skills_dir / sync_target_name(descriptor)

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
            try:
                require_owned_target(target, (descriptor.skill_dir.resolve(),
                    *opts.get("owned_sources", {}).get(target, ())))
            except AstridError as exc:
                steps.append(PlannedStep(str(exc), target=target,
                    extras={"changed": False, "conflict": str(exc)}))
                continue
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
            dry_run = bool(opts.get("dry_run"))
            if action == "uninstall" and dry_run:
                config = self._load_config()
                skills = config.get("skills")
                external_dirs = skills.get("external_dirs", []) if isinstance(skills, dict) else []
                changed = entry in (external_dirs or [])
            else:
                changed = self._rewrite_config(action, entry)
            verb = "registered" if action == "install" else "removed"
            return [
                PlannedStep(
                    description=(verb if changed else "unchanged") + f" {entry}",
                    target=self.config_path,
                    extras={"changed": changed, "mechanism": "external-dir", "entry": entry},
                )
            ]
        steps: list[PlannedStep] = []
        for descriptor in descriptors:
            target = self.target_for(descriptor)
            if action == "install":
                try:
                    changed = ensure_symlink(target, descriptor.skill_dir, force=force, mechanism=mechanism,
                        owned_sources=opts.get("owned_sources", {}).get(target, ()))
                except AstridError as exc:
                    steps.append(PlannedStep(str(exc), target=target, extras={"changed": False, "conflict": str(exc)}))
                    continue
                steps.append(
                    PlannedStep(
                        description=("created" if changed else "ok") + f" {target}",
                        target=target,
                        extras={"changed": changed, "mechanism": target_mechanism(target)},
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
        if action == "uninstall":
            return self._remove_external_dir_entry_preserving_bytes(entry, yaml)
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
        else:  # pragma: no cover - uninstalls take the byte-preserving path above
            external_dirs = [item for item in external_dirs if item != entry]
        if external_dirs == original and self.config_path.exists():
            return False
        skills["external_dirs"] = external_dirs
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
        return True

    def _remove_external_dir_entry_preserving_bytes(self, entry: str, yaml) -> bool:
        if not self.config_path.exists():
            return False
        original_bytes = self.config_path.read_bytes()
        try:
            original_text = original_bytes.decode("utf-8")
            data = yaml.safe_load(original_text) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise RuntimeError(f"cannot safely remove Hermes external-dir entry: {exc}") from exc
        skills = data.get("skills") if isinstance(data, dict) else None
        external_dirs = skills.get("external_dirs") if isinstance(skills, dict) else None
        if not isinstance(external_dirs, list) or entry not in external_dirs:
            return False

        lines = original_text.splitlines(keepends=True)
        skills_line = next((i for i, line in enumerate(lines)
                            if re.match(r"^skills\s*:\s*(?:#.*)?(?:\r?\n)?$", line)), None)
        if skills_line is None:
            raise RuntimeError("cannot safely locate Hermes skills.external_dirs block")
        external_line = None
        for i in range(skills_line + 1, len(lines)):
            line = lines[i]
            if line.strip() and not line.lstrip().startswith("#") and not line[0].isspace():
                break
            if re.match(r"^\s+external_dirs\s*:\s*(?:#.*)?(?:\r?\n)?$", line):
                external_line = i
                break
        if external_line is None:
            raise RuntimeError("cannot safely locate Hermes external_dirs list")

        key_indent = len(lines[external_line]) - len(lines[external_line].lstrip(" "))
        entry_line = None
        for i in range(external_line + 1, len(lines)):
            line = lines[i]
            stripped = line.lstrip(" ")
            if not stripped.strip() or stripped.startswith("#"):
                continue
            indent = len(line) - len(stripped)
            if indent < key_indent or (indent == key_indent and not stripped.startswith("-")):
                break
            if not stripped.startswith("-"):
                raise RuntimeError("cannot safely parse Hermes external_dirs block list")
            try:
                item = yaml.safe_load(stripped)
            except yaml.YAMLError as exc:
                raise RuntimeError("cannot safely parse Hermes external_dirs list item") from exc
            if isinstance(item, list) and len(item) == 1 and item[0] == entry:
                entry_line = i
                break
        if entry_line is None:
            raise RuntimeError("cannot safely locate recorded Hermes external-dir entry")

        updated_lines = lines[:entry_line] + lines[entry_line + 1:]
        updated_text = "".join(updated_lines)
        try:
            updated_data = yaml.safe_load(updated_text) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError("removing Hermes external-dir entry would invalidate config") from exc
        updated_skills = updated_data.get("skills") if isinstance(updated_data, dict) else None
        updated_dirs = updated_skills.get("external_dirs") if isinstance(updated_skills, dict) else None
        expected_dirs = list(external_dirs)
        expected_dirs.remove(entry)
        if list(updated_dirs or []) != expected_dirs:
            raise RuntimeError("removing Hermes external-dir entry changed unrelated list entries")
        if isinstance(updated_skills, dict):
            updated_skills["external_dirs"] = list(updated_dirs or [])
        expected_data = copy.deepcopy(data)
        expected_data["skills"]["external_dirs"] = list(updated_dirs or [])
        if updated_data != expected_data:
            raise RuntimeError("removing Hermes external-dir entry changed unrelated config values")
        self.config_path.write_bytes(updated_text.encode("utf-8"))
        return True


__all__ = ["HermesAdapter", "PACKS_DIR_FOR_EXTERNAL"]
