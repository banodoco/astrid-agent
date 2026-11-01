#!/usr/bin/env python3
"""Bounded OpenAI request expansion and the existing image provider boundary.

Admission callers supply authenticated prompt bytes; this module never fetches
input URLs or interprets caller callbacks.  The legacy action delegates here.
"""


from __future__ import annotations

from astrid.core.contracts.errors import AstridValueError
import argparse
import base64
import json
import re
import subprocess
import time
import uuid
import hashlib
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from astrid.core._shared.result_manifest import complete_output_metadata
from astrid.core.audit import AuditContext
from astrid.core.cli_choices import add_choice_arg
from astrid.core.contracts.errors import AstridError
from astrid.core.foundation.atomic_io import write_json_atomic
from astrid.core.util.credentials_scope import CredentialsScope
from astrid.core.generation.backends.base import BackendAdapter, GenerationResult
from astrid.core.generation.storage_policy import OPENAI_IMAGE_STORAGE_POLICY

API_URL = "https://api.openai.com/v1/images/generations"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_SIZE = "1024x1024"
DEFAULT_QUALITY = "medium"
DEFAULT_FORMAT = "png"

QUALITIES = {"low", "medium", "high", "auto"}
FORMATS = {"png", "jpeg", "jpg", "webp"}
BACKGROUNDS = {"opaque", "auto", "transparent"}
MODERATION = {"auto", "low"}

PRESETS: dict[str, dict[str, Any]] = {
    "saint-peter-of-banodoco": {
        "prompt": (
            "An illuminated medieval manuscript page depicting Saint Peter of Banodoco, "
            "patron of file-based pipelines, haloed in glowing unix prompts, quill in "
            "hand inscribing ffmpeg incantations. Tiny familiar spirits label every step, "
            "LOTA, and MOIRAE peer over his shoulders. Gold leaf, vellum, Celtic "
            "knotwork border."
        ),
        "open_result": True,
    },
}

GPT_IMAGE_2_MIN_PIXELS = 655_360
GPT_IMAGE_2_MAX_PIXELS = 8_294_400
GPT_IMAGE_2_MAX_EDGE = 3840
GPT_IMAGE_2_MAX_RATIO = 3.0


from astrid.core.contracts.die import pack_die as _die



# Secrets resolution delegated to astrid.core.util.secrets.


def _resolve_key(*, env_file: Path | None = None) -> str:
    return CredentialsScope.get_local("openai", env_file=env_file)


def _normalize_format(value: str) -> str:
    fmt = value.lower()
    if fmt not in FORMATS:
        _die("--output-format must be png, jpeg, jpg, or webp", valid_options=sorted(FORMATS), recovery_command="use --output-format with one of: png, jpeg, jpg, webp")
    return "jpeg" if fmt == "jpg" else fmt


