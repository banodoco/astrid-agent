"""Default generation entries as data: packs offer them, the user pins them.

Resolution is request -> user pin -> highest-priority pack default -> none, the same order as the
Placement. Packs declare defaults on their contributions: ``entries[].default_for`` on the
``vibecomfy.workflows`` point, or a row of the ``generation.defaults`` point (the built-in table for
catalog and audio entries). A pin is ``{pack, entry}`` under ``defaults["<kind>.<mode>"]`` in the
user's generation settings document (:mod:`astrid.core.generation.settings`). A pin that names no
real entry, or an entry without that kind and mode, is refused at ``settings.refuse``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from astrid.core.generation import settings

DEFAULTS_POINT = "generation.defaults"


@dataclass(frozen=True)
class Candidate:
    pack: str
    entry: str
    kind: str
    mode: str
    priority: int


@dataclass(frozen=True)
class Resolved:
    kind: str
    mode: str
    pack: str | None
    entry: str | None
    source: str  # "user pin", "pack default" or "none"
    priority: int | None
    origin: str
    others: tuple[Candidate, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"slot": f"{self.kind}.{self.mode}", "kind": self.kind, "mode": self.mode,
                "pack": self.pack, "entry": self.entry, "source": self.source,
                "priority": self.priority, "origin": self.origin,
                "others": [{"pack": c.pack, "entry": c.entry, "priority": c.priority} for c in self.others]}


def pack_candidates(registry: Any = None) -> list[Candidate]:
    """Every default a contribution declares, from both points."""
    from astrid.core.generation import workflows

    if registry is None:
        from astrid.core.extensions.registry import ExtensionRegistry

        registry = ExtensionRegistry().load()
    rows = [Candidate(**row) for row in workflows.default_declarations(registry)]
    for contribution in registry.contributions(DEFAULTS_POINT):
        for raw in contribution.fields.get("defaults", []):
            rows.append(Candidate(contribution.pack, raw["entry"], raw["kind"], raw["mode"], raw["priority"]))
    return rows


def known_entries(registry: Any = None) -> list[dict[str, Any]]:
    """Real entries as ``{pack, entry, kind, modes}``; a pin must name one of these."""
    rows: list[dict[str, Any]] = []
    for candidate in pack_candidates(registry):
        rows.append({"pack": candidate.pack, "entry": candidate.entry, "kind": candidate.kind,
                     "modes": {candidate.mode}})
    try:
        from astrid.core.model_catalog.registry import ModelRegistry

        for model in ModelRegistry.load_default().list_all(include_closed=True):
            rows.append({"pack": "generation", "entry": model.id, "kind": model.modality,
                         "modes": set(model.modes)})
    except Exception:
        pass
    try:
        from astrid.core.generation import entries as workflow_entries

        for row in [*workflow_entries.user_entries(), *workflow_entries.ready_entries()]:
            rows.append({"pack": "vibecomfy", "entry": row["id"], "kind": row["kind"], "modes": set(row["modes"])})
        for row in workflow_entries.action_entries():
            rows.append({"pack": "generation", "entry": row["id"], "kind": row["kind"], "modes": set(row["modes"])})
    except Exception:
        pass
    try:
        from astrid.core.extensions.registry import ExtensionRegistry
        from astrid.core.generation import workflows

        for row in workflows.workflow_entries(registry or ExtensionRegistry().load()):
            pack = row["contribution"].split(".", 1)[0]
            rows.append({"pack": pack, "entry": row["id"], "kind": row["kind"], "modes": set(row["modes"])})
    except Exception:
        pass
    return rows


def slots(candidates: Iterable[Candidate], pins: Mapping[str, Any]) -> list[tuple[str, str]]:
    found = {(c.kind, c.mode) for c in candidates}
    for slot in pins:
        kind, mode = slot.split(".", 1)
        found.add((kind, mode))
    return sorted(found)


def check_pin(slot: str, pin: Any, entries: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Refuse a malformed pin, an entry that does not exist, or one lacking that kind and mode."""
    pin = settings.validate_pin(slot, pin)
    kind, mode = slot.split(".", 1)
    matches = [row for row in entries if row["entry"] == pin["entry"] and row["pack"] == pin["pack"]]
    if not matches:
        options = sorted({f"{row['pack']}/{row['entry']}" for row in entries if row["kind"] == kind and mode in row["modes"]})
        raise settings.refuse(f"defaults.{slot}: no entry {pin['entry']!r} in pack {pin['pack']!r}",
                              key=f"defaults.{slot}", options=options,
                              fix="astrid generate defaults lists the entries that can fill each slot")
    if not any(row["kind"] == kind and mode in row["modes"] for row in matches):
        raise settings.refuse(f"defaults.{slot}: entry {pin['entry']!r} does not support {kind}/{mode}",
                              key=f"defaults.{slot}")
    return pin


