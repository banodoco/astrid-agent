"""One resolver from a workflow's classes to the provider of each node.

Every class a compiled workflow uses comes from exactly one of:

* ``core``  - ComfyUI itself (its schema evidence, or the recipe's required nodes);
* ``pack``  - a node pack in ``custom_nodes.lock`` (exact repository and commit);
* ``owned`` - VibeComfy's own ComfyUI node package, which the worker links into
  ``custom_nodes`` from the VibeComfy revision the recipe pins.

A class no provider offers is refused by name, and nothing else is.  The same plan
drives the real run (install each provider, then make the running engine show every
class, restarting it once if something was installed after it started) and the dry
run (print the plan, change nothing), so a free check covers the paid path.

The worker's lock and its owned class list are read at the recipe's pinned VibeComfy
revision, never from whatever VibeComfy the host happens to import.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_run = subprocess.run  # bound at import: a caller patching subprocess.run must not change pin reads
OWNED_SOURCE = "vibecomfy"
OWNED_PACK = "vibecomfy"
_CORE_PACKS = ("comfy_core", "comfy_extras")


@dataclass(frozen=True)
class Provider:
    kind: str                     # "core" | "pack" | "owned"
    name: str
    repo: str | None = None
    commit: str | None = None
    provisioned: bool = False     # the recipe already installs it; nothing to do

    def describe(self) -> str:
        if self.kind == "owned":
            return f"link VibeComfy nodes (pin {(self.commit or 'unknown')[:8]})"
        if self.provisioned:
            return f"pack {self.name} at {(self.commit or '')[:8]} is provisioned by the recipe"
        return f"install pack {self.name} at commit {(self.commit or '')[:8]}"


class NodeProviderMissing(ValueError):
    """Classes no provider offers."""

    def __init__(self, missing: Mapping[str, str]) -> None:
        self.missing = dict(missing)
        super().__init__("NODE_PROVIDER_MISSING: no provider for node class(es) "
                         + "; ".join(f"{name} ({why})" for name, why in self.missing.items())
                         + ". Add its pack to custom_nodes.lock before claiming a machine")


@dataclass(frozen=True)
class NodePlan:
    """What a target needs done so its engine has every class of one workflow."""

    providers: Mapping[str, Provider] = field(default_factory=dict)
    refused: Mapping[str, str] = field(default_factory=dict)

    @property
    def classes(self) -> tuple[str, ...]:
        return tuple(sorted({*self.providers, *self.refused}))

    @property
    def owned_classes(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, p in self.providers.items() if p.kind == "owned"))

    def packs(self, *, pending_only: bool = True) -> tuple[Provider, ...]:
        found = {p.name: p for p in self.providers.values()
                 if p.kind == "pack" and not (pending_only and p.provisioned)}
        return tuple(found[name] for name in sorted(found))

    def steps(self) -> list[str]:
        """The plain-language plan, in the order a real run does it."""
        actions = []
        owned = next((p for p in self.providers.values() if p.kind == "owned"), None)
        if owned is not None:
            actions.append(owned.describe())
        actions += [p.describe() for p in self.packs()]
        steps = list(actions)
        steps += [p.describe() for p in self.packs(pending_only=False) if p.provisioned]
        if actions:
            steps.append("restart the engine if it started before these were installed")
        steps.append(f"check the engine shows all {len(self.classes)} node classes")
        return steps

    def require(self) -> None:
        if self.refused:
            raise NodeProviderMissing(self.refused)


@dataclass(frozen=True)
class ProviderSource:
    """Where classes come from for one target."""

    lock: tuple[Any, ...] = ()                       # vibecomfy LockEntry rows
    owned_classes: frozenset[str] = frozenset()
    owned_pin: str | None = None
    owned_note: str | None = None                    # why the owned list is unavailable
    core_classes: frozenset[str] = frozenset()
    provisioned: frozenset[tuple[str, str]] = frozenset()   # (normalized repo, commit)
    # An attached ComfyUI is the person's own: packs come from this lookup (class -> packs) and a
    # class nothing provides is taken to be part of their ComfyUI, as the readiness text has
    # always said. Managed workers leave both unset and refuse such a class by name.
    pack_lookup: Any = None
    unknown_is_core: bool = False


def _repo_key(url: str | None) -> str:
    return (url or "").rstrip("/").removesuffix(".git").lower()


def _is_core(class_type: str, source: ProviderSource) -> bool:
    if class_type in source.core_classes:
        return True
    from vibecomfy.porting.object_info.consume import get_class

    entry = get_class(class_type)
    return bool(entry) and (entry.get("pack_slug") or entry.get("pack")) in _CORE_PACKS


def _slug(repository: str) -> str:
    """A node pack's id: its repository name, exactly as the schema captures name it."""
    return repository.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def declared_packs(workflow: Any) -> tuple[tuple[str, str, str], ...]:
    """The (pack id, repository, commit) rows a workflow itself declares it needs."""
    metadata = getattr(workflow, "metadata", None) or {}
    refs = [*((metadata.get("requirements") or {}).get("custom_node_refs") or ()),
            *(metadata.get("custom_node_packs") or {}).values()]
    rows = {}
    for ref in refs:
        repo, commit = ref.get("url") or ref.get("repository"), ref.get("commit") or ref.get("revision")
        if repo and commit:
            rows.setdefault(_repo_key(repo), (_slug(repo), repo, commit))
    return tuple(rows[key] for key in sorted(rows))


