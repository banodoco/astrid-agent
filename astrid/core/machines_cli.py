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


def _setup(*, volume: str | None, gpu_types: list[str], budget_usd: float | None,
           as_json: bool, setup: Callable[..., Any] | None = None,
           volumes: Callable[[], list[dict[str, Any]]] | None = None,
           offered_gpus: Callable[[str | None, bool], list[str]] | None = None,
           input_fn: Callable[[str], str] = input) -> int:
    """Save RunPod setup through runpod.setup; inventory is read-only."""
    from astrid.core.generation import settings
    from astrid.packs.runpod.target_selection import RunPodPreferences, RunPodSetupRequiredError

    saved = settings.machine_policy("runpod") or {}
    if not isinstance(saved, dict):
        saved = {}
    missing_identity = [name for name in ("provider_account_ref", "image") if not saved.get(name)]
    if missing_identity:
        error = RunPodSetupRequiredError(tuple(missing_identity))
        print(json.dumps({"ok": False, "error": str(error)}) if as_json else str(error))
        return 2

    if volumes is None or offered_gpus is None:
        from astrid.core.util.credentials_scope import CredentialsScope
        from astrid.packs.runpod.worker_owner import _run_sync
        from astrid.packs.runpod import adapter

        credential_name = str(saved.get("credential_ref") or "env:RUNPOD_API_KEY").removeprefix("env:")
        try:
            api_key = CredentialsScope.resolve_local("runpod", env_var=credential_name).value
        except Exception as exc:
            message = f"RunPod account access unavailable; configure credentials and retry `astrid machines setup` ({exc})"
            print(json.dumps({"ok": False, "error": message}) if as_json else message)
            return 2
        volumes = volumes or (lambda: _run_sync(adapter.setup_volumes(api_key)))
        offered_gpus = offered_gpus or (lambda dc, has_volume: _run_sync(adapter.setup_gpu_types(
            api_key, datacenter_id=dc, require_network_volume=has_volume)))

    try:
        account_volumes = volumes()
        selected_volume = None
        requested_volume = volume
        if requested_volume is None and as_json:
            stored_volume = saved.get("volume")
            requested_volume = stored_volume.get("id") if isinstance(stored_volume, dict) else None
        if requested_volume is not None:
            selected_volume = next((row for row in account_volumes
                                    if row.get("id") == requested_volume or row.get("name") == requested_volume), None)
            if selected_volume is None:
                raise ValueError(f"RunPod volume {requested_volume!r} was not found; check `astrid machines setup`")
        elif not as_json:
            print("Network volumes (choose an ID/name, or press Enter for ephemeral disk):")
            for row in account_volumes:
                print(f"  {row.get('id')}  {row.get('name')}  {row.get('dataCenterId')}")
            selected = input_fn("Volume [ephemeral]: ").strip()
            if selected:
                selected_volume = next((row for row in account_volumes
                                        if row.get("id") == selected or row.get("name") == selected), None)
                if selected_volume is None:
                    raise ValueError(f"RunPod volume {selected!r} was not found; retry `astrid machines setup`")
        datacenter = (selected_volume or {}).get("dataCenterId") or saved.get("datacenter_id")
        candidates = offered_gpus(datacenter, selected_volume is not None)
        if not candidates:
            raise ValueError("no GPU types are offered in that datacenter; retry `astrid machines setup`")
        chosen = list(gpu_types or saved.get("gpu_types") or ())
        if not chosen and not as_json:
            print("GPU types offered there (enter a comma-separated ordered list):")
            print("  " + ", ".join(candidates))
            chosen = [part.strip() for part in input_fn("GPU types: ").split(",") if part.strip()]
        if not chosen:
            raise RunPodSetupRequiredError(("gpu_types",))
        if any(gpu not in candidates for gpu in chosen):
            raise ValueError("each GPU choice must be offered in the selected datacenter; retry `astrid machines setup`")
        budget = budget_usd if budget_usd is not None else saved.get("budget_cap_usd")
        if budget is None and not as_json:
            budget = float(input_fn("Maximum paid-run budget (USD): ").strip())
        if type(budget) not in (int, float) or not math.isfinite(float(budget)) or float(budget) <= 0:
            raise RunPodSetupRequiredError(("budget_cap_usd",))
        policy = dict(saved)
        policy.update({"gpu_types": chosen, "budget_cap_usd": float(budget),
                       "datacenter_id": datacenter})
        if selected_volume is None:
            policy.pop("volume", None)
        else:
            policy["volume"] = {"id": selected_volume["id"], "name": selected_volume["name"],
                                "size_gb": selected_volume.get("size_gb", selected_volume.get("size")),
                                "mount_path": saved.get("volume", {}).get("mount_path", "/workspace")}
        validated = RunPodPreferences.from_mapping(policy).as_mapping()
        if setup is None:
            from astrid.sdk import AstridClient
            import json as _json

            with AstridClient.open_from_launcher() as client:
                result = client.invoke_result("runpod.setup", kind="action", project=None,
                                              inputs={"placement_policy": _json.dumps(
                                                  {"kind": "runpod", "engine": "comfyui",
                                                   "preferences": validated}, sort_keys=True)},
                                              wait=True, read_managed_outputs=False)
        else:
            result = setup(validated)
        ok = bool(getattr(result, "ok", True))
        payload = {"ok": ok, "preferences": validated}
        if not ok:
            payload["error"] = f"{getattr(result, 'error', None)}; retry `astrid machines setup`"
        print(json.dumps(payload, sort_keys=True, default=str) if as_json else
              (f"Saved RunPod machine policy: GPUs {', '.join(chosen)}; datacenter {datacenter or 'any'}; "
               f"budget ${float(budget):.2f}/run; volume {selected_volume.get('name') if selected_volume else 'ephemeral disk'}."
               if ok else f"RunPod setup failed; retry `astrid machines setup` ({payload['error']})."))
        return 0 if ok else 1
    except (ValueError, RunPodSetupRequiredError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}) if as_json else str(exc))
        return 2
    except Exception:
        message = "RunPod setup could not confirm account inventory or save preferences; check credentials and retry `astrid machines setup`"
        print(json.dumps({"ok": False, "error": message}) if as_json else message)
        return 1