def resolve(kind: str, mode: str, *, pins: Mapping[str, Any], candidates: Iterable[Candidate],
            entries: Iterable[Mapping[str, Any]] | None = None, origin: str = "") -> Resolved:
    slot = f"{kind}.{mode}"
    offered = sorted((c for c in candidates if (c.kind, c.mode) == (kind, mode)),
                     key=lambda c: (-c.priority, c.pack, c.entry))
    pin = pins.get(slot)
    if pin is not None:
        pin = check_pin(slot, pin, list(entries) if entries is not None else known_entries())
        return Resolved(kind, mode, pin["pack"], pin["entry"], "user pin", None, origin, tuple(offered))
    if offered:
        top = offered[0]
        return Resolved(kind, mode, top.pack, top.entry, "pack default", top.priority,
                        f"{top.pack}, priority {top.priority}", tuple(offered[1:]))
    return Resolved(kind, mode, None, None, "none", None, "no pack offers a default", ())


def ordered_ids(kind: str, mode: str, snapshot: settings.Snapshot | None = None, registry: Any = None,
                client: Any = None) -> tuple[str, ...]:
    """The preference order of entry ids for a slot: the pin, then pack defaults by priority."""
    snapshot = snapshot or settings.read(client)
    pins = snapshot.content.get("defaults", {})
    candidates = pack_candidates(registry)
    resolved = resolve(kind, mode, pins=pins, candidates=candidates)
    ids: list[str] = []
    if resolved.source == "user pin":
        ids.append(resolved.entry)
    ids += [c.entry for c in sorted((c for c in candidates if (c.kind, c.mode) == (kind, mode)),
                                    key=lambda c: (-c.priority, c.pack, c.entry)) if c.entry not in ids]
    return tuple(ids)


def pinned_entry(kind: str, mode: str, snapshot: settings.Snapshot) -> str | None:
    pin = snapshot.content.get("defaults", {}).get(f"{kind}.{mode}")
    if pin is None:
        return None
    return check_pin(f"{kind}.{mode}", pin, known_entries())["entry"]


def listing(client: Any = None, registry: Any = None) -> dict[str, Any]:
    """Every slot with its resolved entry, where it came from, the others, and how to edit it."""
    snapshot = settings.read(client)
    candidates = pack_candidates(registry)
    entries = known_entries(registry)
    pins = snapshot.content.get("defaults", {})
    rows = []
    for kind, mode in slots(candidates, pins):
        resolved = resolve(kind, mode, pins=pins, candidates=candidates, entries=entries,
                           origin=snapshot.label)
        row = resolved.as_dict()
        if resolved.source == "user pin":
            row["origin"] = snapshot.label
        rows.append(row)
    return {"settings": {"source": snapshot.source, "version": snapshot.version, "label": snapshot.label},
            "edit": "astrid generate settings checkout --file <path>", "slots": rows}


def set_default(kind: str, mode: str, entry: str, *, pack: str | None = None, client: Any = None,
                registry: Any = None) -> settings.Snapshot:
    """Pin ``entry`` for the slot; refuses an entry that does not exist or lacks that kind and mode."""
    entries = known_entries(registry)
    packs = sorted({row["pack"] for row in entries if row["entry"] == entry
                    and row["kind"] == kind and mode in row["modes"]})
    if pack is None:
        if len(packs) != 1:
            raise settings.refuse(
                f"defaults.{kind}.{mode}: " + (f"{entry!r} is offered by several packs; name one" if packs
                                               else f"no entry {entry!r} supports {kind}/{mode}"),
                key=f"defaults.{kind}.{mode}", options=packs)
        pack = packs[0]
    pin = check_pin(f"{kind}.{mode}", {"pack": pack, "entry": entry}, entries)
    return settings.update(lambda document: document["defaults"].__setitem__(f"{kind}.{mode}", pin), client)


def unset_default(kind: str, mode: str, *, client: Any = None) -> settings.Snapshot:
    return settings.update(lambda document: document["defaults"].pop(f"{kind}.{mode}", None), client)


def check_pins(document: Mapping[str, Any], registry: Any = None) -> None:
    """Refuse every pin of a whole document that names no real entry for its slot."""
    pins = document.get("defaults", {})
    if pins:
        entries = known_entries(registry)
        for slot, pin in pins.items():
            check_pin(slot, pin, entries)
