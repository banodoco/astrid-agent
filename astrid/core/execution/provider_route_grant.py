"""Host-owned, single-use provider route grants.

The grant is an opaque handle.  Its claims stay in the host authority and the
handle is authenticated with a host-only key; it is never placed in the child
environment or in settlement evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Mapping


class ProviderRouteGrantError(RuntimeError):
    """A provider route grant is missing, invalid, expired, or already used."""


@dataclass
class _Grant:
    task_id: str
    capability_id: str
    capability_digest: str
    routes: tuple[str, ...]
    broker_binding: str
    expires_at: float
    consumed: bool = False


def _routes(routes: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({str(route).strip() for route in routes if str(route).strip()}))


class ProviderRouteGrantAuthority:
    """Issue and consume grants for one host process."""

    version = "provider-route-grant-v1"

    def __init__(self, *, secret: bytes | None = None, clock=time.time) -> None:
        self._secret = bytes(secret or secrets.token_bytes(32))
        self._clock = clock
        self._grants: dict[str, _Grant] = {}
        self._lock = threading.Lock()

    @staticmethod
    def binding(*, capability_id: str, capability_digest: str, broker: object) -> str:
        descriptor = {
            "capability_id": str(capability_id),
            "capability_digest": str(capability_digest),
            "broker": str(broker),
            "protocol": "tcp-http-broker-v1",
        }
        raw = repr(sorted(descriptor.items())).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def issue(
        self,
        *,
        task_id: str,
        capability_id: str,
        capability_digest: str,
        routes: Iterable[str],
        broker_binding: str,
        ttl_seconds: int = 60,
    ) -> str:
        ttl = int(ttl_seconds)
        if ttl <= 0 or ttl > 300:
            raise ProviderRouteGrantError("provider route grant TTL must be between 1 and 300 seconds")
        token_id = secrets.token_urlsafe(32)
        claims = _Grant(
            task_id=str(task_id),
            capability_id=str(capability_id),
            capability_digest=str(capability_digest),
            routes=_routes(routes),
            broker_binding=str(broker_binding),
            expires_at=float(self._clock()) + ttl,
        )
        with self._lock:
            self._grants[token_id] = claims
        mac = hmac.new(self._secret, f"{self.version}\0{token_id}".encode(), hashlib.sha256).hexdigest()
        return f"{self.version}.{token_id}.{mac}"

    def consume(
        self,
        token: str | None,
        *,
        task_id: str,
        capability_id: str,
        capability_digest: str,
        routes: Iterable[str],
        broker_binding: str,
    ) -> _Grant:
        if not isinstance(token, str) or not token:
            raise ProviderRouteGrantError("provider route grant is required before provider egress")
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != self.version:
            raise ProviderRouteGrantError("provider route grant is malformed")
        token_id, signature = parts[1], parts[2]
        expected = hmac.new(self._secret, f"{self.version}\0{token_id}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ProviderRouteGrantError("provider route grant authentication failed")
        # The replay check and transition are one critical section.  This is
        # intentionally held through expiry/binding validation: no concurrent
        # host thread may observe an unconsumed grant after this point.
        with self._lock:
            grant = self._grants.get(token_id)
            if grant is None:
                raise ProviderRouteGrantError("provider route grant is unknown")
            if grant.consumed:
                raise ProviderRouteGrantError("provider route grant has already been consumed")
            if float(self._clock()) >= grant.expires_at:
                raise ProviderRouteGrantError("provider route grant has expired")
            if (
                grant.task_id != str(task_id)
                or grant.capability_id != str(capability_id)
                or grant.capability_digest != str(capability_digest)
                or grant.routes != _routes(routes)
                or grant.broker_binding != str(broker_binding)
            ):
                raise ProviderRouteGrantError("provider route grant binding does not match the admitted task")
            grant.consumed = True
            return grant


# These are existing operation identities, not tool/UI titles or route aliases:
# RuntimeService.update_timeline journals timeline.update; the two rendering
# capabilities are declared in the canonical Astrid capability matrix. This
# policy check does not establish executor readiness or authorize publication.
SELECTED_PROJECT_OPERATIONS = frozenset({
    "timeline.update", "rendering.timeline_visualize", "rendering.render",
})


class ProjectGrantError(ValueError):
    """Host policy admission failed with a stable, non-secret decision code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _project_binding(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


@dataclass(frozen=True)
class _ProjectGrant:
    realm: str
    browser_session: str
    projects: tuple[str, ...]
    operations: tuple[str, ...]
    expiry: float


class ProjectGrantAuthority:
    """Host-only policy core, separate from single-use provider egress grants.

    Constructor bindings must come from an authenticated host session and the
    host's selected project, never from request JSON, tool titles, or MCP config.
    No current Astrid/Runtime browser-session issuer is wired to this class:
    Runtime handshake IDs are not retained/authenticated on later operations.
    Until that contract exists, explicit disposable bindings exercise this core
    only; this is neither an authentication adapter nor launch enforcement.

    Opaque handles reference immutable host-owned policies. Decisions are reusable
    until expiry. Revocation and filesystem/resource resolution are separate.
    """

    def __init__(self, *, realm: str, browser_session: str, actor_id: str,
                 selected_project: str, clock=time.time) -> None:
        if not all(_project_binding(v) for v in
                   (realm, browser_session, actor_id, selected_project)):
            raise ProjectGrantError("project_grant_host_binding_missing")
        self._realm = realm
        self._browser_session = browser_session
        self._actor_id = actor_id
        self._selected_project = selected_project
        self._clock = clock
        self._policies: dict[str, _ProjectGrant] = {}
        self._lock = threading.Lock()

    def issue(self, policy: Mapping[str, object]) -> str:
        """Admit the card's five-field policy from the host, not a request.

        Only selected-project default authority (or a subset) is supported. Wider
        authority fails closed; this API creates no local approval mechanism.
        """
        if not isinstance(policy, Mapping) or set(policy) != {
            "realm", "browser_session", "projects", "operations", "expiry",
        }:
            raise ProjectGrantError("project_grant_policy_invalid")
        projects, operations = policy["projects"], policy["operations"]
        expiry = policy["expiry"]
        if (not _project_binding(policy["realm"])
                or not _project_binding(policy["browser_session"])
                or not isinstance(projects, (list, tuple)) or not projects
                or not all(_project_binding(v) for v in projects)
                or not isinstance(operations, (list, tuple)) or not operations
                or not all(_project_binding(v) for v in operations)
                or len(set(projects)) != len(projects)
                or len(set(operations)) != len(operations)
                or type(expiry) not in (int, float)):
            raise ProjectGrantError("project_grant_policy_invalid")
        try:
            expiry = float(expiry)
        except OverflowError as exc:
            raise ProjectGrantError("project_grant_policy_invalid") from exc
        now = float(self._clock())
        if not math.isfinite(expiry) or not math.isfinite(now):
            raise ProjectGrantError("project_grant_policy_invalid")
        if expiry <= now:
            raise ProjectGrantError("project_grant_expired")
        if policy["realm"] != self._realm or policy["browser_session"] != self._browser_session:
            raise ProjectGrantError("project_grant_binding_mismatch")
        if tuple(projects) != (self._selected_project,) or not set(operations) <= SELECTED_PROJECT_OPERATIONS:
            raise ProjectGrantError("project_grant_local_approval_required")
        admitted = _ProjectGrant(self._realm, self._browser_session,
                                 tuple(projects), tuple(operations), expiry)
        handle = "project-grant-v1." + secrets.token_urlsafe(32)
        with self._lock:
            self._policies[handle] = admitted
        return handle

    def decide(self, handle: object, *, realm: str, browser_session: str,
               actor_id: str, project_id: str, operation: str) -> dict[str, object]:
        """Compare request claims with host bindings; claims cannot mint policy."""
        def result(code: str, allowed: bool = False) -> dict[str, object]:
            return {"status": "allow" if allowed else "deny", "code": code,
                    "allowed": allowed, "project_id": project_id, "operation": operation}

        if not isinstance(handle, str) or not handle:
            return result("project_grant_missing")
        with self._lock:
            grant = self._policies.get(handle)
        if grant is None:
            return result("project_grant_unknown")
        now = float(self._clock())
        if not math.isfinite(now) or now >= grant.expiry:
            return result("project_grant_expired")
        if realm != grant.realm:
            return result("project_grant_realm_mismatch")
        if browser_session != grant.browser_session:
            return result("project_grant_session_mismatch")
        if actor_id != self._actor_id:
            return result("project_grant_actor_mismatch")
        if project_id not in grant.projects:
            return result("project_grant_project_denied")
        if operation not in grant.operations:
            return result("project_grant_operation_denied")
        return result("project_grant_allowed", True)


__all__ = ["ProviderRouteGrantAuthority", "ProviderRouteGrantError",
           "ProjectGrantAuthority", "ProjectGrantError", "SELECTED_PROJECT_OPERATIONS"]
