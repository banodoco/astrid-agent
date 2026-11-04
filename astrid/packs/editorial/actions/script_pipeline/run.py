#!/usr/bin/env python3
"""Preset-driven script pipeline executor."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from astrid.pack_api import build_manifest
from astrid.pack_api import write_manifest as write_result_manifest
from astrid.pack_api import AstridError, render_astrid_error
from astrid.pack_api import guard_canonical_entrypoint
from astrid.pack_api import CredentialsScope

guard_canonical_entrypoint("editorial.script_pipeline")

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None


PACKAGE_ROOT = Path(__file__).resolve().parent
PRESETS_DIR = PACKAGE_ROOT / "presets"
DEFAULT_PRESET = "seinfeld"


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    endpoint: str
    api_key_env: str
    timeout_seconds: int


@dataclass(frozen=True)
class PipelineConfig:
    preset_id: str
    title: str
    provider: ProviderConfig
    prompt: str
    prompts: dict[str, str]
    defaults: dict[str, Any]
    source_path: Path


@dataclass(frozen=True)
class Candidate:
    index: int
    work_dir: Path
    md_path: Path
    final_scene: str
    draft_scene: str
    attempts_blob: str


class ChatClient(Protocol):
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        phase: str,
        candidate_index: int | None = None,
        attempt_index: int | None = None,
    ) -> str:
        """Return assistant text for one chat completion."""


class DeepSeekClient:
    def __init__(self, provider: ProviderConfig, api_key: str) -> None:
        self.provider = provider
        self.api_key = api_key

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        phase: str,
        candidate_index: int | None = None,
        attempt_index: int | None = None,
    ) -> str:
        body = {
            "model": self.provider.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        last_error: Exception | None = None
        for attempt in range(1, 4):
            request = Request(
                self.provider.endpoint,
                data=json.dumps(body).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urlopen(request, timeout=self.provider.timeout_seconds) as response:
                    payload = response.read().decode("utf-8")
                data = json.loads(payload)
                if "error" in data:
                    raise AstridError(
                        f"{self.provider.name} API error: {data['error']}",
                        recovery_command="check the provider API key and model name, then retry",
                    )
                return str(data["choices"][0]["message"]["content"])
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise AstridError(
                        f"{self.provider.name} HTTP {exc.code}: {detail}",
                        recovery_command="fix the request payload or provider configuration",
                    ) from exc
                last_error = RuntimeError(f"{self.provider.name} HTTP {exc.code}: {detail}")
            except (URLError, TimeoutError) as exc:
                last_error = RuntimeError(f"{self.provider.name} request failed: {exc}")
            if attempt < 3:
                wait_seconds = 2 ** attempt
                print(
                    f"{self.provider.name} call failed; retrying in {wait_seconds}s "
                    f"({attempt}/3): {last_error}",
                    file=sys.stderr,
                )
                time.sleep(wait_seconds)
        raise AstridError(
            str(last_error),
            recovery_command="retry later or check network connectivity and provider status",
        )


class FakeScriptClient:
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        phase: str,
        candidate_index: int | None = None,
        attempt_index: int | None = None,
    ) -> str:
        idx = candidate_index or 1
        attempt = attempt_index or 1
        if phase == "rough":
            return (
                "INT. ROOM - DAY\n"
                f"CHARACTER A: Fake rough attempt {attempt} for candidate {idx} has a very specific problem.\n"
                "CHARACTER B: I need the problem stated plainly.\n"
                "CHARACTER C: Plain statements are how plans fall apart."
            )
        if phase == "synth":
            return (
                "INT. ROOM - DAY\n"
                f"CHARACTER A: Candidate {idx} is turning the hallway into a help desk.\n"
                "CHARACTER B: The hallway has tickets now?\n"
                "CHARACTER C: I can't be triaged in a hallway."
            )
        if phase == "voice":
            return (
                "INT. ROOM - DAY\n"
                f"CHARACTER A: Candidate {idx} is turning the hallway into a help desk!\n"
                "CHARACTER B: The hallway has tickets now?\n"
                "CHARACTER C: I can't be triaged in a hallway.\n"
                "END."
            )
        if phase == "judge":
            return json.dumps({"winner": 1, "reason": "Fake judge selected the first deterministic candidate."})
        return messages[-1]["content"]


def load_pipeline_config(preset: str | Path | None, config_path: Path | None = None) -> PipelineConfig:
    source_path = _resolve_config_path(config_path or preset or DEFAULT_PRESET)
    raw = _load_mapping(source_path)
    provider_raw = _mapping(raw.get("provider"), "provider")
    prompts = _mapping(raw.get("prompts"), "prompts")
    defaults = dict(_mapping(raw.get("defaults", {}), "defaults"))
    provider = ProviderConfig(
        name=_required_str(provider_raw, "name", "provider.name"),
        model=_required_str(provider_raw, "model", "provider.model"),
        endpoint=_required_str(provider_raw, "endpoint", "provider.endpoint"),
        api_key_env=str(provider_raw.get("api_key_env") or "DEEPSEEK_API_KEY"),
        timeout_seconds=int(provider_raw.get("timeout_seconds") or 320),
    )
    return PipelineConfig(
        preset_id=str(raw.get("id") or source_path.stem),
        title=str(raw.get("title") or source_path.stem),
        provider=provider,
        prompt=_required_str(raw, "prompt", "prompt"),
        prompts={str(key): str(value) for key, value in prompts.items()},
        defaults=defaults,
        source_path=source_path,
    )


def build_chat_client(config: PipelineConfig, *, fake: bool, env: dict[str, str] | None = None) -> ChatClient:
    if fake:
        return FakeScriptClient()
    if config.provider.name != "deepseek":
        raise AstridError(
            f"unsupported script provider: {config.provider.name}",
            valid_options=["deepseek"],
            recovery_command="set provider.name to 'deepseek' in the preset config",
        )
    api_key = CredentialsScope.get_local("deepseek")
    return DeepSeekClient(config.provider, api_key)


def run_pipeline(
    *,
    config: PipelineConfig,
    client: ChatClient,
    produces_dir: Path,
    prompt: str,
    candidates_count: int,
    rough_attempts: int,
    select_best: bool,
    max_workers: int,
) -> dict[str, Any]:
    produces_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[Candidate] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, candidates_count))) as pool:
        futures = [
            pool.submit(
                run_candidate,
                config=config,
                client=client,
                index=index,
                produces_dir=produces_dir,
                prompt=prompt,
                rough_attempts=rough_attempts,
            )
            for index in range(1, candidates_count + 1)
        ]
        for future in as_completed(futures):
            candidates.append(future.result())
    candidates.sort(key=lambda candidate: candidate.index)

    if select_best:
        winner_index, judge_reason = judge_best(config, client, candidates)
    else:
        winner_index = candidates[0].index
        judge_reason = "Selection skipped; defaulted to first candidate."
    selected = next(candidate for candidate in candidates if candidate.index == winner_index)
    selected_path = write_selected_scene(produces_dir, selected, winner_index, judge_reason)
    manifest = write_pipeline_manifest(
        produces_dir,
        config=config,
        prompt=prompt,
        rough_attempts=rough_attempts,
        candidates=candidates,
        selected_index=winner_index,
        selected_path=selected_path,
        judge_reason=judge_reason,
    )
    return manifest


def run_candidate(
    *,
    config: PipelineConfig,
    client: ChatClient,
    index: int,
    produces_dir: Path,
    prompt: str,
    rough_attempts: int,
) -> Candidate:
    run_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{index:02d}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    work_dir = produces_dir / "work" / run_id
    work_dir.mkdir(parents=True, exist_ok=False)

    def rough(attempt_index: int) -> str:
        content = client.complete(
            [{"role": "system", "content": config.prompts["rough_system"]}, {"role": "user", "content": prompt}],
            temperature=_float_default(config, "rough_temperature", 2.0),
            max_tokens=_int_default(config, "max_tokens", 8192),
            phase="rough",
            candidate_index=index,
            attempt_index=attempt_index,
        ).strip()
        write_text(work_dir / f"rough_{attempt_index:02d}.txt", content)
        return content

    with ThreadPoolExecutor(max_workers=rough_attempts) as pool:
        rough_scenes = list(pool.map(rough, range(1, rough_attempts + 1)))

    attempts_blob = format_attempts_blob(rough_scenes)
    synth_prompt = render_template(
        config.prompts["synth_template"],
        prompt=prompt,
        rough_attempts=rough_attempts,
        attempts_blob=attempts_blob,
    )
    draft_scene = client.complete(
        [{"role": "system", "content": config.prompts["synth_system"]}, {"role": "user", "content": synth_prompt}],
        temperature=_float_default(config, "synth_temperature", 1.0),
        max_tokens=_int_default(config, "max_tokens", 8192),
        phase="synth",
        candidate_index=index,
    ).strip()
    write_text(work_dir / "draft_scene.txt", draft_scene)

    voice_prompt = render_template(config.prompts["voice_template"], draft_scene=draft_scene)
    final_scene = client.complete(
        [{"role": "system", "content": config.prompts["voice_system"]}, {"role": "user", "content": voice_prompt}],
        temperature=_float_default(config, "voice_temperature", 1.0),
        max_tokens=_int_default(config, "max_tokens", 8192),
        phase="voice",
        candidate_index=index,
    ).strip()
    md_path = write_candidate_markdown(
        produces_dir,
        config=config,
        candidate_index=index,
        run_id=run_id,
        rough_attempts=rough_attempts,
        final_scene=final_scene,
        draft_scene=draft_scene,
        attempts_blob=attempts_blob,
    )
    return Candidate(index, work_dir, md_path, final_scene, draft_scene, attempts_blob)


def judge_best(config: PipelineConfig, client: ChatClient, candidates: list[Candidate]) -> tuple[int, str]:
    if len(candidates) == 1:
        return candidates[0].index, "Only one candidate was generated."
    blob = "\n\n---\n\n".join(f"## Candidate {candidate.index}\n\n{candidate.final_scene}" for candidate in candidates)
    content = client.complete(
        [{"role": "system", "content": config.prompts["judge_system"]}, {"role": "user", "content": blob}],
        temperature=_float_default(config, "judge_temperature", 0.2),
        max_tokens=_int_default(config, "judge_max_tokens", 1024),
        phase="judge",
    ).strip()
    try:
        payload = json.loads(content)
        winner = int(payload["winner"])
        reason = str(payload["reason"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AstridError(
            f"judge returned invalid JSON: {content}",
            recovery_command="retry with a different prompt or increase judge_max_tokens",
        ) from exc
    if winner not in {candidate.index for candidate in candidates}:
        raise AstridError(
            f"judge selected unknown candidate {winner}",
            recovery_command="increase candidates count or fix the judge prompt",
        )
    return winner, reason


def write_candidate_markdown(
    produces_dir: Path,
    *,
    config: PipelineConfig,
    candidate_index: int,
    run_id: str,
    rough_attempts: int,
    final_scene: str,
    draft_scene: str,
    attempts_blob: str,
) -> Path:
    md_path = produces_dir / "candidates" / f"candidate_{candidate_index:02d}_{run_id}.md"
    markdown = f"""# {config.title} - candidate {candidate_index}

