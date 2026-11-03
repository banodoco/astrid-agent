"""Run one bounded personal Discord generation attempt."""

from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint("discord_local.command")

import argparse
import json
import mimetypes
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.pack_api import write_json_atomic, write_manifest
from astrid.pack_api import AstridError
from astrid.pack_api import sha256_file
from astrid.pack_api import run_pack_main


# Used only to scrub internal paths from debug text. Runtime dependencies are
# supplied explicitly and are never located relative to this package path.
_SOURCE_ROOT_FOR_REDACTION = Path(__file__).resolve().parents[5]
VARIANTS_FILE = Path(__file__).resolve().parents[2] / "model_variants.json"
OPTION_RE = re.compile(
    r"\s+(resolution|duration|seed|aspect_ratio|input_media(?:_(?:[2-9]|10))?):([^\s]+)",
    re.IGNORECASE,
)
COMMAND_RE = re.compile(r"^(\/\S+)\s+prompt\s*:(.*)$", re.IGNORECASE | re.DOTALL)
DISCORD_CHANNEL_RE = re.compile(
    r"^https://discord\.com/channels/\d+/\d+/?$"
)
DISCORD_URL_RE = re.compile(
    r"https://(?:discord\.com/channels/\d+/\d+/?|(?:cdn|media)\.discordapp\.(?:com|net)/\S+)"
)


@dataclass(frozen=True)
class ParsedCommand:
    slash_name: str
    prompt: str
    options: tuple[tuple[str, str], ...]


def _load_variants(path: Path = VARIANTS_FILE) -> dict[str, dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read Discord model variants: {exc}") from exc
    variants = payload.get("variants")
    if not isinstance(variants, dict) or not variants:
        raise ValueError("Discord model variants file has no variants")
    normalized: dict[str, dict[str, str]] = {}
    for key, entry in variants.items():
        if (
            not isinstance(key, str)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("channel_url"), str)
            or not DISCORD_CHANNEL_RE.fullmatch(entry["channel_url"])
        ):
            raise ValueError(f"invalid Discord model variant: {key!r}")
        normalized[key.lower()] = {
            "label": str(entry.get("label") or key),
            "channel_url": entry["channel_url"],
        }
    return normalized


def _resolve_variant(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.variant:
        variants = _load_variants()
        key = args.variant.lower()
        if key not in variants:
            parser.error(
                "--variant must be one of " + ", ".join(sorted(variants))
            )
        mapped_url = variants[key]["channel_url"]
        if args.channel_url and args.channel_url.rstrip("/") != mapped_url.rstrip("/"):
            parser.error("--channel-url conflicts with the selected --variant")
        args.variant = key
        args.channel_url = mapped_url
    if not args.channel_url:
        parser.error("provide --variant or --channel-url")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_command(command: str) -> ParsedCommand:
    text = command.strip()
    match = COMMAND_RE.match(text)
    if not match:
        raise ValueError(
            "command must use '/command prompt:<text> [named options]' format"
        )
    slash_name, body = match.groups()
    option_matches = list(OPTION_RE.finditer(body))
    prompt_end = option_matches[0].start() if option_matches else len(body)
    prompt = body[:prompt_end].strip()
    if not prompt:
        raise ValueError("prompt cannot be empty")
    options = tuple(
        (item.group(1).lower(), item.group(2)) for item in option_matches
    )
    return ParsedCommand(slash_name=slash_name, prompt=prompt, options=options)


def _attachment_source(value: str) -> Path:
    return Path(value[1:] if value.startswith("@") else value).expanduser().resolve()


def _media_type(path: Path, declared: str | None = None) -> str:
    if declared:
        return declared.split(";", 1)[0].strip()
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _role_for(path: Path) -> str:
    media_type = _media_type(path)
    if media_type.startswith("image/"):
        return "appearance_reference"
    if media_type.startswith("video/"):
        return "source_video"
    if media_type.startswith("audio/"):
        return "source_audio"
    return "other"


def _safe_name(name: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name).lstrip("._")
    return safe[:180] or "artifact"


def _stage_inputs(parsed: ParsedCommand, out: Path) -> list[dict[str, Any]]:
    staged: list[dict[str, Any]] = []
    input_dir = out / "inputs"
    for key, value in parsed.options:
        if not key.startswith("input_media"):
            continue
        source = _attachment_source(value)
        if not source.is_file():
            raise ValueError(f"input attachment is missing or unreadable: {source}")
        ordinal = len(staged) + 1
        destination = input_dir / f"{ordinal:02d}-{_safe_name(source.name)}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        staged.append(
            {
                "ordinal": ordinal,
                "role": _role_for(destination),
                "path": destination.relative_to(out).as_posix(),
                "content_hash": f"sha256:{sha256_file(destination)}",
                "media_type": _media_type(destination),
                "option": key,
            }
        )
    return staged


