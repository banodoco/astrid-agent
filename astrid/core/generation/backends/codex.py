"""CodexBackend — image generation through ``codex exec``.

This backend drives Codex's built-in ``image_generation`` tool.  It does not
read ``OPENAI_API_KEY``; Codex authenticates through ``~/.codex/auth.json``.
"""

from __future__ import annotations

import contextlib
import logging
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrid.core.generation.backends.base import (
    BackendAdapter,
    GenerationResult,
    split_feature_support,
)
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry

logger = logging.getLogger(__name__)

CODEX_IMAGE_DIR = Path.home() / ".codex" / "generated_images"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"
SESSION_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{36})")

SIZE_HINTS = {
    "1024x1024": "Use a square 1:1 aspect ratio.",
    "1536x1024": "Use a wide landscape 3:2 aspect ratio.",
    "1024x1536": "Use a tall portrait 2:3 aspect ratio.",
    "auto": "",
}
QUALITY_HINTS = {
    "low": "Draft quality is fine; favour speed.",
    "medium": "",
    "high": "Render at high fidelity and detail.",
    "auto": "",
}
BACKGROUND_HINTS = {
    "transparent": "Make the background fully transparent with no backdrop.",
    "opaque": "",
    "auto": "",
}

SubprocessRunner = Callable[..., subprocess.CompletedProcess[str]]


def codex_unavailable_reason(
    *,
    codex_bin: str = "codex",
    auth_file: Path = CODEX_AUTH_FILE,
) -> str | None:
    """Return a cheap unavailable reason, or ``None`` when Codex is usable."""
    if shutil.which(codex_bin) is None:
        return "`codex` binary not found on PATH"
    if not auth_file.expanduser().is_file():
        return f"Codex auth file missing: {auth_file.expanduser()}"
    return None



_ATTEMPT_HOME_ENV = "ASTRID_CODEX_ATTEMPT_HOME"
_attempt_home_override: str | None = None


@contextlib.contextmanager
def managed_attempt_home(path: str | Path):
    """Bind a managed attempt's private Codex home for this process.

    The pack host's command custody binds the launch environment, and the
    attempt's Git source checks compare against it: writing the home into
    os.environ after launch failed them ("command inventory delegated closure
    is uncertain"). The environment variable still works for standalone use.
    """
    global _attempt_home_override
    previous, _attempt_home_override = _attempt_home_override, str(path)
    try:
        yield
    finally:
        _attempt_home_override = previous


def _managed_home() -> str | None:
    return _attempt_home_override or os.environ.get(_ATTEMPT_HOME_ENV)


