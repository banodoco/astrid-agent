"""The layer interface, the registry and pack discovery."""
from __future__ import annotations

import importlib.util
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

# What a layer may ask for. "frames" makes the planner capture its frames;
# everything else is read from the timeline document.
NEEDS = ("doc", "frames", "words", "beats", "audio")
VIEWS = ("motion", "contact")


@dataclass
class LayerResult:
    """One panel (``None`` for a findings-only layer) and its text findings."""

    image: Any | None
    findings: list[str] = field(default_factory=list)
    title: str = ""
    page: str = "data"  # "data" (time-aligned page) or "frames" (pixel page)


SEVERITIES = ("error", "warn", "info")


@dataclass(frozen=True)
class Finding:
    """One condition result: what, where, how bad, and (when known) the exact fix.

    ``fix`` is machine-applicable: ``{"clip": id, "set": {"params.x": 1104}}``,
    ``{"clip": id, "move_s": 0.12}`` or ``{"clip": id, "set": {"volume": 0.8}}``.
    """

    code: str
    cut: int | None
    t: float | None  # timeline seconds
    message: str
    severity: str = "warn"
    fix: Mapping[str, Any] | None = None
    check: str = ""
    show_scope: bool = True

    def line(self, cut_start: float | None = None) -> str:
        where = f" cut {self.cut}" if self.cut is not None else (" timeline" if self.show_scope else "")
        rel = f" {self.t - cut_start:+.2f}s" if cut_start is not None and self.t is not None else (
            f" {self.t:.2f}s" if self.t is not None and self.cut is None else "")
        return f"{self.code:<6}{where}{rel} {self.message}".rstrip()

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "cut": self.cut,
                "t": None if self.t is None else round(self.t, 3), "message": self.message,
                "fix": dict(self.fix) if self.fix else None, "check": self.check}


@dataclass
class CheckContext:
    """What a check reads. Per-cut checks get ``cut``; timeline checks get ``cut=None``.

    ``params`` holds every threshold (defaults merged with the project rules
    file); ``tracks`` are the data tracks in reach (``motion.data``); ``frames``
    is non-empty only inside ``visualize --view motion``.
    """

    cut: Mapping[str, Any] | None
    cuts: Sequence[Mapping[str, Any]]
    fps: float
    elements: list[Any]
    all_elements: list[Any]
    words: list[Any]
    beats: Mapping[str, list]
    sfx: list[tuple[float, float, str]]
    tracks: list[Any]
    params: Mapping[str, Any]
    track_order: Sequence[str] = ()
    frames: Mapping[int, Path] = field(default_factory=dict)
    shared: dict[str, Any] = field(default_factory=dict)
    extension_context: Any | None = None

    def param(self, key: str) -> Any:
        return self.params.get(key)

    def finding(self, code: str, message: str, *, t: float | None = None, severity: str = "warn",
                fix: Mapping[str, Any] | None = None, show_scope: bool = True) -> Finding:
        """A finding for this context's cut (``t`` defaults to the cut start)."""
        cut = int(self.cut["index"]) if self.cut else None
        if t is None and self.cut:
            t = float(self.cut["start"])
        return Finding(code, cut, t, message, severity, fix, show_scope=show_scope)


@dataclass(frozen=True)
class Check:
    """A condition. ``run(ctx) -> iterable of Finding`` (use ``ctx.finding(...)``).

    ``params`` maps each threshold key to its default (``None`` = off until a
    rules file sets it); keys are shared across checks, so one ``min_text_px``
    feeds every check that reads it. ``scope`` is "cut" (run per cut) or
    "timeline" (once). ``needs=("frames",)`` runs only where frames exist
    (``visualize --view motion``); doc checks also run in ``timelines lint``.
    """

    name: str
    help: str
    run: Callable[[CheckContext], Any]
    params: Mapping[str, Any] = field(default_factory=dict)
    scope: str = "cut"
    needs: tuple[str, ...] = ("doc",)
    codes: tuple[str, ...] = ()
    source: str = "built-in"
    severity: str = "warn"
    reads: tuple[str, ...] = ()


@dataclass(frozen=True)
class Layer:
    """A registered view.

    ``render(ctx) -> LayerResult``. ``frame_budget`` is the number of extra
    frames the layer asks the planner for (the whole sheet is capped).
    ``experimental`` layers run only when named explicitly. ``checks`` are
    conditions shipped with the view (registered with it).
    """

    name: str
    help: str
    render: Callable[["LayerContext"], LayerResult]
    needs: tuple[str, ...] = ("doc",)
    views: tuple[str, ...] = ("motion",)
    frame_budget: int = 0
    experimental: bool = False
    order: int = 100
    source: str = "built-in"
    default: bool = True  # runs when no --layer is given (experimental layers never do)
    checks: tuple[Check, ...] = ()


