"""The node classes a provisioned managed worker provides, decided offline.

A free check is only worth its name if it covers the paid path.  The paid path
runs a compiled workflow on a ComfyUI that the recipe provisions: ComfyUI core,
the recipe's pinned node packs, the node packs the workflow itself declares,
and VibeComfy's own ComfyUI node package (its "owned" nodes, linked into
``custom_nodes`` by managed preparation).  This module answers, without a
machine, whether every class a compiled workflow uses is in that set.

The class-to-pack map is VibeComfy's shipped schema cache (read through its
public API).  A class the cache cannot place, or places in a pack the worker is
never given, is refused by name before any machine is claimed.
"""
from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

_CORE_PACKS = ("comfy_core", "comfy_extras")
_OWNED_PACK = "vibecomfy"
OWNED_SOURCE = "vibecomfy"


@lru_cache(maxsize=1)
def owned_node_classes() -> frozenset[str]:
    """Class ids VibeComfy's own ComfyUI node package registers.

    Read from the package's ``NODE_CLASS_MAPPINGS`` (the mapping ComfyUI itself
    loads), never from a hand-kept list, so a node added to VibeComfy is known
    here without a second edit.  Headless mode keeps the import from touching a
    server.
    """
    import importlib

    prior = os.environ.get("VIBECOMFY_HEADLESS")
    os.environ["VIBECOMFY_HEADLESS"] = "1"
    try:
        module = importlib.import_module("vibecomfy.comfy_nodes")
    finally:
        if prior is None:
            os.environ.pop("VIBECOMFY_HEADLESS", None)
        else:
            os.environ["VIBECOMFY_HEADLESS"] = prior
    return frozenset(str(name) for name in module.NODE_CLASS_MAPPINGS)


def workflow_class_types(api: Mapping[str, Any]) -> frozenset[str]:
    """Every class id in a compiled API-format workflow."""
    return frozenset(str(node["class_type"]) for node in api.values()
                     if isinstance(node, Mapping) and isinstance(node.get("class_type"), str))


def _in_pack(class_type: str, slug: str, commit: str | None, *, exact_commit: bool) -> bool:
    """Whether VibeComfy's shipped schema evidence places ``class_type`` in the pack ``slug``.

    Pack ids are compared exactly.  With a commit, the evidence must have been captured at
    that commit (identity lookup).  Without ``exact_commit``, evidence for the same pack
    at another commit is accepted when no capture exists at the requested one.
    """
    from vibecomfy.errors import ObjectInfoIdentityAmbiguityError
    from vibecomfy.porting.object_info.consume import get_class, has_class_identity

    if commit:
        try:
            if has_class_identity(class_type, pack_slug=slug, git_commit=commit):
                return True
        except ObjectInfoIdentityAmbiguityError:
            return True  # several captures at this exact identity all place it in the pack
    if exact_commit:
        return False
    entry = get_class(class_type)
    return bool(entry) and entry.get("pack_slug") == slug


def _slug(repository: str) -> str:
    """A node pack's id: its repository name, exactly as the schema captures name it."""
    return urlsplit(repository).path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


@dataclass(frozen=True)
class WorkerNodeSet:
    """What one recipe, plus the packs a workflow declares, gives the worker."""

    owned: frozenset[str]
    recipe_packs: tuple[tuple[str, str], ...]      # (pack id, pinned commit): installed by the recipe
    declared_packs: tuple[tuple[str, str], ...]    # (pack id, commit): restored for this workflow
    core_commit: str | None
    required: frozenset[str]
    has_owned_source: bool

    def _where(self, class_type: str) -> str:
        from vibecomfy.porting.object_info.consume import get_class

        entry = get_class(class_type)
        pack = (entry or {}).get("pack_slug") or (entry or {}).get("pack")
        if not pack:
            return ("not a ComfyUI core node, not VibeComfy-owned, and in no pack the worker "
                    "recipe or the workflow declares")
        if pack == _OWNED_PACK:
            return "an old VibeComfy node class that its node package no longer registers"
        return (f"provided by node pack {pack!r}, which the worker recipe does not install "
                "and the workflow does not declare")

    def provides(self, class_type: str) -> bool:
        if class_type in self.owned:
            return self.has_owned_source
        if class_type in self.required:
            return True
        if any(_in_pack(class_type, slug, commit, exact_commit=True) for slug, commit in self.recipe_packs):
            return True
        if any(_in_pack(class_type, slug, commit, exact_commit=False) for slug, commit in self.declared_packs):
            return True
        return any(_in_pack(class_type, slug, self.core_commit if slug == "comfy_core" else None,
                            exact_commit=False) for slug in _CORE_PACKS)

    def unprovided(self, class_types: Iterable[str]) -> dict[str, str]:
        """Map each class the worker would not have to the plain reason why."""
        missing: dict[str, str] = {}
        for class_type in sorted(set(class_types)):
            if class_type in self.owned and not self.has_owned_source:
                missing[class_type] = ("a VibeComfy-owned node, but the worker recipe has no "
                                       f"{OWNED_SOURCE!r} source to link it from")
            elif not self.provides(class_type):
                missing[class_type] = self._where(class_type)
        return missing

    def require(self, class_types: Iterable[str]) -> None:
        """Raise ``WorkerNodeMissing`` naming every class the worker would lack."""
        missing = self.unprovided(class_types)
        if missing:
            raise WorkerNodeMissing(missing)


class WorkerNodeMissing(ValueError):
    """A compiled workflow uses classes the provisioned worker will not have."""

    def __init__(self, missing: Mapping[str, str]) -> None:
        self.missing = dict(missing)
        detail = "; ".join(f"{name} ({reason})" for name, reason in self.missing.items())
        super().__init__(
            "WORKER_NODE_MISSING: the workflow uses node classes the provisioned worker will not "
            "have: " + detail + ". Add the node's pack to the worker recipe or to the workflow's "
            "declared nodes before claiming a machine")


def worker_node_set(recipe: Any, declared_nodes: Iterable[Mapping[str, str]] = ()) -> WorkerNodeSet:
    """Build the set from a loaded engine recipe and a workflow's declared node packs."""
    recipe_packs = tuple((_slug(source.pin.repository), source.pin.revision) for source in recipe.sources
                         if "/custom_nodes/" in "/" + source.relative_path)
    comfy = next((source.pin.revision for source in recipe.sources if source.name == "comfyui"), None)
    return WorkerNodeSet(
        owned=owned_node_classes(),
        recipe_packs=recipe_packs,
        declared_packs=tuple((_slug(str(node["repo"])), str(node.get("commit") or ""))
                             for node in declared_nodes if node.get("repo")),
        core_commit=comfy,
        required=frozenset(recipe.health.required_nodes),
        has_owned_source=any(source.name == OWNED_SOURCE for source in recipe.sources),
    )


def managed_worker_node_set(declared_nodes: Iterable[Mapping[str, str]] = ()) -> WorkerNodeSet:
    """The set for the shipped managed-engine recipe (the one a RunPod worker is provisioned from)."""
    from .engine_local_endpoint import DEFAULT_ENGINE, DEFAULT_PLATFORM
    from .engine_recipe import load_recipe

    return worker_node_set(load_recipe(DEFAULT_ENGINE, DEFAULT_PLATFORM), declared_nodes)