*Pipeline:*
1. Rough ideation - {rough_attempts} attempts
2. Synthesis - structure pass
3. Voice/style pass

---

## Final scene

{final_scene}

---

## Draft before voice/style pass

{draft_scene}

---

## Source attempts

{attempts_blob}
"""
    write_text(md_path, markdown)
    return md_path


def write_selected_scene(produces_dir: Path, selected: Candidate, winner_index: int, judge_reason: str) -> Path:
    selected_md = selected.md_path.read_text(encoding="utf-8")
    selected_md += f"\n---\n\n## Selection\n\nWinner: candidate {winner_index}\n\n{judge_reason}\n"
    selected_path = produces_dir / "selected_scene.md"
    write_text(selected_path, selected_md)
    return selected_path


def write_pipeline_manifest(
    produces_dir: Path,
    *,
    config: PipelineConfig,
    prompt: str,
    rough_attempts: int,
    candidates: list[Candidate],
    selected_index: int,
    selected_path: Path,
    judge_reason: str,
) -> dict[str, Any]:
    manifest = build_manifest(
        kind="script_pipeline_scene",
        inputs={
            "preset": config.preset_id,
            "prompt": prompt,
        },
        outputs=[
            {"path": "selected_scene.md", "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
        preset=config.preset_id,
        preset_path=str(config.source_path),
        provider={
            "name": config.provider.name,
            "model": config.provider.model,
        },
        prompt=prompt,
        rough_attempts=rough_attempts,
        candidates=[
            {
                "index": candidate.index,
                "markdown": str(candidate.md_path),
                "work_dir": str(candidate.work_dir),
            }
            for candidate in candidates
        ],
        selected_index=selected_index,
        selected_scene=str(selected_path),
        judge_reason=judge_reason,
    )
    write_result_manifest(produces_dir / "manifest.json", manifest)
    return manifest


def format_attempts_blob(scenes: list[str]) -> str:
    return "\n\n---\n\n".join(f"### Attempt {index + 1}\n\n{scene.strip()}" for index, scene in enumerate(scenes))


def render_template(template: str, **values: Any) -> str:
    return template.format(**values)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_prompt(args: argparse.Namespace, config: PipelineConfig) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8").strip()
    return str(args.prompt or config.prompt)


def _load_env_file(path: Path | None) -> None:
    if path is None or not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a preset-driven script pipeline.")
    parser.add_argument("--produces-dir", type=Path, required=True)
    parser.add_argument("--preset", default=DEFAULT_PRESET, help="Built-in preset name or path to YAML/JSON.")
    parser.add_argument("--config", type=Path, help="Explicit YAML/JSON preset config path.")
    parser.add_argument("--prompt", help="Scene brief override.")
    parser.add_argument("--prompt-file", type=Path, help="Read scene brief from a text file.")
    parser.add_argument("--candidates", type=int, help="Complete pipeline candidates to generate.")
    parser.add_argument("--rough-attempts", type=int, help="Rough attempts per candidate.")
    parser.add_argument("--select-best", action="store_true", help="Run judge pass when multiple candidates exist.")
    parser.add_argument("--fake", action="store_true", help="Use deterministic no-network responses.")
    parser.add_argument("--max-workers", type=int, default=5, help="Maximum concurrent complete candidates.")
    parser.add_argument("--env-file", type=Path, default=Path.home() / ".hermes" / ".env")
    parser.add_argument("--open-result", action="store_true", help="Open selected_scene.md after writing it.")
    parser.add_argument("--json", action="store_true", help="Print manifest JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_pipeline_config(args.preset, args.config)
    candidates_count = int(args.candidates or config.defaults.get("candidates") or 1)
    rough_attempts = int(args.rough_attempts or config.defaults.get("rough_attempts") or 1)
    if candidates_count < 1:
        raise SystemExit("--candidates must be >= 1")
    if rough_attempts < 1:
        raise SystemExit("--rough-attempts must be >= 1")
    if args.max_workers < 1:
        raise SystemExit("--max-workers must be >= 1")

    _load_env_file(args.env_file)
    client = build_chat_client(config, fake=bool(args.fake))
    prompt = load_prompt(args, config)
    select_best = bool(args.select_best or config.defaults.get("select_best"))
    manifest = run_pipeline(
        config=config,
        client=client,
        produces_dir=args.produces_dir,
        prompt=prompt,
        candidates_count=candidates_count,
        rough_attempts=rough_attempts,
        select_best=select_best,
        max_workers=args.max_workers,
    )
    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        print(f"selected: {manifest['selected_scene']}")
    if args.open_result:
        subprocess.run(["open", str(manifest["selected_scene"])], check=False)

    # --- universal result manifest -----------------------
    result_manifest_path = args.produces_dir / "result_manifest.json"
    result_manifest = build_manifest(
        kind="script_pipeline",
        inputs={
            "preset": str(args.preset),
            "prompt": prompt,
            "produces_dir": str(args.produces_dir),
        },
        outputs=[
            {"path": "selected_scene.md", "type": "file"},
            {"path": "manifest.json", "type": "file"},
            {"path": "candidates", "type": "directory"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_result_manifest(result_manifest_path, result_manifest)
    # -------------------------------------------------------------------------

    return 0


def _resolve_config_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.suffix.lower() in {".yaml", ".yml", ".json"} or path.parent != Path("."):
        return path.resolve()
    return (PRESETS_DIR / f"{path}.yaml").resolve()


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise AstridError(
                "PyYAML is required to parse script pipeline presets",
                recovery_command="install PyYAML with: pip install pyyaml",
            )
        loaded = yaml.safe_load(text)
    else:
        loaded = json.loads(text)
    if not isinstance(loaded, dict):
        raise AstridError(
            f"script pipeline config must be an object: {path}",
            recovery_command="ensure the preset file contains a JSON/YAML object (not a list or scalar)",
        )
    return loaded


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AstridError(
            f"{path} must be an object",
            recovery_command="ensure the preset section is a JSON/YAML object",
        )
    return value


def _required_str(values: dict[str, Any], key: str, path: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise AstridError(
            f"{path} is required",
            recovery_command="add the missing required field to the preset config",
        )
    return value


def _int_default(config: PipelineConfig, key: str, fallback: int) -> int:
    value = config.defaults.get(key, fallback)
    return int(value)


def _float_default(config: PipelineConfig, key: str, fallback: float) -> float:
    value = config.defaults.get(key, fallback)
    return float(value)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AstridError as exc:
        raise SystemExit(render_astrid_error(exc))