_REGISTRY: dict[str, Layer] = {}
_CHECKS: dict[str, Check] = {}
_BROKEN: dict[str, str] = {}
_DISCOVERED = False


def _short_word(kind: str, name: str) -> None:
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise ValueError(f"{kind} name {name!r} must be a short word (letters, digits, - or _)")


def register_check(check: Check) -> Check:
    _short_word("check", check.name)
    if check.scope not in ("cut", "timeline"):
        raise ValueError(f"check {check.name!r}: scope must be 'cut' or 'timeline'")
    unknown = set(check.needs) - set(NEEDS)
    if unknown:
        raise ValueError(f"check {check.name!r} needs unknown inputs: {sorted(unknown)}")
    for key, default in check.params.items():
        for other in _CHECKS.values():
            if other.name != check.name and key in other.params and other.params[key] != default:
                raise ValueError(f"check {check.name!r}: param {key!r} default {default!r} conflicts with "
                                 f"{other.name!r} ({other.params[key]!r}); share one default or rename the key")
    existing = _CHECKS.get(check.name)
    if existing is not None and existing.source != check.source:
        raise ValueError(f"check {check.name!r} from {check.source} would shadow the one from {existing.source}; "
                         "rename one")
    _CHECKS[check.name] = check
    return check


def register(layer: Layer) -> Layer:
    _short_word("layer", layer.name)
    unknown = set(layer.needs) - set(NEEDS)
    if unknown:
        raise ValueError(f"layer {layer.name!r} needs unknown inputs: {sorted(unknown)}")
    existing = _REGISTRY.get(layer.name)
    if existing is not None and existing.source != layer.source:
        raise ValueError(f"layer {layer.name!r} from {layer.source} would shadow the one from {existing.source}; "
                         "rename one")
    for check in layer.checks:
        register_check(Check(**{**check.__dict__, "source": layer.source if check.source == "built-in" else check.source}))
    _REGISTRY[layer.name] = layer
    return layer


def checks(*, include_v3: bool = False) -> dict[str, Check]:
    """Every registered check (built-in and pack-provided)."""
    discover()
    registry = dict(_CHECKS)
    if include_v3:
        try:
            from astrid.core.pack.checks import registered_checks

            for check in registered_checks():
                if check.name in registry:
                    raise ValueError(f"V3 check {check.name!r} collides with an existing check")
                registry[check.name] = check
            _BROKEN.pop("v3/timeline.checks", None)
        except Exception as exc:  # noqa: BLE001 - bad metadata is visible in the check census
            _BROKEN["v3/timeline.checks"] = f"{type(exc).__name__}: {exc}"
    return registry


def check_defaults() -> dict[str, Any]:
    """Every threshold key any check reads, with its default."""
    defaults: dict[str, Any] = {}
    for check in checks().values():
        defaults.update(check.params)
    return defaults


def _packs_root() -> Path:
    return Path(__file__).resolve().parents[4]


def broken() -> dict[str, str]:
    """Pack modules that failed to load (``pack/module`` -> ``Type: message``), after discovery."""
    discover()
    return dict(_BROKEN)


def broken_line(kind: str = "check") -> str | None:
    """One line naming every pack module that could not load, or ``None`` when all loaded.

    ``kind`` picks the command that lists them: "check" (timelines lint) or "layer" (visualize).
    """
    failed = broken()
    if not failed:
        return None
    hint = "timelines lint --list-checks" if kind == "check" else "timelines visualize --list-layers"
    parts = [f"{name}: {error}" for name, error in sorted(failed.items())]
    if len(parts) == 1:
        return f"1 {kind} module could not load: {parts[0]} ({hint})"
    return f"{len(parts)} {kind} modules could not load: {'; '.join(parts)} ({hint})"


