"""Read-only discovery and managed image helpers for a user's local ComfyUI."""

from __future__ import annotations

import argparse
import http.client
import io
import ipaddress
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from astrid.core.contracts.errors import AstridError, AstridValueError, render_astrid_error
from astrid.packs.vibecomfy.asset_manifest import build_asset_manifest

_PINOKIO_INFO_URL = "http://127.0.0.1:42000/pinokio/info"
_PINOKIO_SCAN_BUDGET_SECONDS = 0.5
_PINOKIO_REQUEST_TIMEOUT_SECONDS = 0.2
_PINOKIO_EXTRA_PROBE_TIMEOUT_SECONDS = 0.1
_PINOKIO_MAX_EXTRA_PORTS = 8
# Keep a stalled optional request from accumulating workers across rescans.
_PINOKIO_SCAN_LOCK = threading.Lock()
_LAUNCH_OBSERVATION_TIMEOUT_SECONDS = 0.2


def _get_json(url: str, timeout: float = 2) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _error(port: int, endpoint: str, exc: Exception) -> dict[str, Any]:
    return {"port": port, "endpoint": endpoint, "error": str(exc)}


def _first_custom_nodes(folder_paths: Any) -> str | None:
    if not isinstance(folder_paths, dict):
        return None
    paths = folder_paths.get("custom_nodes")
    if isinstance(paths, str):
        return paths
    if isinstance(paths, list):
        return next((item for item in paths if isinstance(item, str) and item), None)
    return None


def _output_directory(argv: list[str], base_dir: Path | None) -> str | None:
    for index, arg in enumerate(argv):
        if arg == "--output-directory" and index + 1 < len(argv):
            return argv[index + 1]
        if arg.startswith("--output-directory="):
            return arg.partition("=")[2]
    return str(base_dir / "output") if base_dir else None


def _python_guess(base_dir: Path | None, embedded: bool) -> str | None:
    if base_dir is None:
        return None
    if embedded:
        candidate = base_dir.parent / "python_embeded" / "python.exe"
        return str(candidate) if candidate.exists() else None
    candidates = (
        base_dir / "env" / "bin" / "python",
        base_dir / "env" / "Scripts" / "python.exe",
        base_dir / "venv" / "bin" / "python",
        base_dir / ".venv" / "bin" / "python",
        base_dir / "venv" / "Scripts" / "python.exe",
        base_dir / ".venv" / "Scripts" / "python.exe",
    )
    return str(next((path for path in candidates if path.is_file()), "")) or None


def _observed_launch_facts(server: dict[str, Any], *, observer=None,
                           deadline: float | None = None) -> dict[str, Any]:
    """Retain native direct-launch evidence; HTTP argv/Python guesses cannot authorize spawn.

    Discovery stays read-only. Unsupported wrappers, inaccessible processes or
    an unrelated listener simply leave launch unavailable for manual start.
    The launch service independently verifies these facts again on each call.
    """
    from astrid.core.generation.target_launch import (
        ProcLaunchObserver, ProcessObservation, prepare_target_launch,
    )
    from astrid.core.generation.targets import TargetRecord

    try:
        root = server.get('base_dir')
        if not isinstance(root, str) or not Path(root).is_absolute():
            return {}
        bound = time.monotonic() + _LAUNCH_OBSERVATION_TIMEOUT_SECONDS
        deadline = min(deadline, bound) if deadline is not None else bound
        if time.monotonic() >= deadline:
            return {}
        observer = observer or ProcLaunchObserver()
        listeners = observer.listeners(server['url'], deadline=deadline)
        if len(listeners) != 1 or not isinstance(listeners[0], ProcessObservation):
            return {}
        process = listeners[0]
        if not process.argv:
            return {}
        facts = {'launch_app': str(Path(root) / 'main.py'),
                 'launch_argv': list(process.argv),
                 'launch_process': {'pid': process.pid, 'birth_id': process.birth_id}}
        record = TargetRecord(endpoint=server['url'], root=root,
                              python=process.argv[0], facts=facts)
        plan = prepare_target_launch(record)
        if (process.root != plan.paths[0].resolved
                or process.executable != plan.paths[1].resolved
                or observer.process(process.pid, deadline=deadline) != process
                or observer.listeners(plan.endpoint, deadline=deadline) != (process,)
                or prepare_target_launch(record) != plan
                or time.monotonic() >= deadline):
            return {}
        return {'python': plan.python, **facts}
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, IndexError):
        return {}