def _parse_size(size: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", size)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _validate_size(size: str, model: str) -> None:
    if size == "auto":
        return
    if model != "gpt-image-2":
        if size not in {"1024x1024", "1536x1024", "1024x1536"}:
            _die("For models before gpt-image-2, size must be 1024x1024, 1536x1024, 1024x1536, or auto.", valid_options=["1024x1024", "1536x1024", "1024x1536", "auto"], recovery_command="use --size 1024x1024, 1536x1024, 1024x1536, or auto")
        return
    parsed = _parse_size(size)
    if parsed is None:
        _die("size must be auto or WIDTHxHEIGHT, for example 1024x1024", recovery_command="use --size auto or WIDTHxHEIGHT format like 1024x1024")
    width, height = parsed
    if width % 16 or height % 16:
        _die("gpt-image-2 size width and height must be multiples of 16px", recovery_command="use dimensions that are multiples of 16px")
    if max(width, height) > GPT_IMAGE_2_MAX_EDGE:
        _die("gpt-image-2 size maximum edge length is 3840px", recovery_command="use a size with max edge 3840px or less")
    if max(width, height) / min(width, height) > GPT_IMAGE_2_MAX_RATIO:
        _die("gpt-image-2 size long edge to short edge ratio must not exceed 3:1", recovery_command="use a size with aspect ratio 3:1 or less")
    pixels = width * height
    if pixels < GPT_IMAGE_2_MIN_PIXELS or pixels > GPT_IMAGE_2_MAX_PIXELS:
        _die("gpt-image-2 size total pixels must be between 655,360 and 8,294,400", recovery_command="use a size with total pixels between 655,360 and 8,294,400")


def _validate_payload(payload: dict[str, Any]) -> None:
    model = str(payload["model"])
    if not model.startswith("gpt-image-"):
        _die("--model must be a GPT Image model, for example gpt-image-2", recovery_command="use --model gpt-image-2 or another GPT Image model")
    n = int(payload["n"])
    if n < 1 or n > 10:
        _die("--n must be between 1 and 10", recovery_command="use --n between 1 and 10")
    _validate_size(str(payload["size"]), model)
    if payload["quality"] not in QUALITIES:
        _die("--quality must be low, medium, high, or auto", valid_options=sorted(QUALITIES), recovery_command="use --quality low, medium, high, or auto")
    if payload.get("background") and payload["background"] not in BACKGROUNDS:
        _die("--background must be opaque, auto, or transparent", valid_options=sorted(BACKGROUNDS), recovery_command="use --background opaque, auto, or transparent")
    if model == "gpt-image-2" and payload.get("background") == "transparent":
        _die("gpt-image-2 does not support transparent backgrounds; use opaque/auto or explicitly choose an older supported model", recovery_command="use --background opaque or auto, or switch to an older model that supports transparent")
    if payload.get("moderation") and payload["moderation"] not in MODERATION:
        _die("--moderation must be auto or low", valid_options=sorted(MODERATION), recovery_command="use --moderation auto or low")
    compression = payload.get("output_compression")
    if compression is not None and not (0 <= int(compression) <= 100):
        _die("--output-compression must be between 0 and 100", recovery_command="use --output-compression between 0 and 100")



MAX_JOBS = 32
MAX_OUTPUTS = 32
MAX_REFS = 16
MAX_PROMPT_BYTES = 1024 * 1024
MAX_REF_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
REF_PORTS = tuple(f"image_ref_{index:02d}" for index in range(MAX_REFS))
_JOB_FIELDS = {"prompt", "model", "n", "size", "quality", "output_format", "output_compression", "background", "moderation"}
_DEFAULTS = {"model": DEFAULT_MODEL, "n": 1, "size": DEFAULT_SIZE,
             "quality": DEFAULT_QUALITY, "output_format": DEFAULT_FORMAT,
             "background": "auto"}


def _integer(value: Any, label: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{label} must be an integer between 1 and {maximum}")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON prompt job field")
        result[key] = value
    return result


def _prompt_rows(data: bytes) -> list[Any]:
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_PROMPT_BYTES:
        raise ValueError("prompt bytes must be nonempty and at most 1 MiB")
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("prompt file must be UTF-8") from exc
    if not text:
        raise ValueError("prompt file is empty")
    try:
        if text.startswith("["):
            rows = json.loads(text, object_pairs_hook=_unique_object)
        else:
            rows = []
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # JSONL containers/scalars must parse, never become a literal
                # prompt on malformed JSON. Plain text remains a legacy input.
                rows.append(json.loads(line, object_pairs_hook=_unique_object) if line[0] in '{["' else line)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON/JSONL prompt bytes") from exc
    if not isinstance(rows, list):
        raise ValueError("prompt JSON must contain an array")
    return rows


def expand_jobs(inputs: Mapping[str, Any], *, prompt_bytes: bytes | None = None) -> list[dict[str, Any]]:
    """Expand authenticated bytes into a deterministic, bounded provider work set."""
    has_file = inputs.get("prompts_file") not in (None, "")
    prompt = inputs.get("prompt")
    if has_file:
        if prompt not in (None, "", []):
            raise ValueError("prompt and prompts_file are mutually exclusive")
        if prompt_bytes is None:
            raise ValueError("prompt file bytes must be authenticated before expansion")
        rows = _prompt_rows(prompt_bytes)
    else:
        if prompt_bytes is not None:
            raise ValueError("authenticated prompt bytes require prompts_file")
        rows = [{"prompt": p} for p in prompt] if isinstance(prompt, list) else [{"prompt": prompt}]
    if not 1 <= len(rows) <= MAX_JOBS:
        raise ValueError("prompt list must contain 1 to 32 jobs")
    defaults = {key: inputs.get(key) if inputs.get(key) is not None else value
                for key, value in _DEFAULTS.items()}
    defaults["n"] = inputs.get("n", inputs.get("count", 1))
    if defaults["n"] is None:
        defaults["n"] = 1
    for key in ("output_compression", "moderation"):
        if inputs.get(key) is not None:
            defaults[key] = inputs[key]
    _integer(defaults["n"], "n", 10)
    selected_model = defaults["model"]
    if not isinstance(selected_model, str) or not selected_model.startswith("gpt-image-"):
        raise ValueError("OpenAI images require an explicit GPT Image model")
    result = []
    for item in rows:
        if isinstance(item, str):
            item = {"prompt": item}
        if not isinstance(item, dict) or set(item) - _JOB_FIELDS:
            raise ValueError("prompt job contains unsupported fields")
        job = dict(defaults, **item)
        prompt = job.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_000:
            raise ValueError("each prompt must be nonempty and at most 32000 characters")
        job["prompt"] = prompt.strip()
        if job["model"] != selected_model:
            raise ValueError("job model must match the selected GPT Image model")
        _integer(job["n"], "n", 10)
        if not isinstance(job["output_format"], str):
            raise ValueError("unsupported output format")
        if (not isinstance(job.get("quality"), str) or job["quality"] not in QUALITIES
                or not isinstance(job.get("background"), str) or job["background"] not in BACKGROUNDS
                or not isinstance(job.get("size"), str)
                or (job.get("moderation") is not None and job["moderation"] not in ("auto", "low"))):
            raise ValueError("unsupported OpenAI image options")
        compression = job.get("output_compression")
        if compression is not None and (type(compression) is not int or not 0 <= compression <= 100):
            raise ValueError("output_compression must be an integer from 0 to 100")
        try:
            job["output_format"] = _normalize_format(job["output_format"])
            _validate_payload(job)
        except AstridError as exc:
            raise ValueError(str(exc)) from exc
        result.append(job)
    if sum(job["n"] for job in result) > MAX_OUTPUTS:
        raise ValueError("at most 32 images are allowed per task")
    # The result receipt retains each prompt. Reserve the other half of the
    # fixed manifest envelope for bounded provider metadata and output entries;
    # use the writer's ASCII escaping, including non-ASCII prompt expansion.
    if len(json.dumps(result, indent=2, sort_keys=True).encode("utf-8")) > 512 * 1024:
        raise ValueError("expanded OpenAI jobs exceed the result manifest envelope")
    return result


def normalize_request(inputs: Mapping[str, Any], *, prompt_bytes: bytes | None = None,
                      require_frozen: bool = False) -> dict[str, Any]:
    """Normalize once before admission, or prove frozen jobs against retained bytes.

    ``jobs`` is host-derived transport, never a public alternative to prompt
    authentication. Frozen verification always re-expands the original input.
    """
    normalized = dict(inputs)
    supplied_jobs = normalized.get("jobs")
    if supplied_jobs is not None and not require_frozen:
        raise ValueError("jobs is reserved for authenticated admission")
    if normalized.get("execution", "openai") != "openai":
        raise ValueError("OpenAI image execution must be openai")
    # Public convenience references become declared scalar ports before binding.
    public_refs = normalized.pop("image_ref", None)
    if public_refs is not None:
        refs = public_refs if isinstance(public_refs, (list, tuple)) else [public_refs]
        if not 1 <= len(refs) <= MAX_REFS or any(normalized.get(k) is not None for k in REF_PORTS):
            raise ValueError("reference images must use one ordered representation with at most 16 items")
        normalized.update(zip(REF_PORTS, refs))
    for port in ("prompts_file", *REF_PORTS):
        if normalized.get(port) in (None, ""):
            normalized.pop(port, None)
    timeout = normalized.get("timeout")
    if timeout is not None and (type(timeout) is not int or timeout < 1):
        raise ValueError("OpenAI timeout must be a positive integer")
    normalized["timeout"] = 180 if timeout is None else timeout
    refs = []
    seen_digest = set()
    missing = False
    for port in REF_PORTS:
        value = normalized.get(port)
        if value in (None, ""):
            missing = True
            continue
        if missing:
            raise ValueError("OpenAI reference positions must be consecutive from image_ref_00")
        if not isinstance(value, (str, Path, Mapping)):
            raise ValueError("reference images require authenticated file descriptors")
        if isinstance(value, Mapping):
            if "size_bytes" in value or "bytes" in value:
                OPENAI_IMAGE_STORAGE_POLICY.validate_reference_descriptor(value, index=len(refs))
            digest = value.get("digest", value.get("sha256", value.get("content_hash")))
            if isinstance(digest, str):
                digest = digest.removeprefix("sha256:")
            if digest is not None:
                if digest in seen_digest:
                    raise ValueError("duplicate OpenAI reference digests are not supported")
                seen_digest.add(digest)
        refs.append(value)
    if any(key.startswith("image_ref_") and key not in REF_PORTS for key in normalized):
        raise ValueError("at most 16 OpenAI references are allowed")
    mode = normalized.get("mode") or ("edit" if refs else "t2i")
    if mode not in {"t2i", "edit"} or (mode == "edit") != bool(refs):
        raise ValueError("OpenAI edit requires references; t2i refuses references")
    if any(normalized.get(key) is not None for key in ("mask_ref", "style_ref", "brand_ref", "negative_prompt", "seed")):
        raise ValueError("unsupported OpenAI image input")
    if not require_frozen and normalized.get("n") is not None and normalized.get("count") is not None and normalized["n"] != normalized["count"]:
        raise ValueError("n and count must agree when supplied together")
    normalized["n"] = normalized.get("n") if normalized.get("n") is not None else normalized.get("count")
    if normalized["n"] is None:
        normalized["n"] = 1
    normalized["model"] = normalized.get("model") or DEFAULT_MODEL
    normalized["mode"], normalized["execution"] = mode, "openai"
    jobs = expand_jobs(normalized, prompt_bytes=prompt_bytes)
    frozen = json.dumps(jobs, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    count = sum(job["n"] for job in jobs)
    if require_frozen and (not isinstance(supplied_jobs, str) or supplied_jobs != frozen
                           or type(normalized.get("count")) is not int or normalized["count"] != count):
        raise ValueError("frozen OpenAI jobs/cardinality do not match authenticated inputs")
    normalized["jobs"], normalized["count"] = frozen, count
    for key, value in _DEFAULTS.items():
        if key != "n" and normalized.get(key) is None:
            normalized[key] = value
    return normalized


def _reference_paths(inputs: Mapping[str, Any]) -> list[tuple[str, bytes]]:
    refs = []
    seen = set()
    for key in REF_PORTS:
        value = inputs.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, (str, Path)):
            raise ValueError("OpenAI reference is not a materialized authenticated file")
        path = Path(value)
        if path.stat().st_size > MAX_REF_BYTES:
            raise ValueError("OpenAI reference exceeds byte bounds")
        with path.open("rb") as stream:
            data = stream.read(MAX_REF_BYTES + 1)
        mime = OPENAI_IMAGE_STORAGE_POLICY.validate_reference_bytes(data, index=len(refs))
        digest = hashlib.sha256(data).hexdigest()
        if digest in seen:
            raise ValueError("duplicate OpenAI reference digests are not supported")
        seen.add(digest)
        refs.append((mime, data))
    return refs


def call_edit_api(payload: dict[str, Any], refs: list[Path] | list[tuple[str, bytes]], api_key: str, timeout: int) -> dict[str, Any]:
    """Encode ordered, materialized references for the fixed OpenAI edit endpoint."""
    boundary = "astrid-" + uuid.uuid4().hex
    parts = []
    for key, value in payload.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
    for index, ref in enumerate(refs):
        if isinstance(ref, tuple):
            mime, data = ref
        else:
            with ref.open("rb") as stream:
                data = stream.read(MAX_REF_BYTES + 1)
            mime = OPENAI_IMAGE_STORAGE_POLICY.validate_reference_bytes(data, index=index)
        suffix = {"image/png": ".png", "image/jpeg": ".jpeg", "image/webp": ".webp"}[mime]
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="ref-{index}{suffix}"\r\nContent-Type: {mime}\r\n\r\n').encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    request = Request("https://api.openai.com/v1/images/edits", data=b"".join(parts),
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
    return _request_json(request, timeout, expected_count=payload["n"])


def _request_json(request: Request, timeout: int, *, expected_count: int) -> dict[str, Any]:
    # Base64 expands the bounded outputs by 4/3, plus bounded response metadata.
    maximum = expected_count * ((MAX_OUTPUT_BYTES + 2) // 3 * 4) + 1024 * 1024
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("OpenAI response exceeds bounded byte limit")
        result = json.loads(data.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError("OpenAI response must be an object")
        return result
    except HTTPError as exc:
        raise AstridError(f"OpenAI API error {exc.code}") from exc
    except URLError as exc:
        raise AstridError("OpenAI network request failed") from exc

def _slugify(text: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return value[:64] or "image"


def _output_paths(out_dir: Path, prompt: str, fmt: str, job_index: int, n: int, explicit_out: str | None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = "." + fmt
    if explicit_out:
        base = Path(explicit_out)
        if not base.suffix:
            base = base.with_suffix(ext)
        base = out_dir / base.name
    else:
        base = out_dir / f"{job_index:03d}-{_slugify(prompt)}{ext}"
    if n == 1:
        return [base]
    return [base.with_name(f"{base.stem}-{i}{base.suffix}") for i in range(1, n + 1)]


def _call_image_api(payload: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    request = Request(API_URL, data=json.dumps(payload).encode("utf-8"),
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    return _request_json(request, timeout, expected_count=payload["n"])


def _validate_response_metadata(response: dict[str, Any]) -> None:
    usage = response.get("usage")
    if usage is not None and (not isinstance(usage, dict)
            or len(json.dumps(usage, sort_keys=True).encode("utf-8")) > 4096):
        raise ValueError("OpenAI usage metadata exceeds the manifest envelope")
    created = response.get("created")
    if created is not None and (type(created) is not int or not 0 <= created < 2 ** 63):
        raise ValueError("OpenAI created metadata must be a bounded timestamp")


def _write_images(response: dict[str, Any], paths: list[Path], force: bool) -> list[str]:
    items = response.get("data")
    if not isinstance(items, list) or len(items) != len(paths):
        raise ValueError("OpenAI response cardinality does not match admitted outputs")
    decoded = []
    for item, path in zip(items, paths):
        if not isinstance(item, dict) or not isinstance(item.get("b64_json"), str):
            raise ValueError("OpenAI output requires base64 image bytes")
        encoded = item["b64_json"]
        if len(encoded) > (MAX_OUTPUT_BYTES + 2) // 3 * 4:
            raise ValueError("OpenAI output exceeds byte bounds")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ValueError("OpenAI output contains malformed base64") from exc
        if not 0 < len(data) <= MAX_OUTPUT_BYTES:
            raise ValueError("OpenAI output exceeds byte bounds")
        if path.exists() and not force:
            _die(f"Output exists: {path} (use --force to overwrite)")
        decoded.append(data)
    written = []
    for data, path in zip(decoded, paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        print(f"Wrote {path}")
        written.append(str(path))
    return written


def _jobs_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    inputs = vars(args).copy()
    if not inputs.get("prompt") and inputs.get("preset"):
        preset = PRESETS.get(inputs["preset"])
        if preset is None:
            raise ValueError("unknown image preset")
        inputs["prompt"] = preset["prompt"]
    data = None
    if inputs.get("prompts_file"):
        path = Path(inputs["prompts_file"])
        if path.stat().st_size > MAX_PROMPT_BYTES:
            raise ValueError("prompt bytes exceed 1 MiB")
        with path.open("rb") as stream:
            data = stream.read(MAX_PROMPT_BYTES + 1)
    normalized = normalize_request(inputs, prompt_bytes=data,
                                   require_frozen=inputs.get("jobs") is not None)
    args.mode = normalized["mode"]
    args._normalized = normalized
    return json.loads(normalized["jobs"])


def _open_first_rendered(out_dir: Path) -> None:
    rendered = sorted(out_dir.glob("*.png")) + sorted(out_dir.glob("*.jpeg")) + sorted(out_dir.glob("*.webp"))
    if not rendered:
        print("No image was rendered; nothing to open.")
        return
    target = rendered[0]
    print(f"Opening {target}")
    subprocess.run(["open", str(target)], check=False)


def _build_openai_manifest(
    *,
    args: argparse.Namespace,
    jobs: list[dict[str, Any]],
    manifest_path: Path,
) -> dict[str, Any]:
    """Build a v2 universal manifest object for generate_image_openai.

    Converts from the legacy bare-list format to a generation v2 universal
    manifest with ``schema_version``, ``kind``, ``inputs``, ``outputs``,
    ``created``, ``warnings``, and ``jobs`` fields.  Output paths are
    resolved relative to the manifest directory and enriched through the
    shared result-manifest contract.  The executor writes images below
    ``out_dir`` but the GenericHost harvests from the manifest's run root.
    """
    all_output_paths: list[str] = []
    for job in jobs:
        all_output_paths.extend(job.get("outputs") or [])

    out_dir = args.out_dir.expanduser().resolve()
    manifest_root = manifest_path.expanduser().resolve().parent
    outputs: list[dict[str, Any]] = []
    for ordinal, path_str in enumerate(all_output_paths):
        p = Path(path_str).expanduser().resolve()
        try:
            rel = p.relative_to(manifest_root).as_posix()
        except ValueError:
            raise AstridValueError(
                f"OpenAI output is outside its manifest root: {p}"
            ) from None
        outputs.append(
            {
                "name": "generated_images",
                "path": rel,
                "type": "file",
                "artifact_type": "image",
                "ordinal": ordinal,
                "role": "result",
                "is_primary": ordinal == 0,
            }
        )

    inputs: dict[str, Any] = {
        "model": args.model,
        "n": args.n,
        "size": args.size,
        "quality": args.quality,
        "output_format": args.output_format,
        "dry_run": args.dry_run,
    }
    for key in ("output_compression", "background", "moderation"):
        val = getattr(args, key, None)
        if val is not None:
            inputs[key] = val

    manifest: dict[str, Any] = {
        "schema_version": 2,
        "kind": "generation.generate_image_openai",
        "inputs": inputs,
        "outputs": outputs,
        "created": datetime.now(timezone.utc).isoformat(),
        "warnings": [],
        "jobs": jobs,
    }

    # Route output metadata through the shared contract, rooted at the
    # manifest directory used by GenericHost for harvesting.
    manifest["outputs"] = complete_output_metadata(
        manifest["outputs"], root_dir=manifest_root,
    )
    if len(json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")) + 1 > OPENAI_IMAGE_STORAGE_POLICY.manifest_max_bytes:
        raise ValueError("OpenAI result manifest exceeds byte bounds")
    return manifest


def execute_legacy(args: argparse.Namespace, *, transport=None, key_resolver=None, manifest_writer=None) -> int:
    if args.out_dir in (None, ""):
        raise AstridError(
            "generation.generate_image_openai requires an explicit --out-dir staging directory; "
            "invoke through the SDK/runtime for managed output",
            recovery_command="invoke generation.generate_image_openai with a runtime project, or pass --out-dir for explicit isolated tooling",
        )
    args.out_dir = Path(args.out_dir)
    if args.manifest in (None, ""):
        args.manifest = args.out_dir / "manifest.json"
    else:
        args.manifest = Path(args.manifest)
    jobs = _jobs_from_args(args)
    refs = _reference_paths(args._normalized)
    api_key = None if args.dry_run else (key_resolver or _resolve_key)(env_file=args.env_file)
    manifest_writer = manifest_writer or write_json_atomic
    out_dir = args.out_dir
    default_format = _normalize_format(args.output_format)
    manifest_jobs: list[dict[str, Any]] = []
    audit = AuditContext.from_env()

    for index, job in enumerate(jobs, start=1):
        prompt = str(job["prompt"]).strip()
        payload = {
            "model": job.get("model", args.model),
            "prompt": prompt,
            "n": int(job.get("n", args.n)),
            "size": job.get("size", args.size),
            "quality": job.get("quality", args.quality),
            "output_format": _normalize_format(str(job.get("output_format", default_format))),
            "output_compression": job.get("output_compression", args.output_compression),
            "background": job.get("background", args.background),
            "moderation": job.get("moderation", args.moderation),
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        _validate_payload(payload)
        paths = _output_paths(out_dir, prompt, payload["output_format"], index, int(payload["n"]), job.get("out"))

        if args.dry_run:
            print(json.dumps({"endpoint": API_URL, "outputs": [str(path) for path in paths], **payload}, indent=2))
            continue

        try:
            print(f"[{index}/{len(jobs)}] Calling {payload['model']} for {payload['n']} image(s)")
            started = time.time()
            response = (call_edit_api(payload, refs, api_key, args.timeout) if refs
                        else (transport or _call_image_api)(payload, api_key, args.timeout))
            print(f"[{index}/{len(jobs)}] Completed in {time.time() - started:.1f}s")
            _validate_response_metadata(response)
            written = _write_images(response, paths, args.force)
            if audit is not None:
                prompt_id = audit.register_prompt_ref(
                    prompt=prompt,
                    label=f"Image prompt {index}",
                    stage="generate_image",
                    metadata={key: value for key, value in payload.items() if key != "prompt"},
                )
                output_ids = [
                    audit.register_asset(
                        kind="generated_image",
                        path=output,
                        label=Path(output).name,
                        parents=[prompt_id],
                        stage="generate_image",
                        metadata={
                            "model": payload.get("model"),
                            "size": payload.get("size"),
                            "quality": payload.get("quality"),
                            "output_format": payload.get("output_format"),
                            "created": response.get("created"),
                        },
                    )
                    for output in written
                ]
                audit.register_node(
                    stage="generate_image",
                    label=f"Generate image job {index}",
                    parents=[prompt_id],
                    outputs=output_ids,
                    metadata={"model": payload.get("model"), "n": payload.get("n"), "usage": response.get("usage")},
                )
            manifest_jobs.append(
                {
                    "prompt": prompt,
                    "request": {key: value for key, value in payload.items() if key != "prompt"},
                    "outputs": written,
                    "usage": response.get("usage"),
                    "created": response.get("created"),
                }
            )
        except BaseException:
            if manifest_jobs and args.manifest and not args.dry_run:
                try:
                    universal = _build_openai_manifest(
                        args=args, jobs=manifest_jobs, manifest_path=args.manifest,
                    )
                    manifest_writer(args.manifest, universal)
                except Exception:
                    pass
            raise

    if args.manifest and not args.dry_run:
        universal = _build_openai_manifest(
            args=args, jobs=manifest_jobs, manifest_path=args.manifest,
        )
        manifest_writer(args.manifest, universal)
        if audit is not None:
            audit.register_asset(
                kind="image_manifest",
                path=args.manifest,
                label="Generated image manifest",
                stage="generate_image",
                metadata={"jobs": len(manifest_jobs)},
            )
        print(f"Wrote {args.manifest}")
    if args.preset and not args.dry_run and not args.no_open:
        preset = PRESETS.get(args.preset)
        if preset and preset.get("open_result"):
            _open_first_rendered(out_dir)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate images with OpenAI GPT Image models.")
    add = parser.add_argument
    add("--prompt", action="append", help="Prompt; repeat for multiple prompts.")
    add("--prompts-file", type=Path, help="Text, JSON, or JSONL prompt list.")
    add_choice_arg(
        parser,
        "--preset",
        values=sorted(PRESETS),
        help="Use a canned prompt + behaviour preset (e.g. saint-peter-of-banodoco). "
        "If no --prompt or --prompts-file is given, the preset's prompt is used.",
    )
    add("--no-open", action="store_true", help="Suppress opening the rendered image for presets that auto-open.")
    add("--model", default=DEFAULT_MODEL)
    add("--n", type=int, default=1, help="Images per prompt.")
    add("--count", type=int)
    add("--jobs", help=argparse.SUPPRESS)
    add("--mode", choices=("t2i", "edit"))
    add("--execution", default="openai")
    for port in REF_PORTS:
        add("--" + port.replace("_", "-"), dest=port)
    add("--size", default=DEFAULT_SIZE)
    add("--quality", default=DEFAULT_QUALITY)
    add("--output-format", default=DEFAULT_FORMAT)
    add("--output-compression", type=int)
    add_choice_arg(parser, "--background", values=sorted(BACKGROUNDS))
    add_choice_arg(parser, "--moderation", values=sorted(MODERATION))
    add(
        "--out-dir",
        type=Path,
        default=None,
        help="Explicit output/staging directory (required; the runtime supplies this for managed tasks).",
    )
    add(
        "--manifest",
        type=Path,
        default=None,
        help="Optional manifest receipt path; defaults beside --out-dir when omitted.",
    )
    add("--env-file", type=Path)
    add("--timeout", type=int, default=180)
    add("--force", action="store_true")
    add("--dry-run", action="store_true")
    return parser



def validate_reference_bytes(data: bytes, *, index: int = 0) -> str:
    return OPENAI_IMAGE_STORAGE_POLICY.validate_reference_bytes(data, index=index)


class OpenAIBackend(BackendAdapter):
    """Reuse the existing credential-scoped provider boundary for managed jobs."""

    def __init__(self, *, env_file: Path | None = None, transport=None, key_resolver=None) -> None:
        self._env_file = env_file
        self._transport = transport
        self._key_resolver = key_resolver

    def generate(self, entry: Any, mode: str, params: dict[str, Any], out_dir: Path) -> GenerationResult:
        inputs = dict(params, model=getattr(entry, "id", params.get("model", DEFAULT_MODEL)),
                      mode=mode, execution="openai")
        if inputs.get("storage_policy_version") not in (None, OPENAI_IMAGE_STORAGE_POLICY.version):
            raise ValueError("storage policy does not match the OpenAI image capability")
        defaults = dict(_DEFAULTS, prompt=None, prompts_file=None, preset=None,
                        output_compression=None, moderation=None, timeout=180,
                        force=False, dry_run=False, no_open=True)
        defaults.update(inputs)
        defaults.update(out_dir=out_dir, manifest=out_dir.parent / "manifest.json",
                        env_file=self._env_file)
        args = argparse.Namespace(**defaults)
        started = time.monotonic()
        execute_legacy(args, transport=self._transport, key_resolver=self._key_resolver)
        OPENAI_IMAGE_STORAGE_POLICY.validate_manifest(args.manifest)
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        paths = [Path(path) for job in manifest["jobs"] for path in job["outputs"]]
        return GenerationResult(image_paths=paths, model_actual=inputs["model"],
                                duration_ms=int((time.monotonic() - started) * 1000),
                                applied_features=["prompt", "count"] + (["image_ref"] if mode == "edit" else []),
                                manifest=manifest, run_dir=out_dir.parent)
