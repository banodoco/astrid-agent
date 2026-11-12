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
              "COST EST. (USD)", "RATE (USD/HR)", "LAST JOB", "PREPARATION", "RELEASE CAUSE", "ENDED"]
    body = [[str(r.get("id") or "-"), str(r.get("kind") or "-"), str(r.get("gpu") or "-"),
             str(r.get("state") or "-"), _when(r.get("release_at")),
             "-" if r.get("minutes_left") is None else f"{float(r['minutes_left']):.1f}",
             f"{float(r.get('cost_usd') or 0):.2f}",
             "-" if r.get("hourly_usd") is None else f"{float(r['hourly_usd']):.2f}",
             str((r.get("last_job") or {}).get("outcome") or "-"),
             str(r.get("preparation_state") or "-"), str(r.get("release_reason") or "-"),
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


def _claim(*, gpu_types: list[str], datacenter: str | None, max_usd: float, max_hourly_usd: float | None,
           for_duration: str | None, capacity_timeout: str | None, wait_for_capacity: bool,
           consent_scopes: list[str], as_json: bool, claim: Callable[..., Any] | None,
           describe: Callable[..., Any] | None, input_fn: Callable[[str], str]) -> int:
    """Disclose one saved-storage policy, then submit fresh per-claim authority."""
    import sys

    from astrid.core.generation.cli import _gpu_placement
    from astrid.packs.runpod.target_selection import PaidRunConsent, RunPodPreferences, _number
    from astrid.sdk.generation import GenerationFacade

    try:
        cap = _number(max_usd, "USD cap")
        placement = _gpu_placement("runpod", gpu_types, argparse.ArgumentParser(prog="astrid machines claim"))
        timeout = _duration_seconds(capacity_timeout) if capacity_timeout else None
        if describe is None or claim is None:
            from astrid.sdk import AstridClient
            # Claim is served only by a running host; it must not launch a replacement.
            with AstridClient.open_from_launcher(start_pack_host=False) as client:
                facade = GenerationFacade()
                return _claim(gpu_types=gpu_types, datacenter=datacenter, max_usd=cap, max_hourly_usd=max_hourly_usd,
                              for_duration=for_duration, capacity_timeout=capacity_timeout,
                              wait_for_capacity=wait_for_capacity, consent_scopes=consent_scopes,
                              as_json=as_json, input_fn=input_fn,
                              describe=lambda **kwargs: facade.describe(client=client, **kwargs),
                              claim=lambda **kwargs: facade.claim(client=client, **kwargs))
        description = describe(placement=placement, max_hourly_usd=max_hourly_usd, fresh_claim=True, datacenter=datacenter)
        waiting = description["waiting_policy"]
        preferences = RunPodPreferences.from_mapping(waiting["preferences"])
        ceiling = max_hourly_usd
        if ceiling is None:
            ceiling = (description.get("offer") or {}).get("hourly_usd")
            if ceiling is None:
                raise ValueError("no current hourly quote; supply --max-hourly-usd to approve waiting for capacity")
        ceiling = _number(ceiling, "hourly USD ceiling")
        seconds = _duration_seconds(for_duration) if for_duration else float(preferences.idle_timeout_seconds)
        if seconds <= 0:
            seconds = 1800.0
        if cap < seconds * ceiling / 3600:
            seconds = cap / ceiling * 3600
        from astrid.packs.runpod.target_selection import policy_consent_digest
        consent = PaidRunConsent(
            invocation_id=f"astrid-machines-claim-{uuid.uuid4().hex}", offer_digest=None,
            max_usd=cap, deadline=None, consent=("paid",), preparation_seconds=0,
            execution_seconds=0, idle_seconds=seconds,
            policy_digest=policy_consent_digest(preferences, ceiling), max_hourly_usd=ceiling,
            capacity_timeout_seconds=timeout, wait_for_capacity=wait_for_capacity)
        consent.validate_authority(preferences, datetime.now(timezone.utc))
        policy = {"preferences": preferences.as_mapping(), "max_usd": cap,
                  "max_hourly_usd": ceiling, "retention_seconds": seconds,
                  "capacity_timeout_seconds": timeout, "wait_for_capacity": wait_for_capacity}
        print("RunPod claim policy: " + json.dumps(policy, sort_keys=True) +
              ". Capacity polling creates no new pod charges. Automatic release only happens while the owning Astrid host is running.",
              file=sys.stderr)
        if "paid" not in consent_scopes:
            if as_json or not sys.stdin.isatty() and input_fn is input:
                raise ValueError("claim requires paid consent; review the policy and pass --consent paid")
            if input_fn("Claim this paid machine? [y/N]: ").strip().lower() not in {"y", "yes"}:
                raise ValueError("claim cancelled; paid consent was not given")
        result = claim(consent=consent, requested_idle_seconds=seconds, placement=placement, datacenter=datacenter, wait=True)
        if not bool(getattr(result, "ok", True)):
            raise RuntimeError(str(getattr(result, "error", None)))
        raw = getattr(result, "raw_result", {})
        payload = raw.get("result", {}) if isinstance(raw, dict) else {}
        payload = payload.get("payload", payload) if isinstance(payload, dict) else {}
        target = payload.get("target_operation", {}) if isinstance(payload, dict) else {}
        machine_id = target.get("pod_id")
        machine = next((row for row in _leases() if row.get("id") == machine_id), None)
        if machine is None or machine.get("release_at") is None:
            raise TimeoutError("host returned no confirmed machine release time")
        print(json.dumps({"ok": True, "machine": machine, "policy": policy}, sort_keys=True) if as_json else
              f"claimed {machine_id}; release scheduled for {_when(machine['release_at'])} UTC "
              f"(spend cap ${cap:.2f}; hourly ceiling ${ceiling:.2f}). "
              "Automatic release only happens while the owning Astrid host is running.")
        return 0
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}) if as_json else str(exc))
        return 2
    except Exception as exc:
        unknown = isinstance(exc, TimeoutError) or "timeout" in str(exc).lower() or "timed out" in str(exc).lower()
        message = (f"claim result is unknown; check `astrid machines` before retrying ({exc})" if unknown else
                   f"claim failed: {exc}; check `astrid machines` before retrying")
        print(json.dumps({"ok": False, "error": message, "unknown": unknown}) if as_json else message)
        return 1