def discover(*, packs_root: Path | None = None, refresh: bool = False) -> dict[str, Layer]:
    """Built-ins plus declared ``<pack>/rendering/visualize_layers`` modules."""
    global _DISCOVERED
    if _DISCOVERED and not refresh and packs_root is None:
        return dict(_REGISTRY)
    from . import (
        builtin,  # noqa: F401  (registers the built-in layers)
        builtin_checks,  # noqa: F401  (registers the built-in checks)
    )

    root = packs_root or _packs_root()
    _BROKEN.clear()
    layer_paths = list(root.glob("*/rendering/visualize_layers/*.py"))
    layer_paths.extend(root.glob("*/visualize_layers/*.py"))
    for path in sorted(layer_paths):
        if path.name.startswith("_"):
            continue
        module_name = f"astrid_visualize_layer_{path.parent.parent.name}_{path.stem}"
        before_layers, before_checks = dict(_REGISTRY), dict(_CHECKS)
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("not importable")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            layers = list(getattr(module, "LAYERS", None) or [])
            if getattr(module, "LAYER", None) is not None:
                layers.append(module.LAYER)
            found_checks = list(getattr(module, "CHECKS", None) or [])
            if getattr(module, "CHECK", None) is not None:
                found_checks.append(module.CHECK)
            if not layers and not found_checks:
                raise ValueError("defines no LAYER, LAYERS, CHECK or CHECKS")
            source = f"{path.parent.parent.name} pack ({path.relative_to(root.parent.parent).as_posix()})"
            for layer in layers:
                register(Layer(**{**layer.__dict__, "source": source,
                                  "checks": tuple(Check(**{**c.__dict__, "source": source}) for c in layer.checks)}))
            for check in found_checks:
                register_check(Check(**{**check.__dict__, "source": source}))
        except Exception as exc:  # noqa: BLE001 - a broken pack layer must not break visualize
            # A module that fails half-way registers nothing: restore what it touched before it failed.
            _REGISTRY.clear()
            _REGISTRY.update(before_layers)
            _CHECKS.clear()
            _CHECKS.update(before_checks)
            _BROKEN[f"{path.parent.parent.name}/{path.stem}"] = f"{type(exc).__name__}: {exc}"
    _DISCOVERED = True
    return dict(_REGISTRY)


def resolve_layers(names: Sequence[str] | None, *, view: str) -> list[Layer]:
    """The layers to run: the named ones (in order), else every non-experimental layer of ``view``."""
    registry = discover()
    if not names:
        chosen = [layer for layer in registry.values()
                  if view in layer.views and layer.default and not layer.experimental]
        return sorted(chosen, key=lambda layer: (layer.order, layer.name))
    chosen = []
    for raw in names:
        name = str(raw).strip()
        if not name:
            continue
        if name == "all":
            chosen.extend(layer for layer in registry.values() if view in layer.views)
            continue
        layer = registry.get(name)
        if layer is None:
            known = ", ".join(sorted(registry))
            raise ValueError(f"unknown layer {name!r}; known layers: {known} (timelines visualize --list-layers)")
        if view not in layer.views:
            raise ValueError(f"layer {name!r} applies to --view {'/'.join(layer.views)}, not {view}")
        chosen.append(layer)
    unique = list({layer.name: layer for layer in chosen}.values())
    return sorted(unique, key=lambda layer: (layer.order, layer.name))


def layer_help() -> str:
    """The ``--list-layers`` text."""
    registry = discover()
    lines = ["Layers (--layer a,b,…; with no --layer every default layer of the view runs):"]
    for layer in sorted(registry.values(), key=lambda layer: (layer.order, layer.name)):
        tags = [f"views {'/'.join(layer.views)}", f"needs {'+'.join(layer.needs)}"]
        if layer.frame_budget:
            tags.append(f"+{layer.frame_budget} frames")
        if layer.experimental:
            tags.append("experimental")
        elif not layer.default:
            tags.append("opt-in")
        if layer.source != "built-in":
            tags.append(f"from {layer.source}")
        lines.append(f"  {layer.name:<9} {layer.help}  [{'; '.join(tags)}]")
    for name, error in sorted(_BROKEN.items()):
        lines.append(f"  (broken) {name}: {error}")
    lines.append("Add one: a module astrid/packs/<pack>/rendering/visualize_layers/<name>.py defining LAYER = Layer(...) "
                 "(and CHECK = Check(...) for a condition); see the visualize extension guide.")
    return "\n".join(lines)


def check_help(rules: Mapping[str, Any] | None = None) -> str:
    """The ``timelines lint --list-checks`` text (effective values when a rules file is in force)."""
    registry = checks(include_v3=True)
    overrides = dict((rules or {}).get("params") or {})
    lines = ["Checks (timelines lint runs every doc check; frame checks run in --view motion):"]
    for check in sorted(registry.values(), key=lambda c: c.name):
        tags = [check.scope, "needs " + "+".join(check.needs)]
        if check.codes:
            tags.append("codes " + ",".join(check.codes))
        if check.reads:
            tags.append("reads " + "+".join(check.reads))
        if check.source.endswith("V3"):
            tags.append("severity " + check.severity)
        if check.source != "built-in":
            tags.append("from " + check.source)
        lines.append(f"  {check.name:<16} {check.help}  [{'; '.join(tags)}]")
        for key, default in check.params.items():
            if key in overrides:
                lines.append(f"      {key} = {overrides[key]:g}   (from {rules.get('path')}; default {default!r})")
            else:
                lines.append(f"      {key} = {default!r}" + ("   (off until set)" if default is None else ""))
        if check.name in set((rules or {}).get("disable") or ()):
            lines.append("      (disabled by the rules file)")
    for name, error in sorted(_BROKEN.items()):
        lines.append(f"  (broken) {name}: {error}")
    lines.append("Thresholds: astrid-lint.toml (found from the working directory upwards) or --rules FILE, "
                 "e.g. `max_cut_s = 4` and `[severity] EDGE = \"off\"`.")
    return "\n".join(lines)