def main(argv: Sequence[str] | None = None, *, release: Callable[[], Any] | None = None,
         keep: Callable[..., Any] | None = None, setup: Callable[..., Any] | None = None,
         volumes: Callable[[], list[dict[str, Any]]] | None = None,
         offered_gpus: Callable[[str | None, bool], list[str]] | None = None,
         input_fn: Callable[[str], str] = input) -> int:
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
    configure = sub.add_parser("setup", help="Choose and save RunPod machine preferences.")
    configure.add_argument("--volume", help="Existing RunPod network volume ID or name; omit for ephemeral disk.")
    configure.add_argument("--gpu", action="append", dest="gpu_types", default=[], help="Ordered GPU choice; repeat for fallbacks.")
    configure.add_argument("--budget-usd", type=float, help="Maximum paid-run budget in USD.")
    configure.add_argument("--json", action="store_true", dest="setup_json")
    parsed = parser.parse_args(list(argv or ()))
    as_json = parsed.json or bool(getattr(parsed, "release_json", False))
    if parsed.command == "release":
        return _release(parsed.machine_id, as_json=as_json, release=release)
    if parsed.command == "keep":
        return _keep(parsed.machine_id, for_duration=parsed.keep_for,
                     until_release=parsed.until_release, max_usd=parsed.max_usd,
                     as_json=as_json or parsed.keep_json, keep=keep)
    if parsed.command == "setup":
        return _setup(volume=parsed.volume, gpu_types=parsed.gpu_types,
                      budget_usd=parsed.budget_usd, as_json=as_json or parsed.setup_json,
                      setup=setup, volumes=volumes, offered_gpus=offered_gpus, input_fn=input_fn)
    rows = _leases()
    print(json.dumps(rows, sort_keys=True, indent=2) if as_json else render(rows))
    return 0


__all__ = ["main", "render"]
