"""The user's generation settings: one document in the Runtime's per-user app-state store.

The document lives at ``/v1/app-state/user/generation`` and is read and written with
``get_user_app_state`` / ``update_user_app_state`` (versioned, idempotent writes). Its shape is
engine-neutral::

    {"placement": {kind, device?, endpoint?, policy?},
     "defaults":  {"<kind>.<mode>": {"pack": "<pack id>", "entry": "<entry id>"}},
     "machines":  {"runpod": {gpu_types, datacenter_id, image, ...}},
     "models": {"trust_unchanged": bool}}

``machines.<id>`` is placement policy for one machine contribution; no secret is ever stored in it,
only a credential *reference* by name. An old ``[placement]`` / ``[runpod]`` table in VibeComfy's
``~/.vibecomfy/config.toml`` is imported into the document once, when the document does not exist
yet, and is never read for Astrid again.

This module owns the document and its refusal site (:func:`refuse`); resolution of defaults lives
in :mod:`astrid.core.generation.defaults`.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from astrid.core.contracts.errors import AstridError

_logger = logging.getLogger(__name__)

KEY = "generation"
DOCUMENT_KEYS = ("placement", "defaults", "machines", "models")
DEFAULT_KINDS = ("image", "video", "audio")
SLOT_PATTERN = re.compile(r"^(image|video|audio)\.([a-z0-9][a-z0-9_-]*)$")
CHECKOUT_SCHEMA = "astrid.generation-settings-checkout.v1"

_store: ContextVar[Any] = ContextVar("generation_settings_store", default=None)
_store_factory: Callable[[], Any] | None = None
_model_policy: ContextVar[bool | None] = ContextVar("model_trust_unchanged", default=None)


def refuse(message: str, *, key: str, code: str = "generation_settings_invalid", options: Any = (),
           fix: str | None = None, state: Any = None) -> AstridError:
    """The one refusal site for the settings document and for defaults that cannot resolve."""
    return AstridError(message, code=code, field=key, valid_options=options, fix=fix, state_snapshot=state)


@dataclass(frozen=True)
class Snapshot:
    """The document as the Runtime holds it: content, version and where it came from."""

    content: dict[str, Any]
    version: int
    source: str  # "runtime" or "unavailable"
    imported: bool = False

    @property
    def label(self) -> str:
        return f"Runtime user settings, version {self.version}" if self.source == "runtime" else "Runtime unavailable"


def empty_document() -> dict[str, Any]:
    return {"placement": {}, "defaults": {}, "machines": {}, "models": {}}


# -- validation -----------------------------------------------------------------------------------

def validate_pin(slot: str, pin: Any) -> dict[str, str]:
    """Shape check for one pin; existence is checked by ``defaults`` against the entries."""
    if not isinstance(slot, str) or not SLOT_PATTERN.match(slot):
        raise refuse(f"defaults.{slot}: the key must be '<kind>.<mode>' with kind image, video or audio",
                     key=f"defaults.{slot}", options=[f"{kind}.<mode>" for kind in DEFAULT_KINDS])
    if not isinstance(pin, Mapping) or set(pin) != {"pack", "entry"}:
        raise refuse(f"defaults.{slot}: a pin is exactly {{pack, entry}}", key=f"defaults.{slot}")
    for name in ("pack", "entry"):
        if not isinstance(pin[name], str) or not pin[name].strip():
            raise refuse(f"defaults.{slot}.{name}: must be a nonempty string", key=f"defaults.{slot}.{name}")
    return {"pack": pin["pack"], "entry": pin["entry"]}


def validate_document(content: Any) -> dict[str, Any]:
    """Refuse a malformed or unknown key, naming it; return the normalised document."""
    if not isinstance(content, Mapping):
        raise refuse("the generation settings document must be an object", key="$")
    unknown = sorted(set(content) - set(DOCUMENT_KEYS))
    if unknown:
        raise refuse(f"unknown generation settings key(s): {', '.join(map(str, unknown))}; "
                     f"allowed: {', '.join(DOCUMENT_KEYS)}", key=str(unknown[0]), options=DOCUMENT_KEYS)
    out: dict[str, Any] = {}
    placement = content.get("placement", {})
    if not isinstance(placement, Mapping):
        raise refuse("placement must be an object", key="placement")
    if placement:
        from astrid.core.generation.placement import Placement

        try:
            Placement.from_mapping(placement)
        except AstridError as exc:
            raise refuse(str(exc), key=f"placement.{getattr(exc, 'field', None) or ''}".rstrip(".")) from exc
    out["placement"] = copy.deepcopy(dict(placement))
    defaults = content.get("defaults", {})
    if not isinstance(defaults, Mapping):
        raise refuse("defaults must be an object", key="defaults")
    out["defaults"] = {slot: validate_pin(slot, pin) for slot, pin in defaults.items()}
    machines = content.get("machines", {})
    if not isinstance(machines, Mapping):
        raise refuse("machines must be an object", key="machines")
    for name, policy in machines.items():
        if not isinstance(name, str) or not name or not isinstance(policy, Mapping):
            raise refuse("machines.<id> must be an object keyed by machine contribution id",
                         key=f"machines.{name}")
        _no_secret(policy, f"machines.{name}")
    out["machines"] = copy.deepcopy({name: dict(policy) for name, policy in machines.items()})
    models = content.get("models", {})
    if not isinstance(models, Mapping):
        raise refuse("models must be an object", key="models")
    if set(models) - {"trust_unchanged"}:
        raise refuse("models only supports trust_unchanged", key="models")
    if "trust_unchanged" in models and type(models["trust_unchanged"]) is not bool:
        raise refuse("models.trust_unchanged must be a boolean", key="models.trust_unchanged")
    out["models"] = dict(models)
    try:
        json.dumps(out, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise refuse(f"the document must be plain JSON: {exc}", key="$") from exc
    return out


_SECRETISH = re.compile(r"(^|_)(secret|token|password|api_?key)(_|$)", re.I)


def _no_secret(policy: Mapping[str, Any], where: str) -> None:
    for key, value in policy.items():
        if isinstance(key, str) and _SECRETISH.search(key) and not key.endswith("_ref"):
            raise refuse(f"{where}.{key}: secrets are never stored here; store a credential reference "
                         "by name (credential_ref)", key=f"{where}.{key}")
        if isinstance(value, Mapping):
            _no_secret(value, f"{where}.{key}")


# -- the store ------------------------------------------------------------------------------------

@contextmanager
def using(store: Any) -> Iterator[Any]:
    """Run with ``store`` (anything with get_user_app_state/update_user_app_state) as the Runtime."""
    token = _store.set(store)
    try:
        yield store
    finally:
        _store.reset(token)


def set_store_factory(factory: Callable[[], Any] | None) -> None:
    """Replace how a Runtime store is opened when none is in scope (tests install an in-memory one)."""
    global _store_factory
    _store_factory = factory


def transport_of(client: Any) -> Any:
    """The app-state transport of an ``AstridClient``/``RemoteAstridClient``, or the client itself."""
    if client is None or hasattr(client, "get_user_app_state"):
        return client
    remote = getattr(client, "_remote", client)
    return getattr(remote, "_transport", None)


_opened: Any = None


def _launcher_store() -> Any:
    global _opened
    if _opened is None:
        from astrid.sdk.client import AstridClient

        client = AstridClient.open_from_launcher()
        _opened = transport_of(client)
        import atexit

        atexit.register(getattr(client, "close", lambda: None))
    return _opened


def current_store(client: Any = None) -> Any:
    """The store in scope: an explicit client, a ``using`` override, the factory, else the Runtime."""
    explicit = transport_of(client)
    if explicit is not None:
        return explicit
    scoped = _store.get()
    if scoped is not None:
        return scoped
    return _store_factory() if _store_factory is not None else _launcher_store()


def _resource(raw: Any) -> tuple[dict[str, Any] | None, int]:
    content = raw["content"] if not hasattr(raw, "content") else raw.content
    version = raw["version"] if not hasattr(raw, "version") else raw.version
    return content, int(version)


def _legacy(path: str | Path | None) -> dict[str, Any]:
    """Astrid defaults found in VibeComfy's TOML: the placement table and RunPod preferences."""
    from astrid.core.generation import targets

    try:
        document = targets.read_config(path)
    except targets.TargetConfigError:
        return {}
    found: dict[str, Any] = {}
    if isinstance(document.get("placement"), Mapping) and document["placement"]:
        found["placement"] = copy.deepcopy(dict(document["placement"]))
    if isinstance(document.get("runpod"), Mapping) and document["runpod"]:
        found["machines"] = {"runpod": copy.deepcopy(dict(document["runpod"]))}
    return found


