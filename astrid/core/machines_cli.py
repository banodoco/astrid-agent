"""``astrid machines``: rented generation machines (Plan 32 T5d).

    astrid machines [--json]          live and recently ended leases
    astrid machines release <id>      release one live machine (through the host's lease owner)

Each row: id (the machine id; RunPod: pod id), kind, gpu, state, release time,
minutes left, estimated cost so far, hourly rate, end reason. The list reads the
lease journal only; it never calls the provider and never takes the account lock. Release is an ordinary Runtime
task served by the retained host, the only owner allowed to stop paid machines.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Sequence


def _leases() -> list[dict[str, Any]]:
    from astrid.packs.runpod.machine import journal_leases

    return journal_leases()


def _when(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M")


def render(rows: Sequence[dict[str, Any]]) -> str:
    header = ["ID", "KIND", "GPU", "STATE", "RELEASES (UTC)", "MIN LEFT",
              "COST EST. (USD)", "RATE (USD/HR)", "ENDED"]
    body = [[str(r.get("id") or "-"), str(r.get("kind") or "-"), str(r.get("gpu") or "-"),
             str(r.get("state") or "-"), _when(r.get("release_at")),
             "-" if r.get("minutes_left") is None else f"{float(r['minutes_left']):.1f}",
             f"{float(r.get('cost_usd') or 0):.2f}",
             "-" if r.get("hourly_usd") is None else f"{float(r['hourly_usd']):.2f}",
             str(r.get("ended") or "-")] for r in rows]
    widths = [max([len(header[i])] + [len(row[i]) for row in body]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in [header, *body]]
    live = sum(1 for r in rows if r.get("live"))
    return "\n".join([*lines, f"{live} live, {len(rows) - live} ended. Cost is estimated spend so far.\nAutomatic release only happens while the owning Astrid host is running."])


def _release(machine_id: str, *, as_json: bool, release: Callable[[], Any] | None) -> int:
    rows = _leases()
    live = [row for row in rows if row.get("live")]
    ended = next((row for row in rows if row.get("id") == machine_id
                  and row.get("live") is False and row.get("state") == "ended"), None)
    if machine_id not in {row["id"] for row in live} and ended is not None:
        print(json.dumps({"ok": True, "machine": ended}, sort_keys=True, default=str) if as_json
              else f"already released {machine_id}")
        return 0
    if machine_id not in {row["id"] for row in live}:
        message = (f"no live machine {machine_id!r}; live machines: "
                   f"{', '.join(row['id'] for row in live) or 'none'}; check current IDs with `astrid machines`")
        print(json.dumps({"ok": False, "error": message}) if as_json else message)
        return 2
    if release is None:
        from astrid.sdk.generation import GenerationFacade

        def release():
            from astrid.sdk import AstridClient
            with AstridClient.open_from_launcher() as client:
                return GenerationFacade().release(engine="comfyui", placement="runpod", machine_id=machine_id, client=client, wait=True)
    result = release()
    ok = bool(getattr(result, "ok", True))
    rows = [row for row in _leases() if row.get("id") == machine_id]
    payload = {"ok": ok, "machine": rows[0] if rows else None}
    if not ok:
        payload["error"] = getattr(result, "error", None)
    print(json.dumps(payload, sort_keys=True, default=str) if as_json
          else (f"released {machine_id}" if ok else f"release of {machine_id} failed: {payload['error']}; check `astrid machines` before retrying"))
    return 0 if ok else 1


def _duration_seconds(value: str) -> float:
    parts = list(re.finditer(r"(\d+(?:\.\d+)?)([smhd])", value.strip().lower()))
    if not parts or "".join(part.group(0) for part in parts) != value.strip().lower():
        raise ValueError("invalid --for duration; use s, m, h or d (for example 30m or 2h)")
    scales = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = sum(float(part.group(1)) * scales[part.group(2)] for part in parts)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("invalid --for duration; use a finite duration greater than zero (for example 30m or 2h)")
    return seconds


def _keep_consent_data(machine_id: str, hourly_usd: float):
    """Read the private offer digest bound to this exact locally journaled lease."""
    from astrid.packs.runpod.target_selection import canonical_claim_path, read_runpod_preferences

    preferences = read_runpod_preferences()
    path = canonical_claim_path(preferences.provider_account_ref)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("cannot read this machine's saved details; check `astrid machines` before trying keep again") from None
    limits = record.get("paid_limits") if isinstance(record, dict) else None
    if (not isinstance(record, dict) or record.get("pod_id") != machine_id
            or record.get("state") != "claimed" or not isinstance(limits, dict)
            or limits.get("hourly_usd") != hourly_usd
            or not isinstance(limits.get("offer_digest"), str)):
        raise ValueError("machine details changed; check `astrid machines` and try keep again")
    return preferences, limits["offer_digest"]


def _keep(machine_id: str, *, for_duration: str | None, until_release: bool,
          max_usd: float, as_json: bool, keep: Callable[..., Any] | None) -> int:
    rows = _leases()
    machine = next((row for row in rows if row.get("id") == machine_id and row.get("live")), None)
    if machine is None:
        live_ids = ", ".join(row["id"] for row in rows if row.get("live")) or "none"
        message = f"no live machine {machine_id!r}; live machines: {live_ids}; check current IDs with `astrid machines`"
        print(json.dumps({"ok": False, "error": message}) if as_json else message)
        return 2
    try:
        seconds = _duration_seconds(for_duration) if for_duration is not None else None
        cap = float(max_usd)
        if not math.isfinite(cap) or cap <= 0:
            raise ValueError("invalid --max-usd; enter a finite USD amount greater than zero")
        rate_value = machine.get("hourly_usd")
        if type(rate_value) not in (int, float):
            raise ValueError("cannot confirm this machine's hourly rate; check `astrid machines` before trying keep again")
        rate = float(rate_value)
        if not math.isfinite(rate) or rate <= 0:
            raise ValueError("cannot confirm this machine's hourly rate; check `astrid machines` before trying keep again")
        _preferences, offer_digest = _keep_consent_data(machine_id, rate)
        from astrid.packs.runpod.target_selection import PaidRunConsent

        now = datetime.now(timezone.utc)
        requested_seconds = seconds if seconds is not None else cap / rate * 3600
        if not math.isfinite(requested_seconds) or requested_seconds <= 0:
            raise ValueError("keep limit is out of range; choose a different duration or USD cap")
        effective_cap = min(cap, requested_seconds * rate / 3600)
        consent_seconds = min(requested_seconds, effective_cap / rate * 3600)
        # Host-side consent validation happens after the task reaches the retained host.
        # Give that handoff a short grace period; the paid cap still bounds the expiry.
        deadline = (now + timedelta(seconds=consent_seconds + 60)).isoformat().replace("+00:00", "Z")
        consent = PaidRunConsent(
            invocation_id=f"astrid-machines-keep-{uuid.uuid4().hex}",
            offer_digest=offer_digest,
            max_usd=effective_cap,
            deadline=deadline,
            consent=("paid",),
            preparation_seconds=0,
            execution_seconds=0,
            idle_seconds=consent_seconds,
        )
        if keep is None:
            from astrid.sdk import AstridClient
            from astrid.sdk.generation import GenerationFacade

            def keep(**kwargs):
                with AstridClient.open_from_launcher() as client:
                    return GenerationFacade().keep(client=client, wait=True, timeout_seconds=300.0, **kwargs)

        result = keep(machine_id=machine_id, consent=consent, until_release=until_release)
    except TimeoutError as exc:
        message = f"keep result is unknown after a timeout; check `astrid machines` before retrying ({exc})"
        print(json.dumps({"ok": False, "error": message, "unknown": True}) if as_json else message)
        return 1
    except ValueError as exc:
        message = str(exc)
        print(json.dumps({"ok": False, "error": message}) if as_json else message)
        return 2
    except Exception as exc:
        if "timeout" in str(exc).lower() or "timed out" in str(exc).lower():
            message = f"keep result is unknown after a timeout; check `astrid machines` before retrying ({exc})"
            print(json.dumps({"ok": False, "error": message, "unknown": True}) if as_json else message)
            return 1
        message = f"keep failed: {exc}; check `astrid machines` before retrying"
        print(json.dumps({"ok": False, "error": message}) if as_json else message)
        return 1

    if not bool(getattr(result, "ok", True)):
        error = getattr(result, "error", None)
        if "timeout" in str(error).lower() or "timed out" in str(error).lower():
            message = "keep result is unknown after a timeout; check `astrid machines` before retrying"
            print(json.dumps({"ok": False, "error": message, "unknown": True}) if as_json else message)
            return 1
        message = f"keep failed: {error}; check `astrid machines` before retrying"
        print(json.dumps({"ok": False, "error": message}) if as_json else message)
        return 1
    updated = next((row for row in _leases() if row.get("id") == machine_id), None)
    if updated is None or updated.get("release_at") is None:
        message = "keep result is unknown; check `astrid machines` before retrying"
        print(json.dumps({"ok": False, "error": message, "unknown": True}) if as_json else message)
        return 1
    payload = {"ok": True, "machine": updated, "max_usd": effective_cap,
               "requested_max_usd": cap, "until_release": until_release}
    print(json.dumps(payload, sort_keys=True, default=str) if as_json else
          f"kept {machine_id}; release scheduled for {_when(updated.get('release_at') if updated else None)} UTC "
          f"(additional spend cap ${effective_cap:.2f}; requested cap ${cap:.2f}). "
          f"Automatic release only happens while the owning Astrid host is running.")
    return 0


def main(argv: Sequence[str] | None = None, *, release: Callable[[], Any] | None = None,
         keep: Callable[..., Any] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astrid machines", description="Rented generation machines. Automatic release only happens while the owning Astrid host is running.")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    stop = sub.add_parser("release", help="Release one live machine.")
    stop.add_argument("machine_id")
    stop.add_argument("--json", action="store_true", dest="release_json")
    extend = sub.add_parser("keep", help="Keep a live machine within a new spend cap.")
    extend.add_argument("machine_id")
    duration = extend.add_mutually_exclusive_group(required=True)
    duration.add_argument("--for", dest="keep_for", metavar="DURATION", help="Keep for up to this duration (for example 30m or 2h), or less if the USD cap runs out.")
    duration.add_argument("--until-release", action="store_true", help="Turn off idle release until you release the machine or the USD cap runs out; the owning host must stay running.")
    extend.add_argument("--max-usd", type=float, required=True, help="Additional USD authorized by this keep; required for both forms.")
    extend.add_argument("--json", action="store_true", dest="keep_json")
    parsed = parser.parse_args(list(argv or ()))
    as_json = parsed.json or bool(getattr(parsed, "release_json", False))
    if parsed.command == "release":
        return _release(parsed.machine_id, as_json=as_json, release=release)
    if parsed.command == "keep":
        return _keep(parsed.machine_id, for_duration=parsed.keep_for,
                     until_release=parsed.until_release, max_usd=parsed.max_usd,
                     as_json=as_json or parsed.keep_json, keep=keep)
    rows = _leases()
    print(json.dumps(rows, sort_keys=True, indent=2) if as_json else render(rows))
    return 0


__all__ = ["main", "render"]
