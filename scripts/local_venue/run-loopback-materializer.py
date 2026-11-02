#!/usr/bin/env python3
"""Run the exact R-24 materializer with a task-local loopback bind shim."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
from pathlib import Path

HOST = "127.0.0.1"
PORT = 42000
PINOKIOD_REF = "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
MaterializerError = RuntimeError


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _argument(argv: list[str], name: str) -> Path:
    try:
        value = argv[argv.index(name) + 1]
    except (ValueError, IndexError) as exc:
        raise SystemExit(f"missing required launcher argument: {name}") from exc
    selected = Path(value)
    if not selected.is_absolute():
        raise SystemExit(f"launcher argument must be absolute: {name}")
    return selected


def _require_loopback_port_free(port: int = PORT) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((HOST, port))
    except OSError as exc:
        raise MaterializerError(
            f"local-only loopback port {port} is unavailable before pinokiod launch"
        ) from exc


def _write_config(*, run_root: Path, runner: Path, pinokiod: Path,
                  preload: Path, result: int) -> None:
    materializer = runner / "scripts/ci_journey_r24_materializer.py"
    entry = pinokiod / "script/index.js"
    server = pinokiod / "server/index.js"
    config = {
        "schema": "astrid.pinokio.local-venue-bind.v1",
        "scope": "local-reproduction-only",
        "result": result,
        "bind_host": HOST,
        "port": PORT,
        "pterm_endpoint": f"pinokio://{HOST}:{PORT}/api/astrid",
        "hosted_default_changed": False,
        "pinned_checkout_changed": False,
        "preexisting_node_options_preserved": bool(
            os.environ.get("ASTRID_LOCAL_PREEXISTING_NODE_OPTIONS")
        ),
        "effective_node_options_sha256": hashlib.sha256(
            os.environ["NODE_OPTIONS"].encode("utf-8")
        ).hexdigest(),
        "pinokiod_ref": PINOKIOD_REF,
        "hashes": {
            "launcher_sha256": _sha(Path(__file__).resolve()),
            "preload_sha256": _sha(preload),
            "materializer_sha256": _sha(materializer),
            "pinokiod_entry_sha256": _sha(entry),
            "pinokiod_server_sha256": _sha(server),
        },
        "runtime_patch": (
            "one-shot Node preload changes only the pinned entrypoint process's "
            "first listen(42000, callback) call to listen(42000, 127.0.0.1, callback)"
        ),
        "preflight_patch": "probe 127.0.0.1:42000 for this local run only",
    }
    target = run_root / "evidence/materializer/local-venue-bind.json"
    target.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="ascii")


def main(argv: list[str]) -> int:
    runner = _argument(argv, "--runner").resolve(strict=True)
    pinokiod = _argument(argv, "--pinokiod").resolve(strict=True)
    run_root = _argument(argv, "--run-root").resolve()
    entry = (pinokiod / "script/index.js").resolve(strict=True)
    preload = (Path(__file__).parent / "pinokiod-loopback-preload.cjs").resolve(strict=True)

    sys.path.insert(0, str(runner))

    global MaterializerError
    from scripts import ci_journey_r24_materializer as materializer

    MaterializerError = materializer.MaterializerError
    if materializer.PINOKIOD_REF != PINOKIOD_REF or materializer.PORT != PORT:
        raise SystemExit("pinned materializer daemon contract differs")

    existing_node_options = os.environ.get("NODE_OPTIONS", "").strip()
    if "\x00" in existing_node_options or "\n" in existing_node_options:
        raise SystemExit("unsafe pre-existing NODE_OPTIONS encoding")
    os.environ.update({
        "ASTRID_LOCAL_PINOKIOD_BIND_HOST": HOST,
        "ASTRID_LOCAL_PINOKIOD_ENTRY": str(entry),
        "ASTRID_LOCAL_PREEXISTING_NODE_OPTIONS": "1" if existing_node_options else "",
        "NODE_OPTIONS": f"{existing_node_options} --require={preload}".strip(),
    })
    materializer._require_port_free = _require_loopback_port_free
    result = materializer.main(argv)
    _write_config(
        run_root=run_root,
        runner=runner,
        pinokiod=pinokiod,
        preload=preload,
        result=result,
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
