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
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

_CORE_PACKS = frozenset({"comfycore", "comfyextras"})
_OWNED_PACK = "vibecomfy"
OWNED_SOURCE = "vibecomfy"


def _token(name: str) -> str:
    """Compare pack names across repository and cache spellings."""
    name = name.removesuffix(".git").lower()
    name = re.sub(r"[^a-z0-9]", "", name)
    return name.removeprefix("comfyui") or name


def _repo_token(repository: str) -> str:
    return _token(urlsplit(repository).path.rstrip("/").rsplit("/", 1)[-1])


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


@dataclass(frozen=True)
class WorkerNodeSet:
    """What one recipe, plus the packs a workflow declares, gives the worker."""

    owned: frozenset[str]
    pack_tokens: frozenset[str]
    required: frozenset[str]
    has_owned_source: bool

    @staticmethod
    def _cache_pack(class_type: str) -> str | None:
        """The pack VibeComfy's shipped schema cache places a class in, through its public API."""
        from vibecomfy.porting.object_info.consume import get_class

        entry = get_class(class_type)
        pack = (entry or {}).get("pack_slug") or (entry or {}).get("pack")
        return str(pack) if pack else None

    def unprovided(self, class_types: Iterable[str]) -> dict[str, str]:
        """Map each class the worker would not have to the plain reason why."""
        missing: dict[str, str] = {}
        for class_type in sorted(set(class_types)):
            if class_type in self.owned:
                if not self.has_owned_source:
                    missing[class_type] = ("a VibeComfy-owned node, but the worker recipe has no "
                                           f"{OWNED_SOURCE!r} source to link it from")
                continue
            if class_type in self.required:
                continue
            pack = self._cache_pack(class_type)
            if pack is None:
                missing[class_type] = ("not a ComfyUI core node, not VibeComfy-owned, and in no "
                                       "pack the worker recipe or the workflow declares")
                continue
            token = _token(pack)
            if token == _OWNED_PACK:
                missing[class_type] = ("an old VibeComfy node class that its node package no "
                                       "longer registers")
            elif token in _CORE_PACKS:
                continue
            elif not any(token == wanted or (len(wanted) >= 4 and (wanted in token or token in wanted))
                         for wanted in self.pack_tokens):
                missing[class_type] = (f"provided by node pack {pack!r}, which the worker recipe does "
                                       "not install and the workflow does not declare")
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
    tokens = {_repo_token(source.pin.repository) for source in recipe.sources
              if "/custom_nodes/" in "/" + source.relative_path}
    tokens |= {_repo_token(str(node["repo"])) for node in declared_nodes if node.get("repo")}
    return WorkerNodeSet(
        owned=owned_node_classes(),
        pack_tokens=frozenset(token for token in tokens if token),
        required=frozenset(recipe.health.required_nodes),
        has_owned_source=any(source.name == OWNED_SOURCE for source in recipe.sources),
    )


def managed_worker_node_set(declared_nodes: Iterable[Mapping[str, str]] = ()) -> WorkerNodeSet:
    """The set for the shipped managed-engine recipe (the one a RunPod worker is provisioned from)."""
    from .engine_local_endpoint import DEFAULT_ENGINE, DEFAULT_PLATFORM
    from .engine_recipe import load_recipe

    return worker_node_set(load_recipe(DEFAULT_ENGINE, DEFAULT_PLATFORM), declared_nodes)