def _write(store: Any, content: Mapping[str, Any], expected: int, key: str) -> Snapshot:
    raw = store.update_user_app_state(KEY, dict(content), expected_version=expected, idempotency_key=key)
    stored, version = _resource(raw)
    return Snapshot(validate_document(stored or {}), version, "runtime")


def read(client: Any = None, *, legacy_path: str | Path | None = None) -> Snapshot:
    """Read the document; a missing one is created once from any old TOML Astrid default."""
    try:
        store = current_store(client)
    except Exception as exc:  # noqa: BLE001 - best-effort fallback; log only the type, never transport data or a traceback.
        _logger.debug("Could not reach the settings store; using built-in generation settings (%s)", type(exc).__name__)
        return Snapshot(empty_document(), 0, "unavailable")
    if store is None:
        return Snapshot(empty_document(), 0, "unavailable")
    content, version = _resource(store.get_user_app_state(KEY))
    if content is None:
        found = _legacy(legacy_path)
        if found:
            document = validate_document({**empty_document(), **found})
            digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:24]
            snapshot = _write(store, document, 0, f"generation-legacy-import-{digest}")
            return Snapshot(snapshot.content, snapshot.version, "runtime", imported=True)
        return Snapshot(empty_document(), 0, "runtime")
    return Snapshot(validate_document(content), version, "runtime")