class CodexBackend(BackendAdapter):
    """Generate images via Codex's built-in ``image_generation`` tool."""

    def __init__(
        self,
        *,
        runner: SubprocessRunner | None = None,
        codex_bin: str = "codex",
        generated_images_dir: Path | None = None,
    ) -> None:
        self._runner = runner or subprocess.run
        self._codex_bin = codex_bin
        self._generated_images_dir = generated_images_dir or CODEX_IMAGE_DIR

    def generate(
        self,
        entry: ModelEntry,
        mode: str,
        params: dict[str, Any],
        out_dir: Path,
    ) -> GenerationResult:
        mode_spec = entry.modes[mode]
        backend_spec: BackendSpec = mode_spec.backends["codex"]
        endpoint = backend_spec.endpoint or "codex/gpt-image"
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)

        t0 = time.monotonic()
        started_at = time.time()
        # Managed attempts keep Codex's generated files and credentials inside
        # the host's measured scratch tree, never in the user's global cache.
        managed_home = _managed_home()
        if managed_home:
            self._generated_images_dir = Path(managed_home) / "generated_images"
        session_id, _raw_output = self._run_codex_once(params)
        source_paths = self._collect_new_images(session_id, started_at)
        if not source_paths:
            raise RuntimeError(
                f"codex session {session_id} produced no ig_*.png or exec-*.png in "
                f"{self._generated_images_dir / session_id}\n"
                "--- Codex tool output tail ---\n"
                + _diagnostic_tail(_raw_output)
            )

        image_paths = [
            self._copy_output(src, out_dir)
            for src in source_paths
        ]
        duration_ms = int((time.monotonic() - t0) * 1000)
        applied, dropped = split_feature_support(params, mode_spec.supports)
        return GenerationResult(
            image_paths=image_paths,
            model_actual=endpoint,
            duration_ms=duration_ms,
            applied_features=applied,
            request_id=session_id,
            source_urls=[str(path) for path in source_paths],
            error=None,
        )

    def _run_codex_once(self, params: dict[str, Any]) -> tuple[str, str]:
        prompt = _build_codex_prompt(params)
        timeout = int(params.get("timeout") or 300)
        reasoning = str(params.get("reasoning") or "low")
        # macOS refuses a second seatbelt application inside the host's
        # enforced sandbox. Keep that outer FS/network boundary as the sole
        # owner for managed tasks; standalone CLI use retains read-only.
        policy = json.loads(os.environ.get("ASTRID_NETWORK_POLICY") or "{}")
        broker_policy = policy.get("broker", {})
        host_sandboxed = bool(
            _managed_home()
            and os.environ.get("ASTRID_BROKER_PROXY")
            and policy.get("proxy") == os.environ.get("ASTRID_BROKER_PROXY")
            and broker_policy.get("host_managed")
            and broker_policy.get("enforced")
        )
        cmd = [
            self._codex_bin,
            "exec",
            prompt,
            "--skip-git-repo-check",
            "--sandbox",
            "danger-full-access" if host_sandboxed else "read-only",
            "-c",
            f"model_reasoning_effort={reasoning}",
        ]
        # shell_snapshot runs a login shell at start that exits while its children
        # (sed, wc, tr, a nested zsh) still run; the host's process-custody census
        # cannot prove those orphans' ancestry and fails the attempt ("census child
        # requires unadmitted absent ancestry"). A managed image run uses no shell.
        cmd.extend(["--enable", "image_generation", "--disable", "apps", "--disable", "plugins",
                    "--disable", "shell_snapshot"])
        for ref_path in _reference_paths(params):
            cmd.append(f"--image={ref_path}")
        child_env = dict(os.environ)
        managed_home = _managed_home()
        if managed_home:
            child_env["CODEX_HOME"] = managed_home
            # The OS sandbox cannot consult macOS Keychain trust services.
            # Use the system-maintained PEM roots; never disable TLS checks.
            system_roots = Path("/etc/ssl/cert.pem")
            if system_roots.is_file():
                child_env.setdefault("SSL_CERT_FILE", str(system_roots))
                child_env.setdefault("CODEX_CA_CERTIFICATE", str(system_roots))

        try:
            proc = self._runner(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                text=True,
                env=child_env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("`codex` CLI not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"codex timed out after {timeout}s") from exc

        output = proc.stdout or ""
        if proc.returncode != 0:
            raise RuntimeError(
                f"codex exited with return code {proc.returncode}\n"
                "--- codex output tail ---\n"
                + _diagnostic_tail(output)
            )
        match = SESSION_RE.search(output)
        if not match:
            raise RuntimeError(
                "could not find a Codex session id in output; image likely "
                "was not generated\n--- codex output tail ---\n"
                + output[-1500:]
            )
        return match.group(1), output

    def _collect_new_images(self, session_id: str, since: float) -> list[Path]:
        session_dir = self._generated_images_dir / session_id
        if not session_dir.is_dir():
            return []
        found = [
            path
            for path in session_dir.glob("*.png")
            if path.name.startswith(("ig_", "exec-"))
            if path.stat().st_size > 0 and path.stat().st_mtime >= since - 1
        ]
        return sorted(found, key=lambda path: path.stat().st_mtime)

    @staticmethod
    def _copy_output(src: Path, out_dir: Path) -> Path:
        index = 0
        while True:
            dst = out_dir / f"codex_{index:03d}.png"
            if not dst.exists():
                break
            index += 1
        shutil.copy2(src, dst)
        return dst


def _build_codex_prompt(params: dict[str, Any]) -> str:
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("codex backend requires a non-empty prompt")
    parts = [
        "Use ONLY your built-in image generation tool (image_generation or "
        "image_gen.imagegen, as exposed in this session) to create the image "
        "described below. Do NOT run any shell command, script, curl, python, "
        "or external CLI; do NOT use ~/.claude, run.sh, or any saved-image "
        "helper. Just call the image_generation tool directly.",
        "",
        f"Image: {prompt}",
    ]

    negative_prompt = str(params.get("negative_prompt") or "").strip()
    if negative_prompt:
        parts.extend(["", f"Avoid: {negative_prompt}"])

    references = _reference_paths(params)
    if references:
        # Describe only roles actually attached: a character-only t2i call
        # must not tell the model its character sheet is the source image.
        roles = [
            _REFERENCE_ROLE_TEXT[name]
            for name in ("image_ref", "style_ref", "brand_ref")
            if params.get(name)
        ]
        parts.extend([
            "",
            "Attached reference images, in order: "
            + "; ".join(f"{index}) {text}" for index, text in enumerate(roles, start=1))
            + ".",
        ])
        parts.append(
            f"The {len(references)} references are already attached to this conversation. "
            f"Use num_last_images_to_include={len(references)} when the tool exposes it; "
            "do not reopen the attachment files through referenced_image_paths."
        )

    hints = [
        SIZE_HINTS.get(str(params.get("size") or "auto"), ""),
        QUALITY_HINTS.get(str(params.get("quality") or "auto"), ""),
        BACKGROUND_HINTS.get(str(params.get("background") or "auto"), ""),
    ]
    hints = [hint for hint in hints if hint]
    if hints:
        parts.extend(["", " ".join(hints)])

    seed = params.get("seed")
    if seed not in (None, ""):
        parts.extend(["", f"Use seed {seed} as a variation identifier if helpful."])

    parts.extend(
        [
            "",
            "After the tool returns the image, reply with exactly the single "
            "line: GENERATED",
        ]
    )
    return "\n".join(parts)


_REFERENCE_ROLE_TEXT = {
    "image_ref": "the source image: edit this image, keeping its camera, composition "
    "and framing except where the request changes them",
    "style_ref": "the character/style reference: match its art style, and wherever "
    "the request features its character, keep that character's face, hair and clothing",
    "brand_ref": "the brand guide: follow its palette and marks",
}


def _reference_paths(params: dict[str, Any]) -> list[Path]:
    paths = []
    for name in ("image_ref", "style_ref", "brand_ref"):
        value = params.get(name)
        if value:
            path = Path(str(value)).expanduser()
            if not path.is_file():
                raise RuntimeError(f"Codex {name} must be a materialized local image")
            paths.append(path)
    return paths


def _diagnostic_tail(output: str) -> str:
    # Signed download URLs may occur in provider/plugin errors. Preserve the
    # hostname/path and tool error without leaking query credentials.
    sanitized = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<redacted>", output)
    return sanitized[-6000:]