def _setup_show(*, as_json: bool, location: Callable[[], dict[str, Any]] | None = None) -> int:
    """Show non-secret RunPod settings and the Runtime app-state location."""
    from astrid.core.generation import settings
    from astrid.sdk.storage_root import resolve_runtime_data_root

    raw = settings.machine_policy("runpod") or {}
    allowed = {"provider_account_ref", "gpu_types", "image", "volume", "datacenter_id",
               "idle_timeout_seconds", "credential_ref", "budget_cap_usd"}
    preferences = {key: value for key, value in raw.items() if key in allowed} if isinstance(raw, dict) else {}
    if location is None:
        def location():
            root = resolve_runtime_data_root().expanduser().resolve()
            realm = None
            try:
                catalog = json.loads((root / "runtime" / "catalog.json").read_text(encoding="utf-8"))
                if isinstance(catalog, dict):
                    realm = catalog.get("selected_realm_id")
            except (OSError, ValueError, TypeError):
                pass
            return {"data_root": str(root), "realm": realm}
    store = {"document": "user/generation", **location()}
    payload = {"preferences": preferences, "runtime_user_app_state": store}
    print(json.dumps(payload, sort_keys=True, indent=2, default=str) if as_json else
          "RunPod machine preferences:\n" + json.dumps(preferences, sort_keys=True, indent=2, default=str)
          + "\nRuntime user app-state: " + store["document"]
          + "; data root=" + str(store.get("data_root"))
          + "; realm=" + str(store.get("realm") or "not selected"))
    return 0