def _portable_command(parsed: ParsedCommand, staged: list[dict[str, Any]]) -> str:
    replacements = {item["option"]: item["path"] for item in staged}
    parts = [f"{parsed.slash_name} prompt:{parsed.prompt}"]
    for key, value in parsed.options:
        portable = f"@{replacements[key]}" if key in replacements else value
        parts.append(f"{key}:{portable}")
    return " ".join(parts)


def _request_inputs(
    parsed: ParsedCommand | None,
    staged: list[dict[str, Any]],
) -> dict[str, Any]:
    inputs: dict[str, Any] = {
        "execution": "browser",
        "mode": "discord_slash_command",
        "ordered_artifacts": [
            {key: value for key, value in item.items() if key != "option"}
            for item in staged
        ],
    }
    if parsed is None:
        return inputs
    inputs["prompt"] = parsed.prompt
    inputs["prompt_capture"] = "exact"
    inputs["command"] = _portable_command(parsed, staged)
    for key, value in parsed.options:
        if key.startswith("input_media"):
            continue
        if key == "seed" and value.isdigit():
            inputs[key] = int(value)
        else:
            inputs[key] = value
    return inputs


def _sanitize_text(
    text: str, *, out: Path, redact_paths: tuple[Path, ...] = ()
) -> str:
    sanitized = DISCORD_URL_RE.sub("[discord-url-redacted]", text)
    sanitized = sanitized.replace(str(out.resolve()), "$OUT")
    sanitized = sanitized.replace(str(_SOURCE_ROOT_FOR_REDACTION), "$ASTRID_ROOT")
    for path in redact_paths:
        sanitized = sanitized.replace(str(path.expanduser().resolve()), "$LOCAL_PATH")
    return sanitized


def _new_capture_dir(capture_root: Path, before: set[Path]) -> Path | None:
    candidates = [
        item
        for item in capture_root.iterdir()
        if item.is_dir() and item not in before
    ] if capture_root.exists() else []
    if not candidates:
        candidates = [
            item for item in capture_root.iterdir() if item.is_dir()
        ] if capture_root.exists() else []
    return max(candidates, key=lambda item: item.stat().st_mtime_ns) if candidates else None


def _classify_failure(returncode: int, stderr: str) -> tuple[str, str]:
    lowered = stderr.lower()
    message = stderr.strip().splitlines()[-1] if stderr.strip() else (
        f"Discord browser runner exited with status {returncode}"
    )
    if returncode in (130, 143, -2, -15):
        return "interrupted", message
    if "within " in lowered and (
        "no new attachment appeared" in lowered
        or "no matching completed attachment appeared" in lowered
        or "composer did not appear" in lowered
    ):
        return "timed_out", message
    if "discord rejected" in lowered or "not a valid choice" in lowered:
        return "provider_rejected", message
    return "failed", message