def update(change: Callable[[dict[str, Any]], None], client: Any = None, *,
           legacy_path: str | Path | None = None) -> Snapshot:
    """Read, apply ``change`` to a copy, validate and write with the version it was read at."""
    snapshot = read(client, legacy_path=legacy_path)
    if snapshot.source != "runtime":
        raise refuse("the Runtime is not reachable; generation settings cannot be written", key="$",
                     code="generation_settings_unavailable")
    document = copy.deepcopy(snapshot.content)
    change(document)
    document = validate_document(document)
    if document == snapshot.content:
        return snapshot
    digest = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:24]
    return _write(current_store(client), document, snapshot.version, f"generation-{snapshot.version}-{digest}")


@contextmanager
def using_model_trust_unchanged(value: bool) -> Iterator[None]:
    """Project the same user setting from a controller into its one machine job."""
    if type(value) is not bool:
        raise ValueError("models.trust_unchanged must be a boolean")
    token = _model_policy.set(value)
    try:
        yield
    finally:
        _model_policy.reset(token)


def model_trust_unchanged(client: Any = None) -> bool:
    """Trust unchanged models by default; false or unreadable settings force full hashes."""
    projected = _model_policy.get()
    if projected is not None:
        return projected is True
    try:
        snapshot = read(client)
    except (OSError, RuntimeError, ValueError, TypeError, LookupError, AttributeError):
        # Invalid or unreadable settings fail closed to full verification.
        _logger.debug("Could not read model verification setting; using full verification", exc_info=True)
        return False
    return (snapshot.source == "runtime"
            and snapshot.content.get("models", {}).get("trust_unchanged", True) is True)


# -- checkout / checkin ---------------------------------------------------------------------------

def _sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".checkout.json")


