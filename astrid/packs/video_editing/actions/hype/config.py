"""Pipeline order shared by the v3 Hype command-builder callbacks."""

from __future__ import annotations

import json
from pathlib import Path

from astrid.pack_api import AstridError
from astrid.pack_api import has_url_scheme

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None

# Topological order of pipeline steps.  Used by step selection, dry-run
# planning, --from / --skip validation, and extra_args key validation.
STEP_ORDER = (
    "transcribe",
    "scenes",
    "quality_zones",
    "shots",
    "triage",
    "scene_describe",
    "quote_scout",
    "pool_build",
    "pool_merge",
    "arrange",
    "cut",
    "refine",
    "render",
    "editor_review",
    "validate",
)


def usage_error(message: str) -> None:
    raise AstridError(message, recovery_command="astrid hype --help")


def load_config(path_text: str) -> dict:
    path = Path(path_text).expanduser().resolve()
    if not path.is_file():
        usage_error(f"astrid: config file not found: {path}")
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            usage_error(f"astrid: invalid JSON config {path}: {exc.msg}")
    elif suffix in {".yaml", ".yml"}:
        if yaml is None:
            usage_error(f"astrid: YAML config requires PyYAML: {path}")
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            usage_error(f"astrid: invalid YAML config {path}: {exc}")
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            if yaml is None:
                usage_error(f"astrid: unsupported config format for {path}; use JSON or install PyYAML for YAML")
            try:
                data = yaml.safe_load(text)
            except Exception as exc:
                usage_error(f"astrid: invalid config {path}: {exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        usage_error(f"astrid: config must decode to an object: {path}")
    return data


def normalize_config(raw: dict) -> dict:
    data = dict(raw)
    if "from" in data and "from_step" not in data:
        data["from_step"] = data.pop("from")
    if "python" in data and "python_exec" not in data:
        data["python_exec"] = data.pop("python")
    if "assets" in data and "asset" not in data:
        data["asset"] = data.pop("assets")
    return data


def parse_asset_entry(raw: str) -> tuple[str, Path | str]:
    if "=" not in raw:
        usage_error(f"astrid: invalid --asset value {raw!r}; expected KEY=PATH")
    key, path_text = raw.split("=", 1)
    key = key.strip()
    path_text = path_text.strip()
    if not key or not path_text:
        usage_error(f"astrid: invalid --asset value {raw!r}; expected KEY=PATH")
    if key == "main":
        usage_error("astrid: asset key 'main' is reserved; pass the primary video via --video")
    if has_url_scheme(path_text):
        usage_error(
            f"astrid: URL asset {key!r} is not supported; inputs must be runtime-materialized files"
        )
    path = Path(path_text).expanduser().resolve()
    if not path.exists():
        usage_error(f"astrid: asset path not found for {key!r}: {path}")
    return key, path


def normalize_many(raw: object, *, key_name: str) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        usage_error(f"astrid: {key_name} must be a string or list of strings")
    return list(raw)


def normalize_extra_args(raw: object) -> dict[str, list[str]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        usage_error("astrid: extra_args must be an object keyed by step name")
    allowed_steps = set(STEP_ORDER)
    extra_args: dict[str, list[str]] = {}
    for step_name, values in raw.items():
        if step_name not in allowed_steps:
            usage_error(f"astrid: unknown extra_args step {step_name!r}")
        if not isinstance(values, list) or not all(isinstance(item, (str, int, float)) for item in values):
            usage_error(f"astrid: extra_args[{step_name!r}] must be a list of CLI tokens")
        extra_args[step_name] = [str(item) for item in values]
    return extra_args