# ---------------------------------------------------------------- context

@dataclass
class LayerContext:
    """Everything a layer may read for one cut window (all times in timeline seconds)."""

    cut: Mapping[str, Any]
    cuts: Sequence[Mapping[str, Any]]
    window: tuple[float, float]
    fps: float
    elements: list[Any]  # motion.model.Element in the window
    all_elements: list[Any]
    words: list[Any]  # motion.model.Word in the window
    beats: Mapping[str, list] = field(default_factory=dict)
    sfx: list[tuple[float, float, str]] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)  # motion.model.Event in the window
    frames: dict[int, Path] = field(default_factory=dict)  # timeline frame -> captured PNG
    tracks: list[Any] = field(default_factory=list)  # motion.data.Track: app.data.* on clips in the window (+ derived)
    frame_size: tuple[int, int] = (480, 270)
    width: int = 1600
    gutter: int = 150
    shared: dict[str, Any] = field(default_factory=dict)  # results layers pass to later layers

    # -- time axis shared by every time-aligned panel
    @property
    def plot_left(self) -> int:
        return self.gutter

    @property
    def plot_right(self) -> int:
        return self.width - 24

    def x_of(self, t: float) -> float:
        """Panel x of timeline second ``t``, clamped to the plot area."""
        start, end = self.window
        span = max(1e-6, end - start)
        x = self.plot_left + (t - start) / span * (self.plot_right - self.plot_left)
        return min(self.plot_right, max(self.plot_left, x))

    def visible(self, start: float, end: float | None = None) -> bool:
        """True when ``[start, end]`` (or the instant ``start``) falls inside the panel window."""
        end = start if end is None else end
        return end >= self.window[0] and start <= self.window[1]

    def frame_time(self, frame: int) -> float:
        return frame / self.fps

    def rel(self, t: float) -> str:
        """``+1.23s`` relative to the cut start; plain timeline seconds (``7.20s``) for a range window."""
        if self.cut.get("window"):
            return f"{t:.2f}s"
        return f"{t - float(self.cut['start']):+.2f}s"

    def panel(self, height: int, *, title: str = "", axis: bool = True):
        """A blank panel with its title in the gutter and the shared time grid."""
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (self.width, height), PALETTE["bg"])
        draw = ImageDraw.Draw(image)
        if title:
            draw_text(draw, (10, 8), title, 15, PALETTE["ink"])
        if axis:
            start, end = self.window
            step = 0.5 if end - start <= 8 else 1.0
            t = math.ceil(start / step) * step
            while t <= end + 1e-6:
                x = self.x_of(t)
                strong = abs(t - round(t)) < 1e-6
                draw.line((x, 0, x, height), fill=PALETTE["grid_strong" if strong else "grid"])
                t += step
            for edge in (float(self.cut["start"]), float(self.cut["end"])):
                x = self.x_of(edge)
                draw.line((x, 0, x, height), fill=PALETTE["cut"], width=2)
        return image

    def frame_image(self, frame: int):
        from PIL import Image

        path = self.frames.get(frame)
        if path is None:
            return None
        with Image.open(path) as source:
            return source.convert("RGB")


PALETTE = {
    "bg": "#111827",
    "panel": "#162130",
    "ink": "#e5e7eb",
    "muted": "#9fb0bf",
    "grid": "#1f2b3a",
    "grid_strong": "#2c3b4f",
    "cut": "#f97316",
    "word": "#2f6f73",
    "word_ink": "#e6fffb",
    "beat": "#6b7a90",
    "downbeat": "#cbd5e1",
    "hit": "#facc15",
    "sfx": "#c084fc",
    "enter": "#4ade80",
    "exit": "#f87171",
    "key": "#60a5fa",
    "warn": "#fbbf24",
    "bad": "#ef4444",
    "good": "#34d399",
}
SERIES = ("#60a5fa", "#f472b6", "#facc15", "#34d399", "#c084fc", "#fb923c", "#22d3ee", "#a3e635")


def font(size: int):
    from ..filmstrip_cards import _png_font

    return _png_font(size)


def draw_text(draw, xy, text: str, size: int, fill) -> None:
    from ..filmstrip_cards import _png_draw_text

    _png_draw_text(draw, xy, text, font(size), fill=fill)


def text_width(draw, text: str, size: int) -> float:
    from ..filmstrip_cards import _png_text_width

    return _png_text_width(draw, text, font(size))
