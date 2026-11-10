"""Public API for the multi-harness skills install layer.

Three harnesses: Claude Code, Codex, Hermes. One source of truth: per-pack
The pack manifest's ``documentation: {kind: skill, path: ...}`` declaration
selects one Claude-style frontmatter document per pack.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from functools import wraps
from typing import Iterable

from . import discovery, registry, state
from .discovery import SkillDescriptor, list_skills
from .harnesses import ADAPTERS, HarnessAdapter, adapter_for, all_adapters
from .harnesses.base import remove_symlink, target_mechanism
from astrid.core.contracts.errors import AstridError
from .links import LinkScan, scan_links

def default_descriptors(
    descriptors: Iterable[SkillDescriptor] | None = None,
) -> list[SkillDescriptor]:
    """Return source-tree mains and the existing external and managed defaults."""
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
        if (pack.extensions.get("astrid", {}).get("skills", {}).get("default") is True):
            result.append(descriptor)
    return result


def default_pack_ids(descriptors: Iterable[SkillDescriptor] | None = None) -> tuple[str, ...]:
    """Return stable ids for skills installed by default."""
    return tuple(d.pack_id for d in default_descriptors(descriptors))


def developer_descriptors(
    descriptors: Iterable[SkillDescriptor] | None = None,
    extras: Iterable[SkillDescriptor] | None = None,
) -> list[SkillDescriptor]:
    """Return the units that ``--developer`` installs, in a stable order.

    These are the extras whose SKILL.md declares ``metadata.astrid.audience:
    developer`` plus the main skill of each pack that owns one (for example
    ``developing-astrid``). The mains are already composed by a default sync;
    ``--developer`` also links them into the harness by name. Only the
    developer-only extras are withheld from a default view.
    """
    mains = list(descriptors if descriptors is not None else list_skills())
    units = list(extras if extras is not None else discovery.list_extras(mains))
    developer_extras = [d for d in units if d.skill is not None and d.developer_only]
    pack_ids = {d.pack_id for d in developer_extras}
    developer_mains = [d for d in mains if d.pack_id in pack_ids or d.developer_only]
    return sorted([*developer_mains, *developer_extras], key=lambda d: (d.pack_id, d.skill or ""))


def _withheld_extras(extras: Iterable[SkillDescriptor], current_state: dict, harness: str) -> list[SkillDescriptor]:
    """Developer-only extras not selected for *harness*: kept out of its view."""
    selected = current_state["skills"][harness]["skills"]
    return [
        d for d in extras
        if d.skill is not None and d.developer_only and not selected.get(d.skill_id, {}).get("selected")
    ]


def _select_units(current_state: dict, harness: str, descriptors: Iterable[SkillDescriptor]) -> None:
    for descriptor in descriptors:
        _inventory(current_state, harness, descriptor)["selected"] = True
        disabled = current_state["disabled_defaults"].setdefault(harness, [])
        if descriptor.pack_id in disabled:
            disabled.remove(descriptor.pack_id)



def _locked_operation(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with state.locked(kwargs.get("state_path")):
            return fn(*args, **kwargs)
    return wrapped


def _units(pack_ids, skill_names, all_skills, *, uninstalling=False, developer=False, available=None):
    available = list_skills() if available is None else available
    if developer:
        if pack_ids is not None or skill_names or all_skills:
            raise ValueError("--developer selects the developer skills; do not combine it with a pack, --skill or --all-skills")
        return developer_descriptors(available)
    requested = list(pack_ids) if pack_ids is not None else None
    if requested is not None:
        by_id = {d.pack_id: d for d in available}
        missing = [pid for pid in requested if pid not in by_id]
        if missing:
            raise KeyError(f"unknown pack(s): {missing}")
        mains = [by_id[pid] for pid in requested]
    else:
        mains = available
    extras = discovery.list_extras(available)
    if skill_names and (pack_ids is None or len(mains) != 1):
        raise ValueError("--skill requires one pack")
    if all_skills and (pack_ids is None or len(mains) != 1):
        raise ValueError("--all-skills requires one pack")
    if skill_names and all_skills:
        raise ValueError("--skill and --all-skills cannot be combined")
    matching = [d for d in extras if d.pack_id in {m.pack_id for m in mains}]
    if skill_names:
        missing = set(skill_names) - {d.skill for d in matching}
        if missing:
            raise ValueError(f"unknown skill(s): {sorted(missing)}")
        return [d for d in matching if d.skill in skill_names]
    return mains + matching if all_skills or uninstalling else mains


def _inventory(current_state, harness, descriptor):
    units = current_state["skills"][harness]["skills"]
    return units.setdefault(descriptor.skill_id, {
        "role": "extra" if descriptor.skill else "main", "selected": False, "digest": None,
    })


def _validate_sync_targets(targets, descriptors):
    """Reject ambiguous destinations before any skill mutation."""
    for harness_name, adapter in targets.items():
        destinations = {}
        for descriptor in descriptors:
            target = adapter.target_for(descriptor)
            previous = destinations.get(target)
            if previous is not None and previous != descriptor.skill_id:
                raise ValueError(
                    f"Skill target collision for {harness_name}: {previous} and "
                    f"{descriptor.skill_id} both target {target}"
                )
            destinations[target] = descriptor.skill_id


def _ownership_sources(adapter, descriptors, current_state, state_path):
    """Project existing declarations/records into evidence for this operation.

    Names select candidates only. Every link must still match an exact source;
    a stale record's destination never proves what the link currently names.
    """
    declared = {d.skill_dir.resolve() for d in descriptors}
    records = current_state.get("installs", {}).get(adapter.name, {})
    sources = {adapter.target_for(d): set(declared) for d in descriptors}
    view_root = (state_path or state.state_path()).parent / "skills" / adapter.name
    marker = view_root / ".astrid-managed-packs"
    if not view_root.is_symlink() and marker.is_file() and not marker.is_symlink():
        declared.add(view_root.resolve())
        packs_root = view_root / "packs"
        for pack_id in marker.read_text(encoding="utf-8").splitlines():
            # Marker entries are pack IDs, never paths supplied to traversal.
            if pack_id and Path(pack_id).name == pack_id and pack_id not in {".", ".."}:
                pack_dir = packs_root / pack_id
                if not packs_root.is_symlink() and not pack_dir.is_symlink():
                    declared.add(pack_dir.resolve())
        for d in descriptors:
            pack_dir = packs_root / d.pack_id
            extra_dir = pack_dir / d.skill if d.skill else None
            if (extra_dir is not None and not extra_dir.is_symlink()
                    and not packs_root.is_symlink() and not pack_dir.is_symlink()
                    and pack_dir.resolve() in declared):
                declared.add(extra_dir.resolve())
    for target in sources:
        sources[target].update(declared)
    for record in records.values():
        if not isinstance(record, dict) or not isinstance(record.get("target"), str):
            continue
        target = Path(record["target"]).expanduser()
        evidence = sources.setdefault(target, set(declared))
        if isinstance(record.get("source"), str) and Path(record["source"]).is_absolute():
            evidence.add(Path(record["source"]))
    return sources, declared


@_locked_operation
def disable_owned(*, state_path: Path | None = None, dry_run: bool = False) -> dict:
    """Remove proven owned targets, retaining and reporting every conflict."""
    current = state.load(state_path)
    initial_installs = copy.deepcopy(current.get("installs", {}))
    mains = list_skills()
    descriptors = mains + discovery.list_extras(mains)
    _validate_sync_targets(all_adapters(), descriptors)
    report: dict = {"actions": [], "conflicts": [], "ambiguous": []}
    for harness, adapter in all_adapters().items():
        records = current.get("installs", {}).get(harness, {})
        retained_records = dict(records)
        harness_report = {"harness": harness, "removed": [], "preserved": []}
        sources, declared = _ownership_sources(adapter, descriptors, current, state_path)
        paths = set(sources)
        skills_dir = getattr(adapter, "skills_dir", None)
        if skills_dir is not None and skills_dir.is_dir():
            paths.update(entry for entry in skills_dir.iterdir()
                         if entry.name == "astrid" or entry.name.startswith("astrid-"))
        external_targets = set()
        for skill_id, record in records.items():
            if not isinstance(record, dict) or not isinstance(record.get("target"), str):
                report["ambiguous"].append({"harness": harness, "skill": skill_id,
                    "reason": "missing target record; preserved"})
                continue
            target = Path(record["target"]).expanduser()
            if record.get("mechanism") == "external-dir" and harness == "hermes":
                external_targets.add(target)
                recorded_units = [unit for unit in descriptors if unit.skill_id == skill_id]
                if not any(target == adapter.target_for(unit) for unit in recorded_units):
                    report["conflicts"].append({"harness": harness, "path": str(target),
                        "reason": "recorded Hermes external-dir target does not match this skill; preserved"})
                    harness_report["preserved"].append(str(target))
                    continue
                try:
                    steps = adapter.apply("uninstall", descriptors, mechanism="external-dir", dry_run=dry_run)
                except (OSError, RuntimeError, ValueError) as exc:
                    report["conflicts"].append({"harness": harness, "path": str(target), "reason": str(exc)})
                    harness_report["preserved"].append(str(target))
                    continue
                for step in steps:
                    if step.extras.get("changed"):
                        harness_report["removed"].append(step.extras["entry"])
                retained_records.pop(skill_id, None)
        for target in sorted(paths - external_targets):
            try:
                changed = remove_symlink(target, sources=sources.get(target, declared), dry_run=dry_run)
            except AstridError as exc:
                row = {"harness": harness, "path": str(target), "reason": str(exc)}
                report["conflicts"].append(row)
                if target.is_symlink():
                    report["ambiguous"].append(row)
                harness_report["preserved"].append(str(target))
                continue
            if changed:
                harness_report["removed"].append(str(target))
            for skill_id, record in records.items():
                if isinstance(record, dict) and record.get("target") == str(target):
                    retained_records.pop(skill_id, None)
        view_root = (state_path or state.state_path()).parent / "skills" / harness
        marker = view_root / ".astrid-managed-packs"
        if view_root.is_dir() and not view_root.is_symlink() and marker.is_file() and not marker.is_symlink():
            if not dry_run:
                shutil.rmtree(view_root)
            harness_report["removed"].append(str(view_root))
        if harness == "codex" and adapter.agents_md.is_file():
            text = adapter.agents_md.read_bytes().decode("utf-8", errors="surrogateescape")
            if adapter._merge_block(text, "") != text:
                if not dry_run:
                    adapter._rewrite_agents_md([], remove=True)
                harness_report["removed"].append(str(adapter.agents_md))
        current["installs"][harness] = retained_records
        if harness_report["removed"]:
            current["skills"][harness]["synced_at"] = state.now_iso()
        report["actions"].append(harness_report)
    changed = any(action["removed"] for action in report["actions"]) or current["installs"] != initial_installs
    if changed and not dry_run:
        state.save(current, state_path)
    return report


@_locked_operation
def install(
    pack_ids: Iterable[str] | None,
    harness_names: Iterable[str] | None,
    *,
    mechanism: str = "symlink",
    force: bool = False,
    dry_run: bool = False,
    state_path: Path | None = None,
    skill_names: Iterable[str] = (),
    all_skills: bool = False,
    developer: bool = False,
) -> dict:
    mechanism = mechanism or "symlink"
    available = list_skills()
    descriptors = _units(pack_ids, tuple(skill_names), all_skills, developer=developer, available=available)
    known_units = available + discovery.list_extras(available)
    targets = _resolve_harnesses(harness_names)
    _validate_sync_targets(targets, known_units)
    current_state = state.load(state_path)
    extras_for_digest = discovery.list_extras([d for d in descriptors if d.skill is None])

    report: dict = {"actions": []}
    for harness_name, adapter in targets.items():
        selected_mechanism = mechanism if harness_name == "hermes" or mechanism != "external-dir" else "symlink"
        kwargs: dict = {"force": force, "mechanism": selected_mechanism,
                        "owned_sources": _ownership_sources(adapter, known_units,
                                                            current_state, state_path)[0]}
        if harness_name == "codex":
            kwargs["all_after_descriptors"] = list(_codex_after_set(current_state, descriptors, install=True))
        if dry_run:
            steps = adapter.plan("install", descriptors, **kwargs)
        else:
            steps = adapter.apply("install", descriptors, **kwargs)
            conflicts_by_target = {step.target for step in steps if step.extras.get("conflict")}
            for descriptor in descriptors:
                target = adapter.target_for(descriptor)
                if target in conflicts_by_target:
                    continue
                state.record_install(
                    current_state,
                    harness_name,
                    descriptor.skill_id,
                    target=str(target),
                    source=str(descriptor.skill_dir.resolve()),
                    mechanism=(
                        kwargs["mechanism"]
                        if kwargs.get("mechanism") == "external-dir"
                        else target_mechanism(target)
                    ),
                )
            for descriptor in descriptors:
                if adapter.target_for(descriptor) in conflicts_by_target:
                    continue
                entry = _inventory(current_state, harness_name, descriptor)
                entry["selected"] = True
                entry["digest"] = discovery.unit_digest(descriptor, extras_for_digest)
                disabled = current_state["disabled_defaults"][harness_name]
                if descriptor.pack_id in disabled:
                    disabled.remove(descriptor.pack_id)
        report["actions"].append({"harness": harness_name, "steps": [_step_to_dict(s) for s in steps],
                                  **({"visibility": "external-dir: all skills visible"} if harness_name == "hermes" and mechanism == "external-dir" else {})})
        for step in steps:
            if step.extras.get("conflict"):
                report.setdefault("conflicts", []).append({"harness": harness_name,
                    "path": str(step.target) if step.target else None, "reason": step.extras["conflict"]})

    if not dry_run:
        state.save(current_state, state_path)
        if any(d.skill for d in descriptors):
            # A selected developer-only extra was withheld from its pack's copy,
            # so refresh the pack too: its links to the extra become live again.
            refresh_ids = {d.skill_id for d in descriptors} | {d.pack_id for d in descriptors if d.developer_only}
            resynced = sync.__wrapped__(harness_names=targets, mechanism=mechanism, force=force,
                                          state_path=state_path, refresh_ids=refresh_ids)
            # An unselected extra with a conflict is not revisited by sync.
            # Keep the preservation result from the initial replacement attempt.
            for conflict in report.get("conflicts", []):
                if conflict not in resynced.setdefault("conflicts", []):
                    resynced["conflicts"].append(conflict)
            return resynced
    return report


@_locked_operation
def uninstall(
    pack_ids: Iterable[str] | None,
    harness_names: Iterable[str] | None,
    *,
    dry_run: bool = False,
    state_path: Path | None = None,
    skill_names: Iterable[str] = (),
    all_skills: bool = False,
    developer: bool = False,
) -> dict:
    available = list_skills()
    descriptors = _units(pack_ids, tuple(skill_names), all_skills, uninstalling=True, developer=developer, available=available)
    known_units = available + discovery.list_extras(available)
    targets = _resolve_harnesses(harness_names)
    _validate_sync_targets(targets, known_units)
    current_state = state.load(state_path)

    report: dict = {"actions": []}
    for harness_name, adapter in targets.items():
        kwargs: dict = {"owned_sources": _ownership_sources(
            adapter, known_units, current_state, state_path)[0]}
        if harness_name == "codex":
            kwargs["all_after_descriptors"] = list(_codex_after_set(current_state, descriptors, install=False))
        if dry_run:
            steps = adapter.plan("uninstall", descriptors, **kwargs)
        else:
            steps = adapter.apply("uninstall", descriptors, **kwargs)
            conflicts_by_target = {step.target for step in steps if step.extras.get("conflict")}
            for descriptor in descriptors:
                if adapter.target_for(descriptor) in conflicts_by_target:
                    continue
                state.record_uninstall(
                    current_state,
                    harness_name,
                    descriptor.skill_id,
                    # ``--developer`` only unlinks: developing-astrid stays composed.
                    default=not developer and descriptor.skill is None and descriptor in default_descriptors(descriptors),
                )
            for descriptor in descriptors:
                if adapter.target_for(descriptor) not in conflicts_by_target:
                    _inventory(current_state, harness_name, descriptor)["selected"] = False
        for step in steps:
            if step.extras.get("conflict"):
                report.setdefault("conflicts", []).append({"harness": harness_name,
                    "path": str(step.target), "reason": step.extras["conflict"]})
        report["actions"].append({"harness": harness_name, "steps": [_step_to_dict(s) for s in steps]})

    if not dry_run:
        state.save(current_state, state_path)
        if targets and any(d.developer_only for d in descriptors):
            # Recompose so the deselected developer-only skills leave the view.
            hermes_install = current_state["installs"].get("hermes", {}).get("_core", {})
            mechanism = "external-dir" if hermes_install.get("mechanism") == "external-dir" else "symlink"
            resynced = sync.__wrapped__(harness_names=targets, mechanism=mechanism, state_path=state_path)
            report["resync"] = resynced
    return report


@_locked_operation
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
    refresh_ids: set[str] | None = None,
    refresh_selected_only: bool = False,
    developer: bool = False,
) -> dict:
    """Compose the gateway and selected skills, refresh units, and prune orphans.

    Deep sync also links enabled discoverable skills without changing selection.
    Developer-only extras (``metadata.astrid.audience: developer``) stay out of
    the view, even with *deep*, until selected. *developer* selects them and the
    main skill of their pack for every target harness, so later plain syncs
    keep them (``uninstall(developer=True)`` deselects them).
    """
    mechanism = mechanism or "symlink"
    all_descriptors = list_skills()
    extras = discovery.list_extras(all_descriptors)
    all_units = all_descriptors + extras
    selected_pack_ids = tuple(selected_pack_ids)
    targets = _resolve_harnesses(harness_names)
    _validate_sync_targets(targets, all_units)
    if proposed_state is not None and not dry_run:
        raise ValueError("proposed_state is only valid for a dry-run sync")
    selection_path = state_path or state.state_path()
    fresh_state = not selection_path.exists()
    current_state = proposed_state if proposed_state is not None else state.load(state_path)
    if fresh_state and proposed_state is None:
        defaults = default_descriptors(all_descriptors)
        for harness_name in targets:
            _select_units(current_state, harness_name, defaults)
    if developer:
        if dry_run:
            current_state = copy.deepcopy(current_state)
        developer_units = developer_descriptors(all_descriptors, extras)
        for harness_name in targets:
            _select_units(current_state, harness_name, developer_units)

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
        state_before = {
            key: copy.deepcopy(current_state.get(key, {}).get(harness_name))
            for key in ("skills", "installs", "disabled_defaults")
        }
        owned_sources, _declared = _ownership_sources(adapter, all_units, current_state, state_path)
        previous_view_ids: set[str] = set()
        descriptors = _sync_descriptors_for_harness(
            all_descriptors,
            current_state,
            harness_name,
            deep=deep,
            selected_pack_ids=selected_pack_ids,
            view_routes=skill_md_path is None,
        )
        if skill_md_path is not None:
            selected = current_state["skills"][harness_name]["skills"]
            descriptors = [d for d in all_units if (deep or d.pack_id == "_core" or selected.get(d.skill_id, {}).get("selected"))
                           and d.pack_id not in current_state.get("disabled_defaults", {}).get(harness_name, [])]
        view_steps: list = []
        effective_refresh = refresh_ids
        if refresh_selected_only and refresh_ids is not None:
            selected = current_state["skills"][harness_name]["skills"]
            extra_ids = {d.skill_id for d in extras}
            effective_refresh = {sid for sid in refresh_ids if sid not in extra_ids or selected.get(sid, {}).get("selected")}
        if skill_md_path is None:
            from .view import compose_view

            core = next(d for d in descriptors if d.pack_id == "_core")
            pack_descriptors = [d for d in descriptors if d.pack_id != "_core"]
            view_root = view_parent / harness_name
            previous_view_ids = {p.name for p in (view_root / "packs").iterdir()} if (view_root / "packs").is_dir() else set()
            withheld = _withheld_extras(extras, current_state, harness_name)
            withheld_ids = {d.skill_id for d in withheld}
            gateway, view_steps, view_packs = compose_view(
                view_root, core, pack_descriptors, dry_run=dry_run,
                refresh=effective_refresh, extras=extras, withheld=withheld,
            )
            from dataclasses import replace
            view_extras = [replace(d, skill_dir=view_root / "packs" / d.pack_id / d.skill,
                                   skill_md=view_root / "packs" / d.pack_id / d.skill / "SKILL.md")
                           for d in extras if d.pack_id in {p.pack_id for p in pack_descriptors}
                           and d.skill_id not in withheld_ids]
            selected_ids = {sid for sid, item in current_state["skills"][harness_name]["skills"].items() if item.get("selected")}
            visible = [d for d in [*view_packs, *view_extras] if deep or d.skill_id in selected_ids]
            registry_target = gateway.skill_md.parent / "creative-work" / "references" / "packs.md"
            if not dry_run:
                changed = registry.regenerate(
                    skill_md_path=registry_target,
                    descriptors=[*view_packs, *view_extras],
                    deep=deep,
                    selected=selected_ids,
                    view_root=view_root,
                )
            else:
                changed = False
            registry_changes[harness_name] = changed
            descriptors = [gateway, *visible]
        selected_mechanism = mechanism if harness_name == "hermes" or mechanism != "external-dir" else "symlink"
        kwargs: dict = {"force": force, "mechanism": selected_mechanism, "owned_sources": owned_sources}
        if harness_name == "codex":
            kwargs["all_after_descriptors"] = list(descriptors)
        if dry_run:
            steps = adapter.plan("install", descriptors, **kwargs)
        else:
            steps = adapter.apply("install", descriptors, **kwargs)
            conflicts_by_target = {step.target for step in steps if step.extras.get("conflict")}
            disabled_before = list(current_state["disabled_defaults"].get(harness_name, []))
            for descriptor in descriptors:
                target = adapter.target_for(descriptor)
                if target in conflicts_by_target:
                    continue
                state.record_install(
                    current_state,
                    harness_name,
                    descriptor.skill_id,
                    target=str(target),
                    source=str(descriptor.skill_dir.resolve()),
                    mechanism=(
                        kwargs["mechanism"]
                        if kwargs.get("mechanism") == "external-dir"
                        else target_mechanism(target)
                    ),
                )
            current_state["disabled_defaults"][harness_name] = disabled_before
            # Prune orphan installs from state.
            installed_ids = {d.skill_id for d in descriptors}
            # Retain unresolved/conflicting records as evidence for later cleanup.
            skills_dir = getattr(adapter, "skills_dir", None)
            if skills_dir is not None and kwargs.get("mechanism", "symlink") != "external-dir":
                from .harnesses.base import prune_orphan_skill_links

                disabled_ids = set(current_state.get("disabled_defaults", {}).get(harness_name, []))
                known_ids = {d.skill_id for d in all_units if d.pack_id not in disabled_ids}
                prune_conflicts = []
                # Custom names are candidates only when an install record ties
                # that exact destination to an exact source.
                from .harnesses.base import ours_link_to_pack_id
                prune_sources = {target: sources for target, sources in owned_sources.items()
                                 if ours_link_to_pack_id(target.name) is not None}
                for record in current_state["installs"].get(harness_name, {}).values():
                    if not isinstance(record, dict):
                        continue
                    target, source = record.get("target"), record.get("source")
                    if not isinstance(target, str) or not isinstance(source, str) or not Path(source).is_absolute():
                        continue
                    target = Path(target).expanduser()
                    if ours_link_to_pack_id(target.name) is None:
                        prune_sources.setdefault(target, set()).add(Path(source))
                removed_paths = prune_orphan_skill_links(
                    skills_dir, known_ids, owned_sources=prune_sources,
                    known_names={adapter.target_for(d).name for d in all_units if d.pack_id not in disabled_ids},
                    declared_sources=_declared, conflicts=prune_conflicts)
                steps.extend(prune_conflicts)
                for removed in removed_paths:
                    steps.append(_pruned_step(removed))
                    # Obsolete records are removed below by their recorded target.
            # Forget only absent obsolete entries. A preserved path keeps its record.
            for skill_id, record in list(current_state["installs"][harness_name].items()):
                if skill_id in installed_ids or not isinstance(record, dict) or not record.get("target"):
                    continue
                target = Path(record["target"]).expanduser()
                if not target.exists() and not target.is_symlink():
                    state.record_uninstall(current_state, harness_name, skill_id)
        outdated = []
        changed = []
        composed_ids = {d.pack_id for d in _sync_descriptors_for_harness(
            all_descriptors, current_state, harness_name, deep=deep, selected_pack_ids=selected_pack_ids
        )}
        for unit in all_units:
            if unit.pack_id not in composed_ids:
                continue
            if not dry_run and adapter.target_for(unit) in conflicts_by_target:
                continue
            entry = _inventory(current_state, harness_name, unit)
            digest = discovery.unit_digest(unit, extras)
            old = entry.get("digest")
            if old is not None and old != digest:
                outdated.append(unit.skill_id)
            if effective_refresh is None or unit.skill_id in effective_refresh or (skill_md_path is None and unit.pack_id not in previous_view_ids):
                if old is not None and old != digest:
                    changed.append(unit.skill_id)
                if not dry_run:
                    entry["digest"] = digest
        if not dry_run:
            state_after = {
                key: copy.deepcopy(current_state.get(key, {}).get(harness_name))
                for key in ("skills", "installs", "disabled_defaults")
            }
            for snapshot in (state_before, state_after):
                skills_snapshot = snapshot.get("skills")
                if isinstance(skills_snapshot, dict):
                    skills_snapshot.pop("synced_at", None)
            if state_after != state_before:
                current_state["skills"][harness_name]["synced_at"] = state.now_iso()
        report["actions"].append(
            {"harness": harness_name, "steps": [_step_to_dict(s) for s in [*view_steps, *steps]],
             "outdated": outdated, "changed": changed,
             **({"visibility": "external-dir: all skills visible"} if harness_name == "hermes" and mechanism == "external-dir" else {})}
        )
        for step in steps:
            if step.extras.get("conflict"):
                report.setdefault("conflicts", []).append({"harness": harness_name,
                    "path": str(step.target) if step.target else None, "reason": step.extras["conflict"]})

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
    extras = discovery.list_extras(all_descriptors)
    known_ids = {d.skill_id for d in [*all_descriptors, *extras]}
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
        if skill_md_path is not None:
            selected = current_state["skills"][harness_name]["skills"]
            expected = [d for d in [*all_descriptors, *extras]
                        if (deep or d.pack_id == "_core" or selected.get(d.skill_id, {}).get("selected"))
                        and d.pack_id not in current_state.get("disabled_defaults", {}).get(harness_name, [])]
        registry_descriptors = expected
        registry_target = skill_md_path
        view_root = None
        if skill_md_path is None:
            from .view import compose_view

            core = next(d for d in expected if d.pack_id == "_core")
            pack_descriptors = [d for d in expected if d.pack_id != "_core"]
            view_root = (state_path or state.state_path()).parent / "skills" / harness_name
            withheld_ids = {d.skill_id for d in _withheld_extras(extras, current_state, harness_name)}
            gateway, _steps, registry_descriptors = compose_view(
                view_root, core, pack_descriptors, dry_run=True
            )
            from dataclasses import replace
            view_extras = [replace(d, skill_dir=view_root / "packs" / d.pack_id / d.skill,
                                   skill_md=view_root / "packs" / d.pack_id / d.skill / "SKILL.md")
                           for d in extras if d.pack_id in {p.pack_id for p in pack_descriptors}
                           and d.skill_id not in withheld_ids]
            registry_descriptors = [*registry_descriptors, *view_extras]
            selected = {sid for sid, item in current_state["skills"][harness_name]["skills"].items() if item.get("selected")}
            expected = [gateway, *[d for d in registry_descriptors if deep or d.skill_id in selected]]
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
                    selected=selected,
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
            if pack_id is not None and pack_id not in known_ids and name not in {adapter.target_for(d).name for d in [*all_descriptors, *extras]}:
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
    if heal:
        _validate_sync_targets(detected, descriptors + discovery.list_extras(descriptors))

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
                        source=str(descriptor.skill_dir.resolve()),
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
    mains = list_skills()
    extras = discovery.list_extras(mains)
    descriptors = mains + extras
    current_state = state.load(state_path)
    detected = {name: adapter for name, adapter in all_adapters().items() if adapter.detect()}
    items = []
    for descriptor in descriptors:
        per_harness = {}
        for harness_name, adapter_cls in ADAPTERS.items():
            info = current_state["installs"].get(harness_name, {}).get(descriptor.skill_id)
            adapter = detected.get(harness_name) or adapter_cls()
            fs_record = None
            view_root = (state_path or state.state_path()).parent / "skills" / harness_name
            core_info = current_state["installs"].get(harness_name, {}).get("_core", {})
            inspected = descriptor
            if core_info.get("target") and view_root.is_dir() and Path(core_info["target"]).resolve() == view_root.resolve():
                from dataclasses import replace
                skill_dir = view_root if descriptor.pack_id == "_core" else view_root / "packs" / descriptor.pack_id
                if descriptor.skill:
                    skill_dir = skill_dir / descriptor.skill
                inspected = replace(descriptor, skill_dir=skill_dir, skill_md=skill_dir / "SKILL.md")
            try:
                fs_record = adapter.discover_installed(inspected)
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
                "selected": current_state["skills"][harness_name]["skills"].get(descriptor.skill_id, {}).get("selected", False),
                "outdated": (lambda recorded: recorded is not None and recorded != discovery.unit_digest(descriptor, extras))(current_state["skills"][harness_name]["skills"].get(descriptor.skill_id, {}).get("digest")),
            }
            if harness_name == "hermes" and fs_record is not None and fs_record.mechanism == "external-dir":
                entry["visibility"] = "external-dir: all skills visible"
            if fs_record is not None:
                entry["fs_target"] = str(fs_record.target)
                entry["fs_mechanism"] = fs_record.mechanism
            per_harness[harness_name] = entry
        items.append(
            {
                "pack_id": descriptor.pack_id,
                "id": descriptor.skill_id,
                "role": "extra" if descriptor.skill else "main",
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
    mains = list_skills()
    available = {d.skill_id: d for d in [*mains, *discovery.list_extras(mains)]}
    installed_now = set(current_state["installs"].get("codex", {}).keys())
    if install:
        result_ids = installed_now.union(d.skill_id for d in changed_descriptors)
    else:
        result_ids = installed_now.difference(d.skill_id for d in changed_descriptors)
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
    selected_units = {
        skill_id
        for skill_id, item in current_state.get("skills", {}).get(harness_name, {}).get("skills", {}).items()
        if item.get("selected")
    }
    units = list(all_descriptors)
    if selected_units - {d.skill_id for d in units}:
        units.extend(discovery.list_extras(all_descriptors))
    selected_ids = {d.pack_id for d in units if d.skill_id in selected_units}
    expected_ids = (
        installed_ids | selected_ids
        | set(default_pack_ids(all_descriptors))
        | {str(value) for value in retained_integrations}
        | {str(value) for value in selected_pack_ids}
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
    "check",
    "default_descriptors",
    "default_pack_ids",
    "developer_descriptors",
    "doctor",
    "install",
    "list_skills",
    "list_state",
    "sync",
    "uninstall",
]