def _digest(content: Any) -> str:
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def checkout(file: str | Path, client: Any = None) -> dict[str, Any]:
    path = Path(file).expanduser().absolute()
    sidecar = _sidecar(path)
    if path.exists() or sidecar.exists():
        raise refuse("checkout destination already exists; choose a new file path", key=str(path),
                     code="generation_settings_checkout_exists")
    snapshot = read(client)
    if snapshot.source != "runtime":
        raise refuse("the Runtime is not reachable", key="$", code="generation_settings_unavailable")
    path.write_text(json.dumps(snapshot.content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    sidecar.write_text(json.dumps({"schema": CHECKOUT_SCHEMA, "base_version": snapshot.version,
                                   "base_digest": _digest(snapshot.content)}), encoding="utf-8")
    return {"file": str(path), "version": snapshot.version}


def checkin(file: str | Path, client: Any = None) -> dict[str, Any]:
    """Write an edited checkout; a stale base is refused with both versions shown."""
    path = Path(file).expanduser().absolute()
    try:
        meta = json.loads(_sidecar(path).read_text(encoding="utf-8"))
        if meta.get("schema") != CHECKOUT_SCHEMA or type(meta.get("base_version")) is not int:
            raise ValueError("malformed checkout metadata")
        mine = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise refuse(f"cannot read the checkout: {exc}", key=str(path),
                     code="generation_settings_checkout_invalid") from exc
    document = validate_document(mine)
    from astrid.core.generation import defaults

    defaults.check_pins(document)
    snapshot = read(client)
    if snapshot.source != "runtime":
        raise refuse("the Runtime is not reachable", key="$", code="generation_settings_unavailable")
    if snapshot.version != meta["base_version"]:
        error = refuse(f"the settings changed since checkout (version {meta['base_version']} -> {snapshot.version}); "
                       "check out again and reconcile", key="$", code="generation_settings_version_conflict",
                       fix="astrid generate settings checkout --file <new path>",
                       state={"yours": {"version": meta["base_version"], "content": document},
                              "current": {"version": snapshot.version, "content": snapshot.content}})
        raise error
    digest = _digest(document)
    written = _write(current_store(client), document, snapshot.version, f"generation-checkin-{snapshot.version}-{digest[:24]}")
    return {"version": written.version, "changed": written.content != snapshot.content}


# -- the placement default ------------------------------------------------------------------------

def placement_default(client: Any = None, *, legacy_path: str | Path | None = None) -> dict[str, Any]:
    return copy.deepcopy(read(client, legacy_path=legacy_path).content.get("placement") or {})


def machine_policy(machine: str, client: Any = None, *, legacy_path: str | Path | None = None) -> dict[str, Any] | None:
    value = read(client, legacy_path=legacy_path).content.get("machines", {}).get(machine)
    return copy.deepcopy(value) if value is not None else None


def set_machine_policy(machine: str, policy: Mapping[str, Any], client: Any = None, *,
                       legacy_path: str | Path | None = None) -> Snapshot:
    return update(lambda document: document["machines"].__setitem__(machine, dict(policy)), client,
                  legacy_path=legacy_path)


ATTACHED_REFUSAL = "placement must be http://127.0.0.1:<port> or http://localhost:<port>"


def _change_placement(client: Any, change: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Apply ``change`` to the saved placement table; report the result and what it replaced."""
    previous: dict[str, Any] = {}

    def write(document: dict[str, Any]) -> None:
        nonlocal previous
        previous = copy.deepcopy(dict(document.get("placement") or {}))
        document["placement"] = change(copy.deepcopy(previous))

    snapshot = update(write, client)
    return {"version": snapshot.version, "placement": snapshot.content["placement"], "previous": previous}


def set_placement(url: str, client: Any = None) -> dict[str, Any]:
    """Save one attached ComfyUI endpoint as the placement default; the URL must pass ``normalize_target``.

    The saved ``policy`` stays. ``device``, ``machine`` and ``engine`` belong to the place they were saved
    for (the same rule as ``placement._merge``): ``device`` is kept only when the previous kind was already
    attached, and ``machine`` and ``engine`` always go. ``previous`` shows everything that was dropped.
    """
    from astrid.core.generation import targets

    try:
        endpoint = targets.normalize_target({"endpoint": url}).endpoint
    except (targets.TargetConfigError, ValueError) as exc:
        raise refuse(ATTACHED_REFUSAL, key="placement.endpoint", code="generation_settings_invalid") from exc

    def change(current: dict[str, Any]) -> dict[str, Any]:
        same_place = current.get("kind") == "attached"
        for name in ("device", "machine", "engine"):
            if name != "device" or not same_place:
                current.pop(name, None)
        return {**current, "kind": "attached", "endpoint": endpoint}

    return _change_placement(client, change)


def unset_placement(client: Any = None) -> dict[str, Any]:
    """Remove ``kind``, ``endpoint``, ``device``, ``machine`` and ``engine`` (all belong to the place); ``policy`` stays."""
    return _change_placement(client, lambda current: {k: v for k, v in current.items()
                                                      if k not in ("kind", "endpoint", "device", "machine", "engine")})


__all__ = ["KEY", "Snapshot", "checkin", "checkout", "current_store", "empty_document", "machine_policy",
           "placement_default", "read", "refuse", "set_machine_policy", "set_placement", "set_store_factory",
           "unset_placement", "update", "using", "validate_document", "validate_pin", "model_trust_unchanged",
           "using_model_trust_unchanged"]
