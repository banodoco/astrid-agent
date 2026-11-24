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
_CONTENT_HASHES: dict[tuple[Any, ...], str] = {}


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
    """Current-user-owned data-root store, with the custody of template sources.

    Content digests detect damage, not forgery by that user. Refuse a store
    whose ownership/private permissions cannot be established; full preparation
    remains available on platforms without a current-user ownership primitive.
    """

    def __init__(self, data_root: Path | None = None) -> None:
        try:
            root = resolve_runtime_data_root() if data_root is None else data_root
        except (OSError, ValueError) as exc:
            raise StoreUnavailable("prepared-template data root is unavailable") from exc
        if root is None:
            raise StoreUnavailable("prepared-template store requires a runtime data root")
        self.data_root = Path(root).absolute()
        self.root = self.data_root / "cache" / "prepared-template" / "v1" / "comfyui"

    @staticmethod
    def _owner() -> int:
        current_user = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
        if current_user is None:
            raise StoreUnavailable("prepared-template current-user ownership is unavailable")
        return current_user()

    def _check_owned(self, info: os.stat_result, *, private: bool) -> None:
        if info.st_uid != self._owner():
            raise StoreUnavailable("prepared-template cache has a foreign owner")
        forbidden = 0o077 if private else 0o022
        if stat.S_IMODE(info.st_mode) & forbidden:
            raise StoreUnavailable("prepared-template cache permissions do not preserve owner custody")

    def _mkdir_private(self, path: Path) -> None:
        # Create missing ancestors one at a time: parents=True only applies its
        # mode to the leaf. Never chmod/adopt an existing shared cache tree.
        self._owner()
        self._check_path(path)
        missing = []
        parent = path
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
            self._check_path(directory)
        self._check_path(path)

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
            if part == self.data_root or self.data_root in part.parents:
                private_root = self.data_root / "cache" / "prepared-template"
                private = part == private_root or private_root in part.parents
                self._check_owned(info, private=private)
                if part != path and not stat.S_ISDIR(info.st_mode):
                    raise StoreUnavailable("prepared-template cache ancestor is not a directory")

    def _read(self, path: Path, limit: int) -> bytes:
        self._check_path(path)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | _filelock.nofollow_flag(path))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            self._check_owned(info, private=True)
            if not stat.S_ISREG(info.st_mode):
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
            self._mkdir_private(object_path.parent)
            self._mkdir_private(hint_path.parent)
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
                self._mkdir_private(path.parent)
                fd = os.open(path, os.O_RDWR | os.O_CREAT | _filelock.nofollow_flag(path), 0o600)
                info = os.fstat(fd)
                self._check_owned(info, private=True)
                if not stat.S_ISREG(info.st_mode):
                    raise StoreUnavailable("prepared-template lock is not a regular file")
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
        from vibecomfy.prepared_template import TemplateNotReusable
        if self.filled is None or self.store is None or self.locator is None or self.witness is None:
            return
        if self.filled.emission.no_store_reason is not None:
            # resolve() already reported the full/no-store path through its
            # event callback. Do not create or replace even a locator hint.
            return
        with stage("prepared template store"):
            with self.store.locked(self.locator) as locked:
                if not locked:
                    return
                # Recheck inside the writer lock; a preparation raced with a
                # changed source must not overwrite the new source's hint.
                try:
                    with stage("prepared template key"):
                        changed = _first_change(self.context, self.witness())
                except OSError:
                    _event("miss", "entry_invalid")
                    return
                if changed is not None:
                    _event("miss", changed)
                    return
                try:
                    self.store.publish(self.locator, self.filled.snapshot())
                except StoreUnavailable:
                    _event("miss", "store_unreadable")
                except TemplateNotReusable as exc:
                    _event("miss", exc.reason)


def _layout_event(event: Any) -> None:
    _event(event.outcome, event.reason)


