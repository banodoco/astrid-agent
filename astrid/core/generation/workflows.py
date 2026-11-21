"""The ``vibecomfy.workflows`` contribution contract.

A contribution declares a ready root, a list of VibeComfy thin entries, and optionally
``build: file.py:fn`` and ``after_outputs: file.py:fn``. This module only reads that data and
hands the hooks to the engine; it never calls them. The comfyui engine's run site is the
one place that does (``comfyui_engine.ComfyUIEngine.prepare`` and ``.finish``). The optional
``inspect_inputs`` hook is called by the controller before preparing a job.

``after_outputs`` is not VibeComfy's template finalize (templates.py:1381). It runs once, on
the machine that executed the job, after execute and before copy-back.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

POINT = "vibecomfy.workflows"
HOOKS = ("build", "after_outputs")
CONTROLLER_HOOKS = ("inspect_inputs",)
ASTRID_ENTRY_FIELDS = frozenset({"default_for", "inputs", "default_mode"})


@dataclass(frozen=True)
class InputRequirements:
    """Ordered project media handles, their destination file map, and receipt port."""

    handle_ids: tuple[str, ...] = ()
    file_map_port: str | None = None
    lineage_port: str | None = None


@dataclass(frozen=True)
class RemoteBuildTransport:
    """Entry inputs, a managed source, and idempotency fields for a worker."""

    inputs: Mapping[str, Any]
    descriptor: Mapping[str, Any]
    idempotency: Mapping[str, Any]


@dataclass(frozen=True)
class BuildTransport:
    """Preparation metadata consumed by the controller after building the workflow."""

    lineage: Mapping[str, Any] | None = None
    preflight_source_video: bool = True
    remote: RemoteBuildTransport | None = None


def build_transport(result: Mapping[str, Any]) -> BuildTransport:
    """Read optional transport metadata and reject invalid hook results."""
    transport = result.get("transport")
    if transport is None:
        return BuildTransport()
    if (not isinstance(transport, BuildTransport)
            or type(transport.preflight_source_video) is not bool
            or transport.lineage is not None and not _string_keyed(transport.lineage)):
        raise ValueError("workflow build returned invalid transport metadata")
    remote = transport.remote
    if remote is not None and (
        not isinstance(remote, RemoteBuildTransport)
        or not all(_string_keyed(value) for value in (remote.inputs, remote.descriptor, remote.idempotency))
    ):
        raise ValueError("workflow build returned invalid remote transport metadata")
    if remote is not None:
        from astrid.core.execution.managed_inputs import managed_file_digest

        try:
            digest = managed_file_digest(remote.descriptor, "source_video")
        except ValueError as exc:
            raise ValueError("workflow build returned invalid remote transport source descriptor") from exc
        if "sha256" in remote.descriptor and remote.descriptor["sha256"] != digest.removeprefix("sha256:"):
            raise ValueError("workflow build returned inconsistent remote transport source digests")
    return transport


def _string_keyed(value: Any) -> bool:
    return isinstance(value, Mapping) and all(isinstance(key, str) and key for key in value)


def contributions(registry: Any) -> list[Any]:
    return list(registry.contributions(POINT))


def ready_root(contribution: Any) -> Path:
    """The contribution's ready root, resolved against its manifest folder and kept inside its pack."""
    from astrid.core.extensions.refs import resolve_in_pack
    pack_root = _pack_root(contribution)
    return resolve_in_pack(contribution.fields["ready_root"], base_dir=contribution.path.parent,
                           pack_root=pack_root, what="ready_root")


def entries(contribution: Any, *, with_inputs: bool = False) -> list[dict[str, Any]]:
    """The thin entries, in VibeComfy's shape, with ``bundle`` made absolute against the ready root.

    ``with_inputs`` keeps the entry's declared ``inputs``, which are Astrid's and not VibeComfy's.
    """
    root = ready_root(contribution)
    rows = []
    for raw in contribution.fields.get("entries", []):
        # default_for (slot defaults) and inputs (the entry's declared interface) are Astrid's, not
        # part of VibeComfy's thin entry, whose validator (vibecomfy/entries.py) takes exactly
        # id, kind, modes, bundle, needs, binds and defaults.
        row = {"needs": [], "binds": {}, "defaults": {},
               **{k: v for k, v in raw.items() if k not in ASTRID_ENTRY_FIELDS},
               **{k: raw[k] for k in ("inputs", "default_mode") if with_inputs and k in raw}}
        bundle = Path(row["bundle"])
        row["bundle"] = str(bundle if bundle.is_absolute() else (root / bundle).resolve())
        rows.append(row)
    return rows


def hook(registry: Any, contribution: Any, name: str) -> Callable[..., Any] | None:
    """Import the named hook lazily, or None when the contribution declares none."""
    if name not in HOOKS + CONTROLLER_HOOKS:
        raise ValueError(f"unknown workflow hook {name!r}; expected one of {HOOKS + CONTROLLER_HOOKS}")
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
        for row in entries(contribution, with_inputs=True):
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
