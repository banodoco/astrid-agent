"""Create a small, redacted support archive from existing local observations."""

from __future__ import annotations

import getpass
import json
import os
import re
import stat
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any

from astrid.core.gateway.diagnostics import redact_for_shared

_MAX_LOG_BYTES = 256 * 1024
_MAX_TOTAL_LOG_BYTES = 1024 * 1024
_SECRET_TEXT = re.compile(r"(?i)(['\"]?(?:[A-Z0-9]{1,32}_){0,8}(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|credential|password|secret|token|signature|sig|owner_id)['\"]?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY_BEGIN = re.compile(r"^-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_PRIVATE_KEY_END = re.compile(r"^-----END [A-Z0-9 ]*PRIVATE KEY-----")
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@")
_SECRET_KEY = re.compile(r"(?i)(?:credential|token|secret|password|api[_-]?key|authorization|owner_id)")
_USER_PATH = re.compile(r"(?i)(?:[a-z]:[\\/](?:users|documents and settings)[\\/]|/(?:users|home)/)[^\s\"'<>;,)]*")
_CHAT_CONTENT_KEY = re.compile(r"(?i)^(?:chat|chats|conversation|conversations|transcript|transcripts|messages|prompt|prompts|completion|completions|content)$")


def _redact_identity(text: str) -> str:
    home = str(Path.home())
    for spelling in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
        if spelling not in {"/", "\\", "."}:
            text = re.sub(re.escape(spelling), "<redacted:home>", text, flags=re.IGNORECASE)
    text = _USER_PATH.sub("<redacted:path>", text)
    try:
        users = {getpass.getuser(), Path.home().name}
    except OSError:
        users = {Path.home().name}
    for user in users - {""}:
        text = re.sub(r"(?<![\w-])" + re.escape(user) + r"(?![\w-])", "<redacted:user>", text, flags=re.IGNORECASE)
    return text


def _redact_identity_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {name: _redact_identity_value(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_identity_value(item) for item in value]
    return _redact_identity(value) if isinstance(value, str) else value


def _redact_log_value(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(key) or _CHAT_CONTENT_KEY.fullmatch(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(name): _redact_log_value(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_log_value(item, key) for item in value]
    value = redact_for_shared(value)
    return _redact_identity(value) if isinstance(value, str) else value


def _redact_private_key_blocks(text: str) -> str:
    """Remove PEM blocks and any tail prefix ending in a truncated PEM block."""
    output: list[str] = []
    inside = False
    safe_length = 0
    for line in text.splitlines(keepends=True):
        if _PRIVATE_KEY_BEGIN.match(line):
            if not inside:
                output.append("<redacted private key>\n")
            inside = True
        elif _PRIVATE_KEY_END.match(line):
            if inside:
                inside = False
                safe_length = len(output)
            else:
                # The tail started after a key's BEGIN; discard that fragment.
                output = output[:safe_length]
                output.append("<redacted truncated private key>\n")
                safe_length = len(output)
        elif not inside:
            output.append(line)
    return "".join(output)


def redact_log(text: str) -> str:
    """Redact structured and text credentials before logs enter a support archive."""
    text = _redact_private_key_blocks(text)
    text = _URL_CREDENTIALS.sub(r"\1<redacted>@", text)
    text = _BEARER.sub("Bearer <redacted>", text)
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None
    if value is not None:
        return json.dumps(_redact_log_value(value), sort_keys=True) + "\n"
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            value = None
        if value is not None:
            lines.append(json.dumps(_redact_log_value(value), sort_keys=True) + ("\n" if line.endswith("\n") else ""))
        else:
            lines.append(_SECRET_TEXT.sub(r"\1<redacted>", line))
    return _redact_identity("".join(lines))


def _installed_record() -> tuple[Path | None, Path | None]:
    """Find the managed install through its selection record, including custom roots."""
    try:
        from astrid.installer.core import current, read_json
        from astrid.installer.service import LABEL, install_selection

        record = read_json(install_selection(Path.home(), sys.platform))
        if record.get("owner") != LABEL:
            return None, None
        program_root = Path(record["program_root"])
        installed = current(program_root)
        return (program_root, installed.root / "manifest.json") if installed else (program_root, None)
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return None, None


def _selected_tools(program_root: Path | None) -> dict:
    """Read only the recorded media executables, versions, and selection origin."""
    if program_root is None:
        return {}
    try:
        from astrid.installer.core import current

        installed = current(program_root)
        if installed is None:
            return {}
        env = installed.env
        source = env.get("ASTRID_INSTALL_FFMPEG_SOURCE")
        if source not in {"path", "download"}:
            return {}
        result = {}
        for name in ("ffmpeg", "ffprobe"):
            executable = env.get(f"ASTRID_{name.upper()}_EXECUTABLE")
            version = env.get(f"ASTRID_INSTALL_{name.upper()}_VERSION")
            if isinstance(executable, str) and isinstance(version, str):
                result[name] = {"executable": executable, "version": version, "source": source}
        return result
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return {}


def _install_manifest(support_root: Path) -> dict:
    """Project source pins and the small recorded tool selection from metadata."""
    candidates = [support_root / "install-manifest.json", support_root / "runtime" / "install-manifest.json"]
    _program_root, installed_manifest = _installed_record()
    tools = _selected_tools(_program_root)
    selected = {"selected_tools": tools} if tools else {}
    if installed_manifest is not None:
        candidates.append(installed_manifest)
    candidates.extend((support_root / "install.json", support_root / "runtime" / "install.json"))
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("schema_version") == 1 and isinstance(value.get("agent"), dict):
            return {
                **selected,
                "schema_version": 1,
                "agent": {"sha": value["agent"].get("sha")} if isinstance(value["agent"].get("sha"), str) else {},
                "runtime": {"sha": value.get("runtime", {}).get("sha")} if isinstance(value.get("runtime"), dict) and isinstance(value["runtime"].get("sha"), str) else {},
                "tools": {name: {"version": row["version"]} for name, row in value.get("tools", {}).items()
                          if name in {"uv", "python", "bun", "omp", "node", "ffmpeg"} and isinstance(row, dict) and isinstance(row.get("version"), str)},
            }
        result = {key: value[key] for key in ("version", "version_id", "runtime_version", "agent_version")
                  if isinstance(value.get(key), (str, int, float))}
        result.update(selected)
        versions = value.get("versions")
        if isinstance(versions, dict):
            result["versions"] = {
                str(name): {key: row[key] for key in ("version", "sha") if isinstance(row, dict) and isinstance(row.get(key), (str, int, float))}
                for name, row in versions.items() if isinstance(row, dict)
            }
        return result
    return {"available": False, **selected}


def _logs(support_root: Path | None = None) -> list[Path]:
    from runtime_protocol.platform import log_dir

    roots = [log_dir()]
    if support_root is not None:
        roots.extend((support_root, support_root / "runtime", support_root / "runtime" / "logs"))
    files: list[Path] = []
    for root in roots:
        try:
            for path in sorted(root.iterdir()):
                # Service output can contain conversation text. Include only
                # installer diagnostics and Runtime-level logs, never service logs.
                managed_log = (
                    re.fullmatch(r"install\.log(?:\.\d+)?", path.name) is not None
                    or path.name in {"runtime.log", "runtime-service.log"}
                )
                if managed_log and not path.is_symlink() and stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                    files.append(path)
        except OSError:
            continue
    return list(dict.fromkeys(files))


def _read_tail(path: Path) -> bytes:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        offset = max(0, size - _MAX_LOG_BYTES)
        stream.seek(offset, os.SEEK_SET)
        value = stream.read(_MAX_LOG_BYTES)
    if offset:
        boundary = value.find(b"\n")
        return value[boundary + 1:] if boundary >= 0 else b""
    return value


def _installed_runtime_version() -> str | None:
    program_root, _manifest = _installed_record()
    if program_root is None:
        return None
    try:
        from importlib.metadata import distributions

        from astrid.installer.core import current

        installed = current(program_root)
        if installed is None:
            return None
        site_paths = list((installed.python.parent.parent / "lib").glob("python*/site-packages"))
        site_paths += [installed.python.parent.parent / "Lib" / "site-packages"]
        runtime_distribution = next((item for item in distributions(path=[str(path) for path in site_paths])
                                     if "Name" in item.metadata and item.metadata["Name"].lower() == "astrid-runtime"), None)
        return runtime_distribution.version if runtime_distribution else None
    except (ImportError, OSError, ValueError, KeyError):
        return None


def write_bundle(destination: Path, report: dict, support_root: Path | None) -> Path:
    """Write report, versions, safe install metadata, and bounded redacted logs."""
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = _redact_log_value(redact_for_shared(report))
    versions: dict[str, str | None] = {"astrid": None, "astrid_runtime": None, "astrid_runtime_client": None}
    try:
        from importlib.metadata import PackageNotFoundError, version
        for name, package in (("astrid", "astrid"), ("astrid_runtime", "astrid-runtime"),
                              ("astrid_runtime_client", "astrid-runtime-client")):
            try:
                versions[name] = version(package)
            except PackageNotFoundError:
                pass
    except ImportError:
        pass
    versions["astrid_runtime"] = _installed_runtime_version() or versions["astrid_runtime"]
    from runtime_protocol import platform

    used = 0
    private_dir = destination.parent / f".{destination.name}.tmp-{uuid.uuid4().hex}"
    platform.private_directory(private_dir)
    temporary: Path | None = None
    try:
        descriptor, temporary_name = platform.private_temporary(private_dir, "bundle-")
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "w+b") as stream:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("status.json", json.dumps(payload, indent=2, sort_keys=True) + "\n")
                archive.writestr("versions.json", json.dumps(versions, indent=2, sort_keys=True) + "\n")
                manifest = _install_manifest(support_root) if support_root else {"available": False}
                archive.writestr("install-manifest.json", json.dumps(_redact_identity_value(manifest), indent=2, sort_keys=True) + "\n")
                included: set[str] = set()
                logs = _logs(support_root)
                logs.sort(key=lambda path: (path.name not in {"runtime.log", "runtime-service.log"}, path.name))
                for index, path in enumerate(logs):
                    try:
                        if used >= _MAX_TOTAL_LOG_BYTES:
                            continue
                        raw = _read_tail(path)
                        raw = raw[-(_MAX_TOTAL_LOG_BYTES - used):]
                        text = raw.decode("utf-8", errors="replace")
                    except OSError:
                        continue
                    relative = f"logs/{index}-{path.name}"
                    if relative in included:
                        continue
                    archive.writestr(relative, redact_log(text))
                    included.add(relative)
                    used += len(raw)
        platform.durable_replace(temporary, destination)
        platform.owner_only(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        try:
            private_dir.rmdir()
        except OSError:
            pass
    return destination