def _declared_pack_has(class_type: str, slug: str, commit: str) -> bool:
    """Whether VibeComfy's shipped schema evidence places the class in this declared pack.

    Pack ids are compared exactly; evidence captured at the declared commit is preferred, and
    evidence for the same pack id at another commit is accepted when none exists at that one.
    """
    from vibecomfy.errors import ObjectInfoIdentityAmbiguityError
    from vibecomfy.porting.object_info.consume import get_class, has_class_identity

    try:
        if has_class_identity(class_type, pack_slug=slug, git_commit=commit):
            return True
    except ObjectInfoIdentityAmbiguityError:
        return True
    entry = get_class(class_type)
    return bool(entry) and entry.get("pack_slug") == slug


def resolve(class_types: Iterable[str], source: ProviderSource, declared: Iterable[tuple[str, str, str]] = ()) -> NodePlan:
    declared = tuple(declared)
    by_class: dict[str, Any] = {}
    for entry in source.lock:
        for name in entry.class_set:
            by_class.setdefault(name, entry)
    providers: dict[str, Provider] = {}
    refused: dict[str, str] = {}
    for class_type in sorted({str(c) for c in class_types}):
        found = (source.pack_lookup({class_type}) or [None])[0] if source.pack_lookup else None
        if class_type in source.owned_classes:
            providers[class_type] = Provider("owned", OWNED_PACK, commit=source.owned_pin)
        elif _is_core(class_type, source) and found is None:
            providers[class_type] = Provider("core", "ComfyUI")   # what ComfyUI itself ships wins over a pack
        elif found is not None:
            providers[class_type] = Provider("pack", found.name, repo=found.repo)
        elif class_type in by_class:
            entry = by_class[class_type]
            repo = entry.url or entry.path
            if not repo or not entry.commit:
                refused[class_type] = f"pack {entry.name} is in the lock without a repository and commit"
                continue
            providers[class_type] = Provider(
                "pack", entry.name, repo=repo, commit=entry.commit,
                provisioned=(_repo_key(repo), entry.commit) in source.provisioned)
        elif (hit := next((row for row in declared if _declared_pack_has(class_type, row[0], row[2])), None)):
            providers[class_type] = Provider(
                "pack", hit[0], repo=hit[1], commit=hit[2],
                provisioned=(_repo_key(hit[1]), hit[2]) in source.provisioned)
        elif source.unknown_is_core or _is_core(class_type, source):
            providers[class_type] = Provider("core", "ComfyUI")
        elif class_type.lower().startswith(("vibecomfy", "vibecomfy.")) and source.owned_note:
            refused[class_type] = f"a VibeComfy node, but {source.owned_note}"
        else:
            refused[class_type] = "not ComfyUI core, not in custom_nodes.lock, and not a VibeComfy node"
    return NodePlan(providers, refused)


# --- the managed worker's source, read at the recipe's pins ---------------------------------

def _host_vibecomfy_checkout() -> Path | None:
    import vibecomfy

    root = Path(vibecomfy.__file__).resolve().parents[1]
    return root if (root / ".git").exists() else None


def _git(checkout: Path, *argv: str, binary: bool = False) -> Any:
    done = _run(["git", "-C", str(checkout), *argv], capture_output=True, check=True, timeout=120)
    return done.stdout if binary else done.stdout.decode("utf-8")


def _sha(value: str) -> str:
    return value.removeprefix("sha256:")


@lru_cache(maxsize=8)
def _owned_at_pin(pin: str) -> tuple[frozenset[str], str | None]:
    """The classes VibeComfy's node package registers at ``pin``, and why not if unreadable."""
    checkout = _host_vibecomfy_checkout()
    try:
        if checkout is None:
            raise RuntimeError("the host's VibeComfy is not a git checkout")
        head = _git(checkout, "rev-parse", "HEAD").strip()
        with tempfile.TemporaryDirectory(prefix="astrid-vibecomfy-pin-") as scratch:
            root = checkout
            if head != pin:
                archive = _git(checkout, "archive", pin, "vibecomfy", binary=True)
                _run(["tar", "-x", "-C", scratch], input=archive, check=True, timeout=120)
                root = Path(scratch)
            program = ("import sys,json;sys.path.insert(0,sys.argv[1]);"
                       "import vibecomfy.comfy_nodes as m;print(json.dumps(sorted(m.NODE_CLASS_MAPPINGS)))")
            done = _run([sys.executable, "-I", "-c", program, str(root)], capture_output=True,
                                  timeout=120, env={**os.environ, "VIBECOMFY_HEADLESS": "1"}, check=True)
        import json
        return frozenset(json.loads(done.stdout)), None
    except Exception as exc:  # noqa: BLE001 - reported as the plan's reason, never swallowed silently
        return frozenset(), f"its node list at pin {pin[:8]} could not be read ({type(exc).__name__})"


