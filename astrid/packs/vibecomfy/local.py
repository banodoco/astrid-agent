"""Loopback ComfyUI discovery and managed image helpers."""

from __future__ import annotations

import argparse
import http.client
import io
import json
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from astrid.core.cli.domain_output import print_result
from astrid.core.contracts.errors import AstridError, AstridValueError, render_astrid_error
from astrid.packs.vibecomfy.asset_manifest import build_asset_manifest
from astrid.sdk import AstridClient
from astrid.sdk.contracts import DomainResult
from astrid.sdk.exceptions import ServiceUnavailableError

_WORKFLOW_LIMIT = 64 * 1024 * 1024


def _get_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=2) as response:
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
        base_dir / "venv" / "bin" / "python",
        base_dir / ".venv" / "bin" / "python",
        base_dir / "venv" / "Scripts" / "python.exe",
        base_dir / ".venv" / "Scripts" / "python.exe",
    )
    return str(next((path for path in candidates if path.is_file()), "")) or None


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


def scan(ports: list[int]) -> dict[str, Any]:
    """Probe loopback ComfyUI servers and report endpoint errors as data."""
    servers: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for port in ports:
        url = f"http://127.0.0.1:{port}"
        try:
            stats = _get_json(url + "/system_stats")
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            errors.append(_error(port, "/system_stats", exc))
            continue
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
            folders = _get_json(url + "/internal/folder_paths")
            server["folder_paths"] = folders
            custom_nodes = _first_custom_nodes(folders)
            base_dir = Path(custom_nodes).parent if custom_nodes else None
            server["base_dir"] = str(base_dir) if base_dir else None
            server["output_dir"] = _output_directory(argv, base_dir)
            server["manager_present"] = _manager_present(base_dir)
            server["python"] = _python_guess(base_dir, embedded)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
            errors.append(_error(port, "/internal/folder_paths", exc))
        try:
            query = urllib.parse.urlencode({"dir": "workflows"})
            workflows = _get_json(url + "/userdata?" + query)
        except (OSError, urllib.error.URLError, TimeoutError, ValueError) as exc:
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
        servers.append(server)
    return {"servers": servers, "errors": errors}


def import_workflow(name: str, project: str, comfy_url: str | None = None) -> Any:
    """Fetch one saved workflow from loopback ComfyUI and import it into Astrid."""
    if not isinstance(project, str) or not project.strip():
        raise ValueError("project is required")
    url = str(comfy_url or "http://127.0.0.1:8188").rstrip("/")
    try:
        parts = urllib.parse.urlsplit(url)
        loopback = parts.hostname in ("127.0.0.1", "localhost")
    except ValueError:
        parts = None
        loopback = False
    if (parts is None or parts.scheme != "http" or not loopback or parts.path not in ("", "/") or
            parts.username is not None or parts.password is not None or parts.query or parts.fragment):
        raise ValueError("comfy_url must be loopback, e.g. http://127.0.0.1:8188")
    name = str(name or "")
    if "\x00" in name or not name.endswith(".json") or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("name must be a saved workflow file name such as portrait.json (see scan)")
    source = f"{url}/userdata/{urllib.parse.quote('workflows/' + name, safe='')}"
    try:
        class RefuseRedirects(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), RefuseRedirects)
        with opener.open(source, timeout=30) as response:
            raw = response.read(_WORKFLOW_LIMIT + 1)
    except (http.client.HTTPException, OSError, urllib.error.URLError) as exc:
        raise ValueError(f"could not read {source}: {exc}") from exc
    if len(raw) > _WORKFLOW_LIMIT:
        raise ValueError("workflow is larger than 64 MiB")
    try:
        json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{name} is not JSON") from exc
    with tempfile.TemporaryDirectory(prefix="astrid-comfy-workflow-") as scratch:
        target = Path(scratch) / name
        target.write_bytes(raw)
        with AstridClient.open_from_launcher(start_pack_host=False) as client:
            return client.media.import_file(
                project=project,
                path=target,
                realm="managed_local",
                idempotency_key=None,
            )


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
    parser = argparse.ArgumentParser(description="Helpers for local ComfyUI workflows and managed images.")
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan", help="discover running loopback ComfyUI servers")
    scan_parser.add_argument("--ports", default="8188,8000")
    images_parser = commands.add_parser("pack-images", help="build a managed_assets ZIP")
    images_parser.add_argument("out")
    images_parser.add_argument("images", nargs="+")
    workflow_parser = commands.add_parser("import-workflow", help="import a saved loopback ComfyUI workflow")
    workflow_parser.add_argument("name", help="Saved workflow file name, such as portrait.json.")
    workflow_parser.add_argument("--project", required=True, help="Owning project id or immutable slug.")
    workflow_parser.add_argument(
        "--comfy-url",
        default="http://127.0.0.1:8188",
        help="Loopback ComfyUI URL (default http://127.0.0.1:8188).",
    )
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
        elif args.command == "pack-images":
            result = pack_images(args.out, args.images)
        else:
            try:
                result = import_workflow(args.name, args.project, args.comfy_url)
            except ServiceUnavailableError as exc:
                result = DomainResult.failure(exc.to_error_object())
            return print_result(result, as_json=True)
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
