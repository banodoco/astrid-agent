"""The ``vibecomfy.workflows`` point: ready roots, thin entries, and the optional build and finalize hooks.

A contribution declares a ready root, a list of VibeComfy thin entries, and optionally
``build: file.py:fn`` and ``finalize: file.py:fn``. This module only reads that data and
hands the hooks to the engine; it never calls them. The comfyui engine's run site is the
one place that does (``comfyui_engine.ComfyUIEngine.prepare`` and ``.finish``).

``finalize`` here is not VibeComfy's template finalize (templates.py:1381). It runs once, on
the machine that executed the job, after execute and before copy-back.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

POINT = "vibecomfy.workflows"
HOOKS = ("build", "finalize")


def contributions(registry: Any) -> list[Any]:
    return list(registry.contributions(POINT))


def ready_root(contribution: Any) -> Path:
    """The contribution's ready root, resolved against its manifest folder and kept inside its pack."""
    from astrid.core.extensions.refs import resolve_in_pack
    pack_root = _pack_root(contribution)
    return resolve_in_pack(contribution.fields["ready_root"], base_dir=contribution.path.parent,
                           pack_root=pack_root, what="ready_root")


def entries(contribution: Any) -> list[dict[str, Any]]:
    """The thin entries, in VibeComfy's shape, with ``bundle`` made absolute against the ready root."""
    root = ready_root(contribution)
    rows = []
    for raw in contribution.fields.get("entries", []):
        # default_for is Astrid's slot-default declaration, not part of VibeComfy's thin entry.
        row = {"needs": [], "binds": {}, "defaults": {}, **{k: v for k, v in raw.items() if k != "default_for"}}
        bundle = Path(row["bundle"])
        row["bundle"] = str(bundle if bundle.is_absolute() else (root / bundle).resolve())
        rows.append(row)
    return rows


def hook(registry: Any, contribution: Any, name: str) -> Callable[..., Any] | None:
    """Import the named hook lazily, or None when the contribution declares none."""
    if name not in HOOKS:
        raise ValueError(f"unknown workflow hook {name!r}; expected one of {HOOKS}")
    text = contribution.fields.get(name)
    if text is None:
        return None
    from astrid.core.extensions.refs import parse_ref
    from astrid.core.extensions.registry import _import_ref
    ref = parse_ref(text, base_dir=contribution.path.parent, pack_root=_pack_root(contribution),
                    allow_module=False)
    return _import_ref(ref)


def hooks(registry: Any, contribution: Any) -> dict[str, Callable[..., Any]]:
    return {name: fn for name in HOOKS if (fn := hook(registry, contribution, name)) is not None}


def register_ready_roots(registry: Any, plugin_api: Callable[[], Any]) -> list[Path]:
    """Register each contribution's ready root with VibeComfy's plugin API (extras.py:48)."""
    roots = []
    for contribution in contributions(registry):
        root = ready_root(contribution)
        plugin_api().register_ready_root(root)
        roots.append(root)
    return roots


def workflow_entries(registry: Any) -> list[dict[str, Any]]:
    """Every contributed entry, carrying its contribution id and hooks for the engine."""
    rows = []
    for contribution in contributions(registry):
        declared = hooks(registry, contribution)
        for row in entries(contribution):
            rows.append({**row, "source": "workflow-contribution", "contribution": contribution.id,
                         **({"hooks": declared} if declared else {})})
    return rows


def _pack_root(contribution: Any) -> Path:
    # <pack>/extensions/<point>/<key>.yaml
    return contribution.path.parent.parent.parent


def default_declarations(registry: Any) -> list[dict[str, Any]]:
    """Each entry's ``default_for`` slots as ``{pack, entry, kind, mode, priority}`` rows."""
    rows = []
    for contribution in contributions(registry):
        for raw in contribution.fields.get("entries", []):
            for slot in raw.get("default_for", []):
                rows.append({"pack": contribution.pack, "entry": raw["id"], "kind": slot["kind"],
                             "mode": slot["mode"], "priority": slot["priority"]})
    return rows
