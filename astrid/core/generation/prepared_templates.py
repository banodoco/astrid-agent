"""Disposable ComfyUI preparation; current source/target authority stays outside it.

The source adapter admits only a closed declarative repository builder after
the ordinary pin/source checks. It supplies current compiler/source/role/raw
node-response witnesses on every use. A locator only selects a candidate.
Existing binders still apply every request value, and the canonical emitter
still validates and publishes every current Python/companion pair. This module
neither stores approval nor changes a target, install pin, or execution receipt.

Cache I/O is isolated from preparation: a failed read/write/lock is reported
as ``miss/store_unreadable`` and cannot swallow a product validation error.
The only persisted object is VibeComfy's closed, digest-checked JSON recipe.
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from astrid.core.foundation.atomic_io import write_bytes_atomic
from astrid.core.pack import _filelock
from astrid.sdk.command_timing import cache_event, stage
from astrid.sdk.storage_root import resolve_runtime_data_root

logger = logging.getLogger(__name__)
_CACHE = "prepared_template"
_HINT_FORMAT = "astrid.prepared-template-hint/v1"
_MAX_ENTRY_BYTES = 32 * 1024 * 1024


def _event(outcome: str, reason: str | None = None) -> None:
    cache_event(_CACHE, outcome, reason)


def _recompute(reason: str, *, detail: str | None = None) -> None:
    _event("miss", reason)
    _event("recompute", reason)
    if detail:
        logger.debug("prepared template full preparation: %s (%s)", detail, reason)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _hex_key(value: Any) -> str:
    if (not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:")
            or any(c not in "0123456789abcdef" for c in value[7:])):
        raise ValueError("invalid prepared-template content key")
    return value[7:]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for name, item in pairs:
        if name in value:
            raise ValueError("duplicate prepared-template hint field")
        value[name] = item
    return value


class StoreUnavailable(OSError):
    """Only a cache filesystem operation failed, never product preparation."""


@dataclass(frozen=True)
class Lookup:
    template: Any = None
    reason: str | None = None


class PreparedTemplateStore:
    """One install-data-root store; no install/package/project writes."""

    def __init__(self, data_root: Path | None = None) -> None:
        root = resolve_runtime_data_root() if data_root is None else data_root
        if root is None:
            raise ValueError("prepared-template store requires a runtime data root")
        self.root = Path(root) / "cache" / "prepared-template" / "v1" / "comfyui"

    @staticmethod
    def locator(identity: Any) -> str:
        return hashlib.sha256(_json_bytes(identity)).hexdigest()

    def _path(self, group: str, name: str, suffix: str) -> Path:
        if len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
            raise ValueError("invalid prepared-template locator")
        return self.root / group / (name + suffix)

    def _check_path(self, path: Path) -> None:
        # A disposable hint must not redirect cache I/O through a linked tree.
        for part in (path, *path.parents):
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise StoreUnavailable(f"linked prepared-template cache path: {part}")

    def _read(self, path: Path, limit: int) -> bytes:
        self._check_path(path)
        fd = os.open(path, os.O_RDONLY | _filelock.nofollow_flag(path))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise StoreUnavailable("prepared-template entry is not a regular file")
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("prepared-template entry exceeds its byte bound")
        return data

    def lookup(self, locator: str) -> Lookup:
        from vibecomfy.prepared_template import PreparedTemplate

        try:
            hint = json.loads(self._read(self._path("sources", locator, ".json"), 4096),
                              object_pairs_hook=_unique_object)
            if not isinstance(hint, dict) or set(hint) != {"format", "key"} or hint["format"] != _HINT_FORMAT:
                raise ValueError("invalid prepared-template hint")
            key = _hex_key(hint["key"])
            template = PreparedTemplate.from_bytes(
                self._read(self._path("objects", key, ".json"), _MAX_ENTRY_BYTES))
            if template.key != hint["key"]:
                raise ValueError("prepared-template hint names another object")
            return Lookup(template=template)
        except FileNotFoundError:
            return Lookup(reason="absent")
        except OSError:
            return Lookup(reason="store_unreadable")
        except (ValueError, UnicodeError, RecursionError):
            return Lookup(reason="entry_invalid")

    def publish(self, locator: str, template: Any) -> None:
        """Publish object before hint, each by the existing atomic writer."""
        object_path = self._path("objects", _hex_key(template.key), ".json")
        hint_path = self._path("sources", locator, ".json")
        try:
            self._check_path(object_path)
            self._check_path(hint_path)
            write_bytes_atomic(object_path, template.to_bytes())
            write_bytes_atomic(hint_path, _json_bytes({"format": _HINT_FORMAT, "key": template.key}))
        except OSError as exc:
            raise StoreUnavailable("prepared-template publication failed") from exc

    @contextmanager
    def locked(self, locator: str) -> Iterator[bool]:
        """Use the existing portable advisory locks; release before approval."""
        fd: int | None = None
        acquired = False
        try:
            path = self._path("locks", locator, ".lock")
            try:
                self._check_path(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT | _filelock.nofollow_flag(path), 0o600)
                _filelock.lock_fd(fd)
                acquired = True
            except OSError:
                _event("miss", "store_unreadable")
            # The caller's build/refusal is outside every cache exception handler.
            yield acquired
        finally:
            if fd is not None:
                if acquired:
                    try:
                        _filelock.unlock_fd(fd)
                    except OSError:
                        _event("miss", "store_unreadable")
                try:
                    os.close(fd)
                except OSError:
                    _event("miss", "store_unreadable")


@dataclass(frozen=True)
class NodeListCapture:
    """A trusted transport's one response, retained only within this request.

    ``observed`` is the parsed object returned by that same read. It is an
    identity witness for the caller, not a cache object or a wire descriptor.
    An overlay is described separately in the role/custody key inputs.
    """

    raw: bytes
    observed: Mapping[str, Any]
    endpoint: str

    def digest(self) -> str:
        from vibecomfy.prepared_template import node_list_digest
        return node_list_digest(self.raw)


def current_node_capture(client: Any, observed: Any) -> NodeListCapture | None:
    """Never synthesize raw bytes from a parsed object or fetch for a key."""
    capture = getattr(client, "node_list_capture", None)
    if isinstance(capture, NodeListCapture) and capture.observed is observed:
        return capture
    return None


def _first_change(previous: Mapping[str, Any], current: Mapping[str, Any]) -> str | None:
    from vibecomfy.prepared_template import KEY_FIELDS
    for name in KEY_FIELDS[3:]:
        if _json_bytes(previous[name]) != _json_bytes(current[name]):
            return "key_changed:" + name
    return None


@dataclass
class PreparedUse:
    """Explicit request-local handoff to the emitter; never attached to the IR."""

    workflow: Any
    filled: Any = None
    store: PreparedTemplateStore | None = None
    locator: str | None = None
    witness: Callable[[], Mapping[str, Any]] | None = None
    context: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    @property
    def emission_kwargs(self) -> dict[str, Any]:
        return {} if self.filled is None else {"emission_session": self.filled.emission}

    def published(self) -> None:
        """Save learned layouts only after current-byte publication succeeded."""
        if self.filled is None or self.store is None or self.locator is None or self.witness is None:
            return
        with stage("prepared template key"):
            current = self.witness()
            changed = _first_change(self.context, current)
        if changed is not None:
            _event("miss", changed)
            return
        with stage("prepared template store"):
            with self.store.locked(self.locator) as locked:
                if not locked:
                    return
                # Recheck inside the writer lock; a preparation raced with a
                # changed source must not overwrite the new source's hint.
                with stage("prepared template key"):
                    changed = _first_change(self.context, self.witness())
                if changed is not None:
                    _event("miss", changed)
                    return
                try:
                    self.store.publish(self.locator, self.filled.snapshot())
                except StoreUnavailable:
                    _event("miss", "store_unreadable")


def _layout_event(event: Any) -> None:
    _event(event.outcome, event.reason)


def prepare_workflow(*, identity: Any, witness: Callable[[], Mapping[str, Any]],
                     build: Callable[[], Any], slots: Sequence[Any] = (),
                     values: Mapping[str, Any] | None = None,
                     admit_hit: Callable[[], None] | None = None,
                     store: PreparedTemplateStore | None = None,
                     reusable: bool = True, detail: str | None = None,
                     current_template: Any = None) -> PreparedUse:
    """One full-preparation miss path and one detached fill, with fresh witnesses.

    Source callers first prove a closed builder and pass no bound values. Their
    existing request binder mutates the returned detached workflow afterwards.
    Already-built callers provide ``current_template`` to compare its freshly
    computed masked structure; they may reuse layout, never yesterday's graph.
    ``admit_hit`` retains the source loader's confirmation gate on a skipped
    builder. Product errors from that gate or ``build`` are never caught here.
    """
    from vibecomfy.prepared_template import (
        TemplateNotReusable, fill_template, key_record, prepare_template,
    )

    def full(reason: str, *, counted: bool = False) -> Any:
        if counted:
            _event("recompute", reason)
        else:
            _recompute(reason, detail=detail)
        with stage("workflow build"):
            return build()

    if not reusable:
        return PreparedUse(full("entry_invalid"), reason="entry_invalid")
    with stage("prepared template key"):
        context = dict(witness())
        locator = PreparedTemplateStore.locator(identity)
    store = store if store is not None else PreparedTemplateStore()
    values = {} if values is None else values
    with stage("prepared template store"):
        with store.locked(locator) as locked:
            lookup = store.lookup(locator) if locked else Lookup(reason="store_unreadable")
            reason = lookup.reason
            template = lookup.template
            if template is not None:
                try:
                    expected_slots = [slot.record() for slot in slots]
                    if template.to_record()["payload"]["slots"] != expected_slots:
                        raise TemplateNotReusable("current slot contract differs", reason="key_changed:structure")
                    with stage("prepared template key"):
                        # from_bytes already reconstructed and checked structure
                        # from the recipe, not from the hint or claimed key.
                        current = key_record(structure=(current_template.structure_digest
                                                       if current_template is not None
                                                       else template.structure_digest), **context)
                    with stage("prepared template fill"):
                        filled = fill_template(template, values, current_key_inputs=current,
                                               on_layout_event=_layout_event)
                except TemplateNotReusable as exc:
                    reason = exc.reason
                else:
                    with stage("prepared template key"):
                        reason = _first_change(context, witness())
                    if reason is None:
                        if admit_hit is not None:
                            admit_hit()
                        _event("hit")
                        return PreparedUse(filled.workflow, filled, store, locator, witness, context)
            reason = reason or "entry_invalid"
            workflow = full(reason, counted=not locked)
            try:
                with stage("prepared template fill"):
                    template = current_template if current_template is not None else prepare_template(
                        workflow, slots, key_context=context)
                    filled = fill_template(template, values, current_key_inputs=template.key_inputs,
                                           on_layout_event=_layout_event)
            except TemplateNotReusable as exc:
                # A representational limitation is explicit; the successful
                # original preparation remains the product's miss path.
                _event("miss", exc.reason)
                logger.debug("prepared template unsupported: %s: %s", detail, exc)
                return PreparedUse(workflow, reason=exc.reason)
            with stage("prepared template key"):
                changed = _first_change(context, witness())
            if changed is not None:
                _event("miss", changed)
                return PreparedUse(workflow, reason=changed)
            return PreparedUse(filled.workflow, filled, store if locked else None,
                               locator, witness, context, reason)


def emit_prepared(emit: Callable[..., Any], workflow: Any, *args: Any,
                  prepared: PreparedUse | None = None, **kwargs: Any) -> Any:
    """Thread layout reuse through the existing emitter, then publish its data."""
    from vibecomfy.prepared_template import TemplateNotReusable
    if prepared is not None:
        kwargs.update(prepared.emission_kwargs)
    try:
        result = emit(workflow, *args, **kwargs)
    except TemplateNotReusable as exc:
        if "emission_session" not in kwargs:
            raise
        _recompute(exc.reason, detail=str(exc))
        kwargs.pop("emission_session")
        # Same renderer and current workflow; only unusable plan data is removed.
        return emit(workflow, *args, **kwargs)
    if prepared is not None:
        prepared.published()
    return result


@dataclass
class PreparationSession:
    """Explicit output parameter where a historical API returns an IR tuple."""

    use: PreparedUse | None = None

    def emit(self, emit: Callable[..., Any], workflow: Any, *args: Any, **kwargs: Any) -> Any:
        return emit_prepared(emit, workflow, *args, prepared=self.use, **kwargs)


def declarative_source(data: bytes) -> bool:
    """Conservative closed-builder check; executable hooks always build afresh.

    This is an optimization eligibility check, not a Python sandbox. Only
    constructors from the witnessed VibeComfy package, constant declarations,
    and straight-line workflow assembly are accepted. Unknown source shapes
    remain ordinary builds with explicit counters, including future templates.
    """
    try:
        module = ast.parse(data)
    except (SyntaxError, UnicodeError):
        return False
    constructors: set[str] = set()
    constants: set[str] = {"__file__"}
    builders: list[ast.FunctionDef] = []

    def expression(node: ast.AST, names: set[str], workflows: set[str]) -> bool:
        if isinstance(node, ast.Constant):
            return node.value is None or type(node.value) in (str, int, float, bool)
        if isinstance(node, ast.Name):
            return node.id in names
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return all(expression(x, names, workflows) for x in node.elts)
        if isinstance(node, ast.Dict):
            return all(k is not None and expression(k, names, workflows) and expression(v, names, workflows)
                       for k, v in zip(node.keys, node.values))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float)
        if isinstance(node, ast.Subscript):
            return expression(node.value, names, workflows) and expression(node.slice, names, workflows)
        if isinstance(node, ast.Call):
            function = node.func
            allowed = isinstance(function, ast.Name) and function.id in constructors
            if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
                owner = function.value.id
                allowed = ((owner in constructors and function.attr == "build")
                           or (owner in workflows and function.attr in {
                               "finalize", "bind_input", "register_input", "register_output", "add_edge",
                           }))
            return (allowed and all(expression(arg, names, workflows) for arg in node.args)
                    and all(kw.arg is not None and expression(kw.value, names, workflows) for kw in node.keywords))
        return False

    for node in module.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.ImportFrom):
            if node.level or node.module is None:
                return False
            if node.module == "__future__":
                if any(alias.name != "annotations" for alias in node.names):
                    return False
                continue
            if node.module != "vibecomfy.templates" and not node.module.startswith("vibecomfy.nodes."):
                return False
            for alias in node.names:
                if alias.name == "*" or alias.name.startswith("_"):
                    return False
                constructors.add(alias.asname or alias.name)
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if not expression(node.value, constants, set()):
                return False
            constants.add(node.targets[0].id)
            continue
        if isinstance(node, ast.FunctionDef) and node.name == "build":
            if (node.decorator_list or node.args.args or node.args.posonlyargs or node.args.kwonlyargs
                    or node.args.vararg or node.args.kwarg):
                return False
            builders.append(node)
            continue
        return False
    if len(builders) != 1:
        return False
    names, workflows = set(constants), set()
    returned = False
    for node in builders[0].body:
        if returned:
            return False
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if not expression(node.value, names, workflows):
                return False
            target = node.targets[0].id
            names.add(target)
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "new_workflow":
                workflows.add(target)
        elif isinstance(node, ast.Expr) and expression(node.value, names, workflows):
            continue
        elif isinstance(node, ast.Return) and node.value is not None and expression(node.value, names, workflows):
            returned = True
        else:
            return False
    return returned


def source_witness(path: Path, *, identity: Any, companion_required: bool = False) -> dict[str, Any]:
    """Read current bytes, including explicit absence of optional members."""
    from astrid.packs.vibecomfy.production_engine import workflow_members
    members = []
    for name, (member, required) in workflow_members(path).items():
        if name == "companion" and not companion_required:
            required = False  # Repository builders need not be materialized pairs.
        try:
            content = member.read_bytes()
        except FileNotFoundError:
            if required:
                raise
            members.append([name, "absent"])
        else:
            members.append([name, _digest(content)])
    return {"identity": identity, "members": members}


def compiler_witness() -> dict[str, Any]:
    """Hash current compiler/data bytes; no dirty label or persistent mtime cache.

    P82-06's install stamp is not assumed. The complete VibeComfy package source
    and JSON/lock data plus this Agent binding closure are read on every key
    observation. The lane measures the explicit key stage against 0.1 s warm.
    """
    import vibecomfy
    vc_root = Path(vibecomfy.__file__).resolve().parent
    agent_root = Path(__file__).resolve().parents[2]
    agent_paths = (
        "core/generation/prepared_templates.py", "core/generation/entries.py",
        "core/generation/backends/base.py", "core/generation/backends/vibecomfy.py",
        "core/generation/backends/comfy_target.py", "core/generation/backends/comfy_schema.py",
        "core/generation/target_inventory.py", "core/generation/asset_gaps.py",
        "packs/vibecomfy/production_engine.py", "packs/vibecomfy/local_server_engine.py",
        "sdk/generation.py",
    )
    records = [["agent/" + name, _digest((agent_root / name).read_bytes())] for name in agent_paths]
    for path in sorted(vc_root.rglob("*")):
        if path.suffix in {".py", ".json", ".lock"} and path.is_file():
            records.append(["vibecomfy/" + path.relative_to(vc_root).as_posix(), _digest(path.read_bytes())])
    for name in ("custom_nodes.lock", "node_index.json", "object_info.json"):
        path = vc_root.parent / name
        records.append([name, _digest(path.read_bytes()) if path.is_file() else "absent"])
    return {"closure": _digest(_json_bytes(records)), "python": list(sys.version_info[:3]),
            "implementation": sys.implementation.name, "cache_tag": sys.implementation.cache_tag}


def key_context(*, source: Mapping[str, Any], role_plan: Mapping[str, Any],
                capture: NodeListCapture) -> dict[str, Any]:
    from importlib.metadata import version
    return {"compiler_identity": compiler_witness(), "source_provenance": dict(source),
            "vibecomfy_version": version("vibecomfy"), "role_plan": dict(role_plan),
            "node_list_digest": capture.digest()}