def prepare_workflow(*, identity: Any, witness: Callable[[], Mapping[str, Any]],
                     build: Callable[[], Any], slots: Sequence[Any] = (),
                     values: Mapping[str, Any] | None = None,
                     admit_hit: Callable[[], None] | None = None,
                     store: PreparedTemplateStore | None = None,
                     reusable: bool = True, detail: str | None = None,
                     unavailable_reason: str = "entry_invalid",
                     current_template: Any = None,
                     initial_context: Mapping[str, Any] | None = None) -> PreparedUse:
    """One full-preparation miss path and one detached fill, with fresh witnesses.

    Source callers first prove a closed builder and pass no bound values. Their
    existing request binder mutates the returned detached workflow afterwards.
    Already-built callers provide ``current_template`` to compare its freshly
    computed masked structure; they may reuse layout, never yesterday's graph.
    ``admit_hit`` retains the source loader's confirmation gate on a skipped
    builder. Product errors from that gate or ``build`` are never caught here.
    """
    from vibecomfy.prepared_template import (
        TemplateNotReusable,
        fill_template,
        key_record,
        prepare_template,
    )

    def full(reason: str, *, counted: bool = False) -> Any:
        if counted:
            _event("recompute", reason)
        else:
            _recompute(reason, detail=detail)
        # Keep the SDK's established workflow-build parent tree; time only the
        # original source builder here, separately from key/store/fill work.
        with stage("source workflow build"):
            return build()

    if not reusable:
        return PreparedUse(full(unavailable_reason), reason=unavailable_reason)
    try:
        with stage("prepared template key"):
            # Detach the observed witness too: a later mutation of a caller's
            # nested role/source mapping must not rewrite our prior observation.
            context = json.loads(_json_bytes(dict(witness() if initial_context is None else initial_context)))
            locator = PreparedTemplateStore.locator(identity)
    except OSError as exc:
        logger.debug("prepared template current key is unavailable: %s", exc)
        return PreparedUse(full("entry_invalid"), reason="entry_invalid")
    try:
        with stage("prepared template store"):
            store = store if store is not None else PreparedTemplateStore()
    except StoreUnavailable:
        return PreparedUse(full("store_unreadable"), reason="store_unreadable")
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
                    try:
                        with stage("prepared template key"):
                            reason = _first_change(context, witness())
                    except OSError:
                        reason = "entry_invalid"
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
            try:
                with stage("prepared template key"):
                    changed = _first_change(context, witness())
            except OSError:
                changed = "entry_invalid"
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
    source_path: Path | None = None
    source_identity: Any = None
    validated_source: Mapping[str, Any] | None = None

    def retain_validated_source(self, path: Path) -> None:
        """Called only after the ordinary canonical loader accepts this pair.

        The SDK may be loading a frozen copy. Its original path is retained
        separately and must still match this accepted snapshot at lookup and
        publication; a snapshot's temporary filename is never source authority.
        """
        if self.source_path is None:
            self.source_path = path
        try:
            with stage("prepared template key"):
                self.validated_source = source_witness(path, identity=self.source_identity,
                                                       companion_required=True)
        except OSError:
            # Current canonical admission already succeeded; inability to
            # witness its cache closure only makes preparation non-reusable.
            self.validated_source = None

    def prepare_emission(self, workflow: Any, *, capture: NodeListCapture | None,
                         role_plan: Mapping[str, Any], reusable: bool = True) -> None:
        if not isinstance(capture, NodeListCapture):
            _recompute("node_list_unhashable", detail="no current raw target response")
            self.use = PreparedUse(workflow, reason="node_list_unhashable")
            return
        if not reusable or self.source_path is None or self.validated_source is None:
            _recompute("entry_invalid", detail="executable or unwitnessed source")
            self.use = PreparedUse(workflow, reason="entry_invalid")
            return
        from vibecomfy.schema.provider import NodeSchema
        if (workflow.metadata.get("_workflow_snapshot") is not None
                or any(isinstance(node.metadata.get("schema"), NodeSchema) for node in workflow.nodes.values())):
            # These two known live authorities are outside the submitted JSON
            # contract. Avoid hashing a compiler closure that cannot be used;
            # retain the objects intact for the ordinary current emitter.
            _recompute("entry_invalid", detail=f"{self.source_identity}: retained live schema/snapshot")
            self.use = PreparedUse(workflow, reason="entry_invalid")
            return
        source_path = self.source_path

        def witness() -> Mapping[str, Any]:
            current = source_witness(source_path, identity=self.source_identity,
                                     companion_required=True)
            return key_context(source=current, role_plan=role_plan, capture=capture)

        # The graph has already passed the current loader and request binder.
        # Compare its own masked structure; never let a cached graph replace it.
        from vibecomfy.prepared_template import TemplateNotReusable, prepare_template
        try:
            with stage("prepared template key"):
                context = dict(witness())
        except OSError:
            _recompute("entry_invalid", detail="accepted snapshot's current source is unavailable")
            self.use = PreparedUse(workflow, reason="entry_invalid")
            return
        with stage("prepared template key"):
            if context["source_provenance"] != self.validated_source:
                _recompute("key_changed:source_provenance")
                self.use = PreparedUse(workflow, reason="key_changed:source_provenance")
                return
            slots, values = _emission_values(workflow)
            try:
                template = prepare_template(workflow, slots, key_context=context)
            except TemplateNotReusable as exc:
                _recompute(exc.reason, detail=f"{self.source_identity}: {exc}")
                self.use = PreparedUse(workflow, reason=exc.reason)
                return
        self.use = prepare_workflow(identity={"emission": self.source_identity},
            witness=witness, build=lambda: workflow, slots=slots, values=values,
            current_template=template, initial_context=context)

    def emit(self, emit: Callable[..., Any], workflow: Any, *args: Any, **kwargs: Any) -> Any:
        return emit_prepared(emit, workflow, *args, prepared=self.use, **kwargs)


