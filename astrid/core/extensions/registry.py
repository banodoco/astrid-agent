"""The extension registry: one object that finds, validates, orders and loads every contribution (M-Core, X-4).

Discovery walks the pack folders (through ``discover_pack_metadata``) and the SDK folder, and parses
YAML only. Nothing is imported by ``load()``. A contribution's code is imported lazily by ``entry()``,
which also runs the protocol check. Every problem is kept and listed by ``problems()``; none is dropped.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    Contribution,
    ExtensionPoint,
    ManifestError,
    Quarantined,
    parse_contribution,
    parse_point,
)
from .refs import Ref, one_line
from .scopes import Root, contribution_roots, disabled_ids
from .sidecar import ManifestFile, iter_extension_manifests, iter_sdk_manifests

SCOPE_RANK = {"built-in": 0, "installed": 1, "user": 2, "project": 3}
RESERVED_OWNERS = ("timeline", "astrid")
SDK_OWNER = "timeline"
_DEFAULT_SDK_ROOT = Path(__file__).resolve().parents[2] / "sdk" / "timeline" / "extensions"


@dataclass(frozen=True)
class Shadowed:
    id: str
    point: str
    winner: str
    loser_scope: str
    loser_path: Path


@dataclass(frozen=True)
class Disabled:
    id: str
    point: str
    path: Path


class ExtensionUnavailable(Exception):
    """A contribution cannot be used: ``reason`` is quarantined | incompatible | disabled | unknown | custody."""

    def __init__(self, message: str, *, reason: str, fix: str):
        super().__init__(message)
        self.reason = reason
        self.fix = fix


@dataclass(frozen=True)
class _Pack:
    id: str
    root: Path
    version: str
    scope: str
    sdk: bool = False


@dataclass(frozen=True)
class _Entry:
    """One contribution manifest after parsing: either a Contribution or a Quarantined (never neither)."""

    point: str
    key: str
    id: str
    scope: str
    path: Path
    contribution: Contribution | None
    quarantine: Quarantined | None
    reason: str  # for quarantine: "incompatible" (api/compatibility) or "quarantined"


@dataclass(frozen=True)
class _Snapshot:
    points: Mapping[str, ExtensionPoint]
    point_problems: tuple[Quarantined, ...]
    entries: tuple[_Entry, ...]  # best first (scope, then pack id, then path)


class _LoadFailure(Exception):
    def __init__(self, what: str, fix: str):
        super().__init__(one_line(what))
        self.fix = one_line(fix)


def _split(exc: Exception) -> tuple[str, str]:
    """A ManifestError is ``<path>: <field>: <what>; <fix>``; split off the one-line fix."""
    text = one_line(exc)
    head, sep, fix = text.rpartition("; ")
    if not sep:
        return text, "fix the manifest at the path shown"
    return head, fix


def _quarantine(exc: Exception, cid: str, point: str | None, path: Path) -> Quarantined:
    error, fix = _split(exc)
    return Quarantined(id=cid, point=point, path=path, error=error, fix=fix)


_FIELD_RE = re.compile(r"^.*?: ([A-Za-z_][\w.-]*): ")


def _reason(q: Quarantined | None) -> str:
    """ "incompatible" when the failing field is api or compatibility (the field follows the path)."""
    if q is None:
        return ""
    match = _FIELD_RE.match(q.error)
    if match and match.group(1) in ("api", "compatibility"):
        return "incompatible"
    return "quarantined"


def _scope_of(pack_dir: Path, roots: Sequence[Root]) -> str:
    """The scope of the most specific root that contains the pack folder (``installed`` if none does)."""
    target = pack_dir.resolve()
    best: tuple[str, Path] | None = None
    for root in roots:
        try:
            base = root.path.resolve()
        except OSError:
            continue
        if target == base or target.is_relative_to(base):
            if best is None or len(base.parts) > len(best[1].parts):
                best = (root.scope, base)
    return best[0] if best else "installed"


def _parse(items: Sequence[tuple[ManifestFile, _Pack]]) -> _Snapshot:
    """Parse points first, then contributions. Only manifests are read; no contribution code runs."""
    ordered = sorted(items, key=lambda it: (-SCOPE_RANK[it[1].scope], it[1].id, str(it[0].path)))
    points: dict[str, ExtensionPoint] = {}
    point_problems: list[Quarantined] = []
    failed: set[str] = set()
    for m, pack in ordered:
        if m.kind != "point":
            continue
        guess = f"{pack.id}.{m.key}"
        if pack.id in RESERVED_OWNERS and not pack.sdk:
            point_problems.append(
                Quarantined(
                    id=guess,
                    point=None,
                    path=m.path,
                    error=f"{pack.id!r} is a reserved namespace; only the SDK declares its points",
                    fix="move the point into the pack under its own id, or declare it in the SDK",
                )
            )
            failed.add(guess)
            continue
        try:
            point = parse_point(m.path, owner=pack.id)
        except ManifestError as exc:
            point_problems.append(_quarantine(exc, guess, None, m.path))
            failed.add(guess)
            continue
        except Exception as exc:  # noqa: BLE001 - any other failure is still a loud quarantine
            point_problems.append(
                Quarantined(
                    id=guess,
                    point=None,
                    path=m.path,
                    error=one_line(f"{m.path}: {type(exc).__name__}: {exc}"),
                    fix="fix the point manifest at the path shown",
                )
            )
            failed.add(guess)
            continue
        if point.id in points:
            point_problems.append(
                Quarantined(
                    id=point.id,
                    point=point.id,
                    path=m.path,
                    error=f"point {point.id} is already declared at {points[point.id].path}",
                    fix="remove one of the two declarations, or rename one point",
                )
            )
            continue
        points[point.id] = point

    entries: list[_Entry] = []
    known = ", ".join(sorted(points)) or "none"
    for m, pack in ordered:
        if m.kind != "contribution":
            continue
        cid = f"{pack.id}.{m.key}"
        point_id = m.point or ""
        point = points.get(point_id)
        contribution: Contribution | None = None
        quarantine: Quarantined | None = None
        if point_id in failed:
            quarantine = Quarantined(
                id=cid,
                point=point_id,
                path=m.path,
                error=f"point {point_id} is quarantined, so this contribution cannot be checked",
                fix=f"fix the point {point_id} first (see its quarantine)",
            )
        elif point is None:
            quarantine = Quarantined(
                id=cid,
                point=point_id,
                path=m.path,
                error=f"unknown point {point_id!r}; known points: {known}",
                fix="move the file into the folder of a known point, or declare that point",
            )
        else:
            try:
                contribution = parse_contribution(
                    m.path,
                    pack=pack.id,
                    pack_root=pack.root,
                    scope=pack.scope,
                    pack_version=pack.version,
                    point=point,
                )
            except ManifestError as exc:
                quarantine = _quarantine(exc, cid, point_id, m.path)
            except Exception as exc:  # noqa: BLE001 - a loud quarantine, never a silent skip
                quarantine = Quarantined(
                    id=cid,
                    point=point_id,
                    path=m.path,
                    error=one_line(f"{m.path}: {type(exc).__name__}: {exc}"),
                    fix="fix the manifest at the path shown",
                )
        entries.append(
            _Entry(
                point=point_id,
                key=m.key,
                id=cid,
                scope=pack.scope,
                path=m.path,
                contribution=contribution,
                quarantine=quarantine,
                reason=_reason(quarantine),
            )
        )
    return _Snapshot(points, tuple(point_problems), tuple(entries))


def _import_ref(ref: Ref) -> Any:
    """Import a reference lazily. A file is loaded under a unique module name; ``attr`` is then looked up."""
    label = ref.target
    try:
        if ref.kind == "file":
            name = "_astrid_ext_" + hashlib.sha256(ref.target.encode("utf-8")).hexdigest()[:16]
            module = sys.modules.get(name)
            if module is None:
                spec = importlib.util.spec_from_file_location(name, ref.target)
                if spec is None or spec.loader is None:
                    raise _LoadFailure(f"{label} is not a Python file", "point it at a .py file")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                try:
                    spec.loader.exec_module(module)
                except BaseException:
                    sys.modules.pop(name, None)
                    raise
        else:
            module = importlib.import_module(ref.target)
    except _LoadFailure:
        raise
    except Exception as exc:  # noqa: BLE001 - reported as a quarantine by the caller
        raise _LoadFailure(
            f"importing {label} raised {type(exc).__name__}: {exc}",
            f"fix the error in {label}, then load it again",
        ) from None
    if ref.attr is None:
        return module
    try:
        return getattr(module, ref.attr)
    except AttributeError:
        raise _LoadFailure(
            f"{label} has no attribute {ref.attr!r}",
            f"define {ref.attr} in {label}, or drop the :{ref.attr} part of the reference",
        ) from None


def _protocol_members(proto: Any) -> list[tuple[str, list[str]]]:
    """(name, parameter names) of each public callable the protocol itself declares."""
    is_class = inspect.isclass(proto)
    out: list[tuple[str, list[str]]] = []
    for name, member in inspect.getmembers(proto):
        if name.startswith("_") or not callable(member):
            continue
        if is_class:
            if not getattr(member, "__qualname__", "").startswith(proto.__name__ + "."):
                continue
        elif getattr(member, "__module__", None) != getattr(proto, "__name__", None):
            continue
        try:
            params = list(inspect.signature(member).parameters.values())
        except (TypeError, ValueError):
            params = []
        if params and params[0].name in ("self", "cls") and is_class:
            params = params[1:]
        names = [
            p.name
            for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        ]
        out.append((name, names))
    return out


class ExtensionRegistry:
    """One per process. ``load()`` is cheap and cached; ``entry()`` is the only place contribution code runs."""

    def __init__(
        self,
        *,
        roots: Sequence[Root] | None = None,
        sdk_root: Path | None = None,
        discover: Callable[..., Sequence[Any]] | None = None,
    ):
        self._roots_arg = None if roots is None else tuple(roots)
        self._sdk_root = Path(sdk_root) if sdk_root is not None else _DEFAULT_SDK_ROOT
        if discover is None:
            from astrid.core.pack.discovery import discover_pack_metadata

            discover = discover_pack_metadata
        self._discover = discover
        self._snap: _Snapshot | None = None
        self._snap_key: Any = None
        self._points: dict[str, ExtensionPoint] = {}
        self._winners: list[_Entry] = []
        self._short: dict[tuple[str, str], _Entry] = {}
        self._problems: list[Any] = []
        self._disabled: frozenset[str] = frozenset()
        self._loaded: dict[str, Any] = {}
        self._runtime_failed: dict[str, Quarantined] = {}
        self._protocols: dict[str, Any] = {}

    # ------------------------------------------------------------ loading

    def _current_roots(self) -> tuple[Root, ...]:
        if self._roots_arg is not None:
            return self._roots_arg
        return contribution_roots()

    def _packs(self, roots: Sequence[Root]) -> list[_Pack]:
        # Pass only the roots discovery does not already scan (source tree, managed sources, ASTRID_PACKS_PATH).
        extra = tuple(
            str(r.path)
            for r in roots
            if r.scope != "built-in"
            and not r.origin.startswith("managed:")
            and r.origin != "ASTRID_PACKS_PATH"
            and r.path.is_dir()
        )
        records = self._discover(extra_pack_roots=extra)
        packs: list[_Pack] = []
        for (
            rec
        ) in records:  # a repeated pack id is kept: its contributions are shadowed, and reported
            pack_dir = Path(rec.pack_dir)
            inner = getattr(rec, "pack", None)
            version = str(getattr(inner, "version", None) or getattr(rec, "version", "0.0.0"))
            packs.append(_Pack(str(rec.id), pack_dir, version, _scope_of(pack_dir, roots)))
        return packs

    def _collect(self, packs: Sequence[_Pack], sdk_pack: _Pack) -> list[tuple[ManifestFile, _Pack]]:
        items: list[tuple[ManifestFile, _Pack]] = []
        for pack in packs:
            for m in iter_extension_manifests(pack.root):
                if m.kind != "legacy":
                    items.append((m, pack))
        for m in iter_sdk_manifests(self._sdk_root):
            items.append((m, sdk_pack))
        return items

    def _fingerprint(
        self,
        roots: Sequence[Root],
        packs: Sequence[_Pack],
        items: Sequence[tuple[ManifestFile, _Pack]],
    ) -> Any:
        head = (
            tuple((str(r.path), r.scope) for r in roots),
            str(self._sdk_root),
            tuple((p.id, str(p.root), p.version, p.scope) for p in packs),
        )
        files = []
        for m, _pack in items:
            try:
                st = m.path.stat()
                stamp: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
            except OSError:
                stamp = None
            files.append((str(m.path), stamp))
        return head, tuple(files)

    def _disabled_now(self) -> tuple[frozenset[str], list[Quarantined]]:
        try:
            return disabled_ids(), []
        except ValueError as exc:
            return frozenset(), [
                Quarantined(
                    id="[extensions] disable",
                    point=None,
                    path=Path(".astrid") / "config.toml",
                    error=one_line(exc),
                    fix="fix the TOML at the path shown, then load again",
                )
            ]

    def load(self, *, refresh: bool = False) -> ExtensionRegistry:
        """Discover, parse and resolve. Imports nothing. Re-parses only when roots or manifests changed."""
        roots = self._current_roots()
        sdk_pack = _Pack(SDK_OWNER, self._sdk_root.parent, "0.0.0", "built-in", sdk=True)
        packs = self._packs(roots)
        items = self._collect(packs, sdk_pack)
        key = self._fingerprint(roots, packs, items)
        if refresh or self._snap is None or key != self._snap_key:
            self._snap = _parse(items)
            self._snap_key = key
            self._loaded.clear()
            self._runtime_failed.clear()
            self._protocols.clear()
        disabled, config_problems = self._disabled_now()
        self._resolve(self._snap, disabled, config_problems)
        return self

    def _ensure(self) -> None:
        if self._snap is None:
            self.load()

    def _resolve(
        self, snap: _Snapshot, disabled: frozenset[str], config_problems: list[Quarantined]
    ) -> None:
        winners: dict[str, _Entry] = {}
        shadows: list[Shadowed] = []
        for e in snap.entries:  # best first, so the first of a qualified id is the winner
            held = winners.get(e.id)
            if held is None:
                winners[e.id] = e
            elif e.quarantine is None:
                shadows.append(Shadowed(e.id, e.point, held.id, e.scope, e.path))

        groups: dict[tuple[str, str], list[_Entry]] = {}
        for e in winners.values():
            groups.setdefault((e.point, e.key), []).append(e)
        short: dict[tuple[str, str], _Entry] = {}
        for group_key, group in groups.items():
            # A built-in short name is reserved: built-ins rank first, then the most specific scope.
            ordered = sorted(
                group, key=lambda e: (e.scope != "built-in", -SCOPE_RANK[e.scope], e.id)
            )
            holder = ordered[0]
            short[group_key] = holder
            for loser in ordered[1:]:
                if loser.quarantine is None:
                    shadows.append(
                        Shadowed(loser.id, loser.point, holder.id, loser.scope, loser.path)
                    )

        disabled_rows = [
            Disabled(e.id, e.point, e.path) for e in winners.values() if e.id in disabled
        ]
        self._points = dict(snap.points)
        self._winners = list(winners.values())
        self._short = short
        self._disabled = disabled
        self._problems = [
            *snap.point_problems,
            *config_problems,
            *(e.quarantine for e in snap.entries if e.quarantine is not None),
            *shadows,
            *disabled_rows,
        ]

    # ------------------------------------------------------------ listing

    def points(self) -> list[ExtensionPoint]:
        self._ensure()
        return sorted(self._points.values(), key=lambda p: p.id)

    def contributions(
        self, point: str, *, include_experimental: bool = False
    ) -> list[Contribution]:
        self._ensure()
        out: list[Contribution] = []
        for e in self._winners:
            if e.point != point or e.contribution is None or e.quarantine is not None:
                continue
            if e.id in self._disabled:
                continue
            if e.contribution.status == "experimental" and not include_experimental:
                continue
            out.append(e.contribution)
        return sorted(out, key=lambda c: c.id)

    def problems(self) -> list[Quarantined | Shadowed | Disabled]:
        self._ensure()
        return [*self._problems, *self._runtime_failed.values()]

    # ------------------------------------------------------------ resolving

    def resolve(self, point: str, name: str) -> Contribution:
        self._ensure()
        if point not in self._points:
            known = ", ".join(sorted(self._points)) or "none"
            raise ExtensionUnavailable(
                f"unknown point {point!r}",
                reason="unknown",
                fix=f"use a known point id; known points: {known}",
            )
        if "." in name:
            entry = next((e for e in self._winners if e.point == point and e.id == name), None)
            if entry is None:
                elsewhere = next((e for e in self._winners if e.id == name), None)
                if elsewhere is not None:
                    raise ExtensionUnavailable(
                        f"{name} is a contribution of {elsewhere.point}, not {point}",
                        reason="unknown",
                        fix=f"ask for it under {elsewhere.point}",
                    )
                raise ExtensionUnavailable(
                    f"no contribution {name!r} for {point}",
                    reason="unknown",
                    fix="check the qualified id; list the point's contributions to see the ids",
                )
        else:
            entry = self._short.get((point, name))
            if entry is None:
                names = sorted(k for (p, k) in self._short if p == point)
                raise ExtensionUnavailable(
                    f"no contribution named {name!r} for {point}",
                    reason="unknown",
                    fix=f"check the spelling; known names: {', '.join(names) or 'none'}",
                )
        return self._usable(entry)

    def _usable(self, entry: _Entry) -> Contribution:
        if entry.id in self._disabled:
            raise ExtensionUnavailable(
                f"{entry.id} is disabled",
                reason="disabled",
                fix=f"remove {entry.id!r} from [extensions] disable in .astrid/config.toml",
            )
        if entry.quarantine is not None or entry.contribution is None:
            q = entry.quarantine
            raise ExtensionUnavailable(
                f"{entry.id} is quarantined: {q.error if q else 'it did not parse'}",
                reason=entry.reason or "quarantined",
                fix=q.fix if q else "fix the manifest at the path shown",
            )
        c = entry.contribution
        failed = self._runtime_failed.get(c.ref)
        if failed is not None:
            raise ExtensionUnavailable(
                f"{c.id} is quarantined: {failed.error}", reason="quarantined", fix=failed.fix
            )
        return c

    def entry(self, c: Contribution) -> Any:
        """Import the contribution's code (lazily) and run the point's protocol check. Data-only: None."""
        self._ensure()
        failed = self._runtime_failed.get(c.ref)
        if failed is not None:
            raise ExtensionUnavailable(
                f"{c.id} is quarantined: {failed.error}", reason="quarantined", fix=failed.fix
            )
        if c.entry is None:
            return None
        if c.ref in self._loaded:
            return self._loaded[c.ref]
        try:
            obj = _import_ref(c.entry)
            point = self._points.get(c.point)
            if point is not None and point.protocol is not None:
                self._check_protocol(point, obj)
        except _LoadFailure as exc:
            self._runtime_failed[c.ref] = Quarantined(
                id=c.id, point=c.point, path=c.path, error=one_line(f"{c.id}: {exc}"), fix=exc.fix
            )
            raise ExtensionUnavailable(
                f"{c.id} is quarantined: {exc}", reason="quarantined", fix=exc.fix
            ) from None
        self._loaded[c.ref] = obj
        return obj

    def _check_protocol(self, point: ExtensionPoint, obj: Any) -> None:
        assert point.protocol is not None
        proto = self._protocols.get(point.id)
        if proto is None:
            proto = _import_ref(point.protocol)
            self._protocols[point.id] = proto
        problems: list[str] = []
        fixes: list[str] = []
        optional = set(getattr(proto, "__optional__", ()))
        for name, names in _protocol_members(proto):
            member = getattr(obj, name, None)
            if member is None and name in optional:
                continue  # an optional member may be absent; if present it is checked below
            if member is None or not callable(member):
                problems.append(f"has no callable {name}()")
                fixes.append(f"define {name}({', '.join(names)})")
                continue
            try:
                sig = inspect.signature(member)
            except (TypeError, ValueError):
                problems.append(f"{name}() has no inspectable signature")
                fixes.append(f"define {name}({', '.join(names)})")
                continue
            try:
                sig.bind(**{n: None for n in names})
            except TypeError:
                problems.append(f"{name}() does not accept ({', '.join(names)})")
                fixes.append(f"make {name}() accept ({', '.join(names)})")
        if problems:
            raise _LoadFailure(
                f"does not match the protocol of {point.id}: {'; '.join(problems)}",
                "; ".join(fixes) or f"make the entry match {point.id}",
            )


__all__ = [
    "Disabled",
    "ExtensionRegistry",
    "ExtensionUnavailable",
    "SCOPE_RANK",
    "Shadowed",
]
