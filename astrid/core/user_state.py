"""Versioned access to Runtime-owned user app-state documents."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any

APP_STATE_MAX_BYTES = 16 * 1024
KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class UserStateError(Exception):
    def __init__(self, code: str, key: str, message: str | None = None, *,
                 status: int | None = None):
        self.code = code
        self.key = key
        self.status = status
        super().__init__(message or f"{key!r}: {code}")


def _encoded(content: Any) -> bytes:
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def content_digest(content: Any) -> str:
    return "sha256:" + hashlib.sha256(_encoded(content)).hexdigest()


def encoded_size(content: Any) -> int:
    return len(_encoded(content))


def check_key(key: str) -> None:
    if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
        raise UserStateError("invalid_key", key)


def check_size(key: str, content: Any) -> None:
    size = encoded_size(content)
    if size > APP_STATE_MAX_BYTES:
        raise UserStateError("too_large", key,
                             f"{key!r}: {size} bytes exceeds {APP_STATE_MAX_BYTES} byte limit")


_store: ContextVar[Any] = ContextVar("user_state_store", default=None)
_store_factory: Callable[[], Any] | None = None
_UNOPENED = object()
_opened: Any = _UNOPENED
_open_lock = Lock()


@contextmanager
def using(store: Any) -> Iterator[Any]:
    token = _store.set(store)
    try:
        yield store
    finally:
        _store.reset(token)


def set_store_factory(factory_or_None: Callable[[], Any] | None) -> None:
    global _store_factory
    _store_factory = factory_or_None


def _open_default_store() -> Any:
    from astrid.sdk.client import AstridClient

    client = AstridClient.open_existing_from_launcher()
    if hasattr(client, "get_user_app_state"):
        return client
    return getattr(getattr(client, "_remote", client), "_transport", None)


def _launcher_store() -> Any:
    global _opened
    with _open_lock:
        if _opened is _UNOPENED:
            try:
                _opened = _open_default_store()
            except Exception:  # noqa: BLE001 - missing Runtime must be cached as unavailable.
                _opened = None
        return _opened


def reset_default_store() -> None:
    global _opened
    with _open_lock:
        _opened = _UNOPENED


def current_store() -> Any:
    scoped = _store.get()
    if scoped is not None:
        return scoped
    if _store_factory is not None:
        return _store_factory()
    return _launcher_store()


@dataclass(frozen=True)
class Document:
    content: dict | None
    version: int


def _document(raw: Any) -> Document:
    if isinstance(raw, Mapping):
        return Document(raw["content"], raw["version"])
    return Document(raw.content, raw.version)


def _require_store(key: str, store: Any) -> Any:
    try:
        resolved = store if store is not None else current_store()
    except OSError as exc:
        raise UserStateError("unavailable", key) from exc
    if resolved is None:
        raise UserStateError("unavailable", key)
    return resolved


def _call(key: str, operation: Callable[[], Any]) -> Document:
    try:
        raw = operation()
    except OSError as exc:
        raise UserStateError("unavailable", key) from exc
    except Exception as exc:  # noqa: BLE001 - transport errors expose a status, not a shared type.
        if not hasattr(exc, "status"):
            raise
        status = exc.status
        # The generated Runtime client (and WorkspaceClient) wraps socket
        # failures in an API error instead of letting OSError escape. Keep
        # those failures eligible for the consumers' local-state fallback.
        if status == 0 and getattr(exc, "code", None) in ("transport_error", "transport_timeout"):
            raise UserStateError("unavailable", key, status=status) from exc
        raise UserStateError("conflict" if status == 409 else "rejected",
                             key, status=status) from exc
    return _document(raw)


def read(key: str, *, store: Any = None) -> Document:
    check_key(key)
    transport = _require_store(key, store)
    return _call(key, lambda: transport.get_user_app_state(key))


def write(key: str, content: Mapping[str, Any], *, expected_version: int,
          idempotency_key: str, store: Any = None) -> Document:
    check_key(key)
    check_size(key, content)
    transport = _require_store(key, store)
    return _call(key, lambda: transport.update_user_app_state(
        key, dict(content), expected_version=expected_version, idempotency_key=idempotency_key))


def idempotency_key_for(key: str, version: int, content: Any) -> str:
    return f"{key}.v{version}.{content_digest(content)[7:23]}"


def update(key: str, change: Callable[[dict], None], *,
           validate: Callable[[dict], dict] | None = None,
           store: Any = None, attempts: int = 3) -> Document:
    check_key(key)
    for attempt in range(attempts):
        snapshot = read(key, store=store)
        content = copy.deepcopy(snapshot.content if snapshot.content is not None else {})
        change(content)
        if validate is not None:
            content = validate(content)
        if content == snapshot.content or (snapshot.content is None and content == {}):
            return snapshot
        try:
            return write(key, content, expected_version=snapshot.version,
                         idempotency_key=idempotency_key_for(key, snapshot.version, content),
                         store=store)
        except UserStateError as exc:
            if exc.code != "conflict" or attempt == attempts - 1:
                raise
    raise UserStateError("conflict", key)