def _manager_present(base_dir: Path | None) -> bool:
    custom_nodes = base_dir / "custom_nodes" if base_dir else None
    if custom_nodes is None or not custom_nodes.is_dir():
        return False
    try:
        return any(
            entry.is_dir() and entry.name.casefold() == "comfyui-manager"
            for entry in custom_nodes.iterdir()
        )
    except OSError:
        return False


def _loopback_http_port(value: str) -> int | None:
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        if parsed.scheme.casefold() != "http" or not host:
            return None
        if host.casefold() not in {"localhost", "localhost."}:
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    return None
            except ValueError:
                return None
        port = parsed.port if parsed.port is not None else 80
        return port if 1 <= port <= 65535 else None
    except ValueError:
        return None


def _pinokio_scripts(info: Any) -> list[dict[str, Any]]:
    if not isinstance(info, dict):
        return []
    scripts = info.get("scripts", [])
    if isinstance(scripts, dict):
        scripts = list(scripts.values())
    if not isinstance(scripts, list):
        return []
    return [script for script in scripts if isinstance(script, dict)]


def _script_app(script: dict[str, Any]) -> str | None:
    app = script.get("app")
    if isinstance(app, str) and app.strip():
        return app.strip()
    path = script.get("path")
    if isinstance(path, str) and path.strip():
        parts = [
            part
            for part in Path(path.replace("\\", "/")).parts
            if part not in {"/", "\\"}
        ]
        if parts:
            app_path = next((part for part in parts if part.casefold().endswith(".git")), None)
            if app_path:
                return app_path
            return parts[0]
    return None


def _pinokio_candidates(
    info: dict[str, Any], already_probed: set[int]
) -> tuple[list[int], dict[int, list[str]]]:
    candidates: list[int] = []
    apps_by_port: dict[int, list[str]] = {}
    for script in _pinokio_scripts(info):
        if script.get("running") is False:
            continue
        if str(script.get("status", "")).casefold() in {"stopped", "finished", "failed"}:
            continue
        local = script.get("local")
        url = local.get("url") if isinstance(local, dict) else None
        if not isinstance(url, str):
            continue
        port = _loopback_http_port(url)
        if port is None:
            continue
        if port not in already_probed and port not in candidates:
            if len(candidates) >= _PINOKIO_MAX_EXTRA_PORTS:
                continue
            candidates.append(port)
        app = _script_app(script)
        apps = apps_by_port.setdefault(port, [])
        if app and app not in apps:
            apps.append(app)
    return candidates, apps_by_port