def _emission_values(workflow: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Mask current public input values and private output prefixes only.

    Authored defaults, omission, graph links and all other structure remain
    literal recipe data. Current schemas/live state are never stripped to make
    a recipe fit: the contract refuses them and the current emitter still runs.
    """
    from vibecomfy.prepared_template import ValueSlot
    values: dict[str, Any] = {}
    slots = []
    seen: set[tuple[Any, ...]] = set()

    def add(path: tuple[Any, ...], value: Any) -> None:
        if path in seen:
            return
        seen.add(path)
        name = _json_bytes(list(path)).decode("utf-8")
        slots.append(ValueSlot(name, (path,), "json", allow_none=True))
        values[name] = value

    linked = {(str(edge.to_node), edge.to_input) for edge in workflow.edges}
    for name, spec in workflow.inputs.items():
        add(("inputs", name, "value"), spec.value)
        node = workflow.nodes.get(str(spec.node_id))
        if node is None or (str(node.id), spec.field) in linked:
            continue
        for channel in ("inputs", "widgets"):
            fields = getattr(node, channel)
            if spec.field in fields:
                add(("nodes", str(node.id), channel, spec.field), fields[spec.field])
    for index, output in enumerate(workflow.outputs):
        add(("outputs", index, "filename_prefix"), output.filename_prefix)
        node = workflow.nodes.get(str(output.node_id))
        if node is not None and (str(node.id), "filename_prefix") not in linked:
            for channel in ("inputs", "widgets"):
                if "filename_prefix" in getattr(node, channel):
                    add(("nodes", str(node.id), channel, "filename_prefix"),
                        getattr(node, channel)["filename_prefix"])
    if "output_prefix" in workflow.metadata:
        add(("metadata", "output_prefix"), workflow.metadata["output_prefix"])
    return tuple(slots), values


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
                if node.module == "vibecomfy.templates" and alias.name not in {
                        "InputSpec", "ModelAsset", "ReadyMetadata", "new_workflow"}:
                    return False  # Other helper APIs may consult unwitnessed I/O.
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


def _file_digest_current(path: Path) -> str:
    """Only an in-process memo; changed dirty bytes get a new content hash."""
    info = path.stat()
    token = (str(path.resolve()), info.st_dev, info.st_ino, info.st_size,
             info.st_mtime_ns, info.st_ctime_ns)
    digest = _CONTENT_HASHES.get(token)
    if digest is None:
        digest = _digest(path.read_bytes())
        after = path.stat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError(f"compiler source changed during witness: {path}")
        if len(_CONTENT_HASHES) >= 16384:
            _CONTENT_HASHES.clear()
        _CONTENT_HASHES[token] = digest
    return digest


def compiler_witness() -> dict[str, Any]:
    """Hash current compiler/data bytes; no dirty label or persistent mtime cache.

    P82-06's install stamp is not assumed. The complete VibeComfy package source
    and JSON/lock data, selected ready-tree/index, authoring schemas and this
    Agent binding closure are witnessed on every key observation. A content
    hash may be memoized only in this process, by
    path/size/mtime plus inode/ctime; dirty changes never share a generic tag.
    The lane measures the explicit key stage against 0.1 s warm.
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
    records = [["agent/" + name, _file_digest_current(agent_root / name)] for name in agent_paths]
    for path in sorted(vc_root.rglob("*")):
        if (path.suffix in {".py", ".json", ".lock"} or path.name == "CURRENT") and path.is_file():
            records.append(["vibecomfy/" + path.relative_to(vc_root).as_posix(), _file_digest_current(path)])
    from vibecomfy import resources
    from vibecomfy.schema import get_authoring_schema_provider
    from vibecomfy.schema.cache import object_info_cache_candidates
    from vibecomfy.schema.provider import SourceSchemaProvider
    # resources selects one installed/check-out layout, never a mixture. Bind
    # its actual bytes, including checkout resources outside the package.
    for name in ("template_index", "coverage_manifest", "custom_nodes_lock", "strict_ready_exceptions", "comfy_metadata"):
        path = resources._resource_location(name)
        records.append(["resource/" + name, str(path.resolve()),
                        _file_digest_current(path) if path.is_file() else "absent"])
    ready_root = resources._resource_location("ready_templates")
    records.append(["ready-root", str(ready_root.resolve()), ready_root.is_dir()])
    if ready_root.is_dir():
        for path in sorted(ready_root.rglob("*")):
            if (path.suffix in {".py", ".json", ".lock"} or path.name == "CURRENT") and path.is_file():
                records.append(["ready-member", str(path.resolve()), _file_digest_current(path)])
    provider = get_authoring_schema_provider(on_demand_schemas=False)
    records.append(["node-index", str(provider.node_index_path.resolve()),
                    _file_digest_current(provider.node_index_path) if provider.node_index_path.is_file() else "absent"])
    roots = [provider.object_info_index_root]
    # Cache precedence depends on mtime. Keep that ordered path roster as well
    # as file content, without ever hashing a JSON re-serialization of the live
    # target response (which has its own raw-byte input).
    cache_paths = object_info_cache_candidates(provider.object_info_cache_dir)
    if provider.object_info_cache_path is not None:
        cache_paths = [provider.object_info_cache_path]
    records.append(["schema-cache-order", [str(path.resolve()) for path in cache_paths]])
    for path in cache_paths:
        records.append(["schema-cache", str(path.resolve()), _file_digest_current(path)])
    for item in provider._providers:
        if isinstance(item, SourceSchemaProvider):
            roots.extend(item.roots)
    for root in roots:
        root = Path(root)
        records.append(["schema-root", str(root.resolve()), root.is_dir()])
        if root.is_dir():
            for path in sorted(root.rglob("*")):
                if (path.suffix in {".py", ".json", ".lock"} or path.name == "CURRENT") and path.is_file():
                    records.append(["schema-member", str(path.resolve()), _file_digest_current(path)])
    return {"closure": _digest(_json_bytes(records)), "python": list(sys.version_info[:3]),
            "implementation": sys.implementation.name, "cache_tag": sys.implementation.cache_tag,
            "on_demand_schemas": os.environ.get("VIBECOMFY_ON_DEMAND_SCHEMAS", "1")}


def offline_source_schemas(data: bytes) -> bool:
    """Do not skip a builder that could consult an unwitnessed on-demand pack."""
    from vibecomfy.schema import get_authoring_schema_provider
    from vibecomfy.schema.provider import SchemaProviderError
    from vibecomfy.workflow_context import active_workflow
    if active_workflow() is not None:
        return False  # Preserve new_workflow's current nested-context refusal.
    provider = get_authoring_schema_provider(on_demand_schemas=False)
    for node in ast.parse(data).body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("vibecomfy.nodes."):
            for alias in node.names:
                try:
                    if provider.get_schema(alias.name) is None:
                        return False
                except SchemaProviderError:
                    return False
    return True


def key_context(*, source: Mapping[str, Any], role_plan: Mapping[str, Any],
                capture: NodeListCapture, source_is_builder: bool = False) -> dict[str, Any]:
    from importlib.metadata import PackageNotFoundError, version
    try:
        current_version = version("vibecomfy")
    except PackageNotFoundError as exc:
        # A source-only import can still take the original preparation path;
        # it cannot claim a distribution witness it does not possess.
        raise OSError("VibeComfy distribution version is unavailable") from exc
    compiler = compiler_witness()
    provenance = dict(source)
    if source_is_builder:
        # The cached structure is recipe integrity, not independent freshness.
        # Bind the complete supported builder/helper/schema/data closure and
        # its structural environment into source authority, without hashing it
        # twice. Extra executable imports/helpers are ineligible above.
        provenance["builder_closure"] = compiler
    return {"compiler_identity": compiler, "source_provenance": provenance,
            "vibecomfy_version": current_version, "role_plan": dict(role_plan),
            "node_list_digest": capture.digest()}