def _move_outputs(
    raw: dict[str, Any],
    capture_dir: Path | None,
    out: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    outputs: list[dict[str, Any]] = []
    warnings: list[str] = []
    output_dir = out / "outputs"
    for index, item in enumerate(raw.get("downloads", []), start=1):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        source = Path(item["path"]).resolve()
        if capture_dir is None:
            warnings.append("Runner reported a download without a capture directory")
            continue
        try:
            source.relative_to(capture_dir.resolve())
        except ValueError:
            warnings.append("Runner reported a download outside its capture directory")
            continue
        if not source.is_file():
            warnings.append(f"Runner download {index} was missing during finalization")
            continue
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = _safe_name(source.name)
        destination = output_dir / stem
        suffix = 2
        while destination.exists():
            destination = output_dir / f"{Path(stem).stem}-{suffix}{Path(stem).suffix}"
            suffix += 1
        shutil.move(str(source), destination)
        outputs.append(
            {
                "path": destination.relative_to(out).as_posix(),
                "type": "file",
                "media_type": _media_type(destination, item.get("contentType")),
            }
        )
    return outputs, warnings


def _safe_raw_result(raw: dict[str, Any], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "fetched_at": raw.get("fetchedAt"),
        "submitted_at": raw.get("submittedAt"),
        "completed_at": raw.get("completedAt"),
        "after": raw.get("after"),
        "match": raw.get("match"),
        "link_match": raw.get("linkMatch"),
        "expected_author": raw.get("expectedAuthor"),
        "excluded_filenames": raw.get("excludeFilenames", []),
        "response_message_id": raw.get("responseMessageId"),
        "response_preview": raw.get("responsePreview"),
        "source_url_count": len(raw.get("downloads", [])),
        "outputs": outputs,
    }


def _relocate_debug(
    capture_dir: Path | None,
    capture_root: Path,
    out: Path,
    *,
    stdout: str,
    stderr: str,
    safe_raw: dict[str, Any] | None,
    redact_paths: tuple[Path, ...] = (),
) -> str | None:
    debug_root = out / "debug"
    debug_root.mkdir(parents=True, exist_ok=True)
    (debug_root / "runner.stdout.log").write_text(
        _sanitize_text(stdout, out=out, redact_paths=redact_paths), encoding="utf-8"
    )
    (debug_root / "runner.stderr.log").write_text(
        _sanitize_text(stderr, out=out, redact_paths=redact_paths), encoding="utf-8"
    )
    relative_capture = None
    if capture_dir is not None and capture_dir.exists():
        if safe_raw is not None:
            write_json_atomic(capture_dir / "result.json", safe_raw)
        destination = debug_root / capture_dir.name
        if destination.exists():
            destination = debug_root / f"{capture_dir.name}-{time.time_ns()}"
        shutil.move(str(capture_dir), destination)
        relative_capture = destination.relative_to(out).as_posix()
    if capture_root.exists() and not any(capture_root.iterdir()):
        capture_root.rmdir()
    return relative_capture


def _build_runner_command(args: argparse.Namespace, capture_root: Path) -> list[str]:
    command = [
        args.node_bin,
        str(args.runner_script),
        "--channel-url",
        args.channel_url,
        "--profile-dir",
        str(args.profile_dir),
        "--cdp-port",
        str(args.cdp_port),
        "--output-dir",
        str(capture_root),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--no-settle",
    ]
    if args.mode == "fetch":
        command.extend(["--fetch-only", "--after", args.after])
        if args.response_message_id:
            command.extend(["--message-id", args.response_message_id])
    else:
        command.extend(["--command-file", str(args.command_file)])
        if args.mode in {"queue", "submit"}:
            command.append("--submit")
        if args.mode == "queue":
            command.append("--no-watch")
    for flag, value in (
        ("--match", args.match),
        ("--link-match", args.link_match),
        ("--expected-author", args.expected_author),
    ):
        if value:
            command.extend([flag, value])
    for name in args.exclude_filenames:
        command.extend(["--exclude-filename", name])
    return command


def execute(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    started_at = _now()
    start_clock = time.monotonic()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    capture_root = out / ".capture"
    capture_root.mkdir(parents=True, exist_ok=True)

    parsed = None
    staged: list[dict[str, Any]] = []
    if args.command_file:
        command_text = args.command_file.read_text(encoding="utf-8").strip()
        parsed = parse_command(command_text)
        staged = _stage_inputs(parsed, out)
        (out / "prompt.txt").write_text(f"{parsed.prompt}\n", encoding="utf-8")
        (out / "command.txt").write_text(
            f"{_portable_command(parsed, staged)}\n", encoding="utf-8"
        )

    before = {item for item in capture_root.iterdir() if item.is_dir()}
    command = _build_runner_command(args, capture_root)
    returncode = 1
    stdout = ""
    stderr = ""
    status = "failed"
    error: str | None = None
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # The Node runner uses the configured bound independently for
            # page/composer readiness and post-submit/fetch watching.
            timeout=(args.timeout_seconds * 2) + 90,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        if returncode == 0:
            if args.mode == "preview":
                status = "draft"
            elif args.mode == "queue":
                status = "partial"
            else:
                status = "completed"
        else:
            status, error = _classify_failure(returncode, stderr)
    except subprocess.TimeoutExpired as exc:
        status = "timed_out"
        error = "Discord browser runner exceeded its bounded process timeout"
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        returncode = 124

    capture_dir = _new_capture_dir(capture_root, before)
    raw: dict[str, Any] = {}
    raw_result_path = capture_dir / "result.json" if capture_dir else None
    if raw_result_path and raw_result_path.is_file():
        try:
            loaded = json.loads(raw_result_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
        except (OSError, json.JSONDecodeError) as exc:
            if status == "completed":
                status = "failed"
                error = f"Runner result could not be read: {exc}"

    outputs, warnings = _move_outputs(raw, capture_dir, out)
    if status == "completed" and args.mode != "preview" and not outputs:
        status = "failed"
        error = "Discord runner completed without a downloaded media output"
    safe_raw = _safe_raw_result(raw, outputs) if raw else None
    redact_paths = [args.runner_script, args.profile_dir]
    if args.command_file is not None:
        redact_paths.append(args.command_file)
    if parsed is not None:
        redact_paths.extend(
            _attachment_source(value)
            for key, value in parsed.options
            if key.startswith("input_media")
        )
    path_redactions = tuple(redact_paths)
    debug_capture = _relocate_debug(
        capture_dir,
        capture_root,
        out,
        stdout=stdout,
        stderr=stderr,
        safe_raw=safe_raw,
        redact_paths=path_redactions,
    )

    completed_at = _now()
    duration_ms = round((time.monotonic() - start_clock) * 1000)
    inputs = _request_inputs(parsed, staged)
    provider_extension = {
        "action": args.mode,
        "variant": args.variant,
        "watcher": {
            "bounded": True,
            "timeout_seconds": args.timeout_seconds,
            "after": args.after,
            "match": args.match or raw.get("match"),
            "link_match": args.link_match or raw.get("linkMatch"),
            "expected_author": args.expected_author,
            "target_message_id": args.response_message_id,
            "excluded_filenames": args.exclude_filenames,
        },
        "response_message_id": raw.get("responseMessageId"),
        "response_preview": raw.get("responsePreview"),
        "source_url_count": len(raw.get("downloads", [])),
        "debug_capture": debug_capture,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
    }
    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": "discord_local.attempt",
        "status": status,
        "mode": args.mode,
        "variant": args.variant,
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
        "prompt": parsed.prompt if parsed else None,
        "ordered_inputs": inputs["ordered_artifacts"],
        "outputs": outputs,
        "watcher": provider_extension["watcher"],
        "response_message_id": raw.get("responseMessageId"),
        "source_url_count": len(raw.get("downloads", [])),
        "warnings": warnings,
    }
    if error:
        error = _sanitize_text(error, out=out, redact_paths=path_redactions)
        result["error"] = error
    write_json_atomic(out / "result.json", result)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "discord_browser.generate",
        "inputs": inputs,
        "outputs": outputs,
        "created": started_at,
        "warnings": warnings,
        "status": status,
        "provider_extension": provider_extension,
    }
    if error:
        manifest["error"] = error
    written = write_manifest(out / "manifest.json", manifest)
    # Provider lifecycle outcomes are captured data, not executor crashes.
    # Keep the subprocess successful so managed runs remain available to the
    # experiment layer; reserve non-zero for adapter/infrastructure failure or
    # interruption.
    captured_outcomes = {
        "completed",
        "draft",
        "partial",
        "provider_rejected",
        "timed_out",
    }
    return returncode if status not in captured_outcomes else 0, written


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("preview", "queue", "submit", "fetch"),
    )
    parser.add_argument("--channel-url")
    parser.add_argument("--variant")
    parser.add_argument("--command-file", type=Path)
    parser.add_argument("--after")
    parser.add_argument("--match")
    parser.add_argument("--link-match")
    parser.add_argument("--expected-author")
    parser.add_argument("--response-message-id")
    parser.add_argument("--exclude-filenames", default="")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path.home() / ".astrid" / "discord-command-poc-profile",
    )
    parser.add_argument("--cdp-port", type=int, default=9333)
    parser.add_argument("--runner-script", type=Path, required=True)
    parser.add_argument("--node-bin", default="node")
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    _resolve_variant(args, parser)
    if not DISCORD_CHANNEL_RE.fullmatch(args.channel_url):
        parser.error(
            "--channel-url must be https://discord.com/channels/<guild>/<channel>"
        )
    if args.mode in {"preview", "queue", "submit"} and args.command_file is None:
        parser.error("--command-file is required for preview, queue, and submit modes")
    if args.mode == "fetch" and not args.after:
        parser.error("--after is required for fetch mode")
    if args.response_message_id and not args.response_message_id.isdigit():
        parser.error("--response-message-id must be a Discord snowflake")
    if args.command_file is not None and not args.command_file.is_file():
        parser.error(f"--command-file is not a readable file: {args.command_file}")
    if not 30 <= args.timeout_seconds <= 7200:
        parser.error("--timeout-seconds must be between 30 and 7200")
    if not 1024 <= args.cdp_port <= 65535:
        parser.error("--cdp-port must be between 1024 and 65535")
    if not args.runner_script.is_file():
        parser.error(f"Discord runner script is missing: {args.runner_script}")
    args.exclude_filenames = [
        value.strip()
        for value in args.exclude_filenames.split(",")
        if value.strip()
    ]
    if any("/" in value or "\\" in value for value in args.exclude_filenames):
        parser.error("--exclude-filenames accepts basenames only")


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        parser = _parser()
        args = parser.parse_args(argv)
        _validate_args(args, parser)
        returncode, manifest = execute(args)
        print(
            json.dumps(
                {
                    "status": manifest["status"],
                    "manifest": str(args.out / "manifest.json"),
                    "outputs": len(manifest["outputs"]),
                },
                sort_keys=True,
            )
        )
        if returncode:
            raise AstridError(
                manifest.get("error", "Discord browser attempt failed"),
                recovery_command=(
                    "inspect result.json and debug/, then retry preview/fetch or "
                    "submit a new command explicitly"
                ),
                state_snapshot={
                    "status": manifest["status"],
                    "manifest": str(args.out / "manifest.json"),
                },
            )
        return 0

    return run_pack_main("discord_local.command", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