@lru_cache(maxsize=8)
def _lock_at_pin(pin: str, sha256: str, path: str) -> tuple[Any, ...]:
    from vibecomfy.node_packs import read_lockfile

    checkout = _host_vibecomfy_checkout()
    if checkout is None:
        raise RuntimeError("the host's VibeComfy is not a git checkout; cannot read the pinned custom_nodes.lock")
    data = _git(checkout, "show", f"{pin}:{path}", binary=True)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise RuntimeError(f"custom_nodes.lock at VibeComfy {pin[:8]} does not match the recipe's digest")
    with tempfile.TemporaryDirectory(prefix="astrid-pinned-lock-") as scratch:
        target = Path(scratch) / "custom_nodes.lock"
        target.write_bytes(data)
        return tuple(read_lockfile(target))


def recipe_source(recipe: Any) -> ProviderSource:
    """The provider source for a managed worker provisioned from ``recipe``."""
    vibe = next((s for s in recipe.sources if s.name == OWNED_SOURCE), None)
    node_lock = next((e.node_lock for e in recipe.environments if e.node_lock is not None), None)
    pin = vibe.pin.revision if vibe is not None else None
    lock: tuple[Any, ...] = ()
    if pin is not None and node_lock is not None:
        lock = _lock_at_pin(pin, _sha(node_lock.sha256), node_lock.path)
    owned, note = _owned_at_pin(pin) if pin is not None else (frozenset(), "the recipe has no vibecomfy source")
    provisioned = frozenset((_repo_key(s.pin.repository), s.pin.revision) for s in recipe.sources
                            if "/custom_nodes/" in "/" + s.relative_path)
    return ProviderSource(lock=lock, owned_classes=owned, owned_pin=pin, owned_note=note,
                          core_classes=_core_classes() | frozenset(recipe.health.required_nodes),
                          provisioned=provisioned)


@lru_cache(maxsize=1)
def _core_classes() -> frozenset[str]:
    from vibecomfy.node_packs import CORE_COMFY_CLASSES

    return frozenset(CORE_COMFY_CLASSES)


def attached_source(lockfile_path: Any = None) -> ProviderSource:
    """The provider source for an attached ComfyUI: the person installs; nothing is installed for them."""
    from vibecomfy.node_packs import resolve_node_packs

    lock = Path("custom_nodes.lock") if lockfile_path is None else lockfile_path
    try:
        owned = _host_owned()
    except Exception:  # noqa: BLE001
        owned = frozenset()
    return ProviderSource(owned_classes=owned,
                          pack_lookup=lambda classes: list(resolve_node_packs(set(classes), lockfile_path=lock)),
                          unknown_is_core=True)


def _host_owned() -> frozenset[str]:
    prior = os.environ.get("VIBECOMFY_HEADLESS")
    os.environ["VIBECOMFY_HEADLESS"] = "1"
    try:
        import importlib
        return frozenset(importlib.import_module("vibecomfy.comfy_nodes").NODE_CLASS_MAPPINGS)
    finally:
        if prior is None:
            os.environ.pop("VIBECOMFY_HEADLESS", None)
        else:
            os.environ["VIBECOMFY_HEADLESS"] = prior


def managed_source() -> ProviderSource:
    """The shipped managed-engine recipe (the one a RunPod worker is provisioned from)."""
    from .engine_local_endpoint import DEFAULT_ENGINE, DEFAULT_PLATFORM
    from .engine_recipe import load_recipe

    return recipe_source(load_recipe(DEFAULT_ENGINE, DEFAULT_PLATFORM))


def api_class_types(workflow: Any) -> frozenset[str]:
    """Every class a workflow sends to ComfyUI (its compiled API form)."""
    try:
        api = workflow.compile("api", run_inputs={})
        return frozenset(str(n["class_type"]) for n in api.values()
                         if isinstance(n, Mapping) and isinstance(n.get("class_type"), str))
    except Exception:  # noqa: BLE001 - an uncompilable graph still has runtime nodes to provide
        return frozenset(str(n.class_type) for n in workflow.runtime_nodes().values())