def _setup(*, volume: str | None, datacenter: str | None, gpu_types: list[str], budget_usd: float | None,
           image: str | None,
           as_json: bool, setup: Callable[..., Any] | None = None,
           volumes: Callable[[], list[dict[str, Any]]] | None = None,
           datacenters: Callable[[], list[str]] | None = None,
           offered_gpus: Callable[[str | None, bool], list[str]] | None = None,
           input_fn: Callable[[str], str] = input) -> int:
    """Save RunPod setup through runpod.setup; inventory is read-only."""
    from astrid.core.generation import settings
    from astrid.packs.runpod.target_selection import (
        DEFAULT_RUNPOD_WORKER_IMAGE,
        RunPodPreferences,
        RunPodSetupRequiredError,
    )

    saved = settings.machine_policy("runpod") or {}
    if not isinstance(saved, dict):
        saved = {}
    credential_ref = saved.get("credential_ref") or "env:RUNPOD_API_KEY"
    if not isinstance(credential_ref, str) or not credential_ref.startswith("env:"):
        message = "saved RunPod credential reference is invalid; configure it with `astrid credential set runpod`"
        print(json.dumps({"ok": False, "error": message}) if as_json else message)
        return 2
    image_value = image or saved.get("image") or DEFAULT_RUNPOD_WORKER_IMAGE

    if (volumes is None or offered_gpus is None or
            (not as_json and volume is None and datacenter is None and datacenters is None)):
        from astrid.core.util.credentials_scope import CredentialsScope
        from astrid.packs.runpod import adapter
        from astrid.packs.runpod.worker_owner import _run_sync

        credential_name = credential_ref.removeprefix("env:")
        try:
            api_key = CredentialsScope.resolve_local("runpod", env_var=credential_name).value
            if not api_key:
                raise ValueError
        except Exception:
            message = "RunPod credential is missing; run `astrid credential set runpod`"
            print(json.dumps({"ok": False, "error": message}) if as_json else message)
            return 2
        volumes = volumes or (lambda: _run_sync(adapter.setup_volumes(api_key)))
        if not as_json and volume is None and datacenter is None:
            datacenters = datacenters or (lambda: _run_sync(adapter.setup_datacenters(api_key)))
        offered_gpus = offered_gpus or (lambda dc, has_volume: _run_sync(adapter.setup_gpu_types(
            api_key, datacenter_id=dc, require_network_volume=has_volume)))

    def inventory(call, *args):
        # Provider failures may include credentials in their exception text.
        try:
            return call(*args)
        except Exception:
            raise RuntimeError("RunPod inventory failed") from None

    try:
        account_volumes = inventory(volumes)
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
        volume_datacenter = (selected_volume or {}).get("dataCenterId")
        requested_datacenter = None if datacenter is not None and datacenter.lower() == "any" else datacenter
        if datacenter is None and as_json:
            requested_datacenter = saved.get("datacenter_id")
        if selected_volume is not None:
            if requested_datacenter is not None and requested_datacenter != volume_datacenter:
                raise ValueError("the selected network volume is fixed to a different datacenter")
            datacenter = volume_datacenter
        else:
            if datacenter is None and not as_json:
                choices = list(dict.fromkeys(inventory(datacenters)))
                print("Datacenters (enter an ID, or press Enter for any):")
                print("  any" + (("  " + "  ".join(choices)) if choices else ""))
                selected = input_fn("Datacenter [any]: ").strip()
                requested_datacenter = None if not selected or selected.lower() == "any" else selected
                if requested_datacenter is not None and requested_datacenter not in choices:
                    raise ValueError(f"RunPod datacenter {requested_datacenter!r} was not found; retry `astrid machines setup`")
            datacenter = requested_datacenter
        candidates = inventory(offered_gpus, datacenter, selected_volume is not None)
        chosen = list(gpu_types or saved.get("gpu_types") or ())
        if not candidates and chosen and datacenter:
            raise ValueError(f"GPU type {chosen[0]!r} is not offered in datacenter {datacenter!r}")
        if not candidates:
            raise ValueError("RunPod GPU offerings could not be confirmed; preferences were not saved")
        if not chosen and not as_json:
            print("GPU types offered there (enter a comma-separated ordered list):")
            print("  " + ", ".join(candidates))
            chosen = [part.strip() for part in input_fn("GPU types: ").split(",") if part.strip()]
        if not chosen:
            raise RunPodSetupRequiredError(("gpu_types",))
        invalid_gpu = next((gpu for gpu in chosen if gpu not in candidates), None)
        if invalid_gpu is not None:
            if datacenter:
                raise ValueError(f"GPU type {invalid_gpu!r} is not offered in datacenter {datacenter!r}")
            raise ValueError(f"GPU type {invalid_gpu!r} was not found in the RunPod GPU catalog")
        budget = budget_usd if budget_usd is not None else saved.get("budget_cap_usd")
        if budget is None and not as_json:
            budget = float(input_fn("Maximum paid-run budget (USD): ").strip())
        if type(budget) not in (int, float) or not math.isfinite(float(budget)) or float(budget) <= 0:
            raise RunPodSetupRequiredError(("budget_cap_usd",))
        policy = dict(saved)
        policy["provider_account_ref"] = saved.get("provider_account_ref") or credential_ref
        policy["image"] = image_value
        policy["credential_ref"] = credential_ref
        policy.update({"gpu_types": chosen, "budget_cap_usd": float(budget),
                       "datacenter_id": datacenter})
        if selected_volume is None:
            policy.pop("volume", None)
        else:
            policy["volume"] = {"id": selected_volume["id"], "name": selected_volume["name"],
                                "size_gb": selected_volume.get("size_gb", selected_volume.get("size")),
                                "mount_path": saved.get("volume", {}).get("mount_path", "/workspace")}
        validated = RunPodPreferences.from_mapping(policy).as_mapping()
        try:
            if setup is None:
                import json as _json

                from astrid.sdk import AstridClient

                with AstridClient.open_from_launcher() as client:
                    result = client.invoke_result("runpod.setup", kind="action", project=None,
                                                  inputs={"placement_policy": _json.dumps(
                                                      {"kind": "runpod", "engine": "comfyui",
                                                       "preferences": validated}, sort_keys=True)},
                                                  wait=True, read_managed_outputs=False)
            else:
                result = setup(validated)
        except Exception:
            # Invocation exceptions can contain credential/provider details. Keep
            # the user-facing failure fixed and sanitized; action result codes
            # below are safe to report when the Runtime returns one.
            result = type("SetupFailure", (), {
                "ok": False,
                "error": {"code": "runpod_setup_invocation_failed"},
            })()
        ok = bool(getattr(result, "ok", True))
        payload = {"ok": ok, "preferences": validated}
        if not ok:
            error = getattr(result, "error", None)
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not code.strip():
                code = "runpod_setup_failed"
            payload["error"] = {"code": code, "message":
                                "RunPod preferences were not saved; check the error code and retry `astrid machines setup`."}
        print(json.dumps(payload, sort_keys=True, default=str) if as_json else
              (f"Saved RunPod machine policy: GPUs {', '.join(chosen)}; datacenter {datacenter or 'any'}; "
               f"budget ${float(budget):.2f}/run; volume {selected_volume.get('name') if selected_volume else 'ephemeral disk'}. "
               + (f"{', '.join(gpu for gpu in chosen if gpu in getattr(candidates, 'stock_missing', ()))} "
                  f"is not in stock in {datacenter} right now; a paid run will wait for capacity. "
                  if datacenter and any(gpu in getattr(candidates, 'stock_missing', ()) for gpu in chosen) else "")
               + ("RunPod returned no datacenter GPU availability rows; using the global catalog as a fallback. "
                  if getattr(candidates, "availability_fallback", False) else "")
               + "No machine was claimed; each paid run still needs consent."
               if ok else f"RunPod setup failed ({payload['error']['code']}): {payload['error']['message']}"))
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
         claim: Callable[..., Any] | None = None, describe: Callable[..., Any] | None = None,
         volumes: Callable[[], list[dict[str, Any]]] | None = None,
         datacenters: Callable[[], list[str]] | None = None,
         offered_gpus: Callable[[str | None, bool], list[str]] | None = None,
         show_location: Callable[[], dict[str, Any]] | None = None,
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
    configure.add_argument("--volume", help="Existing network volume ID or name; interactive setup offers ephemeral disk when omitted.")
    configure.add_argument("--datacenter", help="RunPod datacenter ID, or 'any' for no datacenter restriction.")
    configure.add_argument("--gpu", action="append", dest="gpu_types", default=[], help="Ordered GPU choice; repeat for fallbacks.")
    configure.add_argument("--budget-usd", type=float, help="Maximum paid-run budget in USD.")
    configure.add_argument("--image", help="Override the worker image with a name@sha256:digest pin for this setup.")
    configure.add_argument("--show", action="store_true", help="Show saved preferences and Runtime app-state location.")
    configure.add_argument("--json", action="store_true", dest="setup_json")
    acquire = sub.add_parser("claim", help="Claim a RunPod machine on saved storage with paid consent.")
    acquire.add_argument("--gpu", action="append", dest="gpu_types", default=[], help="Ordered GPU choice; repeat for fallbacks.")
    acquire.add_argument("--datacenter", help="Override the saved datacenter for this claim; a saved volume must remain in its datacenter.")
    acquire.add_argument("--max-usd", type=float, required=True, help="Maximum initial machine spend in USD.")
    acquire.add_argument("--max-hourly-usd", type=float, help="Hourly ceiling for waiting; defaults to the current quoted rate.")
    acquire.add_argument("--for", dest="claim_for", metavar="DURATION", help="Initial idle retention, capped by the spend limit; defaults to saved idle retention or 30m.")
    capacity = acquire.add_mutually_exclusive_group()
    capacity.add_argument("--capacity-timeout", metavar="DURATION", help="Stop waiting after this duration; default waits until claimed or cancelled.")
    capacity.add_argument("--no-wait-for-capacity", action="store_true", help="Make one capacity observation and at most one create attempt.")
    acquire.add_argument("--consent", action="append", choices=("paid",), default=[], help="Approve the displayed paid claim policy without prompting.")
    acquire.add_argument("--json", action="store_true", dest="claim_json")
    parsed = parser.parse_args(list(argv or ()))
    as_json = parsed.json or bool(getattr(parsed, "release_json", False))
    if parsed.command == "release":
        return _release(parsed.machine_id, as_json=as_json, release=release)
    if parsed.command == "keep":
        return _keep(parsed.machine_id, for_duration=parsed.keep_for,
                     until_release=parsed.until_release, max_usd=parsed.max_usd,
                     as_json=as_json or parsed.keep_json, keep=keep)
    if parsed.command == "claim":
        return _claim(gpu_types=parsed.gpu_types, datacenter=parsed.datacenter, max_usd=parsed.max_usd,
                      max_hourly_usd=parsed.max_hourly_usd, for_duration=parsed.claim_for,
                      capacity_timeout=parsed.capacity_timeout, wait_for_capacity=not parsed.no_wait_for_capacity,
                      consent_scopes=parsed.consent, as_json=as_json or parsed.claim_json,
                      claim=claim, describe=describe, input_fn=input_fn)
    if parsed.command == "setup":
        if parsed.show:
            return _setup_show(as_json=as_json or parsed.setup_json, location=show_location)
        return _setup(volume=parsed.volume, datacenter=parsed.datacenter, gpu_types=parsed.gpu_types,
                      budget_usd=parsed.budget_usd, image=parsed.image,
                      as_json=as_json or parsed.setup_json,
                      setup=setup, volumes=volumes, datacenters=datacenters,
                      offered_gpus=offered_gpus, input_fn=input_fn)
    rows = _leases()
    print(json.dumps(rows, sort_keys=True, indent=2) if as_json else render(rows))
    return 0


__all__ = ["main", "render"]