def _probe_server(
    port: int,
    *,
    timeout: float = 2,
    require_comfyui_shape: bool = False,
    deadline: float | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    url = f"http://127.0.0.1:{port}"
    errors: list[dict[str, Any]] = []

    def get_json(endpoint: str) -> Any:
        request_timeout = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Pinokio scan budget expired")
            request_timeout = min(timeout, remaining)
        return _get_json(url + endpoint, timeout=request_timeout)

    try:
        stats = get_json("/system_stats")
    except (OSError, urllib.error.URLError, http.client.HTTPException, TimeoutError, ValueError) as exc:
        if not require_comfyui_shape:
            errors.append(_error(port, "/system_stats", exc))
        return None, errors

    if require_comfyui_shape and not (
        isinstance(stats, dict)
        and isinstance(stats.get("system"), dict)
        and isinstance(stats.get("devices"), list)
    ):
        return None, errors

    system = stats.get("system", {}) if isinstance(stats, dict) else {}
    if not isinstance(system, dict):
        system = {}
    argv_value = system.get("argv", [])
    argv = [str(value) for value in argv_value] if isinstance(argv_value, list) else []
    embedded = bool(system.get("embedded_python", False))
    server: dict[str, Any] = {
        "url": url,
        "comfyui_version": system.get("comfyui_version"),
        "python_version": system.get("python_version"),
        "embedded_python": embedded,
        "argv": argv,
        "folder_paths": None,
        "base_dir": None,
        "output_dir": None,
        "workflows": None,
        "manager_present": False,
        "python": None,
    }
    try:
        folders = get_json("/internal/folder_paths")
        server["folder_paths"] = folders
        custom_nodes = _first_custom_nodes(folders)
        base_dir = Path(custom_nodes).parent if custom_nodes else None
        server["base_dir"] = str(base_dir) if base_dir else None
        server["output_dir"] = _output_directory(argv, base_dir)
        server["manager_present"] = _manager_present(base_dir)
        server["python"] = _python_guess(base_dir, embedded)
    except (OSError, urllib.error.URLError, http.client.HTTPException, TimeoutError, ValueError) as exc:
        errors.append(_error(port, "/internal/folder_paths", exc))
    try:
        query = urllib.parse.urlencode({"dir": "workflows"})
        workflows = get_json("/userdata?" + query)
    except (OSError, urllib.error.URLError, http.client.HTTPException, TimeoutError, ValueError) as exc:
        errors.append(_error(port, "/userdata?dir=workflows", exc))
    else:
        if isinstance(workflows, list):
            server["workflows"] = workflows
        else:
            errors.append({
                "port": port,
                "endpoint": "/userdata?dir=workflows",
                "error": "workflow listing was not a JSON list",
            })
    server.update(_observed_launch_facts(server, deadline=deadline))
    return server, errors


def _pinokio_discovery(
    already_probed: set[int], deadline: float
) -> tuple[
    dict[str, Any] | None,
    dict[int, list[str]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            info = None
        else:
            try:
                info = _get_json(
                    _PINOKIO_INFO_URL,
                    timeout=min(_PINOKIO_REQUEST_TIMEOUT_SECONDS, remaining),
                )
            except (OSError, urllib.error.URLError, http.client.HTTPException, TimeoutError, ValueError):
                info = None
        if not isinstance(info, dict):
            return None, {}, [], []

        candidates, apps_by_port = _pinokio_candidates(info, already_probed)
        servers: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for port in candidates:
            if time.monotonic() >= deadline:
                break
            server, probe_errors = _probe_server(
                port,
                timeout=_PINOKIO_EXTRA_PROBE_TIMEOUT_SECONDS,
                require_comfyui_shape=True,
                deadline=deadline,
            )
            if server is not None:
                servers.append(server)
                errors.extend(probe_errors)
        return info, apps_by_port, servers, errors
    except (
        OSError,
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        ValueError,
        TypeError,
        AttributeError,
    ):
        return None, {}, [], []


def _bounded_pinokio_discovery(
    already_probed: set[int],
) -> tuple[
    dict[str, Any] | None,
    dict[int, list[str]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Run the optional Pinokio lookup and probes within one wall-clock budget."""
    empty: tuple[
        dict[str, Any] | None,
        dict[int, list[str]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ] = (None, {}, [], [])
    if not _PINOKIO_SCAN_LOCK.acquire(blocking=False):
        return empty

    deadline = time.monotonic() + _PINOKIO_SCAN_BUDGET_SECONDS
    result: list[tuple[Any, float]] = []

    def run() -> None:
        try:
            result.append((_pinokio_discovery(already_probed, deadline), time.monotonic()))
        finally:
            _PINOKIO_SCAN_LOCK.release()

    worker = threading.Thread(target=run, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _PINOKIO_SCAN_LOCK.release()
        return empty
    # urllib's timeout is an inactivity limit, so bound the whole optional phase here.
    worker.join(timeout=max(0, deadline - time.monotonic()))
    if worker.is_alive() or not result or result[0][1] > deadline:
        return empty
    return result[0][0]


def scan(ports: list[int]) -> dict[str, Any]:
    """Probe loopback ComfyUI servers and report endpoint errors as data."""
    servers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    base_ports = list(dict.fromkeys(ports))
    for port in base_ports:
        server, port_errors = _probe_server(port)
        errors.extend(port_errors)
        if server is not None:
            servers.append(server)

    pinokio_info, pinokio_apps, extra_servers, extra_errors = _bounded_pinokio_discovery(
        set(base_ports)
    )
    pinokio_home = pinokio_info.get("home") if pinokio_info else None
    if not isinstance(pinokio_home, str):
        pinokio_home = None
    for server in servers:
        port = urllib.parse.urlsplit(server["url"]).port
        if port is not None and pinokio_apps.get(port):
            server["pinokio_apps"] = pinokio_apps[port]
    for server in extra_servers:
        port = urllib.parse.urlsplit(server["url"]).port
        if port is not None and pinokio_apps.get(port):
            server["pinokio_apps"] = pinokio_apps[port]
    servers.extend(extra_servers)
    errors.extend(extra_errors)
    return {
        "servers": servers,
        "errors": errors,
        "pinokio": {"available": pinokio_info is not None, "home": pinokio_home},
    }


def pack_images(out_path: str, images: list[str]) -> dict[str, Any]:
    """Write the canonical managed_assets archive accepted by vibecomfy.run."""
    target = Path(out_path).expanduser()
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"refusing to overwrite existing path: {target}")
    bindings: dict[str, Path] = {}
    for index, raw in enumerate(images, start=1):
        source = Path(raw).expanduser()
        if source.is_symlink():
            raise AstridValueError(f"image must not be a symlink: {source}")
        source = source.resolve(strict=True)
        if not source.is_file():
            raise AstridValueError(f"image is not a regular file: {source}")
        binding = "image" if index == 1 else f"image_{index}"
        bindings[binding] = source
    manifest = build_asset_manifest(bindings)
    names_by_binding = {
        record["binding"]: Path(record["member"]).name for record in manifest["assets"]
    }
    names = [
        names_by_binding["image" if index == 1 else f"image_{index}"]
        for index in range(1, len(images) + 1)
    ]
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        for record in manifest["assets"]:
            member = record["member"]
            archive.write(bindings[record["binding"]], member)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation closes the race between the existence check and write.
    with target.open("xb") as stream:
        stream.write(payload.getvalue())
    return {"zip": str(target), "names": names}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only helpers for local ComfyUI.")
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan", help="discover running loopback ComfyUI servers")
    scan_parser.add_argument("--ports", default="8188,8000")
    images_parser = commands.add_parser("pack-images", help="build a managed_assets ZIP")
    images_parser.add_argument("out")
    images_parser.add_argument("images", nargs="+")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            try:
                ports = [int(value.strip()) for value in args.ports.split(",") if value.strip()]
            except ValueError as exc:
                result = {
                    "servers": [],
                    "errors": [{"port": args.ports, "endpoint": "--ports", "error": str(exc)}],
                }
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0
            if any(port < 1 or port > 65535 for port in ports):
                result = {
                    "servers": [],
                    "errors": [{
                        "port": args.ports,
                        "endpoint": "--ports",
                        "error": "ports must be between 1 and 65535",
                    }],
                }
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
                return 0
            result = scan(ports)
        else:
            result = pack_images(args.out, args.images)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        render_astrid_error(AstridError(
            f"vibecomfy.local: {exc}",
            recovery_command="python3 -m astrid.packs.vibecomfy.local --help",
        ))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
