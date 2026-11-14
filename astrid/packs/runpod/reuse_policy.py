"""One policy fence for retained RunPod authority and the final create POST."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping, Sequence

if TYPE_CHECKING:
    from .target_selection import RunPodPreferences


class RunPodPolicyMismatch(ValueError):
    """Policy cannot authorize this retained owner or create request."""


def current_policy() -> RunPodPreferences:
    """Read positively known settings; an unavailable store is never a default."""
    from astrid.core.generation import settings

    from .target_selection import RunPodPreferences

    try:
        snapshot = settings.read()
    except Exception:  # noqa: BLE001 - every settings transport failure must refuse paid authority, without exposing its data.
        raise RunPodPolicyMismatch("RunPod settings store unreadable; invoke runpod.setup before spending") from None
    if snapshot.source != "runtime":
        raise RunPodPolicyMismatch("RunPod settings store unreadable; invoke runpod.setup before spending")
    raw = snapshot.content.get("machines", {}).get("runpod")
    if raw is None:
        raise RunPodPolicyMismatch("RunPod has no saved policy; invoke runpod.setup before spending")
    try:
        preferences = RunPodPreferences.from_mapping(raw)
        if preferences.budget_cap_usd is None:
            raise ValueError("missing saved ceiling")
    except Exception:  # noqa: BLE001 - invalid saved policy never falls back to the request or reveals malformed credential data.
        raise RunPodPolicyMismatch("RunPod saved policy invalid; invoke runpod.setup before spending") from None
    return preferences


def check_reuse_policy(retained: Sequence[Any], *, kind: str, engine: str,
                       preferences: Mapping[str, Any], current_saved: bool | str = False) -> None:
    """Compare the complete normalized policy, and current saved paid authority.

    Jobs use the claim journal's policy and recorded budget/expiry, regardless
    of mutable saved defaults. A stricter saved policy is disclosed in status
    and the job receipt; it cannot renew, enlarge or invalidate that claim.
    Fresh claims and keeps instead require positively known current settings.
    """
    from .target_selection import RunPodPreferences

    retained_kind, retained_engine, retained_preferences = retained
    expected = [retained_kind, retained_engine,
                RunPodPreferences.from_mapping(retained_preferences).as_mapping()]
    policies = [preferences]
    if current_saved:
        saved = current_policy()
        value = saved.as_mapping()
        if current_saved == "ceiling":
            # Claim may explicitly select GPU/DC; its saved ceiling still binds.
            value = {**RunPodPreferences.from_mapping(preferences).as_mapping(),
                     "budget_cap_usd": saved.budget_cap_usd}
        policies.append(value)
    for requested in policies:
        # Equality is conservative: raising the saved ceiling also requires a
        # fresh owner; it never silently expands retained paid authority.
        if expected != [kind, engine, RunPodPreferences.from_mapping(requested).as_mapping()]:
            raise RunPodPolicyMismatch(
                "target operation preferences differ from the retained provider owner; "
                "invoke runpod.setup to replace preferences after releasing any owned lease"
            )


def saved_policy_notice(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Disclose stricter defaults without changing already-paid job authority."""
    if not record or record.get("state") != "claimed":
        return None
    try:
        saved = current_policy()
    except RunPodPolicyMismatch:
        return None  # Observation cannot revoke the recorded claim's bounds.
    paid = record.get("paid_limits", {})
    limits = {}
    for name, recorded, value in (
        ("budget_cap_usd", paid.get("max_usd"), saved.budget_cap_usd),
        ("idle_timeout_seconds", paid.get("idle_seconds"), saved.idle_timeout_seconds),
    ):
        if isinstance(recorded, (int, float)) and value is not None and value < recorded:
            limits[name] = {"recorded": recorded, "saved": value}
    if not limits:
        return None
    detail = "; ".join(f"{name}: recorded {values['recorded']}, saved {values['saved']}"
                       for name, values in limits.items())
    return {"code": "saved_policy_stricter_than_claim", "limits": limits,
            "message": "Saved policy is stricter; this job uses its recorded claim bounds. " + detail,
            "recorded_expires_at": paid.get("expires_at")}
