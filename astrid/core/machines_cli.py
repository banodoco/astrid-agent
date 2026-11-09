"""``astrid machines``: rented generation machines (Plan 32 T5d).

    astrid machines [--json]          live and recently ended leases
    astrid machines release <id>      release one live machine (through the host's lease owner)

Each row: id (the machine id; RunPod: pod id), kind, gpu, state, idle expiry,
cost so far, end reason. The list reads the lease journal only; it never calls
the provider and never takes the account lock. Release is an ordinary Runtime
task served by the retained host, the only owner allowed to stop paid machines.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any, Callable, Sequence


def _leases() -> list[dict[str, Any]]:
    from astrid.packs.runpod.machine import journal_leases

    return journal_leases()


def _when(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M")


def render(rows: Sequence[dict[str, Any]]) -> str:
    header = ["ID", "KIND", "GPU", "STATE", "IDLE EXPIRES (UTC)", "COST (USD)", "ENDED"]
    body = [[str(r.get("id") or "-"), str(r.get("kind") or "-"), str(r.get("gpu") or "-"),
             str(r.get("state") or "-"), _when(r.get("idle_expires_at")), f"{float(r.get('cost_usd') or 0):.2f}",
             str(r.get("ended") or "-")] for r in rows]
    widths = [max([len(header[i])] + [len(row[i]) for row in body]) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in [header, *body]]
    live = sum(1 for r in rows if r.get("live"))
    return "\n".join([*lines, f"{live} live, {len(rows) - live} ended"])


def _release(machine_id: str, *, as_json: bool, release: Callable[[], Any] | None) -> int:
    live = [row for row in _leases() if row.get("live")]
    if machine_id not in {row["id"] for row in live}:
        message = (f"no live machine {machine_id!r}; live machines: "
                   f"{', '.join(row['id'] for row in live) or 'none'}")
        print(json.dumps({"ok": False, "error": message}) if as_json else message)
        return 2
    if release is None:
        from astrid.sdk.generation import GenerationFacade

        def release():
            from astrid.sdk import AstridClient
            with AstridClient.open_from_launcher() as client:
                return GenerationFacade().release(engine="comfyui", placement="runpod", client=client, wait=True)
    result = release()
    ok = bool(getattr(result, "ok", True))
    rows = [row for row in _leases() if row.get("id") == machine_id]
    payload = {"ok": ok, "machine": rows[0] if rows else None}
    if not ok:
        payload["error"] = getattr(result, "error", None)
    print(json.dumps(payload, sort_keys=True, default=str) if as_json
          else (f"released {machine_id}" if ok else f"release of {machine_id} failed: {payload['error']}"))
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None, *, release: Callable[[], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astrid machines", description="Rented generation machines.")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    stop = sub.add_parser("release", help="Release one live machine.")
    stop.add_argument("machine_id")
    stop.add_argument("--json", action="store_true", dest="release_json")
    parsed = parser.parse_args(list(argv or ()))
    as_json = parsed.json or bool(getattr(parsed, "release_json", False))
    if parsed.command == "release":
        return _release(parsed.machine_id, as_json=as_json, release=release)
    rows = _leases()
    print(json.dumps(rows, sort_keys=True, indent=2) if as_json else render(rows))
    return 0


__all__ = ["main", "render"]
