#!/usr/bin/env python3
"""Generate image files with OpenAI GPT Image models."""


from __future__ import annotations

from astrid.core.contracts.errors import AstridValueError
from astrid.core.pack.entrypoint import (
    guard_canonical_entrypoint,
    run_pack_main,
    warn_if_unledgered,
)

guard_canonical_entrypoint('generation.generate_image_openai')
import argparse
import base64
import json
import os
import re
import subprocess
import time
import warnings
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


def _warn(message: str) -> None:
    warnings.warn(message)


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


def _normalize_job(item: Any, index: int) -> dict[str, Any]:
    if isinstance(item, str):
        prompt = item.strip()
        if not prompt:
            _die(f"Empty prompt at item {index}", recovery_command="provide a non-empty prompt string")
        return {"prompt": prompt}
    if isinstance(item, dict) and str(item.get("prompt", "")).strip():
        return dict(item)
    _die(f"Invalid prompt item {index}; expected string or object with prompt", recovery_command="provide a valid prompt string or JSON object with a prompt field")
    return {}


def _load_prompts(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        _die(f"Prompts file not found: {path}", recovery_command="verify the prompts file path exists and is readable")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        _die(f"Prompts file is empty: {path}", recovery_command="provide a non-empty prompts file with at least one prompt")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, list):
            _die("JSON prompts file must contain an array", recovery_command="provide a JSON array of prompt objects in the prompts file")
        return [_normalize_job(item, index + 1) for index, item in enumerate(data)]
    jobs: list[dict[str, Any]] = []
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("{") or line.startswith('"'):
            jobs.append(_normalize_job(json.loads(line), line_no))
        else:
            jobs.append({"prompt": line})
    if not jobs:
        _die("No prompts found", recovery_command="provide at least one prompt via --prompt, --prompts-file, or --preset")
    return jobs


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
    request = Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        _die(f"OpenAI API error {exc.code}: {detail}", recovery_command="check your API key and prompt parameters; retry later")
    except URLError as exc:
        _die(f"Network error: {exc}", recovery_command="check your network connection and retry")
    return {}


def _write_images(response: dict[str, Any], paths: list[Path], force: bool) -> list[str]:
    written: list[str] = []
    for item, path in zip(response.get("data") or [], paths):
        image_b64 = item.get("b64_json")
        if not image_b64:
            _warn(f"No b64_json returned for {path}")
            continue
        if path.exists() and not force:
            _die(f"Output exists: {path} (use --force to overwrite)", recovery_command="use --force to overwrite existing output files")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(image_b64))
        print(f"Wrote {path}")
        written.append(str(path))
    return written


def _jobs_from_args(args: argparse.Namespace) -> list[dict[str, Any]]:
    jobs = [{"prompt": prompt} for prompt in args.prompt or []]
    if args.prompts_file:
        jobs.extend(_load_prompts(args.prompts_file))
    if not jobs and args.preset:
        preset = PRESETS.get(args.preset)
        if preset is None:
            _die(f"Unknown preset {args.preset!r}; available: {', '.join(sorted(PRESETS))}", valid_options=sorted(PRESETS), recovery_command="use one of the available presets listed above")
        jobs.append({"prompt": preset["prompt"]})
    if not jobs:
        _die("Provide --prompt, --prompts-file, or --preset", recovery_command="use --prompt, --prompts-file, or --preset to specify at least one prompt")
    return jobs


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
    return manifest


def generate(args: argparse.Namespace) -> int:
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
    api_key = None if args.dry_run else _resolve_key(env_file=args.env_file)
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
            response = _call_image_api(payload, api_key, args.timeout)
            print(f"[{index}/{len(jobs)}] Completed in {time.time() - started:.1f}s")
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
                    write_json_atomic(args.manifest, universal)
                except Exception:
                    pass
            raise

    if args.manifest and not args.dry_run:
        universal = _build_openai_manifest(
            args=args, jobs=manifest_jobs, manifest_path=args.manifest,
        )
        write_json_atomic(args.manifest, universal)
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


def main(argv: list[str] | None = None) -> int:
    warn_if_unledgered()

    def _run() -> int:
        args = build_parser().parse_args(argv)
        return generate(args)

    return run_pack_main("generation.generate_image_openai", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
