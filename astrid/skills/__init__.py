"""Public API for the multi-harness skills install layer.

Three harnesses: Claude Code, Codex, Hermes. One source of truth: per-pack
The pack manifest's ``documentation: {kind: skill, path: ...}`` declaration
selects one Claude-style frontmatter document per pack.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from . import discovery, registry, state
from .discovery import SkillDescriptor, list_skills
from .harnesses import ADAPTERS, HarnessAdapter, adapter_for, all_adapters
from .harnesses.base import target_mechanism
from .links import LinkScan, scan_links

# Frozen setup composition selected from existing first-party declarations.
# Broader shipped-pack availability remains unchanged; these are only the
# stable skill surfaces composed into a normal agent view by default.
DEFAULT_FIRST_PARTY_SKILL_IDS = (
    "vibecomfy",
    "rendering",
    "typed_timeline",
    "video_editing",
)


def default_descriptors(
    descriptors: Iterable[SkillDescriptor] | None = None,
) -> list[SkillDescriptor]:
    """Return the gateway plus setup-selected default skill packs.

    Setup policy owns default external-pack selection.  Managed sources are
    therefore defaults only after they are provisioned and admitted; a v2
    pack manifest does not need a forbidden installer-specific field.
    First-party manifests that explicitly declare a legacy taxonomy tier keep
    their existing behavior.
    """
    from astrid.core.pack import load_pack_manifest, pack_manifest_path
    from astrid.core.pack.source_setup import active_source_inventory

    available = list(descriptors if descriptors is not None else list_skills())
    by_id = {descriptor.pack_id: descriptor for descriptor in available}
    result: list[SkillDescriptor] = [by_id["_core"]] if "_core" in by_id else []
    try:
        managed_default_ids = {
            source.pack_id for source in active_source_inventory().sources
        }
    except Exception:
        managed_default_ids = set()
    for descriptor in available:
        if descriptor.pack_id in managed_default_ids and descriptor not in result:
            result.append(descriptor)
    for pack_id in DEFAULT_FIRST_PARTY_SKILL_IDS:
        descriptor = by_id.get(pack_id)
        if descriptor is not None and descriptor not in result:
            result.append(descriptor)
    for descriptor in available:
        if descriptor in result:
            continue
        manifest_path = pack_manifest_path(descriptor.skill_dir.parent)
        if manifest_path is None:
            continue
        try:
            pack = load_pack_manifest(manifest_path)
        except Exception:  # malformed optional packs remain discoverable only
            continue
        # Canonical v2 currently normalizes omitted taxonomy fields to the
        # default value, so inspect the source manifest to distinguish an
        # explicit default declaration from legacy first-party manifests.
        try:
            import yaml

            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        except Exception:  # malformed optional packs remain opt-in
            raw = {}
        declared_tier = raw.get("install_tier") if isinstance(raw, dict) else None
        if declared_tier in {"core", "default"}:
            result.append(descriptor)
    return result


def default_pack_ids(descriptors: Iterable[SkillDescriptor] | None = None) -> tuple[str, ...]:
    """Return stable ids for skills installed by default."""
    return tuple(d.pack_id for d in default_descriptors(descriptors))



def install(
    pack_ids: Iterable[str] | None,
    harness_names: Iterable[str] | None,
    *,
    mechanism: str = "symlink",
    force: bool = False,
    dry_run: bool = False,
    state_path: Path | None = None,
) -> dict:
    descriptors = _resolve_descriptors(pack_ids)
    targets = _resolve_harnesses(harness_names)
    current_state = state.load(state_path)

    report: dict = {"actions": []}
    for harness_name, adapter in targets.items():
        kwargs: dict = {"force": force}
        if harness_name == "hermes":
            kwargs["mechanism"] = mechanism
        if harness_name == "codex":
            kwargs["all_after_descriptors"] = list(_codex_after_set(current_state, descriptors, install=True))
        if dry_run:
            steps = adapter.plan("install", descriptors, **kwargs)
        else:
            steps = adapter.apply("install", descriptors, **kwargs)
            for descriptor in descriptors:
                target = adapter.target_for(descriptor)
                state.record_install(
                    current_state,
                    harness_name,
                    descriptor.pack_id,
                    target=str(target),
                    mechanism=(
                        kwargs["mechanism"]
                        if kwargs.get("mechanism") == "external-dir"
                        else target_mechanism(target)
                    ),
                )
        report["actions"].append({"harness": harness_name, "steps": [_step_to_dict(s) for s in steps]})

    if not dry_run:
        state.save(current_state, state_path)
    return report


def uninstall(
    pack_ids: Iterable[str] | None,
    harness_names: Iterable[str] | None,
    *,
    dry_run: bool = False,
    state_path: Path | None = None,
) -> dict:
    descriptors = _resolve_descriptors(pack_ids)
    targets = _resolve_harnesses(harness_names)
    current_state = state.load(state_path)

    report: dict = {"actions": []}
    for harness_name, adapter in targets.items():
        kwargs: dict = {}
        if harness_name == "codex":
            kwargs["all_after_descriptors"] = list(_codex_after_set(current_state, descriptors, install=False))
        if dry_run:
            steps = adapter.plan("uninstall", descriptors, **kwargs)
        else:
            steps = adapter.apply("uninstall", descriptors, **kwargs)
            for descriptor in descriptors:
                state.record_uninstall(
                    current_state,
                    harness_name,
                    descriptor.pack_id,
                    default=descriptor in default_descriptors(descriptors),
                )
        report["actions"].append({"harness": harness_name, "steps": [_step_to_dict(s) for s in steps]})

    if not dry_run:
        state.save(current_state, state_path)
    return report


def sync(
    *,
    harness_names: Iterable[str] | None = None,
    mechanism: str = "symlink",
    force: bool = False,
    deep: bool = False,
    dry_run: bool = False,
    state_path: Path | None = None,
    skill_md_path: Path | None = None,
    selected_pack_ids: Iterable[str] = (),
    proposed_state: dict | None = None,
) -> dict:
    """Refresh the gateway link + registry block (and, with *deep*, per-pack links).

    Default (gateway-only) links just the ``_core`` skill as ``astrid`` into
    every detected harness, composes retained/requested integrations into its
    pack routes, and regenerates the managed pack registry block in the
    creative-work supporting reference. With *deep*, every discovered pack skill is also
    linked as ``astrid-<pack>`` and the block records those skill names.
    Orphan ``astrid-*`` installs (no longer-discovered packs) are pruned.
    """
    all_descriptors = list_skills()
    selected_pack_ids = tuple(selected_pack_ids)
    targets = _resolve_harnesses(harness_names)
    if proposed_state is not None and not dry_run:
        raise ValueError("proposed_state is only valid for a dry-run sync")
    current_state = proposed_state if proposed_state is not None else state.load(state_path)

    report: dict = {"actions": []}

    # Explicit skill_md_path is retained as a test/operator escape hatch. The
    # normal path composes one writable view per harness before linking it.
    explicit_registry_changed = False
    if skill_md_path is not None:
        explicit_registry_changed = registry.regenerate(
            skill_md_path=skill_md_path,
            descriptors=all_descriptors,
            deep=deep,
            dry_run=dry_run,
        )
    registry_changes: dict[str, bool] = {}
    view_parent = (state_path or state.state_path()).parent / "skills"

    for harness_name, adapter in targets.items():
        descriptors = _sync_descriptors_for_harness(
            all_descriptors,
            current_state,
            harness_name,
            deep=deep,
            selected_pack_ids=selected_pack_ids,
            view_routes=skill_md_path is None,
        )
        view_steps: list = []
        if skill_md_path is None:
            from .view import compose_view

            core = next(d for d in descriptors if d.pack_id == "_core")
            pack_descriptors = [d for d in descriptors if d.pack_id != "_core"]
            view_root = view_parent / harness_name
            gateway, view_steps, view_packs = compose_view(
                view_root, core, pack_descriptors, dry_run=dry_run
            )
            registry_target = gateway.skill_md.parent / "creative-work" / "references" / "packs.md"
            if not dry_run:
                changed = registry.regenerate(
                    skill_md_path=registry_target,
                    descriptors=view_packs,
                    deep=deep,
                    view_root=view_root,
                )
            else:
                changed = False
            registry_changes[harness_name] = changed
            descriptors = [gateway, *view_packs] if deep else [gateway]
        kwargs: dict = {"force": force}
        if harness_name == "hermes":
            kwargs["mechanism"] = mechanism
        if harness_name == "codex":
            kwargs["all_after_descriptors"] = list(descriptors)
        if dry_run:
            steps = adapter.plan("install", descriptors, **kwargs)
        else:
            steps = adapter.apply("install", descriptors, **kwargs)
            for descriptor in descriptors:
                target = adapter.target_for(descriptor)
                state.record_install(
                    current_state,
                    harness_name,
                    descriptor.pack_id,
                    target=str(target),
                    mechanism=(
                        kwargs["mechanism"]
                        if kwargs.get("mechanism") == "external-dir"
                        else target_mechanism(target)
                    ),
                )
            # Prune orphan installs from state.
            installed_ids = {d.pack_id for d in descriptors}
            current_state["installs"][harness_name] = {
                pid: info
                for pid, info in current_state["installs"].get(harness_name, {}).items()
                if pid in installed_ids
            }
            # Prune stale ``astrid-*`` symlinks whose pack is no longer
            # discoverable. A pack that merely isn't linked this run (e.g.
            # gateway-only mode) is still discovered, so it survives; only
            # packs that vanished entirely are removed.
            skills_dir = getattr(adapter, "skills_dir", None)
            if skills_dir is not None and kwargs.get("mechanism", "symlink") != "external-dir":
                from .harnesses.base import prune_orphan_skill_links

                disabled_ids = set(
                    current_state.get("disabled_defaults", {}).get(harness_name, [])
                )
                known_ids = {
                    d.pack_id for d in all_descriptors if d.pack_id not in disabled_ids
                }
                for removed in prune_orphan_skill_links(skills_dir, known_ids):
                    steps.append(_pruned_step(removed))
                    removed_id = _link_pack_id(removed.name)
                    state.record_uninstall(
                        current_state,
                        harness_name,
                        removed_id,
                        default=removed_id in default_pack_ids(all_descriptors),
                    )
        report["actions"].append(
            {
                "harness": harness_name,
                "steps": [_step_to_dict(s) for s in [*view_steps, *steps]],
            }
        )

    if not dry_run:
        state.save(current_state, state_path)
    report["registry"] = {
        "changed": explicit_registry_changed or any(registry_changes.values()),
        "by_harness": registry_changes,
    }
    report["warnings"] = [
        {"harness": None, "pack": None, "message": message}
        for message in discovery.discovery_problems()
    ]
    if skill_md_path is None and not dry_run:
        scans = _link_scans(targets, state_path)
        report["links"] = _link_summary(scans)
        report["broken_links"] = _broken_link_rows(scans)
    return report


def check(
    *,
    deep: bool = False,
    skill_md_path: Path | None = None,
    state_path: Path | None = None,
) -> dict:
    """Dry-run drift report for the gateway link + registry block + per-pack links.

    Reports, without making any change:

    * ``registry_stale`` — the managed pack registry block in the creative-work
      supporting reference is missing or out of date.
    * ``missing`` — packs that should be linked into a detected harness but
      are not (the gateway ``astrid`` link always; ``astrid-<pack>`` links too
      when *deep*).
    * ``stale_links`` — ``astrid-*`` symlinks whose pack is no longer
      discoverable.

    ``has_drift`` is ``True`` when any category is non-empty; callers map that
    to a non-zero exit code.
    """
    from .harnesses.base import ours_link_to_pack_id

    all_descriptors = list_skills()
    known_ids = {d.pack_id for d in all_descriptors}
    current_state = state.load(state_path)

    detected = {name: adapter for name, adapter in all_adapters().items() if adapter.detect()}

    report: dict = {
        "detected": list(detected.keys()),
        "registry_stale": (
            not registry.is_current(
                skill_md_path=skill_md_path,
                descriptors=all_descriptors,
                deep=deep,
            )
            if skill_md_path is not None
            else False
        ),
        "missing": [],
        "stale_links": [],
    }

    for harness_name, adapter in detected.items():
        expected = _sync_descriptors_for_harness(
            all_descriptors,
            current_state,
            harness_name,
            deep=deep,
            view_routes=skill_md_path is None,
        )
        registry_descriptors = expected
        registry_target = skill_md_path
        view_root = None
        if skill_md_path is None:
            from .view import compose_view

            core = next(d for d in expected if d.pack_id == "_core")
            pack_descriptors = [d for d in expected if d.pack_id != "_core"]
            view_root = (state_path or state.state_path()).parent / "skills" / harness_name
            gateway, _steps, registry_descriptors = compose_view(
                view_root, core, pack_descriptors, dry_run=True
            )
            expected = [gateway, *registry_descriptors] if deep else [gateway]
            registry_target = view_root / "creative-work" / "references" / "packs.md"
        if registry_target is not None and skill_md_path is None:
            if not registry_target.is_file():
                report["registry_stale"] = True
            else:
                report["registry_stale"] = report["registry_stale"] or not registry.is_current(
                    skill_md_path=registry_target,
                    descriptors=registry_descriptors,
                    deep=deep,
                    view_root=view_root,
                )
        for descriptor in expected:
            ok, _msg = adapter.verify(descriptor)
            if not ok:
                report["missing"].append(
                    {"harness": harness_name, "pack": descriptor.pack_id}
                )
        skills_dir = getattr(adapter, "skills_dir", None)
        if skills_dir is None or not skills_dir.is_dir():
            continue
        for entry in sorted(skills_dir.iterdir()):
            name = entry.name
            if name == "astrid" or not name.startswith("astrid-"):
                continue
            if not entry.is_symlink():
                continue
            pack_id = ours_link_to_pack_id(name)
            if pack_id is not None and pack_id not in known_ids:
                report["stale_links"].append(
                    {"harness": harness_name, "link": name, "path": str(entry)}
                )

    scans = _link_scans(detected, state_path) if skill_md_path is None else {}
    report["links"] = _link_summary(scans)
    report["broken_links"] = _broken_link_rows(scans)

    report["has_drift"] = bool(
        report["registry_stale"]
        or report["missing"]
        or report["stale_links"]
        or report["broken_links"]
    )
    return report


def doctor(*, state_path: Path | None = None, heal: bool = False) -> dict:
    descriptors = list_skills()
    detected = {name: adapter for name, adapter in all_adapters().items() if adapter.detect()}

    report: dict = {
        "detected": list(detected.keys()),
        "results": [],
        "lint": [],
        "drift": [],
        "healed": [],
        "warnings": [],
    }
    for problem in discovery.discovery_problems():
        report["warnings"].append({"harness": None, "pack": None, "message": problem})
    for descriptor in descriptors:
        text = descriptor.skill_md.read_text(encoding="utf-8")
        for finding in discovery.lint_shared_skill_md(text):
            report["lint"].append({"pack": descriptor.pack_id, "finding": finding})

    current_state = state.load(state_path)
    state_changed = False
    unlinked_optional: dict[str, list[str]] = {}
    for harness_name, adapter in detected.items():
        installed_ids = set(current_state["installs"].get(harness_name, {}).keys())
        descriptors_for_harness = descriptors
        core_install = current_state["installs"].get(harness_name, {}).get("_core")
        if isinstance(core_install, dict) and isinstance(core_install.get("target"), str):
            from .view import compose_view

            view_root = (state_path or state.state_path()).parent / "skills" / harness_name
            try:
                points_to_view = Path(core_install["target"]).expanduser().resolve() == view_root.resolve()
            except OSError:
                points_to_view = False
            if points_to_view:
                # Compose exactly the set sync would, so doctor judges the same view.
                core = next(d for d in descriptors if d.pack_id == "_core")
                expected = _sync_descriptors_for_harness(
                    descriptors, current_state, harness_name, deep=False
                )
                pack_descriptors = [d for d in expected if d.pack_id != "_core"]
                gateway, _steps, view_packs = compose_view(
                    view_root, core, pack_descriptors, dry_run=True
                )
                descriptors_for_harness = [gateway, *view_packs]

        for descriptor in descriptors_for_harness:
            fs_record = adapter.discover_installed(descriptor)
            in_state = descriptor.pack_id in installed_ids
            in_fs = fs_record is not None

            if not in_state and not in_fs:
                if descriptor.pack_id == "_core":
                    report["results"].append(
                        {
                            "harness": harness_name,
                            "pack": descriptor.pack_id,
                            "ok": False,
                            "message": "not installed",
                        }
                    )
                else:
                    # Optional packs are composed into the view but are not
                    # required to be linked into the harness; absence is a note.
                    unlinked_optional.setdefault(harness_name, []).append(descriptor.pack_id)
                continue

            if in_fs and not in_state:
                # Filesystem has it; state is missing. Drift — heal optional.
                report["drift"].append(
                    {
                        "harness": harness_name,
                        "pack": descriptor.pack_id,
                        "kind": "state-missing",
                        "message": (
                            f"installed on disk at {fs_record.target} but missing from state file; "
                            f"run `skills doctor --heal` to record it"
                        ),
                    }
                )
                if heal:
                    state.record_install(
                        current_state,
                        harness_name,
                        descriptor.pack_id,
                        target=str(fs_record.target),
                        mechanism=fs_record.mechanism,
                    )
                    state_changed = True
                    report["healed"].append(
                        {"harness": harness_name, "pack": descriptor.pack_id, "action": "recorded-from-fs"}
                    )

            if in_state and not in_fs:
                report["drift"].append(
                    {
                        "harness": harness_name,
                        "pack": descriptor.pack_id,
                        "kind": "fs-missing",
                        "message": (
                            "state file claims installed but filesystem disagrees; "
                            "run `skills install --force` or `skills uninstall`"
                        ),
                    }
                )
                if heal:
                    state.record_uninstall(current_state, harness_name, descriptor.pack_id)
                    state_changed = True
                    report["healed"].append(
                        {"harness": harness_name, "pack": descriptor.pack_id, "action": "removed-from-state"}
                    )

            ok, msg = adapter.verify(descriptor)
            report["results"].append(
                {"harness": harness_name, "pack": descriptor.pack_id, "ok": ok, "message": msg}
            )

    for harness_name, pack_ids in unlinked_optional.items():
        report["warnings"].append(
            {
                "harness": harness_name,
                "pack": None,
                "message": (
                    f"{len(pack_ids)} optional packs not linked into this harness "
                    f"(`sync --deep` links them): {', '.join(sorted(pack_ids))}"
                ),
            }
        )
    if heal and state_changed:
        state.save(current_state, state_path)
    scans = _link_scans(detected, state_path)
    report["links"] = _link_summary(scans)
    report["broken_links"] = _broken_link_rows(scans)
    return report


def list_state(*, state_path: Path | None = None) -> dict:
    descriptors = list_skills()
    current_state = state.load(state_path)
    detected = {name: adapter for name, adapter in all_adapters().items() if adapter.detect()}
    items = []
    for descriptor in descriptors:
        per_harness = {}
        for harness_name, adapter_cls in ADAPTERS.items():
            info = current_state["installs"].get(harness_name, {}).get(descriptor.pack_id)
            adapter = detected.get(harness_name) or adapter_cls()
            fs_record = None
            try:
                fs_record = adapter.discover_installed(descriptor)
            except Exception:
                fs_record = None
            in_state = info is not None
            in_fs = fs_record is not None
            installed = in_state or in_fs
            drift = (in_state and not in_fs) or (in_fs and not in_state)
            entry = {
                "installed": installed,
                "detected": harness_name in detected,
                "info": info,
                "fs_installed": in_fs,
                "state_installed": in_state,
                "drift": drift,
            }
            if fs_record is not None:
                entry["fs_target"] = str(fs_record.target)
                entry["fs_mechanism"] = fs_record.mechanism
            per_harness[harness_name] = entry
        items.append(
            {
                "pack_id": descriptor.pack_id,
                "name": descriptor.name,
                "short_description": descriptor.short_description,
                "harnesses": per_harness,
            }
        )
    return {"packs": items, "detected": list(detected.keys())}


def _resolve_descriptors(pack_ids: Iterable[str] | None) -> list[SkillDescriptor]:
    available = list_skills()
    if pack_ids is None:
        return available
    requested = list(pack_ids)
    by_id = {d.pack_id: d for d in available}
    missing = [pid for pid in requested if pid not in by_id]
    if missing:
        raise KeyError(f"unknown pack(s): {missing}")
    return [by_id[pid] for pid in requested]


def _resolve_harnesses(harness_names: Iterable[str] | None) -> dict[str, HarnessAdapter]:
    if harness_names is None or list(harness_names) == ["all"]:
        return {name: adapter for name, adapter in all_adapters().items() if adapter.detect()}
    selected: dict[str, HarnessAdapter] = {}
    for name in harness_names:
        if name not in ADAPTERS:
            raise KeyError(f"unknown harness {name!r}; valid: {sorted(ADAPTERS)}")
        adapter = adapter_for(name)
        if adapter.detect():
            selected[name] = adapter
    return selected


def _codex_after_set(
    current_state: dict,
    changed_descriptors: list[SkillDescriptor],
    *,
    install: bool,
) -> list[SkillDescriptor]:
    """Compute the descriptors that will be installed for codex AFTER applying.

    AGENTS.md should reflect post-state, so we union/diff against the current
    install record.
    """
    available = {d.pack_id: d for d in list_skills()}
    installed_now = set(current_state["installs"].get("codex", {}).keys())
    if install:
        result_ids = installed_now.union(d.pack_id for d in changed_descriptors)
    else:
        result_ids = installed_now.difference(d.pack_id for d in changed_descriptors)
    return [available[pid] for pid in sorted(result_ids) if pid in available]


def _route_pack_ids(all_descriptors: list[SkillDescriptor]) -> set[str]:
    """Packs the gateway and creative-work router link to (composed by default)."""
    core = next((d for d in all_descriptors if d.pack_id == "_core"), None)
    if core is None:
        return set()
    from .view import route_pack_ids

    return route_pack_ids(core, all_descriptors)


def _link_scans(harness_names: Iterable[str], state_path: Path | None) -> dict[str, LinkScan]:
    """Scan every composed view that exists on disk for broken markdown links."""
    base = (state_path or state.state_path()).parent / "skills"
    scans: dict[str, LinkScan] = {}
    for harness_name in harness_names:
        view_root = base / harness_name
        if view_root.is_dir():
            scans[harness_name] = scan_links(view_root)
    return scans


def _broken_link_rows(scans: dict[str, LinkScan]) -> list[dict]:
    return [
        {
            "harness": harness_name,
            "file": broken.file,
            "line": broken.line,
            "target": broken.target,
            "missing": broken.missing,
        }
        for harness_name, scan in scans.items()
        for broken in scan.broken
    ]


def _link_summary(scans: dict[str, LinkScan]) -> dict[str, dict]:
    return {
        harness_name: {"files": scan.files, "checked": scan.checked, "broken": len(scan.broken)}
        for harness_name, scan in scans.items()
    }


def _sync_descriptors_for_harness(
    all_descriptors: list[SkillDescriptor],
    current_state: dict,
    harness_name: str,
    *,
    deep: bool,
    selected_pack_ids: Iterable[str] = (),
    view_routes: bool = True,
) -> list[SkillDescriptor]:
    disabled = set(current_state.get("disabled_defaults", {}).get(harness_name, []))
    if deep:
        return [d for d in all_descriptors if d.pack_id not in disabled]
    installed_ids = set(current_state["installs"].get(harness_name, {}).keys())
    setup_selection = current_state.get("setup_selection")
    retained_integrations = (
        setup_selection.get("integrations", [])
        if isinstance(setup_selection, dict)
        else []
    )
    expected_ids = (
        installed_ids
        | set(default_pack_ids(all_descriptors))
        | {str(value) for value in retained_integrations}
        | {str(value) for value in selected_pack_ids}
        | (_route_pack_ids(all_descriptors) if view_routes else set())
    )
    expected_ids.difference_update(disabled)
    expected_ids.add("_core")
    return [descriptor for descriptor in all_descriptors if descriptor.pack_id in expected_ids]


def _step_to_dict(step) -> dict:
    return {
        "description": step.description,
        "target": str(step.target) if step.target else None,
        "extras": dict(step.extras),
    }


def _pruned_step(target: Path):
    from .harnesses.base import PlannedStep

    return PlannedStep(
        description=f"pruned stale {target}",
        target=target,
        extras={"changed": True, "pruned": True},
    )


def _link_pack_id(link_name: str) -> str:
    from .harnesses.base import ours_link_to_pack_id

    return ours_link_to_pack_id(link_name) or link_name


__all__ = [
    "DEFAULT_FIRST_PARTY_SKILL_IDS",
    "check",
    "default_descriptors",
    "default_pack_ids",
    "doctor",
    "install",
    "list_skills",
    "list_state",
    "sync",
    "uninstall",
]
