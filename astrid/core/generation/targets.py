"""Machine-local Comfy target records and the shared VibeComfy config writer.

``update_config`` is the extension seam for library and future preference
writers. All participants must use ``config_lock_path`` (including standalone
VibeComfy), reread under that lock, and replace the complete document. Readers
never lock, migrate, discover processes, or write. Comments are not retained.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import stat
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from filelock import FileLock

_ALIASES = {"local": "comfyui", "vibecomfy": "comfyui", "vibecomfy-h3": "comfyui", "cloud": "fal"}
_COMFY_TABLES = ("comfyui", "local", "vibecomfy", "vibecomfy-h3")
_LAYOUTS = {"desktop", "git", "portable", "pinokio", "package", "unknown"}
_DIRECTORIES = ("user_dir", "workflows_dir", "output_dir", "models_dir", "custom_nodes_dir")
_LAUNCH = ("launch", "launch_argv", "launch_app", "argv", "app")


class TargetConfigError(ValueError):
    """Invalid or unsupported target state; existing bytes must be retained."""


@dataclass(frozen=True)
class TargetRecord:
    engine: str = "comfyui"
    location: str = "local"
    owner: str = "human"
    layout: str = "unknown"
    endpoint: str | None = None
    root: str | None = None
    python: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    verified_at: str | None = None
    prefer: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TargetSelection:
    record: TargetRecord | None
    source: str
    bound: bool


def canonical_engine(engine: str) -> str:
    if not isinstance(engine, str) or not engine:
        raise TargetConfigError("engine must be a nonempty engine id")
    return _ALIASES.get(engine, engine)


def config_path() -> Path:
    return Path.home() / ".vibecomfy" / "config.toml"


def config_lock_path(path: str | Path | None = None) -> Path:
    """The exact sibling lock shared with standalone VibeComfy writers."""
    target = Path(path) if path is not None else config_path()
    return target.with_name(target.name + ".lock")


def _absolute(value: Any, *, executable: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise TargetConfigError("target paths must be nonempty absolute paths")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise TargetConfigError("target paths must be absolute")
    try:
        # A venv's python is often a symlink to the system executable. Keep
        # that final component: dereferencing it changes the Python environment.
        if executable:
            return str(path.parent.resolve() / path.name)
        return str(path.resolve())
    except (OSError, RuntimeError) as exc:
        raise TargetConfigError("target path cannot be resolved") from exc


def _facts(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TargetConfigError("target facts must be a JSON object")
    def check(item):
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if isinstance(item, list):
            for child in item:
                check(child)
            return
        if isinstance(item, dict) and all(isinstance(k, str) for k in item):
            for child in item.values():
                check(child)
            return
        raise TargetConfigError("target facts must contain only finite JSON values")
    check(value)
    return copy.deepcopy(value)


def normalize_target(value: TargetRecord | Mapping[str, Any]) -> TargetRecord:
    """Normalize current records and legacy flat Comfy tables without I/O."""
    raw = value.as_dict() if isinstance(value, TargetRecord) else dict(value)
    engine = canonical_engine(raw.get("engine", "comfyui"))
    if engine != "comfyui" or raw.get("location", "local") != "local":
        raise TargetConfigError("only local comfyui targets are supported")
    kind = raw.get("kind", "unknown")
    if not isinstance(kind, str) or kind not in _LAYOUTS | {"attached", "managed"}:
        raise TargetConfigError("unsupported Comfy target kind")
    prefer = raw.get("prefer")
    if prefer not in (None, "attached", "managed"):
        raise TargetConfigError("unsupported Comfy target preference")
    owner = raw.get("owner", "astrid" if kind == "managed" else "human")
    if not isinstance(owner, str) or owner not in {"human", "astrid"}:
        raise TargetConfigError("target owner must be human or astrid")
    if prefer == "managed":
        owner = "astrid"
    if owner == "astrid" and prefer == "attached":
        raise TargetConfigError("attached preference conflicts with managed target ownership")
    layout = raw.get("layout", kind if kind in _LAYOUTS else "unknown")
    if not isinstance(layout, str) or layout not in _LAYOUTS:
        raise TargetConfigError("unsupported Comfy install layout")
    endpoint = raw.get("endpoint", raw.get("url"))
    if endpoint is not None:
        from .backends.comfy_target import validate_attached_url
        try:
            endpoint = validate_attached_url(endpoint)
        except (TypeError, ValueError) as exc:
            raise TargetConfigError(str(exc)) from exc
    facts = _facts(raw.get("facts", {}))
    if facts.get("versions") is not None and not isinstance(facts["versions"], dict):
        raise TargetConfigError("target facts.versions must be an object")
    version = raw.get("comfyui_version")
    if version is not None:
        if facts.get("versions") is None:
            facts["versions"] = {}
        versions = facts["versions"]
        versions.setdefault("comfyui", version)
    if owner == "human":
        for key in (*_DIRECTORIES, *_LAUNCH):
            if key in raw:
                facts.setdefault(key, raw[key])
        root = _absolute(raw.get("root", facts.get("root")))
        python = _absolute(raw.get("python", facts.get("python")), executable=True)
    else:
        # TOML ownership is selection, not proof. Ignore legacy managed paths;
        # the reader obtains execution paths from existing install metadata.
        root = python = None
        facts = {}
    verified_at = raw.get("verified_at")
    if verified_at is not None:
        try:
            if (not isinstance(verified_at, str) or re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)",
                    verified_at) is None):
                raise ValueError
            stamp = datetime.fromisoformat(verified_at)
            if stamp.utcoffset() != timedelta(0):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise TargetConfigError("verified_at must be a UTC RFC3339 timestamp") from exc
    return TargetRecord(engine, "local", owner, layout, endpoint, root, python,
                        _facts(facts), verified_at, prefer)


def _validate_document(document: dict[str, Any]) -> None:
    engines = document.get("engines")
    if engines is not None:
        if (not isinstance(engines, dict) or type(engines.get("schema_version")) is not int
                or engines["schema_version"] != 1):
            raise TargetConfigError("unsupported engines schema_version (expected 1)")
        if "primary" in engines:
            canonical_engine(engines["primary"])
    if "library" in document and not isinstance(document["library"], dict):
        raise TargetConfigError("library must be a table")
    selected = [name for name in _COMFY_TABLES if name in document]
    if len(selected) > 1:
        raise TargetConfigError("ambiguous Comfy target tables; explicitly recover the config")
    for name in selected:
        if not isinstance(document[name], dict):
            raise TargetConfigError("Comfy target must be a table")
        normalize_target(document[name])


def read_config(path: str | Path | None = None) -> dict[str, Any]:
    """Read a complete config; missing is empty, corruption is never empty."""
    target = Path(path) if path is not None else config_path()
    try:
        document = tomllib.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError) as exc:
        raise TargetConfigError(f"cannot read target config {target}; repair it before saving") from exc
    _validate_document(document)
    return document


def _toml_value(value: Any) -> str:
    # Inline tables preserve arbitrary nested data, including arrays of tables,
    # without adding a serializer dependency or converting values via repr().
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is float:
        return str(value).lower()
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return "{ " + ", ".join(f"{_toml_value(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TargetConfigError("config contains a value unsupported by TOML")


def _format_toml(document: dict[str, Any]) -> str:
    lines = []
    for key, value in document.items():
        if not isinstance(key, str):
            raise TargetConfigError("config keys must be strings")
        if not isinstance(value, dict):
            lines.append(f"{_toml_value(key)} = {_toml_value(value)}")
    for key, value in document.items():
        if isinstance(value, dict):
            if not all(isinstance(k, str) for k in value):
                raise TargetConfigError("config keys must be strings")
            lines.append(f"\n[{_toml_value(key)}]")
            lines.extend(f"{_toml_value(k)} = {_toml_value(v)}" for k, v in value.items())
    text = "\n".join(lines) + "\n"
    try:
        tomllib.loads(text)
    except (ValueError, UnicodeError) as exc:
        raise TargetConfigError("config cannot be represented as TOML") from exc
    return text


def update_config(update: Callable[[dict[str, Any]], None], *, path: str | Path | None = None) -> Path:
    """Apply a local document mutation under the one sibling config lock.

    Used by target saves, library writes and future machine preferences. The
    callback receives the latest document and must perform no network/provider
    calls. Invalid input or a failed callback leaves existing config untouched.

    Extensions can replace their own table with ``document[section] = values``
    or merge keys with ``document.setdefault(section, {}).update(values)``.
    Keep values TOML-compatible (omit unknown optional values rather than
    inserting None), and leave other sections intact. No selected target is
    required for an independent preference update.
    """
    from astrid.core.foundation.atomic_io import write_text_atomic

    target = Path(path) if path is not None else config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(config_lock_path(target))):
        document = read_config(target)
        update(document)
        _validate_document(document)
        write_text_atomic(target, _format_toml(document))
    return target


def _managed_record(record: TargetRecord) -> TargetRecord:
    """Resolve ownership and paths from existing root/install receipt authority."""
    from .engine_local_endpoint import recorded_engine_root
    from .engine_recipe import load_recipe
    from .engine_runner import _parse_receipt, plan_install

    try:
        roots = {root for name in _COMFY_TABLES
                 if (root := recorded_engine_root(name)) is not None}
        if len(roots) != 1:
            raise TargetConfigError("managed Comfy target needs one recorded engine root")
        root = roots.pop()
        receipt_path = Path(root) / ".astrid-engine" / "install.json"
        info = receipt_path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise TargetConfigError("managed Comfy metadata must be a regular install receipt")
        raw = json.loads(receipt_path.read_bytes())
        if not isinstance(raw, dict):
            raise TargetConfigError("managed Comfy metadata must be an install receipt object")
        if canonical_engine(raw.get("engine")) != "comfyui" or raw.get("status") != "installed":
            raise TargetConfigError("managed Comfy target needs an installed Comfy receipt")
        recipe = load_recipe(raw["engine"], raw["platform"])
        receipt = _parse_receipt(raw, plan_install(recipe, root=root, placement="local"))
        environment = next(e for e in recipe.environments if e.name == recipe.primary_environment)
        executable = "Scripts/python.exe" if recipe.platform.startswith("windows-") else "bin/python"
        python = str(Path(root) / environment.relative_path / executable)
        return replace(record, root=root, python=python,
                       facts={"install_receipt_id": receipt.receipt_id})
    except TargetConfigError:
        raise
    except (OSError, ValueError, KeyError, TypeError, IndexError, RuntimeError) as exc:
        raise TargetConfigError("managed Comfy metadata is unavailable or invalid; repair the installation") from exc


def read_target(*, path: str | Path | None = None, engine: str = "comfyui") -> TargetRecord | None:
    if canonical_engine(engine) != "comfyui":
        raise TargetConfigError("only local comfyui targets are supported")
    return _target_from_document(read_config(path))


def _target_from_document(document: dict[str, Any]) -> TargetRecord | None:
    for name in _COMFY_TABLES:
        if name in document:
            record = normalize_target(document[name])
            return _managed_record(record) if record.owner == "astrid" else record
    return None


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _without_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_without_nulls(v) for v in value]
    return value


def save_target(value: TargetRecord | Mapping[str, Any], *, path: str | Path | None = None,
                primary: bool = False) -> Path:
    """Explicitly replace Comfy's selection, lazily migrating its legacy table.

    Transient selections are never saved by the resolver. Managed paths/facts
    are derived on read and are never copied into this document.
    """
    record = normalize_target(value)
    if record.owner == "astrid":
        _managed_record(record)  # ownership proof, outside the config lock
    wire = {"location": "local", "owner": record.owner, "layout": record.layout}
    if record.owner == "human":
        wire.update(_without_nulls({"url": record.endpoint, "root": record.root,
                                   "python": record.python, "facts": record.facts,
                                   "verified_at": record.verified_at}))
        for key in (*_DIRECTORIES, *_LAUNCH):
            if record.facts.get(key) is not None:
                wire[key] = copy.deepcopy(record.facts[key])
        version = (record.facts.get("versions") or {}).get("comfyui")
        if version is not None:
            wire["comfyui_version"] = version
    if record.prefer is not None:
        wire["prefer"] = record.prefer

    def save(document):
        previous = next((document[n] for n in _COMFY_TABLES if n in document), {})
        if "prefer" not in wire and "prefer" in previous:
            # A new explicit selection supersedes a contradictory old policy.
            wire["prefer"] = "managed" if record.owner == "astrid" else "attached"
        for name in _COMFY_TABLES:
            document.pop(name, None)
        document["comfyui"] = wire
        engines = document.setdefault("engines", {"schema_version": 1})
        if "primary" in engines:
            engines["primary"] = canonical_engine(engines["primary"])
        if primary:
            engines["primary"] = "comfyui"
    return update_config(save, path=path)


def resolve_target(request: str = "auto", *, path: str | Path | None = None,
                   repo: str | Path | None = None, environ: Mapping[str, str] | None = None) -> TargetSelection:
    """Read-only request > env > supplied repo > global > discovery selection."""
    if request == "managed":
        return TargetSelection(None, "request:managed", True)
    if request != "auto":
        return TargetSelection(normalize_target({"endpoint": request}), "request", True)
    environment = os.environ if environ is None else environ
    override = environment.get("VIBECOMFY_COMFYUI_URL")
    if override is not None:
        # A transient address is a new human attachment; no cached ownership,
        # root, Python or launch facts are transferred from another address.
        return TargetSelection(normalize_target({"endpoint": override}), "env", True)
    for source, candidate in (("repo", Path(repo) / "vibecomfy.toml" if repo is not None else None),
                              ("global", Path(path) if path is not None else config_path())):
        if candidate is None:
            continue
        document = read_config(candidate)
        primary = document.get("engines", {}).get("primary", "comfyui")
        if canonical_engine(primary) != "comfyui":
            raise TargetConfigError("selected primary engine is unsupported; explicitly select comfyui")
        # Select primary and target from one atomic document snapshot. A
        # second read could race a writer and bind a different selection.
        record = _target_from_document(document)
        if record is not None:
            return TargetSelection(record, source, True)
    return TargetSelection(TargetRecord(endpoint="http://127.0.0.1:8188"), "discovery", False)
